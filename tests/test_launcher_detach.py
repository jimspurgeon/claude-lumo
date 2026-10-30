"""claude-lumo.sh on Windows starts the proxy and LiteLLM through service_restart.py --start, so
they run detached and outlive the terminal that launched them (#164). A fake curl reports each
service down until the fake service_restart.py has "started" it; a failing render.py ends the
launch right after the services step, before anything real runs."""
import os
import subprocess
import sys

import pytest

from test_revert import BASH, HERE

FAKE_RESTART = """import os, sys
state = os.environ["CLAUDE_LUMO_STATE_DIR"]
with open(os.path.join(state, "calls.txt"), "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
open(os.path.join(state, "up-" + sys.argv[-1]), "w").close()
"""

FAKE_CURL = """#!/bin/bash
url="${@: -1}"
case "$url" in
  *:8790/*) f="$FAKE_STATE/up-proxy";;
  *:4000/*) f="$FAKE_STATE/up-litellm";;
esac
[ -f "$f" ] && printf 200 || printf 000
"""


@pytest.mark.skipif(sys.platform != "win32", reason="the detached start is the Windows path")
def test_windows_launcher_starts_services_detached(tmp_path):
    lumo = tmp_path / ".claude" / "lumo"
    (lumo / "proxy").mkdir(parents=True)
    (lumo / "proxy" / "proxy.mjs").write_text("", encoding="utf-8")
    (lumo / "service_restart.py").write_text(FAKE_RESTART, encoding="utf-8")
    (lumo / "render.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL, encoding="utf-8", newline="\n")
    env = dict(os.environ, HOME=str(tmp_path), CLAUDE_LUMO_STATE_DIR=str(state), FAKE_STATE=str(state),
               PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], CLAUDE_LUMO_UPDATE_CHECK="0")
    proc = subprocess.run([BASH, os.path.join(HERE, "claude-lumo.sh")], env=env, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert "render.py failed" in proc.stderr, proc.stdout + proc.stderr
    calls = (state / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert [c.split()[-2:] for c in calls] == [["--start", "proxy"], ["--start", "litellm"]]
    assert all("--install-dir" in c for c in calls)


@pytest.mark.skipif(sys.platform != "win32", reason="the detached start is the Windows path")
def test_windows_launcher_leaves_healthy_services_alone(tmp_path):
    lumo = tmp_path / ".claude" / "lumo"
    (lumo / "proxy").mkdir(parents=True)
    (lumo / "service_restart.py").write_text(FAKE_RESTART, encoding="utf-8")
    (lumo / "render.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "up-proxy").write_text("", encoding="utf-8")
    (state / "up-litellm").write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL, encoding="utf-8", newline="\n")
    env = dict(os.environ, HOME=str(tmp_path), CLAUDE_LUMO_STATE_DIR=str(state), FAKE_STATE=str(state),
               PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], CLAUDE_LUMO_UPDATE_CHECK="0")
    proc = subprocess.run([BASH, os.path.join(HERE, "claude-lumo.sh")], env=env, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert "render.py failed" in proc.stderr, proc.stdout + proc.stderr
    assert not (state / "calls.txt").exists()
