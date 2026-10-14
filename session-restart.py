"""Offer to restart running claude-lumo sessions after an update.

Called by update-command.sh as: session-restart.py --new-version v1.3.0 (only when stdout
is a tty). It reads the session registry written by hooks/busy-mark.py: one JSON file per
session at <state>/sessions/<session_id>.json with fields session_id, cwd, pid, busy,
updated. <state> is CLAUDE_LUMO_STATE_DIR or ~/.claude-lumo.

The user first makes one choice for all restartable sessions (#118), on the terminal that ran
`claude-lumo update`:
- w: wait until each session is idle (its busy flag becomes false), then restart it.
- i: interrupt every session now, then restart it.
- o: choose one by one, with the per-session prompt below.
- s (default, also plain Enter): skip all.

The per-session prompt offers w (wait until idle), i (interrupt now) and s (skip, the default).

Restart means: stop the session (/exit), start `claude-lumo --resume <session id>` in the
same terminal and folder, then, if it was interrupted mid-task, send a one-line note that
it was restarted for an update followed by "continue".

After "wait for all" or "interrupt all" the proxy and LiteLLM are restarted too, once every
other session is back cleanly (none timed out, failed or was out of reach), through service_restart.py (#119); interrupted sessions get their "continue"
only after that, so it does not die in the restart. The session that ran the update goes last.
With no other sessions the user is asked whether to restart the two services.

Sessions whose registry entry is older than 7 days are treated as stale and pruned.
The session that ran the update (env CLAUDE_CODE_SESSION_ID) is never restarted
automatically; its resume command is printed at the end instead.

Controlling another terminal needs a controller: Orca (orca terminal list/send; the
registry cwd is matched against each terminal's worktreePath), or tmux (matching the
pane's current path). Without either, the exact per-session resume command is printed.
"""
import argparse
import glob
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

STALE_AFTER = timedelta(days=7)
CONTINUE_NOTE = "(restarted for a claude-lumo update) continue"


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


def restart_in_terminal(send, session_id, choice, registry_path, note=True):
    """Restart one session through a controller's send(text, enter, interrupt).

    choice "w": wait until idle first. choice "i": interrupt now. Both then exit the old
    session, relaunch with `claude-lumo --resume` and, if interrupted mid-task, send a
    note and "continue" (note=False leaves that to the caller, #119). Returns (ok, message).
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
    if interrupted and note:
        send(CONTINUE_NOTE, enter=True)
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


BULK_CHOICES = ("w", "i", "o", "s")


def choose_bulk(count, ask=None, out=None):
    """One up-front choice for all `count` restartable sessions (#118): 'w' wait for all, 'i'
    interrupt all, 'o' one by one, 's' skip all (the default, and the answer without a tty)."""
    ask, out = ask or ask_choice, out or print
    choice = ask(f"Restart {count} session{'s' if count != 1 else ''}? [w]ait for all to go idle, "
                 f"[i]nterrupt all now, choose [o]ne by one, [s]kip all (default s): ")
    if choice not in BULK_CHOICES:
        out("Unknown choice; skipping all.")
        return "s"
    return choice


def session_choice(bulk, session_id, ask=None):
    """The w/i/s choice for one session under the bulk choice: 'o' asks per session (#118)."""
    if bulk in ("w", "i", "s"):
        return bulk
    ask = ask or ask_choice
    return ask(f"  Restart {short_sid(session_id)}? [w]ait until idle, "
               f"[i]nterrupt now, [s]kip (default s): ")


def restart_sessions(restartable, matches, bulk, make_send, ask=None, restart=None, out=None):
    """Restart each session under the bulk choice; make_send(target) builds its controller's
    send(). Returns [(session, choice, ok, send)] for the sessions that were not skipped.

    Under "interrupt all" the continue note is held back: restart_services_after() sends it once
    the proxy and LiteLLM are back, so the resumed turn does not die in their restart (#119)."""
    restart, out = restart or restart_in_terminal, out or print
    results = []
    for s in restartable:
        choice = session_choice(bulk, s["session_id"], ask)
        if choice not in ("w", "i", "s"):
            out("  Unknown choice; skipping.")
            continue
        if choice == "s":
            continue
        send = make_send(matches[s["session_id"]])
        ok, msg = restart(send, s["session_id"], choice, s["path"], note=bulk != "i")
        out(f"  {'Restarted' if ok else 'Could not restart'} {short_sid(s['session_id'])}: {msg}.")
        results.append((s, choice, ok, send))
    return results


def load_service_restart():
    """service_restart.py from this install, or None when it is missing (trimmed clones)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        return importlib.import_module("service_restart")
    except ImportError:
        return None


def manual_service_hint():
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_restart.py")
    return (f"The proxy and LiteLLM still run the old release; once no session is mid-task, restart "
            f"them with: python \"{script}\"")


def restart_services_after(results, restart_services, out=None, unmatched=0):
    """After "wait for all" or "interrupt all" (#119): restart the proxy and LiteLLM now that the
    other sessions run the new release, then send the continue notes held back for interrupted
    sessions. The session that ran the update goes last, after this. True when both came back.

    Only when every session restarted cleanly: a session that timed out waiting, failed to
    restart or could not be reached (`unmatched`) may still be mid-turn on the old services, so
    they are left running and the manual command is printed instead."""
    out = out or print
    out("")
    if unmatched or not all(ok for _, _, ok, _ in results):
        out("Not every session restarted cleanly, so the proxy and LiteLLM were left running "
            "(restarting them could cut off a session that is still using them).")
        out(manual_service_hint())
        ok = False
    else:
        out("Restarting the proxy and LiteLLM so they run the new release:")
        ok = restart_services(out=out)
    for s, choice, restarted, send in results:
        if choice == "i" and restarted:
            send(CONTINUE_NOTE, enter=True)
    return ok


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
    services = load_service_restart()
    services_restarted = False

    if not sessions:
        print("No other running claude-lumo sessions found.")
        if services and args.dry_run:
            print("  [dry run] if you agree to restart the proxy and LiteLLM:")
            services.restart_services(dry_run=True)
        elif services and ask_choice(f"Restart the proxy and LiteLLM so they run {args.new_version}? "
                                     f"[y]es, [s]kip (default s): ") == "y":
            services_restarted = services.restart_services()
        elif services:
            print(manual_service_hint())
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

        restartable = []
        for s in sessions:
            target = matches.get(s["session_id"])
            if not target:
                print(f"  Session {short_sid(s['session_id'])}: I cannot restart it myself (no {controller} "
                      f"match); run `claude-lumo --resume {s['session_id']}` in its terminal.")
            elif args.dry_run:
                print(f"  [dry run] would restart {short_sid(s['session_id'])} via {controller} {target}")
            else:
                restartable.append(s)
        if services and args.dry_run:
            print("  [dry run] after \"wait for all\" or \"interrupt all\", the proxy and LiteLLM restart:")
            services.restart_services(dry_run=True)
        bulk = choose_bulk(len(restartable)) if restartable else "s"

        def make_send(target):
            if controller == "orca":
                return lambda text, enter=False, interrupt=False: send_orca_command(
                    orca_cmd, target, text, enter, interrupt)
            return lambda text, enter=False, interrupt=False: send_tmux_command(
                target, text, enter, interrupt)

        results = restart_sessions(restartable, matches, bulk, make_send)
        if services and bulk in ("w", "i"):
            services_restarted = restart_services_after(results, services.restart_services,
                                                        unmatched=len(sessions) - len(restartable))
        elif services and not args.dry_run:
            print(manual_service_hint())

    if self_sid and services_restarted:
        print()
        print(f"The proxy and LiteLLM now run {args.new_version}, but this session (the one that ran "
              f"the update) still runs the old files. Restart it last: exit it, then run "
              f"`claude-lumo --resume {self_sid}`.")
    elif self_sid:
        print(f"Finally, restart the session that ran the update (this one) last: "
              f"claude-lumo --resume {self_sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
