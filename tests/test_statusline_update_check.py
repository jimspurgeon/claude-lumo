"""The status line starts the update check so a long-running session learns of a release (#120).

statusline.py draws the line at import time, so it is loaded in-process with plain-Claude
settings (no version probe on import) and a stand-in stdin; the spawn is always mocked, so no
test launches update-check.py or reaches the network.
"""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_statusline(state):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_LUMO")}
    env.update(CLAUDE_LUMO_STATE_DIR=state, CLAUDE_LUMO_LOCAL=state)
    stdin = SimpleNamespace(buffer=io.BytesIO(b"{}"))
    spec = importlib.util.spec_from_file_location("statusline_under_test", os.path.join(ROOT, "statusline.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["statusline.py"]), \
            mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


class StatuslineUpdateCheck(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="statusline-uc-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.mod = load_statusline(self.state)
        self.spawns = []
        patches = [mock.patch.object(self.mod, "spawn_detached", side_effect=self.spawns.append),
                   mock.patch.dict(os.environ, {"CLAUDE_LUMO_STATE_DIR": self.state}),
                   mock.patch.object(self.mod, "STATE_DIR", self.state)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_LUMO_UPDATE_CHECK", None)

    def stamp(self):
        return os.path.join(self.state, "update-check-spawn.stamp")

    def git_says(self, tag):
        """subprocess.run stand-in for install_version's git probe."""
        return mock.patch.object(self.mod.subprocess, "run",
                                 return_value=SimpleNamespace(returncode=0, stdout=tag + "\n"))

    def test_hourly_refresh_spawns_the_check_detached(self):
        with self.git_says("v1.4.4"):
            self.assertEqual(self.mod.install_version(), "v1.4.4")
        self.assertEqual(self.spawns, [[sys.executable, os.path.join(ROOT, "update-check.py"), ROOT]])
        self.assertTrue(os.path.isfile(self.stamp()))
        self.assertFalse(os.path.exists(self.stamp() + ".lock"))

    def test_cached_redraws_spawn_nothing(self):
        with self.git_says("v1.4.4"):
            self.mod.install_version()
            for _ in range(5):
                self.mod.install_version()
        self.assertEqual(len(self.spawns), 1)

    def test_at_most_once_an_hour_even_when_the_cache_is_refreshed(self):
        now = time.time()
        self.assertTrue(self.mod.start_update_check(now))
        self.assertFalse(self.mod.start_update_check(now + 60))
        self.assertTrue(self.mod.start_update_check(now + self.mod.UPDATE_CHECK_EVERY + 1))
        self.assertEqual(len(self.spawns), 2)

    def test_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"CLAUDE_LUMO_UPDATE_CHECK": "0"}):
            self.assertFalse(self.mod.start_update_check())
        self.assertEqual(self.spawns, [])

    def test_only_0_disables_like_the_launcher(self):
        """claude-lumo.sh and the README switch the check off with exactly 0."""
        with mock.patch.dict(os.environ, {"CLAUDE_LUMO_UPDATE_CHECK": "false"}):
            self.assertTrue(self.mod.start_update_check())
        self.assertEqual(len(self.spawns), 1)

    def test_dev_or_untagged_install_is_not_checked(self):
        with self.git_says(""):  # no exact tag, but rev-parse succeeds: a dev checkout
            self.assertEqual(self.mod.install_version(), "dev")
        self.assertEqual(self.spawns, [])

    def test_no_script_next_to_the_statusline(self):
        with mock.patch.object(self.mod, "__file__", os.path.join(self.state, "statusline.py")):
            self.assertFalse(self.mod.start_update_check())
        self.assertEqual(self.spawns, [])

    def test_a_held_lock_blocks_and_a_stale_one_is_cleared(self):
        lock = self.stamp() + ".lock"
        os.mkdir(lock)
        now = time.time()
        self.assertFalse(self.mod.start_update_check(now))
        self.assertTrue(os.path.isdir(lock))  # fresh: another statusline holds it
        self.assertEqual(self.spawns, [])
        # Stale: the same refresh clears it, takes it and spawns, rather than waiting an hour.
        self.assertTrue(self.mod.start_update_check(now + self.mod.SPAWN_LOCK_STALE + 1))
        self.assertFalse(os.path.exists(lock))  # released afterwards
        self.assertEqual(len(self.spawns), 1)

    def test_concurrent_statuslines_spawn_once(self):
        barrier = threading.Barrier(12)
        results = []

        def redraw():
            barrier.wait()
            results.append(self.mod.start_update_check())
        threads = [threading.Thread(target=redraw) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.spawns), 1)

    def test_a_failing_spawn_never_breaks_the_redraw(self):
        with mock.patch.object(self.mod, "spawn_detached", side_effect=OSError("no python")), \
                self.git_says("v1.4.4"):
            self.assertEqual(self.mod.install_version(), "v1.4.4")
        self.assertFalse(os.path.exists(self.stamp() + ".lock"))

    def test_notice_written_by_the_check_shows_on_a_later_redraw(self):
        """End to end with the files only: the status line shows what update-check.py wrote."""
        import subprocess
        import json
        with open(os.path.join(self.state, "statusline-version.cache"), "w", encoding="utf-8") as f:
            f.write("v1.4.4")
        with open(os.path.join(self.state, "update-notice.txt"), "w", encoding="utf-8") as f:
            f.write("claude-lumo v1.4.5 available: run `claude-lumo update`\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_LUMO")}
        env.update(CLAUDE_LUMO="1", CLAUDE_LUMO_STATE_DIR=self.state, CLAUDE_LUMO_LOCAL=self.state)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "statusline.py")],
                           input=json.dumps({"model": {"id": "lumo-max"}}), capture_output=True,
                           text=True, env=env, timeout=30)
        self.assertIn("v1.4.5 available", r.stdout)
        self.assertFalse(os.path.exists(self.stamp()))  # a fresh cache spawns nothing


if __name__ == "__main__":
    unittest.main()
