// Run: node --test proxy/websearch-guard.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { isWebSearchRequest, webSearchUnavailable, WEBSEARCH_UNAVAILABLE_TEXT, extractSearchQuery, formatBraveResults, braveWebSearch, webSearchResponse } from "./websearch-guard.mjs";

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
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /not available/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /no real search was performed/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /WebFetch/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /Do NOT/i);
});

test("extractSearchQuery pulls the query from a Claude Code prompt (colon variant)", () => {
  const body = JSON.stringify({
    model: "lumo-max",
    web_search_options: {},
    messages: [{ role: "user", content: "Perform a web search for the query: climate change effects 2024" }]
  });
  assert.equal(extractSearchQuery(body), "climate change effects 2024");
});

test("extractSearchQuery pulls the query from a Claude Code prompt (Chinese colon variant)", () => {
  const body = JSON.stringify({
    model: "lumo-max",
    web_search_options: {},
    messages: [{ role: "user", content: "Perform a web search for the query：机器学习趋势" }]
  });
  assert.equal(extractSearchQuery(body), "机器学习趋势");
});

test("extractSearchQuery extracts from the last user message when mixed with system/assistant", () => {
  const body = JSON.stringify({
    model: "lumo-max",
    web_search_options: {},
    messages: [
      { role: "system", content: "You are a helpful assistant." },
      { role: "user", content: "Hello" },
      { role: "assistant", content: "Hi there!" },
      { role: "user", content: "Perform a web search for the query: latest NASA Artemis updates" }
    ]
  });
  assert.equal(extractSearchQuery(body), "latest NASA Artemis updates");
});

test("extractSearchQuery handles OpenAI-style content parts", () => {
  const body = JSON.stringify({
    model: "lumo-max",
    web_search_options: {},
    messages: [{
      role: "user",
      content: [
        { type: "text", text: "Perform a web search for the query: quantum computing breakthroughs" },
        { type: "image_url", url: "https://example.com/img.png" }
      ]
    }]
  });
  assert.equal(extractSearchQuery(body), "quantum computing breakthroughs");
});

test("extractSearchQuery returns trimmed full message when no query pattern", () => {
  const body = JSON.stringify({
    model: "lumo-max",
    web_search_options: {},
    messages: [{ role: "user", content: "  search space exploration  " }]
  });
  assert.equal(extractSearchQuery(body), "search space exploration");
});

test("extractSearchQuery returns empty string on malformed or missing data", () => {
  for (const body of ["not json", "", JSON.stringify({}), JSON.stringify({ messages: [] }), JSON.stringify({ messages: [{ role: "assistant" }] })]) {
    assert.equal(extractSearchQuery(body), "", body);
  }
});

test("formatBraveResults produces a numbered list with title, snippet, URL", () => {
  const mockResults = [
    { title: "Result 1", description: "Snippet one", url: "https://example.com/1" },
    { title: "Result 2", description: "Snippet two", url: "https://example.com/2" }
  ];
  const out = formatBraveResults("test query", mockResults);
  assert.match(out, /\[claude-lumo\] Web search results for "test query"/);
  assert.match(out, /1\. Result 1/);
  assert.match(out, /Snippet one/);
  assert.match(out, /https:\/\/example\.com\/1/);
  assert.match(out, /2\. Result 2/);
});

test("formatBraveResults limits to 5 results", () => {
  const mockResults = Array.from({ length: 10 }, (_, i) => ({
    title: `R${i + 1}`,
    description: `Desc ${i + 1}`,
    url: `https://example.com/${i + 1}`
  }));
  const out = formatBraveResults("query", mockResults);
  assert.equal((out.match(/https:\/\/example\.com\/\d/g) || []).length, 5);
});

test("formatBraveResults filters out results without URLs", () => {
  const mockResults = [
    { title: "Valid", description: "Has URL", url: "https://valid.com" },
    { title: "NoURL", description: "Missing URL" },
    { title: "Also Valid", description: "Another", url: "https://also.com" }
  ];
  const out = formatBraveResults("q", mockResults);
  assert.match(out, /1\. Valid/);
  assert.ok(!out.includes("NoURL"));
  assert.match(out, /2\. Also Valid/);
});

test("formatBraveResults returns empty string when no results", () => {
  assert.equal(formatBraveResults("query", []), "");
  assert.equal(formatBraveResults("query", [{}]), "");
});

test("braveWebSearch returns empty string when no API key", async () => {
  const orig = process.env.LUMO_SEARCH_API_KEY;
  delete process.env.LUMO_SEARCH_API_KEY;
  assert.equal(await braveWebSearch("test query"), "");
  if (orig) process.env.LUMO_SEARCH_API_KEY = orig;
  else delete process.env.LUMO_SEARCH_API_KEY;
});

test("braveWebSearch returns empty string when no query", async () => {
  const orig = process.env.LUMO_SEARCH_API_KEY;
  process.env.LUMO_SEARCH_API_KEY = "fake-key";
  assert.equal(await braveWebSearch(""), "");
  assert.equal(await braveWebSearch(null), "");
  if (orig) process.env.LUMO_SEARCH_API_KEY = orig;
  else delete process.env.LUMO_SEARCH_API_KEY;
});

test("braveWebSearch returns empty string when the API rejects (bad key / 4xx)", async () => {
  const origKey = process.env.LUMO_SEARCH_API_KEY;
  const origFetch = globalThis.fetch;
  process.env.LUMO_SEARCH_API_KEY = "invalid_key_test_12345";
  globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({}) });
  try {
    // Should return "" on auth error, not throw
    assert.equal(await braveWebSearch("test query"), "");
  } finally {
    globalThis.fetch = origFetch;
    if (origKey) process.env.LUMO_SEARCH_API_KEY = origKey; else delete process.env.LUMO_SEARCH_API_KEY;
  }
});

test("braveWebSearch returns empty string when fetch throws (network error)", async () => {
  const origKey = process.env.LUMO_SEARCH_API_KEY;
  const origFetch = globalThis.fetch;
  process.env.LUMO_SEARCH_API_KEY = "any-key";
  globalThis.fetch = async () => { throw new Error("ECONNREFUSED"); };
  try {
    assert.equal(await braveWebSearch("test query"), "");
  } finally {
    globalThis.fetch = origFetch;
    if (origKey) process.env.LUMO_SEARCH_API_KEY = origKey; else delete process.env.LUMO_SEARCH_API_KEY;
  }
});

test("webSearchResponse calls the Brave API correctly and formats live results (stream=false)", async () => {
  const origKey = process.env.LUMO_SEARCH_API_KEY;
  const origFetch = globalThis.fetch;
  process.env.LUMO_SEARCH_API_KEY = "test-token";
  let calledUrl = "", calledHeaders = null;
  globalThis.fetch = async (url, opts) => {
    calledUrl = url; calledHeaders = opts.headers;
    return {
      ok: true,
      json: async () => ({
        web: {
          results: [
            { title: "Example Page", description: "An example snippet.", url: "https://example.com/page" },
            { title: "Another Page", description: "Second snippet.", url: "https://example.org/other" }
          ]
        }
      })
    };
  };
  try {
    const body = JSON.stringify({ model: "lumo-max", stream: false, web_search_options: {}, messages: [{ role: "user", content: "Perform a web search for the query: hello world" }] });
    const out = await webSearchResponse("lumo-max", false, body);
    // Correct endpoint, params and header
    assert.equal(calledUrl, "https://api.search.brave.com/res/v1/web/search?q=hello%20world&count=5&safesearch=off");
    assert.equal(calledHeaders["X-Subscription-Token"], "test-token");
    // Valid plain JSON completion carrying the formatted results
    const d = JSON.parse(out);
    assert.equal(d.choices[0].finish_reason, "stop");
    assert.match(d.choices[0].message.content, /1\. Example Page/);
    assert.match(d.choices[0].message.content, /2\. Another Page/);
    assert.match(d.choices[0].message.content, /https:\/\/example\.com\/page/);
  } finally {
    globalThis.fetch = origFetch;
    if (origKey) process.env.LUMO_SEARCH_API_KEY = origKey; else delete process.env.LUMO_SEARCH_API_KEY;
  }
});

test("webSearchResponse streams live results ending in [DONE] (stream=true)", async () => {
  const origKey = process.env.LUMO_SEARCH_API_KEY;
  const origFetch = globalThis.fetch;
  process.env.LUMO_SEARCH_API_KEY = "test-token";
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ web: { results: [{ title: "Solo", description: "Only hit.", url: "https://one.example" }] } })
  });
  try {
    const body = JSON.stringify({ model: "lumo-max", stream: true, web_search_options: {}, messages: [{ role: "user", content: "Perform a web search for the query: lone query" }] });
    const out = await webSearchResponse("lumo-max", true, body);
    const lines = out.split("\n").filter((l) => l.startsWith("data:"));
    assert.equal(lines.at(-1), "data:[DONE]");
    const events = lines.slice(0, -1).map((l) => JSON.parse(l.slice(5)));
    const text = events.map((e) => e.choices[0].delta.content || "").join("");
    assert.match(text, /1\. Solo/);
    assert.equal(events.at(-1).choices[0].finish_reason, "stop");
  } finally {
    globalThis.fetch = origFetch;
    if (origKey) process.env.LUMO_SEARCH_API_KEY = origKey; else delete process.env.LUMO_SEARCH_API_KEY;
  }
});

test("webSearchResponse falls back to unavailability when Brave API errors", async () => {
  const origKey = process.env.LUMO_SEARCH_API_KEY;
  const origFetch = globalThis.fetch;
  process.env.LUMO_SEARCH_API_KEY = "test-token";
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}) });
  try {
    const body = JSON.stringify({ model: "lumo-max", web_search_options: {}, messages: [{ role: "user", content: "Perform a web search for the query: anything" }] });
    const out = await webSearchResponse("lumo-max", false, body);
    assert.equal(JSON.parse(out).choices[0].message.content, WEBSEARCH_UNAVAILABLE_TEXT);
  } finally {
    globalThis.fetch = origFetch;
    if (origKey) process.env.LUMO_SEARCH_API_KEY = origKey; else delete process.env.LUMO_SEARCH_API_KEY;
  }
});

test("webSearchResponse returns unavailability text when no API key (stream=true)", async () => {
  const body = JSON.stringify({ model: "lumo-max", stream: true, web_search_options: {}, messages: [{ role: "user", content: "Perform a web search for the query: test" }] });
  // Mock by temporarily overriding env to force empty results
  const origKey = process.env.LUMO_SEARCH_API_KEY;
  delete process.env.LUMO_SEARCH_API_KEY;
  const out = await webSearchResponse("lumo-max", true, body);
  if (origKey) process.env.LUMO_SEARCH_API_KEY = origKey;
  const lines = out.split("\n").filter((l) => l.startsWith("data:"));
  assert.equal(lines.at(-1), "data:[DONE]");
  assert.match(out, new RegExp(WEBSEARCH_UNAVAILABLE_TEXT.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
});
