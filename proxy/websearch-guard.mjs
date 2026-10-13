// Answers WebSearch delegation with real Brave Search results (issues #59, #73).
//
// WebSearch is a *server-side* tool from Claude Code's perspective: when the
// model calls it, Claude Code sends a second request to the backend with
// `web_search_options: {}`, no tools, and a prompt like "Perform a web search
// for the query: ..." (captured as proxy/debug/cap-6.req.json). The real
// Anthropic backend runs the search there; Lumo ignores the field and just
// answers from training data, which Claude Code then wraps as the "tool
// result" - fabricated sources presented as search output (#59).
//
// Since #73 the proxy performs the search itself via the Brave Search API
// (configured with LUMO_SEARCH_API_KEY) and answers the side-request with real
// results. Without a key — or when the Brave API errors — the guard falls back
// to a clear unavailability message, so the model never presents training-data
// guesses as search output.

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
  return completion(model, stream, WEBSEARCH_UNAVAILABLE_TEXT);
}

function completion(model, stream, text) {
  const msg = { role: "assistant", content: text };
  if (!stream) return JSON.stringify({ id: "ws-guard", object: "chat.completion", created: Math.floor(Date.now() / 1000), model, choices: [{ index: 0, message: msg, finish_reason: "stop" }] });
  return chunk(model, { role: "assistant", content: "" })
    + chunk(model, { content: text })
    + chunk(model, {}, "stop")
    + "data:[DONE]\n\n";
}

// Plain text of a message's content ("string" or OpenAI-style content parts).
function messageText(m) {
  const c = m?.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.filter((p) => p?.type === "text").map((p) => p.text).join("\n");
  return "";
}

// Extracts the search query from a WebSearch delegation body: Claude Code's
// prompt is typically "Perform a web search for the query: <query>", so first
// try that pattern on the last user message, else fall back to the whole
// (trimmed) message text. Returns "" when nothing usable is found.
export function extractSearchQuery(body) {
  let d; try { d = JSON.parse(body); } catch { return ""; }
  const msgs = Array.isArray(d?.messages) ? d.messages : [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i]?.role !== "user") continue;
    const text = messageText(msgs[i]).trim();
    if (!text) continue;
    const m = text.match(/for the query\s*[:：]\s*(.+)$/is) || text.match(/^(?:web )?search (?:for|query)\s*[:：]\s*(.+)$/is);
    return (m ? m[1] : text).trim();
  }
  return "";
}

// Formats Brave Search results (web.results) as a plain-text numbered list.
export function formatBraveResults(query, results) {
  const top = (results || []).slice(0, 5).filter((r) => r?.url);
  if (!top.length) return "";
  const lines = top.map((r, i) =>
    `${i + 1}. ${String(r.title ?? "(untitled)").trim()}\n   ${String(r.description ?? "").trim()}\n   ${r.url}`);
  return `[claude-lumo] Web search results for "${query}" (Brave Search, top ${top.length}):\n\n`
    + lines.join("\n\n")
    + `\n\nPresent these as your search results; cite the URLs when using them.`;
}

// Runs the search and returns the formatted results text, or "" when no key /
// no query / API error (caller falls back to the unavailability message).
export async function braveWebSearch(query) {
  const key = process.env.LUMO_SEARCH_API_KEY;
  if (!key || !query) return "";
  try {
    const url = "https://api.search.brave.com/res/v1/web/search"
      + "?q=" + encodeURIComponent(query) + "&count=5&safesearch=off";
    const res = await fetch(url, { headers: { "Accept": "application/json", "X-Subscription-Token": key }, signal: AbortSignal.timeout(15000) });
    if (!res.ok) return "";
    const d = await res.json();
    return formatBraveResults(query, d?.web?.results);
  } catch {
    return "";
  }
}

// The WebSearch side-request response: live Brave results when configured and
// reachable, otherwise the unavailability message (never throws).
export async function webSearchResponse(model, stream, body) {
  const results = await braveWebSearch(extractSearchQuery(body));
  return completion(model, stream, results || WEBSEARCH_UNAVAILABLE_TEXT);
}
