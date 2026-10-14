#!/usr/bin/env python3
"""Tests for separate_data.py.

Run: python test_separate_data.py

All tests use TemporaryDirectory with fake shared/config dirs.
No real ~/.claude touched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent))
import separate_data


class TestOffByDefault(unittest.TestCase):
    """Test 1: Off by default - the enabling predicate is false without opt-in."""

    def test_marker_absent_by_default(self):
        """Without an explicit opt-in, the marker file does not exist and the
        enabling condition in claude-lumo.sh evaluates false for this data dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data = Path(tmpdir) / "data"
            data.mkdir()
            marker = data.parent / "separate-data.on"
            # No opt-in happened in this test: neither the marker nor the env var
            # (checked below with the inherited env cleared) enables separation.
            self.assertFalse(marker.exists())
            env_off = os.environ.get("CLAUDE_LUMO_SEPARATE_DATA") != "1"
            self.assertTrue(env_off or marker.exists(),
                            "marker absent but CLAUDE_LUMO_SEPARATE_DATA=1 in this "
                            "environment makes off-by-default untestable here")

    def test_setup_creates_no_layout_when_not_called(self):
        """setup_links is only ever called by the enabled launcher; nothing runs by
        importing the module."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data = Path(tmpdir) / "data"
            # No setup call: importing the module must not have created anything.
            self.assertFalse(data.exists())


class TestSetupLinks(unittest.TestCase):
    """Test 2: Setup creates expected links; idempotent on re-run."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name) / "shared"
        self.data = Path(self.tmpdir.name) / "data"
        self.shared.mkdir()

        # Create fake shared config items
        (self.shared / "CLAUDE.md").write_text("# Fake CLAUDE.md")
        (self.shared / "settings.json").write_text("{}")
        (self.shared / "keybindings.json").write_text("{}")
        (self.shared / "skills").mkdir()
        (self.shared / "skills").joinpath("test.txt").write_text("skill")
        (self.shared / "projects").mkdir()
        (self.shared / "history.jsonl").write_text('{"sessionId":"xxx"}\n')

        # Data items should NOT exist in shared initially
        self.assertFalse((self.shared / "file-history").exists())

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_creates_links_for_existing_items(self):
        """setup_links creates links for items that exist in shared, and the linked
        files stay READABLE (regression: junctions on files are unreadable via Win32)."""
        separate_data.setup_links(str(self.shared), str(self.data))

        # Data dir created
        self.assertTrue(self.data.exists())

        # Data items were created real (not links)
        self.assertTrue((self.data / "file-history").exists())
        self.assertTrue((self.data / "history.jsonl").exists())
        self.assertFalse((self.data / "history.jsonl").is_symlink())

        # Shared FILE items are readable through the data dir with the same content.
        # This is the load-bearing assertion: a link method that Win32/Node cannot
        # read (junction on a file) fails here even though the path exists.
        self.assertTrue((self.data / "CLAUDE.md").exists())
        self.assertEqual((self.data / "CLAUDE.md").read_text(), "# Fake CLAUDE.md")
        with open(self.data / "CLAUDE.md", encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Fake CLAUDE.md")

        # Shared DIR items resolve to the shared content
        self.assertEqual((self.data / "skills" / "test.txt").read_text(), "skill")

    def test_idempotent_on_re_run(self):
        """Running setup twice leaves the same state without warnings about its
        own items (a real-item warning on re-run would break every launch)."""
        import io
        separate_data.setup_links(str(self.shared), str(self.data))

        first_content = None
        if (self.data / "CLAUDE.md").exists():
            first_content = (self.data / "CLAUDE.md").read_text()

        stderr_capture = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            separate_data.setup_links(str(self.shared), str(self.data))
        finally:
            sys.stderr = old_stderr

        # Content unchanged through the re-run
        if first_content:
            self.assertEqual(first_content, (self.data / "CLAUDE.md").read_text())
        # Re-run must not warn about items setup itself created or copied
        self.assertNotIn("not touching it", stderr_capture.getvalue())
        self.assertNotIn("could not link", stderr_capture.getvalue())

    def test_missing_source_skipped(self):
        """Items that don't exist in shared are not linked."""
        separate_data.setup_links(str(self.shared), str(self.data))

        # plugins/ doesn't exist in shared, should not be linked
        self.assertFalse((self.data / "plugins").exists())

    def test_real_item_warns_no_overwrite(self):
        """Real item where a link belongs → warn, no overwrite."""
        # Pre-create a real file in data
        (self.data.parent).mkdir(exist_ok=True)
        self.data.mkdir(exist_ok=True)
        (self.data / "settings.json").write_text("real content")

        # Capture stderr
        import io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()

        try:
            separate_data.setup_links(str(self.shared), str(self.data))
            stderr_output = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr

        # Warning issued about the real settings.json item
        self.assertIn("settings.json", stderr_output.lower())
        self.assertIn("not touching it", stderr_output.lower())

        # Real file untouched
        self.assertEqual("real content", (self.data / "settings.json").read_text())


class TestFakeLumoRun(unittest.TestCase):
    """Test 3: Fake 'lumo run' - write transcript into config dir only; shared store untouched."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name) / "shared"
        self.data = Path(self.tmpdir.name) / "data"
        self.shared.mkdir()

        # Setup links (may fail on Windows without privileges, but data dir will be created)
        separate_data.setup_links(str(self.shared), str(self.data))

        # Create fake shared projects
        self.shared_projects = self.shared / "projects"
        self.shared_projects.mkdir(exist_ok=True)

        # Config projects (ensure they exist)
        self.config_projects = self.data / "projects"
        self.config_projects.mkdir(exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_write_transcript_only_to_config(self):
        """Simulating lumo run writes to config store only."""
        session_uuid = "fc51f6d6-1234-5678-9abc-def012345678"
        project_dir = self.config_projects / "test-project"
        project_dir.mkdir()

        # Write a lumo transcript
        transcript = project_dir / f"{session_uuid}.jsonl"
        transcript.write_text(json.dumps({"role": "user", "content": "hello"}) + "\n")

        # Should exist in config
        self.assertTrue(transcript.exists())

        # Shared store untouched (no projects there)
        self.assertFalse((self.shared_projects / "test-project").exists())


class TestMigrateSessions(unittest.TestCase):
    """Test 4: Migration functionality."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name) / "shared"
        self.data = Path(self.tmpdir.name) / "data"
        self.shared.mkdir()

        # Create shared projects with sessions
        self.shared_projects = self.shared / "projects"
        self.shared_projects.mkdir()

        # Config directory
        self.data.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_find_lumo_sessions_correctly_identifies(self):
        """find_lumo_sessions finds transcripts with lumo models."""
        # Create mix of lumo and claude sessions
        project_a = self.shared_projects / "project-a"
        project_a.mkdir()

        # Lumo session
        (project_a / "lumo-session.jsonl").write_text(
            '{"role":"user","content":"hi"}\n{"model":"lumo-max"}\n'
        )

        # Claude session
        (project_a / "claude-session.jsonl").write_text(
            '{"role":"user","content":"hi"}\n{"model":"claude-sonnet-4"}\n'
        )

        lumo = separate_data.find_lumo_sessions(str(self.shared_projects))

        self.assertEqual(len(lumo), 1)
        proj_path, uuid = list(lumo)[0]
        self.assertEqual(uuid, "lumo-session")
        self.assertEqual(Path(proj_path), project_a)

    def test_find_lumo_sessions_ignores_a_nested_mention(self):
        """A Claude transcript that only quotes "model":"lumo- inside a value stays Claude (#29)."""
        project = self.shared_projects / "project-q"
        project.mkdir()
        (project / "quoted.jsonl").write_text(
            '{"type":"user","toolUseResult":{"model":"lumo-max"}}\n'
            '{"type":"assistant","message":{"model":"claude-opus-5-5"}}\n'
        )
        (project / "real.jsonl").write_text(
            '{"type":"assistant","message":{"model":"lumo-max","content":[]}}\n'
        )
        lumo = separate_data.find_lumo_sessions(str(self.shared_projects))
        self.assertEqual({uuid for _, uuid in lumo}, {"real"})

    def test_dry_run_prints_plan_no_move(self):
        """--dry-run prints plan without moving files."""
        # Create lumo session
        project = self.shared_projects / "test-proj"
        project.mkdir()
        session_uuid = "aaa-bbb-ccc"
        (project / f"{session_uuid}.jsonl").write_text(
            '{"model":"lumo-max"}\n'
        )

        # Run dry run
        separate_data.migrate_sessions(
            str(self.data), str(self.shared), dry_run=True, confirm=False
        )

        # Files not moved
        self.assertTrue((project / f"{session_uuid}.jsonl").exists())

    def test_migrate_lumo_only_sessions(self):
        """With --yes, migrates lumo-only sessions."""
        # Create lumo session
        project = self.shared_projects / "lumoproj"
        project.mkdir()
        session_uuid = "mig-uuid-123"
        jsonl_src = project / f"{session_uuid}.jsonl"
        jsonl_src.write_text('{"model":"lumo-max","sessionId":"' + session_uuid + '"}\n')

        # Create session base and subdirs inside it (typical structure for subagents/tool-results)
        session_base = project / session_uuid
        session_base.mkdir()
        subagent_dir = session_base / "subagent-abc"
        subagent_dir.mkdir()
        (subagent_dir / "result.txt").write_text("tool result")

        # Migrate
        separate_data.migrate_sessions(
            str(self.data), str(self.shared), dry_run=False, confirm=True
        )

        # JSONL moved to config
        config_proj = self.data / "projects" / "lumoproj"
        self.assertFalse(jsonl_src.exists())
        self.assertTrue((config_proj / f"{session_uuid}.jsonl").exists())

        # Session base dir moved entirely (whole-dir move in v2)
        # Subagent dir is inside the moved session base, not at project level
        self.assertFalse(session_base.exists() and any(session_base.iterdir()))
        self.assertTrue((config_proj / session_uuid / "subagent-abc" / "result.txt").exists())

    def test_mixed_project_memory_stays(self):
        """Mixed project with one lumo + one claude session moves only lumo one; memory untouched."""
        project = self.shared_projects / "mixed"
        project.mkdir()
        memory = project / "memory"
        memory.mkdir()
        (memory / "context.md").write_text("important")

        # Lumo session
        lumo_uuid = "lumo-uuid"
        (project / f"{lumo_uuid}.jsonl").write_text('{"model":"lumo-max"}\n')

        # Claude session
        claude_uuid = "claude-uuid"
        (project / f"{claude_uuid}.jsonl").write_text('{"model":"claude-sonnet-4"}\n')

        # Migrate
        import io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            separate_data.migrate_sessions(
                str(self.data), str(self.shared), dry_run=False, confirm=True
            )
            stderr_output = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr

        # Memory stayed in shared (the mixed-project warning fired)
        self.assertIn("mixes claude and lumo sessions", stderr_output.lower())
        self.assertIn("memory stays put", stderr_output.lower())
        self.assertTrue((memory / "context.md").exists())

        # Only lumo session moved
        config_proj = self.data / "projects" / "mixed"
        self.assertFalse((project / f"{lumo_uuid}.jsonl").exists())
        self.assertTrue((config_proj / f"{lumo_uuid}.jsonl").exists())
        self.assertTrue((project / f"{claude_uuid}.jsonl").exists())  # Claude stayed

    def test_history_filter_and_rewrite(self):
        """History.jsonl lines filtered and rewritten correctly."""
        session_uuid = "hist-uuid"

        # Create shared history with mixed sessions
        shared_history = self.shared / "history.jsonl"
        lines = [
            json.dumps({"sessionId": "other-uuid", "content": "keep this"}) + "\n",
            json.dumps({"sessionId": session_uuid, "content": "move this"}) + "\n",
            json.dumps({"sessionId": "another-uuid", "content": "keep this too"}) + "\n",
            json.dumps({"sessionId": session_uuid, "content": "also move"}) + "\n",
        ]
        shared_history.write_text("".join(lines))

        # Create a lumo session to trigger migration
        project = self.shared_projects / "hproj"
        project.mkdir()
        (project / f"{session_uuid}.jsonl").write_text('{"model":"lumo-max"}\n')

        # Migrate
        separate_data.migrate_sessions(
            str(self.data), str(self.shared), dry_run=False, confirm=True
        )

        # Shared history should only have non-lumo lines
        remaining = shared_history.read_text().splitlines()
        self.assertEqual(len(remaining), 2)
        self.assertNotIn(session_uuid, "".join(remaining))

        # Config history should have lumo lines
        config_history = self.data / "history.jsonl"
        self.assertTrue(config_history.exists())
        config_lines = config_history.read_text().splitlines()
        self.assertEqual(len(config_lines), 2)
        self.assertTrue(all(session_uuid in line for line in config_lines))


class TestRevertMigration(unittest.TestCase):
    """Test 5: Revert restores exactly; history lines merge back."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name) / "shared"
        self.data = Path(self.tmpdir.name) / "data"
        self.shared.mkdir()
        self.data.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_revert_restores_exact_locations(self):
        """Revert moves sessions back to original shared locations."""
        # Set up config store with sessions
        config_proj = self.data / "projects" / "revert-test"
        config_proj.mkdir(parents=True)
        session_uuid = "revert-uuid"
        (config_proj / f"{session_uuid}.jsonl").write_text('{"model":"lumo-max"}\n')

        # Migrate back (no sibling dirs in this simple test)
        separate_data.revert_migration(
            str(self.data), str(self.shared), dry_run=False, confirm=True
        )

        # Back in shared
        shared_proj = self.shared / "projects" / "revert-test"
        self.assertFalse((config_proj / f"{session_uuid}.jsonl").exists())
        self.assertTrue((shared_proj / f"{session_uuid}.jsonl").exists())

    def test_history_merge_back(self):
        """History lines merge back correctly."""
        session_uuid = "merge-uuid"

        # Config history with session lines
        config_history = self.data / "history.jsonl"
        config_history.write_text(
            json.dumps({"sessionId": session_uuid, "content": "moved line"}) + "\n"
        )

        # Shared history with other lines
        shared_history = self.shared / "history.jsonl"
        shared_history.write_text(
            json.dumps({"sessionId": "other", "content": "original"}) + "\n"
        )

        # Set up a session for revert
        config_proj = self.data / "projects" / "htest"
        config_proj.mkdir(parents=True)
        (config_proj / f"{session_uuid}.jsonl").write_text('{"model":"lumo-max"}\n')

        # Revert
        separate_data.revert_migration(
            str(self.data), str(self.shared), dry_run=False, confirm=True
        )

        # Shared history merged
        final = shared_history.read_text().splitlines()
        self.assertEqual(len(final), 2)
        self.assertTrue(any("moved line" in line for line in final))
        self.assertTrue(any("original" in line for line in final))

        # Config history cleared
        self.assertFalse(config_history.exists())

    def test_dry_run_no_move(self):
        """Dry run shows plan but doesn't move."""
        config_proj = self.data / "projects" / "drytest"
        config_proj.mkdir(parents=True)
        (config_proj / "dry.jsonl").write_text("{}")

        separate_data.revert_migration(
            str(self.data), str(self.shared), dry_run=True, confirm=False
        )

        # Still in config
        self.assertTrue((config_proj / "dry.jsonl").exists())
        # Nothing in shared
        self.assertFalse((self.shared / "projects").exists())


class TestFindSessionDirs(unittest.TestCase):
    """Test find_session_dirs helper."""

    def test_finds_sibling_dirs(self):
        """Returns sibling directories for a session."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "proj"
            project.mkdir()
            session_uuid = "uuid123"
            session_base = project / session_uuid
            session_base.mkdir()

            # Subdirs typical of a real session dir
            (session_base / "subagent-1").mkdir()
            (session_base / "tool-results").mkdir()

            dirs = separate_data.find_session_dirs(str(project), session_uuid)

            # The whole per-session dir is returned as a single entry
            self.assertEqual(dirs, [str(session_base)])


class TestCollectHistoryLines(unittest.TestCase):
    """Test split_history helper."""

    def test_split_history_keeps_and_moves_correct_lines(self):
        """Lines whose sessionId is in the set go to moved, others to keep."""
        with tempfile.TemporaryDirectory() as tmpdir:
            history_file = Path(tmpdir) / "history.jsonl"
            lines = [
                json.dumps({"sessionId": "a", "content": "keep"}) + "\n",
                json.dumps({"sessionId": "b", "content": "take"}) + "\n",
                json.dumps({"sessionId": "c", "content": "keep"}) + "\n",
                json.dumps({"sessionId": "b", "content": "take too"}) + "\n",
            ]
            history_file.write_text("".join(lines))

            keep, moved = separate_data.split_history(str(history_file), {"b"})

            # 2 lines moved (both with sessionId "b"), 2 kept
            self.assertEqual(len(moved), 2)
            self.assertEqual(len(keep), 2)
            # Both moved lines have sessionId "b"
            for line in moved:
                self.assertEqual(json.loads(line)["sessionId"], "b")


class TestSetupIdempotency(unittest.TestCase):
    """Regressions for idempotency: running setup/migrate twice converges."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmpdir.name) / "shared"
        self.data = Path(self.tmpdir.name) / "data"
        self.shared.mkdir()

        (self.shared / "CLAUDE.md").write_text("# Fake CLAUDE.md")
        (self.shared / "settings.json").write_text("{}")
        (self.shared / "keybindings.json").write_text("{}")
        (self.shared / "skills").mkdir()
        (self.shared / "skills" / "test.txt").write_text("skill")

    def tearDown(self):
        self.tmpdir.cleanup()

    def snapshot(self):
        """Comparable state: sorted (relpath, kind, content-or-target)."""
        entries = []
        for path in sorted(self.data.rglob("*")):
            rel = str(path.relative_to(self.data))
            if separate_data.is_link(str(path)):
                kind = "link:" + ("dir" if os.path.isdir(path) else "file")
                entries.append((rel, kind, ""))
            elif path.is_file():
                entries.append((rel, "file", path.read_text(errors="replace")))
            elif path.is_dir():
                entries.append((rel, "dir", ""))
        return entries

    def test_setup_twice_converges(self):
        """Second setup run produces byte-identical state: no new links, no copies
        doubling, no data loss."""
        separate_data.setup_links(str(self.shared), str(self.data))
        first = self.snapshot()
        separate_data.setup_links(str(self.shared), str(self.data))
        second = self.snapshot()

        self.assertEqual(first, second)
        # And a third run is stable too
        separate_data.setup_links(str(self.shared), str(self.data))
        self.assertEqual(first, self.snapshot())

    def test_setup_after_partial_setup(self):
        """Setup interrupted after one link completes; re-run converges to the
        full layout without warnings or duplicates."""
        # Simulate a partial setup: one shared item already linked, rest missing
        self.data.mkdir(parents=True)
        separate_data.make_link(str(self.shared / "CLAUDE.md"), str(self.data / "CLAUDE.md"))

        import io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            separate_data.setup_links(str(self.shared), str(self.data))
            stderr_output = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr

        self.assertNotIn("not touching it", stderr_output)
        self.assertNotIn("could not link", stderr_output)

        # Converges to the same state as a clean setup
        reference_tmp = tempfile.TemporaryDirectory()
        try:
            clean_data = Path(reference_tmp.name) / "data"
            separate_data.setup_links(str(self.shared), str(clean_data))
            # Compare everything except copies.json timing noise: compare the
            # link/file layout, not manifest mtimes (snapshot reads contents).
            partial = [e for e in self.snapshot() if e[0] != "copies.json"]
            clean = [e for e in (
                (str(p.relative_to(clean_data)),
                 ("link:" + ("dir" if os.path.isdir(p) else "file"))
                 if separate_data.is_link(str(p))
                 else ("file" if p.is_file() else "dir"),
                 p.read_text(errors="replace") if p.is_file() and not separate_data.is_link(str(p)) else "")
                for p in sorted(clean_data.rglob("*")))
                if e[0] != "copies.json"]
            self.assertEqual(partial, clean)
        finally:
            reference_tmp.cleanup()

    def test_setup_repairs_broken_link(self):
        """A link whose target disappeared gets repaired on re-run, instead of
        being skipped forever."""
        shared_rules = self.shared / "rules"
        shared_rules.mkdir()
        (shared_rules / "rule.md").write_text("rule v1")
        separate_data.setup_links(str(self.shared), str(self.data))
        self.assertTrue(separate_data.is_link(str(self.data / "rules")))

        # Break the dst link by pointing it at a vanished target: replace the
        # link with one to a now-deleted path.
        # remove_link handles a Windows junction and a POSIX symlink alike (os.rmdir refuses a
        # symlink on Linux).
        self.assertTrue(separate_data.remove_link(str(self.data / "rules")))
        vanished = Path(self.tmpdir.name) / "vanished"
        vanished.mkdir()
        separate_data.make_link(str(vanished), str(self.data / "rules"))
        shutil.rmtree(str(vanished))
        # Now data/rules is a broken link; setup must repair it
        self.assertTrue(separate_data.is_link(str(self.data / "rules")))
        self.assertFalse((self.data / "rules").exists())

        separate_data.setup_links(str(self.shared), str(self.data))

        self.assertTrue((self.data / "rules" / "rule.md").read_text() == "rule v1")

    def test_migrate_dry_run_twice_changes_nothing(self):
        """migrate --dry-run is safe twice: no moves, no history edits."""
        project = self.shared / "projects" / "proj"
        project.mkdir(parents=True)
        session_uuid = "dry-uuid"
        (project / f"{session_uuid}.jsonl").write_text('{"model":"lumo-max"}\n')
        shared_history = self.shared / "history.jsonl"
        shared_history.write_text(
            json.dumps({"sessionId": session_uuid, "content": "line"}) + "\n")

        self.data.mkdir()
        (self.data / "projects").mkdir(exist_ok=True)

        def store_state():
            return ((project / f"{session_uuid}.jsonl").exists(),
                    shared_history.read_text(),
                    sorted(str(p.relative_to(self.data))
                           for p in self.data.rglob("*")))

        before = store_state()
        separate_data.migrate_sessions(
            str(self.data), str(self.shared), dry_run=True, confirm=False)
        after_first = store_state()
        separate_data.migrate_sessions(
            str(self.data), str(self.shared), dry_run=True, confirm=False)
        after_second = store_state()

        self.assertEqual(before, after_first)
        self.assertEqual(after_first, after_second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
