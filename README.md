# claude-lumo

Run [Claude Code](https://claude.com/code) on **Proton Lumo** with one command.

`claude-lumo` looks and behaves exactly like `claude` — same prompts, same
skills, same config in `~/.claude` — but the models behind it are Proton Lumo's
`lumo-max` (GLM 5.3) and `lumo-lite` (Qwen 3.5) instead of Claude's, reached
through a small proxy that runs on your machine. Plain `claude` is untouched
and keeps using Claude's API.

The status line always shows which one you're on: `[CLAUDE] Opus 5.5` or `[LUMO] lumo-max`.

## Quick start

### One-command install (all OSes)

On **Windows** (PowerShell — no bash or Git needed first):

```powershell
irm https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/install.ps1 | iex
```

On **macOS / Linux** (any terminal):

```bash
curl -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/install.sh | bash
```

Both installers check for and auto-install any missing prerequisite (Git,
Node.js 22+, Python 3.12+, Claude Code itself), then set up `claude-lumo`
into `~/.claude/lumo` and put the `claude-lumo` command on your PATH.
Safe to re-run: existing pieces are kept, missing ones filled in.

After install, test it:

```bash
claude-lumo -p "reply with pong"
```

If you get a `pong` reply, you're done. Use `claude-lumo` anywhere you'd use `claude`.

Both installers are idempotent — safe to re-run any time — and always set up the
latest version. Re-running either one-liner above is how you update.

---

## Manual install (step by step, per OS)

### Step 1: Install prerequisites

| Component | Windows | macOS | Ubuntu / Debian |
|-----------|---------|-------|-----------------|
| Git | `winget install Git.Git` or [git-scm.com](https://git-scm.com) | `xcode-select --install` or `brew install git` | `sudo apt install git` |
| Node.js 22+ | `winget install OpenJS.NodeJS.LTS` | `brew install node` | `curl -fsSL https://deb.nodesource.com/setup_22.x \| sudo -E bash -` then `sudo apt install nodejs` |
| Python 3.12+ | `winget install Python.Python.3.12` (tick "Add to PATH") | `brew install python@3.12` | `sudo apt install python3 python3-venv python3-pip` |
| Claude Code | PowerShell: `irm https://claude.ai/install.ps1 \| iex` | `curl -fsSL https://claude.ai/install.sh \| bash` | `npm install -g @anthropic-ai/claude-code` |

Then log in to Claude Code once (`claude` and follow the prompts), and get a
free Lumo account at [lumo.proton.me](https://proton.me/lumo).

### Step 2: Log in to Lumo in a browser

Visit [lumo.proton.me](https://lumo.proton.me) in **Firefox**, **Chrome**,
**Edge**, **Brave**, **Vivaldi**, **Opera** or LibreWolf, and log in.

That's the whole credential setup. The proxy reads your Lumo login straight
from your browser's cookie store — no API keys or passwords to copy anywhere:

| Browser | Support |
|---------|---------|
| Firefox / LibreWolf | Always works (cookie store is plaintext). |
| Chrome / Edge / Brave / Vivaldi / Opera | Works on Windows, macOS and Linux; cookies are decrypted locally. On very new Chrome versions (app-bound cookies) it may fall back to the manual method below. |

**Manual fallback** (any browser, 30 seconds): in your browser's DevTools →
Application → Cookies → `lumo.proton.me`, copy the cookie whose name starts
with `AUTH-` (the name gives the uid, the value is the token). Save it as
`~/.claude-lumo/login.json`:

```json
{"uid": "AUTH-xxxxx without the AUTH- prefix", "token": "the cookie value"}
```

### Step 3: Install claude-lumo

Either clone the repo:

```bash
git clone https://github.com/jimspurgeon/claude-lumo ~/.claude/lumo
bash ~/.claude/lumo/install.sh
```

Or download the ZIP from this repo's **Releases** page, unzip it, open a
terminal in that folder and run `bash install.sh`.

The installer puts a `claude-lumo` command on your PATH — completely separate
from `claude`. `claude` keeps using your Claude plan; `claude-lumo` runs on Lumo.

```bash
claude-lumo -p "reply with pong"
```

---

## Uninstall

Easy way — grab the uninstaller from this repo:

```bash
curl -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/uninstall.sh | bash
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/uninstall.ps1 | iex
```

Or run `bash uninstall.sh` / `powershell -File uninstall.ps1` from a clone of the repo.

Both stop the background proxy and LiteLLM, then remove everything claude-lumo
added: `~/.claude/lumo`, `~/.claude-lumo`, and the PATH shims. Plain `claude`
and your Claude Code login are untouched.

Manual equivalent:

```bash
rm -rf ~/.claude-lumo ~/.claude/lumo
rm -f "$HOME/.local/bin/claude-lumo" "$HOME/.npm-global/bin/claude-lumo" 2>/dev/null
# Windows also: %APPDATA%/npm/claude-lumo*
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `claude-lumo: command not found` after install | Close and reopen your terminal (PATH refresh), or add `$HOME/.local/bin` / `$APPDATA/npm` to PATH manually. |
| `ECONNREFUSED` mid-response | Proxy or LiteLLM crashed. Run any `claude-lumo` command again — it restarts both automatically. |
| 401 from Lumo | Log in again at lumo.proton.me in your browser; the proxy picks up the fresh cookie by itself. |
| "Slow thinking" or stuck at 5 min | Normal on lumo-max (~16 tok/s, minutes-long reasoning). Not a hang if requests keep finishing: `proxy/proxy.log` logs each one, and `curl 127.0.0.1:8790/v1/debug/live` shows what is in flight. `LUMO_CAPTURE=1` on the proxy saves raw requests and streams to `proxy/debug/`. |
| "Invalid tool parameters" on parallel tool calls | Fixed in the proxy (it splits tool calls LiteLLM would merge). If it returns after a LiteLLM upgrade, check `splitToolCallChunks` in `proxy/proxy.mjs`. |
| Agent stops mid-task right after saying what it will do next | Most often the reply mentioned `[DONE]` (an SSE fixture, say): LiteLLM ends the stream on any line containing it, so the proxy escapes it inside JSON (#13). Otherwise Lumo announced a tool call that never arrived intact. The proxy now repairs it or retries once (`dropped_tool`, `repaired_tool` and `tool_retry` in `proxy/proxy.log`); if both fail, the turn ends with a `[claude-lumo] Your tool call was lost in transit` message instead of going silent. See `proxy/tool-guard.mjs`. |
| Failed Edit with a "[claude-lumo]" note | The proxy added the closest real text to an Edit whose old text was not in the file; the model should retry with the text between the markers. |
| `python` / `python3` not found | Install Python 3.12+. On Windows, make sure it's added to PATH during installation. |
| "No Lumo login found" | Log in at lumo.proton.me in a supported browser, or use the manual `login.json` fallback above. |
| `NODE_ENV` warning | Ignore harmless warnings about NODE_ENV from LiteLLM. |

## How it works (no need to memorize)

```
claude-lumo
  └─ Claude Code → LiteLLM (localhost:4000) → Lumo proxy (localhost:8790) → lumo.proton.me
```

The proxy signs every request with your Lumo login, which it reads straight from your
browser's cookies — so there are no passwords or API keys to copy anywhere. It also
transparently reconnects when your Lumo session expires and retries on hiccups.

Two local ports are used: 4000 and 8790. Nothing leaves your machine except traffic to
lumo.proton.me.

Nothing is stored in the repo or sent anywhere else: no credentials in any file, and
re-running the installer is always safe (it skips what already exists).

## Hook tombstone policy

Hook scripts referenced in `settings-lumo.json` are part of the interface for running
sessions: a session keeps the hook commands it started with, so after an update removes
a hook file, that session's Edit calls fail until it is restarted. When retiring a hook,
leave a tombstone that reads stdin and exits 0 silently (see `hooks/edit-assist.py` as
an example) instead of deleting the script.

**Before every release**, run the check:

```bash
python scripts/check-hook-tombstones.py
```

It scans the current branch's history of `settings-lumo.json` and fails if any hook file
it ever referenced is missing from `hooks/` (a tombstone counts as present). It must pass
before a release is cut.

For the same check on every push, install the opt-in pre-push hook:

```bash
ln -s "$PWD/scripts/pre-push" "$(git rev-parse --git-dir)/hooks/pre-push"
```

## Files

| File | What it does |
|---|---|
| `install.sh` | End-to-end setup incl. prerequisite auto-install; safe to re-run |
| `install.ps1` | Windows one-command entry-point: installs Git Bash (with prompt), then runs install.sh |
| `uninstall.sh` | Removes everything claude-lumo added; plain `claude` untouched |
| `uninstall.ps1` | Windows entry-point for the uninstaller |
| `claude-lumo.sh` | Launcher: starts proxy and LiteLLM if down, then runs Claude Code |
| `settings-lumo.json` | Claude Code settings for Lumo sessions: model mapping, status line, no Claude attribution. `{{LUMO_DIR}}` and `{{PYTHON}}` are filled in at launch into `~/.claude-lumo/settings.json` |
| `LUMO.md` | Appended to the system prompt: Lumo costs and speed, routing, editing rules |
| `render.py` | Builds `~/.claude-lumo/settings.json` and `system-prompt.md` at each launch, merging your personal additions over ours |
| `statusline.py` | Prints the `[LUMO]`/`[CLAUDE]` status line; chains to `statusline-next.sh` from your personal additions if present |
| `proxy/proxy.mjs` | Lumo proxy: auth via browser cookie, retries, stream repair (splits multi-tool-call chunks that LiteLLM would merge), per-request stats in `proxy/proxy.log`, `/v1/debug/live` |
| `proxy/edit-hints.mjs` | Adds the closest real text to a failed Edit (and a reminder to Read before Edit or Write), so the retry copies it instead of guessing again. Claude Code checks edits before any hook runs, so this has to live in the proxy |
| `proxy/tool-guard.mjs` | Holds tool calls until the stream ends, then checks each one: repairs arguments leaked as `<arg_key>` tags, retries the request once when Lumo announced a call but sent none or a broken one, and otherwise ends the turn with a visible message (#11). Tests: `node --test proxy/tool-guard.test.mjs` |
| `proxy/creds.mjs` | Reads Lumo login from any browser's cookies (Firefox, Chrome, Edge, Brave, Vivaldi, Opera) |
| `proxy/litellm.yaml` | LiteLLM route table |
| `requirements.txt` | Just `litellm[proxy]` pinned |
| `hooks/edit-assist.py` | Tombstone (removed in v1.2.4): no-op so running sessions don't break |
| `scripts/check-hook-tombstones.py` | Release check: every hook ever referenced on this branch exists under `hooks/` |
| `scripts/pre-push` | Opt-in git pre-push hook running the tombstone check |

Runtime state (not in the repo): `~/.claude-lumo/` holds the LiteLLM venv, a random
local key, logs, and the settings and system prompt rendered at each launch.

## Personal additions

Keep your own changes out of this repo, in `~/.claude/lumo-local/` (or the folder named by
`CLAUDE_LUMO_LOCAL`). Never edit the files in `~/.claude/lumo` itself: they belong to this repo,
and an update would conflict with or overwrite them (the launcher warns when that clone has local
edits). claude-lumo only reads the personal folder; installing, updating and uninstalling never
write to it or delete it. Everything is optional, and your files win over ours:

| File | Effect |
|---|---|
| `settings.json` | Merged over `settings-lumo.json` at each launch: objects merge key by key, lists are appended (your hooks and permissions add to ours), anything else is replaced. Invalid JSON is skipped with a warning |
| `LUMO.md` | Appended to the system prompt after this repo's `LUMO.md` (your agent names, project rules) |
| `statusline-next.sh` | Run with the same status-line JSON after `[LUMO]` is printed, for tools that read the status line (for example Orca); its output is ignored |

Keep that folder under your own version control (a private repo) if you want it backed up.
Instructions for every session, Claude or Lumo, belong in `~/.claude/CLAUDE.md` as usual.
