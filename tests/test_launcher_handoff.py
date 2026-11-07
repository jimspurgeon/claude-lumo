"""claude-lumo.sh's wipe/privacy-audit handoff (#99, #102): exit codes other than 10 pass
through, and a 10 without the prompt file is an error, not a silent exit. Both cases end before
the launcher touches the proxy or starts anything."""
import os
import subprocess

import pytest

from test_revert import BASH, HERE

FAKE_SCRIPT = """import os, sys
if os.environ.get("CLAUDE_LUMO_LAUNCHER") != "1":
    sys.exit(3)
sys.exit(int(os.environ["FAKE_RC"]))
"""


@pytest.mark.parametrize("command", ["wipe", "privacy-audit"])
@pytest.mark.parametrize("rc,expected", [(0, 0), (1, 1), (10, 1)])
def test_exit_codes(tmp_path, command, rc, expected):
    lumo = tmp_path / ".claude" / "lumo"
    lumo.mkdir(parents=True)
    (lumo / (command.replace("-", "_") + ".py")).write_text(FAKE_SCRIPT, encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path), CLAUDE_LUMO_STATE_DIR=str(tmp_path / "state"),
               FAKE_RC=str(rc))
    proc = subprocess.run([BASH, os.path.join(HERE, "claude-lumo.sh"), command], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    if rc == 10:
        assert "agent prompt was not written" in proc.stderr


FAKE_WIPE = """
import os, sys
state = os.environ["CLAUDE_LUMO_STATE_DIR"]
with open(os.path.join(state, "wipe-agent-prompt.txt"), "w", encoding="utf-8") as f:
    f.write('please "review" the audit')
sys.exit(10)
"""


def test_rc10_happy_path_hands_the_prompt_to_claude(tmp_path):
    """#106: an exit-10 wipe completes the whole launch - render runs, and claude is exec'd
    with the prompt file's content as ONE trailing argument, which is then removed."""
    import sys as _sys
    home, state, bin_dir = (tmp_path / n for n in ("home", "state", "bin"))
    for d in (home, state, bin_dir):
        d.mkdir()
    lumo = home / ".claude" / "lumo"
    lumo.mkdir(parents=True)
    from test_start_lock import BASH, launcher_env, make_install  # reuse the fake harness
    make_install(home, state, bin_dir)
    # Overwrite the failing render.py with one that succeeds minimally.
    (lumo / "render.py").write_text(
        "import sys\n"
        "state = sys.argv[2]\n"
        "open(state + '/settings.json', 'w').write('{}')\n"
        "open(state + '/system-prompt.md', 'w').write('')\n",
        encoding="utf-8")
    # A wipe script that takes the exit-10 path.
    (lumo / "wipe.py").write_text(FAKE_WIPE, encoding="utf-8")
    # The fake claude records each argv element on its own line.
    claude = bin_dir / ("claude.exe" if _sys.platform == "win32" else "claude")
    claude.write_text("#!/bin/sh\nprintf '%s\n' \"$@\" >> \"$CLAUDE_LUMO_STATE_DIR/claude-argv\"\n",
                      encoding="utf-8", newline="\n")
    claude.chmod(0o755)
    env = launcher_env(home, state, bin_dir)
    proc = subprocess.run([BASH, os.path.join(HERE, "claude-lumo.sh"), "wipe"], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    argv = (state / "claude-argv").read_text(encoding="utf-8").splitlines()
    assert argv[:1] == ["--settings"] and \
        os.path.normcase(os.path.normpath(argv[1])) == \
        os.path.normcase(os.path.normpath(str(state / "settings.json"))), argv
    assert argv[2:] == ['please "review" the audit'], argv
    assert not (state / "wipe-agent-prompt.txt").exists()
