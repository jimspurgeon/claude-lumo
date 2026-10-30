"""scripts/watchdog.sh and the launcher's watchdog wiring (#171).

Everything here runs offline: no test binds ports 8790 or 4000, and no test touches the
real install or state dir. The watchdog's pure helpers (rotation, backoff) are exposed
through `--selftest`, and the loop is exercised against a throwaway HTTP server on a
spare port with WATCHDOG_DRYRUN=1, so a detected failure never launches a real service.
"""
import http.server
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# On Windows a bare "bash" resolves to System32's WSL launcher before PATH is searched (#91).
BASH = shutil.which("bash") or "bash"
WATCHDOG = os.path.join(HERE, "scripts", "watchdog.sh")
LAUNCHER = os.path.join(HERE, "claude-lumo.sh")

ISO_PREFIX = "[" + time.strftime("%Y-%m-%dT", time.gmtime())


def free_port():
    """A spare port in 18000-18999 that nothing is listening on (repo testing rule)."""
    for _ in range(50):
        port = 18000 + (os.getpid() * 7919 + int(time.time() * 997)) % 800
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no spare port found")


def run_bash(script, extra_env=None, timeout=60):
    env = dict(os.environ)
    env.update(extra_env or {})
    return subprocess.run([BASH, "-c", script], env=env, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


class FakeService:
    """A minimal HTTP server answering 200 on any path, on a spare port only."""

    def __init__(self):
        self.port = free_port()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/health/liveliness"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Syntax(unittest.TestCase):
    def test_scripts_parse(self):
        for path in (WATCHDOG, LAUNCHER):
            r = run_bash(f'bash -n "{path}"')
            self.assertEqual(r.returncode, 0, f"{path}: {r.stderr}")


class Helpers(unittest.TestCase):
    """Pure helpers, through the script's --selftest entry point."""

    def selftest(self, *args, env=None):
        quoted = " ".join(f'"{a}"' for a in args)
        return run_bash(f'source "{WATCHDOG}" --selftest {quoted}', extra_env=env)

    def test_rotation_leaves_a_small_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "w.log")
            with open(log, "wb") as f:
                f.write(b"x" * 100)
            r = self.selftest("rotate", log, "1000")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(log))
            self.assertFalse(os.path.exists(log + ".1"))

    def test_rotation_renames_an_oversized_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "w.log")
            with open(log, "wb") as f:
                f.write(b"x" * 100)
            r = self.selftest("rotate", log, "50")  # small threshold stands in for 10 MB
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(log))
            self.assertTrue(os.path.exists(log + ".1"))
            self.assertEqual(os.path.getsize(log + ".1"), 100)

    def test_rotation_keeps_one_generation(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "w.log")
            with open(log, "wb") as f:
                f.write(b"new")
            with open(log + ".1", "wb") as f:
                f.write(b"old")
            self.selftest("rotate", log, "1")
            with open(log + ".1", encoding="utf-8") as f:
                self.assertEqual(f.read(), "new")

    def test_rotation_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "absent.log")
            r = self.selftest("rotate", log, "1")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(log + ".1"))

    def test_default_threshold_is_ten_megabytes(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "w.log")
            with open(log, "wb") as f:
                f.truncate(10485760)  # sparse: exactly 10 MB, not over
            self.selftest("rotate", log)
            self.assertTrue(os.path.exists(log), "10 MB exactly must not rotate")
            with open(log, "ab") as f:
                f.write(b"x")
            self.selftest("rotate", log)
            self.assertFalse(os.path.exists(log), "10 MB + 1 must rotate")

    def test_backoff_doubles_then_caps(self):
        table = {0: 30, 1: 60, 2: 120, 3: 240, 4: 480, 5: 600, 6: 600, 100: 600}
        for fails, expected in table.items():
            r = self.selftest("next-interval", str(fails))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), str(expected), f"consecutive={fails}")

    def test_backoff_honours_overrides(self):
        r = self.selftest("next-interval", "2", env={"CLAUDE_LUMO_WATCHDOG_INTERVAL": "10",
                                                     "WATCHDOG_BACKOFF_CAP": "25"})
        self.assertEqual(r.stdout.strip(), "25")


class Behaviour(unittest.TestCase):
    """The loop, against a throwaway server on a spare port, with relaunch disabled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        self.service = FakeService()
        self.addCleanup(self.service.stop)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=10)

    def log_text(self):
        path = os.path.join(self.state, "logs", "watchdog.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def start(self, **env_extra):
        env = dict(os.environ, CLAUDE_LUMO_STATE_DIR=self.state,
                   CLAUDE_LUMO_WATCHDOG_INTERVAL="2", WATCHDOG_DRYRUN="1",
                   WATCHDOG_GRACE="1",
                   WATCHDOG_PROXY_URL=self.service.url,
                   WATCHDOG_LITELLM_URL=self.service.url)
        env.update(env_extra)
        p = subprocess.Popen([BASH, WATCHDOG], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def wait_for(self, predicate, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def test_a_dead_service_is_detected_and_logged_with_a_timestamp(self):
        self.start()
        pidfile = os.path.join(self.state, "watchdog.pid")
        self.assertTrue(self.wait_for(lambda: os.path.exists(pidfile)), "no pidfile")
        self.assertTrue(self.wait_for(lambda: "watchdog started" in self.log_text()))
        self.service.stop()
        self.assertTrue(self.wait_for(lambda: "LiteLLM down" in self.log_text()),
                        self.log_text())
        text = self.log_text()
        self.assertIn("relaunching", text)
        self.assertRegex(text, r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]")
        self.assertIn(ISO_PREFIX, text)

    def test_a_healthy_service_is_never_restarted(self):
        self.start()
        pidfile = os.path.join(self.state, "watchdog.pid")
        self.assertTrue(self.wait_for(lambda: os.path.exists(pidfile)))
        time.sleep(6)  # several intervals with both services answering 200
        text = self.log_text()
        self.assertNotIn("down", text, text)
        self.assertNotIn("relaunching", text, text)

    def test_recovery_resets_the_backoff(self):
        self.start()
        pidfile = os.path.join(self.state, "watchdog.pid")
        self.assertTrue(self.wait_for(lambda: os.path.exists(pidfile)))
        self.service.stop()
        self.assertTrue(self.wait_for(lambda: "LiteLLM down" in self.log_text()))
        # Bring the service back on the same port; the watchdog must notice and reset.
        revived = FakeService.__new__(FakeService)
        revived.port = self.service.port

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass
        revived.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", revived.port), Handler)
        revived.thread = threading.Thread(target=revived.httpd.serve_forever, daemon=True)
        revived.thread.start()
        self.addCleanup(revived.stop)
        self.assertTrue(self.wait_for(lambda: "healthy again" in self.log_text(), timeout=30),
                        self.log_text())

    def test_second_instance_exits_zero_and_writes_nothing(self):
        self.start()
        pidfile = os.path.join(self.state, "watchdog.pid")
        self.assertTrue(self.wait_for(lambda: os.path.exists(pidfile)), "no pidfile")
        with open(pidfile, encoding="utf-8") as f:
            owner = f.read().strip()
        r = run_bash(f'CLAUDE_LUMO_STATE_DIR="{self.state}" bash "{WATCHDOG}"',
                     extra_env={"CLAUDE_LUMO_WATCHDOG_INTERVAL": "2", "WATCHDOG_DRYRUN": "1"},
                     timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "")
        # The loser must not have stolen the pidfile ($$ differs from Popen's Windows
        # pid under Git Bash, so compare content, not the OS pid).
        with open(pidfile, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), owner)

    def test_a_stale_pidfile_is_taken_over(self):
        os.makedirs(os.path.join(self.state, "logs"), exist_ok=True)
        pidfile = os.path.join(self.state, "watchdog.pid")
        with open(pidfile, "w", encoding="utf-8") as f:
            f.write("2147483000")  # above the kernel's max PID: cannot be alive
        self.start()
        self.assertTrue(self.wait_for(lambda: self.log_text() != ""), "never started")
        with open(pidfile, encoding="utf-8") as f:
            self.assertNotEqual(f.read().strip(), "2147483000")

    def test_the_log_rotates_at_its_size_cap(self):
        os.makedirs(os.path.join(self.state, "logs"), exist_ok=True)
        with open(os.path.join(self.state, "logs", "watchdog.log"), "wb") as f:
            f.write(b"x" * 200)
        self.start(WATCHDOG_MAX_LOG_BYTES="100")
        self.assertTrue(self.wait_for(lambda: os.path.exists(
            os.path.join(self.state, "logs", "watchdog.log.1"))), "no rotation")


class LauncherWiring(unittest.TestCase):
    """claude-lumo.sh, asserted at the source level like the other launcher tests."""

    @classmethod
    def setUpClass(cls):
        with open(LAUNCHER, encoding="utf-8") as f:
            cls.src = f.read()

    def test_litellm_log_is_appended_not_truncated(self):
        self.assertIn('--port 4000 >> "$S/litellm.log"', self.src)
        # The POSIX start_litellm, and no truncating redirect to the same file anywhere.
        self.assertNotIn('--port 4000 > "$S/litellm.log"', self.src)

    def test_litellm_log_rotates_before_relaunch(self):
        self.assertIn("10485760", self.src)
        self.assertIn('mv -f "$S/litellm.log" "$S/litellm.log.1"', self.src)

    def test_watchdog_spawn_is_flag_guarded(self):
        self.assertIn('CLAUDE_LUMO_WATCHDOG:-1', self.src)

    def test_windows_watchdog_spawn_goes_through_service_restart(self):
        """#164 applies to the watchdog too: nohup would tie it to the launching terminal on
        Windows, leaving the stack unwatched, so it is spawned detached like the services."""
        self.assertIn("--spawn-watchdog", self.src)
        # POSIX keeps the nohup spawn, and the Windows branch guards on service_restart.py
        # so an old install falls back to nohup, like the services.
        self.assertIn('nohup bash "$D/scripts/watchdog.sh"', self.src)
        self.assertIn('[ -n "$WIN" ] && [ -f "$D/service_restart.py" ]', self.src)

    def test_watchdog_spawn_comes_after_the_health_checks(self):
        self.assertLess(self.src.index("LiteLLM did not start"), self.src.index("watchdog.sh"))

    def test_the_change_names_the_issue(self):
        self.assertIn("#171", self.src)


class WatchdogWiring(unittest.TestCase):
    """scripts/watchdog.sh, asserted at the source level: relaunch must not tie a Windows
    service to the watchdog's own console (#164), and it must degrade like the launcher."""

    @classmethod
    def setUpClass(cls):
        with open(WATCHDOG, encoding="utf-8") as f:
            cls.src = f.read()

    def test_windows_relaunch_goes_through_service_restart(self):
        self.assertIn('service_restart.py" \\', self.src)
        self.assertIn("--start proxy", self.src)
        self.assertIn("--start litellm", self.src)

    def test_windows_routing_is_guarded_by_the_scripts_presence(self):
        # An old install without service_restart.py must fall back to nohup, like the launcher.
        self.assertIn('[ -f "$D/service_restart.py" ]', self.src)
        self.assertIn('nohup node --no-warnings proxy.mjs', self.src)

    def test_posix_relaunch_appends_to_the_litellm_log(self):
        self.assertIn('--port 4000 >> "$LITELLM_LOG"', self.src)

    def test_rotation_failure_is_logged_not_swallowed(self):
        self.assertIn("could not rotate", self.src)


if __name__ == "__main__":
    unittest.main()
