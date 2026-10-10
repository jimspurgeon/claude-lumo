#!/bin/bash
# claude-lumo: Claude Code running on Proton Lumo. Same ~/.claude config, skills and agents as
# `claude`; only the model backend changes. Chain: Claude Code -> LiteLLM :4000 (Anthropic
# format) -> lumo proxy :8790 -> Lumo. Plain `claude` is untouched.
D="$HOME/.claude/lumo"; PROXY="$HOME/.claude/lumo/proxy"; VENV="$HOME/.claude-lumo/litellm-venv"
up() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null | grep -q 200; }
if ! up http://127.0.0.1:8790/v1/health; then
  (cd "$PROXY" && nohup node --no-warnings proxy.mjs >/dev/null 2>&1 &)
fi
KEY=$(cat "$HOME/.claude-lumo/master.key")
if ! up http://127.0.0.1:4000/health/liveliness; then
  (LITELLM_MASTER_KEY="$KEY" PYTHONIOENCODING=utf-8 nohup "$VENV/Scripts/litellm.exe" --config "$PROXY/litellm.yaml" \
    --host 127.0.0.1 --port 4000 > "$HOME/.claude-lumo/litellm.log" 2>&1 &)
  for i in $(seq 1 45); do up http://127.0.0.1:4000/health/liveliness && break; sleep 1; done
  up http://127.0.0.1:4000/health/liveliness || { echo "claude-lumo: LiteLLM did not start, see ~/.claude-lumo/litellm.log" >&2; exit 1; }
fi
export ANTHROPIC_AUTH_TOKEN="$KEY" CLAUDE_LUMO=1
exec claude --settings "$D/settings-lumo.json" --append-system-prompt-file "$D/LUMO.md" "$@"
