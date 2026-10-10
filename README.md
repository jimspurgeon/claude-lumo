# claude-lumo

Run [Claude Code](https://claude.com/code) on **Proton Lumo** with one command.

`claude-lumo` looks and behaves exactly like `claude` — same prompts, same skills, same
config in `~/.claude` — but the models behind it are Proton Lumo's `lumo-max` (GLM 5.3)
and `lumo-lite` (Qwen 3.5) instead of Claude's, reached through a small proxy that runs
on your machine. Plain `claude` is untouched and keeps using Claude's API.

The status line always shows which one you're on: `[CLAUDE] Opus 5.5` or `[LUMO] lumo-max`.

## What you need first (Windows, Git Bash)

1. **Git for Windows** (gives you Git Bash and `curl`)
2. **Node.js 22.13 or newer** — check with `node --version`
3. **Python 3.12 or newer** installed as `python` — check with `python --version`
4. **Claude Code** — `npm i -g @anthropic-ai/claude-code`, then run `claude` once and log in
5. **Firefox**, logged in at [lumo.proton.me](https://lumo.proton.me) (a free Lumo account works)

## Install

Download the ZIP from this repo's **Releases** page (right side of this page), unzip it
into `~/.claude/lumo`, open **Git Bash** there, and run:

```bash
bash install.sh
```

(If you know Git, you can instead clone the repo straight into `~/.claude/lumo`.)

The installer builds the LiteLLM venv and puts the `claude-lumo` command on your PATH.
If install succeeds it prints `Installed.` and tells you how to test.

## Try it

```bash
claude-lumo -p "reply with pong"
```

A `pong` reply means everything works. Then just use `claude-lumo` wherever you'd use
`claude`. In a Lumo session `/model lumo-lite` switches to the cheaper, faster tier.

## How it works (no need to memorize)

```
claude-lumo
  └─ Claude Code → LiteLLM (localhost:4000) → Lumo proxy (localhost:8790) → lumo.proton.me
```

The proxy signs every request with your Lumo login, which it reads straight from your
Firefox cookies — so there are no passwords or API keys to copy anywhere. It also
reconnects when your Lumo session expires and retries on hiccups.

Two local ports are used: 4000 and 8790. Nothing leaves your machine except traffic to
lumo.proton.me.

No credentials are stored in any file, and re-running the installer is always safe
(it skips what already exists).

## Uninstall

```bash
rm -rf ~/.claude-lumo ~/.claude/lumo
rm -f "$APPDATA/npm/claude-lumo"*
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `claude-lumo` not found after install | Close and reopen your terminal (PATH refresh), or start a new Git Bash window. |
| `ECONNREFUSED`, connection lost mid-response | The proxy or LiteLLM stopped. Run any `claude-lumo` command again — it restarts both. |
| 401 from Lumo | Log in again at lumo.proton.me in Firefox; the proxy picks up the fresh cookie by itself. |
| Slow or "stuck thinking" | Normal on lumo-max (about 16 tok/s, minutes-long reasoning). Not a hang. Check `curl 127.0.0.1:8790/v1/debug/live`. |
| `python` not found | Install Python 3.12+ and make sure it's called `python` on PATH (not only `py`). |
| Firefox cookie not found / `creds:false` | Firefox must be installed and logged in at lumo.proton.me. LibreWolf also works. |

## Files

| File | What it does |
|---|---|
| `install.sh` | One-shot setup; safe to re-run |
| `claude-lumo.sh` | Launcher: starts the two services if down, then runs Claude Code |
| `settings-lumo.json` | Claude Code settings for Lumo sessions: model mapping, 100k compact window, status line |
| `LUMO.md` | Appended to the system prompt: model routing rules for Lumo models |
| `statusline.py` | Prints the `[LUMO]`/`[CLAUDE]` status line |
| `proxy/proxy.mjs` | Lumo proxy: auth via Firefox cookie, retries, stream repair |
| `proxy/creds.mjs` | Reads the Lumo login from Firefox cookies (copies the DB, never touches Firefox's lock) |
| `proxy/litellm.yaml` | LiteLLM route table |
| `requirements.txt` | Just `litellm[proxy]` pinned |

Runtime state (not in the repo): `~/.claude-lumo/` holds the LiteLLM venv, a random
local key, and logs.
