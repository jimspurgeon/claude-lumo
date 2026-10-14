"""SessionStart hook: hand the model claude-lumo's system prompt (#77).

    python lumo-prompt.py <state dir> <part number>

This hook is the only way the prompt (LUMO.md plus the personal LUMO.md) reaches a session.
claude-lumo.sh used to pass it with --append-system-prompt-file, but a session Claude Code
re-hosts on its own (for example after the agents view) keeps --settings and drops every other
flag, so the rules silently vanished after its next /compact. An environment marker cannot tell
the two cases apart either, since a re-hosting process can inherit the launcher's environment.
The hook runs on startup, clear and compact; a resumed or forked session already carries its
output in the transcript. It prints <state dir>/prompt-parts/<part>.md, which Claude Code adds to
the context. render.py splits the prompt into parts because Claude Code moves hook output over
10,000 characters into a file the model is not made to read. Missing part files print nothing.
"""
import os, sys


def main():
    try:
        sys.stdin.read()  # hook input; not needed, read so the writer never blocks
    except (OSError, ValueError):
        pass
    if len(sys.argv) < 3:
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
