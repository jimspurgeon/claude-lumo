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
irm https://raw.githubusercontent.com/jimspurgeon/claude-lumo/main/install.ps1 | iex
```

On **macOS / Linux** (any terminal):

```bash
curl -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/main/install.sh | bash
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
curl -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/main/uninstall.sh | bash
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/jimspurgeon/claude-lumo/main/uninstall.ps1 | iex
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
| "Slow thinking" or stuck at 5 min | Normal on lumo-max (~16 tok/s, minutes-long reasoning). Not a hang. |
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

## Files

| File | What it does |
|---|---|
| `install.sh` | End-to-end setup incl. prerequisite auto-install; safe to re-run |
| `install.ps1` | Windows one-command entry-point: installs Git Bash (with prompt), then runs install.sh |
| `uninstall.sh` | Removes everything claude-lumo added; plain `claude` untouched |
| `uninstall.ps1` | Windows entry-point for the uninstaller |
| `claude-lumo.sh` | Launcher: starts proxy and LiteLLM if down, then runs Claude Code |
| `settings-lumo.json` | Claude Code settings for Lumo sessions: model mapping, status line, no Claude attribution |
| `LUMO.md` | Appended to the system prompt: brief routing notes for Lumo models |
| `statusline.py` | Prints the `[LUMO]`/`[CLAUDE]` status line |
| `proxy/proxy.mjs` | Lumo proxy: auth via browser cookie, retries, stream repair |
| `proxy/creds.mjs` | Reads Lumo login from any browser's cookies (Firefox, Chrome, Edge, Brave, Vivaldi, Opera) |
| `proxy/litellm.yaml` | LiteLLM route table |
| `requirements.txt` | Just `litellm[proxy]` pinned |

Runtime state (not in the repo): `~/.claude-lumo/` holds the LiteLLM venv, a random
local key, and logs.
