// Local OpenAI-compatible proxy for Proton Lumo, used by claude-lumo (via LiteLLM).
// - Injects the live Firefox Lumo login (Bearer token + x-pm-uid) into every request.
// - On 401 "Invalid access token": re-reads the cookie and retries once (fixes overnight expiry).
// - Retries connection errors, 429 (honouring Retry-After, max 30s), 5xx, and requests with no response within LUMO_FIRST_BYTE_MS
//   (default 240s), before any bytes are streamed (2 retries, backoff).
// - Logs time-to-first-byte and duration of every chat request to proxy.log.
// - Detects Lumo error payloads returned with HTTP 200 (non-SSE JSON bodies) and logs their
//   type/code (never message content); a transient no_db_connection error is retried once
//   after a short backoff (see sniffer.mjs, issue #43).
// - Streams responses (SSE) through, re-emitting tool calls one per chunk (see splitToolCallChunks). Listens on 127.0.0.1 only.
// - Holds tool calls until the stream ends and checks them; a lost or broken call is repaired or
//   retried once, else the turn ends with a visible message (see tool-guard.mjs, issue #11).
// - Adds the closest real text to failed Edit results, so the retry copies it (see edit-hints.mjs).
import http from "node:http";
import { appendFileSync, mkdirSync, renameSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { readLumoCredentials } from "./creds.mjs";
import { createUpstream } from "./upstream.mjs";
import { addEditHints } from "./edit-hints.mjs";
import { createToolGuard, escapeDone } from "./tool-guard.mjs";
import { upstreamChat, retryText } from "./sniffer.mjs";
import { createContextFit } from "./context-fit.mjs";
import { chatResponse, isWebSearchRequest, runLumoSearch, webSearchUnavailable } from "./websearch-guard.mjs";
import { rotateLog, pruneDir } from "./maintenance.mjs";

const PORT = Number(process.env.LUMO_PROXY_PORT || 8790);
const LOG = join(import.meta.dirname, "proxy.log");
const DEBUG = join(import.meta.dirname, "debug");
const log = (m) => { try { appendFileSync(LOG, `${new Date().toISOString()} ${m}\n`); } catch {} };
const credsRef = { current: readLumoCredentials() }; // mutable: upstream() refreshes it after a 401
rotateLog(LOG, 10 * 1024 * 1024); // one generation: proxy.log -> proxy.log.1
pruneDir(DEBUG, 50); // cap accumulated conversation dumps
log(`start port=${PORT} creds=${credsRef.current ? "ok" : "MISSING"}`);

const upstream = createUpstream({ credsRef, log }); // test seam: fetch/readCreds/sleep/timeouts injectable (see upstream.mjs)

// Lumo can stream several parallel tool calls in one SSE chunk. LiteLLM's OpenAI->Anthropic
// translation reads only one tool call per chunk and glues the rest onto it ({..}{..}), which
// Claude Code rejects as invalid tool input. The tool guard holds tool calls back and re-emits
// them at the end of the stream, one complete call per chunk.
// Also tallies each stream (reasoning/content size, tool calls, finish reason) for the log line.
function splitToolCallChunks() {
  let buf = "";
  const stats = { reasoning: "", content: 0, tools: 0, finish: "", promptTokens: null };
  const guard = createToolGuard();
  const fix = (line) => {
    if (!line.startsWith("data:") || line.startsWith("data:[DONE]")) return guard.line(line);
    let d; try { d = JSON.parse(line.slice(5)); } catch { return line; }
    const c = d.choices?.[0], calls = c?.delta?.tool_calls;
    if (c?.delta?.reasoning) stats.reasoning += c.delta.reasoning;
    if (c?.delta?.content) stats.content += c.delta.content.length;
    if (Array.isArray(calls)) stats.tools += calls.filter((x) => x.id).length;
    if (c?.finish_reason) stats.finish = c.finish_reason;
    if (typeof d.usage?.prompt_tokens === "number") stats.promptTokens = d.usage.prompt_tokens;
    return guard.line(line);
  };
  return {
    push(text) { buf += text; const i = buf.lastIndexOf("\n"); if (i < 0) return ""; const done = buf.slice(0, i + 1); buf = buf.slice(i + 1); return escapeDone(done.split("\n").map(fix).join("\n")); },
    end() { const rest = fix(buf); buf = ""; return escapeDone(rest); },
    finish: async (retry) => escapeDone(await guard.end(retry)),
    stats,
    guard: guard.stats,
  };
}

const fitter = createContextFit(); // keeps prompt + max_tokens inside Lumo's window (#65)
const live = new Map(); // in-flight chat streams, shown at /v1/debug/live
let nextId = 0;

const server = http.createServer(async (req, res) => {
  const path = req.url.replace(/^\/v1/, "");
  if (path === "/debug/live") {
    // Exposes in-flight reasoning tails (last 400 chars of each stream) to any local
    // process: loopback only, no auth. Intentional, for local debugging of stuck turns.
    const now = Date.now(), rows = [...live.values()].map((r) => ({ id: r.id, model: r.model, secs: Math.round((now - r.t0) / 1000), idle: Math.round((now - r.last) / 1000), bytes: r.bytes, reasoning: r.stats?.reasoning.length ?? 0, content: r.stats?.content ?? 0, tail: r.stats?.reasoning.slice(-400) }));
    res.writeHead(200, { "content-type": "application/json" }); return res.end(JSON.stringify(rows, null, 1));
  }
  if (path === "/health") { res.writeHead(200, { "content-type": "application/json" }); return res.end(JSON.stringify({ ok: true, creds: !!credsRef.current })); }
  const chunks = []; for await (const c of req) chunks.push(c);
  let body = chunks.length ? Buffer.concat(chunks) : undefined;
  if (body && path.startsWith("/chat")) body = addEditHints(body);
  // Claude Code's internal WebSearch delegation (web_search_options): run it on Lumo's own
  // web search and pass the answer on only when it shows a search ran and cites sources;
  // otherwise answer with the unavailability message, so no fabricated sources reach the
  // model (#59, #73; see websearch-guard.mjs).
  if (body && path.startsWith("/chat") && isWebSearchRequest(body)) {
    let model = "-", stream = true; try { const r = JSON.parse(body); model = r.model || model; stream = r.stream ?? true; } catch {}
    const t0 = Date.now();
    const { outcome, text } = await runLumoSearch({ body, path, upstream, upstreamChat, log });
    res.writeHead(200, { "content-type": stream ? "text/event-stream" : "application/json" });
    res.end(outcome === "ok" ? chatResponse(model, stream, text) : webSearchUnavailable(model, stream));
    // Only the outcome and sizes are logged, never the query or the answer.
    log(`${path} 200 in=${body.length}B total=${((Date.now() - t0) / 1000).toFixed(1)}s websearch=${outcome} answer=${text.length}c`);
    return;
  }
  const headers = {}; for (const h of ["content-type", "accept"]) if (req.headers[h]) headers[h] = req.headers[h];
  const t0 = Date.now(); let tFirst = 0, bytes = 0;
  try {
    // Chat responses are inspected for error payloads (buffering non-SSE bodies);
    // a no_db_connection error is retried once with a short backoff (see sniffer.mjs, issue #43).
    let fit = null;
    if (body && path.startsWith("/chat")) ({ body, clamp: fit } = fitter.fit(body));
    const { response: up, buffered, errMeta, sent } = await upstreamChat({ upstream, path, method: req.method, body, headers, log, refit: fitter.refit });
    // A refit or a stripped prompt_cache_key changes the request; the tool-guard retry,
    // learn() and the log below must use the one that was actually sent.
    if (sent !== undefined && sent !== body) { if (path.startsWith("/chat")) { try { const d = JSON.parse(sent), o = JSON.parse(body); if (d.max_tokens !== o.max_tokens) fit = { from: fit?.from ?? o.max_tokens, to: d.max_tokens }; } catch {} } body = sent; }
    // Buffered chat responses already had their status/error logged by the sniffer; only
    // non-buffered paths (non-chat routes, SSE error responses) still need the status log.
    if (up.status >= 400 && buffered === null) log(`${req.method} ${path} -> ${up.status}`);
    const out = {}; up.headers.forEach((v, k) => { if (!["content-encoding", "content-length", "transfer-encoding", "connection"].includes(k)) out[k] = v; });
    res.writeHead(up.status, out);
    let model = "-"; try { model = JSON.parse(body).model; } catch {};
    let extra = "";
    if (buffered !== null) {
      // Non-SSE chat response, already consumed for inspection: forward the buffered text.
      bytes = Buffer.byteLength(buffered); tFirst = Date.now();
      if (errMeta) extra = ` err=${errMeta.type}/${errMeta.code}`;
      res.end(buffered);
    } else {
    const sse = (up.headers.get("content-type") || "").includes("event-stream") ? splitToolCallChunks() : null;
    const dec = new TextDecoder();
    const row = { id: ++nextId, model, t0, last: Date.now(), bytes: 0, stats: sse?.stats }; if (sse) live.set(row.id, row);
    try {
      if (up.body) for await (const c of up.body) { if (!tFirst) tFirst = Date.now(); bytes += c.length; row.bytes = bytes; row.last = Date.now(); if (process.env.LUMO_CAPTURE) row.raw = (row.raw || "") + (row.dec2 ||= new TextDecoder()).decode(c, { stream: true }); res.write(sse ? sse.push(dec.decode(c, { stream: true })) : c); }
    } finally { live.delete(row.id); }
    if (process.env.LUMO_CAPTURE && sse) { try { mkdirSync(DEBUG, { recursive: true }); writeFileSync(join(DEBUG, `cap-${row.id}.req.json`), body); writeFileSync(join(DEBUG, `cap-${row.id}.sse`), row.raw || ""); } catch {} }
    if (sse) {
      res.write(sse.end() + dec.decode());
      // A lost tool call is retried with the same request; only its tool calls are forwarded.
      // It goes through upstreamChat, so a no_db_connection answer is retried there too (#49).
      res.write(await sse.finish(() => retryText({ upstream, path, method: req.method, body, headers, log })));
    }
    res.end();
    if (sse) {
      const s = sse.stats; let req = {}; try { req = JSON.parse(body); } catch {}
      fitter.learn(body, s.promptTokens);
      extra = ` model=${req.model} max_tokens=${req.max_tokens ?? req.max_completion_tokens ?? "-"}${fit ? ` fit=${fit.from}->${fit.to}` : ""} prompt_tokens=${s.promptTokens ?? "-"} reasoning=${s.reasoning.length}c content=${s.content}c tools=${s.tools} finish=${s.finish || "none"} dropped_tool=${sse.guard.dropped} repaired_tool=${sse.guard.repaired} tool_retry=${sse.guard.retry}`;
      if (s.reasoning.length > 30000) {
        const f = join(DEBUG, `${new Date().toISOString().replace(/[:.]/g, "-")}.txt`);
        try { mkdirSync(DEBUG, { recursive: true }); writeFileSync(f, s.reasoning); extra += ` dump=${f}`; } catch {}
      }
    }
    }
    if (path.startsWith("/chat")) log(`${path} ${up.status} in=${body ? body.length : 0}B first=${tFirst ? ((tFirst - t0) / 1000).toFixed(1) : "-"}s total=${((Date.now() - t0) / 1000).toFixed(1)}s out=${bytes}B${extra}`);
  } catch (e) {
    log(`proxy error ${e.message}`);
    if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: `lumo-proxy: ${e.message}` } }));
  }
});
// Refuse to shadow a live proxy on the same port instead of dying with EADDRINUSE:
// a test that forgot LUMO_PROXY_PORT should fail loudly, and a second instance must
// never silently take over traffic meant for the running one (#71).
server.once("error", (e) => {
  if (e.code === "EADDRINUSE") {
    log(`fatal: port ${PORT} already in use (another proxy?). Set LUMO_PROXY_PORT to a spare port and retry.`);
    console.error(`lumo-proxy: port ${PORT} already in use. Set LUMO_PROXY_PORT to a spare port and retry.`);
    process.exit(1);
  }
  log(`fatal: ${e.message}`); process.exit(1);
});
server.listen(PORT, "127.0.0.1", () => log("listening"));
