// Run: node --test proxy/websearch-guard.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { isWebSearchRequest, webSearchUnavailable, WEBSEARCH_UNAVAILABLE_TEXT } from "./websearch-guard.mjs";

test("isWebSearchRequest detects the internal WebSearch delegation (cap-6 shape)", () => {
  assert.equal(isWebSearchRequest(JSON.stringify({ model: "lumo-max", stream: true, web_search_options: {}, messages: [] })), true);
});

test("isWebSearchRequest ignores ordinary chat requests (cap-5/cap-7 shape)", () => {
  for (const body of [
    JSON.stringify({ model: "lumo-max", stream: true, tools: [{ type: "function", function: { name: "WebSearch" } }], messages: [] }),
    JSON.stringify({ model: "lumo-max", messages: [] }),
    JSON.stringify(["not", "an", "object"]),
    "not json",
    "",
  ]) assert.equal(isWebSearchRequest(body), false, body);
  assert.equal(isWebSearchRequest(undefined), false);
});

test("the streamed unavailability response is valid SSE ending in [DONE]", () => {
  const out = webSearchUnavailable("lumo-max", true);
  const lines = out.split("\n").filter((l) => l.startsWith("data:"));
  assert.equal(lines.at(-1), "data:[DONE]");
  const events = lines.slice(0, -1).map((l) => JSON.parse(l.slice(5)));
  const text = events.map((e) => e.choices[0].delta.content || "").join("");
  assert.equal(text, WEBSEARCH_UNAVAILABLE_TEXT);
  assert.equal(events.at(-1).choices[0].finish_reason, "stop");
  for (const e of events) assert.equal(e.model, "lumo-max");
});

test("the non-streamed unavailability response carries the same message", () => {
  const d = JSON.parse(webSearchUnavailable("lumo-max", false));
  assert.equal(d.choices[0].message.content, WEBSEARCH_UNAVAILABLE_TEXT);
  assert.equal(d.choices[0].finish_reason, "stop");
});

test("the message tells the model not to fabricate and points at WebFetch", () => {
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /did not return a usable result/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /no real search result is available/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /WebFetch/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /Do NOT/i);
});

// Issue #73: the side-request runs on Lumo's built-in web search instead.
import { answerText, chatResponse, citesSources, LUMO_SEARCH_TOOLS, toLumoSearchRequest } from "./websearch-guard.mjs";

const cap6 = { model: "lumo-lite", stream: false, max_tokens: 4096, web_search_options: {},
  messages: [{ role: "system", content: "You are an assistant for performing a web search tool use" },
             { role: "user", content: "Perform a web search for the query: node.js latest release" }] };

test("toLumoSearchRequest swaps web_search_options for Lumo's web_search tool", () => {
  const d = JSON.parse(toLumoSearchRequest(JSON.stringify(cap6), new Date("2026-10-13T12:00:00Z")));
  assert.equal("web_search_options" in d, false);
  assert.deepEqual(d.tools, [{ name: "web_search" }]);
  assert.deepEqual(d.tools, LUMO_SEARCH_TOOLS);
  assert.equal(d.tool_choice, "auto");
  assert.equal(d.stream, true);
  assert.equal(d.model, "lumo-lite");
  assert.equal(d.max_tokens, 4096);
  assert.equal(d.messages.length, 2);
  assert.match(d.messages[0].content, /^Today's date is 2026-10-13\. Use the web_search tool/);
  assert.match(d.messages[0].content, /Sources:/);
  assert.match(d.messages[0].content, /You are an assistant for performing a web search tool use$/);
  assert.equal(d.messages[1].content, cap6.messages[1].content);
  assert.equal(isWebSearchRequest(JSON.stringify(d)), false, "the rewritten request is not caught again");
});

test("toLumoSearchRequest adds a system message when there is none (or it is not a string)", () => {
  for (const messages of [[{ role: "user", content: "q" }],
                          [{ role: "system", content: [{ type: "text", text: "s" }] }, { role: "user", content: "q" }]]) {
    const d = JSON.parse(toLumoSearchRequest(JSON.stringify({ model: "m", web_search_options: {}, messages })));
    assert.equal(d.messages.length, messages.length + 1);
    assert.equal(d.messages[0].role, "system");
    assert.match(d.messages[0].content, /web_search/);
  }
});

test("answerText joins SSE content deltas and skips reasoning, [DONE] and bad lines", () => {
  const sse = [
    'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
    'data:{"choices":[{"delta":{"role":"assistant","content":"Node "}}]}',
    "data: not json",
    'data: {"choices":[{"delta":{"content":"26.11.1\\nSources:\\nhttps://nodejs.org/en/blog"}}]}',
    "data: [DONE]", ""].join("\n");
  assert.equal(answerText(sse), "Node 26.11.1\nSources:\nhttps://nodejs.org/en/blog");
});

test("answerText reads a stream that starts with Lumo's ':ingesting' comment (search ran)", () => {
  const sse = ':ingesting\n\ndata:{"choices":[{"delta":{"content":"a https://x.org/b"}}]}\n\ndata:[DONE]\n\n';
  assert.equal(answerText(sse), "a https://x.org/b");
});

test("answerText reads a plain completion and tolerates garbage", () => {
  assert.equal(answerText(JSON.stringify({ choices: [{ message: { content: "hi" } }] })), "hi");
  for (const raw of ["", "not json", '{"error":{"type":"no_db_connection"}}', undefined]) assert.equal(answerText(raw), "");
});

test("citesSources needs a real http(s) URL", () => {
  assert.equal(citesSources("Sources:\nhttps://www.bbc.com/news/articles/x"), true);
  assert.equal(citesSources("see (http://example.org/a)"), true);
  for (const t of ["", "no sources here", "https://", "visit bbc.com", "ftp://x.org/f", undefined]) assert.equal(citesSources(t), false, String(t));
});

test("chatResponse carries the search answer in both shapes", () => {
  const text = "answer\nSources:\nhttps://nodejs.org";
  const lines = chatResponse("lumo-lite", true, text).split("\n").filter((l) => l.startsWith("data:"));
  assert.equal(lines.at(-1), "data:[DONE]");
  assert.equal(lines.slice(0, -1).map((l) => JSON.parse(l.slice(5)).choices[0].delta.content || "").join(""), text);
  assert.equal(JSON.parse(chatResponse("lumo-lite", false, text)).choices[0].message.content, text);
});
