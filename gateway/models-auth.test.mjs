// Run: node --test gateway/models-auth.test.mjs
// Issue #202: unauthenticated GET /models returned 500 "Internal Server Error"
// on the live gateway (bad key got a 400, valid key a 200). LiteLLM 1.104.2's
// _user_api_key_auth_builder raises a bare Exception("No api key passed in.")
// when master_key is set and no Authorization header is present, which FastAPI's
// generic handler turns into a 500. The fix is the custom_auth hook in
// gateway/lumo_models_auth.py (registered in gateway/litellm.yaml); this test
// boots a real LiteLLM on a test port with that exact hook and asserts:
//   1. keyless GET /models answers 401, not 500,
//   2. a wrong key answers 401,
//   3. the master key answers 200 and /v1/models reports the per-model limits
//      from model_info (131072 in / 32768 out) for lumo-max and lumo-lite,
//   4. valid-key /v1/messages traffic is unchanged (the hook must not disturb
//      the path every client actually uses),
//   5. /health/liveliness stays keyless (watchdog and boot polls depend on it).
//
// Hermetic like gateway/interop.test.mjs: own test ports (LUMO_MODELS_PORT,
// default 18125, and the next port for a mock OpenAI upstream; 4000/8790/8080
// refused), throwaway master key, own temp config (the repo yaml itself is
// never touched), kills only the processes it started. The custom-auth module
// is copied from the repo's gateway/ next to the temp yaml, because LiteLLM
// loads custom_auth relative to the config file's directory (get_instance_fn).
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { copyFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";

const FORBIDDEN = new Set([4000, 8790, 8080]);
const PORT = Number(process.env.LUMO_MODELS_PORT || 18125);
const MOCK_PORT = Number(process.env.LUMO_MODELS_MOCK_PORT || PORT + 1);
for (const p of [PORT, MOCK_PORT]) {
  assert.ok(!FORBIDDEN.has(p), `refusing to bind live-stack port ${p}; set LUMO_MODELS_PORT`);
}

const GATEWAY_DIR = dirname(fileURLToPath(import.meta.url));

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

const mockAnswer = () => JSON.stringify({
  id: "chatcmpl-mock", object: "chat.completion", created: Math.floor(Date.now() / 1000),
  model: "openai/mock",
  choices: [{ index: 0, message: { role: "assistant", content: "PONG" }, finish_reason: "stop" }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
});

const state = { child: null, server: null, key: null, base: null, dir: null };

async function boot() {
  state.dir = mkdtempSync(join(tmpdir(), "lumo-models-auth-"));
  state.key = "sk-test-" + randomUUID().toString().replace(/-/g, ""); // throwaway, never the real one
  // LiteLLM loads custom_auth from the config's directory: copy the real hook
  // module next to the temp yaml so the test runs the shipped code, not a copy.
  copyFileSync(join(GATEWAY_DIR, "lumo_models_auth.py"), join(state.dir, "lumo_models_auth.py"));
  const yaml = join(state.dir, "litellm.yaml");
  // Mirrors the repo config after this PR: model_info on the hosted models,
  // custom_auth under general_settings, same wildcards LiteLLM needs.
  writeFileSync(yaml, `model_list:
  - model_name: lumo-lite
    litellm_params:
      model: hosted_vllm/lumo-lite
      api_base: http://127.0.0.1:${MOCK_PORT}/v1
      api_key: mock-upstream-key
    model_info:
      max_input_tokens: 131072
      max_output_tokens: 32768
  - model_name: lumo-max
    litellm_params:
      model: hosted_vllm/lumo-max
      api_base: http://127.0.0.1:${MOCK_PORT}/v1
      api_key: mock-upstream-key
    model_info:
      max_input_tokens: 131072
      max_output_tokens: 32768
litellm_settings:
  drop_params: true
router_settings:
  disable_cooldowns: true
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  custom_auth: lumo_models_auth.user_api_key_auth
`);

  state.server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
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
  const tail = [];
  state.child.stderr.on("data", (d) => {
    tail.push(String(d));
    if (tail.length > 60) tail.shift();
  });
  state.tail = tail;

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

const get = async (url, key) => fetch(url, {
  method: "GET",
  ...(key !== undefined ? { headers: { authorization: `Bearer ${key}` } } : {}),
  signal: AbortSignal.timeout(30_000),
});

async function getJson(url, key) {
  const r = await get(url, key);
  const text = await r.text();
  return { status: r.status, text, msg: (() => { try { return JSON.parse(text); } catch { return null; } })() };
}

const suite = BIN ? test : test.skip; // skips everywhere the venv is absent (CI)

suite("keyless model list gets a clean 401, not a 500", async () => {
  for (const path of ["/models", "/v1/models"]) {
    const { status, text } = await getJson(`${state.base}${path}`, undefined);
    assert.equal(status, 401, `${path}: expected 401, got ${status}: ${text}`);
  }
});

suite("wrong key gets a 401", async () => {
  const { status, text } = await getJson(`${state.base}/v1/models`, "sk-not-the-master-key");
    assert.ok(status >= 400 && status < 500, `expected 4xx, got ${status}: ${text}`);
  assert.equal(status, 401, text);
});

// Headers arrive latin-1-decoded, so "caf\xe9" is exactly what the hook sees
// from a fuzzer sending a non-ASCII bearer; it must reject, never 500.
suite("non-ASCII key gets a 401, not a 500", async () => {
  const { status, text } = await getJson(`${state.base}/v1/models`, "caf\xe9");
  assert.equal(status, 401, `expected 401, got ${status}: ${text}`);
});

suite("master key lists models with their token limits", async () => {
  const { status, text, msg } = await getJson(`${state.base}/v1/models`, state.key);
  assert.equal(status, 200, text);
  assert.ok(Array.isArray(msg?.data), `unexpected /v1/models body: ${text}`);
  for (const name of ["lumo-max", "lumo-lite"]) {
    const entry = msg.data.find((m) => m.id === name);
    assert.ok(entry, `no ${name} in the model list: ${text}`);
    // LiteLLM 1.104.2 emits the limits as top-level fields on the model entry
    // (create_model_info_response); model_info nested inside the entry is
    // tolerated so the test survives a shape tweak on upgrade.
    const info = entry.model_info ?? entry;
    assert.equal(info.max_input_tokens, 131072, `${name} max_input_tokens: ${JSON.stringify(entry)}`);
    assert.equal(info.max_output_tokens, 32768, `${name} max_output_tokens: ${JSON.stringify(entry)}`);
  }
});

suite("master-key message traffic is unchanged", async () => {
  const r = await fetch(`${state.base}/v1/messages`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${state.key}`,
      "content-type": "application/json",
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({ model: "lumo-max", max_tokens: 16, messages: [{ role: "user", content: "PING" }] }),
    signal: AbortSignal.timeout(60_000),
  });
  const text = await r.text();
  assert.equal(r.status, 200, text);
  const msg = JSON.parse(text);
  assert.equal(msg.content[0].text, "PONG");
});

suite("health endpoints stay keyless", async () => {
  const { status } = await getJson(`${state.base}/health/liveliness`, undefined);
  assert.equal(status, 200);
});

if (BIN) {
  test.before(async () => {
    await boot();
  });
  test.after(async () => {
    await shutdown();
  });
} else {
  test("litellm venv not found: models-auth interop skipped (install ~/.claude-lumo/litellm-venv)", { skip: true }, () => {});
}
