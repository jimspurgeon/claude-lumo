"""Tests for update-check feature. Run: python test_update_check.py"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
# On Windows a bare "bash" resolves to System32's WSL launcher before PATH is searched (#91).
BASH = shutil.which("bash") or "bash"
sys.path.insert(0, HERE)

# Load session-restart.py dynamically (dash in filename prevents normal import)
spec = importlib.util.spec_from_file_location("session_restart", os.path.join(HERE, "session-restart.py"))
session_restart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_restart)


def git_commit(bare_repo, work_dir, tag, subject):
    """Commit and tag in a work clone, push to bare repo."""
    subprocess.run(["git", "-C", work_dir, "commit", "--allow-empty", "-m", subject],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "tag", tag], capture_output=True, check=True)
    subprocess.run(["git", "-C", work_dir, "push", "-q", "origin", "master", tag],
                   capture_output=True, check=True)


class RepoFixture(unittest.TestCase):
    """Mixin creating shared git fixtures. Subclassed by test groups."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)

        # Create work repo with minimal tree (README.md only, NO install.sh)
        self.work_dir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work_dir)
        subprocess.run(["git", "init", "-q"], cwd=self.work_dir, capture_output=True)
        readme = os.path.join(self.work_dir, "README.md")
        with open(readme, "w") as f:
            f.write("# Test repo\n")
        subprocess.run(["git", "-C", self.work_dir, "add", "README.md"], capture_output=True)
        subprocess.run(["git", "-c", "user.email=tests@example.com", "-c", "user.name=Tests",
                       "commit", "-m", "initial"], cwd=self.work_dir,
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.work_dir, "tag", "v1.2.9"], capture_output=True, check=True)

        # Create bare remote and push
        self.remote_dir = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "--bare", "-q", self.remote_dir], capture_output=True, check=True)
        subprocess.run(["git", "-C", self.work_dir, "remote", "add", "origin", self.remote_dir],
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.work_dir, "push", "-q", "origin", "master", "v1.2.9"],
                      capture_output=True, check=True)

        # Clone for testing
        self.clone_dir = os.path.join(self.tmp.name, "clone")
        subprocess.run(["git", "clone", "-q", self.remote_dir, self.clone_dir],
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.clone_dir, "checkout", "-q", "v1.2.9"],
                      capture_output=True, check=True)

    def run_check(self, extra_env=None):
        """Run update-check.py with fixture env."""
        env = os.environ.copy()
        env["CLAUDE_LUMO_STATE_DIR"] = self.state_dir
        env["CLAUDE_LUMO_UPDATE_REMOTE"] = self.remote_dir
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, os.path.join(HERE, "update-check.py"), self.clone_dir],
                             capture_output=True, env=env, timeout=30)

    def run_update(self, extra_env=None, stdin=subprocess.DEVNULL):
        """Run update-command.sh with fixture env."""
        env = os.environ.copy()
        env["CLAUDE_LUMO_DIR"] = self.clone_dir
        env["CLAUDE_LUMO_STATE_DIR"] = self.state_dir
        if extra_env:
            env.update(extra_env)
        return subprocess.run([BASH, os.path.join(HERE, "update-command.sh")],
                             capture_output=True, env=env, timeout=30, stdin=stdin)

    def add_release(self, tag, subject):
        """Add a release tag to the remote via the work repo."""
        git_commit(self.remote_dir, self.work_dir, tag, subject)

    def notice_path(self):
        return os.path.join(self.state_dir, "update-notice.txt")

    def state_file_path(self):
        return os.path.join(self.state_dir, "update-check-state.json")

    def reset_state(self):
        """Delete state files."""
        for f in [self.notice_path(), self.state_file_path()]:
            if os.path.exists(f):
                os.remove(f)


class UpdateCheckTest(RepoFixture):
    """Tests for silent update checking."""

    def test_no_new_tag_is_silent(self):
        """Remote has only v1.2.9, clone at v1.2.9: no notice, state updated."""
        proc = self.run_check()
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(self.notice_path()))
        self.assertTrue(os.path.exists(self.state_file_path()))
        with open(self.state_file_path()) as f:
            state = json.load(f)
        self.assertIn("last_check", state)
        self.assertNotIn("notified_version", state)

    def test_new_tag_writes_one_line_notice(self):
        """Push v1.3.0: notice created with exact format."""
        self.add_release("v1.3.0", "add feature")
        proc = self.run_check()
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.exists(self.notice_path()))
        with open(self.notice_path(), encoding="utf-8") as f:
            content = f.read()
        expected = "claude-lumo v1.3.0 available: run `claude-lumo update`\n"
        self.assertEqual(content, expected)
        with open(self.state_file_path()) as f:
            state = json.load(f)
        self.assertEqual(state.get("notified_version"), "v1.3.0")

    def test_same_version_not_re_notified(self):
        """After notifying v1.3.0, second check: notice unchanged."""
        self.add_release("v1.3.0", "add feature")
        self.run_check()
        mtime_before = os.path.getmtime(self.notice_path())
        with open(self.notice_path(), encoding="utf-8") as f:
            content_before = f.read()
        import time
        time.sleep(0.1)
        self.run_check()
        with open(self.notice_path(), encoding="utf-8") as f:
            content_after = f.read()
        self.assertEqual(content_before, content_after)
        self.assertEqual(os.path.getmtime(self.notice_path()), mtime_before)

    def test_newer_version_notified_again(self):
        """After v1.3.0 notified, pushing v1.3.1 updates notice."""
        self.add_release("v1.3.0", "add feature")
        self.run_check()
        self.add_release("v1.3.1", "another feature")
        os.remove(self.state_file_path())  # Remove rate limit state
        self.run_check()
        with open(self.notice_path(), encoding="utf-8") as f:
            content = f.read()
        self.assertIn("v1.3.1", content)

    def test_untagged_master_commits_silent(self):
        """Extra commit without tag: no notice created."""
        # Add commit without tag
        subprocess.run(["git", "-C", self.work_dir, "commit", "--allow-empty", "-m", "untagged commit"],
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.work_dir, "push", "-q", "origin", "master"],
                      capture_output=True, check=True)
        self.reset_state()
        proc = self.run_check()
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(self.notice_path()))

    def test_offline_is_silent(self):
        """Nonexistent remote: exit 0, no notice, no last_check in state."""
        env = os.environ.copy()
        env["CLAUDE_LUMO_STATE_DIR"] = self.state_dir
        env["CLAUDE_LUMO_UPDATE_REMOTE"] = os.path.join(self.state_dir, "nonexistent.git")
        proc = subprocess.run([sys.executable, os.path.join(HERE, "update-check.py"), self.clone_dir],
                             capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(self.notice_path()))
        if os.path.exists(self.state_file_path()):
            with open(self.state_file_path()) as f:
                state = json.load(f)
            self.assertNotIn("last_check", state)

    def test_disabled(self):
        """CLAUDE_LUMO_UPDATE_CHECK=0: no notice, no state file."""
        env = os.environ.copy()
        env["CLAUDE_LUMO_STATE_DIR"] = self.state_dir
        env["CLAUDE_LUMO_UPDATE_REMOTE"] = self.remote_dir
        env["CLAUDE_LUMO_UPDATE_CHECK"] = "0"
        subprocess.run([sys.executable, os.path.join(HERE, "update-check.py"), self.clone_dir],
                      capture_output=True, env=env, timeout=30)
        self.assertFalse(os.path.exists(self.notice_path()))
        self.assertFalse(os.path.exists(self.state_file_path()))

    def test_rate_limit_respected(self):
        """Immediate second check: rate limited, notice not recreated."""
        self.add_release("v1.3.0", "add feature")
        self.run_check()
        notice_mtime = os.path.getmtime(self.notice_path())
        os.remove(self.notice_path())
        self.run_check()
        self.assertFalse(os.path.exists(self.notice_path()))
        with open(self.state_file_path()) as f:
            state = json.load(f)
        self.assertEqual(state.get("notified_version"), "v1.3.0")
        # Simulate expiry and test same-version check
        state["last_check"] = (datetime.now() - timedelta(days=2)).isoformat()
        with open(self.state_file_path(), "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f)
        self.run_check()
        self.assertFalse(os.path.exists(self.notice_path()))


class UpdateCommandTest(RepoFixture):
    """Tests for update-command.sh."""

    def test_refuses_on_local_edits(self):
        """Modified tracked file: refusal with 'local changes' message."""
        readme = os.path.join(self.clone_dir, "README.md")
        with open(readme, "a") as f:
            f.write("\nmodified\n")
        proc = self.run_update(stdin=subprocess.DEVNULL)
        self.assertNotEqual(proc.returncode, 0)
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        self.assertIn("local changes", output.lower())

    def test_refuses_on_non_tag_checkout(self):
        """Checked out at master (untagged): refusal with 'release tag' message."""
        # Reset clone to clean state first (previous test may have modified files)
        subprocess.run(["git", "-C", self.clone_dir, "reset", "--hard", "v1.2.9"],
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.clone_dir, "clean", "-q", "-fd"],
                      capture_output=True, check=True)
        # Add an untagged commit to master so checkout at master is not at a tag
        subprocess.run(["git", "-C", self.work_dir, "commit", "--allow-empty", "-m", "dev work"],
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.work_dir, "push", "-q", "origin", "master"],
                      capture_output=True, check=True)
        # Fetch and force local master to match remote
        subprocess.run(["git", "-C", self.clone_dir, "fetch", "-q", "origin"],
                      capture_output=True, check=True)
        subprocess.run(["git", "-C", self.clone_dir, "checkout", "-q", "-B", "master", "origin/master"],
                      capture_output=True, check=True)
        proc = self.run_update(stdin=subprocess.DEVNULL)
        self.assertNotEqual(proc.returncode, 0)
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        self.assertIn("release tag", output.lower())

    def test_updates_to_latest_tag(self):
        """Update when newer tag exists: succeeds with changelog and version."""
        self.add_release("v1.3.0", "add feature")
        proc = self.run_update(stdin=subprocess.DEVNULL)
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("add feature", output)
        self.assertIn("Updated to v1.3.0", output)
        result = subprocess.run(["git", "-C", self.clone_dir, "describe", "--tags", "--exact-match", "HEAD"],
                               capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "v1.3.0")

    def test_version_cache_cleared_even_if_installer_is_interrupted(self):
        """The status line version cache goes right after the checkout (#84): an update stopped
        during the slow installer rerun must not leave the old version showing for an hour."""
        with open(os.path.join(self.work_dir, "install.sh"), "w", newline="\n") as f:
            f.write("kill -TERM $PPID\nsleep 5\n")  # stands in for Ctrl-C during the installer
        subprocess.run(["git", "-C", self.work_dir, "add", "install.sh"], capture_output=True, check=True)
        self.add_release("v1.3.0", "add installer")
        cache = os.path.join(self.state_dir, "statusline-version.cache")
        with open(cache, "w") as f:
            f.write("v1.2.9")
        self.run_update(stdin=subprocess.DEVNULL)
        result = subprocess.run(["git", "-C", self.clone_dir, "describe", "--tags", "--exact-match", "HEAD"],
                               capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "v1.3.0")
        self.assertFalse(os.path.exists(cache))

    def test_already_latest(self):
        """Already at latest tag: success with 'already on the latest release' message."""
        proc = self.run_update(stdin=subprocess.DEVNULL)
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("already on the latest release", output)


from datetime import datetime, timedelta


class BusyMarkTest(unittest.TestCase):
    """Tests for hooks/busy-mark.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        self.hook_path = os.path.join(HERE, "hooks", "busy-mark.py")

    def send_hook(self, session_id, cwd, event):
        """Pipe JSON to busy-mark.py."""
        data = json.dumps({"session_id": session_id, "cwd": cwd, "hook_event_name": event})
        env = os.environ.copy()
        env["CLAUDE_LUMO_STATE_DIR"] = self.state_dir
        return subprocess.run([sys.executable, self.hook_path], input=data.encode("utf-8"),
                             capture_output=True, env=env, timeout=30)

    def test_busy_lifecycle(self):
        """SessionStart -> busy false; UserPromptSubmit -> busy true; Stop -> busy false; SessionEnd -> deleted."""
        sid = "abc123"
        sess_file = os.path.join(self.state_dir, "sessions", f"{sid}.json")
        # SessionStart
        self.send_hook(sid, "/tmp/foo", "SessionStart")
        self.assertTrue(os.path.exists(sess_file))
        with open(sess_file) as f:
            data = json.load(f)
        self.assertFalse(data["busy"])
        # UserPromptSubmit
        self.send_hook(sid, "/tmp/foo", "UserPromptSubmit")
        with open(sess_file) as f:
            data = json.load(f)
        self.assertTrue(data["busy"])
        # Stop
        self.send_hook(sid, "/tmp/foo", "Stop")
        with open(sess_file) as f:
            data = json.load(f)
        self.assertFalse(data["busy"])
        # SessionEnd
        self.send_hook(sid, "/tmp/foo", "SessionEnd")
        self.assertFalse(os.path.exists(sess_file))

    def test_bad_input_silent(self):
        """Invalid JSON: exit 0, no file created."""
        subprocess.run([sys.executable, self.hook_path], input=b"not json",
                      capture_output=True, timeout=30)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "sessions")))


class SessionRestartTest(unittest.TestCase):
    """Tests for pure functions in session-restart.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)
        self.sessions_dir = os.path.join(self.state_dir, "sessions")
        os.makedirs(self.sessions_dir)

    def write_session(self, sid, cwd, busy, days_old=0, pid=None):
        """Write a session registry file. pid defaults to this test process (always alive),
        so the pid-liveness filter in sessions_from_registry does not drop the entry (#123)."""
        updated = datetime.now() - timedelta(days=days_old)
        record = {"session_id": sid, "cwd": cwd, "pid": pid if pid is not None else os.getpid(),
                  "busy": busy, "updated": updated.isoformat()}
        path = os.path.join(self.sessions_dir, f"{sid}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, indent=2)
            f.write("\n")
        return path

    def test_ask_choice_without_a_terminal_skips(self):
        """No usable stdin (None under pythonw, closed, or not a tty) means skip, not a crash (#39)."""
        from unittest import mock
        with mock.patch.object(sys, "stdin", None):
            self.assertEqual(session_restart.ask_choice("? "), "s")
        tty = mock.Mock(isatty=mock.Mock(return_value=True))
        with mock.patch.object(sys, "stdin", tty), mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(session_restart.ask_choice("? "), "s")
        with mock.patch.object(sys, "stdin", tty), mock.patch("builtins.input", return_value=" W "):
            self.assertEqual(session_restart.ask_choice("? "), "w")

    def test_sessions_from_registry(self):
        """Fresh busy + idle returned; stale deleted; self excluded."""
        self.write_session("fresh_busy", "/tmp/a", True, days_old=0)
        self.write_session("fresh_idle", "/tmp/b", False, days_old=0)
        self.write_session("stale", "/tmp/c", True, days_old=8)
        self.write_session("self", "/tmp/d", False, days_old=0)
        sessions = session_restart.sessions_from_registry(self.state_dir, exclude_self="self")
        self.assertEqual(len(sessions), 2)
        sids = {s["session_id"] for s in sessions}
        self.assertIn("fresh_busy", sids)
        self.assertIn("fresh_idle", sids)
        self.assertFalse(os.path.exists(os.path.join(self.sessions_dir, "stale.json")))
        for s in sessions:
            self.assertIn("session_id", s)
            self.assertIn("cwd", s)
            self.assertIn("busy", s)
            self.assertIn("path", s)

    def test_sessions_from_registry_drops_dead_pids(self):
        """Entries whose recorded pid no longer exists are skipped (orphaned registry, #123)."""
        self.write_session("alive", "/tmp/a", True, pid=os.getpid())
        self.write_session("dead", "/tmp/b", False, pid=999999998)  # no such pid
        self.write_session("no_pid", "/tmp/c", False, pid=None)  # unverifiable: kept
        sessions = session_restart.sessions_from_registry(self.state_dir)
        sids = {s["session_id"] for s in sessions}
        self.assertEqual(sids, {"alive", "no_pid"})

    def test_is_pid_alive(self):
        """Own pid alive; nonexistent pid dead; None unverifiable (kept as alive)."""
        self.assertTrue(session_restart.is_pid_alive(os.getpid()))
        self.assertFalse(session_restart.is_pid_alive(999999998))
        self.assertTrue(session_restart.is_pid_alive(None))

    def test_wait_until_idle(self):
        """Polls until idle: True; timeout: False."""
        path = self.write_session("test", "/tmp/x", True)
        def flip():
            time.sleep(0.3)
            record = {"session_id": "test", "cwd": "/tmp/x", "pid": 1, "busy": False,
                     "updated": datetime.now().isoformat()}
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(record, f)
        threading.Thread(target=flip, daemon=True).start()
        result = session_restart.wait_until_idle(path, poll_seconds=0.05, timeout_seconds=5)
        self.assertTrue(result)
        # Timeout case
        path2 = self.write_session("test2", "/tmp/y", True)
        result2 = session_restart.wait_until_idle(path2, poll_seconds=0.05, timeout_seconds=0.2)
        self.assertFalse(result2)

    def test_match_orca_terminal(self):
        """Matching worktreePath returns handle; no match -> None."""
        terminals = [{"handle": "t1", "worktreePath": "/tmp/foo"},
                    {"handle": "t2", "worktreePath": "/tmp/bar"}]
        handle = session_restart.match_orca_terminal(terminals, "/tmp/bar")
        self.assertEqual(handle, "t2")
        handle2 = session_restart.match_orca_terminal(terminals, "/tmp/baz")
        self.assertIsNone(handle2)

    def test_choose_controller(self):
        """Override wins; cleared env -> none; ORCA_WORKTREE_ID + which -> orca."""
        self.assertEqual(session_restart.choose_controller("tmux"), "tmux")
        saved_environ = os.environ.copy()
        try:
            os.environ.pop("ORCA_WORKTREE_ID", None)
            os.environ.pop("TMUX", None)
            self.assertEqual(session_restart.choose_controller(), "none")
        finally:
            os.environ.clear()
            os.environ.update(saved_environ)
        os.environ["ORCA_WORKTREE_ID"] = "x"
        # The orca CLI need not be installed where the tests run (CI), so stand one in.
        with mock.patch.object(session_restart.shutil, "which", return_value="/usr/bin/orca"):
            self.assertEqual(session_restart.choose_controller(), "orca")
        with mock.patch.object(session_restart.shutil, "which", return_value=None):
            self.assertEqual(session_restart.choose_controller(), "none")
        del os.environ["ORCA_WORKTREE_ID"]

    def test_restart_in_terminal_wait_path(self):
        """choice w with idle session: immediate restart."""
        calls = []
        def send(text, enter=False, interrupt=False):
            calls.append((text, enter, interrupt))
            return True
        path = self.write_session("sid", "/tmp/x", False)
        ok, msg = session_restart.restart_in_terminal(send, "sid", "w", path)
        self.assertTrue(ok)
        self.assertEqual(msg, "restarted")
        self.assertEqual(calls[0], ("/exit", True, False))
        self.assertEqual(calls[1], ("claude-lumo --resume sid", True, False))

    def test_restart_in_terminal_interrupt_path(self):
        """Choice i: interrupt sent first, then normal flow."""
        calls = []
        def send(text, enter=False, interrupt=False):
            calls.append((text, enter, interrupt))
            return True
        path = self.write_session("sid", "/tmp/x", True)
        ok, msg = session_restart.restart_in_terminal(send, "sid", "i", path)
        self.assertTrue(ok)
        self.assertEqual(msg, "restarted")
        self.assertTrue(calls[0][2])  # first call is interrupt with text None
        self.assertEqual(calls[1], ("/exit", True, False))
        self.assertEqual(calls[2], ("claude-lumo --resume sid", True, False))
        self.assertTrue(any(c == "(restarted for a claude-lumo update) continue" for c, _, _ in calls))

    def test_restart_in_terminal_send_failure(self):
        """send returns False for /exit: returns failure, no relaunch."""
        calls = []
        def send(text, enter=False, interrupt=False):
            calls.append((text, enter, interrupt))
            return text != "/exit"
        path = self.write_session("sid", "/tmp/x", False)
        ok, msg = session_restart.restart_in_terminal(send, "sid", "w", path)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("/exit", True, False))


if __name__ == "__main__":
    unittest.main()
