// Run: node --test gateway/proxy-integration.test.mjs
//
// Integration tests for proxy.mjs (#50). The top-level listen() now sits behind
// startServer(), so the module can be imported in-process: the state dir and log
// point at a temp dir, creds come from LUMO_TOKEN/LUMO_UID (no browser store is
// read), and the upstream points at a local stub via LUMO_UPSTREAM_BASE. The
// server itself binds port 0 (an OS-assigned ephemeral port). Everything stays on
// loopback; no request leaves the machine and ports 8790/4000 are never bound.
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// ---- fixtures ---------------------------------------------------------------

const CHAT_BODY = JSON.stringify({
  model: "lumo-max",
  stream: false,
  max_tokens: 100,
  messages: [{ role: "user", content: "hi" }],
});
const STREAM_BODY = JSON.stringify({
  model: "lumo-max",
  stream: true,
  max_tokens: 100,
  messages: [{ role: "user", content: "hi" }],
});
const NO_DB_BODY = JSON.stringify({
  error: { message: "No connected db.", type: "no_db_connection", code: "400" },
});
const JSON_HEADERS = { "content-type": "application/json" };

// The stub upstream records requests (url, method, headers, body) and replays
// queued response scripts — one script entry per expected upstream hit.
function startStub() {
  const hits = [];
  const scripts = [];
  return new Promise((resolve) => {
    const s = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        hits.push({ url: req.url, method: req.method, headers: req.headers, body: Buffer.concat(chunks).toString() });
        const next = scripts.shift() ?? { status: 200, headers: {}, text: "" };
        res.writeHead(next.status, next.headers);
        res.end(next.text);
      });
    });
    s.listen(0, "127.0.0.1", () =>
      resolve({ hits, queue: (...rs) => scripts.push(...rs), port: s.address().port, close: () => s.close() }));
  });
}

// ---- one proxy instance per worker ------------------------------------------

// Import the proxy AFTER the env is in place: the state dir, creds and the
// upstream base are read at module top level.
const stub = await startStub();
process.env.CLAUDE_LUMO_STATE_DIR = mkdtempSync(join(tmpdir(), "lumo-it-state-"));
const LOG = join(process.env.CLAUDE_LUMO_STATE_DIR, "logs", "proxy.log");
process.env.LUMO_PROXY_LOG = LOG;
process.env.LUMO_TOKEN = "it-test-token";
process.env.LUMO_UID = "it-test-uid";
process.env.LUMO_UPSTREAM_BASE = `http://127.0.0.1:${stub.port}`;

const { startServer } = await import("./proxy.mjs");
// Port 0: the OS assigns a free ephemeral port — immune to races, and this also
// proves the import itself did not grab the default 8790.
const server = await startServer(0);
const port = server.address().port;

async function chat(body = CHAT_BODY, path = "/chat/completions") {
  return fetch(`http://127.0.0.1:${port}${path}`, { method: "POST", headers: JSON_HEADERS, body });
}

// Log lines are appended synchronously, but the server handler may still be a
// tick behind the client's response resolving; poll briefly instead of racing.
// The baseline is a byte offset into the log file, so each test asserts only on
// the lines appended since the previous one.
let baseline = 0;
async function newLog(timeoutMs = 1000) {
  for (let i = 0; ; i++) {
    let text = "";
    try { text = readFileSync(LOG, "utf8"); } catch { text = ""; }
    if (text.length > baseline || i === 49) return text.slice(baseline);
    await new Promise((r) => setTimeout(r, timeoutMs / 50));
  }
}
test.beforeEach(async () => {
  try { baseline = readFileSync(LOG, "utf8").length; } catch { baseline = 0; }
  stub.hits.length = 0;
});

// ---- tests -------------------------------------------------------------------

test("importing proxy.mjs does not listen; startServer() binds an ephemeral port (#50)", async () => {
  // Importing at the top of this file already proved listening is not automatic:
  // had it been, the import would have grabbed port 8790 and this suite (or any
  // live proxy) would clash. Sanity-check the server came up and answers.
  assert.ok(port > 0, "startServer(0) got an OS-assigned ephemeral port");
  const res = await fetch(`http://127.0.0.1:${port}/health`);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).ok, true);
  // Structural double-check: the auto-start is guarded, listen() only runs via startServer.
  const src = readFileSync(fileURLToPath(import.meta.resolve("./proxy.mjs")), "utf8");
  assert.match(src, /export function startServer/);
  assert.match(src, /realpathSync\(process\.argv\[1\]\)/);
  assert.ok(!/^server\.listen\(/m.test(src), "no top-level listen() call remains");
});

test("buffered (non-SSE) chat response is forwarded whole and logged (#50, #43)", async () => {
  const upstreamBody = `{"id":"x","choices":[{"message":{"role":"assistant","content":"pong"}}]}`;
  stub.queue({ status: 200, headers: JSON_HEADERS, text: upstreamBody });
  const res = await chat();
  assert.equal(res.status, 200);
  assert.equal(await res.text(), upstreamBody); // forwarded byte-for-byte
  assert.equal(stub.hits.length, 1, "buffered answers need exactly one upstream hit");
  const log = await newLog();
  assert.match(log, /\/chat\/completions 200 in=96B first=[\d.]+s total=[\d.]+s out=\d+B/);
  assert.ok(!log.includes("err="), "no error was involved, so no err= in the log line");
  assert.ok(!log.includes("pong"), "the answer text must never be logged");
});

test("non-buffered (SSE) chat response streams through chunk by chunk (#50)", async () => {
  stub.queue({ status: 200, headers: { "content-type": "text/event-stream" },
               text: 'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: {"choices":[{"delta":{"content":"b"}}]}\n\ndata: {"usage":{"prompt_tokens":42},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n' });
  const res = await chat(STREAM_BODY);
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/event-stream");
  const text = await res.text();
  assert.match(text, /"content":"a"/);
  assert.match(text, /"content":"b"/);
  assert.match(text, /\[DONE\]/);
  assert.equal(stub.hits.length, 1, "streamed answers need exactly one upstream hit");
  const log = await newLog();
  assert.match(log, /model=lumo-max/);
  assert.match(log, /prompt_tokens=42/);
  assert.match(log, /finish=stop/);
  assert.match(log, /first=[\d.]+s/); // TTFB is recorded on the streamed path
  assert.ok(!log.includes('"content":"a"'), "the streamed answer must never be logged");
});

test("no_db_connection response is buffered, retried once with backoff, then forwarded (#50, #43)", async () => {
  stub.queue({ status: 200, headers: JSON_HEADERS, text: NO_DB_BODY });
  stub.queue({ status: 200, headers: JSON_HEADERS, text: NO_DB_BODY });
  const res = await chat();
  assert.equal(res.status, 200);
  assert.equal(await res.text(), NO_DB_BODY); // the persistent error is forwarded, not swallowed
  assert.equal(stub.hits.length, 2, "exactly one retry after the first no_db_connection");
  assert.equal(new Set(stub.hits.map((h) => h.body)).size, 1, "the retry reuses the same request body");
  const log = await newLog();
  assert.match(log, /type=no_db_connection code=400; retrying once in/); // the 1.5s backoff really ran
  assert.match(log, /first=1\.[56]s total=1\.[56]s/); // NO_DB_RETRY_MS elapsed before the retry went out
  assert.match(log, /\/chat\/completions 200 in=96B .*err=no_db_connection\/400/);
});

test("only content-type/accept are forwarded upstream; the Lumo login is injected (#50)", async () => {
  stub.queue({ status: 200, headers: JSON_HEADERS, text: `{"data":[]}` });
  const res = await fetch(`http://127.0.0.1:${port}/models`, {
    method: "GET",
    headers: { ...JSON_HEADERS, authorization: "Bearer sk-supersecret", "x-client-extra": "1" },
  });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), `{"data":[]}`);
  const hit = stub.hits.at(-1);
  assert.equal(hit.url, "/models");
  assert.equal(hit.method, "GET");
  // Auth was rebuilt from LUMO_TOKEN/LUMO_UID, replacing the client's own header...
  assert.equal(hit.headers.authorization, "Bearer it-test-token");
  assert.equal(hit.headers["x-pm-uid"], "it-test-uid");
  // ...and the client's extra headers were not forwarded (only content-type/accept survive).
  assert.equal(hit.headers["x-client-extra"], undefined);
  assert.equal(hit.headers["x-lumo-secret"], undefined);
});
test("hop-by-hop upstream headers are stripped from the client response (#50)", async () => {
  // Chat requests have non-SSE responses buffered (sniffer.mjs), so this answer
  // passes through the buffered path end to end.
  const upstreamBody = `{"id":"x","choices":[{"message":{"role":"assistant","content":"ok"}}]}`;
  // The stub sends hop-by-hop headers with distinctive values: if any leaked
  // through the proxy, the client would see exactly those values.
  stub.queue({ status: 200, headers: { ...JSON_HEADERS, connection: "LEAK-conn", "content-encoding": "LEAK-enc" }, text: upstreamBody });
  const res = await chat();
  const text = await res.text();
  assert.equal(res.status, 200);
  assert.equal(text, upstreamBody);
  const log = await newLog();
  assert.match(log, /\/chat\/completions 200 in=96B first=[\d.]+s total=[\d.]+s out=\d+B/);
  // The proxy strips the hop-by-hop set before writeHead. Node's own transport
  // still frames the client response (chunked, keep-alive) — but none of the
  // stub's distinctive LEAK values may appear, proving nothing leaked from
  // upstream, and the response is not reported as encoded.
  assert.equal(res.headers.get("content-encoding"), null);
  assert.ok(!text.includes("LEAK"), "no leaked header value reaches the client");
});

test.after(() => {
  server.close();
  stub.close();
});
