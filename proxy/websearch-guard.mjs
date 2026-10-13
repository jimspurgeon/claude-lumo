// Runs Claude Code's WebSearch on Lumo's own built-in web search (issue #73; before
// that, #59 only blocked it).
//
// WebSearch is a *server-side* tool from Claude Code's perspective: when the
// model calls it, Claude Code sends a second request to the backend with
// `web_search_options: {}`, no tools, and a prompt like "Perform a web search
// for the query: ..." (captured as proxy/debug/cap-6.req.json). Lumo ignores
// that field and would answer from training data, which Claude Code then wraps
// as the "tool result": fabricated sources presented as search output.
//
// Lumo does have a keyless search of its own, the web UI's "Web search" toggle,
// which the public API exposes as a built-in tool: `tools: [{name: "web_search"}]`
// with `tool_choice: "auto"` on the same /chat/completions endpoint (Proton's
// open-source client, ProtonMail/WebClients packages/lumo-api-client:
// types-api.ts ChatCompletionsBuiltInTool, core/chat-completions.ts). Lumo runs
// the search server-side and streams an ordinary answer; sources arrive as URLs
// in the text. So the side-request is rewritten to ask for that search, and the
// answer is only passed on when it cites at least one URL. Anything else (an
// upstream error, an answer without sources) still gets the unavailability
// message, so no invented search result reaches the model.

export const WEBSEARCH_UNAVAILABLE_TEXT =
  "[claude-lumo] WebSearch did not return a usable result: Lumo's web search failed or "
  + "cited no sources, so no real search result is available. Do NOT present results or "
  + "invent sources. Tell the user the search did not work this time, and that WebFetch "
  + "can still retrieve specific URLs.";

export const LUMO_SEARCH_TOOLS = [{ name: "web_search" }];

const searchInstruction = (today) =>
  `Today's date is ${today}. Use the web_search tool to search the web for the query below `
  + "and answer from what the results say. List every source URL you used, one per line, "
  + "under a 'Sources:' heading. If the search finds nothing useful, say so plainly; never "
  + "invent a result or a source.";

// True for Claude Code's internal WebSearch delegation (a chat request whose
// body carries web_search_options). Regular requests never carry the field;
// only the delegation side-request does (verified against captured traffic).
export function isWebSearchRequest(body) {
  if (!body) return false;
  let d; try { d = JSON.parse(body); } catch { return false; }
  return d?.constructor === Object && "web_search_options" in d;
}

// Rewrites a WebSearch side-request into a Lumo request with its built-in search on:
// drops web_search_options, asks for the web_search tool, and puts the date and the
// "cite your sources" instruction first. Always streams upstream (as the web client
// does); the proxy collects the answer before replying. Returns a JSON string.
export function toLumoSearchRequest(body, now = new Date()) {
  const d = JSON.parse(body);
  delete d.web_search_options;
  const instruction = searchInstruction(now.toISOString().slice(0, 10));
  const messages = Array.isArray(d.messages) ? [...d.messages] : [];
  if (messages[0]?.role === "system" && typeof messages[0].content === "string") {
    messages[0] = { ...messages[0], content: `${instruction}\n\n${messages[0].content}` };
  } else {
    messages.unshift({ role: "system", content: instruction });
  }
  return JSON.stringify({ ...d, messages, tools: LUMO_SEARCH_TOOLS, tool_choice: "auto", stream: true });
}

// The answer text of a chat response: the content deltas of an SSE stream, or the
// message content of a plain completion. Reasoning and malformed lines are skipped.
export function answerText(raw) {
  const t = String(raw || "");
  // A stream can open with SSE comments (Lumo sends ":ingesting" while it searches).
  if (!/^data:/m.test(t)) {
    try { return JSON.parse(t)?.choices?.[0]?.message?.content || ""; } catch { return ""; }
  }
  let out = "";
  for (const line of t.split(/\r?\n/)) {
    const m = line.match(/^data:\s*(.*)$/);
    if (!m || m[1] === "[DONE]") continue;
    try { out += JSON.parse(m[1])?.choices?.[0]?.delta?.content || ""; } catch {}
  }
  return out;
}

// True when the answer cites at least one http(s) URL: the sign a search really ran.
export const citesSources = (text) => /https?:\/\/[^\s)>\]]+\.[^\s)>\]]+/i.test(text || "");

const chunk = (model, delta, finish = null) =>
  "data:" + JSON.stringify({ id: "ws-guard", object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model, choices: [{ index: 0, delta, finish_reason: finish }] }) + "\n\n";

// A complete chat response carrying `text`, in the shape of either a streaming or
// a plain chat completion (whichever the WebSearch side-request asked for).
export function chatResponse(model, stream, text) {
  const msg = { role: "assistant", content: text };
  if (!stream) return JSON.stringify({ id: "ws-guard", object: "chat.completion", created: Math.floor(Date.now() / 1000), model, choices: [{ index: 0, message: msg, finish_reason: "stop" }] });
  return chunk(model, { role: "assistant", content: "" })
    + chunk(model, { content: text })
    + chunk(model, {}, "stop")
    + "data:[DONE]\n\n";
}

export const webSearchUnavailable = (model, stream) => chatResponse(model, stream, WEBSEARCH_UNAVAILABLE_TEXT);
