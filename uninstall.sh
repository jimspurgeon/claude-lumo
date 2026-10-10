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

echo "Removing files..."
rm -rf "$HOME/.claude-lumo" "$HOME/.claude/lumo"
rm -f "$HOME/.local/bin/claude-lumo" "$HOME/.npm-global/bin/claude-lumo" \
      "$APPDATA/npm/claude-lumo" "$APPDATA/npm/claude-lumo.cmd" 2>/dev/null || true

echo "Removed. Close any open claude-lumo terminals, and delete any clones/downloads of the repo folder if you kept one."
