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
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /not available/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /no real search was performed/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /WebFetch/);
  assert.match(WEBSEARCH_UNAVAILABLE_TEXT, /Do NOT/i);
});
