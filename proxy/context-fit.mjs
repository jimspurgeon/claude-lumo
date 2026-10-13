// Keeps prompt + reply inside Lumo's 131072-token window (issue #65).
//
// Lumo counts the prompt AND the requested reply (max_tokens) against one 131072-token window,
// and Claude Code asks for max_tokens 32000, so a prompt over about 99K tokens used to fail with
// HTTP 400 "This model's maximum context length is 131072 tokens. However, you requested 32000
// output tokens and your prompt contains at least N input tokens". N is only a lower bound (Lumo
// stops counting at the limit), so the error alone cannot say how much room is left.
//
// The proxy cannot count tokens, but it can learn each session's bytes-per-token ratio: every
// Lumo stream ends with usage.prompt_tokens, and a session's next request repeats almost the same
// prefix (sessions are told apart by the prompt_cache_key LiteLLM derives from Claude Code's
// session id). Before a request goes out, its prompt size is estimated from that ratio, and
// max_tokens is cut when prompt + reply would not fit. Measured 2026-10-13 across 2297 requests:
// median 4.2 bytes/token, 5th percentile 3.65; unknown sessions use the conservative 3.65, which
// overestimates the prompt and so errs towards a smaller reply rather than a failed request.

export const CONTEXT_TOKENS = 131072;
export const DEFAULT_BYTES_PER_TOKEN = 3.65;
export const MIN_OUTPUT_TOKENS = 1024; // below this a reply is not worth asking for
const SAFETY = 1.03; // estimate slack on top of the learned ratio
const MARGIN = 512;
const MAX_SESSIONS = 200;

export function createContextFit({ context = CONTEXT_TOKENS } = {}) {
  const ratios = new Map(); // prompt_cache_key -> bytes per prompt token, most recent last

  const parse = (body) => {
    try { const d = JSON.parse(body); return d && typeof d === "object" && !Array.isArray(d) ? d : null; } catch { return null; }
  };
  const keyOf = (d) => (typeof d?.prompt_cache_key === "string" && d.prompt_cache_key) || null;
  const ratioFor = (key) => (key && ratios.get(key)) || DEFAULT_BYTES_PER_TOKEN;
  const estimate = (bytes, key) => Math.ceil((bytes / ratioFor(key)) * SAFETY) + MARGIN;
  const requested = (d) => d.max_tokens ?? d.max_completion_tokens;
  const withMax = (d, n) => {
    const out = { ...d, max_tokens: n };
    if ("max_completion_tokens" in d) out.max_completion_tokens = n;
    return JSON.stringify(out);
  };

  return {
    // Records a finished request: its body size and the prompt_tokens Lumo reported.
    learn(body, promptTokens) {
      if (!(promptTokens > 0) || !body) return;
      const key = keyOf(parse(body));
      if (!key) return;
      ratios.delete(key);
      ratios.set(key, Buffer.byteLength(body) / promptTokens);
      if (ratios.size > MAX_SESSIONS) ratios.delete(ratios.keys().next().value);
    },
    // Before sending: cuts max_tokens when the estimated prompt plus the reply would not fit.
    // Returns { body, clamp } where clamp is null or { est, from, to } (numbers only).
    fit(body) {
      const d = parse(body);
      const want = d && requested(d);
      if (!(want > 0)) return { body, clamp: null };
      const est = estimate(Buffer.byteLength(body), keyOf(d));
      const room = context - est;
      if (want <= room || room < MIN_OUTPUT_TOKENS) return { body, clamp: null };
      return { body: withMax(d, room), clamp: { est, from: want, to: room } };
    },
    // After a context-length 400: a retry body with max_tokens cut to fit, or null.
    // Uses the larger of Lumo's lower bound and the estimate, discounting the ratio by a
    // further 10% because the first estimate has just been shown too small.
    refit(errorText, body) {
      let e; try { e = JSON.parse(errorText)?.error?.message; } catch { return null; }
      const m = typeof e === "string" && e.match(/maximum context length is (\d+) tokens[\s\S]*?requested (\d+) output tokens[\s\S]*?at least (\d+) input tokens/);
      const d = m && parse(body);
      if (!d) return null;
      const [ctx, want, lower] = m.slice(1).map(Number);
      const est = Math.max(lower + MARGIN, Math.ceil(estimate(Buffer.byteLength(body), keyOf(d)) / 0.9));
      const room = ctx - est;
      if (room < MIN_OUTPUT_TOKENS || room >= want) return null;
      return { body: withMax(d, room), clamp: { est, from: want, to: room } };
    },
    ratioFor,
  };
}
