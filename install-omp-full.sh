#!/bin/bash
# install-omp-full.sh: one command, full stack — lumo gateway + omp + wiring + config sync.
#
# Linux/macOS one-liner:
#   curl -fsSL https://raw.githubusercontent.com/jimspurgeon/lumo-gateway/master/install-omp-full.sh | bash
# Windows PowerShell one-liner (wrapper downloads and runs this script under Git Bash):
#   irm https://raw.githubusercontent.com/jimspurgeon/lumo-gateway/master/install-omp-full.ps1 | iex
#
# Stages (each idempotent — re-running prints "already done" and moves on):
#   1. Gateway:    lumo-gateway setup (checkout ~/.lumo-gateway, LiteLLM venv, master key,
#                  lumo-gateway CLI on PATH). Install.sh never installs the services:
#                  that stays a user-triggered elevated step, printed below.
#   2. omp:        official omp installer (https://omp.sh/install), printed first.
#   3. Wire API:   ~/.omp/agent/models.yml gains a `litellm` provider pointing at
#                  http://127.0.0.1:4000/v1 (python yaml round-trip; existing content
#                  survives; the API key NEVER lands in models.yml). The gateway master
#                  key reaches omp's credential store via `omp auth import` from a
#                  chmod-600 mktemp file (never argv, never echoed, shredded after).
#                  config.yml modelRoles.default is set to litellm/lumo-max ONLY when
#                  unset — an existing choice is never clobbered.
#   4. Sync:       clones jimspurgeon/omp-config and restores its files into ~/.omp/agent
#                  (RULES.md, agents/, extensions/, skills/, config.yml), keeping a local
#                  file whenever it is newer than the repo copy (mtime; every decision is
#                  printed).
#
# Security: the master-key VALUE is never printed, echoed, or passed in argv. Only this
# repo's install.sh and the official omp installer are ever piped to a shell, and both
# URLs are printed before execution. No service mutation happens here (no ports 4000/8790
# touched by this script itself).
#
# Env knobs:
#   CLAUDE_LUMO_STATE_DIR  gateway state dir (default ~/.claude-lumo)
#   OMP_CONFIG_TOKEN       GitHub token; the omp-config clone uses
#                         https://<token>@github.com/jimspurgeon/omp-config.git when
#                         SSH is unavailable. When neither SSH nor a token works the
#                         stage self-skips with a clear message instead of failing.
set -euo pipefail

RAW_BASE="https://raw.githubusercontent.com/jimspurgeon/lumo-gateway/master"
GATEWAY_BASE_URL="http://127.0.0.1:4000"
OMP_INSTALL_URL="https://omp.sh/install"
OMP_CONFIG_REPO="jimspurgeon/omp-config"
S="${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}"
KEY_PATH="$S/master.key"
AGENT_DIR="$HOME/.omp/agent"
MODELS_YML="$AGENT_DIR/models.yml"
CONFIG_YML="$AGENT_DIR/config.yml"
OMP_CONFIG_DIR="$HOME/.omp-config"       # persistent clone, so re-runs are incremental
OMPFULL_SKIP_SYNC="${OMPFULL_SKIP_SYNC:-0}"

section() { echo ""; echo "=== $1 ==="; }
notice()  { echo "[install-omp-full] $*"; }
fail()    { echo "[install-omp-full] ERROR: $*" >&2; exit 1; }

# Gateways setup helpers expect bash+curl+git; piped `bash` gives us the first two.
command -v curl >/dev/null 2>&1 || fail "curl is required (curl -fsSL ... | bash cannot run without it)."
command -v git  >/dev/null 2>&1 || fail "git is required. Install it, then re-run."

resolve_py() {
  if [ -n "${PY:-}" ] && command -v "$PY" >/dev/null 2>&1; then return 0; fi
  for cand in python python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; return 0; fi
  done
  return 1
}
resolve_py || fail "python or python3 is required (models.yml yaml round-trip, gateway venv)."
"$PY" -c 'import yaml' >/dev/null 2>&1 || {
  echo "[install-omp-full] PyYAML missing — attempting bootstrap via pip..."
  if ! "$PY" -m pip install --quiet pyyaml >/dev/null 2>&1; then
    # Debian-family cripples system pip; use the user scheme.
    "$PY" -m pip install --quiet --user pyyaml >/dev/null 2>&1 || \
      "$PY" -m pip install --quiet --break-system-packages pyyaml >/dev/null 2>&1 || \
      fail "Could not auto-install PyYAML. Run: $PY -m pip install pyyaml — then re-run."
  fi
  "$PY" -c 'import yaml' >/dev/null 2>&1 || fail "PyYAML install reported success but import still fails — install manually: $PY -m pip install pyyaml"
}

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) OMP_EXE_HINT="Open a NEW terminal (PATH was updated) and retry.";;
  *)                    OMP_EXE_HINT="PATH note printed by the installer (reopen the terminal) and retry.";;
esac

mkdir -p "$AGENT_DIR"

# =====================================================================================
# Stage 1 — Gateway
# =====================================================================================
section "Stage 1/4: gateway (checkout, LiteLLM venv, master key, lumo-gateway CLI)"

GW_CLI=""
command -v lumo-gateway >/dev/null 2>&1 && GW_CLI="$(command -v lumo-gateway)"

GATEWAY_DONE=0
if [ -n "$GW_CLI" ] && [ -s "$KEY_PATH" ]; then
  GATEWAY_DONE=1
  echo "  Already installed: lumo-gateway CLI ($GW_CLI) and master key present. Skipping setup."
  # Fact-sheet details survive re-runs; print the essential connection facts instead.
  echo "  Base URL:  $GATEWAY_BASE_URL"
  echo "  Key file:  $KEY_PATH"
  echo "  Health:    lumo-gateway status"
fi

if [ "$GATEWAY_DONE" = "0" ]; then
  SRC=""
  _self="${BASH_SOURCE[0]:-}"
  if [ -n "$_self" ] && [ -f "$(dirname "$_self")/install.sh" ]; then
    SRC="$(cd "$(dirname "$_self")" && pwd)"
  fi
  if [ -n "$SRC" ]; then
    echo "  Running this repo's install.sh --gateway-only (found checkout at $SRC)..."
    bash "$SRC/install.sh" --gateway-only
  else
    echo "  Downloading this repo's install.sh (pinned to master):"
    echo "    $RAW_BASE/install.sh"
    curl -fsSL --retry 3 "$RAW_BASE/install.sh" | bash -s -- --gateway-only
  fi
  # install.sh puts the CLI in the bin dir but the CURRENT shell PATH may predate it.
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) GW_BIN="${APPDATA:-}"; [ -n "$GW_BIN" ] && GW_BIN="$GW_BIN/npm" || GW_BIN="";;
    Darwin)               GW_BIN="$HOME/.npm-global/bin";;
    *)                    GW_BIN="$HOME/.local/bin";;
  esac
  case ":$PATH:" in *":$GW_BIN:"*) ;; *) [ -d "$GW_BIN" ] && PATH="$GW_BIN:$PATH" && export PATH;; esac
  [ -s "$KEY_PATH" ] || fail "gateway setup completed but $KEY_PATH is missing."
fi

# Gateway fact sheet: run the fact-sheet part when the checkout is reachable, else print
# the essentials inline (base URL + key path + status hint). Never the key value.
FACTS_SH=""
for _cand in "$HOME/.lumo-gateway" "$HOME/.claude/lumo" "$(dirname "${BASH_SOURCE[0]:-$0}")"; do
  if [ -f "$_cand/gateway/setup-facts.sh" ]; then FACTS_SH="$_cand/gateway/setup-facts.sh"; break; fi
done
if [ -n "$FACTS_SH" ]; then
  # Run the fact sheet from the installed checkout: models come straight from that
  # checkout's gateway/litellm.yaml. Its key-copy prompt is skipped automatically in
  # a piped (non-tty) run, and stage 3 wires the key into omp anyway — no action needed.
  # `sh` vs bash: setup-facts.sh guards its bash-isms, but be safe and use bash.
  export BASE_URL="$GATEWAY_BASE_URL" KEY_PATH="$KEY_PATH"
  bash -c '. "$1"; facts_print_sheet' _ "$FACTS_SH" || true
else
  echo "  Gateway connection facts:"
  echo "    Base URL: $GATEWAY_BASE_URL  (OpenAI + Anthropic protocols)"
  echo "    Models:   lumo-max (default tier), lumo-lite (cheap tier)"
  echo "    Key file: $KEY_PATH (value never printed)"
  echo "    Health:   lumo-gateway status"
fi

# =====================================================================================
# Stage 2 — omp
# =====================================================================================
section "Stage 2/4: install omp (official installer)"

if command -v omp >/dev/null 2>&1; then
  echo "  Already installed: omp $(omp --version 2>/dev/null || echo '(version unknown)'). Skipping."
else
  echo "  No omp found. The official omp installer will now run:"
  echo "    curl -fsSL $OMP_INSTALL_URL | sh"
  # Installers commonly land in ~/.local/bin (Linux) or ~/.omp/bin; the CURRENT shell
  # does not get the updated PATH, so probe the usual candidates before giving up.
  probe_omp_bin_dirs() {
    local d
    for d in "$HOME/.local/bin" "$HOME/bin" "$HOME/.omp/bin" "$HOME/.omp/natives/bin"; do
      if [ -x "$d/omp" ]; then PATH="$d:$PATH"; export PATH; return 0; fi
    done
    return 1
  }
  if curl -fsSL "$OMP_INSTALL_URL" | sh; then
    :
  else
    echo "  Official installer reported an error." >&2
  fi
  command -v omp >/dev/null 2>&1 || probe_omp_bin_dirs || true
  if command -v omp >/dev/null 2>&1; then
    echo "  Installed: omp $(omp --version 2>/dev/null || echo '(version unknown)')"
  else
    fail "omp not found after install. $OMP_EXE_HINT"
  fi
fi

# =====================================================================================
# Stage 3 — Wire omp to the gateway API
# =====================================================================================
section "Stage 3/4: wire omp to the gateway (models.yml + credential + default role)"

# --- 3a. Provider entry in models.yml (python yaml round-trip, key never written here) ---
"$PY" - "$MODELS_YML" "$GATEWAY_BASE_URL" <<'PYEOF'
import sys, os
import yaml

path, base_url = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if content.strip():
        data = yaml.safe_load(content) or {}

providers = data.setdefault("providers", {})
if not isinstance(providers, dict):
    sys.exit("models.yml: 'providers' is not a mapping; refusing to overwrite.")

litellm = providers.setdefault("litellm", {})
if not isinstance(litellm, dict):
    sys.exit("models.yml: providers.litellm is not a mapping; refusing to overwrite.")

changed = []
if litellm.get("baseUrl") != base_url + "/v1":
    litellm["baseUrl"] = base_url + "/v1"
    changed.append("baseUrl=http://" + base_url.rsplit("//", 1)[1] + "/v1")

# models: an empty [] with discovery risks validating as "provider with models and no
# entries"; a models-free override entry with baseUrl stays valid. So models stay absent —
# omp's built-in litellm provider knows the model ids already.
# Also purge any apiKey that might have been written there historically: the key belongs
# in the credential store, never in a config file (do not overwrite with a new one).
if litellm.pop("apiKey", None) is not None:
    changed.append("removed apiKey (key belongs in the credential store)")

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)

if changed:
    print("  models.yml updated: " + ", ".join(changed))
else:
    print("  models.yml: litellm provider already wired to the gateway. No changes.")
PYEOF

# --- 3b. Credential: master key -> omp's store, via a chmod-600 mktemp file ---
# The key VALUE never appears in argv, stdout, stderr or trace: it travels
# key-file -> temp file (0600) -> `omp auth import <file>` -> shred. Import merges
# rather than replaces, so an existing litellm credential keeps working; that is fine —
# it is the same key on this machine.
CRED_FILE="$(mktemp)"
trap 'shred_or_rm' RETURN 2>/dev/null || true
shred_or_rm() { :; }
cleanup_cred() {
  if command -v shred >/dev/null 2>&1; then
    shred -u "$CRED_FILE" 2>/dev/null || rm -f "$CRED_FILE"
  else
    rm -P "$CRED_FILE" 2>/dev/null || rm -f "$CRED_FILE"
  fi
}
trap cleanup_cred EXIT

# Tr reads on a single line in Git Bash too; strip both CR and trailing newline.
KEY_VALUE="$(tr -d '\r\n' < "$KEY_PATH")"
[ -n "$KEY_VALUE" ] || fail "master key at $KEY_PATH is empty."

# Json built by printf with a single %s for the key, all inside the file write.
printf '{"version":1,"credentials":[{"provider":"litellm","credential":{"type":"api_key","key":"%s","source":"login"}}]}\n' \
  "$KEY_VALUE" > "$CRED_FILE"

if omp auth import "$CRED_FILE" >/dev/null 2>&1; then
  echo "  Credential imported into omp's store (litellm api_key; value not printed)."
else
  rm -f "$CRED_FILE" 2>/dev/null || true
  fail "omp auth import failed; check 'omp auth import --help' on this omp version."
fi
unset KEY_VALUE
rm -f "$CRED_FILE" 2>/dev/null || true

# --- 3c. Default model role: set ONLY when unset (never clobber an existing choice) ---
if [ ! -f "$CONFIG_YML" ]; then
  printf 'modelRoles:\n  default: litellm/lumo-max\n' > "$CONFIG_YML"
  echo "  config.yml: created with modelRoles.default = litellm/lumo-max."
elif grep -Eq '^[[:space:]]*default:[[:space:]]*[^[:space:]#]+' "$CONFIG_YML"; then
  # Cheap pre-check first: any default under modelRoles (or anywhere) counts as a
  # user choice and wins — exact-untouched is the requirement, not precision here.
  echo "  config.yml: modelRoles.default already set — not touched (existing choice kept)."
else
  # default unset: set it with a yaml round-trip (same mechanism as models.yml) so a
  # modelRoles block placed mid-file is patched in place, never appended under the
  # wrong parent.
  if ! "$PY" - "$CONFIG_YML" <<'PYEOF'
import sys
import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    content = f.read()
data = yaml.safe_load(content) or {}
roles = data.setdefault("modelRoles", {})
roles["default"] = "litellm/lumo-max"
with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
PYEOF
  then
    echo "  config.yml: could not parse; leaving it untouched." >&2
  else
    echo "  config.yml: set modelRoles.default = litellm/lumo-max (was unset)."
  fi
fi

echo "  Wiring complete: provider $GATEWAY_BASE_URL/v1, credential imported, default role set if it was unset."

# =====================================================================================
# Stage 4 — Sync config from jimspurgeon/omp-config
# =====================================================================================
section "Stage 4/4: sync omp config from $OMP_CONFIG_REPO"

if [ "$OMPFULL_SKIP_SYNC" = "1" ]; then
  echo "  Skipped (OMPFULL_SKIP_SYNC=1)."
else
  CONFIG_CLONE_OK=0

  # Freshen the persistent clone when it exists (it was reachable before, so just pull).
  if [ -d "$OMP_CONFIG_DIR/.git" ]; then
    if git -C "$OMP_CONFIG_DIR" pull --ff-only -q >/dev/null 2>&1; then
      CONFIG_CLONE_OK=1
    else
      echo "  Note: could not update the clone at $OMP_CONFIG_DIR (offline?); using the local copy."
      CONFIG_CLONE_OK=1
    fi
  fi

  # Existing-but-not-git: refuse to clobber, self-skip.
  if [ "$CONFIG_CLONE_OK" = "0" ] && [ -d "$OMP_CONFIG_DIR" ]; then
    echo "  WARNING: $OMP_CONFIG_DIR exists but is not a git clone; leaving it alone."
    echo "  Fix it (remove it) and re-run for config sync."
  fi

  # Clone: try HTTPS+token, then plain HTTPS (public fork case), then SSH.
  if [ "$CONFIG_CLONE_OK" = "0" ]; then
    _OMP_CONFIG_TOKEN="${OMP_CONFIG_TOKEN:-}"
    TOKEN_URL="https://${_OMP_CONFIG_TOKEN}@github.com/${OMP_CONFIG_REPO}.git"
    if [ -n "$_OMP_CONFIG_TOKEN" ] && git clone -q "$TOKEN_URL" "$OMP_CONFIG_DIR" >/dev/null 2>&1; then
      CONFIG_CLONE_OK=1
      echo "  Cloned $OMP_CONFIG_REPO (HTTPS token)."
    elif git clone -q "https://github.com/${OMP_CONFIG_REPO}.git" "$OMP_CONFIG_DIR" >/dev/null 2>&1; then
      CONFIG_CLONE_OK=1
      echo "  Cloned $OMP_CONFIG_REPO (anonymous HTTPS)."
    elif git clone -q "git@github.com:${OMP_CONFIG_REPO}.git" "$OMP_CONFIG_DIR" >/dev/null 2>&1; then
      CONFIG_CLONE_OK=1
      echo "  Cloned $OMP_CONFIG_REPO (SSH)."
    else
      rm -rf "$OMP_CONFIG_DIR" 2>/dev/null || true
    fi
  fi

  if [ "$CONFIG_CLONE_OK" = "0" ]; then
    echo "  Self-skipping this stage: could not clone $OMP_CONFIG_REPO."
    echo "  Access is private. Either:"
    echo "    - set up an SSH key with github.com, or"
    echo "    - export OMP_CONFIG_TOKEN=<GitHub PAT with repo access> and re-run."
    echo "  Everything else is fully installed and wired; only the personal"
    echo "  config sync (agents/extensions/RULES.md/config.yml) is missing."
  else
    echo "  Restoring files into $AGENT_DIR (merge-critical files restored only when absent):"
    restore_tree() {
      local src="$1" dst="$2" f rel ds abs
      # Repo meta files are not agent config; only RULES.md, config.yml, agents/,
      # extensions/, skills/, agent/ (and future config dirs) are restored.
      # Merge-critical files (user-editable config) are restored ONLY when absent
      # locally — same "never clobber an existing choice" rule as stage 3c's
      # modelRoles handling. Timestamps there meant a repo copy could silently
      # revert local edits, so they are not consulted at all.
      # shellcheck disable=SC2044
      for f in $(cd "$src" && find . -type f ! -path './.git/*' ! -name 'README.md' ! -name 'install.sh' ! -name 'sync.sh' | sort); do
        rel="${f#./}"
        ds="$dst/$rel"
        abs="$src/$rel"
        mkdir -p "$(dirname "$ds")"
        if [ ! -e "$ds" ]; then
          cp -p "$abs" "$ds"
          echo "    restored from repo: $rel (new file)"
        elif [ "$rel" = "config.yml" ] || [ "$rel" = "RULES.md" ] || [[ "$rel" == agents/* ]]; then
          echo "    kept local (merge-critical): $rel"
        elif cmp -s "$ds" "$abs"; then
          echo "    identical:          $rel"
        elif [ "$ds" -nt "$abs" ]; then
          echo "    kept local (newer): $rel"
        else
          cp -p "$abs" "$ds"
          echo "    restored from repo: $rel"
        fi
      done
    }
    restore_tree "$OMP_CONFIG_DIR" "$AGENT_DIR"
    # No post-processing beyond restore: plugins install via `omp plugin install`
    # on demand; omp loads RULES.md, agents/, extensions/, skills/ directly.
    echo "  Config sync complete."

    # config.yml is merge-critical at the key level too: restore only keys/
    # sections absent locally (never overwrite an existing value, including an
    # existing modelRoles.default), via the same yaml round-trip as stage 3c.
    if [ -f "$OMP_CONFIG_DIR/config.yml" ] && [ -f "$CONFIG_YML" ]; then
      if ! "$PY" - "$OMP_CONFIG_DIR/config.yml" "$CONFIG_YML" <<'PYEOF'
import sys

import yaml


def merge_missing(src, dst):
    """Copy keys into dst only where absent; dicts merge recursively."""
    changed = False
    for k, v in src.items():
        if k not in dst or dst[k] is None:
            dst[k] = v
            changed = True
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            changed = merge_missing(v, dst[k]) or changed
    return changed


repo_path, user_path = sys.argv[1], sys.argv[2]
with open(repo_path, encoding="utf-8") as f:
    repo = yaml.safe_load(f) or {}
with open(user_path, encoding="utf-8") as f:
    user = yaml.safe_load(f) or {}
if merge_missing(repo, user):
    with open(user_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(user, f, sort_keys=False, allow_unicode=True)
PYEOF
      then
        echo "  config.yml: could not merge; local file left untouched." >&2
      else
        echo "  config.yml: merged keys absent locally (existing choices kept)."
      fi
    fi
  fi
fi

# =====================================================================================
# Summary
# =====================================================================================
section "All-in-one omp install complete"
echo "  Gateway:   $GATEWAY_BASE_URL  (models: lumo-max, lumo-lite)"
echo "  Key file:  $KEY_PATH   (value never printed; see: lumo-gateway api-key)"
echo "  omp:       $(command -v omp)"
echo "  omp dir:   $AGENT_DIR"
if command -v lumo-gateway >/dev/null 2>&1; then
  echo "  Health:    lumo-gateway status"
fi
echo ""
echo "Next steps:"
echo "  1. Make sure the gateway services are installed and running:"
echo "     - Windows (elevated PowerShell): see the command printed by 'lumo-gateway setup'"
echo "       (gateway/install-supervisor.ps1 -Install), or check: lumo-gateway status"
echo "     - Linux/macOS: systemd/launchd recipes in docs/GATEWAY.md"
echo "  2. Start omp anywhere:  omp"
echo "  3. (Optional) config sync re-run: this script; it is safe and incremental."
echo "  4. If omp is not found: close and reopen the terminal (installer PATH note)."
