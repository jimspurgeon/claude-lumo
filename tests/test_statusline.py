"""Tests for statusline.py's install_version() function.

These tests drive install_version() via subprocess to exercise real git and cache behavior,
with CLAUDE_LUMO_STATE_DIR set to a fresh temp dir per test.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_state_dir():
    """Create a fresh temp directory for CLAUDE_LUMO_STATE_DIR."""
    dir_path = tempfile.mkdtemp(prefix="statusline-test-")
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture
def statusline_module_path():
    """Return the path to statusline.py in the worktree."""
    # Get the directory containing this test file, then go up one level
    test_dir = Path(__file__).parent.resolve()
    return str(test_dir.parent / "statusline.py")


def run_install_version(state_dir: str, module_path: str) -> str:
    """Run install_version() via subprocess and return only the version string.

    The statusline.py module prints ANSI output when imported, so we capture stderr
    where we print the version to avoid mixing it with the status line output.
    """
    env = os.environ.copy()
    env["CLAUDE_LUMO_STATE_DIR"] = state_dir
    # Use sys.stderr to print the version so we can separate it from status line stdout
    cmd = [
        "python", "-c",
        "from statusline import install_version; import sys; "
        "sys.stderr.write(install_version() + '\\n')"
    ]
    # Set the working directory to where statusline.py lives so imports work
    module_dir = str(Path(module_path).parent)
    result = subprocess.run(
        cmd,
        env=env,
        cwd=module_dir,
        capture_output=True,
        text=True,
        timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(f"subprocess failed: {result.stderr}")
    # Version is on stderr, status line is on stdout (which we ignore)
    return result.stderr.strip()


class TestUntaggedRepo:
    """Test untagged repo returns 'dev'."""

    def test_head_not_a_tag_returns_dev(self, temp_state_dir, statusline_module_path):
        """HEAD is not a tag, should return 'dev' (worktree-aware, i.e., .git is a pointer file)."""
        version = run_install_version(temp_state_dir, statusline_module_path)
        assert version == "dev"


class TestExactTag:
    """Test exact tag matches."""

    def test_exact_tag_returns_tag(self, temp_state_dir, statusline_module_path):
        """HEAD matches a tag like 'v9.8.7', returns 'v9.8.7'."""
        # Create a test git repo and tag it
        test_repo = tempfile.mkdtemp(prefix="tagged-repo-")
        try:
            # Initialize git repo
            subprocess.run(["git", "init"], cwd=test_repo, check=True, capture_output=True)
            subprocess.run(["git", "-C", test_repo, "config", "user.email", "test@test.com"],
                          check=True, capture_output=True)
            subprocess.run(["git", "-C", test_repo, "config", "user.name", "Test"],
                          check=True, capture_output=True)

            # Create a commit
            test_file = Path(test_repo) / "test.txt"
            test_file.write_text("test content")
            subprocess.run(["git", "-C", test_repo, "add", "."],
                          check=True, capture_output=True)
            subprocess.run(["git", "-C", test_repo, "commit", "-m", "initial"],
                          check=True, capture_output=True)

            # Create a tag
            subprocess.run(["git", "-C", test_repo, "tag", "v9.8.7"],
                          check=True, capture_output=True)

            # Copy statusline.py into the tagged repo root so it detects the tag
            # (the function uses __file__'s directory to run git -C)
            staging_file = Path(test_repo) / "statusline.py"
            shutil.copy(statusline_module_path, staging_file)

            env = os.environ.copy()
            env["CLAUDE_LUMO_STATE_DIR"] = temp_state_dir
            cmd = [
                "python", "-c",
                "from statusline import install_version; import sys; "
                "sys.stderr.write(install_version() + '\\n')"
            ]
            result = subprocess.run(
                cmd,
                env=env,
                cwd=test_repo,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError(f"subprocess failed: {result.stderr}")
            version = result.stderr.strip()
            assert version == "v9.8.7"
        finally:
            shutil.rmtree(test_repo, ignore_errors=True)


class TestNonRepoDir:
    """Test non-git directory returns empty string."""

    def test_non_repo_dir_returns_empty(self, temp_state_dir):
        """Not a git checkout at all, returns ''."""
        # Create a plain temp directory with no git
        plain_dir = tempfile.mkdtemp(prefix="non-repo-")
        # Copy statusline.py there
        stagedir = tempfile.mkdtemp(prefix="stagedir-")
        staging_file = Path(stagedir) / "statusline.py"
        shutil.copy(Path(__file__).parent.parent / "statusline.py", staging_file)

        try:
            env = os.environ.copy()
            env["CLAUDE_LUMO_STATE_DIR"] = temp_state_dir
            cmd = [
                "python", "-c",
                "from statusline import install_version; import sys; "
                "sys.stderr.write(install_version() + '\\n')"
            ]
            result = subprocess.run(
                cmd,
                env=env,
                cwd=stagedir,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError(f"subprocess failed: {result.stderr}")
            version = result.stderr.strip()
            assert version == ""
        finally:
            shutil.rmtree(stagedir, ignore_errors=True)
            shutil.rmtree(plain_dir, ignore_errors=True)


class TestCacheWrite:
    """Test cache gets written correctly."""

    def test_cache_written_after_call(self, temp_state_dir, statusline_module_path):
        """Temp dir starts empty, after calling install_version the cache file contains the expected tag/dev."""
        cache_path = os.path.join(temp_state_dir, "statusline-version.cache")

        # Verify cache doesn't exist yet
        assert not os.path.isfile(cache_path)

        version = run_install_version(temp_state_dir, statusline_module_path)

        # Cache should now exist and contain the version
        assert os.path.isfile(cache_path)
        with open(cache_path, encoding="utf-8") as f:
            cached = f.read().strip()
        assert cached == version
        assert version in ("dev",)  # In worktree, should be "dev"


class TestCacheHit:
    """Test cache hit returns cached value without git."""

    def test_cache_hit_returns_cached_without_git(self, temp_state_dir, statusline_module_path):
        """Within the same hour, second call returns the cached value without spawning git."""
        cache_path = os.path.join(temp_state_dir, "statusline-version.cache")

        # First call
        version1 = run_install_version(temp_state_dir, statusline_module_path)
        assert os.path.isfile(cache_path)
        mtime1 = os.path.getmtime(cache_path)

        # Second call immediately
        version2 = run_install_version(temp_state_dir, statusline_module_path)

        # Should get same value
        assert version2 == version1

        # Cache mtime should not have changed (hit, not recompute)
        mtime2 = os.path.getmtime(cache_path)
        assert mtime1 == mtime2, "Cache mtime changed - cache was not hit"


class TestCacheExpiry:
    """Test cache expiry after 1 hour."""

    def test_cache_expired_triggers_refresh(self, temp_state_dir, statusline_module_path):
        """Simulate an old cache (>1 hour) and force a refresh."""
        cache_path = os.path.join(temp_state_dir, "statusline-version.cache")

        # First call to create cache
        version1 = run_install_version(temp_state_dir, statusline_module_path)
        assert os.path.isfile(cache_path)

        # Artificially age the cache to 1 hour + 1 minute
        old_time = os.path.getmtime(cache_path) - 3660  # 1 hour 1 minute ago
        os.utime(cache_path, (old_time, old_time))

        # Verify cache is now considered old
        import time
        assert time.time() - os.path.getmtime(cache_path) > 3600

        # Second call should recompute and update mtime
        version2 = run_install_version(temp_state_dir, statusline_module_path)

        # Should still get a valid version
        assert version2 in ("dev", "")

        # Cache mtime should have updated
        new_mtime = os.path.getmtime(cache_path)
        assert new_mtime > old_time
