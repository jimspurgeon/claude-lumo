"""SessionStart hook: hand the model claude-lumo's system prompt when the launcher could not (#77).

    python lumo-prompt.py <state dir> <part number>

claude-lumo.sh passes LUMO.md (plus the personal LUMO.md) with --append-system-prompt-file and
exports CLAUDE_LUMO_WRAPPER=1. A session that Claude Code re-hosts on its own (for example after
the agents view) keeps --settings but loses both, so the rules would silently vanish. Then this
hook prints <state dir>/prompt-parts/<part>.md, which Claude Code adds to the context. render.py
splits the prompt into parts because Claude Code moves hook output over 10,000 characters into a
file the model is not made to read. Missing part files print nothing.
"""
import os, sys


def main():
    try:
        sys.stdin.read()  # hook input; not needed, read so the writer never blocks
    except (OSError, ValueError):
        pass
    if os.environ.get("CLAUDE_LUMO_WRAPPER") == "1" or len(sys.argv) < 3:
        return 0
    path = os.path.join(sys.argv[1], "prompt-parts", f"{sys.argv[2]}.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
