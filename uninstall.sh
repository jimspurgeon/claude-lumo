#!/bin/bash
# uninstall.sh: remove claude-lumo (macOS, Linux, and Windows via Git Bash).
# Stops the local proxy/LiteLLM if running, stops and removes the WinSW
# services when installed (gateway/install-supervisor.ps1 -Install, #183),
# then deletes everything claude-lumo added: ~/.claude/lumo, ~/.claude-lumo,
# ~/.lumoservices, and the PATH shims. Plain `claude` is untouched, as are
# Claude Code settings and login. Removing Windows services needs an
# elevated shell; without one the service removal lines are skipped with a
# warning and the rest of the uninstall still runs.
# Not a deletion risk to anything else: it only removes the claude-lumo
# folders and the shim files it created.
set -e

echo "Stopping claude-lumo background processes (proxy, LiteLLM)..."
pkill -f "claude.lumo/proxy/proxy.mjs" 2>/dev/null || true
pkill -f "claude.lumo/gateway/proxy.mjs" 2>/dev/null || true
pkill -f "claude-lumo/litellm-venv" 2>/dev/null || true

# Remove the WinSW services (gateway/install-supervisor.ps1 -Install, #183).
# Preferred path: each service's own WinSW exe (stop + uninstall). Fallback
# when the exes are gone but the services are still registered: sc.exe.
# Both need an elevated shell on Windows; failures are reported, not fatal,
# so the rest of the uninstall still runs.
for id in LumoGatewayLiteLLM LumoGatewayProxy; do
  exe="$HOME/.lumoservices/$id/WinSW.exe"
  if [ -f "$exe" ]; then
    echo "Stopping and removing service $id via its WinSW exe..."
    "$exe" stop >/dev/null 2>&1 || true
    "$exe" uninstall >/dev/null 2>&1 || true
  elif command -v sc >/dev/null 2>&1 && sc query "$id" >/dev/null 2>&1; then
    echo "Service $id registered but its WinSW exe is gone; trying sc.exe..."
    sc stop "$id" >/dev/null 2>&1 || true
    sc delete "$id" >/dev/null 2>&1 || true
  fi
done
# If a service survived (usually no elevation), say so plainly.
if command -v sc >/dev/null 2>&1; then
  for id in LumoGatewayLiteLLM LumoGatewayProxy; do
    sc query "$id" >/dev/null 2>&1 && \
      echo "Warning: service $id is still registered. Rerun from an elevated shell (or run: sc delete $id)."
  done
fi
rm -rf "$HOME/.lumoservices" 2>/dev/null || true

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
      # Tradeoff: the backup captures the entire settings.json before the opt-in,
      # so restoring it discards any manual changes made AFTER opt-in but BEFORE
      # uninstall. If you kept important edits, restore the file manually and
      # re-apply just the opt-in removal with the Python snippet below.
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
