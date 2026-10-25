#!/bin/bash
# install.sh: set up claude-lumo on this machine (Windows, macOS, Linux). Safe to re-run.
# Run from inside this folder (or a clone of it). Installs into ~/.claude/lumo.
# Needs: Git, Node 22.13+, Python 3.12+ (`python` or `python3`), Firefox/Chrome/Edge/Safari
# logged in to lumo.proton.me. Auto-installs Claude Code if missing.
set -e
# SRC is the folder this script came from, but only when that folder really is claude-lumo. When
# piped (`curl ... | bash`) there is no such folder: $0 is "bash" and dirname would be the current
# directory, so SRC stays empty and the repo is cloned below instead of copying the cwd.
SRC=""
_self="${BASH_SOURCE[0]:-}"
if [ -n "$_self" ] && [ -f "$(dirname "$_self")/claude-lumo.sh" ]; then
  SRC="$(cd "$(dirname "$_self")" && pwd)"
fi
# Opt-in extras: --resume-shim also installs the resume shim (see README, "Resume shim").
# Accept the PowerShell-style spelling too, since install.ps1 forwards its own args verbatim.
EXTRA_RESUME_SHIM=0
for _arg in "$@"; do
  case "$_arg" in
    --resume-shim|-ResumeShim) EXTRA_RESUME_SHIM=1;;
  esac
done

REPO_URL="https://github.com/jimspurgeon/claude-lumo"
D="$HOME/.claude/lumo"; S="${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}"; VENV="$S/litellm-venv"

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
# Put our own bin dir on PATH for the rest of this script: the Claude Code installer drops
# its binary in ~/.local/bin on Linux, and without this the check right after it would
# report "still not on PATH" for an install that actually worked.
case ":$PATH:" in *":$BIN:"*) ;; *) PATH="$BIN:$PATH"; export PATH;; esac

# === Linux helper functions =============================================================
# Many Linux environments the one-liner is piped into differ from the Ubuntu-with-sudo
# desktop the original auto-install assumed: bare containers and distrobox shells run as
# root with no sudo binary; Fedora/Bazzite-family systems have dnf, not apt. So the
# prerequisite steps below go through these three helpers instead of `sudo apt-get`.
# as_root CMD ARGS: run as root — directly when we already are root, else sudo, else su.
# Package-manager invocations are always run with stdin closed and (on the deb family)
# DEBIAN_FRONTEND=noninteractive: this script's own stdin is the install script itself
# when piped (`curl ... | bash`), and apt/debconf will happily read and discard it as
# prompt input, silently truncating the script mid-run ("half an install, exit 0").
as_root() {
  local run
  if [ "$(id -u)" = "0" ]; then run=("$@")
  elif command -v sudo >/dev/null 2>&1; then run=(sudo "$@")
  else run=(su -c "$*"); fi
  DEBIAN_FRONTEND=noninteractive "${run[@]}" </dev/null
}
# PM: the first package manager found on PATH ("" when none).
PM=""
if [ "$OS" = "linux" ]; then
  for _pm in apt-get dnf yum pacman zypper apk; do
    command -v "$_pm" >/dev/null 2>&1 && { PM="$_pm"; break; }
  done
fi
# pkg_install GROUP...: install a named group of packages with the distro family's own
# names for it. Falls back to false so callers can print their Manual: hint.
pkg_install() {
  for _group in "$@"; do
    case "$PM:$_group" in
      apt-get:tools)  as_root apt-get update -qq && as_root apt-get install -y git ca-certificates;;
      dnf:tools|yum:tools) as_root "$PM" install -y git;;
      pacman:tools)  as_root pacman -Sy --noconfirm --needed git;;
      zypper:tools)  as_root zypper -n install git;;
      apk:tools)     as_root apk add git;;
      apt-get:python) as_root apt-get update -qq && as_root apt-get install -y python3 python3-venv python3-pip;;
      dnf:python|yum:python) as_root "$PM" install -y python3 python3-pip;;
      pacman:python) as_root pacman -Sy --noconfirm --needed python;;
      zypper:python) as_root zypper -n install python3 python3-pip;;
      apk:python)    as_root apk add python3 py3-pip;;
      *) return 1;;
    esac || return 1
  done
}
# node_install_linux: install Node 22 via the distro (arch/zypper ship a current one) or
# NodeSource (deb/rpm families), so `apt-only` never blocks Fedora-family systems. The
# setup script is downloaded to a file first: piping it into `sudo -E bash -` breaks when
# as_root is a no-op passthrough (root containers), where `-E` would become the command.
node_install_linux() {
  local ns_setup rc=1
  ns_setup="$(mktemp)"
  case "$PM" in
    apt-get) curl -fsSL https://deb.nodesource.com/setup_22.x -o "$ns_setup" && as_root bash "$ns_setup" \
               && as_root apt-get install -y nodejs && rc=0;;
    dnf|yum) curl -fsSL https://rpm.nodesource.com/setup_22.x -o "$ns_setup" && as_root bash "$ns_setup" \
               && as_root "$PM" install -y nodejs && rc=0;;
    pacman)  as_root pacman -Sy --noconfirm --needed nodejs && rc=0;;
    zypper) as_root zypper -n install nodejs20 || as_root zypper -n install nodejs; rc=$?;;
    *)       ;;
  esac
  rm -f "$ns_setup"
  return "$rc"
}
# =========================================================================================

# === Prerequisites checks with auto-install ===
echo "Checking prerequisites..."

# Git
if ! command -v git >/dev/null 2>&1; then
  echo "Git not found. Installing..."
  case "$OS" in
    windows) winget install --accept-source-agreements --accept-package-agreements Git.Git || { echo "Manual: download https://git-scm.com"; exit 1; };;
    macos)   xcode-select --install 2>/dev/null || brew install git || { echo "Manual: https://git-scm.com"; exit 1; };;
    linux)   pkg_install tools || { echo "Manual: https://git-scm.com"; exit 1; };;
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
    linux)   node_install_linux || { echo "Manual: https://nodejs.org"; exit 1; };;
  esac
  hash -r 2>/dev/null || true
fi

# Engines range from package.json (>=22.13 <25; tested majors are 22 and 24). Outside it we
# only warn — never fatal — since a working Node outside the range should not block an install.
if command -v node >/dev/null 2>&1; then
  NV="$(node --version 2>/dev/null | tr -d 'v')"   # e.g. 24.19.0
  NMAJ="${NV%%.*}"; NTMP="${NV#*.}"; NMIN="${NTMP%%.*}"
  case "$NMAJ" in ''|*[!0-9]*) NMAJ=0;; esac
  case "$NMIN" in ''|*[!0-9]*) NMIN=0;; esac
  if [ "$NMAJ" -lt 22 ] || { [ "$NMAJ" -eq 22 ] && [ "$NMIN" -lt 13 ]; } || [ "$NMAJ" -ge 25 ]; then
    echo "WARNING: Node $NV is outside the engines range declared in package.json (>=22.13 <25)."
    echo "This version is untested; continuing anyway."
  fi
fi

# Python 3.12+ (as `python` or `python3`)
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.12+ not found. Installing..."
  case "$OS" in
    windows) winget install --accept-source-agreements --accept-package-agreements Python.Python.3.12 || { echo "Manual: https://python.org (tick 'Add to PATH')"; exit 1; };;
    macos)   brew install python@3.12 || { echo "Manual: https://python.org"; exit 1; };;
    linux)   pkg_install python || { echo "Manual: https://python.org"; exit 1; };;
  esac
  command -v python >/dev/null 2>&1 || PY="python3"
fi
# Version gate, not just existence: distros still ship 3.11 or older (Debian 12) while the
# hash-locked requirements (e.g. numpy 2.5.4) need >=3.12. Failing here with a clear message
# beats dying inside pip's resolver six steps later with a wall of version tables.
if command -v "$PY" >/dev/null 2>&1; then
  PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
  PYMAJ="${PYVER%%.*}"; PYMIN="${PYVER#*.}"
  case "$PYMAJ" in ''|*[!0-9]*) PYMAJ=0;; esac
  case "$PYMIN" in ''|*[!0-9]*) PYMIN=0;; esac
  if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 12 ]; }; then
    echo "ERROR: Python $PYVER found, but claude-lumo needs Python 3.12+ (the hash-locked" >&2
    echo "requirements need it; e.g. Debian 12's python3 is 3.11). Install Python 3.12+ and" >&2
    echo "re-run, or use a newer base image. On immutable/atomic distros (Bazzite, Silverblue)" >&2
    echo "a distrobox container with python3.12+ is the intended path:" >&2
    echo "  distrobox create -n lumo -i debian:13   # then: distrobox enter lumo" >&2
    exit 1
  fi
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

# Get the files into ~/.claude/lumo. The recommended install is a git clone there, so
# re-running this script updates it to the latest release tag.
# Channel: CLAUDE_LUMO_CHANNEL=release (default) checks out the newest v* tag; =master
# follows the master branch (for development).
CHANNEL="${CLAUDE_LUMO_CHANNEL:-release}"

if [ -d "$D/.git" ] && [ "${CLAUDE_LUMO_KEEP_CHECKOUT:-}" = "1" ]; then
  : # update or revert already checked out the release it wants: keep it (#100)
elif [ -d "$D/.git" ]; then
  # Already a clone: update it, never overwrite it from another folder.
  if [ -n "$(git -C "$D" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    echo "Note: $D has local changes; leaving it as is. Put personal changes in the lumo-local folder instead."
  elif [ "$CHANNEL" = "master" ]; then
    git -C "$D" checkout -q master 2>/dev/null \
      && git -C "$D" pull --ff-only 2>/dev/null \
      || echo "Note: could not update from master (offline); keeping current files."
  else
    git -C "$D" fetch --tags 2>/dev/null \
      && git -C "$D" checkout -q "$(git -C "$D" tag -l 'v*' --sort=-v:refname | head -n 1)" 2>/dev/null \
      || echo "Note: could not update to the latest release tag (offline); keeping current files."
  fi
elif [ -z "$SRC" ]; then
  # Piped install: clone.
  if [ -e "$D" ] && [ -n "$(ls -A "$D" 2>/dev/null)" ]; then
    echo "$D exists and is not a git clone. Move it aside, then re-run." >&2; exit 1
  fi
  git clone -q "$REPO_URL" "$D"
  if [ "$CHANNEL" != "master" ]; then
    git -C "$D" fetch --tags -q && git -C "$D" checkout -q "$(git -C "$D" tag -l 'v*' --sort=-v:refname | head -n 1)" \
      || echo "Note: could not check out the latest release tag; staying on master."
  fi
elif [ "$SRC" != "$D" ]; then
  # Run from an unzipped release or another clone: copy the files (not .git).
  mkdir -p "$D"
  (cd "$SRC" && tar --exclude=.git -cf - .) | (cd "$D" && tar -xf -)
  echo "Tip: for easy updates, install as a clone instead: git clone $REPO_URL ~/.claude/lumo"
fi

# A folder that arrived as a zip (saved on Windows, or one of the v1.0/v1.1 release zips)
# can carry CRLF line endings, which break the shebang on Linux/macOS. Strip CRs from
# every shell script we just installed so `bash\r` never happens again. (The check is
# done with grep, not cmp: minimal Fedora images ship no diffutils, and a failing cmp
# would invert the test and rewrite every file.)
for f in "$D"/*.sh "$D"/scripts/*.sh; do
  [ -f "$f" ] || continue
  if grep -q $'\r' "$f" 2>/dev/null; then
    tr -d "\r" < "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "Fixed line endings: $f"
  fi
done

if [ ! -x "$LITELLM" ]; then
  # Debian/Ubuntu split venv support into a separate python3-venv package, so a system
  # python3 can pass the prerequisite check yet fail here ("ensurepip is not available").
  # Retry once after installing the distro's venv package instead of dying on the spot.
  "$PY" -m venv "$VENV" || {
    echo "venv creation failed (often a missing python3-venv / ensurepip); trying once more after"
    echo "installing the distro's venv packages..."
    case "$PM" in
      apt-get) as_root apt-get update -qq && as_root apt-get install -y python3-venv python3-full;;
      dnf|yum) as_root "$PM" install -y python3-pip;;   # pulls in the venv machinery
      *) :;;
    esac
    "$PY" -m venv "$VENV"
  }
  # --require-hashes: every pin in requirements.txt carries a sha256 (#135), so pip refuses
  # any artifact whose hash does not match — no floating transitive dependencies.
  # Deploy-order hazard (#147): the script being piped from master can be newer than the
  # release tag whose requirements.txt is actually on disk in $D. Old tags (pre-hash-lock)
  # have no hashes, and pip aborts on --require-hashes with an unhashed file, so detect that
  # and fall back to a plain install with a warning. Requirements-format changes must ship
  # in a release tag before (or with) any install.sh that assumes them.
  if grep -q '^ *--hash=sha256:' "$D/requirements.txt"; then
    "$VP" -m pip install -q --require-hashes -r "$D/requirements.txt"
  else
    echo "WARNING: $D/requirements.txt carries no sha256 hashes (pre-#135 release tag," 2>&1
    echo "likely v1.4.8 or older). Installing without hash verification; upgrade to a"      2>&1
    echo "release tag after the hash-lock change (#135) to get verified installs."          2>&1
    "$VP" -m pip install -q -r "$D/requirements.txt"
  fi
fi
[ -s "$S/master.key" ] || "$PY" -c "import secrets;print('sk-lumo-'+secrets.token_hex(24))" > "$S/master.key"

# No per-machine edits to tracked files: claude-lumo.sh fills in this machine's paths at launch
# (into ~/.claude-lumo/settings.json), so a git clone stays clean and updates keep working.

# Put claude-lumo on PATH. In a bash shell:
printf '#!/bin/sh\nexec bash "$HOME/.claude/lumo/claude-lumo.sh" "$@"\n' > "$BIN/claude-lumo"
chmod +x "$BIN/claude-lumo"

# Windows also needs a .cmd wrapper for cmd.exe / PowerShell.
if [ "$OS" = "windows" ]; then
  printf '@echo off\r\n"C:\\Program Files\\Git\\bin\\bash.exe" "%%USERPROFILE%%\\.claude\\lumo\\claude-lumo.sh" %%*\r\n' > "$BIN/claude-lumo.cmd"
fi

# Separate data (optional): --separate-data writes a marker and runs setup so the launcher
# exports CLAUDE_CONFIG_DIR on next launch.
if [ "${1:-}" = "--separate-data" ]; then
  echo ""
  echo "Enabling optional data separation..."
  touch "$S/separate-data.on"
  if "$PY" "$D/scripts/separate_data.py" setup --config-dir "$S/data" --shared "$HOME/.claude"; then
    echo "Data separation enabled:"
    echo "  - Config dir: $S/data (transcripts, history, per-session state)"
    echo "  - Shared items linked from ~/.claude (CLAUDE.md, settings.json, skills, agents, ...)"
    echo ""
    echo "Migrate existing Lumo sessions with:"
    echo "  $PY \"$D/scripts/separate_data.py\" migrate --config-dir \"$S/data\" --dry-run  # preview"
    echo "  $PY \"$D/scripts/separate_data.py\" migrate --config-dir \"$S/data\"            # execute"
  else
    echo "Warning: setup failed; CLAUDE_CONFIG_DIR will not be set on next launch" >&2
  fi
fi

# Optional: wire the resume shim into shell profiles (see README, "Resume shim").
if [ "$EXTRA_RESUME_SHIM" = "1" ]; then
  bash "$D/scripts/resume-shim-setup.sh" install
fi

# === Optional plain-Claude opt-in: wipe-offer SessionStart hook in ~/.claude/settings.json ===
# Idempotent: only added if missing; a backup of settings.json is taken first. Skip on plain
# installs where claude-lumo is the only client (the hook is already in settings-lumo.json).
if [ "${CLAUDE_LUMO_PLAIN_OFFER:-0}" = "1" ] && [ -f "$HOME/.claude/settings.json" ]; then
  OFFER_CMD="$PY \"$D/hooks/session-start-offer.py\""
  if ! grep -q "session-start-offer.py" "$HOME/.claude/settings.json" 2>/dev/null; then
    cp "$HOME/.claude/settings.json" "$HOME/.claude/settings.json.bak-wipe-offer"
    "$PY" - "$HOME/.claude/settings.json" "$OFFER_CMD" <<'EOF'
import json, sys
path, offer_cmd = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
hooks = data.setdefault("hooks", {}).setdefault("SessionStart", [])
hooks.append({
    "matcher": "startup",
    "hooks": [{"type": "command", "command": offer_cmd, "timeout": 30}],
})
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
EOF
    echo "Added the wipe-offer SessionStart hook to ~/.claude/settings.json (backup: settings.json.bak-wipe-offer)."
  fi
fi

# Fresh version probe: statusline.py caches the git tag for an hour, so a stale
# cache would keep showing the old version (or "dev") after an update (#51).
rm -f "$S/statusline-version.cache"

echo "Installed into ~/.claude/lumo. If 'claude-lumo' is not found, add $BIN to PATH and reopen the terminal."
echo "Test: claude-lumo -p 'reply with pong'"
