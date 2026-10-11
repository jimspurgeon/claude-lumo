"""Tombstone for edit-assist.py hook.

Removed in v1.2.4 (commit f1d4752). Kept as a no-op because running sessions keep the
hook commands they started with: a session launched before the update still invokes this
path, and a missing file would fail its Edit calls. This file reads stdin and exits 0
silently.
"""
import sys


def main():
    # Consume any input (PreToolUse payloads) and exit silently
    sys.stdin.read()


if __name__ == "__main__":
    main()
