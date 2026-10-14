"""SessionStart hook: hand the model claude-lumo's system prompt (#77).

    python lumo-prompt.py <state dir> <part number>

This hook is the only way the prompt (LUMO.md plus the personal LUMO.md) reaches a session.
claude-lumo.sh used to pass it with --append-system-prompt-file, but a session Claude Code
re-hosts on its own (for example after the agents view) keeps --settings and drops every other
flag, so the rules silently vanished after its next /compact. An environment marker cannot tell
the two cases apart either, since a re-hosting process can inherit the launcher's environment.
The hook runs on startup, clear and compact. On resume and fork it checks the transcript first
and stays silent when the context Claude Code replays already holds this hook's output (a
hook_success attachment after the last compact_boundary), so it is never given twice. Anything
else prints it: text before a compaction is no longer in the context, a session that merely read
LUMO.md or attached it as a file has not been given it as instructions, and a session started
before #77 (prompt_snapshot) had it from a launcher flag that a resume no longer passes.
It prints <state dir>/prompt-parts/<part>.md, which Claude Code adds to the context. render.py
splits the prompt into parts because Claude Code moves hook output over 10,000 characters into
a file the model is not made to read. Missing part files print nothing.
"""
import json, os, sys

MARKER = "claude-lumo system prompt, part "
NEEDLES = (MARKER, json.dumps(MARKER)[1:-1])


def already_has_prompt(transcript):
    """True when a hook_success attachment after the last compact_boundary holds this hook's
    output. A compaction drops everything before it from the context, so it resets the match."""
    found = False
    try:
        with open(transcript, "rb") as f:
            for raw in f:
                if b'"attachment"' not in raw and b'compact_boundary' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
                    found = False
                    continue
                attachment = record.get("attachment")
                if record.get("type") != "attachment" or not isinstance(attachment, dict):
                    continue
                if attachment.get("type") != "hook_success":
                    continue
                body = json.dumps(attachment, ensure_ascii=False)
                if any(n in body for n in NEEDLES):
                    found = True
    except OSError:
        return False
    return found


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    if len(sys.argv) < 3:
        return 0
    path = os.path.join(sys.argv[1], "prompt-parts", f"{sys.argv[2]}.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    transcript = event.get("transcript_path")
    resumed = event.get("source") in ("resume", "fork")
    if resumed and transcript and already_has_prompt(transcript):
        return 0
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
