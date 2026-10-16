#!/usr/bin/env python
"""ci-watcher: poll GitHub Actions for failed runs and launch headless fix attempts.

Part of the claude-lumo capability layer (P2 T5). Zero dependencies beyond Python 3.12.

What it does, one poll cycle:
  1. For every repo in the config file (TOML, scripts/watch-ci.toml), list recent
     GitHub Actions runs via `gh run list --json` and find failed ones.
  2. For a failed run not seen before (handled run ids live in a state file under
     the Lumo state dir, never in the repo), and while the throttle allows it,
     launch one headless fix attempt:
       - a fresh Orca worktree (orca worktree create), falling back to a plain
         `git worktree add` under the system temp dir when orca is unavailable;
       - `claude-lumo -p <prompt>` in that worktree. The prompt (see build_prompt)
         carries the autonomy contract: read the failed log with gh, diagnose, fix,
         then open a PR with a merge-danger section, or park by opening an issue
         labeled parked-needs-human when not confident. It must never block on a
         question.
  3. Every event (poll, detection, throttle, fix attempt, outcome) is appended to
     the journal as a dated line: <journal dir>/ci-watcher-YYYY-MM-DD.log.

Throttle, enforced here in the script (not in the prompt): at most 3 autonomous
fix attempts per night (18:00-06:00 local) and 3 per calendar day. Attempt dates
are kept in the state file. When the throttle trips, the run is logged, marked
handled and skipped.

`--report` (morning mode) reads the last 24h of journal lines and writes a single
markdown summary to <reports dir>/YYYY-MM-DD.md, then tries a BurntToast desktop
notification (silently skipped when BurntToast is not installed).

Usage:
    watch_ci.py --once --dry-run [--config PATH]   # single cycle, detect only
    watch_ci.py --interval 300                     # loop, one cycle every 300 s
    watch_ci.py --report                           # write today's morning report

Defaults are in DEFAULT_* below; the journal and report dirs can be redirected
with WATCH_CI_JOURNAL_DIR / WATCH_CI_REPORTS_DIR for trial runs.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta

# Where logs and reports go. Redirect with env vars for trial runs.
DEFAULT_JOURNAL_DIR = os.environ.get(
    "WATCH_CI_JOURNAL_DIR",
    os.path.join("~", "life", "archive", "journal"),
)
DEFAULT_REPORTS_DIR = os.environ.get(
    "WATCH_CI_REPORTS_DIR",
    os.path.join("~", "life", "archive", "ci-reports"),
)
DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "watch-ci.toml"
)
DEFAULT_CLAUDE_LUMO = "claude-lumo"

# Throttle: at most this many autonomous fix attempts per night window and per
# calendar day. Enforced by record_attempt / throttle_allows, never by the prompt.
MAX_PER_NIGHT = 3
MAX_PER_DAY = 3
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 6

# State file name (inside the Lumo state dir, never the repo).
STATE_FILE_NAME = "ci-watcher-state.json"
# Keep handled-run history bounded; a run id far older than this is pruned.
STATE_MAX_RUNS = 500

LOG_PREFIXES = {
    "poll": "POLL",
    "detected": "DETECTED",
    "throttled": "THROTTLED",
    "attempt": "ATTEMPT",
    "outcome": "OUTCOME",
    "cleanup": "CLEANUP",
    "error": "ERROR",
}


def resolve_state_dir():
    """Lumo state folder: CLAUDE_LUMO_STATE_DIR if set, else ~/.claude-lumo."""
    env_dir = os.environ.get("CLAUDE_LUMO_STATE_DIR")
    return os.path.abspath(os.path.expanduser(env_dir or "~/.claude-lumo"))


def journal_dir():
    """Journal folder, overridable with WATCH_CI_JOURNAL_DIR for trial runs."""
    return os.path.abspath(os.path.expanduser(DEFAULT_JOURNAL_DIR))


def reports_dir():
    """Report folder, overridable with WATCH_CI_REPORTS_DIR for trial runs."""
    return os.path.abspath(os.path.expanduser(DEFAULT_REPORTS_DIR))


def journal_log(now=None):
    """Dated log file name for a given moment: ci-watcher-YYYY-MM-DD.log."""
    now = now or datetime.now()
    return os.path.join(journal_dir(), "ci-watcher-%s.log" % now.strftime("%Y-%m-%d"))


def log_event(kind, message, now=None):
    """Append one dated journal line. Kind must be a key of LOG_PREFIXES."""
    now = now or datetime.now()
    prefix = LOG_PREFIXES.get(kind, kind.upper())
    path = journal_log(now)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "%s %s %s\n" % (now.strftime("%Y-%m-%d %H:%M:%S"), prefix, message)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return line


# ---------------------------------------------------------------------------
# Config


def load_config(path):
    """Parse the TOML config.

    Shape:
        [[repos]]
        repo = "jimspurgeon/claude-lumo"   # owner/name
        branch = "master"                  # optional
        path = "C:/Users/Jim/orca/claude-lumo"  # local clone for worktrees

    Returns a list of dicts with keys repo, branch (may be None), path.
    """
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    out = []
    for entry in data.get("repos", []):
        repo = entry.get("repo")
        if not repo or "/" not in repo:
            raise ValueError("config repo entry needs owner/name: %r" % (entry,))
        out.append(
            {
                "repo": repo,
                "branch": entry.get("branch"),
                "path": entry.get("path"),
            }
        )
    if not out:
        raise ValueError("config lists no repos: %s" % path)
    return out


# ---------------------------------------------------------------------------
# State: handled run ids and throttle counters


def state_path(state_dir=None):
    state_dir = state_dir or resolve_state_dir()
    return os.path.join(state_dir, STATE_FILE_NAME)


def load_state(path):
    """Read the state file; missing or corrupt files yield a fresh state."""
    if not os.path.exists(path):
        return {"runs": [], "attempts": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log_event(
            "error",
            "state file %s unreadable (%s); resetting handled run history" % (path, exc),
        )
        return {"runs": [], "attempts": []}
    if not isinstance(data, dict):
        log_event(
            "error",
            "state file %s has unexpected shape; resetting handled run history" % path,
        )
        return {"runs": [], "attempts": []}
    data.setdefault("runs", [])
    data.setdefault("attempts", [])
    return data


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if len(state["runs"]) > STATE_MAX_RUNS:
        state["runs"] = state["runs"][-STATE_MAX_RUNS:]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def run_key(repo, run_id):
    """A handled-run id is scoped to the repo it belongs to."""
    return "%s#%s" % (repo, run_id)


def mark_run_handled(state, repo, run_id):
    key = run_key(repo, run_id)
    if key not in state["runs"]:
        state["runs"].append(key)


def run_is_new(state, repo, run_id):
    return run_key(repo, run_id) not in state["runs"]


# ---------------------------------------------------------------------------
# Throttle (window computation, pure: testable without clocks or files)


def night_bounds(day):
    """The night window containing an evening moment: 18:00 that day to 06:00 next.

    Returns (start, end) datetimes. A time between 18:00 and midnight belongs to
    the night starting that evening; a time between midnight and 06:00 belongs to
    the night that started the previous evening.
    """
    if day.hour >= NIGHT_START_HOUR:
        start = day.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=12)
    if day.hour < NIGHT_END_HOUR:
        start = (day - timedelta(days=1)).replace(
            hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0
        )
        return start, start + timedelta(hours=12)
    # Daytime: no night window contains this moment.
    return None


def is_night(moment):
    bounds = night_bounds(moment)
    return bounds is not None


def attempts_in_window(attempts, window):
    """Count recorded attempt timestamps that fall inside (start, end)."""
    if window is None:
        return 0
    start, end = window
    count = 0
    for ts in attempts:
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if start <= when < end:
            count += 1
    return count


def attempts_that_day(attempts, day):
    """Count recorded attempt timestamps on the same calendar date as day."""
    count = 0
    for ts in attempts:
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if when.date() == day.date():
            count += 1
    return count


def throttle_allows(state, now):
    """True when one more autonomous fix attempt may start at `now`."""
    if attempts_that_day(state["attempts"], now) >= MAX_PER_DAY:
        return False
    if is_night(now):
        window = night_bounds(now)
        if attempts_in_window(state["attempts"], window) >= MAX_PER_NIGHT:
            return False
    return True


def throttle_reason(state, now):
    if is_night(now):
        window = night_bounds(now)
        if attempts_in_window(state["attempts"], window) >= MAX_PER_NIGHT:
            return "night limit reached (%d)" % MAX_PER_NIGHT
    if attempts_that_day(state["attempts"], now) >= MAX_PER_DAY:
        return "daily limit reached (%d)" % MAX_PER_DAY
    return None


def record_attempt(state, now):
    state["attempts"].append(now.isoformat())


# ---------------------------------------------------------------------------
# Command runner seam: tests substitute these to fake gh/orca/claude.


def run_command(argv, cwd=None, timeout=None):
    """Run a subprocess, return (returncode, stdout+stderr text). Never raises."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


# ---------------------------------------------------------------------------
# Polling


def list_failed_runs(conf_repo, runner=None):
    """Return [(run_id, branch, title)] for recent failed runs of one repo."""
    runner = runner or run_command
    argv = [
        "gh",
        "run",
        "list",
        "--repo",
        conf_repo["repo"],
        "--json",
        "databaseId,conclusion,headBranch,displayTitle",
        "--limit",
        "30",
    ]
    code, out = runner(argv)
    if code != 0:
        raise RuntimeError("gh run list failed for %s: %s" % (conf_repo["repo"], out.strip()))
    runs = json.loads(out)
    failed = []
    for run in runs:
        if run.get("conclusion") == "failure":
            failed.append(
                (
                    str(run["databaseId"]),
                    run.get("headBranch") or "",
                    run.get("displayTitle") or "",
                )
            )
    return failed


# ---------------------------------------------------------------------------
# Fix attempts


def create_worktree(conf_repo, run_id, runner=None):
    """Create a fresh worktree for a fix attempt; return its path or None."""
    runner = runner or run_command
    local = conf_repo.get("path")
    name = "ci-fix-%s" % run_id[:8]
    if shutil.which("orca") and local:
        argv = [
            "orca",
            "worktree",
            "create",
            "--name",
            name,
            "--repo",
            "path:%s" % local,
            "--setup",
            "skip",
            "--json",
        ]
        code, out = runner(argv, timeout=120)
        if code == 0:
            try:
                data = json.loads(out)
                for key in ("path", "worktreePath"):
                    found = _find_path_in(data, key)
                    if found:
                        return found
            except ValueError:
                pass
        # Fall through to the git fallback.
    if not local:
        return None
    target = os.path.join(
        tempfile_dir(), "%s-%s" % (name, run_id[:8])
    )
    argv = ["git", "-C", local, "worktree", "add", target]
    code, _out = runner(argv, timeout=120)
    if code == 0:
        return target
    return None


def _find_path_in(obj, key):
    """Depth-first search for obj[key] holding an existing-looking path string."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v:
                return v
            found = _find_path_in(v, key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_path_in(item, key)
            if found:
                return found
    return None


def tempfile_dir():
    import tempfile

    return tempfile.gettempdir()


def build_prompt(conf_repo, run_id):
    """The autonomy contract sent to the headless fix session.

    The contract lives here in the prompt text: never block on a question;
    park with the parked-needs-human label when not confident.
    """
    return (
        "You are an autonomous CI fixer. CI run {run} failed on {repo}. "
        "Work in the current directory (a fresh worktree of this repo). "
        "AUTONOMY CONTRACT: you are running headless with nobody to answer questions. "
        "Never block on a question and never wait for input; decide, or park. "
        "Steps: 1) Read the failing CI log: gh run view {run} --repo {repo} --log-failed. "
        "2) Diagnose the failure and fix it. "
        "3) If you are confident the fix is correct: run the relevant tests, "
        "commit and open a pull request whose body contains a MERGE DANGER section "
        "stating whether this is a one-way or two-way door and the blast radius. "
        "4) If you are NOT confident: do not force a fix. Create the label "
        "parked-needs-human first if it does not exist "
        "(gh label create parked-needs-human --repo {repo} "
        "--description 'Autonomous fixer parked this; needs a human' || true), "
        "then open an issue describing the failing run {run}, the likely cause, "
        "your analysis and what you would try next, and give it that label. "
        "In either case finish with a one-paragraph summary of what you did. "
        "Do not merge anything and do not delete anything."
    ).format(run=run_id, repo=conf_repo["repo"])


def resolve_claude_cmd(which=None, default=DEFAULT_CLAUDE_LUMO):
    """Resolve the claude-lumo executable to a form shell=False can launch.

    On Windows, `claude-lumo` on PATH is an npm .cmd shim; passing the bare
    name with shell=False raises FileNotFoundError. shutil.which() resolves it
    to an absolute .cmd/.bat path that CreateProcess can execute directly
    (verified empirically on this machine). Pure function: which is injectable.
    """
    which = which or shutil.which
    resolved = which(default)
    if resolved:
        # Absolute path to the shim; subprocess handles .cmd/.bat directly.
        return resolved
    # Not on PATH: fall back to the bare name (works on POSIX).
    return default


def launch_fix(conf_repo, run_id, worktree, claude_cmd=None, runner=None):
    """Run the headless claude-lumo fix session in the worktree.

    Returns (returncode, output). claude_cmd defaults to the resolved
    claude-lumo executable (see resolve_claude_cmd).
    """
    runner = runner or run_command
    claude_cmd = claude_cmd or resolve_claude_cmd()
    prompt = build_prompt(conf_repo, run_id)
    argv = [claude_cmd, "-p", prompt]
    code, out = runner(argv, cwd=worktree, timeout=3600)
    return code, out


# ---------------------------------------------------------------------------
# Report


def read_journal_lines(dir_path, since, until=None):
    """Collect journal lines from the dated files covering [since, until)."""
    lines = []
    day = since.date() - timedelta(days=1)
    last = until.date() if until else datetime.now().date()
    while day <= last:
        path = os.path.join(
            dir_path, "ci-watcher-%s.log" % day.strftime("%Y-%m-%d")
        )
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                lines.extend(fh.read().splitlines())
        day += timedelta(days=1)
    return lines


def parse_journal_line(line):
    """Split 'YYYY-MM-DD HH:MM:SS KIND message' into (datetime, kind, message)."""
    parts = line.split(" ", 3)
    if len(parts) < 4:
        return None
    try:
        when = datetime.strptime(parts[0] + " " + parts[1], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return when, parts[2], parts[3]


def render_report(lines, day_label, now=None):
    """Render the last-24h journal lines as a markdown morning report."""
    now = now or datetime.now()
    cutoff = now - timedelta(hours=24)
    polls = detected = throttled = attempts = outcomes = 0
    details = []
    for line in lines:
        parsed = parse_journal_line(line)
        if not parsed:
            continue
        when, kind, message = parsed
        if when < cutoff:
            continue
        if kind == "POLL":
            polls += 1
        elif kind == "DETECTED":
            detected += 1
            details.append("- Failed run: %s" % message)
        elif kind == "THROTTLED":
            throttled += 1
            details.append("- Throttled: %s" % message)
        elif kind == "ATTEMPT":
            attempts += 1
            details.append("- Fix attempt: %s" % message)
        elif kind == "OUTCOME":
            outcomes += 1
            details.append("- Outcome: %s" % message)
        elif kind == "ERROR":
            details.append("- Error: %s" % message)
    out = []
    out.append("# CI watcher report %s" % day_label)
    out.append("")
    out.append("Last 24 hours (to %s):" % now.strftime("%Y-%m-%d %H:%M"))
    out.append("")
    out.append("| Metric | Count |")
    out.append("|---|---|")
    out.append("| Poll cycles | %d |" % polls)
    out.append("| Failed runs detected | %d |" % detected)
    out.append("| Fix attempts | %d |" % attempts)
    out.append("| Outcomes recorded | %d |" % outcomes)
    out.append("| Items throttled | %d |" % throttled)
    out.append("")
    out.append("## Detail")
    out.append("")
    if details:
        out.extend(details)
    else:
        out.append("(no failed runs, fix attempts, throttle events or errors)")
    out.append("")
    return "\n".join(out)


def ps_quote(value):
    """Escape a value for a single-quoted PowerShell string literal ('' per ')."""
    return value.replace("'", "''")


def notify_desktop(title, message, runner=None):
    """BurntToast notification; silently skipped when unavailable or failing."""
    runner = runner or run_command
    try:
        code, out = runner(
            ["powershell", "-NoProfile", "-Command", "Get-Module -ListAvailable BurntToast"]
        )
    except Exception:
        return False
    if code != 0 or "BurntToast" not in out:
        return False
    command = "New-BurntToastNotification -Text '%s', '%s'" % (
        ps_quote(title),
        ps_quote(message),
    )
    try:
        code, _out = runner(["powershell", "-NoProfile", "-Command", command], timeout=30)
        return code == 0
    except Exception:
        return False


def write_report(now=None, runner=None):
    """Morning mode: write today's markdown report; return its path."""
    now = now or datetime.now()
    day_label = now.strftime("%Y-%m-%d")
    lines = read_journal_lines(journal_dir(), now - timedelta(hours=24), now)
    body = render_report(lines, day_label, now=now)
    out_dir = reports_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s.md" % day_label)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    notify_desktop("CI watcher report ready", day_label, runner=runner)
    return path


# ---------------------------------------------------------------------------
# Cycle


def poll_cycle(config, state, dry_run=False, runner=None, now=None):
    """One poll cycle over every configured repo. Mutates and saves state."""
    now = now or datetime.now()
    runner = runner or run_command
    path = state_path()
    for conf_repo in config:
        repo = conf_repo["repo"]
        try:
            failed = list_failed_runs(conf_repo, runner=runner)
        except (RuntimeError, ValueError) as exc:
            log_event("error", "%s: %s" % (repo, exc), now=now)
            continue
        log_event("poll", "%s: %d failed run(s)" % (repo, len(failed)), now=now)
        for run_id, branch, title in failed:
            if conf_repo["branch"] and branch != conf_repo["branch"]:
                continue
            if not run_is_new(state, repo, run_id):
                continue
            mark_run_handled(state, repo, run_id)
            # Persist at once: a fix attempt below can run for an hour, and a crash
            # mid-attempt must not make the same run look new on the next cycle.
            save_state(path, state)
            log_event(
                "detected",
                "%s run %s (%s): %s" % (repo, run_id, branch, title),
                now=now,
            )
            if dry_run:
                log_event("outcome", "%s run %s: dry run, no fix attempted" % (repo, run_id), now=now)
                continue
            reason = throttle_reason(state, now)
            if reason:
                log_event("throttled", "%s run %s: skipped, %s" % (repo, run_id, reason), now=now)
                continue
            record_attempt(state, now)
            save_state(path, state)
            # NOTE: record_attempt runs before worktree creation, so a setup
            # failure still consumes one throttle slot. That ordering is
            # deliberate: an hour of repeated worktree-creation failures (disk
            # full, orca broken) must not spin through unlimited attempts, and
            # the run is already marked handled so it is never retried anyway.
            # Conservative on budget, safe against setup-failure loops.
            worktree = create_worktree(conf_repo, run_id, runner=runner)
            if not worktree:
                log_event(
                    "outcome",
                    "%s run %s: could not create a worktree (no local path?)"
                    % (repo, run_id),
                    now=now,
                )
                continue
            log_event(
                "attempt",
                "%s run %s in %s" % (repo, run_id, worktree),
                now=now,
            )
            try:
                code, out = launch_fix(conf_repo, run_id, worktree, runner=runner)
            except Exception as exc:  # a failed fix attempt must never kill the loop
                log_event("outcome", "%s run %s: launch error %s" % (repo, run_id, exc), now=now)
                cleanup_worktree(worktree, conf_repo, runner=runner, now=now)
                continue
            log_event(
                "outcome",
                "%s run %s: exit %s in %s" % (repo, run_id, code, worktree),
                now=now,
            )
            cleanup_worktree(worktree, conf_repo, runner=runner, now=now)
    save_state(path, state)


def cleanup_worktree(worktree, conf_repo, runner=None, now=None):
    """Best-effort removal of a fix-attempt worktree after the attempt ends.

    Failures are swallowed and logged: cleanup must never mask the attempt's
    own outcome. Uses the same runner seam as everything else.
    """
    if not worktree:
        return
    runner = runner or run_command
    local = conf_repo.get("path")
    now = now or datetime.now()
    try:
        if local:
            removed = False
            if shutil.which("orca"):
                # Orca-managed worktree: going through `git worktree remove`
                # would leave Orca's records stale, so try the orca CLI first
                # (best-effort; create_worktree falls back to plain git when
                # orca create fails, so a failed orca rm falls back to git too).
                argv = ["orca", "worktree", "rm", "--worktree", "path:%s" % worktree]
                code, out = runner(argv, timeout=120)
                removed = code == 0
            if not removed:
                argv = ["git", "-C", local, "worktree", "remove", "--force", worktree]
                code, out = runner(argv, timeout=120)
                if code != 0:
                    log_event(
                        "cleanup",
                        "could not remove worktree %s (%s)"
                        % (worktree, (out or "").strip()[:200]),
                        now=now,
                    )
        else:
            log_event(
                "cleanup",
                "no local path; worktree %s left in place" % worktree,
                now=now,
            )
            return
    except Exception as exc:
        log_event("cleanup", "worktree cleanup error %s: %s" % (worktree, exc), now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Watch GitHub Actions and launch headless claude-lumo fix attempts."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="TOML config path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="single poll cycle (default)")
    mode.add_argument("--report", action="store_true", help="write the morning report")
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help="loop with one poll cycle every SECONDS",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="detect failed runs but do not launch fixes",
    )
    args = parser.parse_args(argv)

    if args.report:
        path = write_report()
        print(path)
        return 0

    try:
        config = load_config(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print("watch-ci: cannot load config %s: %s" % (args.config, exc), file=sys.stderr)
        return 1

    state = load_state(state_path())
    if args.interval > 0:
        while True:
            poll_cycle(config, state, dry_run=args.dry_run)
            time.sleep(args.interval)
    else:
        poll_cycle(config, state, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
