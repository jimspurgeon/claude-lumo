# claude-lumo

Run [Claude Code](https://claude.com/code) on **Proton Lumo** with one command.

`claude-lumo` looks and behaves exactly like `claude` — same prompts, same skills, same
config in `~/.claude` — but the models behind it are Proton Lumo's `lumo-max` (GLM 5.3)
and `lumo-lite` (Qwen 3.5) instead of Claude's, reached through a small proxy that runs
on your machine. Plain `claude` is untouched and keeps using Claude's API.

The status line always shows which one you're on: `[CLAUDE] Opus 5.5` or `[LUMO] lumo-max`.

## What you need first

| Component | Check | Install if missing |
|-----------|-------|-------------------|
| Git | `git --version` | [Windows Git](https://git-scm.com), `brew install git` (macOS), `sudo apt install git` (Linux) |
| Node.js 22.13+ | `node --version` | [Node.js](https://nodejs.org), `brew install node` (macOS), `sudo apt install nodejs npm` (Linux) |
| Python 3.12+ (`python` or `python3`) | `python --version` or `python3 --version` | [Python](https://python.org), `brew install python` (macOS), `sudo apt install python3` (Linux) |
| Firefox | Open Firefox and visit lumo.proton.me | [Firefox](https://mozilla.org/firefox) |
| Claude Code | `claude --version` | `npm i -g @anthropic-ai/claude-code`, then `claude` once and log in |
| Lumo account | Visit lumo.proton.me | Free account at [Proton Lumo](https://proton.me/lumo) |

Then log in to lumo.proton.me **in Firefox** before running `claude-lumo`.

## Installation

### Quick install (recommended)

From a terminal, run:

```bash
curl -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/main/install.sh | bash
```

This downloads the files into `~/.claude/lumo`, builds the LiteLLM virtual environment,
and puts `claude-lumo` on your PATH.

If you prefer manual installation:

1. Download the ZIP from this repo's **Releases** page and unzip it somewhere.
2. Open a terminal in that folder.
3. Run `bash install.sh`.
4. Move the downloaded folder to `~/.claude/lumo` if you want.

## Test it

After install, run:

```bash
claude-lumo -p "reply with pong"
```

A `pong` reply means everything works. Then use `claude-lumo` anywhere you'd normally
use `claude`.

## Uninstall

```bash
rm -rf ~/.claude-lumo ~/.claude/lumo
rm -f "$HOME/.local/bin/claude-lumo" "$HOME/.npm-global/bin/claude-lumo" 2>/dev/null
# Windows: also remove %APPDATA%/npm/claude-lumo*
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `claude-lumo: command not found` after install | Close and reopen your terminal (PATH refresh), or add `$HOME/.local/bin` / `$APPDATA/npm` to PATH manually. |
| `ECONNREFUSED` mid-response | Proxy or LiteLLM crashed. Run any `claude-lumo` command again — it restarts both automatically. |
| 401 from Lumo | Log in again at lumo.proton.me in Firefox; the proxy picks up the fresh cookie by itself. |
| "Slow thinking" or stuck at 5 min | Normal on lumo-max (~16 tok/s, minutes-long reasoning). Not a hang. |
| `python` / `python3` not found | Install Python 3.12+. On Windows, make sure it's added to PATH during installation. |
| "Firefox cookie not found" | Firefox must be installed and logged in at lumo.proton.me. The proxy reads from Firefox's profile only. |
| `NODE_ENV` warning | Ignore harmless warnings about NODE_ENV from LiteLLM. |

## How it works (no need to memorize)

```
claude-lumo
  └─ Claude Code → LiteLLM (localhost:4000) → Lumo proxy (localhost:8790) → lumo.proton.me
```

The proxy signs every request with your Lumo login, which it reads straight from your
Firefox cookies — so there are no passwords or API keys to copy anywhere. It also
transparently reconnects when your Lumo session expires and retries on hiccups.

Two local ports are used: 4000 and 8790. Nothing leaves your machine except traffic to
lumo.proton.me.

Nothing is stored in the repo or sent anywhere else: no credentials in any file, and
re-running the installer is always safe (it skips what already exists).

## Files

| File | What it does |
|---|---|
| `install.sh` | One-shot setup; safe to re-run |
| `claude-lumo.sh` | Launcher: starts proxy and LiteLLM if down, then runs Claude Code |
| `settings-lumo.json` | Claude Code settings for Lumo sessions: model mapping, status line |
| `LUMO.md` | Appended to the system prompt: brief routing notes for Lumo models |
| `statusline.py` | Prints the `[LUMO]`/`[CLAUDE]` status line |
| `proxy/proxy.mjs` | Lumo proxy: auth via Firefox cookie, retries, stream repair |
| `proxy/creds.mjs` | Reads Lumo login from Firefox cookies (copies the DB, never touches Firefox's lock) |
| `proxy/litellm.yaml` | LiteLLM route table |
| `requirements.txt` | Just `litellm[proxy]` pinned |

Runtime state (not in the repo): `~/.claude-lumo/` holds the LiteLLM venv, a random
local key, and logs.
