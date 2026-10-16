"""Tests for scripts/watch_ci.py (P2 T5).

Ran with: python -m pytest tests/test_watch_ci.py -v
Pure logic only: throttle windows, run-id dedup, config parsing, report rendering,
and the command-runner seams (gh/orca/claude are faked, never invoked).
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import watch_ci


def write_toml(text):
    fd, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def make_state(runs=(), attempts=()):
    return {"runs": list(runs), "attempts": list(attempts)}


class TestLoadConfig:
    def test_parses_repos_branch_and_path(self):
        path = write_toml(
            '[[repos]]\nrepo = "owner/name"\nbranch = "main"\npath = "C:/x"\n'
            '[[repos]]\nrepo = "a/b"\n'
        )
        try:
            config = watch_ci.load_config(path)
        finally:
            os.unlink(path)
        assert config == [
            {"repo": "owner/name", "branch": "main", "path": "C:/x"},
            {"repo": "a/b", "branch": None, "path": None},
        ]

    def test_rejects_missing_slug(self):
        path = write_toml('[[repos]]\nbranch = "main"\n')
        try:
            with pytest.raises(ValueError):
                watch_ci.load_config(path)
        finally:
            os.unlink(path)

    def test_rejects_empty(self):
        path = write_toml("# nothing\n")
        try:
            with pytest.raises(ValueError):
                watch_ci.load_config(path)
        finally:
            os.unlink(path)

    def test_rejects_bad_toml(self):
        path = write_toml("[[repos\n")
        try:
            with pytest.raises(Exception):
                watch_ci.load_config(path)
        finally:
            os.unlink(path)


class TestNightBounds:
    def test_evening_starts_the_window(self):
        day = datetime(2026, 10, 15, 19, 30)
        start, end = watch_ci.night_bounds(day)
        assert start == datetime(2026, 10, 15, 18, 0)
        assert end == datetime(2026, 10, 16, 6, 0)

    def test_exactly_six_pm_is_night(self):
        day = datetime(2026, 10, 15, 18, 0)
        start, end = watch_ci.night_bounds(day)
        assert start == datetime(2026, 10, 15, 18, 0)
        assert end == datetime(2026, 10, 16, 6, 0)

    def test_after_midnight_belongs_to_previous_evening(self):
        day = datetime(2026, 10, 16, 2, 0)
        start, end = watch_ci.night_bounds(day)
        assert start == datetime(2026, 10, 15, 18, 0)
        assert end == datetime(2026, 10, 16, 6, 0)

    def test_daytime_has_no_window(self):
        assert watch_ci.night_bounds(datetime(2026, 10, 15, 14, 0)) is None
        assert watch_ci.is_night(datetime(2026, 10, 15, 14, 0)) is False

    def test_exactly_six_am_ends_the_night(self):
        assert watch_ci.is_night(datetime(2026, 10, 16, 6, 0)) is False

    def test_is_night(self):
        assert watch_ci.is_night(datetime(2026, 10, 15, 22, 0)) is True
        assert watch_ci.is_night(datetime(2026, 10, 16, 3, 0)) is True
        assert watch_ci.is_night(datetime(2026, 10, 15, 9, 0)) is False


class TestThrottle:
    def test_counts_attempts_inside_window_only(self):
        window = (datetime(2026, 10, 15, 18, 0), datetime(2026, 10, 16, 6, 0))
        attempts = [
            datetime(2026, 10, 15, 19, 0).isoformat(),
            datetime(2026, 10, 15, 23, 30).isoformat(),
            datetime(2026, 10, 14, 19, 0).isoformat(),  # previous night
            datetime(2026, 10, 15, 12, 0).isoformat(),  # daytime
            "not-a-date",
        ]
        assert watch_ci.attempts_in_window(attempts, window) == 2
        assert watch_ci.attempts_in_window([], window) == 0
        assert watch_ci.attempts_in_window(attempts, None) == 0

    def test_counts_attempts_on_calendar_day(self):
        attempts = [
            datetime(2026, 10, 15, 1, 0).isoformat(),
            datetime(2026, 10, 15, 23, 0).isoformat(),
            datetime(2026, 10, 14, 23, 0).isoformat(),
        ]
        assert watch_ci.attempts_that_day(attempts, datetime(2026, 10, 15, 12, 0)) == 2

    def test_night_limit_blocks_fourth_attempt(self):
        state = make_state(
            attempts=[
                datetime(2026, 10, 15, 19, 0).isoformat(),
                datetime(2026, 10, 15, 20, 0).isoformat(),
                datetime(2026, 10, 15, 21, 0).isoformat(),
            ]
        )
        now = datetime(2026, 10, 15, 22, 0)
        assert watch_ci.throttle_allows(state, now) is False
        assert "night" in watch_ci.throttle_reason(state, now)

    def test_day_limit_blocks_even_outside_night(self):
        state = make_state(
            attempts=[
                datetime(2026, 10, 15, 8, 0).isoformat(),
                datetime(2026, 10, 15, 10, 0).isoformat(),
                datetime(2026, 10, 15, 12, 0).isoformat(),
            ]
        )
        now = datetime(2026, 10, 15, 14, 0)
        assert watch_ci.throttle_allows(state, now) is False
        assert "daily" in watch_ci.throttle_reason(state, now)

    def test_day_counts_span_midnight(self):
        # Three attempts late in the day; the third hit the night limit already,
        # but a next-day daytime attempt must also be blocked by the daily count.
        state = make_state(
            attempts=[
                datetime(2026, 10, 15, 12, 0).isoformat(),
                datetime(2026, 10, 15, 13, 0).isoformat(),
            ]
        )
        now = datetime(2026, 10, 15, 20, 0)
        assert watch_ci.throttle_allows(state, now) is True
        watch_ci.record_attempt(state, now)
        assert watch_ci.throttle_allows(state, datetime(2026, 10, 15, 23, 0)) is False

    def test_fresh_day_allows_again(self):
        state = make_state(
            attempts=[datetime(2026, 10, 15, 19, 0).isoformat()] * 3
        )
        assert watch_ci.throttle_allows(state, datetime(2026, 10, 16, 9, 0)) is True
        assert watch_ci.throttle_allows(state, datetime(2026, 10, 16, 20, 0)) is True

    def test_record_attempt_appends_iso(self):
        state = make_state()
        watch_ci.record_attempt(state, datetime(2026, 10, 15, 19, 0))
        assert state["attempts"] == [datetime(2026, 10, 15, 19, 0).isoformat()]


class TestRunDedup:
    def test_run_scoped_to_repo(self):
        state = make_state(runs=["a/b#1"])
        assert watch_ci.run_is_new(state, "a/b", 1) is False
        assert watch_ci.run_is_new(state, "c/d", 1) is True
        assert watch_ci.run_is_new(state, "a/b", 2) is True

    def test_mark_then_seen(self):
        state = make_state()
        watch_ci.mark_run_handled(state, "a/b", 42)
        assert "a/b#42" in state["runs"]
        assert watch_ci.run_is_new(state, "a/b", 42) is False

    def test_mark_is_idempotent(self):
        state = make_state()
        watch_ci.mark_run_handled(state, "a/b", 42)
        watch_ci.mark_run_handled(state, "a/b", 42)
        assert state["runs"] == ["a/b#42"]


class TestStateFile:
    def test_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = {"runs": ["a/b#1"], "attempts": ["2026-10-15T19:00:00"]}
        watch_ci.save_state(path, state)
        assert watch_ci.load_state(path) == state

    def test_missing_file_is_fresh(self, tmp_path):
        assert watch_ci.load_state(str(tmp_path / "nope.json")) == {
            "runs": [],
            "attempts": [],
        }

    def test_corrupt_file_is_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        path = str(tmp_path / "state.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        assert watch_ci.load_state(path)["runs"] == []
        # A reset must be named in the journal, never silent.
        today = datetime.now().strftime("%Y-%m-%d")
        log = tmp_path / "journal" / ("ci-watcher-%s.log" % today)
        assert "resetting handled run history" in log.read_text(encoding="utf-8")

    def test_non_dict_state_resets_logged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        path = str(tmp_path / "state.json")
        with open(path, "w") as fh:
            fh.write("[1, 2]")
        assert watch_ci.load_state(path) == {"runs": [], "attempts": []}
        today = datetime.now().strftime("%Y-%m-%d")
        log = tmp_path / "journal" / ("ci-watcher-%s.log" % today)
        assert "resetting handled run history" in log.read_text(encoding="utf-8")

    def test_runs_list_bounded(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = make_state(runs=["a/b#%d" % i for i in range(watch_ci.STATE_MAX_RUNS + 50)])
        watch_ci.save_state(path, state)
        loaded = watch_ci.load_state(path)
        assert len(loaded["runs"]) == watch_ci.STATE_MAX_RUNS
        assert loaded["runs"][-1] == "a/b#%d" % (watch_ci.STATE_MAX_RUNS + 49)


class FakeRunner:
    """Command-runner seam: records argv, returns canned results."""

    def __init__(self, results=None):
        # results: list of (match-prefix, returncode, stdout) or callable(argv)
        self.calls = []
        self.results = results or []

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append((argv, cwd))
        for item in self.results:
            if callable(item):
                result = item(argv, cwd)
                if result is not None:
                    return result
            else:
                prefix, code, out = item
                if " ".join(argv).startswith(prefix):
                    return code, out
        return 0, ""


class TestListFailedRuns:
    def test_parses_and_filters_failures(self):
        payload = json.dumps(
            [
                {"databaseId": 1, "conclusion": "success", "headBranch": "main", "displayTitle": "ok"},
                {"databaseId": 2, "conclusion": "failure", "headBranch": "main", "displayTitle": "bad"},
                {"databaseId": 3, "conclusion": None, "headBranch": "main", "displayTitle": "running"},
            ]
        )
        runner = FakeRunner([("gh run list", 0, payload)])
        conf = {"repo": "a/b", "branch": None, "path": None}
        assert watch_ci.list_failed_runs(conf, runner=runner) == [("2", "main", "bad")]
        assert runner.calls[0][0][:6] == ["gh", "run", "list", "--repo", "a/b", "--json"]

    def test_gh_failure_raises(self):
        runner = FakeRunner([("gh run list", 1, "auth error")])
        conf = {"repo": "a/b", "branch": None, "path": None}
        with pytest.raises(RuntimeError):
            watch_ci.list_failed_runs(conf, runner=runner)


class TestBuildPrompt:
    def test_contains_contract_pieces(self):
        conf = {"repo": "a/b", "branch": None, "path": None}
        prompt = watch_ci.build_prompt(conf, "12345")
        assert "--log-failed" in prompt and "gh run view 12345" in prompt
        assert "parked-needs-human" in prompt
        assert "merge danger" in prompt.lower()
        assert "never block" in prompt.lower()


class TestPollCycle:
    def _config(self, tmp_path):
        path = write_toml(
            '[[repos]]\nrepo = "a/b"\npath = "%s"\n' % str(tmp_path).replace("\\", "/")
        )
        return path

    def _gh_payload(self, run_ids=("1",)):
        return json.dumps(
            [
                {
                    "databaseId": int(rid),
                    "conclusion": "failure",
                    "headBranch": "main",
                    "displayTitle": "title-" + rid,
                }
                for rid in run_ids
            ]
        )

    def test_dry_run_marks_handled_without_fix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WATCH_CI_JOURNAL_DIR", str(tmp_path / "journal"))
        monkeypatch.setenv("CLAUDE_LUMO_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        monkeypatch.setattr(watch_ci, "state_path", lambda: str(tmp_path / "state" / "s.json"))
        payload = self._gh_payload()
        runner = FakeRunner([("gh run list", 0, payload)])
        state = watch_ci.load_state(str(tmp_path / "state" / "s.json"))
        config = watch_ci.load_config(self._config(tmp_path))
        watch_ci.poll_cycle(config, state, dry_run=True, runner=runner, now=datetime(2026, 10, 15, 20, 0))
        # Second cycle: the run is already handled, so gh is polled once more
        # but no detection or fix action is taken.
        watch_ci.poll_cycle(config, state, dry_run=True, runner=runner, now=datetime(2026, 10, 15, 20, 5))
        assert len(runner.calls) == 2  # just the two gh polls
        log = (tmp_path / "journal" / "ci-watcher-2026-10-15.log").read_text(encoding="utf-8")
        assert log.count("DETECTED") == 1
        assert "dry run" in log
        assert not any(c[0][0] == "orca" for c in runner.calls)

    def test_throttle_skips_new_failed_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        state_path = str(tmp_path / "state" / "s.json")
        monkeypatch.setattr(watch_ci, "state_path", lambda: state_path)
        payload = self._gh_payload(("7",))
        runner = FakeRunner([("gh run list", 0, payload)])
        state = make_state(
            attempts=[
                datetime(2026, 10, 15, 19, 0).isoformat(),
                datetime(2026, 10, 15, 19, 30).isoformat(),
                datetime(2026, 10, 15, 20, 0).isoformat(),
            ]
        )
        config = watch_ci.load_config(self._config(tmp_path))
        now = datetime(2026, 10, 15, 21, 0)
        watch_ci.poll_cycle(config, state, dry_run=False, runner=runner, now=now)
        log = (tmp_path / "journal" / "ci-watcher-2026-10-15.log").read_text(encoding="utf-8")
        assert "THROTTLED" in log
        assert not any(c[0][0] == "orca" for c in runner.calls)
        # The run stays handled: no retry next cycle.
        assert not watch_ci.run_is_new(state, "a/b", 7)

    def test_branch_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        payload = json.dumps(
            [
                {"databaseId": 9, "conclusion": "failure", "headBranch": "dev", "displayTitle": "x"},
                {"databaseId": 10, "conclusion": "failure", "headBranch": "main", "displayTitle": "y"},
            ]
        )
        runner = FakeRunner([("gh run list", 0, payload)])
        monkeypatch.setattr(watch_ci, "state_path", lambda: str(tmp_path / "s.json"))
        state = make_state()
        config = [{"repo": "a/b", "branch": "main", "path": str(tmp_path)}]
        watch_ci.poll_cycle(config, state, dry_run=True, runner=runner, now=datetime(2026, 10, 15, 20, 0))
        assert watch_ci.run_is_new(state, "a/b", 9)  # other branch: not handled
        assert not watch_ci.run_is_new(state, "a/b", 10)

    def test_fix_attempt_runs_cleanup_afterwards(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        monkeypatch.setattr(watch_ci, "state_path", lambda: str(tmp_path / "s.json"))
        monkeypatch.setattr(shutil, "which", lambda name: None)
        payload = self._gh_payload(("5",))
        # gh returns the failed run; git worktree add/remove succeed (FakeRunner's
        # default (0, "")); the claude-lumo call is caught the same way.
        runner = FakeRunner([("gh run list", 0, payload)])
        config = [{"repo": "a/b", "branch": None, "path": "C:/wt"}]
        monkeypatch.setattr(watch_ci, "tempfile_dir", lambda: "T:/tmp")
        monkeypatch.setattr(watch_ci, "resolve_claude_cmd", lambda: "claude-lumo")
        state = make_state()
        watch_ci.poll_cycle(config, state, dry_run=False, runner=runner, now=datetime(2026, 10, 15, 20, 0))
        argvs = [" ".join(c[0]) for c in runner.calls]
        assert any("worktree add" in a for a in argvs)
        assert any("worktree remove" in a for a in argvs)  # cleanup fired
        assert any(a.startswith("claude-lumo -p") for a in argvs)


class TestJournalAndReport:
    def test_log_event_writes_dated_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path))
        watch_ci.log_event("poll", "a/b: 1 failed run(s)", now=datetime(2026, 10, 15, 12, 0, 0))
        path = tmp_path / "ci-watcher-2026-10-15.log"
        text = path.read_text(encoding="utf-8")
        assert text == "2026-10-15 12:00:00 POLL a/b: 1 failed run(s)\n"

    def test_parse_journal_line(self):
        parsed = watch_ci.parse_journal_line("2026-10-15 12:00:00 ATTEMPT a/b run 1 in /tmp")
        assert parsed == (
            datetime(2026, 10, 15, 12, 0, 0),
            "ATTEMPT",
            "a/b run 1 in /tmp",
        )
        assert watch_ci.parse_journal_line("garbage line") is None

    def test_read_journal_lines_spans_days(self, tmp_path):
        for day in ("2026-10-14", "2026-10-15"):
            (tmp_path / ("ci-watcher-%s.log" % day)).write_text(
                "2026-10-%s 10:00:00 POLL x\n" % day[8:], encoding="utf-8"
            )
        lines = watch_ci.read_journal_lines(str(tmp_path), datetime(2026, 10, 15, 9, 0))
        assert len(lines) == 2

    def test_render_report_counts_and_filters(self):
        now = datetime(2026, 10, 15, 8, 0)
        recent = "2026-10-15 07:59:00 DETECTED a/b run 1 (main): boom"
        old = "2026-10-13 09:00:00 DETECTED a/b run 0 (main): ancient"
        body = watch_ci.render_report(
            [recent, old, "malformed"], "2026-10-15", now=now
        )
        assert "# CI watcher report 2026-10-15" in body
        assert "| Failed runs detected | 1 |" in body
        assert "| Poll cycles | 0 |" in body
        assert "run 1" in body
        assert "ancient" not in body

    def test_render_report_empty(self):
        body = watch_ci.render_report([], "2026-10-15", now=datetime(2026, 10, 15, 8, 0))
        assert "(no failed runs, fix attempts, throttle events or errors)" in body


class TestNotifyDesktop:
    def test_silent_when_burnttoast_missing(self):
        runner = FakeRunner([("powershell", 1, "")])
        assert watch_ci.notify_desktop("t", "m", runner=runner) is False

    def test_silent_on_exception(self):
        def boom(argv, cwd=None, timeout=None):
            raise OSError("no powershell")

        assert watch_ci.notify_desktop("t", "m", runner=boom) is False

    def test_sends_when_available(self):
        runner = FakeRunner(
            [
                ("powershell -NoProfile -Command Get-Module", 0, "BurntToast 0.8.5"),
                ("powershell -NoProfile -Command New-BurntToastNotification", 0, ""),
            ]
        )
        assert watch_ci.notify_desktop("t", "m", runner=runner) is True


class TestCreateWorktree:
    def test_orca_used_when_available(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/orca" if name == "orca" else None)
        payload = json.dumps({"result": {"worktree": {"path": "C:/wt/ci-fix-1234"}}})
        runner = FakeRunner([("orca worktree create", 0, payload)])
        got = watch_ci.create_worktree(
            {"repo": "a/b", "branch": None, "path": "C:/clone"}, "1234567890", runner=runner
        )
        assert got == "C:/wt/ci-fix-1234"
        argv = runner.calls[0][0]
        assert argv[:4] == ["orca", "worktree", "create", "--name"]

    def test_git_fallback_when_orca_absent(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(watch_ci, "tempfile_dir", lambda: "T:/tmp")
        runner = FakeRunner([("git -C", 0, "")])
        got = watch_ci.create_worktree(
            {"repo": "a/b", "branch": None, "path": "C:/clone"}, "1234567890", runner=runner
        )
        assert got.startswith("T:/tmp")
        argv = runner.calls[0][0]
        assert argv[:3] == ["git", "-C", "C:/clone"]

    def test_returns_none_without_local_path(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert (
            watch_ci.create_worktree(
                {"repo": "a/b", "branch": None, "path": None}, "1", runner=FakeRunner()
            )
            is None
        )


class TestResolveClaudeCmd:
    def test_which_finds_cmd_shim_uses_resolved_path(self):
        # Injected which, no real filesystem: a .cmd shim resolves to its path.
        fake_which = lambda name: "C:/npm/claude-lumo.CMD" if name == "claude-lumo" else None
        assert watch_ci.resolve_claude_cmd(which=fake_which) == "C:/npm/claude-lumo.CMD"

    def test_which_finds_posix_path_uses_it(self):
        fake_which = lambda name: "/usr/local/bin/claude-lumo" if name == "claude-lumo" else None
        assert watch_ci.resolve_claude_cmd(which=fake_which) == "/usr/local/bin/claude-lumo"

    def test_which_returns_none_falls_back_to_bare_name(self):
        assert watch_ci.resolve_claude_cmd(which=lambda name: None) == "claude-lumo"


class TestPsQuote:
    def test_plain_text_unchanged(self):
        assert watch_ci.ps_quote("hello world") == "hello world"

    def test_single_quotes_doubled(self):
        assert watch_ci.ps_quote("it's a'test") == "it''s a''test"

    def test_empty(self):
        assert watch_ci.ps_quote("") == ""

    def test_notify_escapes_quotes_in_command(self):
        runner = FakeRunner(
            [
                ("powershell -NoProfile -Command Get-Module", 0, "BurntToast 0.8.5"),
                ("powershell -NoProfile -Command New-BurntToastNotification", 0, ""),
            ]
        )
        watch_ci.notify_desktop("it's ready", "run 1 done", runner=runner)
        sent = [argv for argv, _ in runner.calls if "New-BurntToastNotification" in " ".join(argv)]
        assert sent and "-Text 'it''s ready', 'run 1 done'" in sent[0][3]


class TestCleanupWorktree:
    def test_orca_rm_used_when_orca_available(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        monkeypatch.setattr(shutil, "which", lambda name: "C:/orca" if name == "orca" else None)
        runner = FakeRunner([("orca worktree rm", 0, "{}")])
        watch_ci.cleanup_worktree(
            "C:/wt/ci-fix-12345678",
            {"repo": "a/b", "branch": None, "path": "C:/clone"},
            runner=runner,
        )
        argv = runner.calls[0][0]
        assert argv == ["orca", "worktree", "rm", "--worktree", "path:C:/wt/ci-fix-12345678"]

    def test_git_fallback_when_orca_rm_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        monkeypatch.setattr(shutil, "which", lambda name: "C:/orca" if name == "orca" else None)
        runner = FakeRunner(
            [
                ("orca worktree rm", 1, "boom"),
                ("git -C", 0, ""),
            ]
        )
        watch_ci.cleanup_worktree(
            "C:/wt/ci-fix-12345678",
            {"repo": "a/b", "branch": None, "path": "C:/clone"},
            runner=runner,
        )
        argv = runner.calls[1][0]
        assert argv[:5] == ["git", "-C", "C:/clone", "worktree", "remove"]

    def test_git_used_directly_when_no_orca(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        monkeypatch.setattr(shutil, "which", lambda name: None)
        runner = FakeRunner([("git -C", 0, "")])
        watch_ci.cleanup_worktree(
            "C:/wt/ci-fix-12345678",
            {"repo": "a/b", "branch": None, "path": "C:/clone"},
            runner=runner,
        )
        argv = runner.calls[0][0]
        assert argv[:5] == ["git", "-C", "C:/clone", "worktree", "remove"]

    def test_no_local_path_logs_and_skips(self, monkeypatch, tmp_path):
        monkeypatch.setattr(watch_ci, "journal_dir", lambda: str(tmp_path / "journal"))
        monkeypatch.setattr(shutil, "which", lambda name: None)
        runner = FakeRunner()
        watch_ci.cleanup_worktree(
            "C:/wt/ci-fix-12345678",
            {"repo": "a/b", "branch": None, "path": None},
            runner=runner,
        )
        assert runner.calls == []
        today = datetime.now().strftime("%Y-%m-%d")
        log = tmp_path / "journal" / ("ci-watcher-%s.log" % today)
        assert "left in place" in log.read_text(encoding="utf-8")

    def test_none_worktree_is_noop(self):
        watch_ci.cleanup_worktree(None, {"repo": "a/b"}, runner=FakeRunner())
