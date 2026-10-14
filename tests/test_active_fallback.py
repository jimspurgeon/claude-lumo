"""Test active session fallback for project wipe (#114, #115)."""
import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wipe  # noqa: E402

DAY = 86400


def add_session(config_dir, project, sid, cwd, age_days, entrypoint="cli"):
    folder = config_dir / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text(json.dumps({"cwd": str(cwd), "entrypoint": entrypoint}) + "\n", encoding="utf-8")
    t = time.time() - age_days * DAY
    os.utime(str(path), (t, t))
    return path


@pytest.fixture
def live_project(tmp_path):
    """A project whose folder exists and which has a live session, so it is not finished."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg = tmp_path / "cfg"
    files = {
        "live": add_session(cfg, "proj", "live", cwd, 0),
        "old-own": add_session(cfg, "proj", "old-own", cwd, 10),
        "new-own": add_session(cfg, "proj", "new-own", cwd, 3),
        "old-script": add_session(cfg, "proj", "old-script", cwd, 2, "sdk-cli"),
        "new-script": add_session(cfg, "proj", "new-script", cwd, 0.5, "sdk-cli"),
    }
    return cfg, files


def test_project_with_active_session_falls_back_to_old_sessions(live_project):
    """When a project has an active session, fall back to old sessions plan."""
    cfg, _ = live_project
    plan = wipe.plan_for_named_project(str(cfg), "proj", False, 7, 1)
    assert plan is not None
    assert plan["partial"] is True
    assert set(plan["sessions"]) == {"old-own", "old-script"}
    assert any("active session present" in r for r in plan["reasons"])
    assert "selected by project" in plan["reasons"]


def test_project_fallback_refuses_when_no_old_sessions(tmp_path):
    """When there are no old sessions, still refuse with active session present."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg = tmp_path / "cfg"
    # Create only one recent session
    add_session(cfg, "proj", "live", cwd, 0)
    with pytest.raises(SystemExit):
        wipe.plan_for_named_project(str(cfg), "proj")


def test_project_env_lock_also_falls_back(tmp_path):
    """Active session via env lock also triggers fallback."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg = tmp_path / "cfg"
    # Create an active session via lock WITH a transcript in the project
    add_session(cfg, "proj", "locked-live", cwd, 40)  # Old transcript
    lock_dir = cfg / "session-env" / "locked-live"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / ".lock").touch()
    # Old session sibling
    add_session(cfg, "proj", "old-sibling", cwd, 10)
    plan = wipe.plan_for_named_project(str(cfg), "proj", False, 7, 1)
    assert plan is not None
    assert plan["partial"] is True
    assert set(plan["sessions"]) == {"old-sibling"}


def test_explicit_thresholds_restrict_plain_project_wipe(live_project):
    """Explicit thresholds restrict a plain --project wipe."""
    cfg, _ = live_project
    # new-own is age 3 >= 2, so it qualifies; live/new-script excluded
    plan = wipe.plan_for_named_project(
        str(cfg), "proj", False, session_days=2, automation_days=1, restrict_to_thresholds=True)
    assert set(plan["sessions"]) == {"old-own", "new-own", "old-script"}


def test_default_thresholds_keep_whole_project_behavior(tmp_path):
    """Default thresholds keep whole-project behavior when all sessions are old."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg = tmp_path / "cfg"
    # Make every session old (40 days), no locks
    files = {
        "s1": add_session(cfg, "proj", "s1", cwd, 40),
        "s2": add_session(cfg, "proj", "s2", cwd, 40),
        "s3": add_session(cfg, "proj", "s3", cwd, 40),
        "s4": add_session(cfg, "proj", "s4", cwd, 40),
        "s5": add_session(cfg, "proj", "s5", cwd, 40),
    }
    plan = wipe.plan_for_named_project(str(cfg), "proj", False)
    assert plan is not None
    assert not plan.get("partial")
    assert set(plan["sessions"]) == {"s1", "s2", "s3", "s4", "s5"}


def test_session_skip_active_flag(tmp_path, capsys):
    """--skip-active flag skips active session instead of refusing."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    cfg = tmp_path / "cfg"
    # Create one recent session (age 0, no lock)
    sess_path = add_session(cfg, "proj", "recent-sess", cwd, 0)
    sess_id = "recent-sess"
    # Without flag: refuses
    with pytest.raises(SystemExit):
        wipe.plan_for_named_session(str(cfg), sess_id)
    # With flag: returns None, prints note
    result = wipe.plan_for_named_session(str(cfg), sess_id, skip_active=True)
    assert result is None
    err = capsys.readouterr().err
    assert f"skipped (--skip-active)" in err


def test_agent_prompt_delete_commands_carry_threshold_flags():
    """Agent prompt includes --session-days and --skip-active flags."""
    assert "--session-days <D>" in wipe.AGENT_PROMPT
    assert "--skip-active" in wipe.AGENT_PROMPT


def test_main_plain_project_with_explicit_thresholds_restricts(
        live_project, monkeypatch, capsys):
    """End to end: explicit thresholds restrict plain project wipe."""
    cfg, files = live_project
    store = {"path": str(cfg), "realpath": os.path.realpath(str(cfg)),
             "origin": "flag", "current": True}
    monkeypatch.setattr(wipe, "discover_stores", lambda: [store])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    wipe.main(["--project", "proj", "--session-days", "2", "--yes"])
    # old-own, new-own, old-script should be gone
    assert not files["old-own"].exists()
    assert not files["new-own"].exists()
    assert not files["old-script"].exists()
    # live and new-script should still exist
    assert files["live"].exists()
    assert files["new-script"].exists()


def test_skip_active_needs_session(capsys):
    """--skip-active without --session is rejected."""
    with pytest.raises(SystemExit):
        wipe.main(["--skip-active", "--dry-run"])
    err = capsys.readouterr().err
    assert "--skip-active needs --session" in err
