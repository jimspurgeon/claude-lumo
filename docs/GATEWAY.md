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
[README](../README.md) (Firefox users need nothing extra). To set them
up, run `powershell -File gateway/install-supervisor.ps1 -Install` from a checkout of
this repo and sign out and back in once if asked; to tear them down again, run
`uninstall.sh` (or pass `-Uninstall` to the same script), which stops and removes both
services. Linux and macOS have no installer, but the same two services can be set up
by hand from the recipes below: [systemd](#always-on-on-linux-systemd) on Linux,
[launchd](#always-on-on-macos-launchd) on macOS.

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
