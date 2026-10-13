// Fails WebSearch loudly instead of letting Lumo hallucinate (issue #59).
//
// WebSearch is a *server-side* tool from Claude Code's perspective: when the
// model calls it, Claude Code sends a second request to the backend with
// `web_search_options: {}`, no tools, and a prompt like "Perform a web search
// for the query: ..." (captured as proxy/debug/cap-6.req.json). The real
// Anthropic backend runs the search there; Lumo ignores the field and just
// answers from training data, which Claude Code then wraps as the "tool
// result" - fabricated sources presented as search output.
//
// We cannot run a real search (there is no search backend behind Lumo), so
// this guard short-circuits that side-request at the proxy and answers it
// directly with a clear unavailability message. Claude Code wraps that as the
// tool result, the main model sees it, and the user gets the truth instead of
// plausible fiction.

export const WEBSEARCH_UNAVAILABLE_TEXT =
  "[claude-lumo] WebSearch is not available on this endpoint: Lumo has no web-search "
  + "backend, so no real search was performed. Do NOT present these results or invent "
  + "sources. Tell the user that web search is unavailable, and that WebFetch can still "
  + "retrieve specific URLs.";

// True for Claude Code's internal WebSearch delegation (a chat request whose
// body carries web_search_options). Regular requests never carry the field;
// only the delegation side-request does (verified against captured traffic).
export function isWebSearchRequest(body) {
  if (!body) return false;
  let d; try { d = JSON.parse(body); } catch { return false; }
  return d?.constructor === Object && "web_search_options" in d;
}

const chunk = (model, delta, finish = null) =>
  "data:" + JSON.stringify({ id: "ws-guard", object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model, choices: [{ index: 0, delta, finish_reason: finish }] }) + "\n\n";

// The synthetic response for a WebSearch side-request, in the shape of either
// a streaming or a plain chat completion (whichever the request asked for).
export function webSearchUnavailable(model, stream) {
  const msg = { role: "assistant", content: WEBSEARCH_UNAVAILABLE_TEXT };
  if (!stream) return JSON.stringify({ id: "ws-guard", object: "chat.completion", created: Math.floor(Date.now() / 1000), model, choices: [{ index: 0, message: msg, finish_reason: "stop" }] });
  return chunk(model, { role: "assistant", content: "" })
    + chunk(model, { content: WEBSEARCH_UNAVAILABLE_TEXT })
    + chunk(model, {}, "stop")
    + "data:[DONE]\n\n";
}
