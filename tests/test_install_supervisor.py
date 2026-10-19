"""install-supervisor.ps1 nssm argument quoting (#140).

The generated `nssm install` lines must survive being pasted back into
PowerShell: inner double quotes get stripped on paste, splitting arguments
whenever a path contains a space. The fix wraps them in single quotes.
These tests prove the arguments arrive intact, using a state dir with a
space in its name.
"""
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "install-supervisor.ps1")


class TestSourceSyntax(unittest.TestCase):
    """Source-level checks for the nssm install lines (run everywhere)."""

    def test_install_lines_use_single_quote_wrapping(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "nssm install LumoLiteLLM '$LiteLLMExeResolved' "
            "'--config \"$LiteLLMConfigResolved\" --host 127.0.0.1 --port 4000'",
            src,
        )
        self.assertIn("nssm install LumoProxy 'node' '--no-warnings proxy.mjs'", src)


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class TestPastedArguments(unittest.TestCase):
    """Run the generator, then paste its output through an nssm stub."""

    def _generate(self, state_dir):
        env = dict(os.environ, CLAUDE_LUMO_STATE_DIR=state_dir)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", SCRIPT, "-Tool", "nssm"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _paste_through_stub(self, temp_dir, line):
        """Paste `line` as PowerShell source after defining an nssm stub;
        return the argument list the stub received (one per line)."""
        out_file = os.path.join(temp_dir, "captured_args.txt")
        stub_ps1 = os.path.join(temp_dir, "stub.ps1")
        script = (
            "function nssm {\n"
            "    $args | Set-Content -Path '%s'\n"
            "}\n"
            "%s\n" % (out_file.replace("'", "''"), line)
        )
        with open(stub_ps1, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", stub_ps1],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out_file, encoding="utf-8") as f:
            return f.read().rstrip("\n").split("\n")

    @staticmethod
    def _extract(stdout, marker):
        for line in stdout.splitlines():
            if line.strip().startswith(marker):
                return line.strip()
        raise AssertionError("no line starting with %r in output" % marker)

    def test_spaced_state_dir_keeps_args_intact(self):
        temp_dir = tempfile.mkdtemp(suffix=" spaced dir")
        try:
            state_dir = os.path.join(temp_dir, "lum o state")
            stdout = self._generate(state_dir)

            lite_line = self._extract(stdout, "nssm install LumoLiteLLM")
            args = self._paste_through_stub(temp_dir, lite_line)
            self.assertEqual(args[0], "install")
            self.assertEqual(args[1], "LumoLiteLLM")
            # Exe path with spaces must arrive as ONE argument.
            self.assertEqual(args[2], os.path.join(state_dir,
                                                    "litellm-venv", "Scripts",
                                                    "litellm.exe"))
            # The whole LiteLLM argument must arrive as ONE argument with the
            # inner double quotes preserved.
            expected_arg = '--config "%s" --host 127.0.0.1 --port 4000' % (
                os.path.join(ROOT, "proxy", "litellm.yaml"))
            self.assertEqual(args[3], expected_arg)

            proxy_line = self._extract(stdout, "nssm install LumoProxy")
            pargs = self._paste_through_stub(temp_dir, proxy_line)
            self.assertEqual(pargs[0], "install")
            self.assertEqual(pargs[1], "LumoProxy")
            self.assertEqual(pargs[2], "node")
            self.assertEqual(pargs[3], "--no-warnings proxy.mjs")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
