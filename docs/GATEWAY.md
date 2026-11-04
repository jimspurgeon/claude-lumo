# Using the gateway from another harness

The models behind claude-lumo sit behind a local gateway that speaks both the Anthropic
protocol and the OpenAI protocol on one port. Any tool that can be pointed at a
compatible endpoint can use them. This page is the whole contract: one base URL, one
key file, three model IDs.

## The contract

**Base URL:** `http://127.0.0.1:4000`

**Endpoints:**

- Anthropic protocol: `/v1/messages`
- OpenAI protocol: `/v1/chat/completions`, plus `/v1/models` for listing

**API key:** the LiteLLM master key, stored at `~/.claude-lumo/master.key`. Send it as
the bearer credential your client uses (Authorization header or the client's token
setting). The repo ships a helper, `gateway/api-key.py`, that prints the key; it exists
so the key can reach clients without being pasted into config files. Treat the key file
as a secret: never copy its contents into a settings file, a script, or a bug report.

**Model IDs:**

| ID | Routes to | Good for |
|----|-----------|----------|
| `lumo-max` | Hosted Lumo, full strength | Default. Real work. |
| `lumo-lite` | Hosted Lumo, smaller tier | Quick and mechanical tasks, background agents |
| `lumo-local` | Local llama.cpp server on 127.0.0.1:8080 (LFM 2.5 24B) | Offline use. Can take minutes per reply, since it runs on the local GPU. |

The full route table, including timeouts, lives in [`gateway/litellm.yaml`](../gateway/litellm.yaml).
Send a model ID the gateway knows. Other names (including `claude-*` names a harness
might default to) are not properly mapped yet and may fail; if your client lets you pick
the model string, pick `lumo-max`.

**Streaming:** both `/v1/messages` and `/v1/chat/completions` accept `"stream": true`
and answer with SSE.

**The gateway must be running.** The primary startup path is the pair of always-on
Windows services, LumoGatewayLiteLLM and LumoGatewayProxy, that
[`gateway/install-supervisor.ps1`](../gateway/install-supervisor.ps1) installs with
WinSW. They start at boot and restart themselves if the underlying process dies, so
another harness can connect at any time without starting anything itself. One
credential caveat: the services run as the LocalSystem account, which cannot read
Chromium cookie stores, so Chrome users must fall back to the `login.json` file
described in the
[README](../README.md#step-2-log-in-to-lumo-in-a-browser) (Firefox users need nothing extra). To set them
up, run `powershell -File gateway/install-supervisor.ps1 -Install` from a checkout of
this repo and sign out and back in once if asked; to tear them down again, run
`uninstall.sh` (or pass `-Uninstall` to the same script), which stops and removes both
services. Linux and macOS have no equivalent yet.

The claude-lumo launcher remains as a fallback: if it finds the services already
answering on :4000 and :8790 it just uses them; otherwise it starts the gateway itself
the old way, so a machine without the services still works (see
[How it works](../README.md#how-it-works)). When the services are present, the watchdog
stops relaunching processes and only reports problems in its log.

## Plain Claude Code

Claude Code can use the gateway without the claude-lumo wrapper. Put this in your
user-scope settings (`~/.claude/settings.json`), replacing `YOU` with your username and
`python` with `python3` on macOS and Linux. Paths must be absolute; the `{{PLACEHOLDER}}`
tokens used inside claude-lumo's own settings do not work in a plain settings file.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_MODEL": "lumo-max",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "lumo-lite",
    "ANTHROPIC_SMALL_FAST_MODEL": "lumo-lite",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "apiKeyHelper": "python \"C:/Users/YOU/.claude/lumo/gateway/api-key.py\" \"C:/Users/YOU/.claude-lumo\"",
  "modelPicker": {
    "options": [
      { "model": "lumo-max", "label": "lumo-max", "description": "Hosted Lumo, full strength" },
      { "model": "lumo-lite", "label": "lumo-lite", "description": "Hosted Lumo, smaller tier" },
      { "model": "lumo-local", "label": "lumo-local (llama.cpp local)", "description": "Local LFM 2.5 24B via llama-server on port 8080" }
    ]
  }
}
```

The `apiKeyHelper` line is how the key reaches Claude Code, and it takes the state
directory (where `master.key` lives) as its argument. One gotcha: if `ANTHROPIC_AUTH_TOKEN`
or `ANTHROPIC_API_KEY` is set in your environment, it wins over the helper and requests
fail with the wrong credential. Clear them.

To opt a single project in instead of every session, put the same JSON block in
`.claude/settings.local.json` at that project's root. Project settings override user
settings, so the gateway config then applies only inside that project.

Two notes. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` stops non-essential calls to
Anthropic's own endpoints, which have no business running when traffic goes to the
gateway. And Claude Code logs a harmless `unrecognized_model` note for `lumo-max` on
stderr, since the name is not in its built-in model registry; the request still works.

## LearnHarness

LearnHarness spawns a plain `claude.exe`, so it is wired up the same way, but through
environment variables set before the app starts instead of a settings file. The launcher
`~/.claude/scripts/learnharness-lumo.cmd` reads the master key at launch time (never
storing it), exports `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and the model aliases,
then starts the app. Use it as the template for any app that spawns `claude` and cannot
read a settings file. It lives outside the repo; copy the pattern, not the path.

## Adding a new harness

Any client needs exactly three facts, all on this page:

1. Base URL: `http://127.0.0.1:4000`, protocol Anthropic or OpenAI, endpoints above.
2. Key source: `~/.claude-lumo/master.key` (bearer credential).
3. Model ID: one of `lumo-max`, `lumo-lite`, `lumo-local`.

The constraint worth repeating: send a model ID the gateway knows. Some harnesses
default to `claude-*` model names; those are not mapped yet and the request may fail.
Set the client's model to `lumo-max` when in doubt. If the harness cannot be configured
at all, its requests cannot be routed, and it needs the launcher treatment LearnHarness
got.
