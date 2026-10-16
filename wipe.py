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
CLAUDE_CODE_TMPDIR or TEMP), stale proxy debug dumps (the repo's proxy/debug/ spill). Settings,
credentials, memory and config are never touched:
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
Repeat --store to limit multiple stores. On a terminal the run ends with a choice menu (go
through it with a claude-lumo agent, delete all after typing yes, print commands, or cancel);
nothing is deleted without a prompt. Add --yes to skip the prompts (for scripting).
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

# In a project that is still in use, single sessions become candidates by age: scripted
# (non-interactive, entrypoint sdk-*) sessions after AUTOMATION_DAYS, others after SESSION_DAYS.
# Before this, a project with one live session protected all of its old ones.
SESSION_DAYS = 7
AUTOMATION_DAYS = 1
LISTED_PATHS_PER_PLAN = 10

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
        if out.get("entrypoint") is None and entry.get("entrypoint"):
            out["entrypoint"] = entry["entrypoint"]
        if "cwd" in out and "branch" in out and "entrypoint" in out:
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
            # Scripted runs (claude -p, the SDK) record an sdk-* entrypoint; interactive ones "cli".
            "automation": str(info.get("entrypoint") or "").startswith("sdk"),
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


def history_matches(history_file, session_ids):
    """Count history.jsonl lines whose sessionId is in the set (read-only, same match as
    history_rewrite). A missing or unreadable file counts as 0."""
    if not session_ids or not os.path.isfile(history_file):
        return 0
    count = 0
    try:
        with open(history_file, encoding="utf-8", newline="") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                sid = entry.get("sessionId") if isinstance(entry, dict) else None
                if sid and sid in session_ids:
                    count += 1
    except OSError:
        return 0
    return count


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


def stale_debug_dumps(days, repo_root=None):
    """Entries in the repo's proxy/debug conversation dumps older than the cutoff.
    proxy/debug/ sits next to proxy/proxy.mjs in this script's repo, not the
    Lumo state dir; gitignored spill, aged out like the Lumo logs."""
    root = repo_root or os.path.dirname(os.path.abspath(__file__))
    debug_dir = os.path.join(root, "proxy", "debug")
    if not os.path.isdir(debug_dir) or is_any_link(debug_dir):
        return []
    cutoff = time.time() - days * 86400
    out = []
    try:
        entries = list(os.scandir(debug_dir))
    except OSError:
        return out
    for entry in entries:
        try:
            if not (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)):
                continue
            if entry.stat().st_mtime < cutoff:
                out.append(("debug-dump", entry.path))
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


def session_file_paths(config_dir, project_info, session):
    """One session's files: its transcript, its folder beside it, and the id-keyed stores."""
    paths = [("transcript", session["transcript"])] if os.path.exists(session["transcript"]) else []
    folder = os.path.join(project_info["folder"], session["id"])
    if os.path.isdir(folder):
        paths.append(("session-folder", folder))
    paths.extend(session_paths(config_dir, session["id"]))
    return paths


def old_sessions_plan(config_dir, project_info, session_days=SESSION_DAYS,
                      automation_days=AUTOMATION_DAYS):
    """Single old sessions in a project that is not finished as a whole: scripted ones older
    than automation_days, others older than session_days. Active sessions are never picked.
    Returns a plan marked "partial", or None when no session qualifies."""
    now = time.time()
    scripted, other = [], []
    for s in project_info["sessions"]:
        if s["active"] or s["mtime"] is None:
            continue
        age_days = (now - s["mtime"]) / 86400
        if s.get("automation") and age_days >= automation_days:
            scripted.append(s)
        elif age_days >= session_days:
            other.append(s)
    picked = scripted + other
    if not picked:
        return None
    reasons = []
    if scripted:
        reasons.append(f"{len(scripted)} automation session(s) older than {automation_days} day(s)")
    if other:
        reasons.append(f"{len(other)} session(s) older than {session_days} day(s)")
    paths = [p for s in picked for p in session_file_paths(config_dir, project_info, s)]
    return {
        "project": project_info["name"],
        "reasons": reasons,
        "sessions": [s["id"] for s in picked],
        "session_ids": {s["id"] for s in picked},
        "paths": paths,
        "size": sum(tree_size(p) for _, p in paths),
        "last_activity": max(s["mtime"] for s in picked),
        "partial": True,
    }


def collect_candidates(config_dir, days, allowlist_path, session_days=SESSION_DAYS,
                       automation_days=AUTOMATION_DAYS):
    """Auto mode: every project folder whose signals say it is finished, plus the old single
    sessions of projects that are still in use (not in allowlist mode)."""
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
        elif allowlist is None:
            plan = old_sessions_plan(config_dir, info, session_days, automation_days)
            if plan:
                candidates.append(plan)
    return candidates


def plan_for_named_session(config_dir, session_id, skip_active=False):
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
        if skip_active:
            print(f"Note: session {session_id} looks active; skipped (--skip-active).",
                  file=sys.stderr)
            return None
        print("Error: this session looks active (recent transcript or env lock), refusing.",
              file=sys.stderr)
        sys.exit(1)
    paths = session_file_paths(config_dir, info, match)
    return {
        "project": project,
        "reasons": ["selected by session id"],
        "sessions": [session_id],
        "session_ids": {session_id},
        "paths": paths,
        "size": sum(tree_size(p) for _, p in paths),
        "last_activity": match["mtime"],
    }


def plan_for_named_project(config_dir, name, old_sessions=False, session_days=SESSION_DAYS,
                           automation_days=AUTOMATION_DAYS, restrict_to_thresholds=False):
    """--project mode: accept the encoded folder name or a real path. With old_sessions, only
    that project's old single sessions (see old_sessions_plan); active ones are skipped, not
    refused, so a project in use can be cleaned.

    A project with an active session is never wiped wholesale; its old single sessions can
    still be wiped (automatic fallback). A plain project wipe with explicit
    --session-days/--automation-days restricts to sessions past those ages.
    """
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
    if old_sessions or restrict_to_thresholds:
        # Only the project's old single sessions, not the project itself.
        plan = old_sessions_plan(config_dir, info, session_days, automation_days)
        if plan:
            plan["reasons"] = ["selected by project"] + plan["reasons"]
        return plan
    if any(s["active"] for s in info["sessions"]):
        # Never wipe a project with an active session wholesale; fall back to
        # its old single sessions (refuse only when none are old enough).
        plan = old_sessions_plan(config_dir, info, session_days, automation_days)
        if plan is None:
            print("Error: this project has a session that looks active, refusing.", file=sys.stderr)
            sys.exit(1)
        plan["reasons"] = ["selected by project",
                           "active session present; only its old sessions can be wiped"] + plan["reasons"]
        return plan
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


def print_report(store_groups, temp_paths, log_paths, dump_paths=()):
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
            for kind, path in c["paths"][:LISTED_PATHS_PER_PLAN]:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
            hidden = len(c["paths"]) - LISTED_PATHS_PER_PLAN
            if hidden > 0:
                print(f"  ... and {hidden} more path(s), {format_size(c['size'])} in all")
            entries = history_matches(os.path.join(g["store"]["path"], "history.jsonl"),
                                      set(c["session_ids"]))
            if entries:
                print(f"  [history.jsonl] {entries} entr{'y' if entries == 1 else 'ies'}")
        if store_paths:
            print("\nStale cache and snapshot stores:")
            for kind, path in store_paths:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
    for group, label in (
            (temp_paths, "stale temp scratch"),
            (log_paths, "stale Lumo logs"),
            (dump_paths, "stale proxy debug dumps")):
        if group:
            print(f"\n{label.title()}:")
            for kind, path in group:
                print(f"  [{kind}] {path}  ({format_size(tree_size(path))})")
    grand = (total + sum(tree_size(p) for _, p in temp_paths)
             + sum(tree_size(p) for _, p in log_paths)
             + sum(tree_size(p) for _, p in dump_paths)
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

    Used by intelligent mode: REMOVE plans are deleted after one confirmation
    (or --yes), ASK plans are surfaced one by one for a per-project decision,
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

    # Old single sessions of a project still in use: scripted runs that finished a while ago
    # are safe; the user's own older conversations are theirs to decide.
    old = [r for r in reasons if " session(s) older than " in r]
    if old:
        if all("automation session(s)" in r for r in old):
            return "REMOVE", "scripted sessions that finished a while ago"
        return "ASK", "older conversations in a project still in use"

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


# Exit code that tells the launcher to start a claude-lumo session with the agent prompt.
AGENT_EXIT = 10
AGENT_PROMPT_FILE = "wipe-agent-prompt.txt"
AGENT_PROMPT = (
    "Help me safely wipe as much old local conversation data as I am comfortable with. "
    "Nothing has been deleted yet. Work with my actual setup; do not assume any particular tool, "
    "folder layout or operating system. "
    "1) Learn the setup first: the OS, which Claude data stores exist (the wipe report lists "
    "them), and which tools on this machine keep their own copies of sessions or run them "
    "(terminal or session managers, IDEs, worktree tools; the wipe report names the ones it "
    "knows). Check what is installed and running instead of guessing, and ask me if unsure. "
    "2) Look at every location: run `claude-lumo wipe --dry-run`, then "
    "`claude-lumo wipe --dry-run --days 7 --session-days 2` for more recent leftovers, and "
    "`claude-lumo privacy-audit --mode manual`. Never run a command that waits for input. "
    "Cover every Claude data store, temp scratch, Lumo logs and other tools' copies. "
    "3) Check each finding beyond the script's rules, from evidence rather than assumed paths: "
    "the working folder recorded in the transcript (its cwd), `git worktree list` in that "
    "repository to find worktrees wherever they live, whether the branch is pushed or merged, "
    "and whether a running process, open terminal or session still uses it. "
    "4) Show me a grouped list with sizes and your recommendation (delete, keep, or ask me), "
    "and ask which to delete, with a 'delete all recommended' option. "
    "5) Delete only what I approve: `claude-lumo wipe --project <name> --session-days <D> --yes` for a finished "
    "project, `claude-lumo wipe --project <name> --old-sessions --session-days <D> --yes` for the old sessions of a "
    "project still in use, or "
    "`--session <id> --skip-active --yes`. Other tools' copies: prefer that tool's own way to "
    "close or remove them, tell me what to close first, and delete only what I name. "
    "Never delete settings, credentials, memory or config, and never this "
    "session. Temp scratch and Lumo logs: list them and delete only the paths I approve. "
    "6) Report what was deleted and how much space was freed."
)

MENU = """
How do you want to go ahead? Nothing has been deleted.
  1) Go through it with a claude-lumo agent (recommended): it checks every location,
     other tools' copies included, and deletes only what you approve
  2) Delete everything listed above (asks you to confirm)
  3) Show the commands to do it by hand
  4) Cancel"""


def choose_mode():
    """Ask how to proceed: 'agent', 'auto', 'manual', or None to cancel (also on no input)."""
    print(MENU)
    try:
        answer = input("Choice [1]: ").strip()
    except (AttributeError, EOFError, OSError, RuntimeError):
        return None
    return {"": "agent", "1": "agent", "2": "auto", "3": "manual"}.get(answer)


def start_agent():
    """Hand over to an agent session. Through the launcher (CLAUDE_LUMO_LAUNCHER=1): leave the
    prompt where it reads it and exit with AGENT_EXIT. Run directly, no session can start, so
    say how to get one instead."""
    if os.environ.get("CLAUDE_LUMO_LAUNCHER") != "1":
        print("\nRun `claude-lumo wipe` to go through it with an agent. Nothing was deleted.")
        return
    state = resolve_state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, AGENT_PROMPT_FILE), "w", encoding="utf-8") as f:
        f.write(AGENT_PROMPT)
    print("\nStarting a claude-lumo session to go through it with you. Nothing has "
          "been deleted.")
    sys.exit(AGENT_EXIT)


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
    parser.add_argument("--old-sessions", action="store_true",
                        help="with --project: only its old single sessions, skipping active ones")
    parser.add_argument("--session-days", type=int, default=SESSION_DAYS,
                        help=f"single sessions older than this are candidates (default {SESSION_DAYS})")
    parser.add_argument("--automation-days", type=int, default=AUTOMATION_DAYS,
                        help="scripted sessions older than this are candidates "
                             f"(default {AUTOMATION_DAYS})")
    parser.add_argument("--skip-active", action="store_true",
                        help="with --session: skip it instead of refusing when it looks active")
    parser.add_argument("--store", action="append", metavar="PATH",
                        help="restrict the wipe to this store (repeatable)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--mode", choices=["agent", "intelligent", "auto", "manual"],
                        help="agent (choose how to proceed, default on a terminal), intelligent "
                             "(classify, confirm REMOVE, ask per borderline item), auto (batch "
                             "delete after one confirmation), manual (show commands only)")
    args = parser.parse_args(argv)

    # Determine mode: --yes and pipes imply auto (which deletes nothing without --yes);
    # a terminal gets the choice menu. Nothing is ever deleted without a prompt or --yes.
    if args.mode:
        mode = args.mode
    elif args.yes or not _stdin_is_tty():
        mode = "auto"
    else:
        mode = "agent"

    for flag, value in (("--days", args.days), ("--session-days", args.session_days),
                        ("--automation-days", args.automation_days)):
        if value < 1:
            print(f"Error: {flag} must be at least 1.", file=sys.stderr)
            sys.exit(1)
    if args.old_sessions and not args.project:
        print("Error: --old-sessions needs --project.", file=sys.stderr)
        sys.exit(1)
    if args.skip_active and not args.session:
        print("Error: --skip-active needs --session.", file=sys.stderr)
        sys.exit(1)

    # Explicit thresholds turn a plain --project wipe into an old-sessions-only wipe.
    restrict = (args.session_days != SESSION_DAYS or args.automation_days != AUTOMATION_DAYS)

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
            plan = plan_for_named_session(store["path"], args.session, args.skip_active)
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
        temp_paths, log_paths, dump_paths = [], [], []
    elif args.project:
        for store in stores:
            plan = plan_for_named_project(store["path"], args.project, args.old_sessions,
                                          args.session_days, args.automation_days,
                                          restrict_to_thresholds=restrict)
            if plan is not None:
                plan["store"] = store
                plans.append(plan)
        if not plans and (args.old_sessions or restrict):
            print(f"No old sessions to wipe in {args.project}.")
            return
        if not plans:
            print(f"No project matching {args.project} found in any store's projects.")
            sys.exit(1)
        temp_paths, log_paths, dump_paths = [], [], []
    else:
        for store in stores:
            plans_for_store = collect_candidates(store["path"], args.days, args.allowlist,
                                                 args.session_days, args.automation_days)
            if mode == "auto" and args.yes:
                # An unattended --yes run never deletes the user's own conversations from a
                # project still in use; those need --project <name> --old-sessions.
                kept = [p for p in plans_for_store if not p.get("partial")
                        or classify_candidate(p, store["path"])[0] == "REMOVE"]
                for p in plans_for_store:
                    if p not in kept:
                        print(f"Skipped with --yes: old sessions in {p['project']} "
                              f"(use --project {p['project']} --old-sessions).")
                plans_for_store = kept
            store_paths = stale_config_stores(store["path"], args.days)
            for plan in plans_for_store:
                plan["store"] = store
            plans.extend(plans_for_store)
            store_groups.append({"store": store, "plans": plans_for_store,
                                  "store_paths": store_paths})
        temp_paths = stale_temp_scratch(resolve_temp_dir(), args.days)
        log_paths = stale_lumo_logs(args.days)
        dump_paths = stale_debug_dumps(args.days)

    if not plans and not any(g["plans"] or g["store_paths"] for g in store_groups) \
            and not temp_paths and not log_paths and not dump_paths:
        print("Nothing to wipe.")
        return

    if not store_groups:
        store_groups = [{"store": p["store"], "plans": [p], "store_paths": []}
                         for p in plans]

    print_report(store_groups, temp_paths, log_paths, dump_paths)
    print_orca_section(describe_orca(resolve_orca_dir()))

    if args.dry_run:
        print("\nDry run: nothing was deleted. Re-run without --dry-run to delete.")
        return

    if mode == "agent":
        mode = choose_mode()
        if mode == "agent":
            start_agent()
            return
        if mode is None:
            print("Cancelled, nothing was deleted.")
            return

    # Mode-specific execution
    if mode == "manual":
        # Manual mode: show commands, delete nothing
        print("\n=== Manual mode: commands to run ===\n")
        for group in store_groups:
            for plan in group["plans"]:
                proj = plan.get("project", "unknown")
                only_old = " --old-sessions" if plan.get("partial") else ""
                print(f"claude-lumo wipe --project {proj}{only_old}  "
                      f"# {', '.join(plan.get('reasons', []))}")
        if temp_paths:
            print("# Temp scratch folders:")
            for _, p in temp_paths:
                print(f"# rm -rf {p}")
        if log_paths:
            print("# Stale logs:")
            for _, p in log_paths:
                print(f"# rm -f {p}")
        if dump_paths:
            print("# Stale proxy debug dumps:")
            for _, p in dump_paths:
                print(f"# rm -rf {p}")
        print("\nTo enable automatic deletion, re-run with --mode auto or --yes.")
        return

    if mode == "intelligent":
        # Intelligent mode: classify, confirm once for REMOVE, prompt per ASK.
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

        # REMOVE plans, temp scratch, logs and debug dumps go only after one confirmation
        # (or --yes). A run that cannot prompt deletes nothing.
        has_bulk = bool(remove_plans or temp_paths or log_paths or dump_paths)
        approved = has_bulk and (args.yes or (_stdin_is_tty() and confirm(
            "Type 'yes' to permanently delete the REMOVE items, temp scratch and logs: ")))
        if has_bulk and not approved:
            print("REMOVE items, temp scratch and logs kept: nothing deleted without "
                  "confirmation.")
        if approved and remove_plans:
            print("--- Deleting REMOVE items ---")
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

        # Temp scratch, logs and debug dumps ride on the same confirmation as the REMOVE items.
        for group in temp_paths, log_paths, dump_paths:
            if not group or not approved:
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

    for group in temp_paths, log_paths, dump_paths:
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
