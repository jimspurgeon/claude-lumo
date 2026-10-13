#!/usr/bin/env python3
"""Optional data separation for claude-lumo.

    python separate_data.py setup [--config-dir PATH] [--shared PATH]
    python separate_data.py migrate [--dry-run] [--yes]
    python separate_data.py revert-migrate [--dry-run] [--yes]
    python separate_data.py status [--config-dir PATH] [--shared PATH]

Builds a separate data store in ~/.claude-lumo/data, linked from ~/.claude for shared config.
Migrates Lumo sessions (transcripts with "model":"lumo-") between stores.
All operations are idempotent and safe to re-run; they never touch non-Lumo sessions.
"""
import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

try:
    import _winapi  # Windows only; junctions work without admin rights
except ImportError:
    _winapi = None


# Items shared via link (linked only when the source exists):
SHARED_ITEMS = [
    "CLAUDE.md", "settings.json", "keybindings.json", "themes/", "rules/", "skills/",
    "commands/", "agents/", "workflows/", "output-styles/", "plugins/", "hooks/"
]

# Items kept separate (real directories/files in the data dir, never linked):
DATA_ITEMS = [
    "projects/", "file-history/", "session-env/", "shell-snapshots/", "todos/",
    "plans/", "paste-cache/", "debug/",
    "history.jsonl", ".credentials.json"
]


def warn(msg):
    print(f"claude-lumo: {msg}", file=sys.stderr)


def info(msg):
    print(f"info: {msg}")


def is_link(path):
    """True for a symlink or (on Windows) a junction."""
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    try:
        return os.stat(path, follow_symlinks=False).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def remove_link(path):
    """Remove a symlink/junction (broken or healthy) without touching its target.

    Junctions and directory symlinks remove with rmdir; file symlinks with remove.
    Returns False (leaving the path alone) when neither works, e.g. it is a real
    directory with content.
    """
    try:
        os.rmdir(str(path))
        return True
    except OSError:
        pass
    try:
        os.remove(str(path))
        return True
    except OSError:
        return False


def make_link(src, dst):
    """Link dst -> src; returns the method used.

    Directories: junction on Windows (no admin needed), symlink elsewhere. Files: a
    junction on a file is a reparse point Win32 cannot read, so files get a real
    symlink when the privilege allows it, then a hardlink (same volume, no admin),
    then a copy with a warning. Copies are refreshed by each setup run (the launcher
    runs setup at every launch), but changes made to them inside a Lumo session do
    not flow back to the shared store.
    """
    if os.path.isdir(src):
        if os.name == "nt" and _winapi is not None:
            try:
                _winapi.CreateJunction(str(src), str(dst))
                return "junction"
            except OSError:
                pass  # Fall through to a real symlink if possible
        os.symlink(str(src), str(dst), target_is_directory=True)
        return "symlink"
    try:
        os.symlink(str(src), str(dst))
        return "symlink"
    except OSError:
        pass
    try:
        os.link(str(src), str(dst))
        return "hardlink"
    except OSError as e:
        shutil.copyfile(str(src), str(dst))
        warn(f"copied {dst} instead of linking it ({e}); it is refreshed by each setup "
             f"run, but changes made to it inside a Lumo session do not flow back")
        return "copy"


def move(src, dst):
    """Move src to dst, skipping (with a warning) if dst already exists."""
    if os.path.exists(dst):
        warn(f"skip: {dst} already exists")
        return False
    dst_parent = os.path.dirname(dst)
    if dst_parent:
        os.makedirs(dst_parent, exist_ok=True)
    shutil.move(src, dst)
    return True


def rmdir_if_empty(path):
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def find_lumo_sessions(projects_dir):
    """Set of (project_dir, session_id) for transcripts containing a lumo model.

    Scans line by line and stops at the first lumo model per file, so Lumo transcripts
    are cheap; pure-Claude transcripts are read to the end (the whole file must be
    clean of lumo models to count as Claude).
    """
    lumo = set()
    projects = Path(projects_dir)
    if not projects.is_dir():
        return lumo
    for proj_dir in projects.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                with open(jsonl, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if '"model":"lumo-' in line:
                            lumo.add((str(proj_dir), jsonl.stem))
                            break
            except OSError:
                pass
    return lumo


def find_session_dirs(project_dir, session_id):
    """Whole per-session sibling dirs (<session-id>/ with subagents, tool-results, ...)."""
    base = Path(project_dir) / session_id
    return [str(base)] if base.is_dir() else []


def read_history_lines(history_file):
    if not os.path.isfile(history_file):
        return []
    with open(history_file, encoding="utf-8") as f:
        return f.readlines()


def write_atomic(path, lines):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    os.replace(tmp, path)


def split_history(history_file, session_ids):
    """(keep_lines, moved_lines): lines whose sessionId is in session_ids are moved."""
    keep, moved = [], []
    for line in read_history_lines(history_file):
        try:
            sid = json.loads(line).get("sessionId")
        except ValueError:
            sid = None
        if sid in session_ids:
            moved.append(line)
        else:
            keep.append(line)
    return keep, moved


COPIES_MANIFEST = "copies.json"


def load_copies(data_dir):
    path = Path(data_dir) / COPIES_MANIFEST
    if not path.is_file():
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (ValueError, OSError):
        return set()


def save_copies(data_dir, names):
    write_atomic(str(Path(data_dir) / COPIES_MANIFEST), [json.dumps(sorted(names)) + "\n"])


def setup_links(shared_dir, data_dir):
    """Create the idempotent link layout: shared items linked, data items real."""
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)

    # Data items: create empty if missing, never touch existing ones.
    for item in DATA_ITEMS:
        path = data / item.rstrip("/")
        if item.endswith("/") and not path.exists():
            path.mkdir(exist_ok=True)
        elif not item.endswith("/") and not path.exists():
            path.touch()

    # Shared items: link when the source exists. Real items are only ever our own file
    # copies (recorded in copies.json so they can be refreshed); anything else is warned
    # about and left alone.
    copies = load_copies(data_dir)
    for item in SHARED_ITEMS:
        name = item.rstrip("/")
        src = Path(shared_dir) / name
        dst = data / name
        if not src.exists() and not is_link(src):
            copies.discard(name)
            continue
        if dst.exists() or is_link(dst):
            if is_link(dst) and dst.exists():
                continue
            if is_link(dst):
                # Broken link (its target disappeared): repair it so setup
                # converges instead of silently skipping, matching status_check.
                if remove_link(dst):
                    warn(f"removed broken link {name}; relinking")
                else:
                    warn(f"{name} is a broken link that could not be removed; "
                         "not touching it")
                    continue
            elif os.path.isfile(dst) and os.path.isfile(src) \
                    and os.path.samefile(str(dst), str(src)):
                continue  # hardlink to the source; already fine
            elif name in copies and os.path.isfile(src) and os.path.isfile(dst):
                shutil.copy2(str(src), str(dst))
                info(f"refreshed: {name}")
                continue
            else:
                warn(f"{name} already exists as a real item in {data}; not touching it")
                continue
        try:
            method = make_link(src, dst)
            info(f"linked: {name} ({method})")
            if method == "copy":
                copies.add(name)
            else:
                copies.discard(name)
        except OSError as e:
            warn(f"could not link {name}: {e}")
    save_copies(data_dir, copies)


def plan_moves(session_list, target_projects):
    """Moves for sessions (jsonl + whole per-session dirs), plus the session ids."""
    moves = []
    session_ids = set()
    for proj_dir, session_id in session_list:
        session_ids.add(session_id)
        proj_name = Path(proj_dir).name
        moves.append((Path(proj_dir) / f"{session_id}.jsonl",
                      Path(target_projects) / proj_name / f"{session_id}.jsonl"))
        for sd in find_session_dirs(proj_dir, session_id):
            moves.append((Path(sd), Path(target_projects) / proj_name / Path(sd).name))
    return moves, session_ids


def memory_moves_for(lumo_sessions, target_projects):
    """Project memory/ dirs move only when EVERY session in the project is Lumo.

    A mixed project keeps its memory in the shared store (Claude sessions still use it);
    that is a documented limitation.
    """
    moves = []
    projects = {}
    for proj_dir, session_id in lumo_sessions:
        projects.setdefault(proj_dir, set()).add(session_id)
    for proj_dir, lumo_ids in projects.items():
        proj_path = Path(proj_dir)
        memory_src = proj_path / "memory"
        if not memory_src.is_dir():
            continue
        all_ids = {p.stem for p in proj_path.glob("*.jsonl")}
        if all_ids and all_ids <= lumo_ids:
            moves.append((memory_src, Path(target_projects) / proj_path.name / "memory"))
        else:
            warn(f"project {proj_path.name} mixes Claude and Lumo sessions; its memory stays put")
    return moves


def migrate_sessions(config_dir, shared_dir, dry_run=False, confirm=False):
    """Move Lumo sessions from the shared store to the data dir."""
    shared_projects = Path(shared_dir) / "projects"
    config_projects = Path(config_dir) / "projects"
    shared_history = Path(shared_dir) / "history.jsonl"
    config_history = Path(config_dir) / "history.jsonl"

    lumo_sessions = find_lumo_sessions(shared_projects)
    moves, session_ids = plan_moves(lumo_sessions, config_projects)
    memories = memory_moves_for(lumo_sessions, config_projects)
    keep_lines, moved_lines = split_history(shared_history, session_ids)

    print("=== Migration plan ===")
    print(f"Lumo sessions: {len(session_ids)}")
    print(f"Transcripts: {len([m for m in moves if m[0].suffix == '.jsonl'])}")
    print(f"Per-session dirs: {len([m for m in moves if m[0].suffix != '.jsonl'])}")
    print(f"Project memory dirs (all-Lumo projects only): {len(memories)}")
    print(f"history.jsonl lines: {len(moved_lines)}")

    if dry_run:
        print("\nDry run: nothing was moved. Re-run without --dry-run to proceed.")
        return
    if not lumo_sessions:
        info("nothing to migrate")
        return
    if not confirm:
        if input("Proceed with migration? (yes/no): ").strip().lower() != "yes":
            print("Migration cancelled")
            return

    # Merge new lines into any existing data-store history (a re-run adds the rest).
    existing = [l for l in read_history_lines(config_history) if l not in moved_lines]
    write_atomic(config_history, existing + moved_lines)
    write_atomic(shared_history, keep_lines)

    project_dirs = set()
    for src, dst in moves + memories:
        if move(str(src), str(dst)):
            info(f"moved: {src} -> {dst}")
        project_dirs.add(src.parent.parent if src.parent.name == "memory" else src.parent)

    # Clean up dirs the moves emptied (per-session dirs, then project dirs).
    for src, _ in moves:
        if src.suffix == ".jsonl":
            rmdir_if_empty(src.parent / src.stem)  # session dir emptied of siblings
    for pd in project_dirs:
        rmdir_if_empty(pd)

    info(f"history.jsonl: {len(moved_lines)} lines moved to {config_history}")


def revert_migration(config_dir, shared_dir, dry_run=False, confirm=False):
    """Move everything from the data store back to the shared store."""
    config_projects = Path(config_dir) / "projects"
    shared_projects = Path(shared_dir) / "projects"
    config_history = Path(config_dir) / "history.jsonl"
    shared_history = Path(shared_dir) / "history.jsonl"

    if not config_projects.is_dir():
        info("no sessions in the data store")
        return

    all_sessions = set()
    memory_back = []
    for proj_dir in config_projects.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            all_sessions.add((str(proj_dir), jsonl.stem))
        if (proj_dir / "memory").is_dir():
            memory_back.append((proj_dir / "memory", shared_projects / proj_dir.name / "memory"))

    moves, session_ids = plan_moves(all_sessions, shared_projects)
    keep_lines, moved_lines = split_history(shared_history, session_ids)
    existing = [l for l in read_history_lines(config_history) if l not in moved_lines]

    print("=== Revert plan ===")
    print(f"Sessions: {len(session_ids)}")
    print(f"Transcripts: {len([m for m in moves if m[0].suffix == '.jsonl'])}")
    print(f"Per-session dirs: {len([m for m in moves if m[0].suffix != '.jsonl'])}")
    print(f"Project memory dirs: {len(memory_back)}")

    if dry_run:
        print("\nDry run: nothing was moved. Re-run without --dry-run to proceed.")
        return
    if not all_sessions:
        info("nothing to revert")
        return
    if not confirm:
        if input("Proceed with revert? (yes/no): ").strip().lower() != "yes":
            print("Revert cancelled")
            return

    # History first: merge data-store lines back, deduplicated.
    write_atomic(shared_history, keep_lines + existing + moved_lines)

    for src, dst in moves + memory_back:
        if move(str(src), str(dst)):
            info(f"moved: {src} -> {dst}")

    for src, _ in moves:
        if src.suffix == ".jsonl":
            rmdir_if_empty(src.parent / src.stem)
    for src, _ in moves + memory_back:
        rmdir_if_empty(src.parent)
    rmdir_if_empty(config_projects)

    if config_history.exists():
        config_history.unlink()
    info(f"history.jsonl: {len(moved_lines) + len(existing)} lines restored to {shared_history}")


def status_check(config_dir, shared_dir):
    """Show whether separation is on, link health and session counts per store."""
    data_dir = Path(config_dir)
    marker = data_dir.parent / "separate-data.on"
    env_on = os.environ.get("CLAUDE_LUMO_SEPARATE_DATA") == "1"
    print(f"Separate data enabled: {'yes' if (marker.exists() or env_on) else 'no'}"
          f"{' (env)' if env_on and not marker.exists() else ''}")
    print(f"Data dir: {data_dir} ({'present' if data_dir.is_dir() else 'not created yet'})")

    def count_sessions(projects):
        n = 0
        projects = Path(projects)
        if projects.is_dir():
            for proj in projects.iterdir():
                if proj.is_dir():
                    n += len(list(proj.glob("*.jsonl")))
        return n

    print(f"Sessions in shared store: {count_sessions(Path(shared_dir) / 'projects')}")
    print(f"Sessions in data store: {count_sessions(data_dir / 'projects')}")

    if data_dir.is_dir():
        broken = [item.rstrip("/") for item in SHARED_ITEMS
                  if is_link(data_dir / item.rstrip("/")) and not os.path.exists(data_dir / item.rstrip("/"))]
        if broken:
            warn(f"broken links: {', '.join(broken)}")
        else:
            print("Links: healthy")


def main():
    parser = argparse.ArgumentParser(description="Optional data separation for claude-lumo")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--config-dir", default=str(Path.home() / ".claude-lumo" / "data"))
        p.add_argument("--shared", default=str(Path.home() / ".claude"))

    setup_p = sub.add_parser("setup", help="Build/repair the link layout")
    add_common(setup_p)
    status_p = sub.add_parser("status", help="Show separation status")
    add_common(status_p)
    mig_p = sub.add_parser("migrate", help="Move Lumo sessions to the data store")
    add_common(mig_p)
    mig_p.add_argument("--dry-run", action="store_true")
    mig_p.add_argument("--yes", action="store_true")
    rev_p = sub.add_parser("revert-migrate", help="Move sessions back to the shared store")
    add_common(rev_p)
    rev_p.add_argument("--dry-run", action="store_true")
    rev_p.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    shared, config_dir = Path(args.shared), Path(args.config_dir)

    if args.command == "setup":
        setup_links(shared, config_dir)
    elif args.command == "migrate":
        migrate_sessions(config_dir, shared, args.dry_run, args.yes)
    elif args.command == "revert-migrate":
        revert_migration(config_dir, shared, args.dry_run, args.yes)
    elif args.command == "status":
        status_check(config_dir, shared)


if __name__ == "__main__":
    main()
