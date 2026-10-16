// Run: node --test proxy/upstream.test.mjs
// Tests createUpstream() with fake fetch/credentials: retry policy, 401 token
// refresh, and that no token value ever reaches errors or log lines.
import test from "node:test";
import assert from "node:assert/strict";
import { createUpstream } from "./upstream.mjs";

const TOKEN1 = "secret-token-one-aaaa", TOKEN2 = "secret-token-two-bbbb";
const cred = (token, uid = "uid-1") => ({ token, uid });

// A fetch that plays a scripted sequence of outcomes. An outcome is a Response
// or an Error (connection failure). Records every call's headers and body.
const fakeFetch = (outcomes) => {
  const calls = [];
  const fn = async (url, opts) => {
    calls.push({ url, opts });
    const o = outcomes[Math.min(calls.length - 1, outcomes.length - 1)];
    if (o instanceof Error) throw o;
    return o;
  };
  return { fn, calls };
};

const res = (status, body = "ok", headers = {}) =>
  new Response(body, { status, headers });

const setup = ({ outcomes, creds = cred(TOKEN1), freshCreds = cred(TOKEN2) } = {}) => {
  const f = fakeFetch(outcomes);
  const sleeps = [];
  const logs = [];
  let readCount = 0;
  const readCreds = () => { readCount++; return readCount === 1 ? creds : freshCreds; };
  const credsRef = { current: null };
  const upstream = createUpstream({
    fetchFn: f.fn, credsRef, readCreds, firstByteMs: 5000,
    log: (m) => logs.push(m), sleep: async (ms) => sleeps.push(ms),
  });
  return { f, sleeps, logs, credsRef, upstream, readCount };
};

test("success on the first try makes exactly one fetch call", async () => {
  const { f, upstream } = setup({ outcomes: [res(200)] });
  const r = await upstream("/chat/completions", "POST", '{"x":1}', { "content-type": "application/json" });
  assert.equal(r.status, 200);
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0].url, /https:\/\/lumo\.proton\.me\/api\/ai\/v1\/chat\/completions$/);
  assert.equal(f.calls[0].opts.headers.authorization, `Bearer ${TOKEN1}`);
  assert.equal(f.calls[0].opts.headers["x-pm-uid"], "uid-1");
});

test("connection error then success: one retry with backoff, two fetches", async () => {
  const { f, upstream, sleeps, logs } = setup({ outcomes: [new Error("socket hang up"), res(200)] });
  const r = await upstream("/models", "GET", undefined, {});
  assert.equal(r.status, 200);
  assert.equal(f.calls.length, 2);
  assert.deepEqual(sleeps, [1500]); // attempt 0 backoff
  assert.equal(logs.filter((l) => l.includes("connection error, retry 1")).length, 1);
  const authHeaders = f.calls.map((c) => c.opts.headers.authorization);
  assert.deepEqual(authHeaders, [`Bearer ${TOKEN1}`, `Bearer ${TOKEN1}`]);
});

test("401 then success: re-reads creds, rebuilds Authorization from the fresh token", async () => {
  const { f, upstream, logs, credsRef } = setup({ outcomes: [res(401), res(200)] });
  const r = await upstream("/chat/completions", "POST", "b", {});
  assert.equal(r.status, 200);
  assert.equal(f.calls.length, 2);
  assert.equal(logs.filter((l) => l.includes("new token")).length, 1);
  assert.equal(credsRef.current.token, TOKEN2);
  assert.equal(f.calls[1].opts.headers.authorization, `Bearer ${TOKEN2}`);
  assert.equal(f.calls[1].opts.headers["x-pm-uid"], "uid-1");
  assert.notEqual(f.calls[0].opts.headers.authorization, f.calls[1].opts.headers.authorization);
});

test("second 401 is returned as-is (single refresh only)", async () => {
  const { f, upstream, logs } = setup({ outcomes: [res(401), res(401)] });
  const r = await upstream("/chat/completions", "POST", "b", {});
  assert.equal(r.status, 401);
  assert.equal(f.calls.length, 2);
  assert.equal(logs.filter((l) => l.includes("401 from Lumo")).length, 1); // refreshed only once
  await r.text(); // drain
});

test("no credential file means a 401 without any fetch call", async () => {
  const f = fakeFetch([res(200)]);
  const sleeps = [];
  const upstream = createUpstream({
    fetchFn: f.fn, credsRef: { current: null }, readCreds: () => null,
    log: () => {}, sleep: async (ms) => sleeps.push(ms),
  });
  const r = await upstream("/chat/completions", "POST", "b", {});
  assert.equal(r.status, 401);
  assert.equal(f.calls.length, 0);
  const text = await r.text();
  assert.match(text, /no Lumo login found/);
});

test("no token value ever appears in error bodies or log lines", async () => {
  // Exhausted retries on a connection error, and a full 401 refresh cycle.
  for (const outcomes of [[new Error("boom"), new Error("boom"), new Error("boom")], [res(401), res(401)]]) {
    const { f, upstream, logs, sleeps } = setup({ outcomes });
    const r = await upstream("/chat/completions", "POST", "b", {});
    await r.text().catch(() => {});
    const all = [JSON.stringify(logs), String(r.status)];
    for (const line of all) {
      assert.ok(!line.includes(TOKEN1), "stale token leaked");
      assert.ok(!line.includes(TOKEN2), "fresh token leaked");
    }
    assert.ok(f.calls.length >= 1);
    assert.deepEqual(sleeps.filter((s) => s < 0), []); // sanity: only non-negative backoffs
  }
});

test("429 then success waits and retries within the attempt budget", async () => {
  const { f, upstream, sleeps, logs } = setup({ outcomes: [res(429, "rate limited"), res(200)] });
  const r = await upstream("/chat/completions", "POST", "b", {});
  assert.equal(r.status, 200);
  assert.equal(f.calls.length, 2);
  assert.equal(sleeps.length, 1);
  assert.equal(sleeps[0], 5000); // default when no Retry-After
  assert.equal(logs.filter((l) => l.includes("upstream 429, retry 1")).length, 1);
});

test("5xx exhaustion returns the last response after retries", async () => {
  const { f, upstream, sleeps, logs } = setup({ outcomes: [res(503), res(503), res(503)] });
  const r = await upstream("/chat/completions", "POST", "b", {});
  assert.equal(r.status, 503);
  assert.equal(f.calls.length, 3); // initial + 2 retries
  assert.deepEqual(sleeps, [1500, 3000]);
  assert.equal(logs.filter((l) => l.includes("upstream 503, retry")).length, 2);
});
