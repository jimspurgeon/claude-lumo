"""Tests for statusline.py's install_version() function.

These tests drive install_version() via subprocess to exercise real git and cache behavior,
with CLAUDE_LUMO_STATE_DIR set to a fresh temp dir per test.
"""
import json
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
    """Path to a copy of statusline.py inside a throwaway, untagged git worktree.

    install_version() asks git about the directory statusline.py lives in, so running the real
    file would make these tests depend on whether the real clone's HEAD is tagged: every release
    checkout would fail them (#87). The copy lives in a linked worktree, so .git is a pointer file
    as in an Orca worktree.
    """
    src = Path(__file__).parent.parent.resolve() / "statusline.py"
    base = tempfile.mkdtemp(prefix="untagged-repo-")
    repo, wt = os.path.join(base, "repo"), os.path.join(base, "wt")
    git = ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    try:
        os.makedirs(repo)
        shutil.copy(src, os.path.join(repo, "statusline.py"))
        subprocess.run(git + ["init", "-q", repo], check=True, capture_output=True)
        subprocess.run(git + ["-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(git + ["-C", repo, "commit", "-q", "-m", "initial"], check=True, capture_output=True)
        subprocess.run(git + ["-C", repo, "worktree", "add", "-q", "--detach", wt],
                       check=True, capture_output=True)
        yield os.path.join(wt, "statusline.py")
    finally:
        shutil.rmtree(base, ignore_errors=True)


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


# ---------------------------------------------------------------------------
# Issue #42: model shown must be the one that answered (from the transcript),
# not the configured model. last_transcript_model() is imported via subprocess
# because statusline.py writes the status line at import time.
# ---------------------------------------------------------------------------

def run_last_model(state_dir: str, module_path: str, transcript_path: str) -> str:
    """Run last_transcript_model() via subprocess; returns the repr of the result."""
    env = os.environ.copy()
    env["CLAUDE_LUMO_STATE_DIR"] = state_dir
    cmd = [
        "python", "-c",
        "from statusline import last_transcript_model; import sys; "
        f"sys.stderr.write(repr(last_transcript_model(sys.argv[1])) + '\\n')",
        transcript_path,
    ]
    result = subprocess.run(
        cmd, env=env, cwd=str(Path(module_path).parent),
        capture_output=True, text=True, timeout=10,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"subprocess failed: {result.stderr}")
    return result.stderr.strip()


def run_statusline(state_dir: str, module_path: str, payload: dict) -> str:
    """Run statusline.py end to end with payload on stdin; returns stdout."""
    env = os.environ.copy()
    env["CLAUDE_LUMO_STATE_DIR"] = state_dir
    env.pop("CLAUDE_LUMO", None)  # deterministic [CLAUDE] branch
    result = subprocess.run(
        ["python", module_path], env=env,
        input=json.dumps(payload), capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"subprocess failed: {result.stderr}")
    return result.stdout


def write_transcript(entries):
    """Write entries as JSONL to a temp file; returns its path. Caller cleans up."""
    fd, path = tempfile.mkstemp(prefix="transcript-", suffix=".jsonl")
    with os.fdopen(fd, "wb") as f:
        for e in entries:
            f.write((json.dumps(e) + "\n").encode())
    return path


def assistant_entry(model):
    return {"type": "assistant", "message": {"role": "assistant", "model": model}}


class TestLastTranscriptModel:
    def _dir(self):
        d = tempfile.mkdtemp(prefix="transcripts-")
        self._dirs.append(d)
        return d

    def setup_method(self, _):
        self._dirs = []

    def teardown_method(self, _):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def test_none_for_missing_or_unreadable_path(self, temp_state_dir, statusline_module_path):
        assert run_last_model(temp_state_dir, statusline_module_path, "") == "None"
        assert run_last_model(temp_state_dir, statusline_module_path,
                              str(Path(self._dir()) / "nope.jsonl")) == "None"

    def test_none_for_empty_file(self, temp_state_dir, statusline_module_path):
        p = write_transcript([])
        try:
            assert run_last_model(temp_state_dir, statusline_module_path, p) == "None"
        finally:
            os.unlink(p)

    def test_last_assistant_model_wins(self, temp_state_dir, statusline_module_path):
        p = write_transcript([
            assistant_entry("lumo-max"),
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            assistant_entry("lumo-lite"),
        ])
        try:
            assert run_last_model(temp_state_dir, statusline_module_path, p) == "'lumo-lite'"
        finally:
            os.unlink(p)

    def test_no_assistant_entries_returns_none(self, temp_state_dir, statusline_module_path):
        p = write_transcript([
            {"type": "summary", "summary": "stuff"},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ])
        try:
            assert run_last_model(temp_state_dir, statusline_module_path, p) == "None"
        finally:
            os.unlink(p)

    def test_partial_final_line_is_skipped(self, temp_state_dir, statusline_module_path):
        # The writer can flush mid-record; a truncated last line must be ignored.
        p = write_transcript([assistant_entry("lumo-max")])
        try:
            with open(p, "ab") as f:
                f.write(b'{"type": "assistant", "mess')  # cut mid-JSON, no newline
            assert run_last_model(temp_state_dir, statusline_module_path, p) == "'lumo-max'"
        finally:
            os.unlink(p)

    def test_tail_read_handles_cut_first_line(self, temp_state_dir, statusline_module_path):
        # A tail window starting mid-file cuts the first line in the chunk;
        # that fragment must be dropped, not parsed.
        filler = {"type": "user", "message": {"content": "x" * 40}}
        entries = [filler] * 50 + [assistant_entry("lumo-max")]
        p = write_transcript(entries)
        try:
            # tail smaller than the file: forces the mid-line cut path
            env = os.environ.copy()
            env["CLAUDE_LUMO_STATE_DIR"] = temp_state_dir
            cmd = ["python", "-c",
                   "from statusline import last_transcript_model; import sys; "
                   "sys.stderr.write(repr(last_transcript_model("
                   "sys.argv[1], tail_bytes=int(sys.argv[2]))) + '\\n')",
                   p, str(len(json.dumps(filler)))]
            result = subprocess.run(cmd, env=env,
                                    cwd=str(Path(statusline_module_path).parent),
                                    capture_output=True, text=True, timeout=10,
                                    stdin=subprocess.DEVNULL)
            assert result.returncode == 0, result.stderr
            assert result.stderr.strip() == "'lumo-max'"
        finally:
            os.unlink(p)


class TestNormalization:
    """Issue #42 round 2: transcript ids like 'claude-opus-5-5' vs configured 'opus'
    must NOT be reported as a fallback."""

    def run_helper(self, state_dir, module_path, code):
        env = os.environ.copy()
        env["CLAUDE_LUMO_STATE_DIR"] = state_dir
        cmd = ["python", "-c",
               "from statusline import normalize_model, looks_like_fallback; import sys; "
               f"sys.stderr.write(eval({code!r}) + chr(10))"]
        result = subprocess.run(
            cmd, env=env, cwd=str(Path(module_path).parent),
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"subprocess failed: {result.stderr}")
        return result.stderr.strip()

    def test_normalize_strips_prefix_and_versions(self, temp_state_dir, statusline_module_path):
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(normalize_model('claude-opus-5-5'))")
        assert out == "'opus'"
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(normalize_model('Opus'))")
        assert out == "'opus'"
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(normalize_model('lumo-max'))")
        assert out == "'lumo-max'"

    def test_no_false_fallback_across_id_shapes(self, temp_state_dir, statusline_module_path):
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(looks_like_fallback('claude-opus-5-5', 'opus'))")
        assert out == "False"
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(looks_like_fallback('claude-opus-4', 'opus'))")
        assert out == "False"
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(looks_like_fallback('', 'opus'))")
        assert out == "False"

    def test_real_fallback_still_detected(self, temp_state_dir, statusline_module_path):
        out = self.run_helper(temp_state_dir, statusline_module_path,
                              "repr(looks_like_fallback('lumo-lite', 'lumo-max'))")
        assert out == "True"

    def test_full_id_matching_configured_not_marked(self, temp_state_dir, statusline_module_path):
        """Transcript id 'claude-opus-5-5' with configured 'claude-opus-5-5': no fallback,
        and the [CLAUDE] branch shows the display name."""
        p = write_transcript([assistant_entry("claude-opus-5-5")])
        try:
            payload = {"model": {"id": "claude-opus-5-5", "display_name": "Opus"},
                       "transcript_path": p}
            out = run_statusline(temp_state_dir, statusline_module_path, payload)
            assert "Opus" in out
            assert "fallback" not in out
        finally:
            os.unlink(p)

    def test_short_configured_vs_full_transcript_not_marked(self, temp_state_dir, statusline_module_path):
        """Transcript id 'claude-opus-5-5' with configured 'opus': no fallback shown."""
        p = write_transcript([assistant_entry("claude-opus-5-5")])
        try:
            payload = {"model": {"id": "opus", "display_name": "Opus"},
                       "transcript_path": p}
            out = run_statusline(temp_state_dir, statusline_module_path, payload)
            assert "fallback" not in out
        finally:
            os.unlink(p)


class TestStatuslineOutput:
    def test_shows_configured_model_when_no_fallback(self, temp_state_dir, statusline_module_path):
        payload = {"model": {"id": "lumo-max", "display_name": "Lumo Max"},
                   "transcript_path": None}
        out = run_statusline(temp_state_dir, statusline_module_path, payload)
        # [CLAUDE] branch prefers the display name when nothing fell back
        assert "Lumo Max" in out
        assert "fallback" not in out

    def test_shows_actual_model_on_fallback(self, temp_state_dir, statusline_module_path):
        p = write_transcript([assistant_entry("lumo-lite")])
        try:
            payload = {"model": {"id": "lumo-max", "display_name": "Lumo Max"},
                       "transcript_path": p}
            out = run_statusline(temp_state_dir, statusline_module_path, payload)
            assert "lumo-lite (fallback from lumo-max)" in out
        finally:
            os.unlink(p)

    def test_no_transcript_shows_configured(self, temp_state_dir, statusline_module_path):
        payload = {"model": {"id": "lumo-max"}}
        out = run_statusline(temp_state_dir, statusline_module_path, payload)
        assert "lumo-max" in out
        assert "fallback" not in out


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Issue #63: a fallback is visible in the transcript (the turns lumo-lite answers
# are recorded as lumo-lite); failed turns are "<synthetic>" and name no model.
# Entry shapes match real Claude Code 2.1.296 transcripts.
# ---------------------------------------------------------------------------

def fallback_entry():
    return {"type": "system", "subtype": "model_fallback", "isMeta": False, "level": "warning",
            "originalModel": "lumo-max", "fallbackModel": "lumo-lite", "trigger": "server_error",
            "content": "Switched to lumo-lite due to high demand for lumo-max"}


def synthetic_entry():
    # Claude Code's record of a failed request: no model produced it.
    return {"type": "assistant", "isApiErrorMessage": True, "isSidechain": False,
            "message": {"role": "assistant", "model": "<synthetic>", "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": "API Error: 400"}]}}


class TestFallbackReplay:
    def test_fallback_then_lite_answer_shows_fallback(self, temp_state_dir, statusline_module_path):
        path = write_transcript([assistant_entry("lumo-max"), fallback_entry(),
                                 assistant_entry("lumo-lite")])
        try:
            out = run_statusline(temp_state_dir, statusline_module_path,
                                 {"model": {"id": "lumo-max"}, "transcript_path": path})
        finally:
            os.unlink(path)
        assert "lumo-lite (fallback from lumo-max)" in out

    def test_synthetic_error_turn_is_skipped(self, temp_state_dir, statusline_module_path):
        path = write_transcript([assistant_entry("lumo-lite"), synthetic_entry()])
        try:
            out = run_statusline(temp_state_dir, statusline_module_path,
                                 {"model": {"id": "lumo-max"}, "transcript_path": path})
        finally:
            os.unlink(path)
        assert "lumo-lite (fallback from lumo-max)" in out
        assert "synthetic" not in out

    def test_fallback_that_never_answered_keeps_configured(self, temp_state_dir, statusline_module_path):
        # The fallback request failed too: no model answered, so nothing fell back.
        path = write_transcript([assistant_entry("lumo-max"), fallback_entry(),
                                 synthetic_entry()])
        try:
            out = run_statusline(temp_state_dir, statusline_module_path,
                                 {"model": {"id": "lumo-max"}, "transcript_path": path})
        finally:
            os.unlink(path)
        assert "fallback" not in out and "synthetic" not in out
        assert "lumo-max" in out


# ---------------------------------------------------------------------------
# Issue #65: on Lumo the ctx percentage is measured against the auto-compact
# trigger (window - 20000 - 13000), so 100% means compaction is due.
# ---------------------------------------------------------------------------

def run_lumo_statusline(state_dir: str, module_path: str, payload: dict, extra_env=None) -> str:
    env = os.environ.copy()
    env["CLAUDE_LUMO_STATE_DIR"] = state_dir
    env["CLAUDE_LUMO"] = "1"
    env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
    env.pop("CLAUDE_CODE_MAX_OUTPUT_TOKENS", None)
    env.update(extra_env or {})
    result = subprocess.run(
        ["python", module_path], env=env,
        input=json.dumps(payload), capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"subprocess failed: {result.stderr}")
    return result.stdout


def write_settings(state_dir: str, window) -> None:
    with open(os.path.join(state_dir, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"autoCompactWindow": window} if window else {}, f)


def usage_payload(tokens: int, used_pct=None) -> dict:
    cw = {"context_window_size": 131072,
          "current_usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": tokens - 1000, "output_tokens": 500}}
    if used_pct is not None:
        cw["used_percentage"] = used_pct
    return {"model": {"id": "lumo-max"}, "context_window": cw}


class TestCompactThresholdPercentage:
    def test_percentage_of_trigger_not_of_window(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 110000)  # trigger at 110000 - 20000 - 13000 = 77000
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, usage_payload(38500, 29))
        assert "ctx 50%" in out

    def test_near_trigger_reads_near_full(self, temp_state_dir, statusline_module_path):
        # The #65 symptom: the window-based figure says ~35% when compaction is about due.
        write_settings(temp_state_dir, 100000)  # trigger 67000
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, usage_payload(66330, 35))
        assert "ctx 99%" in out

    def test_capped_at_100(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 100000)
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, usage_payload(90000, 69))
        assert "ctx 100%" in out

    def test_env_overrides_setting(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 110000)
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, usage_payload(33500),
                                  {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"})
        assert "ctx 50%" in out

    def test_falls_back_to_used_percentage_without_window(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, None)
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, usage_payload(40000, 31))
        assert "ctx 31%" in out

    def test_without_current_usage_uses_percentage_times_size(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 110000)
        payload = {"model": {"id": "lumo-max"},
                   "context_window": {"context_window_size": 154000, "used_percentage": 25}}
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, payload)
        assert "ctx 50%" in out  # 38500 of 77000

    def test_window_capped_by_model_size(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 1000000)
        payload = usage_payload(49000)
        payload["context_window"]["context_window_size"] = 131000  # trigger 98000
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, payload)
        assert "ctx 50%" in out

    def test_unreadable_settings_does_not_break(self, temp_state_dir, statusline_module_path):
        with open(os.path.join(temp_state_dir, "settings.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, usage_payload(40000, 31))
        assert "ctx 31%" in out

    def test_no_context_window_shows_nothing(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 110000)
        out = run_lumo_statusline(temp_state_dir, statusline_module_path, {"model": {"id": "lumo-max"}})
        assert "ctx" not in out

    def test_plain_claude_keeps_used_percentage(self, temp_state_dir, statusline_module_path):
        write_settings(temp_state_dir, 110000)
        out = run_statusline(temp_state_dir, statusline_module_path, usage_payload(38500, 29))
        assert "ctx 29%" in out
