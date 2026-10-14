"""Single old sessions in projects that are still in use become wipe candidates (#103)."""
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
    p = "proj"
    files = {
        "live": add_session(cfg, p, "live", cwd, 0),
        "old-own": add_session(cfg, p, "old-own", cwd, 10),
        "new-own": add_session(cfg, p, "new-own", cwd, 3),
        "old-script": add_session(cfg, p, "old-script", cwd, 2, "sdk-cli"),
        "new-script": add_session(cfg, p, "new-script", cwd, 0.5, "sdk-cli"),
    }
    return cfg, files


def ids(plan):
    return set(plan["sessions"])


def test_old_sessions_of_a_live_project_are_candidates(live_project):
    cfg, _ = live_project
    plans = wipe.collect_candidates(str(cfg), 30, None)
    assert len(plans) == 1 and plans[0]["partial"]
    assert ids(plans[0]) == {"old-own", "old-script"}
    assert plans[0]["reasons"] == ["1 automation session(s) older than 1 day(s)",
                                   "1 session(s) older than 7 day(s)"]


def test_thresholds_are_adjustable(live_project):
    cfg, _ = live_project
    plan = wipe.collect_candidates(str(cfg), 30, None, session_days=2, automation_days=3)[0]
    # old-script (2 days) misses the scripted limit but meets the general one.
    assert ids(plan) == {"old-own", "new-own", "old-script"}
    plan = wipe.collect_candidates(str(cfg), 30, None, session_days=30, automation_days=1)[0]
    assert ids(plan) == {"old-script"}


def test_active_and_young_sessions_are_never_picked(live_project):
    cfg, _ = live_project
    plan = wipe.collect_candidates(str(cfg), 30, None, session_days=1, automation_days=1)[0]
    assert "live" not in ids(plan) and "new-script" not in ids(plan)


def test_classification(live_project):
    cfg, _ = live_project
    plan = wipe.collect_candidates(str(cfg), 30, None)[0]
    assert wipe.classify_candidate(plan, str(cfg))[0] == "ASK"
    scripted_only = dict(plan, reasons=["3 automation session(s) older than 1 day(s)"])
    assert wipe.classify_candidate(scripted_only, str(cfg))[0] == "REMOVE"


def test_allowlist_mode_adds_no_single_sessions(live_project, tmp_path):
    cfg, _ = live_project
    allow = tmp_path / "allow.txt"
    allow.write_text(str(tmp_path / "work"), encoding="utf-8")
    assert wipe.collect_candidates(str(cfg), 30, str(allow)) == []


def test_named_project_old_sessions_deletes_only_those(live_project, monkeypatch, capsys):
    cfg, files = live_project
    mem = cfg / "projects" / "proj" / "memory"
    mem.mkdir()
    store = {"path": str(cfg), "realpath": os.path.realpath(str(cfg)), "origin": "flag",
             "current": True}
    monkeypatch.setattr(wipe, "discover_stores", lambda: [store])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    wipe.main(["--project", "proj", "--old-sessions", "--yes"])
    assert not files["old-own"].exists() and not files["old-script"].exists()
    for keep in ("live", "new-own", "new-script"):
        assert files[keep].exists(), keep
    assert mem.exists()


def test_old_sessions_needs_project(capsys):
    with pytest.raises(SystemExit):
        wipe.main(["--old-sessions", "--dry-run"])
    assert "--old-sessions needs --project" in capsys.readouterr().err


def test_long_plans_list_a_few_paths(live_project, capsys):
    cfg, _ = live_project
    for i in range(15):
        add_session(cfg, "proj", f"s{i}", cfg.parent / "work", 3, "sdk-cli")
    plan = dict(wipe.collect_candidates(str(cfg), 30, None)[0], store={"path": str(cfg)})
    group = {"store": {"path": str(cfg), "current": True}, "plans": [plan], "store_paths": []}
    wipe.print_report([group], [], [])
    out = capsys.readouterr().out
    assert "more path(s)" in out
    assert out.count("[transcript]") == wipe.LISTED_PATHS_PER_PLAN


def test_history_line_counts_real_entries(live_project, capsys):
    cfg, _ = live_project
    plan = dict(wipe.collect_candidates(str(cfg), 30, None)[0], store={"path": str(cfg)})
    group = {"store": {"path": str(cfg), "current": True}, "plans": [plan], "store_paths": []}
    wipe.print_report([group], [], [])
    assert "[history.jsonl]" not in capsys.readouterr().out  # no history.jsonl at all
    lines = [{"sessionId": "old-own"}, {"sessionId": "old-own"}, {"sessionId": "live"}]
    (cfg / "history.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines),
                                       encoding="utf-8")
    wipe.print_report([group], [], [])
    assert "[history.jsonl] 2 entries" in capsys.readouterr().out


def test_unattended_yes_keeps_own_old_sessions(live_project, monkeypatch, capsys):
    cfg, files = live_project
    store = {"path": str(cfg), "realpath": os.path.realpath(str(cfg)), "origin": "flag",
             "current": True}
    monkeypatch.setattr(wipe, "discover_stores", lambda: [store])
    monkeypatch.setattr(wipe, "stale_temp_scratch", lambda *a: [])
    monkeypatch.setattr(wipe, "stale_lumo_logs", lambda *a: [])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    wipe.main(["--yes"])
    assert "--project proj --old-sessions" in capsys.readouterr().out
    assert files["old-own"].exists() and files["old-script"].exists()
