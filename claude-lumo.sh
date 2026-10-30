#!/bin/bash
# claude-lumo: Claude Code running on Proton Lumo. Same ~/.claude config, skills and agents as
# `claude`; only the model backend changes. Chain: Claude Code -> LiteLLM :4000 (Anthropic
# format) -> lumo proxy :8790 -> Lumo. Plain `claude` is untouched.
# Works on Windows (Git Bash), macOS and Linux.
D="$HOME/.claude/lumo"; PROXY="$HOME/.claude/lumo/proxy"; S="${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}"; VENV="$S/litellm-venv"
# Optional personal additions, kept outside this repo (see README, "Personal additions").
LOCAL="${CLAUDE_LUMO_LOCAL:-$HOME/.claude/lumo-local}"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) LITELLM="$VENV/Scripts/litellm.exe"; PY=python; WIN=1; native() { cygpath -m "$1"; };;
  *) LITELLM="$VENV/bin/litellm"; PY=$(command -v python3 >/dev/null && echo python3 || echo python); native() { echo "$1"; };;
esac

# `claude-lumo update`: bring this install to the latest release (v1.3.0+).
[ "$1" = "update" ] && exec bash "$D/update-command.sh"

# `claude-lumo revert <version>|last`: roll back to a previous release (see revert-command.sh).
[ "$1" = "revert" ] && exec bash "$D/revert-command.sh" "${@:2}"

# `-h`/`--help`: print our subcommands, then `claude --help` for everything else. Fast path:
# no proxy, LiteLLM or render work runs for a help request.
case "$1" in
  -h|--help|help)
    cat <<'EOF'
claude-lumo: Claude Code running on Proton Lumo (https://github.com/jimspurgeon/claude-lumo)

Usage: claude-lumo [claude-arguments...]

Subcommands:
  update                   Bring this install to the latest release
  revert [<version>|last]  Roll back to a previous release
  wipe                     Delete finished projects' session data (offline)
  privacy-audit            Report on local data posture (offline, read-only)

All other arguments are passed to `claude`, including its own flags; see
`claude --help` or https://claude.com/docs for Claude Code usage.
EOF
    exit 0
    ;;
esac

# Offline subcommands: data hygiene, no proxy or Lumo login needed.
case "$1" in
  wipe|privacy-audit)
    SCRIPT="$D/${1//-/_}.py"
    [ -f "$SCRIPT" ] || { echo "claude-lumo: $SCRIPT not found" >&2; exit 1; }
    # Exit 10 means the user chose to go through the results with an agent (#99, #102): the script
    # left the opening prompt in the state dir, and this launch continues as a session that starts
    # with it. The script gets the same state dir, as a native path.
    CLAUDE_LUMO_LAUNCHER=1 CLAUDE_LUMO_STATE_DIR="$(native "$S")" \
      "$PY" "$(native "$SCRIPT")" "${@:2}"; rc=$?
    PROMPT_FILE="$S/$1-agent-prompt.txt"
    [ "$rc" -eq 10 ] || exit "$rc"
    [ -f "$PROMPT_FILE" ] || { echo "claude-lumo: the agent prompt was not written ($PROMPT_FILE); nothing was deleted." >&2; exit 1; }
    set -- "$(cat "$PROMPT_FILE")"; rm -f "$PROMPT_FILE"
    ;;
esac
up() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null | grep -q 200; }
start_proxy() { (cd "$PROXY" && nohup node --no-warnings proxy.mjs >/dev/null 2>&1 &); }
start_litellm() {
  (LITELLM_MASTER_KEY="$(cat "$S/master.key")" PYTHONIOENCODING=utf-8 nohup "$LITELLM" \
    --config "$PROXY/litellm.yaml" --host 127.0.0.1 --port 4000 > "$S/litellm.log" 2>&1 &)
}
# On Windows `nohup ... &` leaves the services attached to this terminal's console, so closing the
# terminal that happened to start them ends them for every session (#164). service_restart.py
# starts them detached instead, with the same command, folder, key and log.
if [ -n "$WIN" ] && [ -f "$D/service_restart.py" ]; then
  start_proxy() { CLAUDE_LUMO_STATE_DIR="$(native "$S")" "$PY" "$(native "$D")/service_restart.py" --install-dir "$(native "$D")" --start proxy; }
  start_litellm() { CLAUDE_LUMO_STATE_DIR="$(native "$S")" "$PY" "$(native "$D")/service_restart.py" --install-dir "$(native "$D")" --start litellm; }
fi
if ! up http://127.0.0.1:8790/v1/health; then
  start_proxy
fi
if ! up http://127.0.0.1:4000/health/liveliness; then
  start_litellm
  for i in $(seq 1 45); do up http://127.0.0.1:4000/health/liveliness && break; sleep 1; done
  up http://127.0.0.1:4000/health/liveliness || { echo "claude-lumo: LiteLLM did not start, see $S/litellm.log" >&2; exit 1; }
fi

# Separate data (optional): when enabled, build link layout and export CLAUDE_CONFIG_DIR so
# transcripts, history, and per-session state go into ~/.claude-lumo/data, not the shared store.
# Enable via env CLAUDE_LUMO_SEPARATE_DATA=1 or marker ~/.claude-lumo/separate-data.on.
# Runs before render.py so the settings can carry CLAUDE_CONFIG_DIR (see below).
if [ "${CLAUDE_LUMO_SEPARATE_DATA:-}" = "1" ] || [ -f "$S/separate-data.on" ]; then
  info_sep() { echo "info: $*" >&2; }
  if ! "$PY" "$(native "$D")/scripts/separate_data.py" setup \
       --config-dir "$S/data" --shared "$HOME/.claude"; then
    echo "claude-lumo: could not run separate_data.py setup; leaving CLAUDE_CONFIG_DIR unset" >&2
  else
    export CLAUDE_CONFIG_DIR="$S/data"
    info_sep "CLAUDE_CONFIG_DIR=$S/data (separate data enabled)"
  fi
fi

# Render this machine's settings and system prompt into ~/.claude-lumo, merging the optional
# personal additions in $LOCAL over ours (see render.py). Tracked files here are only read, so a
# clone stays clean and updates (re-running install.sh) keep working. CLAUDE_CONFIG_DIR (separate
# data, or the user's own) goes into the settings too, so a session Claude Code re-hosts without
# this launcher keeps writing to the same store (#77).
CFG=""
[ -n "${CLAUDE_CONFIG_DIR:-}" ] && CFG="$(native "$CLAUDE_CONFIG_DIR")"
"$PY" "$(native "$D")/render.py" "$(native "$D")" "$(native "$S")" "$(native "$LOCAL")" "$PY" "$CFG" \
  || { echo "claude-lumo: could not build settings (render.py failed)" >&2; exit 1; }

# Personal changes belong in $LOCAL, not here: an update would conflict with or overwrite them.
if [ -d "$D/.git" ] && [ -n "$(git -C "$D" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "claude-lumo: note: $D has local edits; updates may conflict. Put personal changes in $LOCAL instead." >&2
fi

# Personal agents (render.py turns $LOCAL/agents/*.md into agents.json). Passed inline because claude
# reads --agents from a file only with --print; render.py keeps it under the command-line limit.
AGENTS=()
[ -s "$S/agents.json" ] && AGENTS=(--agents "$(cat "$S/agents.json")")

# Update check (v1.3.0+): silent, in the background, at most once a day. It only asks the
# repo's own remote for release tags and never touches the running session. Disable with
# CLAUDE_LUMO_UPDATE_CHECK=0; see README.
if [ "${CLAUDE_LUMO_UPDATE_CHECK:-1}" != "0" ] && [ -f "$D/update-check.py" ]; then
  (nohup "$PY" "$(native "$D")/update-check.py" "$(native "$D")" >/dev/null 2>&1 &)
fi

# A session Claude Code re-hosts by itself (agents view) keeps --settings but none of these exports
# and none of the other flags (#77), so the settings carry the same values (env, apiKeyHelper), and
# the system prompt comes only from hooks/lumo-prompt.py (SessionStart on startup, clear and
# compact), never from a flag: that way a re-hosted session gets it exactly like a launched one,
# and it is never missing. The key comes only from apiKeyHelper: a token in the environment
# wins over it and makes Claude Code warn that both are set (#86), and an inherited one would send
# the wrong credential to LiteLLM, so both variables are cleared.
unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
# The release this session starts on: the status line asks for a restart once the install moves
# past it (#35). A re-hosted session loses it and simply shows no hint.
CLAUDE_LUMO_SESSION_VERSION=$(git -C "$D" describe --tags --exact-match HEAD 2>/dev/null || true)
export CLAUDE_LUMO_SESSION_VERSION CLAUDE_LUMO=1 CLAUDE_LUMO_LOCAL="$LOCAL" CLAUDE_CODE_AUTO_MODE_SERVER=0
exec claude --settings "$S/settings.json" "${AGENTS[@]}" "$@"
