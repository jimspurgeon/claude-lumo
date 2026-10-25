"""After an update, one up-front choice covers every running session (#118)."""
import importlib.util
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("session_restart", os.path.join(ROOT, "session-restart.py"))
session_restart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_restart)


def answers(*replies):
    """An ask() that returns the replies in order and records each question."""
    asked = []
    it = iter(replies)

    def ask(question):
        asked.append(question)
        return next(it)
    ask.asked = asked
    return ask


def sessions(*sids):
    return [{"session_id": sid, "cwd": f"/tmp/{sid}", "busy": False, "path": f"/reg/{sid}.json"}
            for sid in sids]


class ChooseBulk(unittest.TestCase):
    def test_each_answer_is_returned(self):
        for reply in ("w", "i", "o", "s"):
            self.assertEqual(session_restart.choose_bulk(2, ask=answers(reply)), reply)

    def test_question_names_every_option_and_the_count(self):
        ask = answers("s")
        session_restart.choose_bulk(3, ask=ask)
        q = ask.asked[0]
        for part in ("3 sessions", "[w]ait for all", "[i]nterrupt all", "[o]ne by one", "[s]kip all"):
            self.assertIn(part, q)
        ask = answers("s")
        session_restart.choose_bulk(1, ask=ask)
        self.assertIn("1 session?", ask.asked[0])

    def test_unknown_answer_skips_all(self):
        out = []
        self.assertEqual(session_restart.choose_bulk(2, ask=answers("x"), out=out.append), "s")
        self.assertIn("skipping all", out[0])

    def test_without_a_terminal_skips_all(self):
        """The default ask falls back to skip when stdin is not a tty (#39)."""
        with mock.patch.object(sys, "stdin", None):
            self.assertEqual(session_restart.choose_bulk(2), "s")


class RestartSessions(unittest.TestCase):
    def run_bulk(self, bulk, ask=None):
        calls = []

        def restart(send, sid, choice, path, note=True):
            calls.append((send, sid, choice, path, note))
            return True, "restarted"
        ss = sessions("aaaaaaaa1", "bbbbbbbb2")
        matches = {"aaaaaaaa1": "t1", "bbbbbbbb2": "t2"}
        out = []
        results = session_restart.restart_sessions(ss, matches, bulk, lambda t: f"send:{t}",
                                                   ask=ask, restart=restart, out=out.append)
        return calls, results, out

    def test_wait_all_and_interrupt_all_never_ask(self):
        for bulk in ("w", "i"):
            ask = answers()  # any question would raise StopIteration
            calls, results, _ = self.run_bulk(bulk, ask)
            self.assertEqual([(c[1], c[2]) for c in calls], [("aaaaaaaa1", bulk), ("bbbbbbbb2", bulk)])
            self.assertEqual([c[0] for c in calls], ["send:t1", "send:t2"])
            self.assertEqual(len(results), 2)
            self.assertEqual(ask.asked, [])

    def test_skip_all_restarts_nothing(self):
        calls, results, _ = self.run_bulk("s", answers())
        self.assertEqual((calls, results), ([], []))

    def test_one_by_one_asks_per_session(self):
        ask = answers("i", "")  # "" stands for an answer that is neither w, i nor s
        calls, results, out = self.run_bulk("o", ask)
        self.assertEqual(len(ask.asked), 2)
        self.assertIn("Restart aaaaaaaa?", ask.asked[0])
        self.assertEqual([(c[1], c[2]) for c in calls], [("aaaaaaaa1", "i")])
        self.assertIn("  Unknown choice; skipping.", out)

    def test_one_by_one_skip(self):
        calls, _, _ = self.run_bulk("o", answers("s", "w"))
        self.assertEqual([(c[1], c[2]) for c in calls], [("bbbbbbbb2", "w")])


class ListOrcaTerminals(unittest.TestCase):
    """Encoding handling for the orca CLI call (#167).

    orca emits UTF-8. On Windows, subprocess.run(text=True) with no encoding decodes
    with the ANSI code page (cp1252), which has no mapping for byte 0x90; the decode
    error fires in subprocess's reader thread, stdout comes back None, and
    json.loads(None) raised TypeError out of a function that only caught
    (TimeoutExpired, OSError, ValueError). The whole session-restart script died.
    """

    # Valid UTF-8 JSON whose bytes cannot be decoded as cp1252: U+0090 encodes to
    # C2 90, and 0x90 is undefined in cp1252. Real orca output contains such bytes
    # whenever a worktree path has non-ASCII characters.
    PAYLOAD = (b'{"result":{"terminals":[{"handle": "t1", "worktreePath": "C:/Users/Jim/'
               + "path-\u00e9-\u0090-chars".encode("utf-8")
               + b'"}]}}\n')

    def fake_run(self, seen):
        """Stand-in for subprocess.run that records the kwargs it was called with."""
        def run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen.update(kwargs)
            class R:
                returncode = 0
                stdout = self.PAYLOAD.decode(kwargs.get("encoding") or "utf-8",
                                             kwargs.get("errors") or "strict")
            return R()
        return run

    def test_payload_really_breaks_the_windows_default(self):
        # Guard the guard: if this payload decoded as cp1252, the regression test
        # below would pass even with the bug present.
        with self.assertRaises(UnicodeDecodeError):
            self.PAYLOAD.decode("cp1252")

    def test_explicit_utf8_encoding_is_passed(self):
        seen = {}
        with mock.patch.object(session_restart.subprocess, "run", self.fake_run(seen)):
            terminals = session_restart.list_orca_terminals("orca")
        self.assertEqual(seen.get("encoding"), "utf-8")
        self.assertEqual(seen.get("errors"), "replace")
        self.assertEqual(terminals[0]["handle"], "t1")
        self.assertEqual(seen["cmd"], ["orca", "terminal", "list", "--json"])

    def test_none_stdout_returns_empty_list(self):
        # Belt-and-braces: even if stdout is None (a decode failure in a reader
        # thread our fix does not control), json.loads(None) raising TypeError must
        # surface as [] instead of crashing the whole script.
        class R:
            returncode = 0
            stdout = None
        with mock.patch.object(session_restart.subprocess, "run",
                               lambda cmd, **kw: R()):
            self.assertEqual(session_restart.list_orca_terminals("orca"), [])


if __name__ == "__main__":
    unittest.main()
