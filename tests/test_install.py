"""Tests for install.sh, run in a sandbox: temporary HOME, APPDATA and state dir, a fake `claude`,
a pre-made LiteLLM venv so nothing is downloaded, and a PATH with `python3` but no `python`."""
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = platform.system() == "Windows"
# On Windows a bare "bash" resolves to System32's WSL launcher before PATH is searched (#91).
BASH = shutil.which("bash") or "bash"


def to_posix(path):
    """Path as bash sees it (Git Bash wants /c/... on Windows)."""
    if not WINDOWS:
        return str(path)
    return subprocess.run([BASH, "-c", 'cygpath -u "$1"', "_", str(path)],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def sandbox(tmp_path):
    home, appdata, state, fakebin = (tmp_path / n for n in ("home", "appdata", "state", "fakebin"))
    for d in (home, appdata, state, fakebin):
        d.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    # A stale version cache in the custom state dir: install.sh must clear it there (#54).
    (state / "statusline-version.cache").write_text("v0.0.1", encoding="utf-8")
    # Pre-made venv so install.sh skips `python -m venv` and the pip install.
    litellm = state / "litellm-venv" / ("Scripts/litellm.exe" if WINDOWS else "bin/litellm")
    litellm.parent.mkdir(parents=True)
    litellm.write_text("#!/bin/sh\n", encoding="utf-8")
    litellm.chmod(0o755)
    # Only python3 exists, as on many Linux and macOS systems (#32).
    for name, body in (("python3", f'exec "{to_posix(sys.executable)}" "$@"'), ("claude", "exit 0")):
        f = fakebin / name
        f.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
        f.chmod(0o755)
    return {"home": home, "appdata": appdata, "state": state, "fakebin": fakebin}


def run_install(sb):
    tool_dirs = [sb["fakebin"]] + [Path(shutil.which(t)).parent for t in ("node", "git")]
    path = ":".join([to_posix(d) for d in tool_dirs] + ["/usr/bin", "/bin"])
    env = dict(os.environ, HOME=str(sb["home"]), APPDATA=str(sb["appdata"]),
               CLAUDE_LUMO_STATE_DIR=str(sb["state"]), CLAUDE_LUMO_PLAIN_OFFER="1", LUMO_PATH=path)
    probe = subprocess.run([BASH, "-c", 'PATH="$LUMO_PATH"; command -v python'],
                           env=env, capture_output=True, text=True)
    if probe.returncode == 0:
        pytest.skip(f"`python` is in a system directory ({probe.stdout.strip()}); cannot hide it")
    return subprocess.run([BASH, "-c", 'PATH="$LUMO_PATH"; exec bash "$1"', "_",
                           to_posix(ROOT / "install.sh")],
                          env=env, capture_output=True, text=True, timeout=300)


def node_major():
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


# Below Node 22 install.sh installs Node system-wide (winget, or curl | sudo on Linux): never in a test.
@pytest.mark.skipif(not (shutil.which("node") and shutil.which("git") and shutil.which("bash"))
                    or node_major() < 22, reason="needs node 22+, git and bash")
def test_install_honours_state_dir_and_python3(sandbox):
    r = run_install(sandbox)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    state, home = sandbox["state"], sandbox["home"]
    # #54: state goes to CLAUDE_LUMO_STATE_DIR, never to the default ~/.claude-lumo.
    assert (state / "master.key").read_text(encoding="utf-8").startswith("sk-lumo-")
    assert not (state / "statusline-version.cache").exists()
    assert not (home / ".claude-lumo").exists()
    # #32: the plain-Claude opt-in hook uses the interpreter install.sh found, not `python`.
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = [h["command"] for e in settings["hooks"]["SessionStart"] for h in e["hooks"]]
    assert len(cmds) == 1 and cmds[0].startswith("python3 "), cmds
