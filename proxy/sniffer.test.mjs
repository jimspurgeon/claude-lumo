// Run: node --test proxy/sniffer.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { errorMetaFrom, upstreamChat } from "./sniffer.mjs";

const errBody = (type, code, message = "secret prompt content") =>
  JSON.stringify({ error: { message, type, param: null, code } });
const res = (text, status = 200, type = "application/json") =>
  new Response(text, { status, headers: { "content-type": type } });

test("errorMetaFrom extracts type/code from an error payload", () => {
  const m = errorMetaFrom(errBody("no_db_connection", "400"));
  assert.deepEqual(m, { type: "no_db_connection", code: "400", isNoDb: true });
});

test("errorMetaFrom ignores non-error and malformed bodies", () => {
  for (const t of ['{"choices":[]}', "not json", "", '{"error":"flat"}', '{"ok":true}']) {
    assert.equal(errorMetaFrom(t), null, t);
  }
});

test("an SSE response streams through untouched", async () => {
  const calls = [];
  const r = await upstreamChat({
    upstream: async () => { calls.push(1); return new Response("data:{}", { headers: { "content-type": "text/event-stream" } }); },
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: () => assert.fail("no log expected"),
  });
  assert.equal(calls.length, 1);
  assert.equal(r.buffered, null);
  assert.equal(r.errMeta, null);
  assert.ok((await r.response.text()).includes("data:"));
});

test("a non-chat path is passed straight through", async () => {
  const r = await upstreamChat({ upstream: async () => res('{"models":[]}'), method: "GET", path: "/models", body: undefined, headers: {}, log: () => {} });
  assert.equal(r.buffered, null);
  assert.equal(r.errMeta, null);
});

test("a non-no_db error is logged and returned, not retried", async () => {
  const lines = [];
  let calls = 0;
  const r = await upstreamChat({
    upstream: async () => { calls++; return res(errBody("rate_limited", "429")); },
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: (m) => lines.push(m),
    sleep: () => assert.fail("no sleep expected"),
  });
  assert.equal(calls, 1);
  assert.deepEqual(lines, ["/chat/completions upstream error 200 type=rate_limited code=429"]);
  assert.equal(r.errMeta.type, "rate_limited");
  assert.equal(await r.response.text(), errBody("rate_limited", "429"));
  assert.equal(r.buffered, errBody("rate_limited", "429"));
});

test("no_db_connection is retried once after a backoff; a success then streams normally", async () => {
  const lines = [];
  let calls = 0, slept = 0;
  const r = await upstreamChat({
    upstream: async () => { calls++; return calls === 1 ? res(errBody("no_db_connection", "400")) : new Response("data:{}", { headers: { "content-type": "text/event-stream" } }); },
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: (m) => lines.push(m),
    sleep: async (ms) => { slept++; assert.equal(ms, 1500); },
  });
  assert.equal(calls, 2);
  assert.equal(slept, 1);
  assert.match(lines[0], /type=no_db_connection code=400; retrying once/);
  assert.equal(r.retried, true);
  assert.equal(r.errMeta, null); // final response is the good one
  assert.equal(r.buffered, null);
  assert.ok((await r.response.text()).includes("data:"));
});

test("no_db_connection failing twice is logged and returned to the client, never retried a third time", async () => {
  const lines = [];
  let calls = 0;
  const r = await upstreamChat({
    upstream: async () => { calls++; return res(errBody("no_db_connection", "400")); },
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: (m) => lines.push(m), sleep: async () => {},
  });
  assert.equal(calls, 2);
  assert.match(lines[0], /retrying once/);
  assert.match(lines[1], /^\/chat\/completions upstream error 200 type=no_db_connection code=400$/);
  assert.equal(r.retried, true);
  assert.equal(r.buffered, errBody("no_db_connection", "400"));
});

test("persistent no_db_connection is retried once more with prompt_cache_key stripped (issue #60)", async () => {
  const lines = [];
  const bodies = [];
  let calls = 0, slept = 0;
  const key = "sess-" + "k".repeat(20);
  const reqBody = JSON.stringify({ model: "lumo-max", prompt_cache_key: key, user: key, messages: [{ role: "user", content: "hi" }] });
  const r = await upstreamChat({
    upstream: async (_p, _m, b) => {
      calls++;
      bodies.push(b);
      if (calls <= 2) return res(errBody("no_db_connection", "400"));
      return new Response("data:{}", { headers: { "content-type": "text/event-stream" } });
    },
    method: "POST", path: "/chat/completions", body: reqBody, headers: {}, log: (m) => lines.push(m),
    sleep: async (ms) => { slept++; assert.equal(ms, 1500); },
  });
  assert.equal(calls, 3);
  assert.equal(slept, 2);
  assert.deepEqual(bodies.slice(0, 2), [reqBody, reqBody]); // first two sent unchanged
  assert.notEqual(bodies[2], reqBody); // third is the stripped variant
  const stripped = JSON.parse(bodies[2]);
  assert.ok(!("prompt_cache_key" in stripped));
  assert.ok(!("user" in stripped)); // mirrored user removed with the key
  assert.equal(stripped.model, "lumo-max");
  assert.match(lines[0], /type=no_db_connection code=400; retrying once in 1.5s/);
  assert.match(lines[1], /type=no_db_connection code=400; retrying once with prompt_cache_key stripped/);
  assert.equal(r.retried, true);
  assert.equal(r.errMeta, null); // stripped retry succeeded
  assert.equal(r.buffered, null);
  assert.ok((await r.response.text()).includes("data:"));
});

test("a `user` field that does not match prompt_cache_key survives the strip", async () => {
  const bodies = [];
  let calls = 0;
  const reqBody = JSON.stringify({ prompt_cache_key: "abc", user: "someone-else", messages: [] });
  await upstreamChat({
    upstream: async (_p, _m, b) => {
      bodies.push(b);
      calls++;
      return calls <= 2 ? res(errBody("no_db_connection", "400")) : new Response("data:{}", { headers: { "content-type": "text/event-stream" } });
    },
    method: "POST", path: "/chat/completions", body: reqBody, headers: {}, log: () => {}, sleep: async () => {},
  });
  const stripped = JSON.parse(bodies[2]);
  assert.equal(stripped.user, "someone-else");
  assert.ok(!("prompt_cache_key" in stripped));
});

test("persistent no_db_connection with no cache key stops at two attempts", async () => {
  const bodies = [];
  let calls = 0;
  const reqBody = JSON.stringify({ model: "lumo-max", messages: [] });
  const r = await upstreamChat({
    upstream: async (_p, _m, b) => {
      bodies.push(b);
      calls++;
      return res(errBody("no_db_connection", "400"));
    },
    method: "POST", path: "/chat/completions", body: reqBody, headers: {}, log: () => {}, sleep: async () => {},
  });
  assert.equal(calls, 2);
  assert.equal(r.errMeta.isNoDb, true);
});

test("a non-JSON body with persistent no_db_connection stops at two attempts", async () => {
  let calls = 0;
  const r = await upstreamChat({
    upstream: async () => { calls++; return res(errBody("no_db_connection", "400")); },
    method: "POST", path: "/chat/completions", body: "not-json", headers: {}, log: () => {}, sleep: async () => {},
  });
  assert.equal(calls, 2);
  assert.equal(r.errMeta.isNoDb, true);
});

test("a third no_db_connection after the stripped retry is returned to the client", async () => {
  const lines = [];
  let calls = 0;
  const reqBody = JSON.stringify({ prompt_cache_key: "k", messages: [] });
  const r = await upstreamChat({
    upstream: async () => { calls++; return res(errBody("no_db_connection", "400")); },
    method: "POST", path: "/chat/completions", body: reqBody, headers: {}, log: (m) => lines.push(m), sleep: async () => {},
  });
  assert.equal(calls, 3);
  assert.equal(lines.length, 3);
  assert.match(lines[1], /retrying once with prompt_cache_key stripped/);
  assert.match(lines[2], /^\/chat\/completions upstream error 200 type=no_db_connection code=400$/);
  assert.equal(r.errMeta.isNoDb, true);
  assert.equal(r.buffered, errBody("no_db_connection", "400"));
});

test("logged lines contain type/code but never the error message content", async () => {
  const lines = [];
  await upstreamChat({
    upstream: async () => res(errBody("no_db_connection", "400", "No connected db.")),
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: (m) => lines.push(m), sleep: async () => {},
  });
  await upstreamChat({
    upstream: async () => res(errBody("no_db_connection", "400", "No connected db."), 500),
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: (m) => lines.push(m), sleep: async () => {},
  });
  const joined = lines.join("\n");
  assert.ok(!joined.includes("No connected db."));
  assert.ok(!joined.includes("secret"));
});

test("an HTTP-4xx non-error-body response keeps its existing status log line", async () => {
  const lines = [];
  const r = await upstreamChat({
    upstream: async () => res('{"detail":"bad"}', 404),
    method: "POST", path: "/chat/completions", body: "{}", headers: {}, log: (m) => lines.push(m),
  });
  assert.deepEqual(lines, ["/chat/completions -> 404"]);
  assert.equal(r.response.status, 404);
});

// Lumo's 131072-token window covers prompt + reply; a context-length 400 is retried once
// through the refit hook (#65). Message text is the real one, measured 2026-10-13.
import { createContextFit } from "./context-fit.mjs";

const ctxErr = (requested, input) => JSON.stringify({ error: {
  message: `This model's maximum context length is 131072 tokens. However, you requested ${requested} output tokens and your prompt contains at least ${input} input tokens, for a total of at least ${requested + input} tokens. Please reduce the length of the input prompt or the number of requested output tokens. (parameter=input_tokens, value=${input})`,
  type: "BadRequestError", param: "input_tokens", code: 400 } });
const sseOk = () => new Response("data:{}", { headers: { "content-type": "text/event-stream" } });
// A body whose size makes the default estimate land near `tokens`.
const sized = (tokens, extra = {}) => JSON.stringify({ model: "lumo-max", max_tokens: 32000, ...extra, pad: "x".repeat(Math.round(tokens * 3.65)) });

test("a context-length 400 is retried once with a smaller max_tokens, then streams", async () => {
  const sent = [], logs = [];
  const r = await upstreamChat({
    upstream: async (_p, _m, body) => { sent.push(JSON.parse(body)); return sent.length === 1 ? res(ctxErr(32000, 99073), 400) : sseOk(); },
    method: "POST", path: "/chat/completions", body: sized(100000), headers: {}, log: (l) => logs.push(l), refit: createContextFit().refit,
  });
  assert.equal(sent.length, 2);
  assert.ok(sent[1].max_tokens < 32000 && sent[1].max_tokens >= 1024, String(sent[1].max_tokens));
  assert.equal(r.buffered, null);
  assert.equal(r.retried, true);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /context_refit est=\d+ max_tokens 32000->\d+; retrying once/);
  assert.doesNotMatch(logs[0], /maximum context length/);
});

test("without a refit hook a context-length 400 is returned as before", async () => {
  let n = 0;
  const r = await upstreamChat({ upstream: async () => { n++; return res(ctxErr(32000, 99073), 400); }, method: "POST", path: "/chat/completions", body: sized(100000), headers: {}, log: () => {} });
  assert.equal(n, 1);
  assert.equal(r.response.status, 400);
});

test("the context refit is tried only once", async () => {
  let n = 0;
  const r = await upstreamChat({
    upstream: async () => { n++; return res(ctxErr(32000, 99073), 400); },
    method: "POST", path: "/chat/completions", body: sized(100000), headers: {}, log: () => {}, refit: createContextFit().refit,
  });
  assert.equal(n, 2);
  assert.equal(r.response.status, 400);
});

test("no_db_connection after a context refit still gets both of its retries", async () => {
  const sent = [];
  await upstreamChat({
    upstream: async (_p, _m, body) => { sent.push(JSON.parse(body)); return sent.length === 1 ? res(ctxErr(32000, 99073), 400) : res(errBody("no_db_connection", "400")); },
    method: "POST", path: "/chat/completions", body: sized(100000, { prompt_cache_key: "k" }), headers: {}, log: () => {},
    sleep: async () => {}, refit: createContextFit().refit,
  });
  assert.equal(sent.length, 4, "refit, plain retry, stripped retry");
  assert.equal("prompt_cache_key" in sent[3], false);
  assert.equal(sent[3].max_tokens, sent[1].max_tokens);
});
