"""apiKeyHelper for claude-lumo (#77): print the local LiteLLM master key.

    python api-key.py <state dir>

This is the only way the key reaches Claude Code: claude-lumo.sh clears ANTHROPIC_AUTH_TOKEN and
ANTHROPIC_API_KEY, which would win over it (#86). A session Claude Code re-hosts on its own keeps
--settings but no exports, so it authenticates through this helper too; without it, requests
reach LiteLLM with the wrong credential and fail with "No connected db" (#60).
"""
import os, sys

with open(os.path.join(sys.argv[1], "master.key"), encoding="utf-8") as f:
    print(f.read().strip())
