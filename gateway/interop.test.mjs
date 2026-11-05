// Run: node --test gateway/interop.test.mjs
// Interop test: a real local LiteLLM instance (the same binary the gateway service
// runs, from ~/.claude-lumo/litellm-venv) must accept what foreign clients send at the
// live gateway on :4000, with no client-side changes (#183):
//   1. a plain Anthropic /v1/messages request passes through untouched: the upstream
//      sees no reasoning effort and no thinking key (byte-transparent baseline),
//   2. the legacy thinking shape {type:"enabled", budget_tokens:N} is translated to
//      reasoning_effort for the OpenAI upstream (LiteLLM does this natively; this test
//      locks the behavior in against LiteLLM upgrades and gateway config drift),
//   3. an unknown claude-* model name routes through the "*" wildcard instead of
//      failing, and comes back as a well-formed Anthropic message.
//
// Hermetic: never touches the live stack. It binds its own two test ports
// (LUMO_TEST_PORT, default 18123, and the next port for a mock OpenAI upstream;
// 4000/8790 are refused outright), writes its own litellm.yaml into a temp dir
// with a throwaway master key (the real master.key is never read), spawns LiteLLM on
// the test port against the mock, and kills every process it started.
//
// Injection seams (per repo convention, env-based here): LUMO_TEST_PORT (LiteLLM),
// LUMO_MOCK_PORT (mock upstream), LUMO_LITELLM_VENV (venv root) or LUMO_LITELLM_BIN
// (launcher path). CI installs only pytest and pyyaml, so when the venv is absent the
// whole suite skips; see the issue referenced from the interop PR for running it for
// real (locally or on a runner with the venv).
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

// Live-stack ports (LiteLLM :4000, proxy :8790). A test that forgot
// its env override must fail loudly here, not shadow a running service (#71 again).
const FORBIDDEN = new Set([4000, 8790]);
const PORT = Number(process.env.LUMO_TEST_PORT || 18123);
const MOCK_PORT = Number(process.env.LUMO_MOCK_PORT || PORT + 1);
for (const p of [PORT, MOCK_PORT]) {
  assert.ok(!FORBIDDEN.has(p), `refusing to bind live-stack port ${p}; set LUMO_TEST_PORT`);
}

// The same launcher the gateway service runs (Windows: Scripts/litellm.exe, POSIX:
// bin/litellm; see the systemd recipe in docs/GATEWAY.md).
function findLitellm() {
  if (process.env.LUMO_LITELLM_BIN && existsSync(process.env.LUMO_LITELLM_BIN)) {
    return process.env.LUMO_LITELLM_BIN;
  }
  const venv = process.env.LUMO_LITELLM_VENV || join(homedir(), ".claude-lumo", "litellm-venv");
  const bin = process.platform === "win32"
    ? join(venv, "Scripts", "litellm.exe")
    : join(venv, "bin", "litellm");
  return existsSync(bin) ? bin : null;
}
const BIN = findLitellm();

// A minimal OpenAI chat-completion answer. LiteLLM turns this back into an Anthropic
// message, so the text here is what the client under test must receive.
const mockAnswer = () => JSON.stringify({
  id: "chatcmpl-mock", object: "chat.completion", created: Math.floor(Date.now() / 1000),
  model: "openai/mock",
  choices: [{ index: 0, message: { role: "assistant", content: "PONG" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
});

const post = async (url, key, body) => fetch(url, {
  method: "POST",
  headers: {
    "authorization": `Bearer ${key}`,
    "content-type": "application/json",
    "anthropic-version": "2023-06-01",
  },
  body: JSON.stringify(body),
  signal: AbortSignal.timeout(60_000),
});

// Fetch and parse in one step; assert messages can then quote the text freely.
const postJson = async (url, key, body) => {
  const r = await post(url, key, body);
  const text = await r.text();
  return { status: r.status, text, msg: (() => { try { return JSON.parse(text); } catch { return null; } })() };
};

const state = { child: null, server: null, key: null, base: null, seen: [], dir: null };

async function boot(ctx) {
  state.dir = mkdtempSync(join(tmpdir(), "lumo-interop-"));
  state.key = "sk-test-" + randomUUID().toString().replace(/-/g, ""); // throwaway, never the real one
  const yaml = join(state.dir, "litellm.yaml");
  // Mirrors the repo config's shape: an exact lumo-max entry plus the "*" wildcard
  // that catches unknown (claude-*) names. hosted_vllm (like the live config) speaks
  // OpenAI chat completions; the plain openai/ provider would hit /v1/responses.
  writeFileSync(yaml, `model_list:
  - model_name: lumo-max
    litellm_params:
      model: hosted_vllm/mock
      api_base: http://127.0.0.1:${MOCK_PORT}/v1
      api_key: mock-upstream-key
  - model_name: "*"
    litellm_params:
      model: hosted_vllm/mock
      api_base: http://127.0.0.1:${MOCK_PORT}/v1
      api_key: mock-upstream-key
litellm_settings:
  drop_params: true
router_settings:
  # Same as the live config: a test upstream hiccup must not cool the deployment
  # down and 429 the later scenarios.
  disable_cooldowns: true
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
`);

  // The mock upstream records every body LiteLLM forwards, and answers like a
  // plain OpenAI endpoint. Loopback only, like everything in this stack.
  state.server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      let body = null;
      try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch {}
      state.seen.push({ method: req.method, url: req.url, body });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(mockAnswer());
    });
  });
  await new Promise((resolve, reject) => {
    state.server.once("error", reject);
    state.server.listen(MOCK_PORT, "127.0.0.1", resolve);
  });

  state.base = `http://127.0.0.1:${PORT}`;
  state.child = spawn(BIN, ["--config", yaml, "--host", "127.0.0.1", "--port", String(PORT)], {
    cwd: state.dir,
    env: { ...process.env, LITELLM_MASTER_KEY: state.key, PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "ignore", "pipe"],
  });
  // Keep the last stderr lines to explain a failed boot; never printed on success.
  const tail = [];
  state.child.stderr.on("data", (d) => {
    tail.push(String(d));
    if (tail.length > 60) tail.shift();
  });
  state.tail = tail;

  // /health/liveliness answers without calling the upstream, unlike /health.
  const deadline = Date.now() + 90_000;
  for (;;) {
    if (state.child.exitCode !== null) {
      throw new Error(`litellm exited early (code ${state.child.exitCode}): ${tail.join("")}`);
    }
    try {
      const r = await fetch(`${state.base}/health/liveliness`, { signal: AbortSignal.timeout(3000) });
      if (r.ok) break;
    } catch {}
    if (Date.now() > deadline) throw new Error("litellm did not become healthy within 90s");
    await new Promise((r) => setTimeout(r, 500));
  }
}

async function shutdown() {
  const child = state.child;
  if (child && child.exitCode === null) {
    if (process.platform === "win32") {
      // Only the PID we spawned, tree-killed: litellm.exe has no children here.
      await new Promise((resolve) => {
        const tk = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
        tk.on("close", resolve);
        tk.on("error", resolve);
      });
    } else {
      child.kill("SIGTERM");
      await new Promise((resolve) => {
        child.once("close", resolve);
        setTimeout(resolve, 5000).unref();
      });
    }
  }
  if (state.server) await new Promise((resolve) => state.server.close(resolve));
  if (state.dir) rmSync(state.dir, { recursive: true, force: true });
}

const suite = BIN ? test : test.skip; // skips everywhere the venv is absent (CI)

suite("plain Anthropic request is answered and forwarded untouched", async () => {
  state.seen.length = 0;
  const { status, text, msg } = await postJson(`${state.base}/v1/messages`, state.key, {
    model: "lumo-max", max_tokens: 16,
    messages: [{ role: "user", content: "PING" }],
  });
  assert.equal(status, 200, text);
  assert.equal(msg.type, "message");
  assert.equal(msg.role, "assistant");
  assert.equal(msg.content[0].type, "text");
  assert.equal(msg.content[0].text, "PONG");
  // Transparency baseline: no thinking block in, so nothing reasoning-shaped out.
  const hit = state.seen.find((s) => s.url.startsWith("/v1/chat/completions"));
  assert.ok(hit, "mock upstream saw no completion request");
  assert.equal(hit.body.reasoning_effort, undefined, "plain request must not gain a reasoning effort");
  assert.equal(hit.body.thinking, undefined, "plain request must not gain a thinking block");
});

suite("legacy thinking block is translated to a reasoning effort", async () => {
  state.seen.length = 0;
  const { status, text, msg } = await postJson(`${state.base}/v1/messages`, state.key, {
    model: "lumo-max", max_tokens: 8192,
    thinking: { type: "enabled", budget_tokens: 4096 },
    messages: [{ role: "user", content: "PING" }],
  });
  assert.equal(status, 200, text);
  assert.equal(msg.type, "message");
  assert.equal(msg.content[0].text, "PONG");
  // 4096 budget maps to reasoning_effort "high" in LiteLLM's buckets, and the
  // Anthropic-only "thinking" key must not be forwarded to the OpenAI upstream.
  const hit = state.seen.find((s) => s.url.startsWith("/v1/chat/completions"));
  assert.ok(hit, "mock upstream saw no completion request");
  assert.equal(hit.body.reasoning_effort, "high");
  assert.equal(hit.body.thinking, undefined, "thinking key must not reach the upstream");
});

suite("unknown claude-* model name routes through the wildcard", async () => {
  state.seen.length = 0;
  const { status, text, msg } = await postJson(`${state.base}/v1/messages`, state.key, {
    model: "claude-sonnet-4-5", max_tokens: 16,
    messages: [{ role: "user", content: "PING" }],
  });
  assert.equal(status, 200, text);
  assert.equal(msg.type, "message");
  assert.equal(msg.content[0].type, "text");
  assert.equal(msg.content[0].text, "PONG");
  assert.ok(state.seen.some((s) => s.url.startsWith("/v1/chat/completions")),
    "claude-* request was not routed to an upstream");
});

if (BIN) {
  test.before(async () => {
    await boot();
  });
  test.after(async () => {
    await shutdown();
  });
} else {
  // The boot/after hooks above only run inside the suite; make the skip reason visible.
  test("litellm venv not found: live interop skipped (install ~/.claude-lumo/litellm-venv)", { skip: true }, () => {});
}
