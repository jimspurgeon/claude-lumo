#!/usr/bin/env python
"""claude-lumo wipe: find and permanently delete conversation data that is no longer needed.

A session is a candidate when signals say its project is finished: inactivity (configurable
days, default 30), the project folder is gone, the git branch it ran on is merged or removed,
a done marker exists, or (allowlist mode) the project is outside the allowlist file.

Stores enumerated: projects/<project>/<session>.jsonl and folders, session-env/, file-history/,
tasks/, history.jsonl (matching lines only), Lumo state logs, temp scratch (claude-* under
CLAUDE_CODE_TMPDIR or TEMP). Settings, credentials, memory and config are never touched.
Orca data is report-only: the wipe output lists what Orca keeps and how to clear it by hand.

Deletion is permanent but logical, not forensic: SSD wear levelling, backups, cloud sync and
other tools may still hold copies.

Usage:
    wipe.py [--dry-run] [--days N] [--allowlist FILE]
    wipe.py --session <session-id> [--dry-run]
    wipe.py --project <name-or-path> [--dry-run]
Add --yes to skip the confirmation prompt (for scripting).
"""

import argparse
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

# A session whose transcript changed this recently, or whose session-env lock exists, counts
# as active and is protected from every wipe mode.
ACTIVE_SECONDS = 6 * 3600
GIT_TIMEOUT = 5


def resolve_config_dir():
    """Data store root: CLAUDE_CONFIG_DIR if set, else ~/.claude."""
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = os.path.expanduser(env_dir) if env_dir else os.path.expanduser("~/.claude")
    return os.path.abspath(root)


def is_mount_point(path):
    """True for a Windows junction (directory reparse point)."""
    if hasattr(os.path, "isjunction"):
        try:
            return os.path.isjunction(path)
        except OSError:
            return False
    if os.name != "nt":
        return False
    try:
        return os.stat(path, follow_symlinks=False).st_reparse_tag \
            == stat.IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def is_any_link(path):
    """True for a symlink or junction: never deleted through, never followed."""
    return os.path.islink(path) or is_mount_point(path)


def resolve_temp_dir():
    """Temp scratch root: CLAUDE_CODE_TMPDIR if set, else the platform temp dir."""
    env_dir = os.environ.get("CLAUDE_CODE_TMPDIR")
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))
    for var in ("TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(var)
        if value:
            return os.path.abspath(value)
    return "/tmp"


def resolve_orca_dir():
    """Orca's data folder, for the report-only section."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "orca")
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "orca")
    return os.path.expanduser("~/.local/share/orca")


def run_git(args, cwd):
    """Run a git command, return stdout on success, None on any failure."""
    try:
        result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def format_size(num_bytes):
    """Human-readable size."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def tree_size(path):
    """Total size of a file or folder, 0 if unreadable. Links are never
    followed (junction loops would hang, and link targets are never ours)."""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError:
        return 0
    total = 0
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return total
    for entry in entries:
        try:
            if is_any_link(entry.path):
                continue
            if entry.is_dir(follow_symlinks=False):
                total += tree_size(entry.path)
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
        except OSError:
            pass
    return total


def probe_transcript(path, read_bytes=65536):
    """Read the head of a transcript for cwd and gitBranch. Returns a dict, possibly empty."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(read_bytes)
    except OSError:
        return {}
    out = {}
    for raw in chunk.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if out.get("cwd") is None and entry.get("cwd"):
            out["cwd"] = entry["cwd"]
        if out.get("branch") is None and entry.get("gitBranch"):
            out["branch"] = entry["gitBranch"]
        if "cwd" in out and "branch" in out:
            break
    return out


def session_is_active(config_dir, session_id, transcript_mtime):
    """True when the session looks live: env lock present or transcript freshly written."""
    lock = os.path.join(config_dir, "session-env", session_id, ".lock")
    if os.path.exists(lock):
        return True
    return transcript_mtime is not None and (time.time() - transcript_mtime) < ACTIVE_SECONDS


def collect_project(config_dir, project):
    """All we know about one projects/<project> folder: its sessions and their metadata."""
    folder = os.path.join(config_dir, "projects", project)
    sessions = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        session_id = name[:-len(".jsonl")]
        path = os.path.join(folder, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        info = probe_transcript(path)
        sessions.append({
            "id": session_id,
            "transcript": path,
            "mtime": mtime,
            "cwd": info.get("cwd"),
            "branch": info.get("branch"),
            "active": session_is_active(config_dir, session_id, mtime),
        })
    return {"name": project, "folder": folder, "sessions": sessions}


def last_activity(project_info):
    """Most recent transcript mtime across the project's sessions, as epoch seconds."""
    times = [s["mtime"] for s in project_info["sessions"] if s["mtime"]]
    return max(times) if times else None


def branch_signal(cwd, branch):
    """Classify the branch a session ran on: 'merged', 'removed', 'current' or 'unknown'."""
    if not cwd or not os.path.isdir(cwd):
        return "unknown"
    listing = run_git(["for-each-ref", "refs/heads", "--format=%(refname:short)"], cwd)
    if listing is None:
        return "unknown"
    branches = [b.strip() for b in listing.splitlines() if b.strip()]
    if branch and branch != "HEAD":
        if branch not in branches:
            return "removed"
        for default in ("master", "main", "origin/master", "origin/main"):
            if default in branches:
                check = run_git(["merge-base", "--is-ancestor", branch, default], cwd)
                if check is not None:
                    return "merged"
        return "current"
    return "current" if branches else "unknown"


def detect_reasons(project_info, config_dir, days, allowlist):
    """Decide whether a project is a wipe candidate, and why."""
    reasons = []
    sessions = project_info["sessions"]
    if not sessions:
        reasons.append("empty project folder")
        return reasons

    if any(s["active"] for s in sessions):
        return []  # never wipe a project with a live session

    if allowlist is not None:
        # Allowlist mode: not covered by the allowlist is the whole test.
        return [] if project_covered(project_info, allowlist) else ["outside allowlist"]

    activity = last_activity(project_info)
    if activity is not None and (time.time() - activity) / 86400 >= days:
        reasons.append(f"inactive for {int((time.time() - activity) / 86400)} days")

    cwds = {s["cwd"] for s in sessions if s["cwd"]}
    if cwds and not any(os.path.isdir(c) for c in cwds):
        reasons.append("project folder gone")

    statuses = [branch_signal(s["cwd"], s["branch"]) for s in sessions if s["cwd"]]
    statuses = [st for st in statuses if st != "unknown"]
    if statuses and all(st in ("merged", "removed") for st in statuses):
        reasons.append("branch merged or removed")

    if os.path.isfile(os.path.join(project_info["folder"], "wipe-done")):
        reasons.append("marked done")

    return reasons


def project_covered(project_info, allowlist):
    """True when any of the project's working directories is on the allowlist."""
    cwds = {s["cwd"] for s in project_info["sessions"] if s["cwd"]}
    for cwd in cwds:
        norm = os.path.normcase(os.path.normpath(os.path.abspath(cwd)))
        for entry in allowlist:
            if os.path.normcase(os.path.normpath(os.path.abspath(entry))) == norm:
                return True
    return False


def load_allowlist(path):
    """One project path per line; blank lines and #comments ignored. None if unreadable."""
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


def session_paths(config_dir, session_id, base_dir=None):
    """Every store keyed by session id, for one session."""
    base = base_dir or config_dir
    out = []
    for store in ("session-env", "file-history", "tasks"):
        p = os.path.join(base, store, session_id)
        if os.path.exists(p):
            out.append((store, p))
    return out


def project_wipe_plan(config_dir, project_info):
    """Paths and history changes to delete for a whole project."""
    folder = project_info["folder"]
    paths = [] if not os.path.exists(folder) else [("project", folder)]
    session_ids = set()
    for s in project_info["sessions"]:
        session_ids.add(s["id"])
        paths.extend(session_paths(config_dir, s["id"]))
    return paths, session_ids


def history_rewrite(history_file, drop_session_ids):
    """Remove history.jsonl lines whose sessionId is in the set; keep everything else.
    Returns (removed_count, error_or_None). Lines without a sessionId are always kept."""
    if not drop_session_ids or not os.path.isfile(history_file):
        return 0, None
    kept, removed = [], 0
    try:
        with open(history_file, encoding="utf-8", newline="") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    kept.append(line)
                    continue
                sid = entry.get("sessionId") if isinstance(entry, dict) else None
                if sid and sid in drop_session_ids:
                    removed += 1
                else:
                    kept.append(line)
        tmp = history_file + ".wipe-tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.writelines(kept)
        os.replace(tmp, history_file)
    except OSError as e:
        return 0, str(e)
    return removed, None


def stale_temp_scratch(temp_dir, days):
    """claude-* paths under the temp dir older than the cutoff."""
    cutoff = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(temp_dir, "claude*")):
        if os.path.basename(path) == "CLAUDE.md.bak-tier":
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                out.append(("temp", path))
        except OSError:
            pass
    return out


def stale_lumo_logs(days):
    """Top-level log files in the Lumo state folder, older than the cutoff."""
    lumo_state = os.path.expanduser("~/.claude-lumo")
    if not os.path.isdir(lumo_state):
        return []
    cutoff = time.time() - days * 86400
    out = []
    for path in glob.glob(os.path.join(lumo_state, "*.log")):
        try:
            if os.path.getmtime(path) < cutoff:
                out.append(("lumo-log", path))
        except OSError:
            pass
    return out


# Config-dir stores that hold no session key: entries are aged out globally,
# never attached to a per-session wipe plan.
STALE_STORE_NAMES = ("paste-cache", "image-cache", "shell-snapshots", "plans", "debug", "uploads")


def stale_config_stores(config_dir, days):
    """Entries in the age-keyed config stores older than the cutoff."""
    cutoff = time.time() - days * 86400
    out = []
    for name in STALE_STORE_NAMES:
        store = os.path.join(config_dir, name)
        if not os.path.isdir(store):
            continue
        try:
            entries = list(os.scandir(store))
        except OSError:
            continue
        for entry in entries:
            try:
                if not (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)):
                    continue
                if entry.stat().st_mtime < cutoff:
                    out.append((name, entry.path))
            except OSError:
                pass
    return out


def find_project_of_session(config_dir, session_id):
    """Locate the projects/<project> folder holding <session_id>.jsonl, else None."""
    projects = os.path.join(config_dir, "projects")
    try:
        names = os.listdir(projects)
    except OSError:
        return None
    for project in names:
        if os.path.isfile(os.path.join(projects, project, session_id + ".jsonl")):
            return project
    return None


def collect_candidates(config_dir, days, allowlist_path):
    """Auto mode: every project folder whose signals say it is finished."""
    allowlist = load_allowlist(allowlist_path) if allowlist_path else None
    if allowlist_path and allowlist is None:
        print(f"Error: cannot read allowlist file {allowlist_path}", file=sys.stderr)
        sys.exit(1)
    projects_root = os.path.join(config_dir, "projects")
    try:
        names = sorted(os.listdir(projects_root))
    except OSError:
        names = []
    candidates = []
    for project in names:
        if not os.path.isdir(os.path.join(projects_root, project)):
            continue
        info = collect_project(config_dir, project)
        reasons = detect_reasons(info, config_dir, days, allowlist)
        if reasons:
            paths, session_ids = project_wipe_plan(config_dir, info)
            size = sum(tree_size(p) for _, p in paths)
            candidates.append({
                "project": project,
                "reasons": reasons,
                "sessions": [s["id"] for s in info["sessions"]],
                "session_ids": session_ids,
                "paths": paths,
                "size": size,
                "last_activity": last_activity(info),
            })
    return candidates


def plan_for_named_session(config_dir, session_id):
    """--session mode: one session's files across all keyed stores."""
    # Session ids are opaque identifiers; anything path-like is refused rather
    # than joined into store paths (defense in depth, same rule as --project).
    if session_id in (".", "..") or "/" in session_id or os.sep in session_id \
            or (len(session_id) > 1 and session_id[1] == ":"):
        print(f"Error: {session_id!r} is not a valid session id.", file=sys.stderr)
        sys.exit(1)
    project = find_project_of_session(config_dir, session_id)
    if project is None:
        return None
    info = collect_project(config_dir, project)
    match = next((s for s in info["sessions"] if s["id"] == session_id), None)
    if match is None:
        return None
    if match["active"]:
        print("Error: this session looks active (recent transcript or env lock), refusing.",
              file=sys.stderr)
        sys.exit(1)
    paths = [("transcript", match["transcript"])] if os.path.exists(match["transcript"]) else []
    folder = os.path.join(info["folder"], session_id)
    if os.path.isdir(folder):
        paths.append(("session-folder", folder))
    paths.extend(session_paths(config_dir, session_id))
    return {
        "project": project,
        "reasons": ["selected by session id"],
        "sessions": [session_id],
        "session_ids": {session_id},
        "paths": paths,
        "size": sum(tree_size(p) for _, p in paths),
        "last_activity": match["mtime"],
    }


def plan_for_named_project(config_dir, name):
    """--project mode: accept the encoded folder name or a real path."""
    projects_root = os.path.join(config_dir, "projects")
    # Treat ".", ".." and anything with a separator (either kind, so "/" counts on
    # Windows too) as a path, never a folder name: encoded project names contain
    # none of these, and letting ".." through as a name would resolve above
    # projects/ and delete the whole config dir. Path-like values fall through
    # to the cwd-matching branch, which only ever picks real project folders.
    is_path_like = (name in (".", "..") or "/" in name or os.sep in name
                     or (len(name) > 1 and name[1] == ":") or name.startswith("~"))
    project = name if not is_path_like and os.path.isdir(os.path.join(projects_root, name)) else None
    if project is None:
        wanted = os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(name))))
        try:
            names = os.listdir(projects_root)
        except OSError:
            names = []
        for candidate in names:
            info = collect_project(config_dir, candidate)
            for s in info["sessions"]:
                if s["cwd"] and os.path.normcase(
                        os.path.normpath(os.path.abspath(s["cwd"]))) == wanted:
                    project = candidate
                    break
            if project:
                break
    if project is None:
        return None
    info = collect_project(config_dir, project)
    if any(s["active"] for s in info["sessions"]):
        print("Error: this project has a session that looks active, refusing.", file=sys.stderr)
        sys.exit(1)
    paths, session_ids = project_wipe_plan(config_dir, info)
    return {
        "project": project,
        "reasons": ["selected by project"],
        "sessions": [s["id"] for s in info["sessions"]],
        "session_ids": session_ids,
        "paths": paths,
        "size": sum(tree_size(p) for _, p in paths),
        "last_activity": last_activity(info),
    }


def describe_orca(orca_dir):
    """Report-only inventory of Orca's own stores (never edited by this tool)."""
    entries = [
        ("terminal scrollback", os.path.join(orca_dir, "terminal-history")),
        ("agent session runtimes", os.path.join(orca_dir, "agent-session-runtimes")),
        ("session recovery data", os.path.join(orca_dir, "agent-session-recovery.json")),
        ("orchestration database", os.path.join(orca_dir, "orchestration.db")),
    ]
    found = []
    for label, path in entries:
        if os.path.exists(path):
            found.append((label, path, tree_size(path)))
    return found


def print_orca_section(found):
    if not found:
        return
    print("\nOrca keeps its own copies of this data. claude-lumo will not touch them:")
    print("(unsupported formats; editing them can corrupt Orca and break after updates).")
    for label, path, size in found:
        print(f"  {label}: {path} ({format_size(size)})")
    print("Manual cleanup: close Orca terminals you no longer need, then delete their folders")
    print("under terminal-history yourself. Orca's Claude session list rebuilds from the")
    print("transcript files, so wiping above already removes the sessions from that list.")


def delete_all(target_groups):
    """Delete every (kind, path), return per-path results."""
    results = []
    for _, path in target_groups:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.exists(path) or os.path.islink(path):
                os.remove(path)
            else:
                results.append((path, False, "not found"))
                continue
            results.append((path, True, None))
        except OSError as e:
            results.append((path, False, str(e)))
    return results


def print_plans(candidates, days, temp_paths, log_paths, store_paths):
    total = sum(c["size"] for c in candidates)
    session_count = sum(len(c["sessions"]) for c in candidates)
    print(f"Found {session_count} conversation(s) in {len(candidates)} project(s) to wipe.")
    for c in candidates:
        when = ""
        if c["last_activity"]:
            when = datetime.fromtimestamp(c["last_activity"], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"\nProject: {c['project']}  (last activity {when or 'unknown'})")
        print(f"Reasons: {'; '.join(c['reasons'])}")
        for kind, path in c["paths"]:
            print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
        print(f"  [history.jsonl] {len(c['session_ids'])} matching line(s)")
    for group, label in (
            (temp_paths, "stale temp scratch"),
            (log_paths, "stale Lumo logs"),
            (store_paths, "stale cache and snapshot stores")):
        if group:
            print(f"\n{label.title()}:")
            for kind, path in group:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
    grand = (total + sum(tree_size(p) for _, p in temp_paths)
             + sum(tree_size(p) for _, p in log_paths)
             + sum(tree_size(p) for _, p in store_paths))
    print(f"\nTotal to delete: {format_size(grand)}")
    print("Deletion is permanent but logical, not forensic: SSD wear levelling, backups,")
    print("cloud sync and other tools' copies may still hold the data.")


def confirm(prompt="Type 'yes' to permanently delete: "):
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-lumo wipe",
        description="Find and permanently delete conversation data that is no longer needed.")
    parser.add_argument("--dry-run", action="store_true", help="list paths without deleting")
    parser.add_argument("--days", type=int, default=30, help="inactivity threshold (default 30)")
    parser.add_argument("--allowlist", help="file of project paths; anything outside is a candidate")
    parser.add_argument("--session", help="wipe one session by id")
    parser.add_argument("--project", help="wipe one project by folder name or path")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    config_dir = resolve_config_dir()
    if not os.path.isdir(config_dir):
        print("Error: cannot determine the data store path "
              f"({config_dir} does not exist). Set CLAUDE_CONFIG_DIR.", file=sys.stderr)
        sys.exit(1)
    if args.days < 1:
        print("Error: --days must be at least 1.", file=sys.stderr)
        sys.exit(1)

    if args.session:
        plan = plan_for_named_session(config_dir, args.session)
        if plan is None:
            print(f"No session {args.session} found under {config_dir}/projects.")
            sys.exit(1)
        plans = [plan]
        temp_paths, log_paths, store_paths = [], [], []
    elif args.project:
        plan = plan_for_named_project(config_dir, args.project)
        if plan is None:
            print(f"No project matching {args.project} found under {config_dir}/projects.")
            sys.exit(1)
        plans = [plan]
        temp_paths, log_paths, store_paths = [], [], []
    else:
        plans = collect_candidates(config_dir, args.days, args.allowlist)
        temp_paths = stale_temp_scratch(resolve_temp_dir(), args.days)
        log_paths = stale_lumo_logs(args.days)
        store_paths = stale_config_stores(config_dir, args.days)

    if not plans and not temp_paths and not log_paths and not store_paths:
        print("Nothing to wipe.")
        return

    print_plans(plans, args.days, temp_paths, log_paths, store_paths)
    print_orca_section(describe_orca(resolve_orca_dir()))

    if args.dry_run:
        print("\nDry run: nothing was deleted. Re-run without --dry-run to delete.")
        return

    if not args.yes and not confirm():
        print("Aborted, nothing was deleted.")
        return

    deleted, failed, freed = 0, 0, 0
    all_groups = []
    for c in plans:
        all_groups.append(c["paths"])
    all_groups.extend([temp_paths, log_paths, store_paths])
    for group in all_groups:
        if not group:
            continue
        freed += sum(tree_size(p) for _, p in group)
        for path, ok, err in delete_all(group):
            if ok:
                deleted += 1
                print(f"  deleted: {path}")
            else:
                failed += 1
                print(f"  FAILED: {path} ({err})", file=sys.stderr)

    for plan in plans:
        removed, err = history_rewrite(
            os.path.join(config_dir, "history.jsonl"), plan["session_ids"])
        if err:
            failed += 1
            print(f"  FAILED: history.jsonl ({err})", file=sys.stderr)
        elif removed:
            deleted += 1
            print(f"  removed {removed} history.jsonl line(s) for {plan['project']}")

    print(f"\nDone: {deleted} path(s) deleted, {failed} failed, {format_size(freed)} freed.")

    if failed:
        sys.exit(1)

    # Interactive runs only: offer a read-only audit of what is left. Dry runs
    # return above, so this is a completed deletion pass.
    if sys.stdin.isatty():
        try:
            answer = input("Run a privacy audit to check what's left? [y/N] ")
        except (AttributeError, EOFError, OSError):
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            audit = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "privacy_audit.py")
            result = subprocess.run([sys.executable, audit])
            sys.exit(result.returncode)


if __name__ == "__main__":
    main()
