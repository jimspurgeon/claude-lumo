// Run: node --test proxy/context-fit.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { CONTEXT_TOKENS, createContextFit, DEFAULT_BYTES_PER_TOKEN, MIN_OUTPUT_TOKENS } from "./context-fit.mjs";

const body = (bytes, extra = {}) => {
  const base = { model: "lumo-max", max_tokens: 32000, ...extra, pad: "" };
  const len = Buffer.byteLength(JSON.stringify(base));
  return JSON.stringify({ ...base, pad: "x".repeat(Math.max(0, bytes - len)) });
};
const ctxErr = (requested, input) => JSON.stringify({ error: { message: `This model's maximum context length is 131072 tokens. However, you requested ${requested} output tokens and your prompt contains at least ${input} input tokens, for a total of at least ${requested + input} tokens.`, type: "BadRequestError", code: 400 } });

test("a small prompt is left alone", () => {
  const f = createContextFit(), b = body(40000);
  assert.deepEqual(f.fit(b), { body: b, clamp: null });
});

test("a big prompt from an unknown session gets max_tokens cut with the conservative ratio", () => {
  const f = createContextFit();
  const r = f.fit(body(Math.round(105000 * DEFAULT_BYTES_PER_TOKEN)));
  assert.ok(r.clamp, "clamped");
  assert.equal(r.clamp.from, 32000);
  assert.equal(JSON.parse(r.body).max_tokens, r.clamp.to);
  assert.ok(r.clamp.est >= 105000, "the estimate errs high");
  assert.ok(r.clamp.to + r.clamp.est <= CONTEXT_TOKENS);
});

test("a learned session ratio gives an accurate estimate", () => {
  const f = createContextFit();
  // This session runs at 4.2 bytes/token: 420000 bytes was 100000 prompt tokens.
  f.learn(body(420000, { prompt_cache_key: "s1" }), 100000);
  assert.ok(Math.abs(f.ratioFor("s1") - 4.2) < 0.01);
  const r = f.fit(body(462000, { prompt_cache_key: "s1" })); // about 110000 tokens
  assert.ok(r.clamp);
  assert.ok(r.clamp.est >= 110000 && r.clamp.est < 114500, String(r.clamp.est));
  assert.ok(r.clamp.to >= 16500, "keeps a useful reply budget, " + r.clamp.to);
  // The same bytes from an unknown session are estimated higher (conservative): here too high
  // to leave a useful reply, so the request goes out unchanged. A session's ratio is learned
  // from its earlier, smaller requests, so this only happens for the first request after a
  // proxy restart.
  assert.equal(f.fit(body(462000)).clamp, null);
});

test("max_completion_tokens is cut together with max_tokens", () => {
  const r = createContextFit().fit(body(Math.round(110000 * DEFAULT_BYTES_PER_TOKEN), { max_completion_tokens: 32000 }));
  const d = JSON.parse(r.body);
  assert.equal(d.max_completion_tokens, d.max_tokens);
});

test("hopeless prompts, requests without max_tokens and non-JSON bodies pass through", () => {
  const f = createContextFit();
  for (const b of [body(Math.round(131000 * DEFAULT_BYTES_PER_TOKEN)), JSON.stringify({ model: "m" }), "not json", "[1]"]) {
    assert.deepEqual(f.fit(b), { body: b, clamp: null });
  }
});

test("learn ignores missing usage and bodies without a session key, and keeps at most 200 sessions", () => {
  const f = createContextFit();
  f.learn(body(1000), 500);
  f.learn(body(1000, { prompt_cache_key: "a" }), null);
  assert.equal(f.ratioFor("a"), DEFAULT_BYTES_PER_TOKEN);
  for (let i = 0; i < 201; i++) f.learn(body(1000, { prompt_cache_key: `k${i}` }), 250);
  assert.equal(f.ratioFor("k0"), DEFAULT_BYTES_PER_TOKEN, "oldest evicted");
  assert.ok(Math.abs(f.ratioFor("k200") - 4) < 0.1);
});

test("refit uses the larger of Lumo's lower bound and a discounted estimate", () => {
  const f = createContextFit();
  const b = body(Math.round(100000 * DEFAULT_BYTES_PER_TOKEN));
  const r = f.refit(ctxErr(32000, 99073), b);
  assert.ok(r.clamp.est > 99073 + 512);
  assert.ok(r.clamp.to >= MIN_OUTPUT_TOKENS && r.clamp.to < 32000);
  assert.equal(JSON.parse(r.body).max_tokens, r.clamp.to);
});

test("refit declines other errors, garbage and cases with no useful room", () => {
  const f = createContextFit();
  assert.equal(f.refit(JSON.stringify({ error: { message: "other" } }), body(1000)), null);
  assert.equal(f.refit("not json", body(1000)), null);
  assert.equal(f.refit(ctxErr(32000, 99073), "not json"), null);
  assert.equal(f.refit(ctxErr(32000, 130500), body(1000)), null);
});
