#!/usr/bin/env python
"""claude-lumo wipe: find and permanently delete conversation data that is no longer needed.

Multi-store discovery: in auto mode, scans all Claude/claude-lumo stores (known locations
plus bounded home walk). Named modes (--session, --project) search every store; ambiguous
matches are refused. The --store flag restricts the scan to given store(s).

A session is a candidate when signals say its project is finished: inactivity (configurable
days, default 30), the project folder is gone, the git branch it ran on is merged or removed,
a done marker exists, or (allowlist mode) the project is outside the allowlist file.

Stores enumerated: projects/<project>/<session>.jsonl and folders, session-env/, file-history/,
tasks/, history.jsonl (matching lines only), Lumo state logs, temp scratch (claude-* under
CLAUDE_CODE_TMPDIR or TEMP). Settings, credentials, memory and config are never touched:
a project wipe keeps projects/<project>/memory/ and deletes the rest of that project folder.
Orca data is report-only: the wipe output lists what Orca keeps and how to clear it by hand.

Link safety: directory links (junctions or symlinks) are never deleted through and never
followed during collection. Any target that resolves outside its owning store is refused.

Deletion is permanent but logical, not forensic: SSD wear levelling, backups, cloud sync and
other tools may still hold copies.

Scan roots: CLAUDE_LUMO_SCAN_ROOT (default: the home folder) is where the store scan looks
for ~/.claude and walks for other stores; CLAUDE_LUMO_STATE_DIR (default ~/.claude-lumo) holds
the Lumo logs and the separate data store. Tests point both at fixture folders.

Usage:
    wipe.py [--dry-run] [--days N] [--store PATH] [--allowlist FILE]
    wipe.py --session <session-id> [--dry-run]
    wipe.py --project <name-or-path> [--dry-run]
Repeat --store to limit multiple stores. Add --yes to skip the confirmation prompt.
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

# Per-project memory notes (projects/<project>/memory/) survive every wipe mode.
PROJECT_MEMORY_DIR = "memory"


def resolve_scan_root():
    """Home folder the store scan starts from: CLAUDE_LUMO_SCAN_ROOT if set, else ~."""
    env_dir = os.environ.get("CLAUDE_LUMO_SCAN_ROOT")
    return os.path.abspath(os.path.expanduser(env_dir or "~"))


def resolve_state_dir():
    """Lumo state folder: CLAUDE_LUMO_STATE_DIR if set, else ~/.claude-lumo."""
    env_dir = os.environ.get("CLAUDE_LUMO_STATE_DIR")
    return os.path.abspath(os.path.expanduser(env_dir or "~/.claude-lumo"))


def resolve_config_dir():
    """Data store root: CLAUDE_CONFIG_DIR if set, else <scan root>/.claude."""
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = os.path.expanduser(env_dir) if env_dir else os.path.join(resolve_scan_root(), ".claude")
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


# Directory names the home walk never enters: dependency caches, build output
# and app internals that can never hold a store but can hold millions of files.
WALK_SKIP_NAMES = frozenset((
    "node_modules", ".git", "__pycache__", "venv", ".venv", "Cache", "Caches",
    "Code Cache", "GPUCache", "dist", "build", "target", ".cargo", ".npm",
    ".rustup", "site-packages", "electron", "Temp", "Tmp",
))

# Top-level home folders the walk never enters: app data, cloud sync mounts.
WALK_SKIP_TOPLEVEL = frozenset((
    "AppData", "OneDrive", "Dropbox", "Google Drive", "iCloudDrive", "iCloud Drive",
))

# How many projects/ subfolders looks_like_store inspects before giving up.
STORE_PROBE_LIMIT = 50


def resolve_known_stores():
    """Store roots at fixed locations: realpath'd, deduped, priority order.
    Returns (path, origin) pairs; nonexistent paths are dropped."""
    candidates = []
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        candidates.append((os.path.expanduser(env_dir), "env"))
    candidates.append((os.path.join(resolve_scan_root(), ".claude"), "default"))
    state_dir = resolve_state_dir()
    candidates.append((os.path.join(state_dir, "data"), "separate"))
    legacy = os.path.join(state_dir, "config")
    if os.path.isdir(legacy):
        candidates.append((legacy, "legacy"))
    out = []
    seen = set()
    for path, origin in candidates:
        if not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        key = os.path.normcase(real)
        if key in seen:
            continue
        seen.add(key)
        out.append((real, origin))
    return out


def looks_like_store(path):
    """Strict check that a folder is a Claude/claude-lumo conversation store:
    real projects/ dir with a *.jsonl transcript in one of its first 50
    subfolders, plus at least one secondary marker (history.jsonl file,
    session-env/ or file-history/ real dir). Links satisfy nothing."""
    if is_any_link(path):
        return False
    projects = os.path.join(path, "projects")
    if not os.path.isdir(projects) or is_any_link(projects):
        return False
    has_transcripts = False
    try:
        with os.scandir(projects) as it:
            entries = list(it)
    except OSError:
        return False
    scanned = 0
    for entry in entries:
        scanned += 1
        if scanned > STORE_PROBE_LIMIT:
            break
        if is_any_link(entry.path):
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            with os.scandir(entry.path) as sub:
                if any(e.name.endswith(".jsonl") and not is_any_link(e.path)
                       and e.is_file(follow_symlinks=False) for e in sub):
                    has_transcripts = True
                    break
        except OSError:
            pass
    if not has_transcripts:
        return False
    history = os.path.join(path, "history.jsonl")
    if os.path.isfile(history) and not is_any_link(history):
        return True

    def real_dir(name):
        p = os.path.join(path, name)
        return os.path.isdir(p) and not is_any_link(p)

    return real_dir("session-env") or real_dir("file-history")


def walk_home_for_stores(home=None, max_depth=4):
    """Bounded scandir walk of the user profile looking for stores.
    Never descends into a link (junction loops would hang) or an accepted
    store; depth is capped at max_depth levels below home."""
    if home is None:
        home = resolve_scan_root()
    found = []

    def visit(dir_path, depth):
        try:
            with os.scandir(dir_path) as it:
                entries = list(it)
        except OSError:
            return
        for entry in entries:
            if entry.name in WALK_SKIP_NAMES:
                continue
            if depth == 1 and entry.name in WALK_SKIP_TOPLEVEL:
                continue
            if is_any_link(entry.path):
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            if looks_like_store(entry.path):
                found.append(os.path.abspath(entry.path))
                continue
            if depth < max_depth:
                visit(entry.path, depth + 1)

    visit(home, 1)
    return found


def discover_stores():
    """Every conversation store on the machine: known locations first, then
    whatever the home walk finds. Deduped by realpath, ordered."""
    current = os.path.normcase(os.path.realpath(resolve_config_dir()))
    out = []
    seen = set()

    def add(path, origin):
        real = os.path.realpath(path)
        key = os.path.normcase(real)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "path": os.path.abspath(path),
            "realpath": real,
            "origin": origin,
            "current": key == current,
        })

    for path, origin in resolve_known_stores():
        add(path, origin)
    for path in walk_home_for_stores():
        add(path, "discovered")
    return out


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
    """Paths and history changes to delete for a whole project. The project's
    memory/ folder is never deleted: everything beside it goes instead."""
    folder = project_info["folder"]
    paths = []
    if os.path.lexists(os.path.join(folder, PROJECT_MEMORY_DIR)):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        paths = [("project", os.path.join(folder, name)) for name in names
                 if os.path.normcase(name) != os.path.normcase(PROJECT_MEMORY_DIR)]
    elif os.path.exists(folder):
        paths = [("project", folder)]
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
    lumo_state = resolve_state_dir()
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
        project_path = os.path.join(projects_root, project)
        if not os.path.isdir(project_path) or is_any_link(project_path):
            continue
        info = collect_project(config_dir, project)
        reasons = detect_reasons(info, config_dir, days, allowlist)
        if reasons:
            paths, session_ids = project_wipe_plan(config_dir, info)
            if not paths and not session_ids:
                continue  # only memory/ is left: nothing to wipe
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


def delete_all(target_groups, store_root=None):
    """Delete every (kind, path), return per-path results. Refuses to delete
    through directory links, and with store_root set, anything that resolves
    outside the store (targets stay theirs even if a stray link points there)."""
    results = []
    store_real = os.path.normcase(os.path.realpath(store_root)) if store_root else None
    for _, path in target_groups:
        try:
            if os.path.isdir(path) and is_any_link(path):
                results.append((path, False, "is a link; not deleting through it"))
                continue
            if store_real:
                real = os.path.normcase(os.path.realpath(path))
                if real != store_real and not real.startswith(store_real + os.sep) \
                        and not real.startswith(store_real + "/"):
                    results.append((path, False, "resolves outside the store; refusing"))
                    continue
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


def print_report(store_groups, temp_paths, log_paths):
    """One combined report across every scanned store.

    Each store group is {"store": record, "plans": [...], "store_paths": [...]}
    with one entry per scanned store (empty ones print no section)."""
    scanned = ", ".join(g["store"]["path"] for g in store_groups)
    print(f"Scanning {len(store_groups)} store(s): {scanned}")
    total = sum(c["size"] for g in store_groups for c in g["plans"])
    session_count = sum(len(c["sessions"]) for g in store_groups for c in g["plans"])
    project_count = sum(len(g["plans"]) for g in store_groups)
    print(f"Found {session_count} conversation(s) in {project_count} project(s) to wipe.")
    for g in store_groups:
        candidates = g["plans"]
        store_paths = g["store_paths"]
        if not candidates and not store_paths:
            continue
        label = f"Store: {g['store']['path']}"
        if g["store"].get("current"):
            label += " [current store]"
        print(f"\n{label}")
        for c in candidates:
            when = ""
            if c["last_activity"]:
                when = datetime.fromtimestamp(c["last_activity"], tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"\nProject: {c['project']}  (last activity {when or 'unknown'})")
            print(f"Reasons: {'; '.join(c['reasons'])}")
            for kind, path in c["paths"]:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
            print(f"  [history.jsonl] {len(c['session_ids'])} matching line(s)")
        if store_paths:
            print("\nStale cache and snapshot stores:")
            for kind, path in store_paths:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
    for group, label in (
            (temp_paths, "stale temp scratch"),
            (log_paths, "stale Lumo logs")):
        if group:
            print(f"\n{label.title()}:")
            for kind, path in group:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
    grand = (total + sum(tree_size(p) for _, p in temp_paths)
             + sum(tree_size(p) for _, p in log_paths)
             + sum(tree_size(p) for g in store_groups for _, p in g["store_paths"]))
    print(f"\nTotal to delete: {format_size(grand)}")
    print("Deletion is permanent but logical, not forensic: SSD wear levelling, backups,")
    print("cloud sync and other tools' copies may still hold the data.")


def confirm(prompt="Type 'yes' to permanently delete: "):
    try:
        answer = input(prompt)
    except (AttributeError, EOFError, OSError, RuntimeError):
        # RuntimeError: input() with sys.stdin None ("lost sys.stdin", pythonw).
        return False
    return answer.strip().lower() == "yes"


def classify_candidate(candidate, config_dir):
    """Classify a wipe candidate as REMOVE (clearly safe), ASK (borderline)
    or KEEP (protect), with a short plain-language reason.

    Used by intelligent mode: REMOVE plans are deleted without further
    prompting, ASK plans are surfaced one by one for a per-project decision,
    KEEP plans are never deleted.
    """
    # Already protected by active check, so we focus on removal eligibility
    reasons = candidate.get("reasons", [])

    # Explicitly selected by the user (--session/--project): theirs to confirm.
    if any(r.startswith("selected by ") for r in reasons):
        return "REMOVE", "you selected it explicitly"

    # "marked done" is explicitly safe
    if "marked done" in reasons:
        return "REMOVE", "explicitly marked as complete"

    # "project folder gone" is safe (nothing left to lose)
    if "project folder gone" in reasons:
        return "REMOVE", "working folder no longer exists"

    if reasons == ["empty project folder"]:
        return "REMOVE", "empty project folder"

    # Pure inactivity is generally safe
    if len(reasons) == 1 and "inactive for" in reasons[0]:
        return "REMOVE", reasons[0]

    # Branch merged/removed: potentially safe but unpushed work may remain
    if "branch merged or removed" in reasons:
        return "ASK", "branch is merged or removed; verify no unpushed work remains"

    # Allowlist mode (outside allowlist): defer to user
    if "outside allowlist" in reasons:
        return "ASK", "project not in allowlist; confirm it is safe to remove"

    # Mixed signals: play it safe
    if len(reasons) > 1:
        return "ASK", f"multiple signals: {'; '.join(reasons)}"

    # Unknown: keep to be safe
    return "KEEP", "insufficient signals to classify as safe removal"


def _stdin_is_tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-lumo wipe",
        description="Find and permanently delete conversation data that is no longer needed.")
    parser.add_argument("--dry-run", action="store_true", help="list paths without deleting")
    parser.add_argument("--days", type=int, default=30, help="inactivity threshold (default 30)")
    parser.add_argument("--allowlist", help="file of project paths; anything outside is a candidate")
    parser.add_argument("--session", help="wipe one session by id")
    parser.add_argument("--project", help="wipe one project by folder name or path")
    parser.add_argument("--store", action="append", metavar="PATH",
                        help="restrict the wipe to this store (repeatable)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--mode", choices=["intelligent", "auto", "manual"],
                        help="deletion mode: intelligent (execute REMOVE, defer ASK), auto (batch delete), manual (show commands only)")
    args = parser.parse_args(argv)

    # Determine mode: --yes implies auto, pipe implies auto, otherwise interactive/intelligent
    if args.mode:
        mode = args.mode
    elif args.yes or not _stdin_is_tty():
        mode = "auto"
    else:
        mode = "intelligent"

    if args.days < 1:
        print("Error: --days must be at least 1.", file=sys.stderr)
        sys.exit(1)

    if args.store:
        stores = []
        for p in args.store:
            path = os.path.abspath(os.path.expanduser(p))
            if not os.path.isdir(path) or is_any_link(path):
                print(f"Error: store path {p} is not an existing directory.", file=sys.stderr)
                sys.exit(1)
            stores.append({"path": path, "realpath": os.path.realpath(path),
                           "origin": "flag", "current": False})
    else:
        stores = discover_stores()
    if not stores:
        print("Error: no Claude data stores found.", file=sys.stderr)
        sys.exit(1)

    # Named modes search all stores: --session refuses an ambiguous match,
    # --project combines plans across stores.
    plans = []
    store_groups = []
    if args.session:
        matches = []
        for store in stores:
            plan = plan_for_named_session(store["path"], args.session)
            if plan is not None:
                plan["store"] = store
                matches.append(plan)
        if not matches:
            print(f"No session {args.session} found in any store.")
            sys.exit(1)
        if len(matches) > 1:
            print(f"Error: session {args.session} matches multiple stores:", file=sys.stderr)
            for plan in matches:
                print(f"  {plan['store']['path']}", file=sys.stderr)
            sys.exit(1)
        plans = matches
        temp_paths, log_paths = [], []
    elif args.project:
        for store in stores:
            plan = plan_for_named_project(store["path"], args.project)
            if plan is not None:
                plan["store"] = store
                plans.append(plan)
        if not plans:
            print(f"No project matching {args.project} found in any store's projects.")
            sys.exit(1)
        temp_paths, log_paths = [], []
    else:
        for store in stores:
            plans_for_store = collect_candidates(store["path"], args.days, args.allowlist)
            store_paths = stale_config_stores(store["path"], args.days)
            for plan in plans_for_store:
                plan["store"] = store
            plans.extend(plans_for_store)
            store_groups.append({"store": store, "plans": plans_for_store,
                                  "store_paths": store_paths})
        temp_paths = stale_temp_scratch(resolve_temp_dir(), args.days)
        log_paths = stale_lumo_logs(args.days)

    if not plans and not any(g["plans"] or g["store_paths"] for g in store_groups) \
            and not temp_paths and not log_paths:
        print("Nothing to wipe.")
        return

    if not store_groups:
        store_groups = [{"store": p["store"], "plans": [p], "store_paths": []}
                         for p in plans]

    print_report(store_groups, temp_paths, log_paths)
    print_orca_section(describe_orca(resolve_orca_dir()))

    if args.dry_run:
        print("\nDry run: nothing was deleted. Re-run without --dry-run to delete.")
        return

    # Mode-specific execution
    if mode == "manual":
        # Manual mode: show commands, delete nothing
        print("\n=== Manual mode: commands to run ===\n")
        for group in store_groups:
            for plan in group["plans"]:
                proj = plan.get("project", "unknown")
                print(f"claude-lumo wipe --project {proj}  # {', '.join(plan.get('reasons', []))}")
        if temp_paths:
            print("# Temp scratch folders:")
            for _, p in temp_paths:
                print(f"# rm -rf {p}")
        if log_paths:
            print("# Stale logs:")
            for _, p in log_paths:
                print(f"# rm -f {p}")
        print("\nTo enable automatic deletion, re-run with --mode auto or --yes.")
        return

    if mode == "intelligent":
        # Intelligent mode: classify, delete REMOVE automatically, prompt per ASK.
        deleted, failed, freed = 0, 0, 0
        remove_plans, ask_plans, keep_plans = [], [], []
        deleted_plans = set()  # ids of plans actually deleted

        for group in store_groups:
            for plan in group["plans"]:
                cls, reason = classify_candidate(plan, group["store"]["path"])
                plan["_classification"] = (cls, reason)
                if cls == "REMOVE":
                    remove_plans.append((group, plan))
                elif cls == "ASK":
                    ask_plans.append((group, plan))
                else:
                    keep_plans.append((group, plan))

        # Print classification summary
        print(f"\nClassification: {len(remove_plans)} REMOVE, "
              f"{len(ask_plans)} ASK, {len(keep_plans)} KEEP\n")

        def delete_plan(group, plan):
            """Delete one plan's paths (plus its store's stale entries once)
            and record it for the history rewrite."""
            nonlocal deleted, failed, freed
            paths = list(plan["paths"])
            # Stale store-level entries ride along with the first deletion
            # from that store, exactly once.
            if group.get("store_paths"):
                paths += group["store_paths"]
                group["store_paths"] = []
            store_root = group["store"]["realpath"]
            freed += sum(tree_size(p) for _, p in paths)
            for path, ok, err in delete_all(paths, store_root=store_root):
                if ok:
                    deleted += 1
                    print(f"  DELETED: {path}")
                else:
                    failed += 1
                    print(f"  FAILED: {path} ({err})", file=sys.stderr)
            deleted_plans.add(id(plan))

        # Delete REMOVE plans without global confirm
        print("--- Auto-deleting REMOVE items ---")
        for group, plan in remove_plans:
            delete_plan(group, plan)

        # Prompt per ASK plan (interactive runs only)
        if ask_plans and _stdin_is_tty():
            print("\n--- Borderline items (ASK) ---")
            for group, plan in ask_plans:
                _, reason = plan["_classification"]
                proj = plan.get("project", "unknown")
                print(f"\n{proj}: {reason}")
                try:
                    ans = input(f"Delete {proj}? [y/N] ")
                except (AttributeError, EOFError, OSError):
                    ans = "n"
                if ans.strip().lower() in ("y", "yes"):
                    delete_plan(group, plan)
                else:
                    print(f"  KEPT: {proj}")

        # Keep plans are reported only
        if keep_plans:
            print("\n--- Kept items (KEEP) ---")
            for group, plan in keep_plans:
                _, reason = plan["_classification"]
                print(f"  {plan.get('project', 'unknown')}: {reason}")

        # Handle temp/log paths (always delete in intelligent mode)
        for group in temp_paths, log_paths:
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

        # History rewrite only for plans that were actually deleted.
        for group in store_groups:
            session_ids = set()
            for plan in group["plans"]:
                if id(plan) in deleted_plans:
                    session_ids |= plan["session_ids"]
            if not session_ids:
                continue
            removed, err = history_rewrite(
                os.path.join(group["store"]["path"], "history.jsonl"), session_ids)
            if err:
                failed += 1
                print(f"  FAILED: history.jsonl ({err})", file=sys.stderr)
            elif removed:
                deleted += 1
                print(f"  removed {removed} history.jsonl line(s) for "
                      f"{group['store']['path']}")

        print(f"\nDone: {deleted} path(s) deleted, {failed} failed, {format_size(freed)} freed.")
        if failed:
            sys.exit(1)
        return

    # Auto mode: original behavior. A piped (non-TTY) run that did not pass
    # --yes never prompts: it explains how to proceed and deletes nothing.
    if not args.yes:
        if not _stdin_is_tty():
            print("Non-interactive run: pass --yes to delete these, "
                  "or --mode manual for commands. Nothing was deleted.")
            return
        if not confirm():
            print("Aborted, nothing was deleted.")
            return

    deleted, failed, freed = 0, 0, 0
    for group in store_groups:
        paths = [p for c in group["plans"] for p in c["paths"]] + group["store_paths"]
        if not paths:
            continue
        store_root = group["store"]["realpath"]
        freed += sum(tree_size(p) for _, p in paths)
        for path, ok, err in delete_all(paths, store_root=store_root):
            if ok:
                deleted += 1
                print(f"  deleted: {path}")
            else:
                failed += 1
                print(f"  FAILED: {path} ({err})", file=sys.stderr)

    for group in temp_paths, log_paths:
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

    # History rewrite batched per store over the union of that store's plans.
    for group in store_groups:
        session_ids = set()
        for plan in group["plans"]:
            session_ids |= plan["session_ids"]
        if not session_ids:
            continue
        removed, err = history_rewrite(
            os.path.join(group["store"]["path"], "history.jsonl"), session_ids)
        if err:
            failed += 1
            print(f"  FAILED: history.jsonl ({err})", file=sys.stderr)
        elif removed:
            deleted += 1
            print(f"  removed {removed} history.jsonl line(s) for "
                  f"{group['store']['path']}")

    print(f"\nDone: {deleted} path(s) deleted, {failed} failed, {format_size(freed)} freed.")

    if failed:
        sys.exit(1)

    # Interactive runs only: offer a read-only audit of what is left.
    if _stdin_is_tty():
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
