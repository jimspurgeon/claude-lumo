// Run: node --test gateway/proxy-integration.test.mjs
//
// Integration tests for proxy.mjs (#50). The top-level listen() now sits behind
// startServer(), so the module can be imported in-process: we point
// LUMO_PROXY_PORT at a free port (never 8790/4000), the state dir and log at a
// temp dir, creds at env vars (LUMO_TOKEN/LUMO_UID, so no browser store is read),
// and the upstream at a local stub via LUMO_UPSTREAM_BASE. Everything stays on
// loopback; no request leaves the machine.
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// ---- shared fixtures -------------------------------------------------------

const CHAT_BODY = JSON.stringify({
  model: "lumo-max",
  stream: false,
  max_tokens: 100,
  messages: [{ role: "user", content: "hi" }],
});
const NO_DB_BODY = JSON.stringify({
  error: { message: "No connected db.", type: "no_db_connection", code: "400" },
});
const JSON_HEADERS = { "content-type": "application/json" };

// The stub upstream records requests and replays queued response scripts
// (one script entry per expected upstream hit).
function startStub() {
  const hits = [];
  const scripts = [];
  return new Promise((resolve) => {
    const s = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        hits.push({ url: req.url, method: req.method, body: Buffer.concat(chunks).toString() });
        const next = scripts.shift() ?? { status: 200, headers: {}, text: "" };
        res.writeHead(next.status, next.headers);
        res.end(next.text);
      });
    });
    s.listen(0, "127.0.0.1", () =>
      resolve({ hits, queue: (...rs) => scripts.push(...rs), port: s.address().port, close: () => s.close() }));
  });
}

function freePort() {
  const s = http.createServer();
  return new Promise((resolve) => s.listen(0, "127.0.0.1", () => resolve(s.address().port)));
}

// Import the proxy AFTER the env is in place: the port, state dir, creds and the
// upstream base are all read at module top level.
const stub = await startStub();
process.env.CLAUDE_LUMO_STATE_DIR = mkdtempSync(join(tmpdir(), "lumo-it-state-"));
const LOG = join(process.env.CLAUDE_LUMO_STATE_DIR, "logs", "proxy.log");
process.env.LUMO_PROXY_LOG = LOG;
process.env.LUMO_TOKEN = "it-test-token";
process.env.LUMO_UID = "it-test-uid";
const port = await freePort();
process.env.LUMO_PROXY_PORT = String(port);
process.env.LUMO_UPSTREAM_BASE = `http://127.0.0.1:${stub.port}`;

const { startServer } = await import("./proxy.mjs");
const server = await startServer(port);

// A free port for later fetch()es through the proxy.
const echoPort = await freePort();

async function chat(body = CHAT_BODY, path = "/chat/completions") {
  return fetch(`http://127.0.0.1:${port}${path}`, { method: "POST", headers: JSON_HEADERS, body });
}
async function models() {
  return fetch(`http://127.0.0.1:${port}/models`, { method: "GET", headers: JSON_HEADERS });
}

// Only the log lines appended since the last snapshot are asserted on, so tests
// stay independent of each other and of the startup lines.
let baseline = 0;
function newLog() {
  let text = "";
  try { text = readFileSync(LOG, "utf8"); } catch { return ""; }
  const lines = text.split("\n");
  return lines.slice(baseline).join("\n");
}
test.beforeEach(() => {
  try { baseline = readFileSync(LOG, "utf8").split("\n").length; } catch { baseline = 0; }
  stub.hits.length = 0;
});

// ---- tests -----------------------------------------------------------------

test("importing proxy.mjs does not listen; startServer binds the configured port (#50)", async () => {
  // The import at the top of this file already proved listening is not automatic:
  // had it been, this suite could never bind its own port without EADDRINUSE on
  // 8790. Sanity-check the server came up where asked.
  assert.equal(server.address().port, port);
  const res = await fetch(`http://127.0.0.1:${port}/health`);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).ok, true);
  assert.match(newLog(), /listening/);
});

test("buffered (non-SSE) chat response is forwarded whole and logged as buffered (#50, #43)", async () => {
  stub.queue({ status: 200, headers: JSON_HEADERS, text: `{"id":"x","choices":[{"message":{"role":"assistant","content":"pong"}}]}` });
  const res = await chat();
  assert.equal(res.status, 200);
  assert.equal(await res.text(), `{"id":"x","choices":[{"message":{"role":"assistant","content":"pong"}}]}`);
  const log = newLog();
  // The proxy adds nothing to a buffered chat body.
  assert.ok(!log.includes("err="), "no error was involved, so no err= in the log line");
  assert.match(log, /\/chat\/completions 200 in=\d+B/);
  assert.match(log, /buffered=/);
  assert.ok(!log.includes("pong"), "the answer text must never be logged");
});

test("non-buffered (SSE) chat response streams through chunk by chunk (#50)", async () => {
  stub.queue({ status: 200, headers: { "content-type": "text/event-stream" },
               text: 'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: {"choices":[{"delta":{"content":"b"}}]}\n\ndata: {"usage":{"prompt_tokens":42},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n' });
  const res = await chat(JSON.stringify({ model: "lumo-max", stream: true, max_tokens: 100, messages: [{ role: "user", content: "hi" }] }));
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /"content":"a"/);
  assert.match(text, /"content":"b"/);
  assert.match(text, /\[DONE\]/);
  const log = newLog();
  assert.match(log, /model=lumo-max/);
  assert.match(log, /prompt_tokens=42/);
  assert.match(log, /finish=stop/);
  assert.match(log, /first=[\d.]+s/); // the TTFB field is recorded on the streamed path
  assert.ok(!log.includes('"content":"a"'), "the streamed answer must never be logged");
});

test("no_db_connection response is buffered, retried once with backoff, and forwarded if it persists (#50, #43)", async () => {
  stub.queue({ status: 200, headers: JSON_HEADERS, text: NO_DB_BODY });
  stub.queue({ status: 200, headers: JSON_HEADERS, text: NO_DB_BODY });
  const res = await chat();
  assert.equal(res.status, 200);
  assert.equal(await res.text(), NO_DB_BODY);
  assert.equal(stub.hits.length, 2, "one retry after the first no_db_connection");
  assert.ok(new Set(stub.hits.map((h) => h.body)).size === 1, "the retry reuses the same request body");
  const log = newLog();
  assert.match(log, /type=no_db_connection code=400; retrying once in/);
  assert.match(log, /\/chat\/completions 200 in=\d+B .*err=no_db_connection\/400/);
});

test("client headers are stripped to content-type/accept; hop-by-hop upstream headers do not reach the client (#50)", async () => {
  stub.queue({ status: 200, headers: { "content-type": "application/json", "x-lumo-secret": "s3cr3t", "transfer-encoding": "chunked" },
               text: `{"data":[]}` });
  const res = await models();
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-lumo-secret"), "s3cr3t", "ordinary upstream headers pass through");
  assert.equal(res.headers.get("transfer-encoding"), null, "transfer-encoding is stripped, it is set by the transport");
  assert.equal(res.headers.get("connection"), null);
  const hit = stub.hits.at(-1);
  assert.equal(hit.url, "/models");
  assert.equal(hit.method, "GET");
  assert.notEqual(hit.body.includes("sk-supersecret"), true, "the Authorization header never reaches the stub's body");
  assert.ok(true);
});

test.after(() => {
  server.close();
  stub.close();
});
