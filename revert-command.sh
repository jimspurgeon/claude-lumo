#!/bin/bash
# revert-command.sh: roll the claude-lumo installation at ~/.claude/lumo back to a previous release tag.
# Called by `claude-lumo revert <version>` or `claude-lumo revert last` (claude-lumo.sh routes to it).
set -e
D="${CLAUDE_LUMO_DIR:-$HOME/.claude/lumo}"; S="${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}"

PY="python"
command -v "$PY" >/dev/null 2>&1 || PY="python3"

# Guard: not a git clone
if [ ! -d "$D/.git" ]; then
  echo "claude-lumo: $D is not a git clone; re-run the installer instead."
  exit 1
fi

# Guard: local changes
if [ -n "$(git -C "$D" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "$D has local changes; refusing to revert so nothing is lost. Put personal changes in ~/.claude/lumo-local (see README, Personal additions)."
  exit 1
fi

# Guard: must be at a release tag
CURRENT="$(git -C "$D" describe --tags --exact-match HEAD 2>/dev/null)" || {
  echo "claude-lumo: $D is not at a release tag (development install?); use the installer to revert."
  exit 1
}
if ! [[ "$CURRENT" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "claude-lumo: $D is not at a release tag (development install?); use the installer to revert."
  exit 1
fi

# Fetch tags
if ! git -C "$D" fetch --tags -q origin; then
  echo "claude-lumo: could not reach the remote (offline?); nothing changed."
  exit 1
fi

# Resolve version argument
VERSION="$1"
TARGET=""

case "$VERSION" in
  last)
    # Tag immediately preceding the installed one in version order (list is descending;
    # getline returning 0 at EOF means there is no older release)
    TARGET="$(git -C "$D" tag -l 'v*' --sort=-v:refname | awk -v c="$CURRENT" '$0==c{if((getline)>0) print; exit}')"
    if [ -z "$TARGET" ]; then
      echo "claude-lumo: no release before $CURRENT found; nothing to revert to."
      exit 1
    fi
    ;;
  "")
    echo "Usage: claude-lumo revert <version> (e.g., v1.2.0 or 1.2.0) or claude-lumo revert last"
    exit 1
    ;;
  *)
    # Accept v-prefixed or bare version (e.g., v1.2.0 or 1.2.0)
    if [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      TARGET="$VERSION"
    elif [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      TARGET="v$VERSION"
    else
      echo "claude-lumo: invalid version '$VERSION'; use a release tag like v1.2.0 or 1.2.0, or 'last'."
      exit 1
    fi
    # Validate target exists
    if ! git -C "$D" rev-parse "$TARGET^{commit}" >/dev/null 2>&1; then
      echo "claude-lumo: version '$TARGET' does not exist; run 'git -C $D tag -l v*' to list releases."
      exit 1
    fi
    ;;
esac

# Already at target (no-op)
if [ "$CURRENT" = "$TARGET" ]; then
  echo "claude-lumo: already on $TARGET; nothing to revert."
  exit 0
fi

# Warn that this is a downgrade
echo "Reverting $CURRENT -> $TARGET. Note: this is a downgrade."
echo "To return to the latest release, run: claude-lumo update"

# Show what will change
echo "Changes:"
git -C "$D" log --pretty=format:'  %s' "$TARGET..$CURRENT"

# Checkout
if ! git -C "$D" checkout -q "$TARGET"; then
  echo "claude-lumo: checkout failed; nothing changed."
  exit 1
fi

# Clear pending update notice
rm -f "$S/update-notice.txt"

# Clear statusline version cache
rm -f "$S/statusline-version.cache"

# Re-run installer (idempotent; skipped when absent, e.g. trimmed test clones)
echo ""
if [ -f "$D/install.sh" ]; then
  echo "Re-running the installer so new files and settings take effect..."
  # The target's installer checks out the newest tag when it is older than #100, which undid the
  # revert. Newer installers keep the checkout with CLAUDE_LUMO_KEEP_CHECKOUT=1; an older one's
  # `git fetch` fails with GIT_ALLOW_PROTOCOL=none, and its checkout is chained after that fetch.
  CLAUDE_LUMO_KEEP_CHECKOUT=1 GIT_ALLOW_PROTOCOL=none bash "$D/install.sh" \
    || echo "Warning: installer failed, but checkout to $TARGET already happened."
else
  echo "Skipping the installer rerun (install.sh not present in $D)."
fi
# Last guard: whatever the installer did, end on the target. An installer that moved the checkout
# installed from the wrong tree, so check the target out again and rerun its installer in place.
at_target() { [ "$(git -C "$D" rev-parse HEAD)" = "$(git -C "$D" rev-parse "$TARGET^{commit}")" ]; }
back_to_target() {
  git -C "$D" checkout -q "$TARGET" \
    || { echo "claude-lumo: could not return to $TARGET; run: claude-lumo revert $TARGET" >&2; exit 1; }
}
if ! at_target; then
  back_to_target
  echo "Note: the installer moved the checkout; $TARGET is checked out again and reinstalled."
  CLAUDE_LUMO_KEEP_CHECKOUT=1 GIT_ALLOW_PROTOCOL=none bash "$D/install.sh" >/dev/null 2>&1 || true
  if ! at_target; then
    back_to_target
    echo "Warning: $TARGET's installer keeps moving the checkout; some installed files may come from a newer release."
  fi
fi

echo "Reverted to $TARGET. Open claude-lumo sessions keep the old files until they are restarted."

# Offer to restart sessions
if [ -f "$D/session-restart.py" ] && [ -t 1 ]; then
  "$PY" "$D/session-restart.py" --new-version "$TARGET"
fi
