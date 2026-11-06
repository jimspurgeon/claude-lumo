# Using the gateway from another harness

The models behind claude-lumo sit behind a local gateway that speaks both the Anthropic
protocol and the OpenAI protocol on one port. Any tool that can be pointed at a
compatible endpoint can use them. This page is the whole contract: one base URL, one
key file, two model IDs.

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

**LiteLLM version:** the gateway targets LiteLLM 1.104.2, the hash-pinned version in
`requirements.txt` (regenerate with `pip-compile` after bumping the pin in
`requirements.in`). Older versions are unsupported; newer ones should work but are
untested. Note that `gateway/lumo_models_auth.py` (the `custom_auth` hook that turns
keyless/wrong-key 500s into clean 401s, issue #202) is a workaround for a LiteLLM
1.104.2 bug; if a future LiteLLM release returns 401 natively, the hook can be
retired then — re-check on upgrade, and refresh the upstream line-number citations
in its docstring, which drift between releases.

**Model IDs:**

| ID | Routes to | Good for |
|----|-----------|----------|
| `lumo-max` | Hosted Lumo, full strength | Default. Real work. |
| `lumo-lite` | Hosted Lumo, smaller tier | Quick and mechanical tasks, background agents |

The full route table, including timeouts, lives in [`gateway/litellm.yaml`](../gateway/litellm.yaml).
Send a model ID the gateway knows. Unknown names, including `claude-*` names a harness
might default to, do not fail: they fall through to `lumo-max` via the `"*"` wildcard
route, except names containing `haiku` (e.g. `claude-3-5-haiku-20241022`), which match
the `"*haiku*"` route first and go to `lumo-lite`. Parameters the upstream does not
understand (an Anthropic `thinking` block among them) are translated or dropped
(`drop_params`). This is a fallback, so prefer sending an explicit ID;
`gateway/interop.test.mjs` locks the behavior in.

**Reasoning effort:** how much the model reasons before answering. There is no
way to advertise this through `/v1/models`, so enter it by hand where a client
has a slot for it:

| ID | Accepted values |
|----|-----------------|
| `lumo-max` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| `lumo-lite` | (none; leave the field empty. The model has no reasoning stage.) |

Send it the way your client expects (Anthropic `thinking`, or a
`reasoning_effort`/`reasoning` parameter). The gateway translates or passes it
through; an unrecognized value is dropped (`drop_params`), never fatal.

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
[README](../README.md) (Firefox users need nothing extra). To set them
up, run this from an elevated PowerShell (from the standard install location;
adjust the path if you cloned elsewhere):
`powershell -File "$env:USERPROFILE\.claude\lumo\gateway\install-supervisor.ps1" -Install`,
and sign out and back in once if asked; to tear them down again, run
`uninstall.sh` (or pass `-Uninstall` to the same script), which stops and removes both
services. Linux and macOS have no installer, but the same two services can be set up
by hand from the recipes below: [systemd](#always-on-on-linux-systemd) on Linux,
[launchd](#always-on-on-macos-launchd) on macOS.

The claude-lumo launcher remains as a fallback: if it finds the services already
answering on :4000 and :8790 it just uses them; otherwise it starts the gateway itself
the old way, so a machine without the services still works (see
[How it works](../README.md#how-it-works)). When the services are present, the watchdog
stops relaunching processes and only reports problems in its log. The watchdog has no
401/cookie-expiry logic — that re-auth-and-retry path lives in `gateway/upstream.mjs` (#190).

## Plain Claude Code

Claude Code can use the gateway without the claude-lumo wrapper. Put this in your
user-scope settings (`~/.claude/settings.json`), replacing `YOU` with your username and
`python` with `python3` on macOS and Linux. Paths must be absolute; the `{{PLACEHOLDER}}`
tokens used inside claude-lumo's own settings do not work in a plain settings file.

On macOS and Linux the path root changes as well as the username: there is no `C:/`.
Write the paths as `/Users/YOU/...` on macOS or `/home/YOU/...` on Linux. Claude Code
runs the `apiKeyHelper` command through the system shell (`/bin/sh` on macOS and
Linux, `cmd` on Windows), so on those systems the tilde form also works:
`python3 ~/.claude/lumo/gateway/api-key.py ~/.claude-lumo`. The snippet shows the
absolute form because one shape has to serve every platform, and the Windows shell
does not expand `~`.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_MODEL": "lumo-max",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "lumo-max",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "lumo-lite",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "lumo-lite",
    "ANTHROPIC_SMALL_FAST_MODEL": "lumo-lite",
    "CLAUDE_CODE_SUBAGENT_MODEL": "lumo-lite",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "apiKeyHelper": "python \"C:/Users/YOU/.claude/lumo/gateway/api-key.py\" \"C:/Users/YOU/.claude-lumo\"",
  "modelPicker": {
    "options": [
      { "model": "lumo-max", "label": "lumo-max", "description": "Hosted Lumo, full strength" },
      { "model": "lumo-lite", "label": "lumo-lite", "description": "Hosted Lumo, smaller tier" }
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

The `DEFAULT_*` variables matter as much as `ANTHROPIC_MODEL`: without them a `/model
opus` pick, or a subagent whose tier is not pinned, resolves to a built-in `claude-*`
name and silently burns the max-tier route for a request you meant to be cheap. Map
every tier explicitly, as above; the launcher's own settings
(`settings-lumo.json`) use the same mapping.

## LearnHarness

LearnHarness spawns a plain `claude.exe`, so it is wired up the same way, but through
environment variables set before the app starts instead of a settings file. The launcher
`~/.claude/scripts/learnharness-lumo.cmd` reads the master key at launch time (never
storing it), exports `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and the model aliases,
then starts the app. Use it as the template for any app that spawns `claude` and cannot
read a settings file. It lives outside the repo; copy the pattern, not the path.

## Always-on on Linux (systemd)

The Windows services are the primary path, but the two processes themselves are plain
Node and Python and run anywhere. This recipe mirrors, as user units, exactly what
`gateway/install-supervisor.ps1` wraps on Windows: the venv `litellm` with
`--config ... --host 127.0.0.1 --port 4000` (working directory the state dir), and
`node --no-warnings proxy.mjs` (working directory the `gateway/` directory of your
claude-lumo checkout). Written from that Windows implementation; not tested on this
machine (no Linux here), so corrections from Linux users are welcome.

First create an environment file for the master key, so the key is read from
`master.key` once and never pasted into a unit file:

```sh
mkdir -p ~/.config/lumo-gateway
printf 'LITELLM_MASTER_KEY=%s\n' "$(cat ~/.claude-lumo/master.key)" \
  > ~/.config/lumo-gateway/litellm.env
chmod 600 ~/.config/lumo-gateway/litellm.env
```

Then two files in `~/.config/systemd/user/`:

```ini
# ~/.config/systemd/user/lumo-gateway-litellm.service
[Unit]
Description=Lumo LiteLLM gateway (127.0.0.1:4000)

[Service]
WorkingDirectory=%h/.claude-lumo
ExecStart=%h/.claude-lumo/litellm-venv/bin/litellm \
  --config %h/.claude/lumo/gateway/litellm.yaml --host 127.0.0.1 --port 4000
EnvironmentFile=%h/.config/lumo-gateway/litellm.env
Environment=PYTHONIOENCODING=utf-8
Environment=CLAUDE_LUMO_STATE_DIR=%h/.claude-lumo
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```ini
# ~/.config/systemd/user/lumo-gateway-proxy.service
[Unit]
Description=Lumo proxy (127.0.0.1:8790)

[Service]
WorkingDirectory=%h/.claude/lumo/gateway
ExecStart=node --no-warnings proxy.mjs
Environment=CLAUDE_LUMO_STATE_DIR=%h/.claude-lumo
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`%h` is systemd's specifier for your home directory. If the manager's PATH does not
include `node`, spell out its absolute path from `command -v node` in `ExecStart`.
Then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now lumo-gateway-litellm.service lumo-gateway-proxy.service
```

User units stop when your last session logs out unless lingering is enabled, so run
`sudo loginctl enable-linger "$USER"` once for a truly always-on machine.

Both units log to journald, which needs no extra configuration here (contrast the
Windows services, which roll their own files at 10 MB × 2). Read live output with
`journalctl --user -f -u lumo-gateway-litellm.service -u lumo-gateway-proxy.service`
and past runs by dropping `-f`. The same caveat applies as for the Windows services:
not tested on this machine.

## Always-on on macOS (launchd)

The same two command lines as LaunchAgents. launchd has no environment-file
indirection, so the master key must be inside the plist's `EnvironmentVariables`
dict: paste it there once at install time, from `cat ~/.claude-lumo/master.key`, then
`chmod 600` the plist so it is exactly as protected as `master.key` itself. Never
commit or share the edited file. Also written from the Windows implementation; not
tested on this machine (no macOS here), so corrections from macOS users are welcome.
launchd does not expand `~`, so every path is spelled out; replace `YOU` with your
username.

Two files in `~/Library/LaunchAgents/`:

```xml
<!-- ~/Library/LaunchAgents/ai.lumo.litellm.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.lumo.litellm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.claude-lumo/litellm-venv/bin/litellm</string>
    <string>--config</string>
    <string>/Users/YOU/.claude/lumo/gateway/litellm.yaml</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>4000</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOU/.claude-lumo</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LITELLM_MASTER_KEY</key><string>PASTE THE KEY FROM master.key HERE</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
    <key>CLAUDE_LUMO_STATE_DIR</key><string>/Users/YOU/.claude-lumo</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```xml
<!-- ~/Library/LaunchAgents/ai.lumo.proxy.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.lumo.proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/node</string>
    <string>--no-warnings</string>
    <string>proxy.mjs</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOU/.claude/lumo/gateway</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLAUDE_LUMO_STATE_DIR</key><string>/Users/YOU/.claude-lumo</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

Find your node with `command -v node` and use that path; Homebrew on Apple silicon puts
it at `/opt/homebrew/bin/node`. Then load both agents (and again after any edit):

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.lumo.litellm.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.lumo.proxy.plist
```

To unload them again, replace `bootstrap` with `bootout` in each line.

launchd, unlike systemd, captures nothing by default: without the keys below a crashed
service leaves no trace. Add them to each plist's top-level `<dict>` (outside
`EnvironmentVariables`), pointing wherever you want the logs; the Windows services'
equivalent rolls at 10 MB and keeps 2, which launchd does not manage for you:

```xml
  <key>StandardOutPath</key><string>/tmp/ai.lumo.litellm.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/ai.lumo.litellm.err.log</string>
```
(same pair for the proxy plist). Check them with `log show`/`tail`. As above, this
recipe was written from the Windows implementation and is not tested on this machine.

## gg-coder

gg-coder can use the gateway as-is: it speaks the Anthropic protocol, so it needs
nothing but the three facts every client needs. Where its settings live depends on the
install; the values are always these:

- **Base URL:** `http://127.0.0.1:4000` (in gg-coder's provider/API settings)
- **API key:** the contents of `~/.claude-lumo/master.key`, read at setup time (paste
  it into gg-coder's key field, or point the key-setting command at the file)
- **Model:** `lumo-max` (map the smaller tier to `lumo-lite` if gg-coder exposes a
  separate fast/cheap model slot)

If gg-coder is configured to send an Anthropic `thinking` block
(`{type: "enabled", budget_tokens: N}`) or a `claude-*` model name, do not work around
it client-side: both are accepted. The thinking block is translated to a reasoning
effort for the upstream, and unknown model names fall through to `lumo-max` (or
`lumo-lite` when the name contains `haiku`; see
[The contract](#the-contract)).

## Adding a new harness

Any client that speaks either protocol works. It needs exactly three facts:

1. **Base URL:** `http://127.0.0.1:4000`. Anthropic clients call `/v1/messages`;
   OpenAI clients call `/v1/chat/completions` (and `/v1/models` to list).
2. **API key:** the contents of `~/.claude-lumo/master.key`, sent as the bearer
   credential. Print it on demand, without pasting it into a config file, with:
   `python gateway/api-key.py ~/.claude-lumo`
3. **Model ID:** one of `lumo-max`, `lumo-lite` (table above).

A client with model discovery can skip fact 3 entirely: `GET /v1/models` on the
base URL lists the IDs, so a picker that reads it shows them automatically. It
also reports each hosted model's context limits (`max_input_tokens` 131072,
`max_output_tokens` 32768). The key is required: a request without it (or with a
wrong one) gets a 401, so enter the key before probing for models.

Generic configuration for an Anthropic-protocol client (SDK or settings form):

```
base_url = http://127.0.0.1:4000
apiKey   = <read from ~/.claude-lumo/master.key, e.g. via gateway/api-key.py>
model    = lumo-max
```

Generic configuration for an OpenAI-protocol client:

```
base_url = http://127.0.0.1:4000/v1
chat completions path = /chat/completions
apiKey   = <read from ~/.claude-lumo/master.key, e.g. via gateway/api-key.py>
model    = lumo-max
```

When a client's model slots have tier-like names, alias them like this (the same
mapping claude-lumo's own launcher uses):

| Client slot | Send | Falls back to |
|-------------|------|---------------|
| Main / full-strength model | `lumo-max` | `lumo-max` |
| Mid tier (sonnet-class) | `lumo-max` or `lumo-lite` (your cost call) | `lumo-max` |
| Fast / cheap tier (haiku-class) | `lumo-lite` | `lumo-max` |
| Any `claude-*` name the client insists on | leave it | `lumo-max` (`"*"` wildcard; `*haiku*` names go to `lumo-lite`) |

The constraint worth repeating: send a model ID the gateway knows. A harness that
defaults to a `claude-*` name still works (it falls through to `lumo-max`, or
`lumo-lite` when the name contains `haiku`, see
[The contract](#the-contract)), but set the client's model to `lumo-max` when you can,
so routing is explicit instead of accidental. If the harness cannot be configured
at all, aliasing still saves the request; only a harness that also rewrites the
protocol needs the launcher treatment LearnHarness got.

## The `lumo-gateway` CLI

`lumo-gateway` is the control-plane command for the gateway itself (as opposed to
`claude-lumo`, which drives Claude Code through the gateway). It ships at the repo
root and is put on PATH by `install.sh` (both modes). Gateway-only machines — boxes
that serve other harnesses and never run Claude Code — install it with:

```bash
git clone https://github.com/jimspurgeon/claude-lumo ~/.lumo-gateway
bash ~/.lumo-gateway/install.sh --gateway-only
```

That installs only: the repo checkout (`~/.lumo-gateway`), the LiteLLM virtualenv,
the master key, and the `lumo-gateway` CLI. It never touches `~/.claude`, Node.js,
or Claude Code. Preview it with `--dry-run`.

### omp in one command

`lumo-gateway omp` (or the standalone
[`install-omp-full.sh`](../install-omp-full.sh)) is the full path for the omp
harness: it installs the gateway as above, installs omp with the official
installer (<https://omp.sh/install>, printed before running), wires omp to the
gateway (`~/.omp/agent/models.yml` gains a `litellm` provider at
`http://127.0.0.1:4000/v1`; the master key goes into omp's credential store via
`omp auth import` from a chmod-600 temp file — the key value is never printed,
echoed, or passed in argv), and finally syncs the personal config from the
private `jimspurgeon/omp-config` repo into `~/.omp/agent` (keeping local files
that are newer). Every stage is idempotent: re-running prints already-done and
moves on. The Windows one-liner wrapper is
[`install-omp-full.ps1`](../install-omp-full.ps1).

### What `setup` prints

When `setup` finishes it prints a fact sheet — everything a client needs on one
screen — and then handles the two credentials through the clipboard, so nothing
secret ever lands in the terminal or a scrollback buffer:

- **Base URL** `http://127.0.0.1:4000` — copied to the clipboard automatically
  (clip.exe on Windows, pbcopy on macOS, wl-copy/xclip/xsel on Linux; if none is
  present the URL is simply printed).
- **Protocols**: OpenAI-compatible (`/v1/chat/completions`, `/v1/models`) and
  Anthropic Messages (`/v1/messages`).
- **Models**: one line per model read from `gateway/litellm.yaml` at runtime,
  with its token limits — not a hardcoded list, so adding a model to the yaml
  is all it takes to see it here.
- **Master key path** (`~/.claude-lumo/master.key`) — the key value is never
  printed. On the prompt "press Enter to copy the API key", one Enter puts the
  key straight on the clipboard; in a non-interactive session the step is
  skipped and the key path is printed instead.
- **Services**: whether `LumoGatewayLiteLLM` is running, and if not, the exact
  command to install the always-on services.
- **Wiring**: two generic bullets — point any OpenAI-compatible client at the
  base URL, or register the gateway as a LiteLLM provider specifically (model
  id `litellm/lumo-max`, `api_base http://127.0.0.1:4000/v1`). No per-harness
  recipes; the tables above cover individual tools.

| Command | What it does |
| --- | --- |
| `setup` | Gateway-only install (runs `install.sh --gateway-only`): checkout, LiteLLM venv, master key, CLI — then prints the fact sheet and walks the clipboard (URL auto-copied, key on Enter; value never printed). Service installation stays a separate elevated step (printed when absent); safe to re-run |
| `update` | Move to a newer release — not implemented yet (exit 2) |
| `revert` | Roll back to the previous release — not implemented yet (exit 2) |
| `uninstall` | Stop/remove the services and files — not implemented yet (exit 2) |
| `api-key` | Prints the base URL (`http://127.0.0.1:4000`) and the master key **file path** — never the key value |
| `status` | `LumoGatewayLiteLLM` service state + a `:4000` health check |
| `omp` | One-command full install for the omp harness (runs `install-omp-full.sh`): gateway setup + official omp installer + wiring (`models.yml` provider, credential import, default role) + sync of `jimspurgeon/omp-config`; safe to re-run |
| `help` | Full command reference |

Exit codes: `0` success, `1` bad/missing command, `2` placeholder ("not implemented
yet"). Read the key with `cat "$(lumo-gateway api-key | awk '{print $3}')"` — by
design no subcommand ever prints it.
