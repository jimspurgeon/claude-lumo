"""install-supervisor.ps1 instruction-line quoting (#140, #143).

The generated `nssm install` and WinSW setup lines must survive being pasted
back into PowerShell: inner double quotes get stripped on paste, splitting
arguments whenever a path contains a space (nssm, #140) or breaking the line
into multiple statements (WinSW, #143). The fix wraps them in single quotes.
These tests prove the pasted lines arrive intact, using a state dir with a
space in its name (nssm) and paste-through stubs for the WinSW cmdlets.
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

    def test_winsw_foreach_body_lines_use_single_quoted_write_output(self):
        """The WinSW foreach-body Write-Output lines use single quotes (#143)."""
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "Write-Output '        New-Item -ItemType Directory -Force "
            "\"$HOME\\.lumoservices\\$s\\logs\" | Out-Null'",
            src,
        )
        self.assertIn(
            "Write-Output '        Invoke-WebRequest "
            "https://github.com/winsw/winsw/releases/download/v2.12.0/"
            "WinSW-x64.exe -OutFile \"$HOME\\.lumoservices\\$s\\WinSW.exe\"'",
            src,
        )


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class TestPastedArguments(unittest.TestCase):
    """Run the generator, then paste its output through an nssm/winsw stub."""

    def _generate(self, state_dir, tool):
        env = dict(os.environ, CLAUDE_LUMO_STATE_DIR=state_dir)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", SCRIPT, "-Tool", tool],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _paste_through_stub(self, temp_dir, line):
        """Paste `line` as PowerShell source after defining a command stub;
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

    def _extract_block(self, stdout):
        """Extract the '# 1.' foreach block from the winsw output verbatim."""
        lines = stdout.splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "foreach ($s in 'LumoLiteLLM','LumoProxy') {":
                start = i
                break
        if start is None:
            raise AssertionError("foreach line not found in winsw output")
        # Block runs to the closing "    }" line.
        for j in range(start, len(lines)):
            if lines[j].strip() == "}":
                return lines[start:j + 1]
        raise AssertionError("closing brace not found after foreach line")

    def test_spaced_state_dir_keeps_nssm_args_intact(self):
        temp_dir = tempfile.mkdtemp(suffix=" spaced dir")
        try:
            state_dir = os.path.join(temp_dir, "lum o state")
            stdout = self._generate(state_dir, "nssm")

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

    def test_winsw_lines_parse_and_expand_correctly(self):
        """The WinSW foreach block pastes back as valid PowerShell (#143).

        The generated block is pasted into a fresh PowerShell with New-Item
        and Invoke-WebRequest stubbed out, so we observe exactly what those
        cmdlets receive. With the defect, the inner double quotes vanish and
        the line breaks into stray statements; with the fix, a path containing
        spaces arrives as one quoted argument with the variables expanded.
        $HOME is read-only in PowerShell, so the block's `$HOME` references
        are substituted with a space-containing stub path before pasting.
        """
        temp_dir = tempfile.mkdtemp(suffix=" spaced winsw")
        try:
            home_stub = os.path.join(temp_dir, "home dir with spaces")
            stdout = self._generate(temp_dir, "winsw")
            block = [line.replace("$HOME", home_stub)
                     for line in self._extract_block(stdout)]

            out_file = os.path.join(temp_dir, "captured.txt")
            stub_ps1 = os.path.join(temp_dir, "stub.ps1")
            quoted = out_file.replace("'", "''")
            script = (
                "function New-Item {\n"
                "    'NEWITEM:' + ($args -join '|') | Add-Content -Path '%s'\n"
                "}\n"
                "function Invoke-WebRequest {\n"
                "    'WEBREQ:' + ($args -join '|') | Add-Content -Path '%s'\n"
                "}\n"
                "%s\n" % (quoted, quoted, "\n".join(block))
            )
            with open(stub_ps1, "w", encoding="utf-8") as f:
                f.write(script)
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", stub_ps1],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0,
                             "pasted block failed:\n%s\n%s" %
                             (proc.stdout, proc.stderr))
            with open(out_file, encoding="utf-8") as f:
                captured = f.read().splitlines()

            newitem = [c for c in captured if c.startswith("NEWITEM:")]
            webreq = [c for c in captured if c.startswith("WEBREQ:")]
            # Two services -> each cmdlet runs twice.
            self.assertEqual(len(newitem), 2, captured)
            self.assertEqual(len(webreq), 2, captured)

            # New-Item receives: -ItemType Directory -Force <path>. The
            # space-containing logs path must arrive as ONE argument.
            for svc, entry in zip(("LumoLiteLLM", "LumoProxy"), newitem):
                args = entry[len("NEWITEM:"):].split("|")
                self.assertIn("-ItemType", args)
                self.assertIn("Directory", args)
                self.assertIn("-Force", args)
                expected_logs = os.path.join(home_stub, ".lumoservices", svc,
                                             "logs")
                self.assertIn(expected_logs, args,
                              "logs path not one arg: %r" % args)

            # Invoke-WebRequest receives the URI and -OutFile <path>; the
            # space-containing exe path must arrive as ONE argument.
            for svc, entry in zip(("LumoLiteLLM", "LumoProxy"), webreq):
                args = entry[len("WEBREQ:"):].split("|")
                self.assertIn(
                    "https://github.com/winsw/winsw/releases/download/"
                    "v2.12.0/WinSW-x64.exe", args)
                self.assertIn("-OutFile", args)
                expected_exe = os.path.join(home_stub, ".lumoservices", svc,
                                            "WinSW.exe")
                self.assertIn(expected_exe, args,
                              "exe path not one arg: %r" % args)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
