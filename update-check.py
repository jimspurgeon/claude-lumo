"""Silent periodic update check for claude-lumo.

Checks whether a newer release tag (v*) exists on the remote. Runs in the background from
the launcher so it never delays startup, and never prints anything: on success or failure
it just exits 0. If a newer release exists, it writes a one-line notice to
<state-dir>/update-notice.txt, which the status line then shows until the next update.

Env knobs (the last two exist so tests can point elsewhere):
    CLAUDE_LUMO_UPDATE_CHECK: 0/false/no disables the check entirely.
    CLAUDE_LUMO_UPDATE_CHECK_INTERVAL: hours between checks (default 24).
    CLAUDE_LUMO_UPDATE_REMOTE: git URL or path of the remote to ask (default: this repo).
    CLAUDE_LUMO_STATE_DIR: state directory (default ~/.claude-lumo).

State lives in <state-dir>/update-check-state.json: last_check (ISO timestamp, for the
rate limit) and notified_version (the tag the notice file was written for, so each
version is offered once). A failed or offline check does not update last_check, so the
next launch simply tries again.

Usage (from the launcher):
    python update-check.py <repo-path> &
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO_URL = "https://github.com/jimspurgeon/claude-lumo.git"
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
TIMEOUT_SECONDS = 5


def state_dir():
    return os.environ.get("CLAUDE_LUMO_STATE_DIR") or str(Path.home() / ".claude-lumo")


def remote():
    return os.environ.get("CLAUDE_LUMO_UPDATE_REMOTE") or REPO_URL


def interval_hours():
    return int(os.environ.get("CLAUDE_LUMO_UPDATE_CHECK_INTERVAL", "24"))


def disabled():
    return os.environ.get("CLAUDE_LUMO_UPDATE_CHECK", "").lower() in ("0", "false", "no")


def load_state(state_file):
    """Load cached check state. Corrupt or missing file is treated as empty state."""
    try:
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state_file, state):
    """Save check state atomically (temp file then os.replace, like render.py)."""
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(state_file))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(state, f)
            os.replace(tmp, state_file)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def parse_version(tag):
    """Parse v1.2.3 into the tuple (1, 2, 3) for comparison. None if not a release tag."""
    m = VERSION_RE.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def latest_release_tag(repo_remote):
    """Newest v* tag on the remote, via git ls-remote (never touches the local clone).

    None on any failure (offline, timeout, bad exit) or when no release tag exists.
    Peeled refs (ending in ^{}) are ignored.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", repo_remote, "refs/tags/v*"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None
    tags = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1].endswith("^{}"):
            continue
        tag = parts[1][len("refs/tags/"):]
        if VERSION_RE.match(tag):
            tags.append(tag)
    best = max(tags, key=parse_version, default=None)
    return best


def installed_tag(repo):
    """Tag the local clone is checked out at, or None (untagged HEAD, timeout, not a clone)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
        tag = result.stdout.strip() if result.returncode == 0 else ""
        return tag if VERSION_RE.match(tag) else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def notice_line(version):
    return f"claude-lumo {version} available: run `claude-lumo update`"


def main(argv):
    try:
        if len(argv) < 2 or not os.path.isdir(argv[1]):
            return
        if disabled():
            return

        sdir = state_dir()
        state_file = os.path.join(sdir, "update-check-state.json")
        notice_file = os.path.join(sdir, "update-notice.txt")
        state = load_state(state_file)

        # Rate limit: one check per interval. A corrupt timestamp is ignored, not fatal.
        last = state.get("last_check")
        if last:
            try:
                if datetime.now() < datetime.fromisoformat(last) + timedelta(hours=interval_hours()):
                    return
            except (ValueError, TypeError):
                pass

        latest = latest_release_tag(remote())
        if not latest:
            return  # Offline or no release tags: stay silent and retry next launch.

        inst = installed_tag(argv[1])
        if not inst or parse_version(inst) >= parse_version(latest):
            # Up to date, or a dev checkout (HEAD not at a release tag): never offer.
            state["last_check"] = datetime.now().isoformat()
            save_state(state_file, state)
            return

        if state.get("notified_version") != latest:
            try:
                os.makedirs(sdir, exist_ok=True)
                with open(notice_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(notice_line(latest) + "\n")
            except OSError:
                pass
            state["notified_version"] = latest
        state["last_check"] = datetime.now().isoformat()
        save_state(state_file, state)
    except Exception:
        pass  # The check must never disturb a launch.


if __name__ == "__main__":
    sys.exit(main(sys.argv))
