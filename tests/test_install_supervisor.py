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
import shutil
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "gateway", "install-supervisor.ps1")


class TestSourceSyntax(unittest.TestCase):
    """Source-level checks for the nssm install lines (run everywhere)."""

    def test_install_lines_use_single_quote_wrapping(self):
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "nssm install LumoGatewayLiteLLM '$LiteLLMExeResolved' "
            "'--config \"$LiteLLMConfigResolved\" --host 127.0.0.1 --port 4000'",
            src,
        )
        self.assertIn("nssm install LumoGatewayProxy 'node' '--no-warnings proxy.mjs'", src)

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
            capture_output=True, text=True, encoding="utf-8", env=env,
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
            capture_output=True, text=True, encoding="utf-8",
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
            if line.strip() == "foreach ($s in 'LumoGatewayLiteLLM','LumoGatewayProxy') {":
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

            lite_line = self._extract(stdout, "nssm install LumoGatewayLiteLLM")
            args = self._paste_through_stub(temp_dir, lite_line)
            self.assertEqual(args[0], "install")
            self.assertEqual(args[1], "LumoGatewayLiteLLM")
            # Exe path with spaces must arrive as ONE argument.
            self.assertEqual(args[2], os.path.join(state_dir,
                                                    "litellm-venv", "Scripts",
                                                    "litellm.exe"))
            # The whole LiteLLM argument must arrive as ONE argument with the
            # inner double quotes preserved.
            expected_arg = '--config "%s" --host 127.0.0.1 --port 4000' % (
                os.path.join(ROOT, "gateway", "litellm.yaml"))
            self.assertEqual(args[3], expected_arg)

            proxy_line = self._extract(stdout, "nssm install LumoGatewayProxy")
            pargs = self._paste_through_stub(temp_dir, proxy_line)
            self.assertEqual(pargs[0], "install")
            self.assertEqual(pargs[1], "LumoGatewayProxy")
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
                capture_output=True, text=True, encoding="utf-8",
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
            for svc, entry in zip(("LumoGatewayLiteLLM", "LumoGatewayProxy"), newitem):
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
            for svc, entry in zip(("LumoGatewayLiteLLM", "LumoGatewayProxy"), webreq):
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

    def test_direct_winsw_exe_lines_survive_spaced_home(self):
        """The four '& ... WinSW.exe install/start' paste lines run the real
        exe as one executable token when $HOME contains a space (#145).

        A copy of cmd.exe stands in for WinSW.exe at the (spaced) target path.
        With the defect (unquoted path), PowerShell splits the command at the
        first space and nothing under the spaced path runs at all.
        """
        temp_dir = tempfile.mkdtemp(suffix=" spaced direct")
        try:
            home_stub = os.path.join(temp_dir, "home dir with spaces")
            stdout = self._generate(temp_dir, "winsw")
            lines = [line.strip() for line in stdout.splitlines()
                     if "WinSW.exe" in line and line.strip().startswith("&")]
            self.assertEqual(len(lines), 4, "expected 4 paste lines: %r" % lines)

            expected_order = ["install", "install", "start", "start"]
            marker = os.path.join(temp_dir, "hit.txt")
            for action, line in zip(expected_order, lines):
                svc_dir = os.path.join(home_stub, ".lumoservices",
                                       "LumoGatewayLiteLLM" if "LiteLLM" in line
                                       else "LumoGatewayProxy")
                os.makedirs(svc_dir, exist_ok=True)
                stub_cmd = os.path.join(svc_dir, "WinSWStub.exe")
                shutil.copyfile(os.path.join(os.environ["SystemRoot"], "System32",
                                              "cmd.exe"), stub_cmd)
                pasted = (line.replace("$HOME", home_stub)
                              .replace("WinSW.exe", "WinSWStub.exe")
                              .replace(action,
                                       '/c echo RAN:%s >> "%s"'
                                       % (action, marker)))
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", pasted],
                    capture_output=True, text=True, encoding="utf-8",
                )
                self.assertEqual(
                    proc.returncode, 0,
                    "pasted line failed: %r\n%s\n%s" % (pasted, proc.stdout,
                                                        proc.stderr))
                with open(marker, encoding="utf-16") as f:
                    hits = f.read().splitlines()
                self.assertEqual(
                    hits, ["RAN:%s" % action],
                    "expected exactly one %s hit at %r: %r" % (action, svc_dir,
                                                                hits))
                os.remove(marker)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class TestInstallMode(unittest.TestCase):
    """-Install in offline test mode (LUMO_INSTALL_SUPERVISOR_TEST=1): the XMLs
    land in LUMO_SERVICES_DIR with a placeholder key, and the script exits
    before probing ports, downloading WinSW, or touching the service manager
    (#183). Nothing here installs, starts or binds anything live."""

    def _install_offline(self, services_dir, state_dir):
        env = dict(os.environ, LUMO_INSTALL_SUPERVISOR_TEST="1",
                   LUMO_SERVICES_DIR=services_dir,
                   CLAUDE_LUMO_STATE_DIR=state_dir)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", SCRIPT, "-Install"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_offline_install_writes_both_service_xmls(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = os.path.join(tmp, "services")
            stdout = self._install_offline(services, os.path.join(tmp, "state"))
            self.assertIn("stopping before download/install/start", stdout)
            for svc in ("LumoGatewayLiteLLM", "LumoGatewayProxy"):
                path = os.path.join(services, svc, svc + ".xml")
                with open(path, encoding="utf-8-sig") as f:
                    xml = f.read()
                self.assertIn("<id>%s</id>" % svc, xml)
                self.assertIn("<startmode>Automatic</startmode>", xml)
                self.assertIn('<onfailure action="restart"', xml)
                self.assertIn('<log mode="roll-by-size">', xml)
                # No XML, no log line, may carry a real master key.
                self.assertNotIn("PLACEHOLDER-TEST-KEY", stdout)
            # The placeholder key goes into the LiteLLM XML only.
            litellm_xml = os.path.join(services, "LumoGatewayLiteLLM",
                                       "LumoGatewayLiteLLM.xml")
            with open(litellm_xml, encoding="utf-8-sig") as f:
                self.assertIn("PLACEHOLDER-TEST-KEY", f.read())
            proxy_xml = os.path.join(services, "LumoGatewayProxy",
                                     "LumoGatewayProxy.xml")
            with open(proxy_xml, encoding="utf-8-sig") as f:
                self.assertNotIn("LITELLM_MASTER_KEY", f.read())

    def test_offline_install_redirects_profile_and_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = os.path.join(tmp, "services")
            state = os.path.join(tmp, "state dir with spaces")
            self._install_offline(services, state)
            for svc in ("LumoGatewayLiteLLM", "LumoGatewayProxy"):
                path = os.path.join(services, svc, svc + ".xml")
                with open(path, encoding="utf-8-sig") as f:
                    xml = f.read()
                # LocalSystem services get the installing user's profile paths
                # so cookie discovery and os.homedir() still resolve (#183).
                self.assertIn('name="CLAUDE_LUMO_STATE_DIR" value="%s"' % state, xml)
                self.assertIn('name="USERPROFILE"', xml)
                self.assertIn('name="APPDATA"', xml)
                self.assertIn('name="LOCALAPPDATA"', xml)

    def test_offline_install_keeps_working_dirs_inside_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = os.path.join(tmp, "services")
            self._install_offline(services, os.path.join(tmp, "state"))
            proxy_xml = os.path.join(services, "LumoGatewayProxy",
                                     "LumoGatewayProxy.xml")
            with open(proxy_xml, encoding="utf-8-sig") as f:
                xml = f.read()
            # Derived from the script's own location ($PSScriptRoot = ROOT/gateway),
            # so the service keeps running the checkout the script came from (#183).
            expected = os.path.join(ROOT, "gateway")
            self.assertIn("<workingdirectory>%s</workingdirectory>" % expected, xml)

    def test_offline_install_downloads_and_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = os.path.join(tmp, "services")
            stdout = self._install_offline(services, os.path.join(tmp, "state"))
            # Test mode exits before any download, install or start.
            self.assertNotIn("Downloading WinSW", stdout)
            self.assertNotIn("Installing service", stdout)
            self.assertNotIn("Starting service", stdout)
            # No WinSW exe was fetched or copied into the services tree.
            for root, _dirs, files in os.walk(services):
                self.assertNotIn("WinSW.exe", files)

    def test_install_mode_checks_winsw_exit_codes_and_boot_wording(self):
        """-Install checks & $exe exit codes and says "boot", not "logon" (#192).

        The test seam exits before any WinSW call, so the failure paths cannot
        be driven at runtime; these prove the checks and the corrected wording
        are in the -Install flow, next to the calls they guard.
        """
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('& $exe install 2>&1 | Write-Output', src)
        self.assertIn('& $exe start 2>&1 | Write-Output', src)
        # Each call is followed by an exit-code check that names the service
        # and the operation, and exits non-zero before the next step.
        self.assertIn('if ($LASTEXITCODE -ne 0) {', src)
        self.assertGreaterEqual(src.count("if ($LASTEXITCODE -ne 0) {"), 2)
        self.assertIn("WinSW failed to install service $id (exit code $LASTEXITCODE)", src)
        self.assertIn("WinSW failed to start service $id (exit code $LASTEXITCODE)", src)
        # startmode Automatic under LocalSystem means at BOOT (#192).
        self.assertNotIn("next logon", src)
        self.assertIn("they come back at next boot", src)

    def test_install_mode_has_elevation_precheck_before_key_read(self):
        """-Install refuses to run unelevated before reading master.key (#192).

        Unelevated runs cannot be simulated offline (the elevation gate is
        skipped in test mode), so this proves the check exists in the
        -Install flow and sits ahead of the key read.
        """
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn(
            "[Security.Principal.WindowsBuiltInRole]::Administrator", src)
        self.assertIn("ABORT: this PowerShell is not elevated", src)
        gate_pos = src.index("if (-not $elevated) {")
        key_read_pos = src.index("$MasterKeyValue = ")
        self.assertLess(gate_pos, key_read_pos,
                        "elevation gate must run before the key read")


    def test_uninstall_mode_checks_winsw_exit_codes(self):
        """-Uninstall checks & $exe exit codes for stop/uninstall (#196).

        Same seam limitation as #192: -Uninstall talks to the live service
        manager, so the failure paths cannot be driven offline; these prove
        the checks are in the -Uninstall flow, next to the calls they guard.
        """
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        # The -Uninstall block runs before the -Install block; isolate it so
        # the counting below is about uninstall's own checks.
        install_pos = src.index("# --- Install ---")
        uninstall_src = src[:install_pos]
        self.assertIn('& $exe stop 2>&1 | Write-Output', uninstall_src)
        self.assertIn('& $exe uninstall 2>&1 | Write-Output', uninstall_src)
        self.assertGreaterEqual(
            uninstall_src.count("if ($LASTEXITCODE -ne 0) {"), 2)
        self.assertIn("WinSW failed to stop service $id (exit code $LASTEXITCODE)", uninstall_src)
        self.assertIn("WinSW failed to uninstall service $id (exit code $LASTEXITCODE)", uninstall_src)

    def test_uninstall_mode_has_elevation_precheck_before_winsw(self):
        """-Uninstall refuses to run unelevated before invoking WinSW (#196)."""
        with open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        install_pos = src.index("# --- Install ---")
        uninstall_src = src[:install_pos]
        self.assertIn(
            "[Security.Principal.WindowsBuiltInRole]::Administrator", uninstall_src)
        self.assertIn("ABORT: this PowerShell is not elevated", uninstall_src)
        gate_pos = uninstall_src.index("if (-not $elevated) {")
        winsw_pos = uninstall_src.index("& $exe stop")
        self.assertLess(gate_pos, winsw_pos,
                        "elevation gate must run before the first WinSW call")

@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class TestConnectCard(unittest.TestCase):
    """The "Connect any harness" card prints after a successful -Install (and in
    the offline banner mode), giving every fact another tool needs: base URL,
    endpoints, model ids, key file location, api-key.py, GATEWAY.md. The key
    VALUE never appears in the output."""

    def _install_offline(self, services_dir, state_dir):
        env = dict(os.environ, LUMO_INSTALL_SUPERVISOR_TEST="1",
                   LUMO_SERVICES_DIR=services_dir,
                   CLAUDE_LUMO_STATE_DIR=state_dir)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", SCRIPT, "-Install"],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout, env

    def test_install_output_contains_connect_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state dir with spaces")
            services = os.path.join(tmp, "services")
            stdout, _env = self._install_offline(services, state_dir)
            # The card's facts, all on the -Install path (test-mode exit carries
            # the same card as the real success path).
            self.assertIn("Connect any harness", stdout)
            self.assertIn("http://127.0.0.1:4000", stdout)
            self.assertIn("/v1/messages", stdout)
            self.assertIn("/v1/chat/completions", stdout)
            self.assertIn("lumo-max", stdout)
            self.assertIn("lumo-lite", stdout)
            self.assertIn("unknown claude-", stdout)
            self.assertIn("haiku", stdout)
            self.assertIn(os.path.join(state_dir, "master.key"), stdout)
            self.assertIn("api-key.py", stdout)
            self.assertIn("docs\\GATEWAY.md", stdout)

    def test_install_output_never_prints_the_key_value(self):
        """The card names the key FILE; the placeholder key value stays out."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            services = os.path.join(tmp, "services")
            stdout, env = self._install_offline(services, state_dir)
            # Test mode bakes PLACEHOLDER-TEST-KEY into the XML; that value
            # proves what WOULD leak if the card printed the key. Card + banners
            # must never contain it, nor a line of bare key material.
            self.assertNotIn("PLACEHOLDER-TEST-KEY", stdout)
            self.assertNotIn("sk-lumo-", stdout)

    def test_banner_mode_prints_connect_card_too(self):
        """The offline generator (no -Install) shows the same card."""
        env = dict(os.environ, CLAUDE_LUMO_STATE_DIR=os.path.join(
            tempfile.gettempdir(), "lumo-card-banner-state"))
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", SCRIPT],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Connect any harness", proc.stdout)
        self.assertIn("http://127.0.0.1:4000", proc.stdout)


if __name__ == "__main__":
    unittest.main()
