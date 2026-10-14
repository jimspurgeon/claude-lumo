"""Offer to restart running claude-lumo sessions after an update.

Called by update-command.sh as: session-restart.py --new-version v1.3.0 (only when stdout
is a tty). It reads the session registry written by hooks/busy-mark.py: one JSON file per
session at <state>/sessions/<session_id>.json with fields session_id, cwd, pid, busy,
updated. <state> is CLAUDE_LUMO_STATE_DIR or ~/.claude-lumo.

Per session the user chooses on the terminal that ran `claude-lumo update`:
- w: wait until the session is idle (its busy flag becomes false), then restart it.
- i: interrupt it now, then restart it.
- s (default, also plain Enter): skip.

Restart means: stop the session (/exit), start `claude-lumo --resume <session id>` in the
same terminal and folder, then, if it was interrupted mid-task, send a one-line note that
it was restarted for an update followed by "continue".

Sessions whose registry entry is older than 7 days are treated as stale and pruned.
The session that ran the update (env CLAUDE_CODE_SESSION_ID) is never restarted
automatically; its resume command is printed at the end instead.

Controlling another terminal needs a controller: Orca (orca terminal list/send; the
registry cwd is matched against each terminal's worktreePath), or tmux (matching the
pane's current path). Without either, the exact per-session resume command is printed.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

STALE_AFTER = timedelta(days=7)


def state_dir():
    return os.environ.get("CLAUDE_LUMO_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude-lumo")


def short_sid(session_id):
    return session_id[:8]


def normalize_path(path):
    """Path as a comparable string (realpath, lowercase on Windows)."""
    try:
        resolved = os.path.realpath(path or "")
    except OSError:
        return path or ""
    return resolved.lower() if os.name == "nt" else resolved


def sessions_from_registry(sdir, exclude_self=None):
    """Load <state>/sessions/*.json into session dicts (session_id, cwd, pid, busy, path).

    Entries with an updated timestamp older than STALE_AFTER are pruned from disk and
    skipped. Unreadable files are skipped. The self session is excluded from the list.
    """
    sessions = []
    for path in sorted(glob.glob(os.path.join(sdir, "sessions", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            updated = datetime.fromisoformat(data["updated"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # Compare against a now() with the same awareness as the stored timestamp.
        if datetime.now(updated.tzinfo) - updated > STALE_AFTER:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        session_id = data.get("session_id")
        if not session_id or session_id == exclude_self:
            continue
        sessions.append({
            "session_id": session_id,
            "cwd": data.get("cwd", ""),
            "pid": data.get("pid"),
            "busy": bool(data.get("busy")),
            "path": path,
        })
    return sessions


def choose_controller(override=None):
    """Pick the controller: orca (orca on PATH and ORCA_WORKTREE_ID set), tmux, or none."""
    if override and override in ("orca", "tmux", "none"):
        return override
    orca_cmd = os.environ.get("CLAUDE_LUMO_ORCA_CMD", "orca")
    if shutil.which(orca_cmd) and os.environ.get("ORCA_WORKTREE_ID"):
        return "orca"
    if os.environ.get("TMUX"):
        return "tmux"
    return "none"


def match_orca_terminal(terminals, cwd):
    """Terminal handle whose worktreePath matches cwd, or None."""
    target = normalize_path(cwd)
    for term in terminals:
        if normalize_path(term.get("worktreePath")) == target:
            return term.get("handle")
    return None


def list_orca_terminals(orca_cmd):
    """Orca terminals as a list of dicts (handle, worktreePath, ...). [] on any failure."""
    try:
        result = subprocess.run(
            [orca_cmd, "terminal", "list", "--json"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout).get("result", {}).get("terminals", [])
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return []


def match_tmux_pane(panes, cwd):
    """Pane id whose current path matches cwd, or None."""
    target = normalize_path(cwd)
    for pane_id, pane_path in panes:
        if normalize_path(pane_path) == target:
            return pane_id
    return None


def list_tmux_panes():
    """tmux panes as (pane_id, current_path) tuples. [] on any failure."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return []
        return [tuple(line.split("\t", 1)) for line in result.stdout.splitlines() if "\t" in line]
    except (subprocess.TimeoutExpired, OSError):
        return []


def wait_until_idle(session_path, poll_seconds=5, timeout_seconds=1800):
    """Poll the registry file until busy is false. True when idle, False on timeout."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with open(session_path, encoding="utf-8") as f:
                if not json.load(f).get("busy"):
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(poll_seconds)
    return False


def send_orca_command(orca_cmd, handle, text, enter=False, interrupt=False):
    """Send input to an Orca terminal. True on success."""
    cmd = [orca_cmd, "terminal", "send", "--terminal", handle]
    if interrupt:
        cmd.append("--interrupt")
    else:
        cmd.extend(["--text", text])
    if enter:
        cmd.append("--enter")
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def send_tmux_command(pane_id, text, enter=False, interrupt=False):
    """Send input to a tmux pane. True on success."""
    try:
        if interrupt:
            cmd = ["tmux", "send-keys", "-t", pane_id, "C-c"]
        else:
            cmd = ["tmux", "send-keys", "-t", pane_id, "-l", text]
        ok = subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
        if enter and not interrupt:
            ok = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"],
                                capture_output=True, timeout=20).returncode == 0 and ok
        return ok
    except (subprocess.TimeoutExpired, OSError):
        return False


def restart_in_terminal(send, session_id, choice, registry_path):
    """Restart one session through a controller's send(text, enter, interrupt).

    choice "w": wait until idle first. choice "i": interrupt now. Both then exit the old
    session, relaunch with `claude-lumo --resume` and, if interrupted mid-task, send a
    note and "continue". Returns (ok, message).
    """
    interrupted = choice == "i"
    if choice == "w" and not wait_until_idle(registry_path):
        return False, "timed out waiting for the session to go idle"
    if interrupted and not send(None, interrupt=True):
        return False, "could not interrupt the terminal"
    time.sleep(1 if interrupted else 0)
    if not send("/exit", enter=True):
        return False, "could not type /exit into the terminal"
    time.sleep(2)
    if not send(f"claude-lumo --resume {session_id}", enter=True):
        return False, "could not relaunch the session"
    time.sleep(3)
    if interrupted:
        send("(restarted for a claude-lumo update) continue", enter=True)
    return True, "restarted"


def ask_choice(question):
    """The user's answer to `question`, or 's' (skip) when there is no terminal to ask: stdin is
    not a tty, is None under pythonw (#39), or closes before an answer."""
    try:
        if not sys.stdin.isatty():
            return "s"
        return input(question).strip().lower() or "s"
    except (AttributeError, EOFError, OSError):
        return "s"


def main():
    parser = argparse.ArgumentParser(description="Offer to restart running claude-lumo sessions.")
    parser.add_argument("--new-version", required=True, help="new version tag, e.g. v1.3.0")
    parser.add_argument("--controller", choices=("auto", "orca", "tmux", "none"), default="auto")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen instead of acting")
    args = parser.parse_args()

    sdir = state_dir()
    self_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    sessions = sessions_from_registry(sdir, exclude_self=self_sid)
    controller = choose_controller(args.controller)

    if not sessions:
        print("No other running claude-lumo sessions found.")
    else:
        orca_cmd = os.environ.get("CLAUDE_LUMO_ORCA_CMD", "orca")
        if controller == "orca":
            terminals = list_orca_terminals(orca_cmd)
            matches = {s["session_id"]: match_orca_terminal(terminals, s["cwd"]) for s in sessions}
        elif controller == "tmux":
            panes = list_tmux_panes()
            matches = {s["session_id"]: match_tmux_pane(panes, s["cwd"]) for s in sessions}
        else:
            matches = {}

        for s in sessions:
            target = matches.get(s["session_id"])
            state = "busy" if s["busy"] else "idle"
            where = target if target else f"no {controller} terminal match"
            print(f"Session {short_sid(s['session_id'])} in {s['cwd']} ({state}, {where})")
        print()

        for s in sessions:
            target = matches.get(s["session_id"])
            if not target:
                print(f"  Session {short_sid(s['session_id'])}: I cannot restart it myself (no {controller} "
                      f"match); run `claude-lumo --resume {s['session_id']}` in its terminal.")
                continue
            if args.dry_run:
                print(f"  [dry run] would restart {short_sid(s['session_id'])} via {controller} {target}")
                continue
            choice = ask_choice(f"  Restart {short_sid(s['session_id'])}? [w]ait until idle, "
                                f"[i]nterrupt now, [s]kip (default s): ")
            if choice not in ("w", "i", "s"):
                print("  Unknown choice; skipping.")
                continue
            if choice == "s":
                continue

            if controller == "orca":
                send = lambda text, enter=False, interrupt=False: send_orca_command(
                    orca_cmd, target, text, enter, interrupt)
            else:
                send = lambda text, enter=False, interrupt=False: send_tmux_command(
                    target, text, enter, interrupt)
            ok, msg = restart_in_terminal(send, s["session_id"], choice, s["path"])
            print(f"  {'Restarted' if ok else 'Could not restart'} {short_sid(s['session_id'])}: {msg}.")

    if self_sid:
        print(f"Finally, restart the session that ran the update (this one) last: "
              f"claude-lumo --resume {self_sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
