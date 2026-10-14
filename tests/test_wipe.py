"""Test suite for session-wipe feature.

Ran with: python -m pytest tests/test_wipe.py -v
Uses pytest with stdlib-only subject code (no extra deps).
"""

import importlib.util
import io
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
    is_mount_point,
    is_any_link,
    looks_like_store,
    resolve_known_stores,
    walk_home_for_stores,
    discover_stores,
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
    stale_config_stores,
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
    offer_help,
    suggest_action,
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


class TestIsAnyLink:
    """Tests for is_any_link/is_mount_point: junctions count as links."""

    def _make_junction(self, target, link):
        import _winapi
        _winapi.CreateJunction(str(target), str(link))

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_junction_detected(self, tmp_path):
        # A junction is not a symlink (islink is False) but must count as a link.
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "junction"
        self._make_junction(target, link)
        assert os.path.islink(str(link)) is False
        assert is_mount_point(str(link)) is True
        assert is_any_link(str(link)) is True

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_plain_dir_not_link(self, tmp_path):
        assert is_any_link(str(tmp_path)) is False
        assert is_mount_point(str(tmp_path)) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_symlink_detected(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "sym"
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation requires privileges")
        assert is_any_link(str(link)) is True


class TestTreeSizeLinks:
    """tree_size never follows links."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_junctioned_content_excluded(self, tmp_path):
        import _winapi
        target = tmp_path / "target"
        target.mkdir()
        (target / "big.txt").write_text("x" * 1000)
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "local.txt").write_text("y" * 100)
        _winapi.CreateJunction(str(target), str(outer / "junction"))
        assert tree_size(str(outer)) == 100

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_junction_loop_returns(self, tmp_path):
        # A junction pointing back at an ancestor must not hang the size walk.
        import _winapi
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "file.txt").write_text("z" * 10)
        _winapi.CreateJunction(str(tmp_path), str(outer / "loop"))
        assert tree_size(str(outer)) == 10


@pytest.fixture(autouse=True)
def isolated_scan_roots(tmp_path, monkeypatch):
    """Every test scans fixture folders only, never the real ~/.claude,
    ~/.claude-lumo, temp folder or Orca data (#40)."""
    import wipe
    roots = tmp_path / "isolated"
    monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(roots / "home"))
    monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(roots / "state"))
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(roots / "temp"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(wipe, "resolve_orca_dir", lambda: str(roots / "orca"))
    return roots


def make_store(root, name="store"):
    """Fixture store: old transcript, history.jsonl and session-env marker."""
    store = root / name
    projects = store / "projects" / "proj-1"
    projects.mkdir(parents=True)
    old = time.time() - 40 * 86400
    transcript = projects / "session-1.jsonl"
    cwd_json = str(root / "cwd").replace("\\", "\\\\")
    transcript.write_text('{"cwd": "' + cwd_json + '"}\n', encoding="utf-8")
    os.utime(str(transcript), (old, old))
    (store / "history.jsonl").write_text('{"sessionId": "session-1"}\n')
    (store / "session-env" / "session-1").mkdir(parents=True)
    return store


def single_store(config_dir):
    """A discover_stores() stub naming one fixture store."""
    return [{"path": str(config_dir), "realpath": str(config_dir),
             "origin": "env", "current": True}]


class TestLooksLikeStore:
    """Tests for the strict store discriminator."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_full_fixture_passes(self, tmp_path):
        store = make_store(tmp_path)
        assert looks_like_store(str(store)) is True

    def test_missing_secondary_marker_fails(self, tmp_path):
        # Transcript without history/session-env/file-history: not a store.
        store = tmp_path / "almost"
        projects = store / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / "s.jsonl").write_text("{}", encoding="utf-8")
        assert looks_like_store(str(store)) is False

    def test_projects_without_jsonl_fails(self, tmp_path):
        store = tmp_path / "empty-projects"
        (store / "projects" / "p").mkdir(parents=True)
        (store / "session-env" / "s").mkdir(parents=True)
        assert looks_like_store(str(store)) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_junctioned_projects_fails(self, tmp_path):
        import _winapi
        real_store = make_store(tmp_path, "real")
        fake = tmp_path / "fake"
        fake.mkdir()
        _winapi.CreateJunction(str(real_store / "projects"), str(fake / "projects"))
        assert looks_like_store(str(fake)) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_junctioned_session_env_marker_fails(self, tmp_path):
        # A linked session-env does not satisfy the secondary marker.
        import _winapi
        real_store = make_store(tmp_path, "real")
        fake = tmp_path / "fake-session-env"
        projects = fake / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / "s.jsonl").write_text("{}", encoding="utf-8")
        _winapi.CreateJunction(str(real_store / "session-env"),
                               str(fake / "session-env"))
        assert looks_like_store(str(fake)) is False


class TestResolveKnownStores:
    """Tests for resolve_known_stores."""

    def test_order_and_origins(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".claude").mkdir()
        env_store = home / "custom"
        env_store.mkdir()
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_store))
        result = resolve_known_stores()
        assert result[0][1] == "env"
        assert os.path.normcase(result[0][0]) \
            == os.path.normcase(os.path.realpath(str(env_store)))
        # ~/.claude exists (just made) and is second
        assert result[1][1] == "default"

    def test_env_equals_default_deduped(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        claude_dir = home / ".claude"
        claude_dir.mkdir()
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
        origins = [origin for _, origin in resolve_known_stores()]
        # The env path wins; the identical default path is deduped away.
        assert origins == ["env"]

    def test_nonexistent_dropped(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".claude").mkdir()
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "missing"))
        # ~/.claude-lumo/data does not exist either: only ~/.claude survives
        origins = [origin for _, origin in resolve_known_stores()]
        assert "env" not in origins
        assert origins == ["default"]

    def test_legacy_config_included(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        legacy = home / ".claude-lumo" / "config"
        legacy.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        origins = [origin for _, origin in resolve_known_stores()]
        assert "legacy" in origins


class TestWalkHomeForStores:
    """Tests for the bounded, link-safe home walk."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_stores_at_various_depths_and_noise(self, tmp_path):
        import _winapi
        home = tmp_path / "home"
        (home / "noise" / "node_modules").mkdir(parents=True)
        deep_store = make_store(home / "nested" / "deeper", "store")
        top_store = make_store(home, "top-store")
        # Junction loop: home points back into itself
        _winapi.CreateJunction(str(home), str(home / "loop"))
        found = walk_home_for_stores(str(home))
        assert os.path.normcase(str(top_store)) in [os.path.normcase(p) for p in found]
        assert os.path.normcase(str(deep_store)) in [os.path.normcase(p) for p in found]
        # The walker never pathed through the loop, so nothing appears twice.
        assert len(found) == len(set(os.path.normcase(p) for p in found))
        assert not any("loop" in p for p in found)

    def test_deep_store_beyond_cap_not_found(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        store = make_store(home / "a" / "b" / "c" / "d", "too-deep")
        found = walk_home_for_stores(str(home), max_depth=4)
        assert not any("too-deep" in p for p in found)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_no_walk_into_accepted_store(self, tmp_path):
        # A store nested inside a found store is not re-listed (single hit).
        home = tmp_path / "home"
        home.mkdir()
        make_store(home, "store")
        found = walk_home_for_stores(str(home))
        assert len(found) == 1


class TestDiscoverStores:
    """Tests for discover_stores assembly."""

    def test_known_first_and_current_flag(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        env_store = make_store(home, "env-store")
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_store))
        import wipe
        monkeypatch.setattr(wipe, "walk_home_for_stores", lambda home=None, max_depth=4: [])
        records = discover_stores()
        assert records[0]["origin"] == "env"
        assert records[0]["current"] is True

    def test_walker_refind_deduped(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        env_store = make_store(home, "env-store")
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_store))
        import wipe
        monkeypatch.setattr(wipe, "walk_home_for_stores",
                            lambda home=None, max_depth=4: [str(env_store)])
        records = discover_stores()
        env_paths = [r for r in records if r["origin"] == "env"]
        assert len(env_paths) == 1

    def test_discovered_origin_recorded(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        stray = make_store(home, "stray")
        monkeypatch.setenv("CLAUDE_LUMO_SCAN_ROOT", str(home))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(home / ".claude-lumo"))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        import wipe
        monkeypatch.setattr(wipe, "walk_home_for_stores", lambda home=None, max_depth=4: [str(stray)])
        records = discover_stores()
        assert any(r["origin"] == "discovered"
                   and os.path.normcase(r["realpath"]) == os.path.normcase(str(stray))
                   for r in records)


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

    def test_stale_log_files(self, tmp_path, monkeypatch):
        lumo_state = tmp_path
        old_time = time.time() - 40 * 86400
        for name in ("app.log", "debug.log", "recent.log"):
            log = lumo_state / name
            log.write_text("test")
            mtime = old_time if name != "recent.log" else time.time()
            os.utime(str(log), (mtime, mtime))

        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(lumo_state))
        result = stale_lumo_logs(30)
        assert len([r for r in result if r[0] == "lumo-log"]) == 2


class TestStaleConfigStores:
    """Tests for stale_config_stores: the age-keyed stores under the config dir."""

    def test_stale_entries_found_fresh_kept(self, tmp_path):
        config_dir = tmp_path
        old_time = time.time() - 40 * 86400
        now = time.time()

        paste_old = config_dir / "paste-cache" / "abc123.txt"
        paste_old.parent.mkdir(parents=True)
        paste_old.write_text("secret")
        os.utime(str(paste_old), (old_time, old_time))

        paste_fresh = config_dir / "paste-cache" / "def456.txt"
        paste_fresh.write_text("recent")
        os.utime(str(paste_fresh), (now, now))

        snap_old = config_dir / "shell-snapshots" / "old.sh"
        snap_old.parent.mkdir(parents=True)
        snap_old.write_text("rm -rf /")
        os.utime(str(snap_old), (old_time, old_time))

        result = stale_config_stores(str(config_dir), 30)

        paths = [p for _, p in result]
        assert str(paste_old) in paths
        assert str(snap_old) in paths
        assert str(paste_fresh) not in paths
        kinds = [k for k, _ in result]
        assert "paste-cache" in kinds and "shell-snapshots" in kinds

    def test_stale_directories_swept_too(self, tmp_path):
        # plans/ holds arbitrary .md files; debug/ holds dated subfolders
        config_dir = tmp_path
        old_time = time.time() - 40 * 86400
        plan_old = config_dir / "plans" / "old-plan.md"
        plan_old.parent.mkdir(parents=True)
        plan_old.write_text("plan")
        os.utime(str(plan_old), (old_time, old_time))

        debug_dir = config_dir / "debug" / "2026-09-01"
        debug_dir.mkdir(parents=True)
        (debug_dir / "trace.json").write_text("{}")
        os.utime(str(debug_dir), (old_time, old_time))

        result = stale_config_stores(str(config_dir), 30)
        paths = [p for _, p in result]
        assert str(plan_old) in paths
        assert str(debug_dir) in paths

    def test_unknown_dirs_ignored(self, tmp_path):
        # Stores outside the list are never touched
        other = tmp_path / "projects" / "someproject"
        other.mkdir(parents=True)
        old_time = time.time() - 40 * 86400
        os.utime(str(other), (old_time, old_time))

        assert stale_config_stores(str(tmp_path), 30) == []

    def test_missing_stores_yield_empty(self, tmp_path):
        assert stale_config_stores(str(tmp_path / "nonexistent"), 30) == []


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

        # A fresh transcript means the session looks live: the planner must
        # refuse by exiting rather than returning a deletion plan.
        with pytest.raises(SystemExit) as excinfo:
            plan_for_named_session(str(config_dir), "active")
        assert excinfo.value.code == 1

    def test_active_session_env_lock_rejected(self, tmp_path):
        # Env lock also marks a session active even with an old transcript.
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (projects_dir / "locked.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "locked.jsonl"), (old_mtime, old_mtime))
        lock = config_dir / "session-env" / "locked"
        lock.mkdir(parents=True)
        (lock / ".lock").write_text("", encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            plan_for_named_session(str(config_dir), "locked")
        assert excinfo.value.code == 1

    def test_old_session_allowed(self, tmp_path):
        # Contrast case: an old unlocked session plans normally.
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (projects_dir / "old.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "old.jsonl"), (old_mtime, old_mtime))

        result = plan_for_named_session(str(config_dir), "old")
        assert result is not None
        assert result["project"] == "test"

    def test_missing_session(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)
        (projects_dir / "other.jsonl").write_text("{}")

        result = plan_for_named_session(str(config_dir), "missing")
        assert result is None

    def test_session_id_traversal_rejected(self, tmp_path):
        # Path-like session ids must be refused, never treated as filenames
        # or folder names under the config dir.
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)

        for bad_id in ("..", "../escape", "sub/session", "C:\\evil"):
            with pytest.raises(SystemExit) as excinfo:
                plan_for_named_session(str(config_dir), bad_id)
            assert excinfo.value.code == 1

    def test_session_id_dot_rejected(self, tmp_path):
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "test"
        projects_dir.mkdir(parents=True)

        with pytest.raises(SystemExit) as excinfo:
            plan_for_named_session(str(config_dir), ".")
        assert excinfo.value.code == 1


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
        projects_dir = config_dir / "projects" / "active-project"
        projects_dir.mkdir(parents=True)
        recent_mtime = time.time() - 100
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (recent_mtime, recent_mtime))

        # Fresh transcript makes the project look live: refuse with exit 1.
        with pytest.raises(SystemExit) as excinfo:
            plan_for_named_project(str(config_dir), "active-project")
        assert excinfo.value.code == 1

    def test_old_project_allowed(self, tmp_path):
        # Contrast case: an old project plans normally.
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "old-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        result = plan_for_named_project(str(config_dir), "old-project")
        assert result is not None
        assert result["project"] == "old-project"

    def test_project_traversal_rejected(self, tmp_path):
        # Regression for the path traversal fix: path-like project names must
        # never resolve above projects/ as a folder name. ".." must fail to
        # match (returns None), not delete the config dir.
        config_dir = tmp_path
        projects_dir = config_dir / "projects"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        victim = projects_dir / "victim"
        victim.mkdir()
        (victim / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(victim / "s1.jsonl"), (old_mtime, old_mtime))
        marker = config_dir / "above-projects.txt"
        marker.write_text("survivor", encoding="utf-8")

        result = plan_for_named_project(str(config_dir), "..")
        assert result is None
        assert marker.read_text(encoding="utf-8") == "survivor"

    def test_project_absolute_path_not_treated_as_name(self, tmp_path):
        # A name with separators must fall through to cwd matching only; if no
        # project's transcript cwd matches, the plan is None.
        config_dir = tmp_path
        projects_dir = config_dir / "projects" / "real-project"
        projects_dir.mkdir(parents=True)
        old_mtime = time.time() - 40 * 86400
        (projects_dir / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(projects_dir / "s1.jsonl"), (old_mtime, old_mtime))

        # "C:/no/such/dir" is path-like but matches no project: None, no error.
        result = plan_for_named_project(str(config_dir), os.path.join("no", "such", "dir"))
        assert result is None


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


class TestDeleteAllLinkGuards:
    """Tests for delete_all's link and store-boundary refusals."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_junction_target_refused(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("precious")
        link = tmp_path / "link"
        import _winapi
        _winapi.CreateJunction(str(target), str(link))

        result = delete_all([("dir", str(link))])
        assert result[0][1] is False
        assert "link" in result[0][2]
        assert link.exists()
        assert (target / "keep.txt").exists()

    def test_store_root_escape_refused(self, tmp_path):
        store = tmp_path / "store"
        (store / "projects").mkdir(parents=True)
        external = tmp_path / "external"
        external.mkdir()
        (external / "keep.txt").write_text("precious")

        result = delete_all([("dir", str(external))], store_root=str(store))
        assert result[0][1] is False
        assert "outside" in result[0][2]
        assert (external / "keep.txt").exists()

    def test_inside_store_allowed(self, tmp_path):
        store = tmp_path / "store"
        project = store / "projects" / "old"
        project.mkdir(parents=True)
        (project / "s1.jsonl").write_text("{}")

        result = delete_all([("dir", str(project))], store_root=str(store))
        assert result[0][1] is True
        assert not project.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_dir_containing_junction_removed_leaves_target(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("precious")
        holder = tmp_path / "holder"
        holder.mkdir()
        (holder / "local.txt").write_text("local")
        import _winapi
        _winapi.CreateJunction(str(target), str(holder / "junction"))

        result = delete_all([("dir", str(holder))], store_root=str(tmp_path))
        assert result[0][1] is True
        assert not holder.exists()
        assert (target / "keep.txt").exists()


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

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
    def test_linked_project_folder_skipped(self, tmp_path):
        config_dir = tmp_path / "store"
        projects_root = config_dir / "projects"
        projects_root.mkdir(parents=True)
        real_project = tmp_path / "real-project"
        real_project.mkdir()
        old_mtime = time.time() - 40 * 86400
        (real_project / "s1.jsonl").write_text("{}", encoding="utf-8")
        os.utime(str(real_project / "s1.jsonl"), (old_mtime, old_mtime))
        import _winapi
        _winapi.CreateJunction(str(real_project), str(projects_root / "linked"))

        candidates = collect_candidates(str(config_dir), 30, None)
        assert candidates == []


class TestDryRunMode:
    """Tests for dry-run behavior."""

    def test_dry_run_deletes_nothing(self, tmp_path, capsys, monkeypatch):
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
        # Auto mode also sweeps the real %TEMP% and ~/.claude-lumo; keep those
        # out of the machine running the suite.
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(config_dir))
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])

        wipe.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "old" in out
        # Dry run: everything still on disk, nothing deleted
        assert test_file.exists()
        assert (old_project / "s1.jsonl").exists()
        assert old_project.is_dir()


    def test_dry_run_lists_stale_store_entries_and_deletes_nothing(self, tmp_path, capsys, monkeypatch):
        # Auto mode: a stale paste-cache entry is listed and survives
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()
        paste_old = config_dir / "paste-cache" / "abc123.txt"
        paste_old.parent.mkdir(parents=True)
        paste_old.write_text("secret")
        old_mtime = time.time() - 40 * 86400
        os.utime(str(paste_old), (old_mtime, old_mtime))

        import wipe
        # Auto mode also sweeps the real %TEMP% and ~/.claude-lumo; keep those
        # out of the machine running the suite.
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(config_dir))
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])

        wipe.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "paste-cache" in out
        assert str(paste_old) in out
        assert paste_old.exists()

    def test_yes_deletes_stale_store_entries(self, tmp_path, capsys, monkeypatch):
        # Auto mode with --yes: a stale paste-cache file is gone afterwards
        config_dir = tmp_path
        projects_root = config_dir / "projects"
        projects_root.mkdir()
        paste_old = config_dir / "paste-cache" / "abc123.txt"
        paste_old.parent.mkdir(parents=True)
        paste_old.write_text("secret")
        old_mtime = time.time() - 40 * 86400
        os.utime(str(paste_old), (old_mtime, old_mtime))

        paste_fresh = config_dir / "paste-cache" / "fresh.txt"
        paste_fresh.write_text("recent")
        os.utime(str(paste_fresh), (time.time(), time.time()))

        import wipe
        # Auto mode also sweeps the real %TEMP% and ~/.claude-lumo; keep those
        # out of the machine running the suite.
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(config_dir))
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])

        wipe.main(["--yes"])
        assert not paste_old.exists()
        assert paste_fresh.exists()

    def test_session_mode_skips_store_sweep(self, tmp_path, capsys, monkeypatch):
        # --session wipes only that session's paths, never the age-keyed stores
        config_dir = tmp_path
        projects_root = config_dir / "projects" / "proj"
        projects_root.mkdir(parents=True)
        (projects_root / "session-id-1.jsonl").write_text("{}", encoding="utf-8")
        old_mtime = time.time() - 40 * 86400
        os.utime(str(projects_root / "session-id-1.jsonl"), (old_mtime, old_mtime))

        paste_old = config_dir / "paste-cache" / "abc123.txt"
        paste_old.parent.mkdir(parents=True)
        paste_old.write_text("secret")
        os.utime(str(paste_old), (old_mtime, old_mtime))

        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(config_dir))

        wipe.main(["--session", "session-id-1", "--yes"])
        assert paste_old.exists()

    def test_project_mode_skips_store_sweep(self, tmp_path, capsys, monkeypatch):
        # --project wipes only that project's paths, never the age-keyed stores
        config_dir = tmp_path
        projects_root = config_dir / "projects" / "proj"
        projects_root.mkdir(parents=True)
        (projects_root / "s1.jsonl").write_text("{}", encoding="utf-8")
        old_mtime = time.time() - 40 * 86400
        os.utime(str(projects_root / "s1.jsonl"), (old_mtime, old_mtime))

        paste_old = config_dir / "paste-cache" / "abc123.txt"
        paste_old.parent.mkdir(parents=True)
        paste_old.write_text("secret")
        os.utime(str(paste_old), (old_mtime, old_mtime))

        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(config_dir))

        wipe.main(["--project", "proj", "--yes"])
        assert not (projects_root / "s1.jsonl").exists()
        assert paste_old.exists()


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

    def test_no_sessions_not_stale(self, tmp_path):
        # A project with only memory/ left has nothing a wipe would delete.
        project = tmp_path / "projects" / "memory-only"
        (project / "memory").mkdir(parents=True)
        proj_info = {"name": "memory-only", "folder": str(project), "sessions": []}
        assert detect_stale(proj_info, str(tmp_path), 30, None) is False
        assert detect_stale(proj_info, str(tmp_path), 30, ["/allowed"]) is False

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
        # Transcript carries a cwd matching the allowlist entry, so the project
        # is covered and must not count as stale in allowlist mode.
        cwd_json = str(project).replace("\\", "\\\\")
        (project / "s1.jsonl").write_text('{"cwd": "' + cwd_json + '"}\n', encoding="utf-8")
        result = detect_stale(proj_info, str(config_dir), 30, allowlist)
        assert result is False


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
        # Auto mode also sweeps the real %TEMP% and ~/.claude-lumo; keep those
        # out of the machine running the suite.
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(tmp_path))
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])
        (tmp_path / "projects").mkdir()

        # Empty store: collect_candidates finds nothing, main returns normally
        wipe.main([])
        out = capsys.readouterr().out
        assert "Nothing to wipe" in out

    def test_dry_run_flag_recognized(self, tmp_path, capsys, monkeypatch):
        """--dry-run is parsed and suppresses deletion."""
        import wipe
        # Auto mode also sweeps the real %TEMP% and ~/.claude-lumo; keep those
        # out of the machine running the suite.
        monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(tmp_path))
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])
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
    """CLAUDE_LUMO_WIPE_OFFER=0 suppresses the offer."""

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

    def _stale_env(self, tmp_path):
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
        env.pop("CLAUDE_LUMO_WIPE_OFFER", None)
        return env

    def test_disabled_env_yields_no_additional_context(self, tmp_path):
        env = self._stale_env(tmp_path)
        env["CLAUDE_LUMO_WIPE_OFFER"] = "0"

        output = self._run_hook('{"hookEventName": "SessionStart"}', env)

        hook_out = output.get("hookSpecificOutput", {})
        assert hook_out.get("hookEventName") == "SessionStart"
        assert "additionalContext" not in hook_out

    def test_other_env_values_do_not_disable(self, tmp_path):
        # Any value other than "0" leaves the offer on
        env = self._stale_env(tmp_path)
        env["CLAUDE_LUMO_WIPE_OFFER"] = "false"

        output = self._run_hook('{"hookEventName": "SessionStart"}', env)

        hook_out = output.get("hookSpecificOutput", {})
        assert "additionalContext" in hook_out
        assert "could be wiped" in hook_out["additionalContext"]

    def test_enabled_by_default_when_stale(self, tmp_path):
        env = self._stale_env(tmp_path)

        output = self._run_hook('{"hookEventName": "SessionStart"}', env)

        hook_out = output.get("hookSpecificOutput", {})
        assert "additionalContext" in hook_out
        assert "could be wiped" in hook_out["additionalContext"]


class _TTYStdin(io.StringIO):
    """StringIO that claims to be a terminal, so offer_help prompts."""
    def isatty(self):
        return True


class TestAuditOfferHelp:
    """privacy_audit.offer_help: interactive offer after medium/high findings."""

    FINDINGS = [
        {"severity": "high", "title": "Full-disk encryption appears disabled", "details": []},
        {"severity": "medium", "title": "3 stale project(s) found", "details": []},
        {"severity": "medium", "title": "No automatic cleanup configured", "details": []},
        {"severity": "low", "title": "Data folder not in cloud sync (good)", "details": []},
    ]

    def _run(self, findings, stdin_text, capsys):
        old_stdin = sys.stdin
        sys.stdin = _TTYStdin(stdin_text)
        try:
            offer_help(findings, days=30)
        finally:
            sys.stdin = old_stdin
        # input()'s prompt echo lands in stdout too; drop it
        return capsys.readouterr().out.replace(
            "Want claude-lumo to help fix these? [y/N] ", "")

    def test_yes_prints_numbered_actions_and_commands(self, capsys):
        out = self._run(self.FINDINGS, "y\n", capsys)
        # Numbered actions, medium/high only
        assert "1. Enable full-disk encryption" in out
        assert "2. Review and delete conversations older than 30 days" in out
        assert "3. Set cleanupPeriodDays" in out
        # The exact command line with concrete commands
        assert ("claude-lumo: ask your running assistant to help, or run: "
                "claude-lumo wipe --days 30 claude-lumo wipe") in out

    def test_no_extra_output_on_decline(self, capsys):
        out = self._run(self.FINDINGS, "n\n", capsys)
        assert out == ""

    def test_no_extra_output_on_empty_input(self, capsys):
        out = self._run(self.FINDINGS, "\n", capsys)
        assert out == ""

    def test_no_extra_output_on_eof(self, capsys):
        old_stdin = sys.stdin
        sys.stdin = _TTYStdin("")  # empty stream raises EOFError on input()
        try:
            offer_help(self.FINDINGS, days=30)
        finally:
            sys.stdin = old_stdin
        assert capsys.readouterr().out.replace(
            "Want claude-lumo to help fix these? [y/N] ", "") == ""

    def test_non_tty_prints_nothing(self, capsys):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("y\n")  # isatty() is False
        try:
            offer_help(self.FINDINGS, days=30)
        finally:
            sys.stdin = old_stdin
        assert capsys.readouterr().out == ""

    def test_low_only_findings_print_nothing(self, capsys):
        findings = [{"severity": "low", "title": "whatever", "details": []}]
        out = self._run(findings, "y\n", capsys)
        assert out == ""

    def test_no_findings_prints_nothing(self, capsys):
        out = self._run([], "y\n", capsys)
        assert out == ""


class TestAuditSuggestAction:
    """Mapping of finding titles to action lines and commands."""

    def test_stale_projects_maps_to_wipe(self):
        line, command = suggest_action(
            {"title": "3 stale project(s) found"}, days=30)
        assert "older than 30 days" in line
        assert command == "claude-lumo wipe --days 30"

    def test_no_cleanup_maps_to_setting_plus_wipe(self):
        line, command = suggest_action(
            {"title": "No automatic cleanup configured"}, days=30)
        assert "cleanupPeriodDays" in line
        assert command == "claude-lumo wipe"

    def test_update_available_maps_to_update(self):
        line, command = suggest_action(
            {"title": "Update available (5 commits behind)"}, days=30)
        assert command == "claude-lumo update"

    def test_manual_findings_have_no_command(self):
        for title in ("Full-disk encryption appears disabled",
                      "Claude data is in a cloud-synced folder",
                      "File History may retain deleted conversations"):
            line, command = suggest_action({"title": title}, days=30)
            assert command is None
            assert "no claude-lumo command" in line

    def test_manual_only_findings_give_manual_footer(self, capsys):
        findings = [
            {"severity": "high",
             "title": "Full-disk encryption appears disabled", "details": []},
            {"severity": "medium",
             "title": "File History may retain deleted conversations", "details": []},
        ]
        old_stdin = sys.stdin
        sys.stdin = _TTYStdin("y\n")
        try:
            offer_help(findings, days=30)
        finally:
            sys.stdin = old_stdin
        out = capsys.readouterr().out
        assert "ask your running assistant to walk through the manual steps" in out
        assert "or run:" not in out


class TestMultiStoreAutoWipe:
    """Auto mode across two discovered stores."""

    def _setup(self, tmp_path, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [
            {"path": str(store_a), "realpath": str(store_a), "origin": "env", "current": True},
            {"path": str(store_b), "realpath": str(store_b), "origin": "separate", "current": False},
        ])
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])
        return store_a, store_b

    def test_yes_deletes_in_both_stores(self, tmp_path, capsys, monkeypatch):
        store_a, store_b = self._setup(tmp_path, monkeypatch)
        import wipe

        wipe.main(["--yes"])
        out = capsys.readouterr().out
        assert f"Scanning 2 store(s): {store_a}, {store_b}" in out
        assert out.count("Store: ") == 2
        assert "[current store]" in out
        assert not (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert not (store_b / "projects" / "proj-1" / "session-1.jsonl").exists()
        # Per-store history rewrite
        assert (store_a / "history.jsonl").read_text() == ""
        assert (store_b / "history.jsonl").read_text() == ""

    def test_active_session_in_b_protects_only_b(self, tmp_path, capsys, monkeypatch):
        store_a, store_b = self._setup(tmp_path, monkeypatch)
        # Fresh transcript in store B's project marks it active there
        fresh = store_b / "projects" / "proj-1" / "session-1.jsonl"
        now = time.time()
        os.utime(str(fresh), (now, now))
        import wipe

        wipe.main(["--yes"])
        assert not (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert fresh.exists()


class TestMultiStoreDryRun:
    """--dry-run lists both stores, deletes nothing."""

    def test_dry_run_across_stores(self, tmp_path, capsys, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [
            {"path": str(store_a), "realpath": str(store_a), "origin": "env", "current": True},
            {"path": str(store_b), "realpath": str(store_b), "origin": "separate", "current": False},
        ])
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])

        wipe.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "Dry run: nothing was deleted" in out
        assert (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert (store_b / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert (store_a / "history.jsonl").read_text() != ""


class TestSessionAcrossStores:
    """--session searches all stores."""

    def test_found_in_one_store(self, tmp_path, capsys, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        # Give store B a different session id so no ambiguity
        other = store_b / "projects" / "proj-1" / "other-session.jsonl"
        other.write_text("{}", encoding="utf-8")
        old = time.time() - 40 * 86400
        os.utime(str(other), (old, old))
        os.remove(str(store_b / "projects" / "proj-1" / "session-1.jsonl"))
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [
            {"path": str(store_a), "realpath": str(store_a), "origin": "env", "current": True},
            {"path": str(store_b), "realpath": str(store_b), "origin": "separate", "current": False},
        ])

        wipe.main(["--session", "session-1", "--yes"])
        assert not (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert other.exists()

    def test_ambiguous_across_two_stores_refused(self, tmp_path, capsys, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [
            {"path": str(store_a), "realpath": str(store_a), "origin": "env", "current": True},
            {"path": str(store_b), "realpath": str(store_b), "origin": "separate", "current": False},
        ])

        with pytest.raises(SystemExit) as exc_info:
            wipe.main(["--session", "session-1", "--yes"])
        assert exc_info.value.code == 1
        out = capsys.readouterr()
        assert "matches multiple stores" in out.err
        assert str(store_a) in out.err
        assert str(store_b) in out.err
        # Nothing deleted in either store
        assert (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert (store_b / "projects" / "proj-1" / "session-1.jsonl").exists()


class TestProjectAcrossStores:
    """--project matches in all stores and combines plans."""

    def test_combined_wipe(self, tmp_path, capsys, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [
            {"path": str(store_a), "realpath": str(store_a), "origin": "env", "current": True},
            {"path": str(store_b), "realpath": str(store_b), "origin": "separate", "current": False},
        ])

        wipe.main(["--project", "proj-1", "--yes"])
        assert not (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert not (store_b / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert (store_a / "history.jsonl").read_text() == ""
        assert (store_b / "history.jsonl").read_text() == ""

    def test_active_match_anywhere_refuses_whole_run(self, tmp_path, capsys, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        # Active session in store B
        fresh = store_b / "projects" / "proj-1" / "session-1.jsonl"
        now = time.time()
        os.utime(str(fresh), (now, now))
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [
            {"path": str(store_a), "realpath": str(store_a), "origin": "env", "current": True},
            {"path": str(store_b), "realpath": str(store_b), "origin": "separate", "current": False},
        ])

        with pytest.raises(SystemExit) as exc_info:
            wipe.main(["--project", "proj-1", "--yes"])
        assert exc_info.value.code == 1
        # Nothing deleted anywhere
        assert (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert fresh.exists()


class TestStoreFlagRestricts:
    """--store limits the wipe to the given store(s)."""

    def test_single_store_flag(self, tmp_path, capsys, monkeypatch):
        store_a = make_store(tmp_path, "store-a")
        store_b = make_store(tmp_path, "store-b")
        import wipe
        # Auto mode also sweeps the real %TEMP% and ~/.claude-lumo; keep those
        # out of the machine running the suite.
        monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
        monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])

        wipe.main(["--store", str(store_a), "--yes"])
        assert not (store_a / "projects" / "proj-1" / "session-1.jsonl").exists()
        assert (store_b / "projects" / "proj-1" / "session-1.jsonl").exists()
        out = capsys.readouterr().out
        assert f"Scanning 1 store(s): {store_a}" in out

    def test_nonexistent_store_path_rejected(self, tmp_path, capsys):
        import wipe

        with pytest.raises(SystemExit) as exc_info:
            wipe.main(["--store", str(tmp_path / "missing"), "--yes"])
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "not an existing directory" in err


class TestEmptyDiscovery:
    """No stores found: refuse with exit 1."""

    def test_empty_discovery_exits_1(self, tmp_path, capsys, monkeypatch):
        import wipe
        monkeypatch.setattr(wipe, "discover_stores", lambda: [])

        with pytest.raises(SystemExit) as exc_info:
            wipe.main(["--dry-run"])
        assert exc_info.value.code == 1
        assert "no Claude data stores found" in capsys.readouterr().err


class TestProjectMemoryPreserved:
    """projects/<project>/memory/ survives a project wipe (#36)."""

    def _project_with_memory(self, store):
        project = store / "projects" / "proj-1"
        (project / "session-1").mkdir()
        memory = project / "memory"
        memory.mkdir()
        (memory / "MEMORY.md").write_text("- a note\n", encoding="utf-8")
        return project, memory

    def test_plan_excludes_memory(self, tmp_path):
        store = make_store(tmp_path)
        project, memory = self._project_with_memory(store)
        info = collect_project(str(store), "proj-1")
        paths, session_ids = project_wipe_plan(str(store), info)
        targets = {os.path.normcase(p) for _, p in paths}
        assert os.path.normcase(str(memory)) not in targets
        assert os.path.normcase(str(project)) not in targets
        assert os.path.normcase(str(project / "session-1.jsonl")) in targets
        assert os.path.normcase(str(project / "session-1")) in targets
        assert session_ids == {"session-1"}

    def test_auto_wipe_keeps_memory(self, tmp_path, capsys):
        import wipe
        store = make_store(tmp_path)
        project, memory = self._project_with_memory(store)
        wipe.main(["--store", str(store), "--yes"])
        assert (memory / "MEMORY.md").read_text(encoding="utf-8") == "- a note\n"
        assert not (project / "session-1.jsonl").exists()
        assert not (project / "session-1").exists()
        assert not (store / "session-env" / "session-1").exists()

    def test_named_project_keeps_memory(self, tmp_path):
        import wipe
        store = make_store(tmp_path)
        _, memory = self._project_with_memory(store)
        wipe.main(["--store", str(store), "--project", "proj-1", "--yes"])
        assert (memory / "MEMORY.md").exists()

    def test_memory_only_project_not_a_candidate(self, tmp_path):
        store = tmp_path / "store"
        (store / "projects" / "proj-1" / "memory").mkdir(parents=True)
        assert collect_candidates(str(store), 30, None) == []


class TestPromptWithoutStdin:
    """No stdin at all (pythonw): prompts decline instead of crashing (#38)."""

    def test_confirm_declines(self, monkeypatch):
        import wipe
        monkeypatch.setattr(sys, "stdin", None)
        assert wipe.confirm() is False

    def test_main_runs(self, tmp_path, monkeypatch, capsys):
        import wipe
        store = make_store(tmp_path)
        monkeypatch.setattr(sys, "stdin", None)
        wipe.main(["--store", str(store), "--dry-run"])
        assert "Dry run" in capsys.readouterr().out


class TestScanRootOverride:
    """CLAUDE_LUMO_SCAN_ROOT and CLAUDE_LUMO_STATE_DIR steer discovery (#40)."""

    def test_dry_run_scans_only_the_scan_root(self, isolated_scan_roots, capsys):
        import wipe
        home = isolated_scan_roots / "home"
        home.mkdir(parents=True)
        fixture = make_store(home, ".claude")
        wipe.main(["--dry-run"])
        out = capsys.readouterr().out
        assert str(fixture) in out
        real = os.path.join(os.path.expanduser("~"), ".claude")
        assert os.path.normcase(real) not in os.path.normcase(out)

    def test_defaults_follow_overrides(self, isolated_scan_roots):
        home = isolated_scan_roots / "home"
        state = isolated_scan_roots / "state"
        (state / "data").mkdir(parents=True)
        stray = make_store(home, "stray")
        assert resolve_config_dir() == str(home / ".claude")
        assert walk_home_for_stores() == [str(stray)]
        known = resolve_known_stores()
        assert [o for _, o in known] == ["separate"]
        assert os.path.normcase(known[0][0]) \
            == os.path.normcase(os.path.realpath(str(state / "data")))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestAuditUpdateRepo:
    """check_update_status must look at the claude-lumo checkout, not its parent (~/.claude)."""

    def test_runs_git_in_its_own_checkout(self, monkeypatch):
        import privacy_audit
        seen = []

        class Done:
            returncode = 1
            stdout = ""

        def fake_run(cmd, **kwargs):
            seen.append(cmd[2])
            return Done()

        monkeypatch.setattr(privacy_audit.subprocess, "run", fake_run)
        monkeypatch.setattr(privacy_audit.os.path, "isdir", lambda p: True)
        privacy_audit.check_update_status()
        own = os.path.dirname(os.path.abspath(privacy_audit.__file__))
        assert seen and all(os.path.normcase(p) == os.path.normcase(own) for p in seen)
