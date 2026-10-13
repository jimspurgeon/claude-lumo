"""Tests for the three-mode wipe/audit interaction (issue #52).

Modes:
  - intelligent: classify candidates, execute REMOVE automatically, prompt for ASK
  - auto: batch delete everything (existing --yes behavior)
  - manual: print commands/steps, delete nothing

Non-interactive (piped) runs default to auto with no new prompts.
"""

import io
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wipe
import privacy_audit


class _TTYStdin(io.StringIO):
    """StringIO that claims to be a TTY, so interactive prompts fire."""
    def isatty(self):
        return True


def single_store(config_dir):
    return [{"path": str(config_dir), "realpath": str(config_dir),
             "origin": "env", "current": True}]


def isolate(monkeypatch, config_dir, tmp_path):
    """Keep the wipe away from the real machine's stores/temp/logs."""
    monkeypatch.setattr(wipe, "discover_stores", lambda: single_store(config_dir))
    monkeypatch.setattr(wipe, "resolve_temp_dir", lambda: str(tmp_path / "absent-temp"))
    monkeypatch.setattr(wipe, "stale_lumo_logs", lambda days: [])


def make_allowlist(tmp_path):
    """Allowlist file covering nothing: every project becomes ASK."""
    path = tmp_path / "allowlist.txt"
    path.write_text(str(tmp_path / "somewhere-else"), encoding="utf-8")
    return str(path)


def make_merged_branch_project(config_dir, name="merged-proj"):
    """Inactive project whose branch is merged into master: ASK.

    Mirrors TestBranchMergedSignal: a real git repo where feature-test
    was merged into master, so branch_signal reports "merged" and
    detect_reasons yields the borderline branch signal.
    """
    repo = config_dir / (name + "-repo")
    repo.mkdir()
    (repo / "file.txt").write_text("test", encoding="utf-8")
    wipe.run_git(["init"], str(repo))
    wipe.run_git(["config", "user.email", "test@test.com"], str(repo))
    wipe.run_git(["config", "user.name", "Test"], str(repo))
    wipe.run_git(["add", "."], str(repo))
    wipe.run_git(["commit", "-m", "initial"], str(repo))
    wipe.run_git(["checkout", "-b", "feature-test"], str(repo))
    (repo / "feature.txt").write_text("feature", encoding="utf-8")
    wipe.run_git(["add", "."], str(repo))
    wipe.run_git(["commit", "-m", "feature"], str(repo))
    wipe.run_git(["checkout", "master"], str(repo))
    wipe.run_git(["merge", "feature-test"], str(repo))

    project = config_dir / "projects" / name
    project.mkdir(parents=True)
    transcript = project / "sess.jsonl"
    cwd_json = str(repo).replace("\\", "\\\\")
    transcript.write_text(
        '{"cwd": "' + cwd_json + '"}\n{"gitBranch": "feature-test"}\n',
        encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(str(transcript), (old, old))
    return project


def make_marked_done_project(config_dir, name="done-proj"):
    """Project explicitly marked done: clearly-safe REMOVE."""
    project = config_dir / "projects" / name
    project.mkdir(parents=True)
    (project / "wipe-done").write_text("", encoding="utf-8")
    transcript = project / "sess.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(str(transcript), (old, old))
    return project


def make_inactive_project(config_dir, name="inactive-proj", branch="feature-x"):
    """Inactive project with a live branch: ASK (unpushed work may remain)."""
    project = config_dir / "projects" / name
    project.mkdir(parents=True)
    transcript = project / "sess.jsonl"
    cwd_json = str(project).replace("\\", "\\\\")
    transcript.write_text(
        '{"cwd": "' + cwd_json + '"}\n{"gitBranch": "' + branch + '"}\n',
        encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(str(transcript), (old, old))
    return project


class TestClassifyCandidate:
    """classify_candidate maps wipe reasons to REMOVE / ASK / KEEP."""

    def test_marked_done_is_remove(self):
        cls, reason = wipe.classify_candidate({"reasons": ["marked done"]}, "/any")
        assert cls == "REMOVE"
        assert "complete" in reason

    def test_selected_by_session_is_remove(self):
        cls, _ = wipe.classify_candidate(
            {"reasons": ["selected by session id"]}, "/any")
        assert cls == "REMOVE"

    def test_selected_by_project_is_remove(self):
        cls, _ = wipe.classify_candidate(
            {"reasons": ["selected by project"]}, "/any")
        assert cls == "REMOVE"

    def test_project_folder_gone_is_remove(self):
        cls, _ = wipe.classify_candidate(
            {"reasons": ["project folder gone"]}, "/any")
        assert cls == "REMOVE"

    def test_empty_project_folder_is_remove(self):
        cls, _ = wipe.classify_candidate(
            {"reasons": ["empty project folder"]}, "/any")
        assert cls == "REMOVE"

    def test_single_inactive_is_remove(self):
        cls, _ = wipe.classify_candidate(
            {"reasons": ["inactive for 40 days"]}, "/any")
        assert cls == "REMOVE"

    def test_branch_merged_or_removed_is_ask(self):
        cls, reason = wipe.classify_candidate(
            {"reasons": ["branch merged or removed"]}, "/any")
        assert cls == "ASK"
        assert "verify" in reason or "unpushed" in reason

    def test_outside_allowlist_is_ask(self):
        cls, _ = wipe.classify_candidate({"reasons": ["outside allowlist"]}, "/any")
        assert cls == "ASK"

    def test_multiple_reasons_are_ask(self):
        cls, reason = wipe.classify_candidate(
            {"reasons": ["inactive for 40 days", "branch merged or removed"]}, "/any")
        assert cls == "ASK"
        assert "multiple" in reason or "branch" in reason

    def test_unknown_empty_reasons_are_keep(self):
        cls, reason = wipe.classify_candidate({"reasons": []}, "/any")
        assert cls == "KEEP"

    def test_unrecognized_reason_is_keep(self):
        cls, _ = wipe.classify_candidate({"reasons": ["something new"]}, "/any")
        assert cls == "KEEP"


class TestModeParsing:
    """--mode accepts the three documented values."""

    def test_mode_rejects_unknown_value(self, capsys):
        with pytest.raises(SystemExit):
            wipe.main(["--mode", "turbo"])
        err = capsys.readouterr().err
        assert "invalid choice" in err

    def test_manual_lists_commands_and_deletes_nothing(
            self, tmp_path, capsys, monkeypatch):
        config_dir = tmp_path
        project = make_inactive_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)

        wipe.main(["--mode", "manual"])

        out = capsys.readouterr().out
        assert "inactive-proj" in out
        assert "claude-lumo wipe --project inactive-proj" in out
        assert project.is_dir()


class TestManualMode:
    """Manual mode: plain-language report with commands, nothing executed."""

    def test_manual_deletes_nothing(self, tmp_path, capsys, monkeypatch):
        config_dir = tmp_path
        done = make_marked_done_project(config_dir)
        inactive = make_inactive_project(config_dir)
        transcript = done / "sess.jsonl"
        isolate(monkeypatch, config_dir, tmp_path)

        wipe.main(["--mode", "manual"])

        out = capsys.readouterr().out
        assert done.is_dir()
        assert inactive.is_dir()
        assert transcript.exists()
        # Exact commands for each finding
        assert "claude-lumo wipe --project done-proj" in out
        assert "claude-lumo wipe --project inactive-proj" in out
        assert "--mode auto" in out

    def test_manual_prompts_for_nothing(self, tmp_path, capsys, monkeypatch):
        """Piped stdin + manual: no prompt blocks, no deletion."""
        config_dir = tmp_path
        project = make_inactive_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO())  # non-TTY

        wipe.main(["--mode", "manual"])

        out = capsys.readouterr().out
        assert project.is_dir()
        assert "[y/N]" not in out  # no interactive prompt block


class TestIntelligentMode:
    """Intelligent mode: REMOVE executed, ASK deferred to user, KEEP untouched."""

    def test_intelligent_removes_clearly_safe_without_confirm(
            self, tmp_path, capsys, monkeypatch):
        """Marked-done project is deleted with no global y/N confirm."""
        config_dir = tmp_path
        done = make_marked_done_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        # No tty -> ASK prompting skipped, but REMOVE must still execute.
        monkeypatch.setattr(sys, "stdin", io.StringIO())

        wipe.main(["--mode", "intelligent"])

        out = capsys.readouterr().out
        assert not done.exists()
        assert "confirm" not in out.lower()
        assert "Type 'yes'" not in out

    def test_intelligent_defers_ask_when_not_tty(self, tmp_path, capsys, monkeypatch):
        """Borderline item survives a non-interactive intelligent run."""
        config_dir = tmp_path
        inactive = make_inactive_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO())

        # --allowlist makes the project "outside allowlist": a genuine ASK.
        wipe.main(["--mode", "intelligent", "--allowlist", make_allowlist(tmp_path)])

        out = capsys.readouterr().out
        assert inactive.exists()
        assert "ASK" in out

    def test_intelligent_prompts_per_ask_item_on_tty(
            self, tmp_path, capsys, monkeypatch):
        """On a TTY, borderline items get an individual y/N prompt."""
        config_dir = tmp_path
        inactive = make_inactive_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", _TTYStdin("n\n"))

        wipe.main(["--mode", "intelligent", "--allowlist", make_allowlist(tmp_path)])

        out = capsys.readouterr().out
        assert inactive.exists()
        assert "Delete inactive-proj?" in out
        assert "KEPT" in out

    def test_intelligent_prompt_yes_deletes_ask_item(
            self, tmp_path, capsys, monkeypatch):
        config_dir = tmp_path
        inactive = make_inactive_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", _TTYStdin("y\n"))

        wipe.main(["--mode", "intelligent", "--allowlist", make_allowlist(tmp_path)])

        out = capsys.readouterr().out
        assert not inactive.exists()
        assert "DELETED" in out

    def test_intelligent_declined_ask_keeps_history(
            self, tmp_path, capsys, monkeypatch):
        """An ASK item answered 'n' keeps its history.jsonl lines too."""
        config_dir = tmp_path
        merged = make_merged_branch_project(config_dir)
        history = config_dir / "history.jsonl"
        history.write_text('{"sessionId": "sess-1"}\n', encoding="utf-8")
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", _TTYStdin("n\n"))

        wipe.main(["--mode", "intelligent"])

        assert merged.exists()
        assert '"sess-1"' in history.read_text(encoding="utf-8")

    def test_intelligent_mix_removes_and_defers(
            self, tmp_path, capsys, monkeypatch):
        """REMOVE deleted, ASK kept on 'n', both reported.

        done-proj is marked done (REMOVE); merged-proj's branch is merged
        into master (ASK: unpushed work may remain).
        """
        config_dir = tmp_path
        done = make_marked_done_project(config_dir)
        merged = make_merged_branch_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", _TTYStdin("n\n"))

        wipe.main(["--mode", "intelligent"])

        out = capsys.readouterr().out
        assert not done.exists()
        assert merged.exists()
        assert "DELETED" in out
        assert "KEPT: merged-proj" in out


class TestModeDefaults:
    """Mode resolution: --yes and pipes imply auto; TTY implies intelligent."""

    def test_yes_overrides_tty_to_auto(self, tmp_path, capsys, monkeypatch):
        """--yes on a TTY runs the auto path: everything deleted, no prompts."""
        config_dir = tmp_path
        done = make_marked_done_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", _TTYStdin(""))

        wipe.main(["--yes"])

        out = capsys.readouterr().out
        assert not done.exists()
        assert "Classification:" not in out

    def test_piped_stdin_defaults_to_auto_no_prompts(
            self, tmp_path, capsys, monkeypatch):
        """Without --mode/--yes and piped stdin, the run must not prompt.

        The piped default is auto mode (no per-item intelligent prompts);
        the run explains how to proceed and deletes nothing, never blocks.
        """
        config_dir = tmp_path
        done = make_marked_done_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO())

        wipe.main([])

        out = capsys.readouterr().out
        assert done.exists()
        assert "Classification:" not in out
        assert "Non-interactive run" in out
        assert "Type 'yes'" not in out

    def test_piped_stdin_intelligent_mode_no_prompts(
            self, tmp_path, capsys, monkeypatch):
        """Explicit --mode intelligent with piped stdin: no blocking prompts."""
        config_dir = tmp_path
        inactive = make_inactive_project(config_dir)
        isolate(monkeypatch, config_dir, tmp_path)
        monkeypatch.setattr(sys, "stdin", io.StringIO())

        wipe.main(["--mode", "intelligent", "--allowlist", make_allowlist(tmp_path)])

        out = capsys.readouterr().out
        assert inactive.exists()


class TestPrivacyAuditModes:
    """privacy-audit --mode: findings plus recommended next step."""

    def test_audit_manual_prints_commands(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        projects = tmp_path / "projects" / "old"
        projects.mkdir(parents=True)
        (projects / "s.jsonl").write_text("{}", encoding="utf-8")
        old = time.time() - 40 * 86400
        os.utime(str(projects / "s.jsonl"), (old, old))

        privacy_audit.main(["--mode", "manual"])

        out = capsys.readouterr().out
        assert "Privacy Audit" in out
        assert "claude-lumo wipe" in out

    def test_audit_auto_stays_current_behavior(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        projects = tmp_path / "projects" / "old"
        projects.mkdir(parents=True)
        (projects / "s.jsonl").write_text("{}", encoding="utf-8")
        old = time.time() - 40 * 86400
        os.utime(str(projects / "s.jsonl"), (old, old))
        monkeypatch.setattr(sys, "stdin", io.StringIO())

        privacy_audit.main([])

        out = capsys.readouterr().out
        assert "Privacy Audit" in out
        assert "Want claude-lumo to help fix these?" not in out

    def test_audit_piped_stdin_prompts_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        projects = tmp_path / "projects" / "old"
        projects.mkdir(parents=True)
        (projects / "s.jsonl").write_text("{}", encoding="utf-8")
        old = time.time() - 40 * 86400
        os.utime(str(projects / "s.jsonl"), (old, old))
        monkeypatch.setattr(sys, "stdin", io.StringIO())

        privacy_audit.main([])

        out = capsys.readouterr().out
        assert "Want claude-lumo to help fix these?" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
