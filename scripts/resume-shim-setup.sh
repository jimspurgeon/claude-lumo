#!/bin/bash
# resume-shim-setup.sh: add or remove the resume shim lines from shell profiles.
# Usage: bash resume-shim-setup.sh install|uninstall
# Wired in by install.sh --resume-shim (opt-in) and by uninstall.sh.
# Idempotent: the profile gains exactly one marked line per file, and uninstall removes
# exactly those lines. On Windows, PowerShell profiles are created if missing; bash/zsh
# rc files are only touched if they already exist.
set -u
D="$HOME/.claude/lumo"
MARKER='claude-lumo resume shim'
MODE="${1:-}"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) OS="windows";;
  Darwin)               OS="macos";;
  *)                    OS="linux";;
esac

add_line() { # add_line <file> <line> <create-missing?>  — append once, after a marker comment
  local f="$1" line="$2" create="${3:-no}"
  if [ ! -f "$f" ]; then
    [ "$create" = "yes" ] && mkdir -p "$(dirname "$f")" && touch "$f" || return 0
  fi
  grep -qF "$MARKER" "$f" && { echo "  $f already has the resume shim."; return 0; }
  printf '\n# %s (opt-in; uninstall with claude-lumo)\n%s\n' "$MARKER" "$line" >> "$f"
  echo "  $f updated."
}

del_lines() { # del_lines <file>  — remove exactly what add_line appended
  local f="$1"
  [ -f "$f" ] || return 0
  grep -qF "$MARKER" "$f" || return 0
  local n
  n=$(grep -c '' "$f")
  # add_line appends three lines: a blank separator, the marker comment, the source line.
  # If the file still ends with exactly that block, cut it off (byte-identical restore).
  if [ "$n" -ge 3 ] && [ -z "$(tail -n 3 "$f" | sed -n 1p)" ] \
     && tail -n 3 "$f" | sed -n 2p | grep -qF "$MARKER" \
     && tail -n 3 "$f" | sed -n 3p | grep -qF 'resume-shim'; then
    head -n $((n - 3)) "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  else
    # Older or hand-edited block: filter the lines out instead.
    grep -vF "$MARKER" "$f" | grep -vF '.claude/lumo/scripts/resume-shim' > "$f.tmp" \
      && mv "$f.tmp" "$f" \
      || { rm -f "$f.tmp"; return 0; }
  fi
  # If nothing is left, the file was one we created (only PowerShell profiles are created
  # when missing); remove it and its folder so uninstall leaves nothing behind.
  if [ ! -s "$f" ]; then
    rm -f "$f"
    rmdir "$(dirname "$f")" 2>/dev/null || true
  fi
  echo "  $f cleaned."
}

case "$MODE" in
  install)
    echo "Resume shim: wiring profiles (sources $D/scripts/, the repo copy)..."
    # bash / zsh: only touch rc files that already exist.
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      add_line "$rc" '[ -f "$HOME/.claude/lumo/scripts/resume-shim.sh" ] && . "$HOME/.claude/lumo/scripts/resume-shim.sh"'
    done
    # Windows: also the PowerShell profiles (PS7 and Windows PowerShell 5.1). They are
    # opt-in by convention, so create them if missing (a brand-new install works too).
    if [ "$OS" = "windows" ]; then
      for pf in "$HOME/Documents/PowerShell/Microsoft.PowerShell_profile.ps1" \
                "$HOME/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1"; do
        add_line "$pf" 'if (Test-Path "$HOME\.claude\lumo\scripts\resume-shim.ps1") { . "$HOME\.claude\lumo\scripts\resume-shim.ps1" }' yes
      done
    fi
    echo "Resume shim installed. Restart the shell (or reload the profile) to use it."
    ;;
  uninstall)
    echo "Resume shim: removing profile lines..."
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" \
              "$HOME/Documents/PowerShell/Microsoft.PowerShell_profile.ps1" \
              "$HOME/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1"; do
      del_lines "$rc"
    done
    echo "Resume shim removed."
    ;;
  *)
    echo "usage: bash resume-shim-setup.sh install|uninstall" >&2
    exit 2
    ;;
esac
