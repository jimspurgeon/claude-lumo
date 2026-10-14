"""Restarting the proxy and LiteLLM after "wait for all" or "interrupt all" (#119).

Every runner is a fake: no test lists, stops or starts a real process or touches ports 8790/4000.
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import service_restart as sr  # noqa: E402

spec = importlib.util.spec_from_file_location("session_restart", os.path.join(ROOT, "session-restart.py"))
session_restart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_restart)

WINDOWS_NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1000
  TCP    127.0.0.1:8790         0.0.0.0:0              LISTENING       111
  TCP    127.0.0.1:18790        0.0.0.0:0              LISTENING       555
  TCP    127.0.0.1:52000        127.0.0.1:8790         ESTABLISHED     777
  TCP    127.0.0.1:4000         0.0.0.0:0              ABHOEREN        222
  TCP    [::1]:8790             [::]:0                 LISTENING       111
  UDP    0.0.0.0:8790           *:*                                    888
"""

LINUX_NETSTAT = """Active Internet connections (only servers)
Proto Recv-Q Send-Q Local Address           Foreign Address         State       PID/Program name
tcp        0      0 127.0.0.1:8790          0.0.0.0:*               LISTEN      111/node
tcp        0      0 127.0.0.1:4000          0.0.0.0:*               LISTEN      -
tcp        0      0 127.0.0.1:18790         0.0.0.0:*               LISTEN      555/python
tcp6       0      0 ::1:8790                :::*                    LISTEN      111/node
tcp        0      0 127.0.0.1:4001          0.0.0.0:*               LISTEN      333/my server
"""

SS_OUTPUT = """State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
LISTEN 0      511        127.0.0.1:8790       0.0.0.0:*     users:(("node",pid=111,fd=20))
LISTEN 0      2048       127.0.0.1:4000       0.0.0.0:*
LISTEN 0      511        127.0.0.1:18790      0.0.0.0:*     users:(("python",pid=555,fd=3))
LISTEN 0      511            [::1]:8790          [::]:*     users:(("node",pid=111,fd=21))
"""


class Parsing(unittest.TestCase):
    def test_windows_netstat(self):
        self.assertEqual(sr.parse_listeners(WINDOWS_NETSTAT, 8790, "windows"), [111])
        # The state word is localized; the :0 foreign address marks the listener.
        self.assertEqual(sr.parse_listeners(WINDOWS_NETSTAT, 4000, "windows"), [222])
        self.assertEqual(sr.parse_listeners(WINDOWS_NETSTAT, 9999, "windows"), [])

    def test_linux_netstat(self):
        self.assertEqual(sr.parse_listeners(LINUX_NETSTAT, 8790, "linux"), [111])
        self.assertEqual(sr.parse_listeners(LINUX_NETSTAT, 4000, "linux"), [])  # PID hidden: '-'
        self.assertEqual(sr.parse_listeners(LINUX_NETSTAT, 4001, "linux"), [333])  # name with a space

    def test_linux_ss(self):
        self.assertEqual(sr.parse_listeners(SS_OUTPUT, 8790, "ss"), [111])
        self.assertEqual(sr.parse_listeners(SS_OUTPUT, 4000, "ss"), [])  # another user's: no process
        self.assertEqual(sr.parse_listeners(SS_OUTPUT, 9999, "ss"), [])

    def test_linux_falls_back_to_ss_when_netstat_is_missing(self):
        def run(argv):
            return (None, "") if argv[0] == "netstat" else (0, SS_OUTPUT)
        self.assertEqual(sr.find_listeners(8790, "linux", run), [111])
        self.assertIsNone(sr.find_listeners(8790, "linux", lambda argv: (None, "")))

    def test_process_images(self):
        self.assertEqual(sr.parse_image('"node.exe","111","Console","1","50,000 K"\r\n', "windows"), "node")
        self.assertEqual(sr.parse_image('"python3.12.exe","2","Console","1","9 K"', "windows"), "python")
        self.assertIsNone(sr.parse_image("INFO: No tasks are running which match the specified criteria.",
                                         "windows"))
        self.assertEqual(sr.parse_image("litellm\n", "linux"), "litellm")
        self.assertEqual(sr.parse_image("/usr/bin/node\n", "linux"), "node")
        self.assertIsNone(sr.parse_image("", "linux"))
        self.assertEqual(sr.image_command(7, "windows"), ["tasklist", "/FI", "PID eq 7", "/FO", "CSV", "/NH"])
        self.assertEqual(sr.image_command(7, "linux"), ["ps", "-p", "7", "-o", "comm="])

    def test_macos_lsof(self):
        self.assertEqual(sr.parse_listeners("111\n112\n", 8790, "darwin"), [111, 112])
        self.assertEqual(sr.parse_listeners("", 8790, "darwin"), [])

    def test_commands_per_platform(self):
        self.assertEqual(sr.stop_command(111, "windows"), ["taskkill", "/PID", "111", "/F"])
        self.assertEqual(sr.stop_command(111, "linux"), ["kill", "111"])
        self.assertEqual(sr.listeners_commands(8790, "windows"), [(["netstat", "-ano"], "windows")])
        self.assertEqual(sr.listeners_commands(8790, "linux"),
                         [(["netstat", "-tlnp"], "linux"), (["ss", "-tlnp"], "ss")])
        self.assertEqual(sr.listeners_commands(8790, "darwin")[0][0][0], "lsof")

    def test_services_match_the_launcher(self):
        svcs = sr.services("/inst", "/state", "windows")
        self.assertEqual([s["port"] for s in svcs], [8790, 4000])  # proxy first
        proxy, litellm = svcs
        self.assertEqual(proxy["argv"], ["node", "--no-warnings", "proxy.mjs"])
        self.assertEqual(proxy["cwd"], os.path.join("/inst", "proxy"))
        self.assertEqual(proxy["health"], "http://127.0.0.1:8790/v1/health")
        self.assertEqual(litellm["argv"][0], os.path.join("/state", "litellm-venv", "Scripts", "litellm.exe"))
        self.assertEqual(litellm["argv"][1:], ["--config", os.path.join("/inst", "proxy", "litellm.yaml"),
                                               "--host", "127.0.0.1", "--port", "4000"])
        self.assertEqual(litellm["log"], os.path.join("/state", "litellm.log"))
        self.assertEqual(litellm["cwd"], "/state")  # never the caller's folder
        self.assertEqual(litellm["health"], "http://127.0.0.1:4000/health/liveliness")
        self.assertEqual(sr.services("/i", "/s", "linux")[1]["argv"][0],
                         os.path.join("/s", "litellm-venv", "bin", "litellm"))


class World:
    """Fake processes and ports: netstat, taskkill/kill, spawn, health and a clock."""

    def __init__(self, plat="windows", listeners=None, kill_fails=(), spawn_fails=(), never_healthy=(),
                 netstat_fails=False, images=None, install="/inst"):
        self.plat, self.install = plat, install
        self.images = dict(images if images is not None else {111: "node", 222: "python"})
        self.listeners = dict(listeners if listeners is not None else {8790: 111, 4000: 222})
        self.kill_fails, self.spawn_fails = set(kill_fails), set(spawn_fails)
        self.never_healthy, self.netstat_fails = set(never_healthy), netstat_fails
        self.events, self.out, self.now, self.next_pid = [], [], 0.0, 900

    def run(self, argv):
        if argv[0] == "tasklist":
            pid = int(argv[2].split()[-1])
            image = self.images.get(pid)
            return 0, (f'"{image}.exe","{pid}","Console","1","9 K"' if image else "INFO: No tasks")
        if argv[0] == "ps":
            image = self.images.get(int(argv[2]))
            return (0, image + "\n") if image else (1, "")
        if argv[0] in ("netstat", "ss", "lsof"):
            if self.netstat_fails:
                return None, ""
            if self.plat == "windows":
                lines = [f"  TCP    127.0.0.1:{p}   0.0.0.0:0   LISTENING   {pid}" for p, pid in self.listeners.items()]
            else:
                lines = [f"tcp 0 0 127.0.0.1:{p} 0.0.0.0:* LISTEN {pid}/x" for p, pid in self.listeners.items()]
            return 0, "\n".join(lines)
        pid = int(argv[2] if argv[0] == "taskkill" else argv[1])
        self.events.append(("stop", pid))
        if pid in self.kill_fails:
            return 1, ""
        self.listeners = {p: q for p, q in self.listeners.items() if q != pid}
        return 0, ""

    def port_for(self, argv):
        return 8790 if argv[0] == "node" else 4000

    def spawn(self, argv, cwd=None, env=None, log=None):
        port = self.port_for(argv)
        self.events.append(("start", port, tuple(argv), cwd, (env or {}).get("LITELLM_MASTER_KEY"), log))
        if port in self.spawn_fails:
            raise FileNotFoundError(argv[0])
        self.next_pid += 1
        self.listeners[port] = self.next_pid
        self.images[self.next_pid] = "node" if port == 8790 else "litellm"

    def healthy(self, url):
        port = 8790 if ":8790/" in url else 4000
        return port not in self.never_healthy and port in self.listeners

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now

    def restart(self, state, dry_run=False):
        return sr.restart_services(self.install, state, self.plat, run=self.run, spawn=self.spawn,
                                   healthy=self.healthy, sleep=self.sleep, clock=self.clock,
                                   out=self.out.append, dry_run=dry_run)


class RestartServices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        with open(os.path.join(self.state, "master.key"), "w", encoding="utf-8") as f:
            f.write("sk-test\n")
        self.install = os.path.join(self.tmp.name, "lumo")
        os.makedirs(os.path.join(self.install, "proxy"))
        open(os.path.join(self.install, "proxy", "proxy.mjs"), "w").close()

    def world(self, **kw):
        return World(install=self.install, **kw)

    def kinds(self, world):
        return [(e[0], e[1]) for e in world.events]

    def test_stops_then_starts_each_service_proxy_first(self):
        w = self.world()
        self.assertTrue(w.restart(self.state))
        self.assertEqual(self.kinds(w), [("stop", 111), ("start", 8790), ("stop", 222), ("start", 4000)])
        proxy, litellm = [e for e in w.events if e[0] == "start"]
        self.assertEqual(proxy[2], ("node", "--no-warnings", "proxy.mjs"))
        self.assertEqual(proxy[3], os.path.join(self.install, "proxy"))
        self.assertEqual(litellm[3], self.state)
        self.assertEqual(litellm[4], "sk-test")  # LITELLM_MASTER_KEY from <state>/master.key
        self.assertEqual(litellm[5], os.path.join(self.state, "litellm.log"))
        self.assertIn("  Restarted the Lumo proxy (port 8790).", w.out)
        self.assertIn("  Restarted LiteLLM (port 4000).", w.out)

    def test_a_foreign_listener_is_never_stopped(self):
        """Port 8790 held by something that is not node: left alone, manual command printed (#119)."""
        w = self.world(images={111: "nginx", 222: "python"})
        self.assertFalse(w.restart(self.state))
        self.assertEqual(self.kinds(w), [("stop", 222), ("start", 4000)])
        msg = [o for o in w.out if "PID 111" in o][0]
        self.assertIn("does not look like the Lumo proxy", msg)
        self.assertIn("taskkill /PID 111 /F", msg)
        w = self.world(images={111: "nginx"})
        w.restart(self.state, dry_run=True)
        self.assertTrue(any("would be left alone" in o for o in w.out))

    def test_a_generic_interpreter_needs_the_health_check(self):
        """python on 4000 that fails LiteLLM's health check is not stopped, even if it is a hung
        LiteLLM; node on 8790 that answers the proxy's health check is (#119)."""
        w = self.world(images={111: "node", 222: "python"}, never_healthy={4000})
        self.assertFalse(w.restart(self.state))
        self.assertEqual(self.kinds(w), [("stop", 111), ("start", 8790)])
        msg = [o for o in w.out if "PID 222" in o][0]
        self.assertIn("does not answer http://127.0.0.1:4000/health/liveliness", msg)
        self.assertIn("a hung LiteLLM looks the same", msg)
        w = self.world(images={111: "node", 222: "node"})
        self.assertFalse(w.restart(self.state))  # node is not LiteLLM's interpreter
        self.assertNotIn(("stop", 222), self.kinds(w))

    def test_a_litellm_image_is_stopped_on_the_image_alone(self):
        w = self.world(images={111: "node", 222: "litellm"}, never_healthy={4000})
        w.restart(self.state)
        self.assertIn(("stop", 222), self.kinds(w))

    def test_unknown_image_needs_the_health_check(self):
        # No image visible and nothing answers the health URL: not ours, not stopped.
        w = self.world(images={}, never_healthy={8790, 4000})
        self.assertFalse(w.restart(self.state))
        self.assertEqual(w.events, [])
        # No image visible but the health URL answers: Lumo's, stopped and restarted.
        w = self.world(images={})
        w.healthy = lambda url: True
        self.assertTrue(w.restart(self.state))
        self.assertEqual(self.kinds(w)[0], ("stop", 111))

    def test_no_install_touches_nothing(self):
        w = World(install=os.path.join(self.tmp.name, "missing"))
        self.assertFalse(w.restart(self.state))
        self.assertEqual(w.events, [])
        self.assertIn("No claude-lumo install", w.out[0])

    def test_failed_listing_while_waiting_is_not_free(self):
        """A listing that fails after the stop proves nothing, so the port is not taken as free."""
        w = self.world()
        real_run, calls = w.run, []

        def run(argv):
            if argv[0] == "netstat":
                calls.append(argv)
                if len(calls) > 1:
                    return None, ""
            return real_run(argv)
        w.run = run
        self.assertFalse(w.restart(self.state))
        self.assertNotIn(("start", 8790), self.kinds(w))
        self.assertTrue(any("Port 8790 is still in use" in o for o in w.out))

    def test_posix_uses_kill(self):
        w = self.world(plat="linux")
        self.assertTrue(w.restart(self.state))
        self.assertEqual(self.kinds(w)[0], ("stop", 111))

    def test_nothing_running_just_starts(self):
        w = self.world(listeners={})
        self.assertTrue(w.restart(self.state))
        self.assertEqual(self.kinds(w), [("start", 8790), ("start", 4000)])

    def test_dry_run_touches_nothing(self):
        w = self.world()
        self.assertTrue(w.restart(self.state, dry_run=True))
        self.assertEqual(w.events, [])
        text = "\n".join(w.out)
        self.assertIn("taskkill /PID 111 /F", text)
        self.assertIn("would start the Lumo proxy: node --no-warnings proxy.mjs", text)
        self.assertIn("http://127.0.0.1:4000/health/liveliness", text)

    def test_a_failed_stop_leaves_that_service_and_carries_on(self):
        w = self.world(kill_fails={111})
        self.assertFalse(w.restart(self.state))
        self.assertEqual(self.kinds(w), [("stop", 111), ("stop", 222), ("start", 4000)])
        text = "\n".join(w.out)
        self.assertIn("run: taskkill /PID 111 /F", text)
        self.assertIn("Port 8790 is still in use", text)
        self.assertIn("nohup node --no-warnings proxy.mjs", text)  # the manual start command

    def test_listing_failure_is_reported(self):
        w = self.world(netstat_fails=True)
        self.assertFalse(w.restart(self.state))
        self.assertEqual(w.events, [])
        self.assertIn("Could not list the processes on port 8790", w.out[0])

    def test_hidden_pid_of_a_healthy_service_is_not_guessed(self):
        w = self.world(plat="linux", listeners={8790: 950})
        w.listeners = {8790: 950}
        with mock.patch.object(sr, "parse_listeners", return_value=[]):
            self.assertFalse(w.restart(self.state))
        self.assertNotIn(("stop", 950), self.kinds(w))
        self.assertIn("PID is not visible", w.out[0])

    def test_a_failed_start_prints_the_manual_command(self):
        w = self.world(spawn_fails={8790})
        self.assertFalse(w.restart(self.state))
        self.assertIn(("start", 4000), self.kinds(w))
        self.assertTrue(any("Could not start the Lumo proxy" in o and "nohup node" in o for o in w.out))

    def test_failed_health_check_times_out_on_the_fake_clock(self):
        w = self.world(never_healthy={4000}, images={111: "node", 222: "litellm"})
        self.assertFalse(w.restart(self.state))
        msg = [o for o in w.out if "did not answer" in o][0]
        self.assertIn("within 45s", msg)
        self.assertIn("litellm.log", msg)
        self.assertGreaterEqual(w.now, sr.LITELLM_TIMEOUT)

    def test_missing_master_key_skips_litellm(self):
        os.remove(os.path.join(self.state, "master.key"))
        w = self.world()
        self.assertFalse(w.restart(self.state))
        self.assertNotIn(("start", 4000), self.kinds(w))
        self.assertTrue(any("master.key" in o for o in w.out))


class SpawnDetached(unittest.TestCase):
    def test_detached_child_runs_with_env_and_log(self):
        """The real spawner, on a short-lived python child that binds no port."""
        import time
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "child.log")
            env = dict(os.environ, LUMO_TEST_VALUE="ok-119")
            sr.spawn_detached([sys.executable, "-c", "import os; print(os.environ['LUMO_TEST_VALUE'])"],
                              cwd=d, env=env, log=log)
            deadline = time.monotonic() + 20
            text = ""
            while time.monotonic() < deadline and "ok-119" not in text:
                time.sleep(0.1)
                with open(log, encoding="utf-8") as f:
                    text = f.read()
            self.assertIn("ok-119", text)
            time.sleep(0.5)  # let the child exit so the temp dir can be removed on Windows


class SessionOrder(unittest.TestCase):
    """Other sessions first, then the proxy and LiteLLM, then the held-back continue notes."""

    def run_bulk(self, bulk):
        events = []

        def restart(send, sid, choice, path, note=True):
            events.append(("session", sid, note))
            return True, "restarted"

        def make_send(target):
            return lambda text, enter=False, interrupt=False: events.append(("send", target, text)) or True

        ss = [{"session_id": sid, "cwd": "/", "busy": True, "path": "/r"} for sid in ("a1", "b2")]
        results = session_restart.restart_sessions(ss, {"a1": "t1", "b2": "t2"}, bulk, make_send,
                                                   restart=restart, out=lambda *_: None)

        def services(out=print):
            events.append(("services",))
            return True
        self.assertTrue(session_restart.restart_services_after(results, services, out=lambda *_: None))
        return events

    def test_interrupt_all_holds_the_continue_note_until_services_are_back(self):
        events = self.run_bulk("i")
        self.assertEqual(events, [
            ("session", "a1", False), ("session", "b2", False), ("services",),
            ("send", "t1", session_restart.CONTINUE_NOTE), ("send", "t2", session_restart.CONTINUE_NOTE)])

    def test_wait_all_restarts_services_after_sessions_and_sends_no_note(self):
        self.assertEqual(self.run_bulk("w"), [("session", "a1", True), ("session", "b2", True), ("services",)])

    def test_restart_in_terminal_can_leave_the_note_to_the_caller(self):
        sent = []
        with mock.patch.object(session_restart.time, "sleep"):
            ok, _ = session_restart.restart_in_terminal(
                lambda text, enter=False, interrupt=False: sent.append(text) or True, "sid", "i", "/r", note=False)
        self.assertTrue(ok)
        self.assertEqual(sent, [None, "/exit", "claude-lumo --resume sid"])


class MainFlow(unittest.TestCase):
    """session-restart.py main() with the services module replaced by a recorder."""

    def run_main(self, argv, answers=(), sessions=(), restart_result=(True, "restarted")):
        calls = []
        fake = types.SimpleNamespace(restart_services=lambda **kw: calls.append(kw) or True)
        out = []
        it = iter(answers)
        env = {"CLAUDE_CODE_SESSION_ID": "self-sid"}
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, dict(env, CLAUDE_LUMO_STATE_DIR=state)), \
                mock.patch.object(sys, "argv", ["session-restart.py", "--new-version", "v9.9.9"] + argv), \
                mock.patch.object(session_restart, "load_service_restart", return_value=fake), \
                mock.patch.object(session_restart, "sessions_from_registry", return_value=list(sessions)), \
                mock.patch.object(session_restart, "list_tmux_panes", return_value=[("%1", "/w")]), \
                mock.patch.object(session_restart, "ask_choice", side_effect=lambda q: next(it)), \
                mock.patch.object(session_restart, "restart_in_terminal", return_value=restart_result), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            session_restart.main()
        return calls, "\n".join(out)

    def test_dry_run_only_plans(self):
        calls, text = self.run_main(["--dry-run", "--controller", "tmux"],
                                    sessions=[{"session_id": "s1", "cwd": "/w", "busy": False, "path": "/r"}])
        self.assertEqual(calls, [{"dry_run": True}])
        self.assertIn("[dry run] would restart s1", text)

    def test_no_other_sessions_offers_a_services_restart(self):
        calls, text = self.run_main([], answers=["y"])
        self.assertEqual(calls, [{}])
        self.assertIn("Restart it last", text)
        self.assertIn("claude-lumo --resume self-sid", text)
        calls, text = self.run_main([], answers=["s"])
        self.assertEqual(calls, [])
        self.assertIn("service_restart.py", text)

    def test_skip_all_leaves_services_alone(self):
        calls, text = self.run_main(["--controller", "tmux"], answers=["s"],
                                    sessions=[{"session_id": "s1", "cwd": "/w", "busy": False, "path": "/r"}])
        self.assertEqual(calls, [])
        self.assertIn("Finally, restart the session that ran the update (this one) last", text)

    def test_wait_all_restarts_services_then_names_the_update_session(self):
        calls, text = self.run_main(["--controller", "tmux"], answers=["w"],
                                    sessions=[{"session_id": "s1", "cwd": "/w", "busy": False, "path": "/r"}])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("dry_run", calls[0])
        self.assertLess(text.index("Restarted s1"), text.index("Restarting the proxy and LiteLLM"))
        self.assertIn("`claude-lumo --resume self-sid`", text)


    def test_a_session_that_did_not_restart_keeps_the_services_running(self):
        """A wait timeout or failed restart leaves the proxy and LiteLLM alone (#119)."""
        for bulk in ("w", "i"):
            calls, text = self.run_main(["--controller", "tmux"], answers=[bulk],
                                        sessions=[{"session_id": "s1", "cwd": "/w", "busy": True, "path": "/r"}],
                                        restart_result=(False, "timed out waiting for the session to go idle"))
            self.assertEqual(calls, [])
            self.assertIn("Not every session restarted cleanly", text)
            self.assertIn("service_restart.py", text)
            self.assertIn("Finally, restart the session that ran the update", text)

    def test_an_unreachable_session_keeps_the_services_running(self):
        calls, text = self.run_main(["--controller", "tmux"], answers=["w"],
                                    sessions=[{"session_id": "s1", "cwd": "/w", "busy": False, "path": "/r"},
                                              {"session_id": "s2", "cwd": "/elsewhere", "busy": True, "path": "/r2"}])
        self.assertEqual(calls, [])
        self.assertIn("Restarted s1", text)
        self.assertIn("Not every session restarted cleanly", text)


class HeldNotes(unittest.TestCase):
    def test_interrupted_sessions_still_get_their_note_when_services_are_skipped(self):
        sent = []

        def sender(name):
            return lambda text, enter=False, interrupt=False: sent.append((name, text))
        results = [({"session_id": "a"}, "i", True, sender("a")), ({"session_id": "b"}, "i", False, sender("b"))]
        services = mock.Mock()
        out = []
        self.assertFalse(session_restart.restart_services_after(results, services, out=out.append))
        services.assert_not_called()
        self.assertEqual(sent, [("a", session_restart.CONTINUE_NOTE)])


if __name__ == "__main__":
    unittest.main()
