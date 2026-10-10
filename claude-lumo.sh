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
# clone stays clean and `git pull` keeps working.
"$PY" "$(native "$D")/render.py" "$(native "$D")" "$(native "$S")" "$(native "$LOCAL")" "$PY" \
  || { echo "claude-lumo: could not build settings (render.py failed)" >&2; exit 1; }

# Personal changes belong in $LOCAL, not here: a pull would conflict with or overwrite them.
if [ -d "$D/.git" ] && [ -n "$(git -C "$D" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "claude-lumo: note: $D has local edits; updates may conflict. Put personal changes in $LOCAL instead." >&2
fi

export ANTHROPIC_AUTH_TOKEN="$KEY" CLAUDE_LUMO=1 CLAUDE_LUMO_LOCAL="$LOCAL"
exec claude --settings "$S/settings.json" --append-system-prompt-file "$S/system-prompt.md" "$@"
