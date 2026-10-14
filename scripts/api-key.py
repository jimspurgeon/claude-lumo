"""apiKeyHelper for claude-lumo (#77): print the local LiteLLM master key.

    python api-key.py <state dir>

claude-lumo.sh also exports the key as ANTHROPIC_AUTH_TOKEN, which wins when set. A session
Claude Code re-hosts on its own keeps --settings but not that export; without this helper its
requests reach LiteLLM with the wrong credential and fail with "No connected db" (#60).
"""
import os, sys

with open(os.path.join(sys.argv[1], "master.key"), encoding="utf-8") as f:
    print(f.read().strip())
