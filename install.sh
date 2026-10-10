#!/bin/bash
# install.sh: set up claude-lumo on this machine (Windows, macOS, Linux). Safe to re-run.
# Run from inside this folder (or a clone of it). Installs into ~/.claude/lumo.
# Needs: Claude Code, Node 22.13+, Python 3.12+ (`python` or `python3`), Firefox
# logged in to lumo.proton.me.
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
