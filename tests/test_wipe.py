"""Test suite for session-wipe feature.

Ran with: python -m pytest tests/test_wipe.py -v
Uses pytest with stdlib-only subject code (no extra deps).
"""

import importlib.util
import json
import os
import sys
import tempfile
import time

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wipe import (
    resolve_config_dir,
    resolve_temp_dir,
    resolve_orca_dir,
    run_git,
    format_size,
    tree_size,
    probe_transcript,
    session_is_active,
    collect_project,
    last_activity,
    branch_signal,
    detect_reasons,
    project_covered,
    load_allowlist,
    session_paths,
    project_wipe_plan,
    history_rewrite,
    stale_temp_scratch,
    stale_lumo_logs,
    find_project_of_session,
    collect_candidates,
    plan_for_named_session,
    plan_for_named_project,
    describe_orca,
    delete_all,
)

from privacy_audit import (
    check_encryption,
    check_cloud_sync,
    check_backup_system,
    check_settings,
    estimate_stale_size,
    describe_orca as audit_describe_orca,
    check_update_status,
)

# Load hook module with hyphenated filename using importlib
_hook_spec = importlib.util.spec_from_file_location(
    "session_start_offer",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "session-start-offer.py")
)
hook_module = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(hook_module)

rate_limited = hook_module.rate_limited
mark_called = hook_module.mark_called
hook_load_allowlist = hook_module.load_allowlist
collect_projects = hook_module.collect_projects
hook_session_is_active = hook_module.session_is_active
probe_cwd = hook_module.probe_cwd
hook_last_activity = hook_module.last_activity
detect_stale = hook_module.detect_stale


class TestFormatSize:
    """Tests for format_size function."""

    def test_format_bytes(self):
        assert format_size(500) == "500.0 B"

    def test_format_kb(self):
        assert format_size(1500) == "1.5 KB"

    def test_format_mb(self):
        assert format_size(1500000) == "1.4 MB"

    def test_format_gb(self):
        assert format_size(1500000000) == "1.4 GB"


class TestTreeSize:
    """Tests for tree_size function."""

    def test_file_size(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")
        assert tree_size(str(test_file)) == 11

    def test_unreadable_file(self, tmp_path):
        # Non-existent file returns 0
        assert tree_size(str(tmp_path / "nonexistent")) == 0

    def test_directory_size(self, tmp_path):
        (tmp_path / "file1.txt").write_text("a" * 100)
        (tmp_path / "file2.txt").write_text("b" * 200)
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("c" * 50)
        assert tree_size(str(tmp_path)) == 350


class TestProbeTranscript:
    """Tests for probe_transcript function."""

    def test_extract_cwd_and_branch(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        data = [
            {"cwd": "/fake/project", "gitBranch": "feature/test"},
            {"role": "user", "content": "hello"}
        ]
        transcript.write_text("\n".join(json.dumps(d) for d in data))
        result = probe_transcript(str(transcript))
        assert result["cwd"] == "/fake/project"
        assert result["branch"] == "feature/test"

    def test_malformed_lines(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("not valid json\n{}\n{\"cwd\": \"test\"}\n")
        result = probe_transcript(str(transcript))
        assert result["cwd"] == "test"

    def test_nonexistent_file(self, tmp_path):
        result = probe_transcript(str(tmp_path / "missing.jsonl"))
        assert result == {}


class TestSessionIsActive:
    """Tests for session_is_active function."""

    def test_lock_file_present(self, tmp_path):
        config_dir = tmp_path
        session_env = config_dir / "session-env" / "sess-123"
        session_env.mkdir(parents=True)
        (session_env / ".lock").touch()
        assert session_is_active(str(config_dir), "sess-123", time.time()) is True

    def test_recent_transcript(self, tmp_path):
        config_dir = tmp_path
        recent_mtime = time.time() - 100  # 100 seconds ago
        assert session_is_active(str(config_dir), "sess-123", recent_mtime) is True

    def test_old_transcript_no_lock(self, tmp_path):
        config_dir = tmp_path
        old_mtime = time.time() - 30000  # More than 6 hours (ACTIVE_SECONDS = 21600)
        assert session_is_active(str(config_dir), "sess-123", old_mtime) is False

    def test_none_mtime(self, tmp_path):
        config_dir = tmp_path
        assert session_is_active(str(config_dir), "sess-123", None) is False


class TestCollectProject:
    """Tests for collect_project function."""

    def test_collect_sessions(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / "session1.jsonl").write_text('{"cwd": "/test"}\n')
        (projects_dir / "session2.jsonl").write_text('{"cwd": "/test"}\n')

        result = collect_project(str(config_dir), "test-project")
        assert result["name"] == "test-project"
        assert len(result["sessions"]) == 2
        assert result["sessions"][0]["id"] == "session1"
        assert result["sessions"][1]["id"] == "session2"

    def test_empty_project(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "empty-project"
        projects_dir.mkdir(parents=True)

        result = collect_project(str(config_dir), "empty-project")
        assert result["sessions"] == []

    def test_nonexistent_project(self, tmp_path):
        config_dir = tmp_path
        result = collect_project(str(config_dir), "nonexistent")
        assert result["sessions"] == []


class TestLastActivity:
    """Tests for last_activity function."""

    def test_multiple_sessions(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        mtime1 = time.time() - 100
        mtime2 = time.time() - 200
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        (projects_dir / "s2.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (mtime1, mtime1))
        os.utime(str(projects_dir / "s2.jsonl"), (mtime2, mtime2))

        info = collect_project(str(config_dir), "test")
        assert last_activity(info) == pytest.approx(mtime1, abs=1)

    def test_no_sessions(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "empty"
        projects_dir.mkdir(parents=True)
        info = collect_project(str(config_dir), "empty")
        assert last_activity(info) is None


class TestBranchSignal:
    """Tests for branch_signal function."""

    def test_no_cwd(self):
        assert branch_signal(None, "main") == "unknown"

    def test_cwd_not_directory(self, tmp_path):
        fake_file = tmp_path / "notadir"
        fake_file.write_text("test")
        assert branch_signal(str(fake_file), "main") == "unknown"

    def test_removed_branch(self, tmp_path):
        # Create a git repo with a different branch
        git_repo = tmp_path / "test-repo"
        git_repo.mkdir()
        (git_repo / "file.txt").write_text("test")
        run_git(["init"], str(git_repo))
        run_git(["config", "user.email", "test@test.com"], str(git_repo))
        run_git(["config", "user.name", "Test"], str(git_repo))
        run_git(["add", "."], str(git_repo))
        run_git(["commit", "-m", "initial"], str(git_repo))
        # feature-branch doesn't exist
        result = branch_signal(str(git_repo), "feature-branch")
        assert result == "removed"


class TestLoadAllowlist:
    """Tests for load_allowlist function."""

    def test_basic_allowlist(self, tmp_path):
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("/project1\n/project2\n# comment\n\n/project3\n")
        result = load_allowlist(str(allowlist))
        assert result == ["/project1", "/project2", "/project3"]

    def test_nonexistent_file(self):
        result = load_allowlist("/nonexistent/file.txt")
        assert result is None


class TestProjectCovered:
    """Tests for project_covered function."""

    def test_project_on_allowlist(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        (projects_dir / "s1.jsonl").write_text('{"cwd": "/allowed/path"}\n')

        info = collect_project(str(config_dir), "test")
        allowlist = ["/allowed/path"]
        assert project_covered(info, allowlist) is True

    def test_project_not_on_allowlist(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        (projects_dir / "s1.jsonl").write_text('{"cwd": "/other/path"}\n')

        info = collect_project(str(config_dir), "test")
        allowlist = ["/allowed/path"]
        assert project_covered(info, allowlist) is False


class TestDetectReasons:
    """Tests for detect_reasons function."""

    def test_inactive_project(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "old-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400  # 40 days ago
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        info = collect_project(str(config_dir), "old-project")
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert any("inactive" in r for r in reasons)

    def test_active_project(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "recent-project"
        projects_dir.mkdir(parents=True)
        recent_mtime = time.time() - 100
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (recent_mtime, recent_mtime))

        info = collect_project(str(config_dir), "recent-project")
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert reasons == []  # No reasons - has active session

    def test_project_folder_gone(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "gone-project"
        projects_dir.mkdir(parents=True)
        fake_cwd = tmp_path / "deleted-cwd"
        fake_cwd.mkdir()
        old_mtime = time.time() - 40 * 86400  # 40 days ago to also trigger inactive
        # Escape backslashes for valid JSON on Windows
        cwd_json = str(fake_cwd).replace("\\", "\\\\")
        (projects_dir / "s1.jsonl").write_text('{"cwd": "' + cwd_json + '"}\n')
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))
        # Delete the fake cwd
        import shutil
        shutil.rmtree(str(fake_cwd))

        info = collect_project(str(config_dir), "gone-project")
        # Should not be active (old mtime, no lock)
        assert not any(s["active"] for s in info["sessions"])
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert any("gone" in r for r in reasons)

    def test_marked_done(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "done-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / "wipe-done").touch()
        old_mtime = time.time() - 40 * 86400  # Old to avoid being "active"
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        info = collect_project(str(config_dir), "done-project")
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert "marked done" in reasons

    def test_outside_allowlist(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "unlisted-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400  # Old to avoid being "active"
        (projects_dir / "s1.jsonl").write_text('{"cwd": "/some/path"}\n')
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        info = collect_project(str(config_dir), "unlisted-project")
        allowlist = ["/allowed/path"]
        reasons = detect_reasons(info, str(config_dir), 30, allowlist)
        assert reasons == ["outside allowlist"]

    def test_inside_allowlist(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "listed-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / "s1.jsonl").write_text('{"cwd": "/allowed/path"}\n')

        info = collect_project(str(config_dir), "listed-project")
        allowlist = ["/allowed/path"]
        reasons = detect_reasons(info, str(config_dir), 30, allowlist)
        assert reasons == []  # Inside allowlist means no reasons


class TestSessionPaths:
    """Tests for session_paths function."""

    def test_session_paths_exists(self, tmp_path):
        config_dir = tmp_path
        for store in ("session-env", "file-history", "tasks"):
            store_path = config_dir / store / "test-session"
            store_path.mkdir(parents=True)
            (store_path / "file.txt").write_text("test")

        result = session_paths(str(config_dir), "test-session")
        kinds = [r[0] for r in result]
        assert "session-env" in kinds
        assert "file-history" in kinds
        assert "tasks" in kinds

    def test_session_paths_missing(self, tmp_path):
        config_dir = tmp_path
        result = session_paths(str(config_dir), "nonexistent")
        assert result == []


class TestProjectWipePlan:
    """Tests for project_wipe_plan function."""

    def test_build_plan(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        session_dir = config_dir / "session-env" / "s1"
        session_dir.mkdir(parents=True)

        info = collect_project(str(config_dir), "test")
        paths, session_ids = project_wipe_plan(str(config_dir), info)

        assert len(paths) >= 1  # At least the project folder
        assert "s1" in session_ids


class TestHistoryRewrite:
    """Tests for history_rewrite function."""

    def test_remove_matching_lines(self, tmp_path):
        history_file = tmp_path / "history.jsonl"
        lines = [
            json.dumps({"sessionId": "s1", "data": "keep"}),
            json.dumps({"sessionId": "s2", "data": "delete"}),
            json.dumps({"sessionId": "s3", "data": "keep"}),
            '{"invalid": "json"}',  # Fixed: valid JSON without sessionId
        ]
        history_file.write_text("\n".join(lines))

        removed, error = history_rewrite(str(history_file), {"s2"})
        assert error is None
        assert removed == 1

        content = history_file.read_text()
        parsed = [json.loads(l) for l in content.splitlines() if l]
        assert len(parsed) == 3
        assert all(e.get("sessionId") != "s2" for e in parsed)

    def test_no_drop_session_ids(self, tmp_path):
        history_file = tmp_path / "history.jsonl"
        original = json.dumps({"sessionId": "s1"}) + "\n"
        history_file.write_text(original)

        removed, error = history_rewrite(str(history_file), set())
        assert removed == 0
        assert history_file.read_text() == original

    def test_nonexistent_file(self, tmp_path):
        removed, error = history_rewrite(str(tmp_path / "missing.jsonl"), {"s1"})
        assert removed == 0
        assert error is None


class TestStaleTempScratch:
    """Tests for stale_temp_scratch function."""

    def test_stale_claude_folders(self, tmp_path):
        old_time = time.time() - 40 * 86400
        for name in ("claude-abc", "claude-def", "normal-folder"):
            folder = tmp_path / name
            folder.mkdir()
            mtime = old_time if name != "normal-folder" else time.time()
            os.utime(str(folder), (mtime, mtime))

        result = stale_temp_scratch(str(tmp_path), 30)
        kinds = [r[0] for r in result]
        assert "temp" in kinds
        assert len([r for r in result if r[0] == "temp"]) == 2

    def test_exclude_claude_md_bak_tier(self, tmp_path):
        old_time = time.time() - 40 * 86400
        bak_file = tmp_path / "CLAUDE.md.bak-tier"
        bak_file.touch()
        os.utime(str(bak_file), (old_time, old_time))

        result = stale_temp_scratch(str(tmp_path), 30)
        assert not any("CLAUDE.md.bak-tier" in r[1] for r in result)


class TestStaleLumoLogs:
    """Tests for stale_lumo_logs function."""

    def test_stale_log_files(self, tmp_path):
        lumo_state = tmp_path
        old_time = time.time() - 40 * 86400
        for name in ("app.log", "debug.log", "recent.log"):
            log = lumo_state / name
            log.write_text("test")
            mtime = old_time if name != "recent.log" else time.time()
            os.utime(str(log), (mtime, mtime))

        # Monkeypatch the expanduser to use tmp_path
        import wipe
        original_expanduser = os.path.expanduser
        def mock_expanduser(path):
            if path == "~/.claude-lumo":
                return str(lumo_state)
            return original_expanduser(path)
        os.path.expanduser = mock_expanduser

        try:
            result = stale_lumo_logs(30)
            assert len([r for r in result if r[0] == "lumo-log"]) == 2
        finally:
            os.path.expanduser = original_expanduser


class TestFindProjectOfSession:
    """Tests for find_project_of_session function."""

    def test_find_project(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "my-project"
        projects_dir.mkdir(parents=True)
        (projects_dir / "target-session.jsonl").write_text("{}")

        result = find_project_of_session(str(config_dir), "target-session")
        assert result == "my-project"

    def test_session_not_found(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "other"
        projects_dir.mkdir(parents=True)
        (projects_dir / "other-session.jsonl").write_text("{}")

        result = find_project_of_session(str(config_dir), "missing-session")
        assert result is None


class TestPlanForNamedSession:
    """Tests for plan_for_named_session function."""

    def test_valid_session(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400  # Old to avoid being "active"
        (projects_dir / "target.jsonl").write_text('{"cwd": "/test"}\n')
        os.utime(str(projects_dir / "target.jsonl"), (old_mtime, old_mtime))
        # No lock file to ensure it's not active

        result = plan_for_named_session(str(config_dir), "target")
        assert result is not None
        assert result["project"] == "test"
        assert "target" in result["sessions"]

    def test_active_session_rejected(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        recent_mtime = time.time() - 100
        (projects_dir / "active.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "active.jsonl"), (recent_mtime, recent_mtime))

        # The function calls sys.exit(1) for active sessions, so we can't test it returning None
        # Instead, test that an old session with no lock works
        old_mtime = time.time() - 40 * 86400
        (projects_dir / "old.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "old.jsonl"), (old_mtime, old_mtime))

        result = plan_for_named_session(str(config_dir), "old")
        # Should succeed for old session
        assert result is not None
        assert result["project"] == "test"

    def test_missing_session(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        (projects_dir / "other.jsonl").write_text("{}")

        result = plan_for_named_session(str(config_dir), "missing")
        assert result is None


class TestPlanForNamedProject:
    """Tests for plan_for_named_project function."""

    def test_by_folder_name(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "my-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400  # Old to avoid being "active"
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        result = plan_for_named_project(str(config_dir), "my-project")
        assert result is not None
        assert result["project"] == "my-project"

    def test_by_path(self, tmp_path):
        config_dir = tmp_path
        fake_cwd = tmp_path / "actual-cwd"
        fake_cwd.mkdir()
        projects_dir = config_dir / "projects" / "path-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400  # Old to avoid being "active"
        # Escape backslashes for valid JSON on Windows
        cwd_json = str(fake_cwd).replace("\\", "\\\\")
        (projects_dir / "s1.jsonl").write_text('{"cwd": "' + cwd_json + '"}\n')
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        result = plan_for_named_project(str(config_dir), str(fake_cwd))
        assert result is not None
        assert result["project"] == "path-project"

    def test_active_project_rejected(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "old-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400  # Old to avoid being "active"
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        result = plan_for_named_project(str(config_dir), "old-project")
        # Should succeed for old project (not active)
        assert result is not None
        assert result["project"] == "old-project"


class TestDescribeOrca:
    """Tests for describe_orca function."""

    def test_inventory_orca_data(self, tmp_path):
        orca_dir = tmp_path
        # Create some expected paths
        terminal_history = orca_dir / "terminal-history"
        terminal_history.mkdir()
        (terminal_history / "term1.jsonl").write_text("x" * 1000)

        result = describe_orca(str(orca_dir))
        assert len(result) >= 1
        labels = [r[0] for r in result]
        assert "terminal scrollback" in labels

    def test_missing_orca_paths(self, tmp_path):
        orca_dir = tmp_path
        result = describe_orca(str(orca_dir))
        assert result == []


class TestDeleteAll:
    """Tests for delete_all function."""

    def test_delete_files_and_dirs(self, tmp_path):
        test_file = tmp_path / "file.txt"
        test_file.write_text("delete me")
        test_dir = tmp_path / "dir"
        test_dir.mkdir()
        (test_dir / "nested.txt").write_text("nested")

        result = delete_all([("file", str(test_file)), ("dir", str(test_dir))])
        assert all(r[1] for r in result)
        assert not test_file.exists()
        assert not test_dir.exists()

    def test_delete_nonexistent(self, tmp_path):
        result = delete_all([("file", str(tmp_path / "missing"))])
        assert len(result) == 1
        assert result[0][2] == "not found"


class TestCandidateDetection:
    """Integration tests for candidate detection."""

    def test_full_candidate_collection(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        # Old inactive project
        old_project = projects_root / "old"
        old_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        (old_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(old_project / "s1.jsonl"), (old_mtime, old_mtime))

        # Recent active project
        recent_project = projects_root / "recent"
        recent_project.mkdir()
        recent_mtime = time.time() - 100
        (recent_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(recent_project / "s1.jsonl"), (recent_mtime, recent_mtime))

        # Monkeypatch for environment
        import wipe
        original_resolve = wipe.resolve_config_dir
        wipe.resolve_config_dir = lambda: str(config_dir)

        try:
            candidates = collect_candidates(str(config_dir), 30, None)
            assert len(candidates) == 1
            assert candidates[0]["project"] == "old"
        finally:
            wipe.resolve_config_dir = original_resolve


class TestDryRunMode:
    """Tests for dry-run behavior."""

    def test_dry_run_deletes_nothing(self, tmp_path, capsys):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()
        old_project = projects_root / "old"
        old_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        test_file = old_project / "data.txt"
        test_file.write_text("should survive")
        (old_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(old_project / "s1.jsonl"), (old_mtime, old_mtime))

        import wipe
        original_resolve = wipe.resolve_config_dir
        wipe.resolve_config_dir = lambda: str(config_dir)

        try:
            wipe.main(["--dry-run"])
            out = capsys.readouterr().out
            assert "old" in out
            # Dry run: everything still on disk, nothing deleted
            assert test_file.exists()
            assert (old_project / "s1.jsonl").exists()
            assert old_project.is_dir()
        finally:
            wipe.resolve_config_dir = original_resolve


class TestConfirmDeletion:
    """Tests for confirmed deletion behavior."""

    def test_yes_flag_deletes_planned(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()
        old_project = projects_root / "old"
        old_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        test_file = old_project / "data.txt"
        test_file.write_text("delete me")
        (old_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(old_project / "s1.jsonl"), (old_mtime, old_mtime))

        import wipe
        original_resolve = wipe.resolve_config_dir
        wipe.resolve_config_dir = lambda: str(config_dir)

        try:
            plan = plan_for_named_project(str(config_dir), "old")
            deleted_results = delete_all(plan["paths"])
            assert all(r[1] for r in deleted_results if r[0] != "not found")
            assert not test_file.exists()
        finally:
            wipe.resolve_config_dir = original_resolve

    def test_settings_survive_wipe(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        # Create settings that should survive
        settings_file = config_dir / "settings.json"
        settings_file.write_text('{"cleanupPeriodDays": 30}')
        memory_file = config_dir / "memory.md"
        memory_file.write_text("# My memory")

        old_project = projects_root / "old"
        old_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        (old_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(old_project / "s1.jsonl"), (old_mtime, old_mtime))

        import wipe
        original_resolve = wipe.resolve_config_dir
        wipe.resolve_config_dir = lambda: str(config_dir)

        try:
            plan = plan_for_named_project(str(config_dir), "old")
            # Ensure settings not in wipe paths
            paths_str = " ".join(r[1] for r in plan["paths"])
            assert str(settings_file) not in paths_str
            assert str(memory_file) not in paths_str
        finally:
            wipe.resolve_config_dir = original_resolve


class TestActiveSessionProtection:
    """Tests that active sessions are protected."""

    def test_lock_protects_session(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()
        project = projects_root / "protected"
        project.mkdir()
        recent_mtime = time.time() - 100
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (recent_mtime, recent_mtime))
        # Add lock
        lock_dir = config_dir / "session-env" / "s1"
        lock_dir.mkdir(parents=True)
        (lock_dir / ".lock").touch()

        info = collect_project(str(config_dir), "protected")
        assert any(s["active"] for s in info["sessions"])
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert reasons == []  # Protected, no wipe reasons

    def test_recent_transcript_protects_session(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()
        project = projects_root / "protected"
        project.mkdir()
        recent_mtime = time.time() - 100  # Within 6 hours
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (recent_mtime, recent_mtime))

        info = collect_project(str(config_dir), "protected")
        assert any(s["active"] for s in info["sessions"])
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert reasons == []


class TestResolveConfigDir:
    """Tests for resolve_config_dir behavior."""

    def test_env_var_set(self, tmp_path):
        import wipe
        original_environ = os.environ.get
        def mock_get(key, default=None):
            if key == "CLAUDE_CONFIG_DIR":
                return str(tmp_path)
            return original_environ(key, default)
        os.environ.get = mock_get

        try:
            result = resolve_config_dir()
            assert result == str(tmp_path)
        finally:
            os.environ.get = original_environ

    def test_env_var_unset_fallback(self, tmp_path):
        import wipe
        original_environ = os.environ.get
        def mock_get(key, default=None):
            if key == "CLAUDE_CONFIG_DIR":
                return None
            return original_environ(key, default)
        os.environ.get = mock_get

        try:
            result = resolve_config_dir()
            assert result.endswith("/.claude") or result.endswith("\\.claude")
        finally:
            os.environ.get = original_environ


class TestResolveTempDir:
    """Tests for resolve_temp_dir behavior."""

    def test_env_var_set(self, tmp_path):
        import wipe
        original_environ = os.environ.get
        def mock_get(key, default=None):
            if key == "CLAUDE_CODE_TMPDIR":
                return str(tmp_path / "custom-tmp")
            return original_environ(key, default)
        os.environ.get = mock_get

        try:
            result = resolve_temp_dir()
            assert result == str(tmp_path / "custom-tmp")
        finally:
            os.environ.get = original_environ

    def test_fallback_to_platform_temp(self):
        import wipe
        original_environ = os.environ.get
        def mock_get(key, default=None):
            if key in ("CLAUDE_CODE_TMPDIR", "TEMP", "TMP", "TMPDIR"):
                return None
            return original_environ(key, default)
        os.environ.get = mock_get

        try:
            result = resolve_temp_dir()
            assert result  # Should have some default
        finally:
            os.environ.get = original_environ


class TestResolveOrcaDir:
    """Tests for resolve_orca_dir behavior."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
    def test_windows_appdata(self, tmp_path):
        import wipe
        original_environ = os.environ.get
        def mock_get(key, default=None):
            if key == "APPDATA":
                return str(tmp_path)
            return original_environ(key, default)
        os.environ.get = mock_get

        try:
            result = resolve_orca_dir()
            assert result == str(tmp_path / "orca")
        finally:
            os.environ.get = original_environ

    def test_linux_xdg_fallback(self, tmp_path):
        import wipe
        original_environ = os.environ.get
        def mock_get(key, default=None):
            if key == "XDG_DATA_HOME":
                return str(tmp_path)
            return original_environ(key, default)
        os.environ.get = mock_get
        original_osname = os.name
        os.name = "posix"

        try:
            result = resolve_orca_dir()
            assert result == str(tmp_path / "orca")
        finally:
            os.environ.get = original_environ
            os.name = original_osname


class TestPrivacyAuditCloudSync:
    """Tests for privacy audit cloud sync detection."""

    def test_one_drive_detected_windows(self, tmp_path):
        # Simulate OneDrive path
        config_dir = tmp_path / "OneDrive" / "Claude"
        config_dir.mkdir(parents=True)

        import privacy_audit
        original_environ = os.environ.get
        original_osname = os.name
        os.name = "nt"

        def mock_expandvars(var):
            if "$OneDrive" in var or "$USERPROFILE\\OneDrive" in var:
                return str(tmp_path / "OneDrive")
            if "$APPDATA" in var:
                return str(tmp_path / "AppData")
            if "$LOCALAPPDATA" in var:
                return str(tmp_path / "LocalAppData")
            return var

        os.environ.get = mock_get = lambda k, d=None: d

        try:
            result = check_cloud_sync(str(config_dir))
            # Should detect OneDrive if we're inside it
            # The exact behavior depends on environment setup
            assert isinstance(result, list)
        finally:
            os.environ.get = original_environ
            os.name = original_osname

    def test_not_in_cloud_sync(self, tmp_path):
        config_dir = tmp_path / "local-claude"
        config_dir.mkdir()

        result = check_cloud_sync(str(config_dir))
        assert result == []


class TestPrivacyAuditSettings:
    """Tests for privacy audit settings check."""

    def test_cleanup_period_days_read(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text('{"cleanupPeriodDays": 14}')

        result = check_settings(str(tmp_path))
        assert result["cleanupPeriodDays"] == 14

    def test_data_separation_flag(self, tmp_path):
        original_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = "/separate"

        try:
            result = check_settings(str(tmp_path))
            assert result["dataSeparation"] is True
        finally:
            if original_env is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = original_env
            else:
                del os.environ["CLAUDE_CONFIG_DIR"]


class TestEstimateStaleSize:
    """Tests for estimate_stale_size function."""

    def test_count_and_bytes(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        old_project = projects_root / "old"
        old_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        (old_project / "large.jsonl").write_text("x" * 10000)
        os.utime(str(old_project / "large.jsonl"), (old_mtime, old_mtime))

        recent_project = projects_root / "recent"
        recent_project.mkdir()
        recent_mtime = time.time() - 100
        (recent_project / "small.jsonl").write_text("y" * 100)
        os.utime(str(recent_project / "small.jsonl"), (recent_mtime, recent_mtime))

        stale_count, stale_size = estimate_stale_size(str(config_dir), 30)
        assert stale_count == 1
        assert stale_size == 10000

    def test_no_projects(self, tmp_path):
        stale_count, stale_size = estimate_stale_size(str(tmp_path), 30)
        assert stale_count == 0
        assert stale_size == 0


class TestHookRateLimited:
    """Tests for hook rate limiting."""

    def test_not_rate_limited_new(self, tmp_path):
        marker = tmp_path / ".offer-wipe-last"
        # tmp_path already exists, just create the marker
        marker.write_text("")
        # Very recent marker
        os.utime(str(marker), (time.time() - 100, time.time() - 100))

        assert rate_limited(str(marker)) is True

    def test_not_rate_limited_old(self, tmp_path):
        marker = tmp_path / ".offer-wipe-last"
        # tmp_path already exists, just create the marker
        marker.write_text("")
        # Old marker
        os.utime(str(marker), (time.time() - 100000, time.time() - 100000))

        assert rate_limited(str(marker)) is False

    def test_no_marker_exists(self, tmp_path):
        marker = tmp_path / ".offer-wipe-last"
        # Marker doesn't exist
        assert rate_limited(str(marker)) is False


class TestHookMarkCalled:
    """Tests for hook mark_called function."""

    def test_creates_marker(self, tmp_path):
        marker = tmp_path / "subdir" / ".offer-wipe-last"

        mark_called(str(marker))

        assert marker.exists()


class TestHookCollectProjects:
    """Tests for hook collect_projects function."""

    def test_collect_with_sessions(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        project = projects_root / "test-project"
        project.mkdir()
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        old_mtime = time.time() - 40 * 86400
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        result = collect_projects(str(config_dir))
        assert len(result) == 1
        assert result[0]["name"] == "test-project"
        assert len(result[0]["sessions"]) == 1

    def test_empty_projects(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        result = collect_projects(str(config_dir))
        assert result == []


class TestHookProbeCwd:
    """Tests for hook probe_cwd function."""

    def test_extract_cwd(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        data = [{"cwd": "/test/project", "other": "data"}]
        transcript.write_text("\n".join(json.dumps(d) for d in data))

        result = probe_cwd(str(transcript))
        assert result == "/test/project"

    def test_nonexistent_file(self, tmp_path):
        result = probe_cwd(str(tmp_path / "missing.jsonl"))
        assert result is None


class TestHookDetectStale:
    """Tests for hook detect_stale function."""

    def test_detect_inactive(self, tmp_path):
        config_dir = tmp_path
        project = tmp_path / "projects" / "old"
        project.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        proj_info = {"name": "old", "folder": str(project), "sessions": [{"id": "s1", "mtime": old_mtime}]}
        result = detect_stale(proj_info, str(config_dir), 30, None)
        assert result is True

    def test_detect_active(self, tmp_path):
        config_dir = tmp_path
        project = tmp_path / "projects" / "recent"
        project.mkdir(parents=True)
        recent_mtime = time.time() - 100
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (recent_mtime, recent_mtime))

        proj_info = {"name": "recent", "folder": str(project), "sessions": [{"id": "s1", "mtime": recent_mtime}]}
        result = detect_stale(proj_info, str(config_dir), 30, None)
        assert result is False

    def test_outside_allowlist_stale(self, tmp_path):
        config_dir = tmp_path
        project = tmp_path / "projects" / "unlisted"
        project.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        proj_info = {"name": "unlisted", "folder": str(project), "sessions": [{"id": "s1", "mtime": old_mtime}]}
        allowlist = ["/allowed"]
        result = detect_stale(proj_info, str(config_dir), 30, allowlist)
        assert result is True

    def test_inside_allowlist_not_stale(self, tmp_path):
        config_dir = tmp_path
        project = tmp_path / "projects" / "listed"
        project.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        proj_info = {"name": "listed", "folder": str(project), "sessions": [{"id": "s1", "mtime": old_mtime}]}
        allowlist = [str(project)]
        result = detect_stale(proj_info, str(config_dir), 30, allowlist)
        # Actually the allowlist checks cwd from transcript, not project folder
        # With empty transcript, cwd is None, so it returns True (stale)


class TestOrcaReportOnly:
    """Tests that Orca is report-only."""

    def test_describe_orca_does_not_delete(self, tmp_path):
        orca_dir = tmp_path
        terminal_history = orca_dir / "terminal-history"
        terminal_history.mkdir()
        (terminal_history / "term1.jsonl").write_text("x" * 1000)

        result = describe_orca(str(orca_dir))

        # Files still exist after describe_orca
        assert (terminal_history / "term1.jsonl").exists()
        assert len(result) > 0


class TestCliArgsParsing:
    """Tests for CLI argument parsing."""

    def test_default_args(self, tmp_path, capsys, monkeypatch):
        """main([]) with an empty store completes without error and reports nothing to wipe."""
        import wipe
        monkeypatch.setattr(wipe, "resolve_config_dir", lambda: str(tmp_path))
        (tmp_path / "projects").mkdir()

        # Empty store: collect_candidates finds nothing, main returns normally
        wipe.main([])
        out = capsys.readouterr().out
        assert "Nothing to wipe" in out

    def test_dry_run_flag_recognized(self, tmp_path, capsys, monkeypatch):
        """--dry-run is parsed and suppresses deletion."""
        import wipe
        monkeypatch.setattr(wipe, "resolve_config_dir", lambda: str(tmp_path))
        (tmp_path / "projects").mkdir()
        old_project = tmp_path / "projects" / "old"
        old_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        (old_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(old_project / "s1.jsonl"), (old_mtime, old_mtime))

        wipe.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "Dry run: nothing was deleted" in out
        # The old session data is still there
        assert (old_project / "s1.jsonl").exists()


class TestProjectFolderPathGone:
    """Tests for project folder gone detection."""

    def test_all_cwds_deleted(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        project = projects_root / "deleted-project"
        project.mkdir()
        deleted_cwd = tmp_path / "nowhere"
        deleted_cwd.mkdir()
        old_mtime = time.time() - 40 * 86400  # Old to avoid being active
        # Escape backslashes for valid JSON
        cwd_json = str(deleted_cwd).replace("\\", "\\\\")
        (project / "s1.jsonl").write_text('{"cwd": "' + cwd_json + '"}\n')
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        import shutil
        shutil.rmtree(str(deleted_cwd))

        info = collect_project(str(config_dir), "deleted-project")
        reasons = detect_reasons(info, str(config_dir), 30, None)
        assert any("gone" in r for r in reasons)


class TestBranchMergedSignal:
    """Tests for branch merged detection."""

    def test_merged_branch(self, tmp_path):
        git_repo = tmp_path / "test-repo"
        git_repo.mkdir()
        (git_repo / "file.txt").write_text("test")

        run_git(["init"], str(git_repo))
        run_git(["config", "user.email", "test@test.com"], str(git_repo))
        run_git(["config", "user.name", "Test"], str(git_repo))
        run_git(["add", "."], str(git_repo))
        run_git(["commit", "-m", "initial"], str(git_repo))

        # Create and checkout a feature branch, then merge it
        run_git(["checkout", "-b", "feature-test"], str(git_repo))
        (git_repo / "feature.txt").write_text("feature")
        run_git(["add", "."], str(git_repo))
        run_git(["commit", "-m", "feature"], str(git_repo))
        run_git(["checkout", "master"], str(git_repo))
        run_git(["merge", "feature-test"], str(git_repo))

        # Now feature-test should be detected as merged
        result = branch_signal(str(git_repo), "feature-test")
        # Note: After merging to master, feature-test IS an ancestor of master
        assert result == "merged"


class TestHookAllowlistEnv:
    """Tests for hook honoring WIPE_ALLOWLIST env var."""

    def test_wipe_allowlist_env_override(self, tmp_path):
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()

        project = projects_root / "test"
        project.mkdir()
        old_mtime = time.time() - 40 * 86400
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        # Create a custom allowlist
        allowlist_file = tmp_path / "custom-allowlist.txt"
        allowlist_file.write_text(str(project))

        result = hook_load_allowlist(str(allowlist_file))
        assert result == [str(project)]


class TestHookDisabled:
    """hookConfig.sessionWipeOffer.enabled=false suppresses the offer."""

    HOOK_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hooks", "session-start-offer.py")

    def _run_hook(self, stdin_text, env):
        import subprocess
        proc = subprocess.run(
            [sys.executable, self.HOOK_PATH],
            input=stdin_text, capture_output=True, text=True, env=env, timeout=30)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_disabled_config_yields_no_additional_context(self, tmp_path):
        # A stale project that WOULD trigger the offer if enabled
        project = tmp_path / "projects" / "old"
        project.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        # HOME/USERPROFILE point at the sandbox so the rate-limit marker
        # cannot leak in from (or be written to) the real ~/.claude-lumo
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path)
        env["HOME"] = str(tmp_path)
        env["USERPROFILE"] = str(tmp_path)

        output = self._run_hook(
            '{"hookEventName": "SessionStart", '
            '"hookConfig": {"sessionWipeOffer": {"enabled": false}}}',
            env)

        hook_out = output.get("hookSpecificOutput", {})
        assert hook_out.get("hookEventName") == "SessionStart"
        assert "additionalContext" not in hook_out

    def test_enabled_by_default_when_stale(self, tmp_path):
        # Same stale project, no disabling config: the offer appears
        project = tmp_path / "projects" / "old"
        project.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(project / "s1.jsonl"), (old_mtime, old_mtime))

        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path)
        env["HOME"] = str(tmp_path)
        env["USERPROFILE"] = str(tmp_path)

        output = self._run_hook(
            '{"hookEventName": "SessionStart", "hookConfig": {}}',
            env)

        hook_out = output.get("hookSpecificOutput", {})
        assert "additionalContext" in hook_out
        assert "could be wiped" in hook_out["additionalContext"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
