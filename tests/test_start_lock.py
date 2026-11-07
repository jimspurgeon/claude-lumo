"""The launcher's start lock (#165): when two sessions start at once, only the launch that wins
the mkdir spawns LiteLLM; the loser waits for health and never spawns its own litellm.

The fake `curl` must actually be picked up by the launcher, so on Windows this uses Git's
usr/bin/bash: Git/bin/bash re-prepends /mingw64/bin to PATH inside the child, and the REAL
curl there answers the REAL gateway's health endpoints (test environments often have them
running), which would silently flip every verdict in this suite.
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_CURL = """#!/bin/bash
url="${@: -1}"
case "$url" in
  *:8790/*) printf 200;;                       # proxy always healthy: not under test
  *:4000/*) [ -f "$FAKE_STATE/litellm-up" ] && printf 200 || printf 000;;
esac
"""


def _bash():
    """A bash whose PATH does not get /mingw64/bin force-prepended (see module docstring)."""
    bash = shutil.which("bash") or "bash"
    if platform.system() == "Windows" and "Git\\bin\\bash" in bash:
        usr = bash.replace("Git\\bin\\bash", "Git\\usr\\bin\\bash")
        if os.path.exists(usr):
            return usr
    return bash


BASH = _bash()


def launcher_env(home, state, bin_dir):
    return dict(os.environ, HOME=str(home), CLAUDE_LUMO_STATE_DIR=str(state), FAKE_STATE=str(state),
                PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], CLAUDE_LUMO_UPDATE_CHECK="0",
                CLAUDE_LUMO_WATCHDOG="0")


def make_install(home, state, bin_dir):
    """A minimal ~/.claude/lumo: a gateway dir, a failing render.py (ends the launch right
    after the services step), a fake litellm that records its spawn, and a fake curl."""
    lumo = home / ".claude" / "lumo"
    (lumo / "gateway").mkdir(parents=True)
    (lumo / "gateway" / "litellm.yaml").write_text("", encoding="utf-8")
    (lumo / "claude-lumo.sh").write_text(
        Path(HERE, "claude-lumo.sh").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    (lumo / "render.py").write_text("import sys; sys.exit(1)\n", encoding="utf-8")
    venv = state / "litellm-venv" / ("Scripts" if sys.platform == "win32" else "bin")
    venv.mkdir(parents=True)
    litellm = venv / ("litellm.exe" if sys.platform == "win32" else "litellm")
    litellm.write_text(
        '#!/bin/sh\n'
        'echo "spawn $$" >> "$FAKE_STATE/spawns.txt"\n'
        'touch "$FAKE_STATE/litellm-up"\n',
        encoding="utf-8", newline="\n")
    litellm.chmod(0o755)
    (state / "master.key").write_text("sk-lumo-test\n", encoding="utf-8")
    curl = bin_dir / ("curl.exe" if sys.platform == "win32" else "curl")
    curl.write_text(FAKE_CURL, encoding="utf-8", newline="\n")
    curl.chmod(0o755)


def run_launcher(env, extra_argv=()):
    return subprocess.run([BASH, os.path.join(HERE, "claude-lumo.sh"), *extra_argv], env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120, stdin=subprocess.DEVNULL)


def test_health_check_via_fake_curl_and_install(tmp_path):
    """Sanity of the harness itself (fails loudly if the environment hides the fake curl)."""
    home, state, bin_dir = (tmp_path / n for n in ("home", "state", "bin"))
    for d in (home, state, bin_dir):
        d.mkdir()
    make_install(home, state, bin_dir)
    env = launcher_env(home, state, bin_dir)
    probe = subprocess.run(
        [BASH, "-c", 'url="http://127.0.0.1:4000/health/liveliness"; '
                     'printf "%s" "${url##*/}"; echo; curl -s -o /dev/null -m 2 -w "%{http_code}" "$url"'],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert probe.stdout.splitlines()[-1] == "000", (
        "the fake curl was not picked up (its 000 verdict is missing): " + probe.stdout)


def test_winner_spawns_once_and_frees_the_lock(tmp_path):
    home, state, bin_dir = (tmp_path / n for n in ("home", "state", "bin"))
    for d in (home, state, bin_dir):
        d.mkdir()
    make_install(home, state, bin_dir)
    env = launcher_env(home, state, bin_dir)
    # The fake litellm flips health on, so the winner's own wait loop succeeds quickly.
    proc = run_launcher(env)
    assert "did not start" not in proc.stderr, proc.stdout + proc.stderr
    spawns = (state / "spawns.txt").read_text(encoding="utf-8").splitlines()
    assert len(spawns) == 1, spawns
    # The lock is freed even though render.py fails later in the launch.
    assert not (state / "start.lock").exists()


def test_second_launch_while_first_still_waits_spawns_nothing(tmp_path):
    """A wedge test with a real first launch: the fake litellm records its spawn but stays
    unhealthy (no litellm-up marker), holding the winner in its wait loop. The second launch
    must lose the lock, wait out the loop and spawn nothing itself."""
    home, state, bin_dir = (tmp_path / n for n in ("home", "state", "bin"))
    for d in (home, state, bin_dir):
        d.mkdir()
    make_install(home, state, bin_dir)
    # Never-healthy fake litellm: the winner will spin the full 45s wait.
    litellm = state / "litellm-venv" / ("Scripts/litellm.exe" if sys.platform == "win32" else "bin/litellm")
    litellm.write_text('#!/bin/sh\necho "spawn $$" >> "$FAKE_STATE/spawns.txt"\n',
                       encoding="utf-8", newline="\n")
    litellm.chmod(0o755)
    env = launcher_env(home, state, bin_dir)
    first = subprocess.Popen([BASH, os.path.join(HERE, "claude-lumo.sh")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8")
    try:
        # Give the winner a moment to grab the lock, then start the loser.
        deadline = 45
        waited = 0.0
        while waited < deadline:
            if (state / "start.lock").exists():
                break
            import time
            time.sleep(0.1)
            waited += 0.1
        assert (state / "start.lock").exists(), "the first launch never took the lock"
        proc = run_launcher(env)
        # The loser never spawns its own litellm: only the winner's one spawn is recorded.
        spawns = (state / "spawns.txt").read_text(encoding="utf-8").splitlines()
        assert len(spawns) == 1, spawns
        # The loser failed after waiting, with the usual message, and left the lock alone.
        assert "LiteLLM did not start" in proc.stderr, proc.stdout + proc.stderr
        assert (state / "start.lock").exists()
    finally:
        first.kill()
        first.wait(timeout=30)
        for stream in (first.stdout, first.stderr):
            stream.close()


def test_stale_lock_does_not_block_startup(tmp_path):
    """A crashed launch can leave $S/start.lock behind; a later launch then takes the loser
    path. If LiteLLM is (made) healthy it proceeds normally; if not, it fails after its wait —
    but it never hangs. Health here: the marker exists, so the loser path exits at once."""
    home, state, bin_dir = (tmp_path / n for n in ("home", "state", "bin"))
    for d in (home, state, bin_dir):
        d.mkdir()
    make_install(home, state, bin_dir)
    (state / "start.lock").mkdir()  # stale wedge from a crashed launch
    (state / "litellm-up").write_text("", encoding="utf-8")  # LiteLLM already healthy
    env = launcher_env(home, state, bin_dir)
    proc = run_launcher(env)
    assert "did not start" not in proc.stderr, proc.stdout + proc.stderr
    assert not (state / "spawns.txt").exists()  # healthy: nobody spawned anything
