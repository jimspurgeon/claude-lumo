#!/bin/bash
# Everything the setup UX prints lives here, so the dispatcher stays thin and the
# pure pieces (model parsing, sheet rendering) are unit-testable in isolation
# (tests/test_setup_facts.py).
#
# Globals consumed (with sane defaults so the file can also be sourced standalone
# by the tests): BASE_URL, KEY_PATH, and optionally FACTS_CONFIG (the litellm.yaml
# to report on; auto-detected when unset).
BASE_URL="${BASE_URL:-http://127.0.0.1:4000}"

# facts_key_path: resolved at CALL time (the env can change between sourcing
# and calling — the unit tests source once and vary the scratch HOME).
facts_key_path() {
  printf '%s\n' "${KEY_PATH:-${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}/master.key}"
}

# facts_config_default: the litellm.yaml `setup` should report on. Prefer the
# gateway-only checkout the install just wrote, then the plain install's, then
# this checkout (development / pre-install runs).
facts_config_default() {
  local d
  for d in "$HOME/.lumo-gateway" "$HOME/.claude/lumo" "${CLAUDE_LUMO_CHECKOUT:-}"; do
    [ -n "$d" ] && [ -f "$d/gateway/litellm.yaml" ] && { printf '%s\n' "$d/gateway/litellm.yaml"; return 0; }
  done
  printf '%s\n' "${BASH_SOURCE[0]%/*}/litellm.yaml"
}

# facts_parse_models YAML -> "name|max_in|max_out" lines for every concrete
# model entry (wildcard routes like "*" or "*haiku*" are routing, not models).
# Pure: reads only its argument, prints only the parsed lines.
facts_parse_models() {
  local cfg="$1" name="" min="" max="" line val
  [ -f "$cfg" ] || return 0
  _facts_emit() {
    [ -n "$name" ] || return 0
    case "$name" in *\**) return 0;; esac   # wildcard routes are not models
    printf '%s|%s|%s\n' "$name" "$min" "$max"
  }
  # Tolerate CRLF files (a yaml saved on Windows) as well as LF.
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in \#*) continue;; esac   # comments are not model entries
    case "$line" in
      *model_name:*)
        _facts_emit
        name="${line##*model_name:}"
        name="${name// /}"; name="${name//$'\t'/}"; name="${name//\"/}"
        min=""; max=""
        ;;
      *max_input_tokens:*)
        val="${line##*max_input_tokens:}"
        min="${val// /}"
        ;;
      *max_output_tokens:*)
        val="${line##*max_output_tokens:}"
        max="${val// /}"
        ;;
    esac
  done < "$cfg"
  _facts_emit
}

# facts_render_models YAML -> one human line per model with its token limits.
facts_render_models() {
  facts_parse_models "$1" | while IFS='|' read -r n min max; do
    if [ -n "$min" ] && [ -n "$max" ]; then
      printf '    %s — context %s tokens, replies up to %s tokens\n' "$n" "$min" "$max"
    else
      printf '    %s — (limits not advertised in litellm.yaml)\n' "$n"
    fi
  done
}

# copy_to_clipboard: reads stdin and hands it to the first available clipboard
# tool. rc 1 when no tool exists (stdin is still consumed so callers may go on).
copy_to_clipboard() {
  if command -v clip.exe >/dev/null 2>&1; then clip.exe
  elif command -v pbcopy >/dev/null 2>&1; then pbcopy
  elif command -v wl-copy >/dev/null 2>&1; then wl-copy
  elif command -v xclip >/dev/null 2>&1; then xclip -selection clipboard
  elif command -v xsel >/dev/null 2>&1; then xsel --clipboard --input
  else cat >/dev/null; return 1
  fi
}

# facts_service_state: read-only probe, identical to the ones `lumo-gateway
# status` uses (Get-Service / systemctl --user / sc). Prints "running",
# "installed" (present but not running) or "absent". Never mutates anything.
facts_service_state() {
  if command -v powershell.exe >/dev/null 2>&1; then
    local st
    st="$(powershell.exe -NoProfile -Command \
      '(Get-Service -Name LumoGatewayLiteLLM -ErrorAction SilentlyContinue).Status' \
      2>/dev/null | tr -d '\r')" || st=""
    case "$st" in
      Running) echo running;;
      "")      echo absent;;
      *)       echo installed;;
    esac
  elif command -v systemctl >/dev/null 2>&1; then
    if systemctl --user is-active lumo-gateway-litellm.service >/dev/null 2>&1; then
      echo running
    elif systemctl --user list-unit-files lumo-gateway-litellm.service 2>/dev/null | grep -q lumo-gateway-litellm; then
      echo installed
    else
      echo absent
    fi
  elif command -v sc >/dev/null 2>&1 && sc query LumoGatewayLiteLLM >/dev/null 2>&1; then
    echo installed
  else
    echo absent
  fi
}

# facts_install_cmd: the exact next-step command slice-a prints at the end of a
# gateway-only install (install.sh builds the same string). CFG is the
# litellm.yaml whose checkout the command must point at.
facts_install_cmd() {
  local cfg="$1" root ps1
  root="$(cd "$(dirname "$cfg")/.." && pwd)"
  if command -v powershell.exe >/dev/null 2>&1; then
    ps1="$(cygpath -w "$root/gateway/install-supervisor.ps1" 2>/dev/null || echo "$root\\gateway\\install-supervisor.ps1")"
    printf 'powershell -File "%s" -Install' "$ps1"
  else
    printf 'set up the systemd/launchd units per docs/GATEWAY.md ("Always-on on Linux (systemd)") in the checkout (%s)' "$root"
  fi
}

# facts_service_line CFG: the "Services:" fact-sheet row.
facts_service_line() {
  local cfg="$1"
  case "$(facts_service_state)" in
    running)   echo "  Services:      running (LumoGatewayLiteLLM)";;
    installed) echo "  Services:      installed but not running — start them (services.msc) or reboot";;
    *)         echo "  Services:      not installed yet — next action: $(facts_install_cmd "$cfg")";;
  esac
}

# facts_copy_key_flow: copy the master key to the clipboard WITHOUT ever letting
# the key value near stdout/stderr: it goes file -> clipboard tool only.
facts_copy_key_flow() {
  local key_path; key_path="$(facts_key_path)"
  printf 'Press Enter to copy the API key (it will NOT be printed)... '
  if [[ -t 0 ]] && IFS= read -r _facts_discard; then
    if [ -s "$key_path" ]; then
      if copy_to_clipboard < "$key_path"; then
        echo "API key copied to clipboard (read from $key_path; never printed)."
      else
        echo "no clipboard tool found; key NOT copied — copy it yourself from: $key_path"
      fi
    else
      echo "no master key at $key_path — run 'lumo-gateway setup' first."
    fi
  else
    echo ""
    echo "non-interactive session; key NOT copied — copy it yourself from: $key_path"
  fi
}

# facts_print_sheet: the whole post-setup UX, in spec order — fact sheet,
# clipboard flow (base URL announced, key on Enter), generic wiring instructions.
facts_print_sheet() {
  local cfg="${FACTS_CONFIG:-$(facts_config_default)}"
  echo ""
  echo "Gateway fact sheet:"
  echo "  Base URL:      $BASE_URL"
  echo "  Protocols:     OpenAI-compatible (/v1/chat/completions, /v1/models)"
  echo "                 and Anthropic Messages (/v1/messages)"
  echo "  Models:"
  facts_render_models "$cfg"
  local key_path; key_path="$(facts_key_path)"
  echo "  Master key:    $key_path (the key value is never printed to the terminal)"
  facts_service_line "$cfg"
  echo ""
  if printf '%s' "$BASE_URL" | copy_to_clipboard; then
    echo "Base URL copied to clipboard: $BASE_URL"
  else
    echo "Clipboard unavailable; copy the base URL yourself: $BASE_URL"
  fi
  facts_copy_key_flow
  echo ""
  echo "Wiring it up:"
  echo "  - Any OpenAI-compatible client: point it at $BASE_URL (OpenAI SDK"
  echo "    base_url, LiteLLM SDK, curl, etc.)."
  echo "  - As a LiteLLM provider specifically: model id litellm/lumo-max,"
  echo "    api_base $BASE_URL/v1, key from the path above."
  echo ""
  echo "Check with: lumo-gateway status"
}
