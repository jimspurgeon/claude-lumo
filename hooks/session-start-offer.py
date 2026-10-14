#!/usr/bin/env python
"""SessionStart hook: offer to wipe old conversations.

Rate-limited to at most once per day via a timestamp marker. Surfaces one short line
when candidates exist: summary + offer for assistant-led assessment, or alternatives
(run script directly, manual report). Silent when nothing qualifies. Disable by
setting CLAUDE_LUMO_WIPE_OFFER=0 in your environment, or by removing the SessionStart
hook entry from settings (plain installs).

Input/output format for hooks:
  stdin: {"hookEventName": "SessionStart", ...}
  stdout: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "text"}}
"""

import json
import os
import sys
import time

MARKER_NAME = ".offer-wipe-last"
OFFER_INTERVAL_SECONDS = 86400  # at most once per day

SCRIPT_PATH = os.path.abspath(__file__)


def resolve_config_dir():
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = os.path.expanduser(env_dir) if env_dir else os.path.expanduser("~/.claude")
    return os.path.abspath(root)


def rate_limited(marker_path):
    marker = os.path.expanduser(marker_path)
    try:
        mtime = os.path.getmtime(marker)
    except OSError:
        return False
    return (time.time() - mtime) < OFFER_INTERVAL_SECONDS


def mark_called(marker_path):
    marker = os.path.expanduser(marker_path)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    try:
        with open(marker, "w") as f:
            f.write("")
        os.utime(marker, None)
    except OSError:
        pass


def load_allowlist(path):
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(line)
    except OSError:
        return None
    return entries


def collect_projects(config_dir):
    projects_root = os.path.join(config_dir, "projects")
    try:
        names = sorted(os.listdir(projects_root))
    except OSError:
        return []
    out = []
    for project in names:
        folder = os.path.join(projects_root, project)
        if not os.path.isdir(folder):
            continue
        sessions = []
        for name in os.listdir(folder):
            if name.endswith(".jsonl"):
                path = os.path.join(folder, name)
                try:
                    mtime = os.path.getmtime(path)
                    sessions.append({"id": name[:-len(".jsonl")], "mtime": mtime})
                except OSError:
                    pass
        if sessions:
            out.append({"name": project, "folder": folder, "sessions": sessions})
    return out


def session_is_active(project, config_dir, session_id, transcript_mtime):
    lock = os.path.join(config_dir, "session-env", session_id, ".lock")
    if os.path.exists(lock):
        return True
    return transcript_mtime is not None and (time.time() - transcript_mtime) < 6 * 3600


def probe_cwd(transcript_path):
    """Working directory from the head of a transcript, or None."""
    try:
        with open(transcript_path, "rb") as f:
            chunk = f.read(65536)
    except OSError:
        return None
    for raw in chunk.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("cwd"):
            return entry["cwd"]
    return None


def last_activity(project):
    """Most recent transcript mtime across the project's sessions."""
    times = [s["mtime"] for s in project["sessions"] if s["mtime"]]
    return max(times) if times else None


def detect_stale(project, config_dir, days, allowlist):
    # No sessions left (for example only memory/, which a wipe keeps): nothing to offer.
    if not project["sessions"]:
        return False
    if any(session_is_active(project, config_dir, s["id"], s["mtime"]) for s in project["sessions"]):
        return False

    if allowlist is not None:
        for s in project["sessions"]:
            cwd = probe_cwd(os.path.join(project["folder"], s["id"] + ".jsonl"))
            if cwd:
                norm_cwd = os.path.normcase(os.path.normpath(os.path.abspath(cwd)))
                for entry in allowlist:
                    if os.path.normcase(os.path.normpath(os.path.abspath(entry))) == norm_cwd:
                        return False
        return True

    activity = last_activity(project)
    if activity is not None and (time.time() - activity) / 86400 >= days:
        return True

    # Project working folder gone: every session's cwd no longer exists.
    cwds = []
    for s in project["sessions"]:
        cwd = probe_cwd(os.path.join(project["folder"], s["id"] + ".jsonl"))
        if cwd:
            cwds.append(cwd)
    if cwds and not any(os.path.isdir(c) for c in cwds):
        return True

    return False


def main():
    try:
        input_data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, OSError):
        input_data = {}

    # Claude Code sends no hook configuration on stdin, so the off switch is
    # an environment variable the hook reads itself.
    if os.environ.get("CLAUDE_LUMO_WIPE_OFFER") == "0":
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
        print(json.dumps(output))
        return

    config_dir = resolve_config_dir()
    state_dir = os.environ.get("CLAUDE_LUMO_STATE_DIR") or os.path.expanduser("~/.claude-lumo")
    marker = os.path.join(state_dir, MARKER_NAME)

    if rate_limited(marker):
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
        print(json.dumps(output))
        return

    allowlist = load_allowlist(os.path.join(config_dir, "allowlist.txt"))
    allowlist_path = os.environ.get("WIPE_ALLOWLIST")
    if allowlist_path:
        allowlist = load_allowlist(allowlist_path)

    projects = collect_projects(config_dir)
    stale_count = 0
    stale_projects = set()

    for project in projects:
        if detect_stale(project, config_dir, 30, allowlist):
            stale_count += len(project["sessions"])
            stale_projects.add(project["name"])

    if stale_count == 0:
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
        print(json.dumps(output))
        return

    mark_called(marker)
    message = (f"{stale_count} old conversation(s) in {len(stale_projects)} project(s) could be wiped. "
              "Offer to go through them together: run the privacy audit, assess each finding beyond the script's "
              "classification, recommend what to remove, then delete only what the user approves, "
              "one project or session at a time (`claude-lumo wipe --project <name> --yes`). Alternatives: "
              "`claude-lumo wipe` in a terminal shows the findings and a menu, `--mode manual` prints a do-it-yourself report.")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message
        }
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
