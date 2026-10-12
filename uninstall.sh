#!/bin/bash
# uninstall.sh: remove claude-lumo (macOS, Linux, and Windows via Git Bash).
# Stops the local proxy/LiteLLM if running, then deletes everything claude-lumo
# added: ~/.claude/lumo, ~/.claude-lumo, and the PATH shims. Plain `claude` is
# untouched, as are Claude Code settings and login.
# Not a deletion risk to anything else: it only removes the two claude-lumo
# folders and the three shim files it created.
set -e

echo "Stopping claude-lumo background processes (proxy, LiteLLM)..."
pkill -f "claude.lumo/proxy/proxy.mjs" 2>/dev/null || true
pkill -f "claude-lumo/litellm-venv" 2>/dev/null || true

# Remove the resume shim profile lines first (before deleting ~/.claude/lumo).
if [ -f "$HOME/.claude/lumo/scripts/resume-shim-setup.sh" ]; then
  bash "$HOME/.claude/lumo/scripts/resume-shim-setup.sh" uninstall
fi

echo "Removing files..."
rm -rf "$HOME/.claude-lumo" "$HOME/.claude/lumo"
rm -f "$HOME/.local/bin/claude-lumo" "$HOME/.npm-global/bin/claude-lumo" \
      "$APPDATA/npm/claude-lumo" "$APPDATA/npm/claude-lumo.cmd" 2>/dev/null || true

# Remove the plain-Claude opt-in wipe-offer hook from ~/.claude/settings.json, if the
# optional opt-in step added it. Anything else in settings.json stays untouched.
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && grep -q "session-start-offer.py" "$SETTINGS" 2>/dev/null; then
  PY="python"
  command -v "$PY" >/dev/null 2>&1 || PY="python3"
  if command -v "$PY" >/dev/null 2>&1; then
    if [ -f "$SETTINGS.bak-wipe-offer" ]; then
      # Restore the pre-opt-in backup (made by install.sh when it added the hook).
      cp "$SETTINGS.bak-wipe-offer" "$SETTINGS"
      rm -f "$SETTINGS.bak-wipe-offer"
      echo "Restored ~/.claude/settings.json from the pre-opt-in backup."
    else
      # No backup: strip just the hook entry the installer added.
      "$PY" - "$SETTINGS" <<'EOF'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
start = data.get("hooks", {}).get("SessionStart", [])
kept = [
    entry for entry in start
    if not any(
        "session-start-offer.py" in (h or {}).get("command", "")
        for h in (entry or {}).get("hooks", [])
    )
]
if kept:
    data["hooks"]["SessionStart"] = kept
else:
    del data["hooks"]["SessionStart"]
    if not data["hooks"]:
        del data["hooks"]
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
EOF
      echo "Removed the wipe-offer hook from ~/.claude/settings.json."
    fi
  else
    echo "Note: python not found; remove the session-start-offer.py entry from ~/.claude/settings.json yourself."
  fi
fi

echo "Removed. Close any open claude-lumo terminals, and delete any clones/downloads of the repo folder if you kept one."
