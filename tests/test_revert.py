"""Tests for `claude-lumo revert` (revert-command.sh).

Run: python -m pytest tests/test_revert.py -v

Mirrors test_update_check.py's RepoFixture pattern: a bare remote, a work repo
that authors release tags, and a clone standing in for ~/.claude/lumo via
CLAUDE_LUMO_DIR. CLAUDE_LUMO_STATE_DIR points at a fresh temp dir so no test
ever touches the real install or state.
"""
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Fixture:
    """Git fixture: remote with v1.2.9, v1.3.0, v1.3.1; clone checked out at v1.3.1."""

    def __init__(self, releases=("v1.2.9", "v1.3.0", "v1.3.1"), checkout=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state_dir)

        self.work_dir = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work_dir)
        self._git("init", "-q", cwd=self.work_dir)
        readme = os.path.join(self.work_dir, "README.md")
        with open(readme, "w") as f:
            f.write("# Test repo\n")
        self._git("add", "README.md", cwd=self.work_dir)
        self._git("-c", "user.email=tests@example.com", "-c", "user.name=Tests",
                  "commit", "-m", "initial", cwd=self.work_dir)

        self.remote_dir = os.path.join(self.tmp.name, "remote.git")
        self._git("init", "--bare", "-q", self.remote_dir, cwd=self.tmp.name)
        self._git("remote", "add", "origin", self.remote_dir, cwd=self.work_dir)
        self._git("push", "-q", "origin", "master", cwd=self.work_dir)

        for tag in releases:
            self._git("-c", "user.email=tests@example.com", "-c", "user.name=Tests",
                      "commit", "--allow-empty", "-m", f"release {tag}", cwd=self.work_dir)
            self._git("tag", tag, cwd=self.work_dir)
            self._git("push", "-q", "origin", "master", tag, cwd=self.work_dir)

        self.clone_dir = os.path.join(self.tmp.name, "clone")
        self._git("clone", "-q", self.remote_dir, self.clone_dir, cwd=self.tmp.name)
        self._git("checkout", "-q", checkout or releases[-1], cwd=self.clone_dir)

    def _git(self, *args, cwd):
        subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, check=True)

    def cleanup(self):
        self.tmp.cleanup()

    def run_revert(self, arg, extra_env=None):
        env = os.environ.copy()
        env["CLAUDE_LUMO_DIR"] = self.clone_dir
        env["CLAUDE_LUMO_STATE_DIR"] = self.state_dir
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(["bash", os.path.join(HERE, "revert-command.sh"), arg],
                              capture_output=True, env=env, timeout=30,
                              stdin=subprocess.DEVNULL)
        out = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        return proc.returncode, out

    def clone_tag(self):
        r = subprocess.run(["git", "-C", self.clone_dir, "describe", "--tags",
                            "--exact-match", "HEAD"], capture_output=True, text=True)
        return r.stdout.strip()

    def write_state_files(self):
        for name in ("update-notice.txt", "statusline-version.cache"):
            with open(os.path.join(self.state_dir, name), "w") as f:
                f.write("stale\n")


@pytest.fixture
def fx():
    f = Fixture()
    yield f
    f.cleanup()


@pytest.fixture
def fx_old():
    """Clone at v1.2.9 (oldest release): 'last' has no predecessor."""
    f = Fixture(checkout="v1.2.9")
    yield f
    f.cleanup()


class TestVersionParsing:
    """v-prefixed and bare versions both resolve to the same tag."""

    def test_v_prefixed(self, fx):
        code, out = fx.run_revert("v1.3.0")
        assert code == 0
        assert fx.clone_tag() == "v1.3.0"

    def test_bare_version(self, fx):
        code, out = fx.run_revert("1.3.0")
        assert code == 0
        assert fx.clone_tag() == "v1.3.0"

    def test_invalid_input_refused(self, fx):
        code, out = fx.run_revert("banana")
        assert code != 0
        assert "invalid version" in out.lower()
        assert fx.clone_tag() == "v1.3.1"

    def test_two_component_version_refused(self, fx):
        code, out = fx.run_revert("1.3")
        assert code != 0
        assert "invalid version" in out.lower()

    def test_missing_argument_shows_usage(self, fx):
        proc_env = os.environ.copy()
        proc_env["CLAUDE_LUMO_DIR"] = fx.clone_dir
        proc_env["CLAUDE_LUMO_STATE_DIR"] = fx.state_dir
        proc = subprocess.run(["bash", os.path.join(HERE, "revert-command.sh")],
                              capture_output=True, env=proc_env, timeout=30,
                              stdin=subprocess.DEVNULL)
        out = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        assert proc.returncode != 0
        assert "usage" in out.lower()


class TestLastResolution:
    """'last' resolves to the release immediately before the installed one."""

    def test_last_from_middle_release(self, fx):
        # Installed at v1.3.1 (fixture default); previous is v1.3.0.
        code, out = fx.run_revert("last")
        assert code == 0
        assert fx.clone_tag() == "v1.3.0"

    def test_last_from_older_install_skips_newer_tags(self, fx):
        # Reinstall at v1.3.0 (not the latest): 'last' must give v1.2.9, not v1.3.1.
        subprocess.run(["git", "-C", fx.clone_dir, "checkout", "-q", "v1.3.0"],
                       capture_output=True, check=True)
        code, out = fx.run_revert("last")
        assert code == 0
        assert fx.clone_tag() == "v1.2.9"

    def test_last_at_oldest_release_refused(self, fx_old):
        code, out = fx_old.run_revert("last")
        assert code != 0
        assert "no release before" in out.lower()


class TestGuards:
    """Same guard behavior as update-command.sh."""

    def test_refuses_non_clone(self, fx):
        env = {"CLAUDE_LUMO_DIR": fx.state_dir}  # plain dir, no .git
        proc_env = os.environ.copy()
        proc_env["CLAUDE_LUMO_DIR"] = fx.state_dir
        proc_env["CLAUDE_LUMO_STATE_DIR"] = fx.state_dir
        proc = subprocess.run(["bash", os.path.join(HERE, "revert-command.sh"), "v1.3.0"],
                              capture_output=True, env=proc_env, timeout=30,
                              stdin=subprocess.DEVNULL)
        out = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        assert proc.returncode != 0
        assert "not a git clone" in out.lower()

    def test_refuses_local_changes(self, fx):
        with open(os.path.join(fx.clone_dir, "README.md"), "a") as f:
            f.write("\nmodified\n")
        code, out = fx.run_revert("v1.3.0")
        assert code != 0
        assert "local changes" in out.lower()
        assert fx.clone_tag() == "v1.3.1"

    def test_refuses_nonexistent_version(self, fx):
        code, out = fx.run_revert("v9.9.9")
        assert code != 0
        assert "does not exist" in out.lower()
        assert fx.clone_tag() == "v1.3.1"

    def test_refuses_untagged_checkout(self, fx):
        subprocess.run(["git", "-C", fx.work_dir, "-c",
                        "user.email=tests@example.com", "-c", "user.name=Tests",
                        "commit", "--allow-empty", "-m", "dev work"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", fx.work_dir, "push", "-q", "origin", "master"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", fx.clone_dir, "fetch", "-q", "origin"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", fx.clone_dir, "checkout", "-q", "-B", "master",
                        "origin/master"], capture_output=True, check=True)
        code, out = fx.run_revert("v1.3.0")
        assert code != 0
        assert "release tag" in out.lower()


class TestBehavior:
    """Happy path: rollback, no-op, state cleanup, user data untouched."""

    def test_revert_to_current_version_is_noop(self, fx):
        code, out = fx.run_revert("v1.3.1")
        assert code == 0
        assert "already on v1.3.1" in out.lower()
        assert fx.clone_tag() == "v1.3.1"

    def test_downgrade_note_printed(self, fx):
        code, out = fx.run_revert("v1.3.0")
        assert code == 0
        assert "downgrade" in out.lower()
        assert "claude-lumo update" in out
        assert "reverted to v1.3.0" in out.lower()

    def test_clears_notice_and_version_cache(self, fx):
        fx.write_state_files()
        code, out = fx.run_revert("v1.3.0")
        assert code == 0
        assert not os.path.exists(os.path.join(fx.state_dir, "update-notice.txt"))
        assert not os.path.exists(os.path.join(fx.state_dir, "statusline-version.cache"))

    def test_noop_leaves_state_files(self, fx):
        # A friendly no-op should not have side effects on state either way;
        # the notice/cache clear belongs to the actual rollback path.
        fx.write_state_files()
        code, out = fx.run_revert("v1.3.1")
        assert code == 0
        assert os.path.exists(os.path.join(fx.state_dir, "update-notice.txt"))
        assert os.path.exists(os.path.join(fx.state_dir, "statusline-version.cache"))

    def test_user_data_untouched(self, fx):
        user_file = os.path.join(fx.state_dir, "sessions", "abc.json")
        os.makedirs(os.path.dirname(user_file), exist_ok=True)
        with open(user_file, "w") as f:
            f.write("{}\n")
        code, out = fx.run_revert("v1.3.0")
        assert code == 0
        assert os.path.exists(user_file)
        with open(user_file) as f:
            assert f.read() == "{}\n"
