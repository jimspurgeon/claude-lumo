"""Tombstone for edit-assist.py hook.

Removed in v1.2.4 (commit f1d4752). Kept as a no-op so that running sessions
started before the update don't fail when they re-read the PreToolUse hook
configuration. This file reads stdin and exits 0 silently.
"""
import sys


def main():
    # Consume any input (PreToolUse payloads) and exit silently
    sys.stdin.read()


if __name__ == "__main__":
    main()
