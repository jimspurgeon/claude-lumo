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
import { answerText, chatResponse, citesSources, LUMO_SEARCH_TOOLS, parseSearchResponse, runLumoSearch, searchRan, SEARCH_TOKEN_MARGIN, toLumoSearchRequest } from "./websearch-guard.mjs";

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

test("answerText reads a stream that starts with an SSE comment (Lumo sends ':ingesting')", () => {
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

// Search evidence: a URL alone is not proof (Lumo can answer from memory with tool_choice auto).
const sseWith = (text, promptTokens, extra = []) => [":ingesting", "",
  ...extra.map((o) => "data:" + JSON.stringify(o)),
  "data:" + JSON.stringify({ choices: [{ delta: { content: text } }] }),
  "data:" + JSON.stringify({ choices: [], usage: { prompt_tokens: promptTokens } }),
  "data:[DONE]", ""].join("\n");

test("parseSearchResponse reads usage and web_search tool chunks", () => {
  const p = parseSearchResponse(sseWith("t", 4339, [{ object: "chat.tool_call", tool_call: { name: "web_search" } }, { object: "chat.tool_result" }]));
  assert.deepEqual(p, { text: "t", promptTokens: 4339, toolCall: true, toolResult: true });
  assert.equal(parseSearchResponse(JSON.stringify({ choices: [{ message: { content: "x" } }], usage: { prompt_tokens: 9 } })).promptTokens, 9);
});

test("searchRan needs prompt tokens well beyond the request, or a tool_call/result pair", () => {
  // Measured 2026-10-13: 433 prompt tokens without search, 4,339 to 6,943 with it, for a ~700 B request.
  assert.equal(searchRan({ promptTokens: 433 }, 700), false);
  assert.equal(searchRan({ promptTokens: 4339 }, 700), true);
  assert.equal(searchRan({ promptTokens: null }, 700), false);
  assert.equal(searchRan({ promptTokens: 4339 }, 30000), false, "a large request explains its own tokens");
  assert.equal(searchRan({ promptTokens: Math.ceil(30000 / 3) + SEARCH_TOKEN_MARGIN }, 30000), true);
  assert.equal(searchRan({ promptTokens: null, toolCall: true, toolResult: true }, 700), true);
  assert.equal(searchRan({ promptTokens: null, toolCall: true, toolResult: false }, 700), false);
});

const fakeChat = (respond) => async ({ body }) => respond(JSON.parse(body));
const sseResponse = (raw) => ({ response: new Response(raw, { status: 200, headers: { "content-type": "text/event-stream" } }), buffered: null, errMeta: null });
const run = (upstreamChat, extra = {}) => runLumoSearch({ body: JSON.stringify(cap6), path: "/chat/completions", upstream: null, upstreamChat, log: () => {}, ...extra });

test("runLumoSearch: ok only with search evidence and a URL", async () => {
  let sent;
  const r = await run(fakeChat((b) => { sent = b; return sseResponse(sseWith("Node 26\nSources:\nhttps://nodejs.org/en/blog", 5036)); }));
  assert.deepEqual(r, { outcome: "ok", text: "Node 26\nSources:\nhttps://nodejs.org/en/blog" });
  assert.deepEqual(sent.tools, [{ name: "web_search" }]);
  assert.equal("web_search_options" in sent, false);
});

test("runLumoSearch: a memory answer with URLs but no search is refused", async () => {
  assert.deepEqual(await run(fakeChat(() => sseResponse(sseWith("from memory https://nodejs.org", 433)))), { outcome: "no_search", text: "" });
});

test("runLumoSearch: a search without a cited URL is refused", async () => {
  assert.deepEqual(await run(fakeChat(() => sseResponse(sseWith("nothing found", 5000)))), { outcome: "no_sources", text: "" });
});

test("runLumoSearch: upstream error payloads, HTTP errors and exceptions map to outcomes", async () => {
  assert.deepEqual(await run(fakeChat(() => ({ response: new Response("{}", { status: 200 }), buffered: "{}", errMeta: { type: "no_db_connection", code: "400" } }))), { outcome: "err_no_db_connection", text: "" });
  assert.deepEqual(await run(fakeChat(() => ({ response: new Response("x", { status: 502 }), buffered: "x", errMeta: null }))), { outcome: "http_502", text: "" });
  assert.deepEqual(await run(async () => { throw new TypeError("boom"); }), { outcome: "error_TypeError", text: "" });
  assert.deepEqual(await runLumoSearch({ body: "not json", path: "/chat/completions", upstreamChat: async () => { throw new Error("unreached"); }, log: () => {} }), { outcome: "error_SyntaxError", text: "" });
});

test("runLumoSearch: a stalled stream ends as timeout", async () => {
  const stalled = new ReadableStream({ start() {} }); // never sends, never closes
  const r = await run(fakeChat(() => ({ response: new Response(stalled, { status: 200, headers: { "content-type": "text/event-stream" } }), buffered: null, errMeta: null })), { timeoutMs: 50 });
  assert.deepEqual(r, { outcome: "timeout", text: "" });
});
