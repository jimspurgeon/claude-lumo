#!/bin/bash
# install.sh: set up claude-lumo on this machine (Windows, macOS, Linux). Safe to re-run.
# Run from inside this folder (or a clone of it). Installs into ~/.claude/lumo.
# Needs: Git, Node 22.13+, Python 3.12+ (`python` or `python3`), Firefox/Chrome/Edge/Safari
# logged in to lumo.proton.me. Auto-installs Claude Code if missing.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
D="$HOME/.claude/lumo"; S="$HOME/.claude-lumo"; VENV="$S/litellm-venv"

PY="python"
command -v "$PY" >/dev/null 2>&1 || PY="python3"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) OS="windows";;
  Darwin)               OS="macos";;
  *)                    OS="linux";;
esac
case "$OS" in
  windows) BIN="$APPDATA/npm";         VP="$VENV/Scripts/python"; LITELLM="$VENV/Scripts/litellm.exe";;
  macos)   BIN="$HOME/.npm-global/bin"; VP="$VENV/bin/python";    LITELLM="$VENV/bin/litellm";;
  linux)   BIN="$HOME/.local/bin";      VP="$VENV/bin/python";    LITELLM="$VENV/bin/litellm";;
esac

mkdir -p "$S" "$BIN"

# === Prerequisites checks with auto-install ===
echo "Checking prerequisites..."

# Git
if ! command -v git >/dev/null 2>&1; then
  echo "Git not found. Installing..."
  case "$OS" in
    windows) winget install --accept-source-agreements --accept-package-agreements Git.Git || { echo "Manual: download https://git-scm.com"; exit 1; };;
    macos)   xcode-select --install 2>/dev/null || brew install git || { echo "Manual: https://git-scm.com"; exit 1; };;
    linux)   sudo apt-get update -qq && sudo apt-get install -y git || { echo "Manual: https://git-scm.com"; exit 1; };;
  esac
fi

# Node.js 22.13+
NODE_OK=0
if command -v node >/dev/null 2>&1; then
  MAJOR="$(node --version | sed 's/^v//' | cut -d. -f1)"
  [ "${MAJOR:-0}" -ge 22 ] && NODE_OK=1
fi
if [ "$NODE_OK" = "0" ]; then
  echo "Node.js 22+ not found. Installing..."
  case "$OS" in
    windows) winget install --accept-source-agreements --accept-package-agreements OpenJS.NodeJS.LTS || { echo "Manual: https://nodejs.org"; exit 1; };;
    macos)   brew install node || { echo "Manual: https://nodejs.org"; exit 1; };;
    linux)   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs || { echo "Manual: https://nodejs.org"; exit 1; };;
  esac
  hash -r 2>/dev/null || true
fi

# Python 3.12+ (as `python` or `python3`)
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.12+ not found. Installing..."
  case "$OS" in
    windows) winget install --accept-source-agreements --accept-package-agreements Python.Python.3.12 || { echo "Manual: https://python.org (tick 'Add to PATH')"; exit 1; };;
    macos)   brew install python@3.12 || { echo "Manual: https://python.org"; exit 1; };;
    linux)   sudo apt-get update -qq && sudo apt-get install -y python3 python3-venv python3-pip || { echo "Manual: https://python.org"; exit 1; };;
  esac
  command -v python >/dev/null 2>&1 || PY="python3"
fi

# Claude Code — install from latest if missing
if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code not found. Installing the latest version..."
  case "$OS" in
    windows) powershell.exe -NoProfile -Command "irm https://claude.ai/install.ps1 | iex" || npm install -g @anthropic-ai/claude-code || { echo "Manual: npm i -g @anthropic-ai/claude-code"; };;
    *)       curl -fsSL https://claude.ai/install.sh | bash || npm install -g @anthropic-ai/claude-code || { echo "Manual: npm i -g @anthropic-ai/claude-code"; };;
  esac
  hash -r 2>/dev/null || true
fi
if command -v claude >/dev/null 2>&1; then
  echo "Claude Code: $(command -v claude)"
else
  echo "WARNING: Claude Code still not on PATH. If it was just installed, reopen the"
  echo "terminal and re-run this script, or install manually: npm i -g @anthropic-ai/claude-code"
fi

echo "Prerequisites check complete."

mkdir -p "$S" "$BIN"

# Copy this folder's files into ~/.claude/lumo (skip the .git dir if run from a clone).
if [ "$SRC" != "$D" ]; then
  mkdir -p "$D"
  cp -r "$SRC"/. "$D"/
  rm -rf "$D/.git"
fi

if [ ! -x "$LITELLM" ]; then
  "$PY" -m venv "$VENV"
  "$VP" -m pip install -q -r "$D/requirements.txt"
fi
[ -s "$S/master.key" ] || "$PY" -c "import secrets;print('sk-lumo-'+secrets.token_hex(24))" > "$S/master.key"

# Point the status line at this machine's install path (forward slashes work everywhere).
"$PY" - "$D" <<'PY'
import json, sys
p = sys.argv[1].replace("\\", "/") + "/settings-lumo.json"
with open(p) as f: cfg = json.load(f)
cfg["statusLine"]["command"] = "python " + sys.argv[1].replace("\\", "/") + "/statusline.py"
with open(p, "w") as f: json.dump(cfg, f, indent=2)
PY

# Put claude-lumo on PATH. In a bash shell:
printf '#!/bin/sh\nexec bash "$HOME/.claude/lumo/claude-lumo.sh" "$@"\n' > "$BIN/claude-lumo"
chmod +x "$BIN/claude-lumo"

# Windows also needs a .cmd wrapper for cmd.exe / PowerShell.
if [ "$OS" = "windows" ]; then
  printf '@echo off\r\n"C:\\Program Files\\Git\\bin\\bash.exe" "%%USERPROFILE%%\\.claude\\lumo\\claude-lumo.sh" %%*\r\n' > "$BIN/claude-lumo.cmd"
fi

echo "Installed into ~/.claude/lumo. If 'claude-lumo' is not found, add $BIN to PATH and reopen the terminal."
echo "Test: claude-lumo -p 'reply with pong'"
