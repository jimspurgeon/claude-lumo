# claude-lumo

Run [Claude Code](https://claude.com/product/claude-code) on [Proton Lumo](https://proton.me/lumo) with one command.

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

By default the installer checks out the newest `v*` release tag (so you never get
unreleased changes). To follow the `master` branch instead — for development — set
`CLAUDE_LUMO_CHANNEL=master` before re-running it. If the clone has local changes,
or the checkout fails (e.g. offline), it is left as is with a note.

### Updating (v1.3.0+)

Instead of re-running the installer, run:

```bash
claude-lumo update
```

It shows what changed since the installed version, checks out the newest `v*`
release tag (never `master`), and re-runs the installer so new files and settings
take effect. It refuses when `~/.claude/lumo` has local changes (put personal
changes in `~/.claude/lumo-local`, see "Personal additions") or when the clone is
not at a release tag, and the only network traffic is a `git fetch` to this
repo's own remote. Open sessions keep the old files until restarted; the command
then offers to restart them one at a time (wait until idle, interrupt now, or
skip, default skip) when Orca or tmux can drive their terminals, and prints the
exact `claude-lumo --resume` command otherwise. It never restarts anything
without your confirmation.

Each launch also runs a silent background check for a newer release (at most
once a day, 5 second timeout, nothing leaves your machine except release-tag
requests to this repo's remote). When a newer release is out, the status line
shows a one-line notice, once per version, until you update or 7 days pass.
Disable it with `CLAUDE_LUMO_UPDATE_CHECK=0`; set the interval with
`CLAUDE_LUMO_UPDATE_CHECK_INTERVAL` (hours, default 24).

Idle detection for the restart offer: every session writes a busy flag to
`~/.claude-lumo/sessions/<session id>.json` (set when a prompt is submitted,
cleared when the turn ends), which the update command reads to tell busy from
idle sessions.

### Reverting to a previous release

To roll the installation back to a previous released version (a code rollback
only — your data in `~/.claude` and `~/.claude-lumo` is never touched):

```bash
claude-lumo revert v1.3.0   # or: claude-lumo revert 1.3.0
claude-lumo revert last     # the release immediately before the installed one
```

It checks out the given release tag, re-runs the installer so any changed
wiring takes effect, and offers to restart open sessions exactly like
`claude-lumo update` does. The same guards apply (refuses when `~/.claude/lumo`
has local changes, when it is not a clone or not at a release tag, and when
the remote cannot be reached), plus: reverting to the currently installed
version is a friendly no-op, and reverting to a version that does not exist is
refused with the list command to run. After a revert, the status line's update
notice and version cache are cleared, and `claude-lumo update` brings you back
to the latest release at any time.

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

## Resume shim (optional)

Some tools resume sessions with a plain `claude --resume <id>` (Orca does). A resumed
Lumo session would then come back on regular Claude, which does not know the lumo
models. The resume shim fixes that: when the resumed session's transcript used a
`lumo-*` model, it re-launches through `claude-lumo` with the same arguments. Every
other `claude` call (new sessions, non-Lumo resumes, `--version`, scripts) reaches the
real `claude` unchanged: same arguments, same exit code, no output added.

Opt in at install time (ships in v1.3.0):

```bash
bash install.sh --resume-shim        # macOS / Linux / Windows (Git Bash)
```

```powershell
# Windows (PowerShell)
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -ResumeShim
```

This adds one clearly commented line to your shell profiles (`~/.bashrc`, `~/.zshrc`,
and on Windows the PowerShell profiles), sourcing the shim from `~/.claude/lumo/scripts/`
so updates flow to it. It is idempotent: re-running keeps exactly one line. Transcripts
are looked up in `$CLAUDE_CONFIG_DIR` if set, then `~/.claude`, then
`~/.claude-lumo/data` (the separated Lumo data store). Uninstalling claude-lumo
removes the lines too.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `claude-lumo: command not found` after install | Close and reopen your terminal (PATH refresh), or add `$HOME/.local/bin` / `$APPDATA/npm` to PATH manually. |
| `claude-lumo update` refuses: "local changes" | Expected: your changes in `~/.claude/lumo` would be overwritten. Move them to `~/.claude/lumo-local` (see "Personal additions") or commit them, then retry. |
| `claude-lumo update` refuses: "not at a release tag" | Expected: the clone tracks `master` or an untagged commit (development install). Re-run the installer to reset to a release. |
| Update notice stuck on the status line | It clears on the next `claude-lumo update`; it also expires after 7 days. `CLAUDE_LUMO_UPDATE_CHECK=0` disables the check entirely. |
| `ECONNREFUSED` mid-response | Proxy or LiteLLM crashed. Run any `claude-lumo` command again — it restarts both automatically. |
| 401 from Lumo | Log in again at lumo.proton.me in your browser; the proxy picks up the fresh cookie by itself. |
| "Slow thinking" or stuck at 5 min | Normal on lumo-max (~16 tok/s, minutes-long reasoning). Not a hang if requests keep finishing: `proxy/proxy.log` logs each one, and `curl 127.0.0.1:8790/v1/debug/live` shows what is in flight. `LUMO_CAPTURE=1` on the proxy saves raw requests and streams to `proxy/debug/`. |
| "Invalid tool parameters" on parallel tool calls | Fixed in the proxy (it splits tool calls LiteLLM would merge). If it returns after a LiteLLM upgrade, check `splitToolCallChunks` in `proxy/proxy.mjs`. |
| Agent stops mid-task right after saying what it will do next | Most often the reply mentioned `[DONE]` (an SSE fixture, say): LiteLLM ends the stream on any line containing it, so the proxy escapes it inside JSON (#13). Otherwise Lumo announced a tool call that never arrived intact. The proxy now repairs it or retries once (`dropped_tool`, `repaired_tool` and `tool_retry` in `proxy/proxy.log`); if both fail, the turn ends with a `[claude-lumo] Your tool call was lost in transit` message instead of going silent. See `proxy/tool-guard.mjs`. |
| **Pressing LEFT ARROW in an agent view then re-entering that same agent causes permanent `no_db_connection` failures** (issue #60) | One cause is fixed in #77: Claude Code re-hosts a session that leaves the agents view without the launcher's environment, so its requests reached LiteLLM without the local key (LiteLLM answers `No connected db`); the settings now supply the key through `apiKeyHelper`, plus the rest of the launcher's environment and, through `hooks/lumo-prompt.py`, the system prompt. The proxy now recovers by retrying with `prompt_cache_key` stripped after two consecutive `no_db_connection` errors. If you still see persistent failures in a session after leaving the agent list, start a fresh `claude-lumo` session (or use `claude-lumo --resume <session_id>` for the session-resume hook); fresh sessions work because they get a new cache key. Never press into the agents view in other sessions. |
| "response exceeded the 32000 output token maximum" | The model spent its whole budget thinking (often drafting a big batch in its head) before it answered; `proxy/proxy.log` shows `finish=length content=0c tools=0`. Split the work into smaller steps, for example one batch per tool call. The cap is `CLAUDE_CODE_MAX_OUTPUT_TOKENS` in `settings-lumo.json` (32000, #19); you can raise it in your own `settings.json`, but each stuck turn then runs longer, at about 1.2K tokens a minute on lumo-max. |
| Failed Edit with a "[claude-lumo]" note | The proxy added the closest real text to an Edit whose old text was not in the file; the model should retry with the text between the markers. |
| WebSearch says it did not return a usable result | Since #73 WebSearch runs on Lumo's own built-in web search (the web UI's "Web search" toggle, no API key needed). The proxy passes the answer on only when the usage shows Lumo actually searched (the search results add thousands of prompt tokens) and the answer cites at least one source URL; otherwise the model gets a `[claude-lumo] WebSearch did not return a usable result` message instead of training-data guesses (#59). `proxy/proxy.log` shows the outcome: `websearch=ok`, `no_search` (Lumo answered from memory), `no_sources`, `timeout` (no answer within 120 s), `err_<type>` or `http_<status>` (upstream error) or `error_<name>` (proxy-side failure). Retry, or use WebFetch for specific URLs. |
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

## Testing safety (ports and process cleanup)

**Live test servers must only bind to spare ports**. Never use port 8790 (Lumo proxy), 4000 (LiteLLM), or any port an existing listener owns. Pick a random port in the range 18000-18999, then verify it's free with `netstat -ano` before binding.

**Kill only PIDs you started**, and kill them by PID, not by image name. Before killing, verify the command line with `powershell Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" | select ProcessId,CommandLine` or `tasklist /FI "PID eq <PID>" /V` points at your test process (Windows 11 has no `wmic` by default). Never use `taskkill /IM node.exe` or kill every node.exe process of a binary: on this machine, node.exe includes the production Lumo proxy (issue #71) and LiteLLM.

**After cleanup**, verify both that your ports are free and that production services are still up:
```bash
curl -s --max-time 5 http://127.0.0.1:8790/v1/health
```
If a test stopped the proxy, restart it (`cd ~/.claude/lumo/proxy && nohup node --no-warnings proxy.mjs`) and report it in your verdict. Hiding it is a finding.

On 2026-10-13, a QA run killed all node.exe PIDs from a `tasklist | grep` result; PID 29388 turned out to be the production proxy. The agent process/port-safety rules (qa.md, worker.md, light-worker.md) now prohibit this pattern.

## Files

| File | What it does |
|---|---|
| `install.sh` | End-to-end setup incl. prerequisite auto-install; safe to re-run |
| `install.ps1` | Windows one-command entry-point: installs Git Bash (with prompt), then runs install.sh |
| `uninstall.sh` | Removes everything claude-lumo added; plain `claude` untouched |
| `uninstall.ps1` | Windows entry-point for the uninstaller |
| `claude-lumo.sh` | Launcher: starts proxy and LiteLLM if down, then runs Claude Code; `claude-lumo update` runs `update-command.sh` |
| `update-command.sh` | `claude-lumo update`: checks out the newest `v*` release tag, re-runs the installer, offers session restarts |
| `revert-command.sh` | `claude-lumo revert <version>\|last`: checks out a previous `v*` release tag, re-runs the installer, offers session restarts |
| `update-check.py` | Silent background check for a newer release at each launch; writes the one-line status-line notice |
| `session-restart.py` | After an update: finds running sessions, offers restarts (wait until idle / interrupt now / skip) via Orca or tmux |
| `migrate-to-lumo.sh` | Moves Lumo sessions from `~/.claude` into the separate store (see "Separate data") |
| `migrate-from-lumo.sh` | Moves them back; undoes `migrate-to-lumo.sh` |
| `scripts/resume-shim.ps1`, `scripts/resume-shim.sh` | The optional resume shim (PowerShell / bash+zsh), sourced from shell profiles. Tests: `bash test-resume-shim.sh` |
| `scripts/resume-shim-setup.sh` | Adds/removes the resume shim profile lines (used by `install.sh --resume-shim` and the uninstaller) |
| `test-resume-shim.sh` | Resume shim tests: fake transcripts and stub binaries, no real sessions |
| `settings-lumo.json` | Claude Code settings for Lumo sessions: model mapping, status line, no Claude attribution. `{{LUMO_DIR}}` and `{{PYTHON}}` are filled in at launch into `~/.claude-lumo/settings.json` |
| `LUMO.md` | The system prompt, delivered by `hooks/lumo-prompt.py` at startup and after /clear and /compact: Lumo costs and speed, routing, editing rules |
| `render.py` | Builds `~/.claude-lumo/settings.json`, `system-prompt.md` and its hook parts (`prompt-parts/`) at each launch, merging your personal additions over ours, and `agents.json` from your personal agents |
| `statusline.py` | Prints the `[LUMO]`/`[CLAUDE]` status line (`--lumo` forces `[LUMO]`; keep that flag if your personal `settings.json` overrides `statusLine`); chains to `statusline-next.sh` from your personal additions if present |
| `wipe.py` | Finds and deletes finished conversation data (inactive 30+ days, branch merged/removed, project folder gone): run as `claude-lumo wipe` |
| `privacy_audit.py` | Reads data safety posture (encryption, cloud sync, stale data volume): run as `claude-lumo privacy-audit` |
| `proxy/proxy.mjs` | Lumo proxy: auth via browser cookie, retries, stream repair (splits multi-tool-call chunks that LiteLLM would merge), per-request stats in `proxy/proxy.log`, `/v1/debug/live` |
| `proxy/sniffer.mjs` | Buffer non-SSE chat responses; extract error type/code; retry no_db_connection once |
| `proxy/sniffer.test.mjs` | Sniffer tests: `node --test proxy/sniffer.test.mjs` |
| `proxy/context-fit.mjs` | Keeps prompt + reply inside Lumo's 131072-token window: learns each session's bytes-per-token ratio from Lumo's usage reports and cuts `max_tokens` before a long prompt would be rejected, with one retry if Lumo still says the request is too long (#65). Tests: `node --test proxy/context-fit.test.mjs` |
| `proxy/edit-hints.mjs` | Adds the closest real text to a failed Edit (and a reminder to Read before Edit or Write), so the retry copies it instead of guessing again. Claude Code checks edits before any hook runs, so this has to live in the proxy |
| `proxy/tool-guard.mjs` | Holds tool calls until the stream ends, then checks each one: repairs arguments leaked as `<arg_key>` tags, retries the request once when Lumo announced a call but sent none or a broken one, and otherwise ends the turn with a visible message (#11). Tests: `node --test proxy/tool-guard.test.mjs` |
| `proxy/websearch-guard.mjs` | Runs Claude Code's internal WebSearch delegation (`web_search_options: {}`) on Lumo's built-in web search (`tools: [{name: "web_search"}]`) and passes the answer on only when the usage shows a search ran and the answer cites sources; otherwise answers with a clear message instead of letting Lumo hallucinate from training data (#59, #73). Tests: `node --test proxy/websearch-guard.test.mjs` |
| `proxy/creds.mjs` | Reads Lumo login from any browser's cookies (Firefox, Chrome, Edge, Brave, Vivaldi, Opera) |
| `proxy/litellm.yaml` | LiteLLM route table |
| `test_render.py` | Tests for the personal agents in `render.py`: `python test_render.py` |
| `test_update_check.py` | Tests for the update check, `claude-lumo update`, the busy-flag registry and the restart offer: `python test_update_check.py` |
| `tests/test_revert.py` | Tests for `claude-lumo revert` version parsing and guard behavior: `python -m pytest tests/test_revert.py` |
| `requirements.txt` | Just `litellm[proxy]` pinned |
| `hooks/edit-assist.py` | Tombstone (removed in v1.2.4): no-op so running sessions don't break |
| `hooks/busy-mark.py` | Session registry for the restart offer: writes a per-session busy flag (set on prompt submit, cleared when the turn ends) |
| `hooks/lumo-prompt.py` | SessionStart hook (startup, clear, compact; on resume and fork only when the transcript lacks the prompt): the only way the system prompt (LUMO.md plus the personal one) reaches a session, printed in parts under the hook output cap, so a session Claude Code re-hosts after the agents view gets it exactly like a launched one (#77) |
| `scripts/api-key.py` | `apiKeyHelper`: prints the local LiteLLM key, so a re-hosted session still authenticates (#77) |
| `hooks/session-start-offer.py` | SessionStart hook: offers to wipe old conversations, at most once per day, silent when nothing qualifies |
| `scripts/check-hook-tombstones.py` | Release check: every hook ever referenced on this branch exists under `hooks/` |
| `scripts/separate_data.py` | Optional data separation: build/repair the link layout, migrate sessions between stores, status. Tests: `python scripts/test_separate_data.py` |
| `scripts/pre-push` | Opt-in git pre-push hook running the tombstone check |

Runtime state (not in the repo): `~/.claude-lumo/` holds the LiteLLM venv, a random
local key, logs, and the settings and system prompt rendered at each launch.

## Separate data (optional)

By default claude-lumo shares everything with `claude`: one `~/.claude`, so one set of
conversations, prompt history, file snapshots and logins. Optional data separation gives
claude-lumo its own store for that data while rules stay shared:

| Kept separate in `~/.claude-lumo/data` | Shared with `claude` (linked) |
|---|---|
| `projects/` (conversations, auto memory), `history.jsonl`, `file-history/`, `session-env/`, `shell-snapshots/`, `todos/`, `plans/`, `paste-cache/`, `debug/`, `.credentials.json` | `CLAUDE.md`, `settings.json`, `keybindings.json`, `themes/`, `rules/`, `skills/`, `commands/`, `agents/`, `workflows/`, `output-styles/`, `plugins/`, `hooks/` |

The launcher exports `CLAUDE_CONFIG_DIR=~/.claude-lumo/data` for its own process only, so
plain `claude` is untouched. It also writes the value into `~/.claude-lumo/settings.json`
(read only by claude-lumo), so a session Claude Code re-hosts after the agents view keeps
using the separate store (#77). Directories are linked as junctions on Windows (no admin
rights needed) and symlinks elsewhere; files are hardlinks (or symlinks when the privilege
allows, with a refreshed copy as the last resort, warned about).

Turn it on (off by default, nothing changes for existing users):

```bash
# option 1: reinstall with the flag
bash ~/.claude/lumo/install.sh --separate-data   # or: powershell install.ps1 --separate-data

# option 2: just the marker, layout is built on next launch
touch ~/.claude-lumo/separate-data.on

# option 3: one session only
CLAUDE_LUMO_SEPARATE_DATA=1 claude-lumo
```

Check state any time with `python ~/.claude/lumo/scripts/separate_data.py status`.

Moving existing Lumo sessions into the store (they are the transcripts containing a
`lumo-` model; Claude sessions are never touched):

```bash
bash ~/.claude/lumo/migrate-to-lumo.sh --dry-run   # show the plan
bash ~/.claude/lumo/migrate-to-lumo.sh --yes       # move: transcripts, their per-session
                                                   # dirs, history lines; project memory/
                                                   # only when every session there is Lumo
bash ~/.claude/lumo/migrate-from-lumo.sh --yes     # undo: everything back to ~/.claude
```

Two limitations worth knowing. A project dir mixing Claude and Lumo sessions keeps its
`memory/` in the shared store (both kinds of session use it), and a plain
`claude --resume <id>` from a normal terminal cannot see Lumo-store sessions: resume them
via `claude-lumo --resume <id>` or from an environment that also sets `CLAUDE_CONFIG_DIR`.

Separation is new and will ship with v1.3.0 (not yet released). Uninstall removes
`~/.claude-lumo` including the separate store, but not what lives in `~/.claude` (run
migrate-from-lumo.sh first if you want those sessions kept).

## Personal additions

Keep your own changes out of this repo, in `~/.claude/lumo-local/` (or the folder named by
`CLAUDE_LUMO_LOCAL`). Never edit the files in `~/.claude/lumo` itself: they belong to this repo,
and an update would conflict with or overwrite them (the launcher warns when that clone has local
edits). claude-lumo only reads the personal folder; installing, updating and uninstalling never
write to it or delete it. Everything is optional, and your files win over ours:

| File | Effect |
|---|---|
| `settings.json` | Merged over `settings-lumo.json` at each launch: objects merge key by key, lists are appended (your hooks and permissions add to ours), anything else is replaced. Invalid JSON is skipped with a warning |
| `LUMO.md` | Added to the system prompt after this repo's `LUMO.md` (your agent names, project rules) |
| `agents/*.md` | Your own versions of agents, in the usual Claude Code agent format (frontmatter with `name`, `description`, `tools`, `model`, then the prompt). Passed to claude with `--agents`, so in claude-lumo they replace the agents of the same name in `~/.claude/agents`, which stay untouched for plain `claude`. See below |
| `statusline-next.sh` | Run with the same status-line JSON after `[LUMO]` is printed, for tools that read the status line (for example Orca); its output is ignored |

### Personal agents

Put a file such as `agents/gatekeeper.md` in the personal folder to give claude-lumo its own
gatekeeper, for example one with `model: lumo-max`. A `model:` line wins over
`CLAUDE_CODE_SUBAGENT_MODEL` (which sends other agents to `lumo-lite`); without one the agent runs on
`lumo-lite`. Only `name`, `description`, `tools`, `disallowedTools` and `model` are read; values sit
on one line, except lists (`a, b`, `[a, b]` or `- a` lines) and `|` or `>` blocks.

Limit: claude reads `--agents` from a file only with `--print`, so the agents travel inline on the
command line, and Windows caps a whole command line at 32,767 characters. The launcher keeps the
personal agents under 24,000 characters (100,000 elsewhere) and skips any that do not fit, with a
warning; the shared agent of that name is then used. Two agents of about 10,000 and 7,500
characters fit. Tests: `python test_render.py`.

Keep that folder under your own version control (a private repo) if you want it backed up.
Instructions for every session, Claude or Lumo, belong in `~/.claude/CLAUDE.md` as usual.

---

## Data hygiene commands

### `claude-lumo wipe`

Finds conversation data that is no longer needed and permanently deletes it.

A session/project is a candidate when signals say its project is finished:
- Inactivity (default 30+ days since last transcript write)
- The project's git branch is merged into master/main or removed
- The project working folder no longer exists
- A `wipe-done` marker file exists in the project folder
- (Allowlist mode) The project path is NOT in an allowlist file

**Usage:**
```bash
# Dry run: list what would be deleted
claude-lumo wipe --dry-run

# Review and confirm deletion
claude-lumo wipe

# Custom inactivity threshold
claude-lumo wipe --days 60

# Wipe one specific session by ID (searched in every store)
claude-lumo wipe --session <session-id>

# Wipe all sessions for a project (searched in every store)
claude-lumo wipe --project <project-name-or-path>

# Restrict the scan to one or more stores (repeat the flag)
claude-lumo wipe --store ~/.claude --store ~/.claude-lumo/data

# Skip confirmation (for scripting)
claude-lumo wipe --yes

# Allowlist mode: only wipe projects outside this file
claude-lumo wipe --allowlist ~/.claude/allowlist.txt
```

The default way to clean up is asking the assistant in a session: assessment in
conversation, per-project recommendations, then execution. The CLI modes are for
running the script directly, with `--mode manual` for a self-serve report.

# Three execution modes
```bash
claude-lumo wipe --mode intelligent   # classify, execute REMOVE, prompt ASK (default on TTY)
claude-lumo wipe --mode auto          # batch delete, --yes implied (default when piped)
claude-lumo wipe --mode manual        # list commands, delete nothing
```

**Modes:** `--mode` controls how candidates are handled.
- `intelligent` (default on an interactive TTY): each candidate is classified
  as REMOVE (clearly safe: marked done, explicitly selected, project folder
  gone/empty, or purely inactive), ASK (borderline: branch merged or removed,
  outside allowlist, or multiple mixed signals) or KEEP. REMOVE items are
  deleted without a global confirm, ASK items get a per-item `y/N` prompt,
  KEEP items are only reported.
- `auto` (default with `--yes` or when stdin is not a TTY): current
  behavior — one batch after a single confirmation. A non-interactive run
  without `--yes` prints guidance and deletes nothing (it never prompts or
  hangs).
- `manual`: plain-language report with the exact `claude-lumo wipe` command
  per finding; nothing is executed.

**Stores scanned:** auto mode discovers and scans every Claude data store on the
machine: `$CLAUDE_CONFIG_DIR` if set, `~/.claude`, `~/.claude-lumo/data`, the
legacy `~/.claude-lumo/config`, plus a bounded walk of your home directory
(common noise like `node_modules`, caches and cloud-sync folders is skipped).
`--store PATH` restricts the run to the given store(s); `--session` and
`--project` search every store (an id matching multiple stores is refused with
the list). The same rules apply in every store.

**Deleted:**
- `projects/<project>/<session>.jsonl` and session subfolders (subagents, tool-results)
- `session-env/<session>/`, `file-history/`, `tasks/` (entry-keyed folders)
- Matching lines from `history.jsonl`
- Stale temp scratch files (`claude-*` under `%TEMP%` or CLAUDE_CODE_TMPDIR)
- Stale Lumo logs (`~/.claude-lumo/*.log`)
- Stale entries (default 30+ days) in the age-keyed stores: `paste-cache/`,
  `image-cache/`, `shell-snapshots/`, `plans/`, `debug/`, `uploads/`

The age-keyed stores hold no session key, so entries are swept by age in
`wipe` auto mode only (not with `--session` or `--project`). The wipe never
deletes through a directory link (junction or symlink): such entries are
reported and left for you to remove manually, and nothing outside a store is
ever deleted.

**Protected:**
- Sessions with recent activity (< 6 hours) or active env lock
- Settings, memory, credentials, config files

**Important:** Deletion is logical, not forensic. SSD wear levelling, backups,
cloud sync and other tools' copies may still hold the data.

**Orca data:** Orca keeps its own session data in `%APPDATA%/orca`. The wipe command
lists these paths but does not delete them (V1 has no supported CLI for Orca data).
Manual cleanup: close unneeded Orca terminals, then delete their folders under
`terminal-history` yourself.

### `claude-lumo privacy-audit`

Read-only assessment of your data safety posture. Checks encryption, cloud sync,
cleanup settings, stale data volume and update status. Outputs findings with severity.
Runs occasionally (rate-limited), never reads or writes your conversation data
(the only files it writes are its own tiny rate-limit markers under `~/.claude-lumo`).

**Usage:**
```bash
# Full audit with all checks
claude-lumo privacy-audit

# Suggest mode: weekly reminder if stale data exists (quiet)
claude-lumo privacy-audit --suggest

# Custom staleness threshold
claude-lumo privacy-audit --days 60
```

# Three execution modes
```bash
claude-lumo privacy-audit --mode intelligent  # show next step for medium/high findings (default on TTY)
claude-lumo privacy-audit --mode auto         # current behavior, no new prompts (default when piped)
claude-lumo privacy-audit --mode manual       # list commands for every finding
```

**Checks performed:**
- Full-disk encryption (BitLocker on Windows, FileVault on macOS, LUKS on Linux)
- Cloud sync exposure (OneDrive, Dropbox, iCloud)
- Backup systems (Time Machine on macOS, File History on Windows)
- Cleanup settings (cleanupPeriodDays, CLAUDE_CODE_SKIP_PROMPT_HISTORY)
- Data separation (CLAUDE_CONFIG_DIR for lumo/plain split)
- Stale data volume estimation
- Update status (installed version vs latest release)
- Orca data presence (report-only with manual cleanup steps)

**Modes:** `--mode` controls how findings are presented.
- `intelligent` (default on an interactive TTY): show the recommended next
  step for medium/high severity findings.
- `auto` (default when stdin is not a TTY): current behavior — the optional
  interactive offer to help fix findings, or a summary if piped.
- `manual`: list the suggested action and command for every finding, execute
  nothing.

The full audit always runs when asked. Only `--suggest` is rate-limited: weekly, and
silent when no stale data exists.

---

## Plain Claude Code opt-in

The wipe and offer features work with plain Claude Code too, not just lumo.
To enable:

1. Create `~/.claude/allowlist.txt` (optional): one project path per line
2. Add this to your `~/.claude/settings.json`, replacing the python and script
   paths with your own absolute paths:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          { "type": "command", "command": "python \"C:/Users/YOU/.claude/lumo/hooks/session-start-offer.py\"", "timeout": 30 }
        ]
      }
    ]
  }
}
```
On macOS/Linux use `/home/YOU/.claude/lumo/hooks/session-start-offer.py`.
The `{{PYTHON}}` and `{{LUMO_DIR}}` placeholders used inside claude-lumo's own
settings are filled in by its launcher and do not work in plain settings.json.

To disable, set `CLAUDE_LUMO_WIPE_OFFER=0` in your environment (works everywhere,
including claude-lumo installs where the hook is part of the shipped settings), or
remove the SessionStart hook entry from your settings. The offer is rate-limited to
at most once per day and silent when nothing qualifies.
