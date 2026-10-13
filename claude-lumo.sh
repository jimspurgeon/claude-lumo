#!/bin/bash
# claude-lumo: Claude Code running on Proton Lumo. Same ~/.claude config, skills and agents as
# `claude`; only the model backend changes. Chain: Claude Code -> LiteLLM :4000 (Anthropic
# format) -> lumo proxy :8790 -> Lumo. Plain `claude` is untouched.
# Works on Windows (Git Bash), macOS and Linux.
D="$HOME/.claude/lumo"; PROXY="$HOME/.claude/lumo/proxy"; S="$HOME/.claude-lumo"; VENV="$S/litellm-venv"
# Optional personal additions, kept outside this repo (see README, "Personal additions").
LOCAL="${CLAUDE_LUMO_LOCAL:-$HOME/.claude/lumo-local}"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) LITELLM="$VENV/Scripts/litellm.exe"; PY=python; native() { cygpath -m "$1"; };;
  *) LITELLM="$VENV/bin/litellm"; PY=$(command -v python3 >/dev/null && echo python3 || echo python); native() { echo "$1"; };;
esac

# `claude-lumo update`: bring this install to the latest release (v1.3.0+).
[ "$1" = "update" ] && exec bash "$D/update-command.sh"

# `claude-lumo revert <version>|last`: roll back to a previous release (see revert-command.sh).
[ "$1" = "revert" ] && exec bash "$D/revert-command.sh" "${@:2}"

# Offline subcommands: data hygiene, no proxy or Lumo login needed.
case "$1" in
  wipe|privacy-audit)
    SCRIPT="$D/${1//-/_}.py"
    [ -f "$SCRIPT" ] || { echo "claude-lumo: $SCRIPT not found" >&2; exit 1; }
    exec "$PY" "$(native "$SCRIPT")" "${@:2}"
    ;;
esac
up() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null | grep -q 200; }
if ! up http://127.0.0.1:8790/v1/health; then
  (cd "$PROXY" && nohup node --no-warnings proxy.mjs >/dev/null 2>&1 &)
fi
KEY=$(cat "$S/master.key")
if ! up http://127.0.0.1:4000/health/liveliness; then
  (LITELLM_MASTER_KEY="$KEY" PYTHONIOENCODING=utf-8 nohup "$LITELLM" --config "$PROXY/litellm.yaml" \
    --host 127.0.0.1 --port 4000 > "$S/litellm.log" 2>&1 &)
  for i in $(seq 1 45); do up http://127.0.0.1:4000/health/liveliness && break; sleep 1; done
  up http://127.0.0.1:4000/health/liveliness || { echo "claude-lumo: LiteLLM did not start, see $S/litellm.log" >&2; exit 1; }
fi

# Render this machine's settings and system prompt into ~/.claude-lumo, merging the optional
# personal additions in $LOCAL over ours (see render.py). Tracked files here are only read, so a
# clone stays clean and updates (re-running install.sh) keep working.
"$PY" "$(native "$D")/render.py" "$(native "$D")" "$(native "$S")" "$(native "$LOCAL")" "$PY" \
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

# Separate data (optional): when enabled, build link layout and export CLAUDE_CONFIG_DIR so
# transcripts, history, and per-session state go into ~/.claude-lumo/data, not the shared store.
# Enable via env CLAUDE_LUMO_SEPARATE_DATA=1 or marker ~/.claude-lumo/separate-data.on.
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

export ANTHROPIC_AUTH_TOKEN="$KEY" CLAUDE_LUMO=1 CLAUDE_LUMO_LOCAL="$LOCAL"
exec claude --settings "$S/settings.json" --append-system-prompt-file "$S/system-prompt.md" "${AGENTS[@]}" "$@"
