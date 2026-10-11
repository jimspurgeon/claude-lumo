#!/usr/bin/env python3
"""Check that all hook files referenced in settings-lumo.json history still exist.

Run this before each release to ensure no hook has been deleted without a tombstone.
Fails if any hook file that was ever referenced in git history is now missing.
"""
import subprocess
import sys
from pathlib import Path


def main():
    repo_root = Path(__file__).parent.parent
    hooks_dir = repo_root / "hooks"

    # Get all hook file paths ever referenced in settings-lumo.json
    result = subprocess.run(
        ["git", "log", "-p", "--all", "--", "settings-lumo.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    # Extract hook paths like hooks/foo.py
    import re
    hook_pattern = re.compile(r"hooks/([A-Za-z0-9_.-]+\.py)")
    hook_files = set(hook_pattern.findall(result.stdout))

    if not hook_files:
        print("No hook files found in settings-lumo.json history")
        return 0

    errors = []
    for hook in sorted(hook_files):
        hook_path = hooks_dir / hook
        if not hook_path.exists():
            errors.append(f"MISSING: {hook}")
        else:
            print(f"OK: {hook}")

    if errors:
        print("\nHook tombstone check FAILED:")
        for e in errors:
            print(f"  {e}")
        print("\nFix: create a tombstone file for each missing hook")
        print("See issue #8 for guidance on tombstone contents.")
        return 1

    print(f"\nAll {len(hook_files)} hook file(s) present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
