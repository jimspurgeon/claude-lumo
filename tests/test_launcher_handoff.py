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
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    if rc == 10:
        assert "agent prompt was not written" in proc.stderr
