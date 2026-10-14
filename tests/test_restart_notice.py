"""The status line asks for a restart when the install has moved past the session's release (#35)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

STATUSLINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "statusline.py")


def render(installed, started, lumo="1", notice=None):
    with tempfile.TemporaryDirectory() as state:
        # A fresh version cache stands in for the installed release, so no git probe runs.
        with open(os.path.join(state, "statusline-version.cache"), "w", encoding="utf-8") as f:
            f.write(installed)
        if notice is not None:
            with open(os.path.join(state, "update-notice.txt"), "w", encoding="utf-8") as f:
                f.write(notice)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_LUMO")}
        env.update(CLAUDE_LUMO=lumo, CLAUDE_LUMO_STATE_DIR=state, CLAUDE_LUMO_LOCAL=state)
        if started is not None:
            env["CLAUDE_LUMO_SESSION_VERSION"] = started
        stdin = json.dumps({"model": {"id": "lumo-max", "display_name": "lumo-max"}})
        r = subprocess.run([sys.executable, STATUSLINE], input=stdin, capture_output=True,
                           text=True, env=env, timeout=30)
        return r.stdout


class RestartNotice(unittest.TestCase):
    def test_shown_when_the_install_moved_on(self):
        self.assertIn("restart to load v1.4.1", render("v1.4.1", "v1.4.0"))

    def test_hidden_when_versions_match_or_are_unknown(self):
        for installed, started in (("v1.4.1", "v1.4.1"), ("v1.4.1", ""), ("v1.4.1", None),
                                   ("dev", "v1.4.0"), ("", "v1.4.0")):
            self.assertNotIn("restart to load", render(installed, started), (installed, started))

    def test_hidden_outside_lumo(self):
        self.assertNotIn("restart to load", render("v1.4.1", "v1.4.0", lumo="0"))

    def test_plain_claude_shows_no_lumo_version_or_update_notice(self):
        out = render("v1.4.1", "v1.4.0", lumo="0", notice="claude-lumo v9.9.9 is available")
        self.assertIn("[CLAUDE]", out)
        self.assertNotIn("v1.4.1", out)
        self.assertNotIn("v9.9.9", out)

    def test_lumo_still_shows_version_and_update_notice(self):
        out = render("v1.4.1", "v1.4.1", notice="claude-lumo v9.9.9 is available")
        self.assertIn("v1.4.1", out)
        self.assertIn("v9.9.9", out)


if __name__ == "__main__":
    unittest.main()
