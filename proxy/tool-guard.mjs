// Makes sure every tool call Lumo announces reaches Claude Code intact (issue #11).
//
// Lumo sometimes ends a stream with finish_reason "tool_calls" but never streams the call, or
// streams one whose arguments are not valid JSON (GLM can leak <arg_key>/<arg_value> tags into
// them). LiteLLM then has no usable tool call, Claude Code sees end_turn, and the agent goes idle in
// the middle of its task. So the guard holds tool-call deltas and the finish chunk back (text and
// reasoning still stream live), checks each call at the end of the stream, repairs tag leakage,
// and if no valid call is left, re-sends the request once and forwards only the retry's tool
// calls. If that fails too, it ends the turn with a visible message telling the model to retry
// with smaller tool calls, so the session never stalls silently.

const LOST_TEXT = "\n\n[claude-lumo] Your tool call was lost in transit (Lumo announced it but it never "
  + "arrived intact). Retry the same action now, split into smaller tool calls: for a large file, Write a "
  + "short stub first, then add the rest with Edits of about 40 lines or fewer.";

const sse = (obj) => "data:" + JSON.stringify(obj) + "\n\n";

// LiteLLM ends the whole stream as soon as a line merely contains "[DONE]" (issue #13), so a tool
// call or text that mentions it (an SSE test fixture, say) was cut off and the turn ended silently.
// Inside a JSON data line, "[" decodes to "[", so the content arrives unchanged. Run this on
// everything sent to LiteLLM, after any re-encoding; the real "data:[DONE]" terminator is left alone.
const DONE_TEXT = /\[DONE\]/g;
export const escapeDone = (text) => text.split("\n").map((l) => (l.startsWith("data:{") ? l.replace(DONE_TEXT, "\\u005bDONE]") : l)).join("\n");

// GLM sometimes emits arguments as <arg_key>k</arg_key><arg_value>v</arg_value> pairs instead of JSON.
const TAG_PAIR = /<arg_key>([\s\S]*?)<\/arg_key>\s*<arg_value>([\s\S]*?)<\/arg_value>/g;
const repairTags = (s) => {
  const out = {};
  let n = 0;
  for (const m of s.matchAll(TAG_PAIR)) {
    const raw = m[2];
    let v = raw;
    try { const p = JSON.parse(raw); if (typeof p !== "string") v = p; } catch {}
    out[m[1].trim()] = v;
    n++;
  }
  return n ? out : null;
};

// Returns { args, repaired } for a usable call, or null.
export function checkCall(call) {
  if (!call.id || !call.name) return null;
  try {
    const p = JSON.parse(call.args);
    if (p && typeof p === "object" && !Array.isArray(p)) return { args: call.args, repaired: false };
  } catch {}
  const fixed = repairTags(call.args || "");
  return fixed ? { args: JSON.stringify(fixed), repaired: true } : null;
}

// Collects the tool calls of a complete SSE body (used for the retry).
export function collectCalls(text) {
  const g = createToolGuard();
  for (const line of text.split("\n")) g.line(line);
  return g.validCalls();
}

export function createToolGuard() {
  const calls = new Map(); // index -> { id, name, args }
  let base = null; // a chunk to copy id/model/created from when re-emitting
  let finish = null; // the held finish chunk
  const stats = { dropped: 0, repaired: 0, retry: 0 };

  const guard = {
    stats,
    // Takes one SSE line and returns what may be sent now ("" holds it back).
    line(l) {
      if (!l.startsWith("data:")) return l;
      if (l.startsWith("data:[DONE]") || l.startsWith("data: [DONE]")) return "";
      let d; try { d = JSON.parse(l.slice(5)); } catch { return l; }
      const c = d.choices?.[0];
      if (!c) return l;
      base ||= d;
      const tcs = c.delta?.tool_calls;
      if (Array.isArray(tcs)) {
        for (const tc of tcs) {
          const i = tc.index ?? 0;
          const rec = calls.get(i) || { id: null, name: null, args: "" };
          rec.id ||= tc.id || null;
          rec.name ||= tc.function?.name || null;
          rec.args += tc.function?.arguments || "";
          calls.set(i, rec);
        }
      }
      if (c.finish_reason) finish = d;
      if (!Array.isArray(tcs) && !c.finish_reason) return l;
      // Pass on any text or reasoning that rode along with a held chunk.
      const { tool_calls, ...rest } = c.delta || {};
      if (rest.content || rest.reasoning) return "data:" + JSON.stringify({ ...d, choices: [{ ...c, delta: rest, finish_reason: null }] });
      return "";
    },
    validCalls() {
      const ok = [];
      for (const [, call] of [...calls].sort((a, b) => a[0] - b[0])) {
        const r = checkCall(call);
        if (r) { ok.push({ ...call, args: r.args }); if (r.repaired) stats.repaired++; }
        else stats.dropped++;
      }
      return ok;
    },
    // Ends the stream: the checked tool calls, the finish chunk and [DONE], retrying once if needed.
    async end(retry) {
      const ref = base || finish || { object: "chat.completion.chunk", choices: [] };
      const wantsTools = calls.size > 0 || finish?.choices?.[0]?.finish_reason === "tool_calls";
      const fin = (reason) => sse({ ...ref, choices: [{ index: 0, delta: {}, finish_reason: reason }] }) + "data:[DONE]\n\n";
      const emit = (list) => list.map((call, i) => sse({ ...ref, choices: [{ index: 0, delta: { role: "assistant", tool_calls: [{ index: i, id: call.id, type: "function", function: { name: call.name, arguments: call.args } }] }, finish_reason: null }] })).join("");
      if (!wantsTools) return (finish ? sse(finish) : "") + "data:[DONE]\n\n";
      let ok = guard.validCalls();
      if (!ok.length && retry) {
        stats.retry = 1;
        try { ok = collectCalls(await retry()); } catch { ok = []; }
      }
      if (ok.length) return emit(ok) + fin("tool_calls");
      return sse({ ...ref, choices: [{ index: 0, delta: { role: "assistant", content: LOST_TEXT }, finish_reason: null }] }) + fin("stop");
    },
  };
  return guard;
}
