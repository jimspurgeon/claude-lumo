#!/bin/bash
# install.sh: set up claude-lumo on this machine (Windows, Git Bash). Safe to re-run.
# Run from inside this folder (or a clone of it). Installs into ~/.claude/lumo.
# Needs: Claude Code, Node 22.13+, Python 3.12 on PATH as `python`, Firefox logged
# in to lumo.proton.me.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
D="$HOME/.claude/lumo"; S="$HOME/.claude-lumo"; VENV="$S/litellm-venv"; BIN="$APPDATA/npm"
mkdir -p "$S" "$BIN"

# Copy this folder's files into ~/.claude/lumo (skip the .git dir if run from a clone).
if [ "$SRC" != "$D" ]; then
  mkdir -p "$D"
  cp -r "$SRC"/. "$D"/
  rm -rf "$D/.git"
fi

if [ ! -x "$VENV/Scripts/litellm.exe" ]; then
  python -m venv "$VENV"
  "$VENV/Scripts/python" -m pip install -q -r "$D/requirements.txt"
fi
[ -s "$S/master.key" ] || python -c "import secrets;print('sk-lumo-'+secrets.token_hex(24))" > "$S/master.key"

# Point the status line at this machine's path (forward slashes for Git Bash).
python - "$D" <<'PY'
import json, sys, os
p = os.path.join(sys.argv[1], "settings-lumo.json")
with open(p) as f: cfg = json.load(f)
cfg["statusLine"]["command"] = f'python "{sys.argv[1].replace(chr(92), "/")}/statusline.py"'
with open(p, "w") as f: json.dump(cfg, f, indent=2)
PY

# Shims: put claude-lumo on PATH (%APPDATA%/npm is on PATH once Node is installed).
printf '#!/bin/sh\nexec bash "$HOME/.claude/lumo/claude-lumo.sh" "$@"\n' > "$BIN/claude-lumo"
printf '@echo off\r\n"C:\\Program Files\\Git\\bin\\bash.exe" "%%USERPROFILE%%\\.claude\\lumo\\claude-lumo.sh" %%*\r\n' > "$BIN/claude-lumo.cmd"

echo "Installed. Test: claude-lumo -p 'reply with pong'"
