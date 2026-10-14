#!/bin/bash
# update-command.sh: bring the claude-lumo install at ~/.claude/lumo to the latest release tag.
# Called by `claude-lumo update` (claude-lumo.sh routes to it).
set -e
D="${CLAUDE_LUMO_DIR:-$HOME/.claude/lumo}"; S="${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}"

PY="python"
command -v "$PY" >/dev/null 2>&1 || PY="python3"

# Guard: not a git clone
if [ ! -d "$D/.git" ]; then
  echo "claude-lumo: $D is not a git clone; re-run the installer instead."
  exit 1
fi

# Guard: local edits
if [ -n "$(git -C "$D" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "$D has local changes; refusing to update so nothing is lost. Put personal changes in ~/.claude/lumo-local (see README, Personal additions)."
  exit 1
fi

# Guard: must be at a release tag
INSTALLED="$(git -C "$D" describe --tags --exact-match HEAD 2>/dev/null)" || {
  echo "claude-lumo: $D is not checked out at a release tag (development install?); use the installer to update."
  exit 1
}
if ! [[ "$INSTALLED" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "claude-lumo: $D is not checked out at a release tag (development install?); use the installer to update."
  exit 1
fi

# Fetch tags
if ! git -C "$D" fetch --tags -q origin; then
  echo "claude-lumo: could not reach the remote (offline?); nothing changed."
  exit 1
fi

# Find latest tag
LATEST="$(git -C "$D" tag -l 'v*' --sort=-v:refname | head -n 1)"
if [ -z "$LATEST" ]; then
  echo "claude-lumo: no release tags found; nothing changed."
  exit 1
fi

# Already latest
if [ "$LATEST" = "$INSTALLED" ]; then
  echo "claude-lumo: already on the latest release ($INSTALLED)."
  exit 0
fi

# Show changes
echo "Updating $INSTALLED -> $LATEST. Changes:"
git -C "$D" log --pretty=format:'  %s' "$INSTALLED..$LATEST"

# Checkout
if ! git -C "$D" checkout -q "$LATEST"; then
  echo "claude-lumo: checkout failed; nothing changed."
  exit 1
fi

# Clear the pending notice and the status line's cached version right after the checkout, before
# the slow installer rerun: statusline.py caches the git tag for an hour, so a cache left behind by
# an update interrupted during the installer kept showing the old version (#51, #84).
rm -f "$S/update-notice.txt" "$S/statusline-version.cache"

# Re-run installer (idempotent; skipped when absent, e.g. trimmed test clones)
echo ""
if [ -f "$D/install.sh" ]; then
  echo "Re-running the installer so new files and settings take effect..."
  CLAUDE_LUMO_KEEP_CHECKOUT=1 bash "$D/install.sh" \
    || echo "Warning: installer failed, but checkout to $LATEST already happened."
else
  echo "Skipping the installer rerun (install.sh not present in $D)."
fi

echo "Updated to $LATEST. Open claude-lumo sessions keep the old files until they are restarted."

# Offer to restart sessions
if [ -f "$D/session-restart.py" ] && [ -t 1 ]; then
  "$PY" "$D/session-restart.py" --new-version "$LATEST"
fi
