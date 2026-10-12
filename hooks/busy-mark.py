"""Busy-flag registry for Claude Code hook events.

Reads JSON hook input from stdin and maintains a registry file per session at
<state>/sessions/<session_id>.json where <state> is CLAUDE_LUMO_STATE_DIR or ~/.claude-lumo.

Fields: session_id, cwd, pid (int from CLAUDE_PID, null if unset), busy (bool), updated (ISO timestamp).

Events handled:
- SessionStart: write with busy=false
- UserPromptSubmit: write with busy=true
- Stop: write with busy=false
- SessionEnd: delete the file

Atomic writes via temp + os.replace. All failures exit 0 silently; this hook must never disturb a session.

This registry lets `claude-lumo update` find running sessions and tell busy from idle; idle
detection is this file's busy flag, testable by editing the JSON directly.
"""
import json
import os
import sys
from datetime import datetime, timezone


def main():
    # Read all stdin
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    session_id = data.get("session_id")
    cwd = data.get("cwd")
    event = data.get("hook_event_name")

    if not session_id or not event:
        sys.exit(0)

    # State directory
    state = os.environ.get("CLAUDE_LUMO_STATE_DIR") or os.path.expanduser("~/.claude-lumo")
    sessions_dir = os.path.join(state, "sessions")
    session_file = os.path.join(sessions_dir, f"{session_id}.json")

    # PID from environment
    pid_str = os.environ.get("CLAUDE_PID")
    pid = int(pid_str) if pid_str and pid_str.isdigit() else None

    # Timestamp
    updated = datetime.now(timezone.utc).isoformat()

    # Build record
    record = {"session_id": session_id, "cwd": cwd, "pid": pid, "updated": updated}

    if event == "SessionStart":
        record["busy"] = False
        _write_atomic(session_file, record)
    elif event == "UserPromptSubmit":
        record["busy"] = True
        _write_atomic(session_file, record)
    elif event == "Stop":
        record["busy"] = False
        _write_atomic(session_file, record)
    elif event == "SessionEnd":
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except OSError:
                pass
    # Unknown events: do nothing


def _write_atomic(path: str, record: dict):
    """Write JSON atomically via temp + rename."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
