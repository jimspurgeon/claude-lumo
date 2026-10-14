"""SessionStart hook: hand the model claude-lumo's system prompt (#77).

    python lumo-prompt.py <state dir> <part number>

This hook is the only way the prompt (LUMO.md plus the personal LUMO.md) reaches a session.
claude-lumo.sh used to pass it with --append-system-prompt-file, but a session Claude Code
re-hosts on its own (for example after the agents view) keeps --settings and drops every other
flag, so the rules silently vanished after its next /compact. An environment marker cannot tell
the two cases apart either, since a re-hosting process can inherit the launcher's environment.
The hook runs on startup, clear and compact. On resume and fork it checks the transcript first
and stays silent when the prompt is already there, so it is never given twice. Only two kinds of
record count: this hook's own output (a hook_success attachment) and the prompt_snapshot of a
session started before this change. A session that merely read LUMO.md or this repo's source,
or attached it as a file, has the same text elsewhere and must still get the prompt.
It prints <state dir>/prompt-parts/<part>.md, which Claude Code adds to the context. render.py
splits the prompt into parts because Claude Code moves hook output over 10,000 characters into
a file the model is not made to read. Missing part files print nothing.
"""
import json, os, sys

MARKER = "claude-lumo system prompt, part "
# Attachment types that carry the prompt itself (checked against real transcripts, 2026-10-13).
PROMPT_ATTACHMENTS = ("hook_success", "prompt_snapshot")


def first_line(text):
    """The first non-empty line of the prompt itself (after the part header)."""
    body = text.split("\n\n", 1)[-1]
    return next((l.strip() for l in body.splitlines() if l.strip()), "")


def already_has_prompt(transcript, text):
    """True when a prompt-carrying attachment (PROMPT_ATTACHMENTS) holds this hook's output or the
    prompt's own first line (a session started before #77 carries it as a prompt_snapshot)."""
    needles = [MARKER]
    line = first_line(text)
    if line:
        needles.append(line)
    needles += [json.dumps(n, ensure_ascii=False)[1:-1] for n in needles]
    try:
        with open(transcript, "rb") as f:
            for raw in f:
                if b'"attachment"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "attachment":
                    continue
                attachment = record.get("attachment")
                if not isinstance(attachment, dict):
                    continue
                if attachment.get("type") not in PROMPT_ATTACHMENTS:
                    continue
                body = json.dumps(attachment, ensure_ascii=False)
                if any(n in body for n in needles):
                    return True
    except OSError:
        return False
    return False


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
    if resumed and transcript and already_has_prompt(transcript, text):
        return 0
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
