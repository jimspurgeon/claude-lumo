#!/usr/bin/env python3
"""Check that all hook files referenced in settings-lumo.json history still exist.

Run this before each release (or via the opt-in pre-push hook) to ensure no hook
has been deleted without a tombstone. Scans only the current branch's history, so
a hook referenced on an unmerged branch cannot fail master forever.
"""
import re
import subprocess
import sys
from pathlib import Path

SETTINGS_FILE = "settings-lumo.json"
HOOK_RE = re.compile(r"hooks/([A-Za-z0-9_.-]+\.py)")


def main():
    repo_root = Path(__file__).resolve().parent.parent
    hooks_dir = repo_root / "hooks"

    result = subprocess.run(
        ["git", "log", "-p", "--", SETTINGS_FILE],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"git log failed (exit {result.returncode}):")
        print(result.stderr.strip() or result.stdout.strip())
        return 1

    hook_files = set(HOOK_RE.findall(result.stdout))
    if not hook_files:
        print(f"No hook files found in {SETTINGS_FILE} history; expected at least one. "
              "Run this from a claude-lumo checkout.")
        return 1

    errors = []
    for hook in sorted(hook_files):
        if (hooks_dir / hook).exists():
            print(f"OK: {hook}")
        else:
            errors.append(f"MISSING: {hook}")

    if errors:
        print("\nHook tombstone check FAILED:")
        for e in errors:
            print(f"  {e}")
        print("\nFix: create a tombstone file (reads stdin, exits 0) for each missing "
              "hook. See issue #8.")
        return 1

    print(f"\nAll {len(hook_files)} hook file(s) present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
