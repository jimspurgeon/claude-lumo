// Runs Claude Code's WebSearch on Lumo's own built-in web search (issue #73; before
// that, #59 only blocked it).
//
// WebSearch is a *server-side* tool from Claude Code's perspective: when the
// model calls it, Claude Code sends a second request to the backend with
// `web_search_options: {}`, no tools, and a prompt like "Perform a web search
// for the query: ..." (captured as <state>/debug/cap-6.req.json). Lumo ignores
// that field and would answer from training data, which Claude Code then wraps
// as the "tool result": fabricated sources presented as search output.
//
// Lumo does have a keyless search of its own, the web UI's "Web search" toggle,
// which the public API exposes as a built-in tool: `tools: [{name: "web_search"}]`
// with `tool_choice: "auto"` on the same /chat/completions endpoint (Proton's
// open-source client, ProtonMail/WebClients packages/lumo-api-client:
// types-api.ts ChatCompletionsBuiltInTool, core/chat-completions.ts). Lumo runs
// the search server-side and streams an ordinary answer; sources arrive as URLs
// in the text. So the side-request is rewritten to ask for that search.
//
// With tool_choice "auto" Lumo may still answer from memory, URLs included, so a
// URL alone proves nothing. The plaintext API sends no chat.tool_call or
// chat.tool_result chunks (the web client's streaming.ts parses them; probes on
// 2026-10-13 never received any), but the search results are fed into the prompt:
// usage.prompt_tokens rose from 433 without the tool to 4,339 to 6,943 with it.
// Offering the tool by itself costs far less: a control on 2026-10-13 with the tool
// offered but a question Lumo answers without searching ("17 times 23", "say hello")
// came back at 962 to 975 prompt tokens, against 514 for the same request without the
// tool, while two real searches in the same run reached 4,556 and 6,401. So the tool
// adds about 450 tokens and a search adds about 4,000; the margin sits between them.
// The answer is passed on only when the usage shows that jump (or a
// tool_call/tool_result pair for web_search does arrive) AND it cites a URL.
// Anything else (an upstream error, a stall, a memory answer) gets the
// unavailability message, so no invented search result reaches the model.

export const WEBSEARCH_UNAVAILABLE_TEXT =
  "[claude-lumo] WebSearch did not return a usable result: Lumo's web search failed or "
  + "cited no sources, so no real search result is available. Do NOT present results or "
  + "invent sources. Tell the user the search did not work this time, and that WebFetch "
  + "can still retrieve specific URLs.";

export const LUMO_SEARCH_TOOLS = [{ name: "web_search" }];

const searchInstruction = (today) =>
  `Today's date is ${today}. Use the web_search tool to search the web for the query below `
  + "and report what the results say. The reader cannot see the results, only your report, so "
  + "do not just summarise: list each relevant result with its headline or title, its date when "
  + "shown, and its key facts, covering every distinct relevant result the search returned. "
  + "Then list every source URL you used, one per line, "
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

// Reads a chat response: the answer text (content deltas of an SSE stream, or the
// message content of a plain completion), the reported prompt_tokens, and whether
// web_search tool_call / tool_result chunks arrived. Reasoning and malformed lines
// are skipped.
export function parseSearchResponse(raw) {
  const t = String(raw || "");
  const out = { text: "", promptTokens: null, toolCall: false, toolResult: false };
  const take = (d) => {
    if (typeof d?.usage?.prompt_tokens === "number") out.promptTokens = d.usage.prompt_tokens;
    if (d?.object === "chat.tool_call" && d.tool_call?.name === "web_search") out.toolCall = true;
    if (d?.object === "chat.tool_result") out.toolResult = true;
  };
  // A stream can open with SSE comments (Lumo sends ":ingesting" while it works).
  if (!/^data:/m.test(t)) {
    try { const d = JSON.parse(t); take(d); out.text = d?.choices?.[0]?.message?.content || ""; } catch {}
    return out;
  }
  for (const line of t.split(/\r?\n/)) {
    const m = line.match(/^data:\s*(.*)$/);
    if (!m || m[1] === "[DONE]") continue;
    try { const d = JSON.parse(m[1]); take(d); out.text += d?.choices?.[0]?.delta?.content || ""; } catch {}
  }
  return out;
}

export const answerText = (raw) => parseSearchResponse(raw).text;

// True when the answer cites at least one http(s) URL.
export const citesSources = (text) => /https?:\/\/[^\s)>\]]+\.[^\s)>\]]+/i.test(text || "");

// Search results added to the prompt: at least this many prompt tokens beyond what
// the request itself can account for (measured: +3,900 or more when a search ran,
// about +450 when the tool was offered but not used).
export const SEARCH_TOKEN_MARGIN = 1500;

// True when the response shows Lumo actually searched: a web_search tool_call and
// tool_result pair, or prompt_tokens well beyond the request's own size (counted
// generously as one token per 3 bytes, so a long request cannot pass by itself).
export function searchRan(parsed, requestBytes) {
  if (parsed.toolCall && parsed.toolResult) return true;
  return typeof parsed.promptTokens === "number"
    && parsed.promptTokens >= Math.ceil(requestBytes / 3) + SEARCH_TOKEN_MARGIN;
}

export const SEARCH_TIMEOUT_MS = 120000;

// Runs a WebSearch side-request on Lumo's search. `upstreamChat` is the sniffer's
// (retries included); `upstream` the proxy's fetch. Returns { outcome, text }:
// outcome "ok" only with evidence of a search and a cited URL; text is "" otherwise.
// Never throws. A stream that stalls past timeoutMs ends as "timeout".
export async function runLumoSearch({ body, path, upstream, upstreamChat, log, now = new Date(), timeoutMs = SEARCH_TIMEOUT_MS }) {
  let timer, reader, timedOut = false;
  try {
    const searchBody = toLumoSearchRequest(body, now);
    const work = (async () => {
      const { response: up, buffered, errMeta } = await upstreamChat({ upstream, path, method: "POST", body: searchBody, headers: { "content-type": "application/json", accept: "text/event-stream" }, log });
      if (errMeta) return { outcome: `err_${errMeta.type}`, text: "" };
      if (!up.ok) return { outcome: `http_${up.status}`, text: "" };
      const parsed = parseSearchResponse(buffered ?? await readAll(up));
      if (!searchRan(parsed, Buffer.byteLength(searchBody))) return { outcome: "no_search", text: "" };
      if (!citesSources(parsed.text)) return { outcome: "no_sources", text: "" };
      return { outcome: "ok", text: parsed.text };
    })();
    // On a stall, cancel the upstream read too, so the connection does not stay open.
    const stall = new Promise((resolve) => { timer = setTimeout(() => { timedOut = true; reader?.cancel().catch(() => {}); resolve({ outcome: "timeout", text: "" }); }, timeoutMs); });
    return await Promise.race([work, stall]);
  } catch (e) {
    return { outcome: `error_${e?.name || "Error"}`, text: "" };
  } finally {
    clearTimeout(timer);
  }

  async function readAll(res) {
    if (!res.body) return "";
    reader = res.body.getReader();
    // Headers that arrive after the deadline: nobody waits for this read any more.
    if (timedOut) { reader.cancel().catch(() => {}); return ""; }
    const dec = new TextDecoder();
    let out = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return out + dec.decode();
      out += dec.decode(value, { stream: true });
    }
  }
}

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
