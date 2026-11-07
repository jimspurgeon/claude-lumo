"""Unit tests for install.sh's Linux helpers and body snippets (#163): as_root's
elevation branches, suCommand's quoting, pkg_install, node_install_linux, the Python
3.12 gate, the CRLF strip and the PATH prepend. Mirrors tests/test_install.py's style.

The helpers live inside install.sh, which is not a library (sourcing it would run an
install), so the tests extract the `=== Linux helper functions ===` block — and the
gate/CRLF/PATH snippets verbatim from the installer's body — into temp scripts and run
those. A change to install.sh therefore shows up here as a failure.

Cross-platform notes learned the hard way (see tests/test_start_lock.py):
- On Windows the child bash must be Git's usr/bin/bash and PATH entries must be POSIX
  (/c/...): Git/bin/bash re-prepends /mingw64/bin, shadowing the fakes with real tools.
- MSYS GNU grep 3.0 does not see CR bytes that a file demonstrably contains, so the
  CRLF-strip test would be unreliable on this host; it runs on POSIX CI and skips here.
"""
import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = platform.system() == "Windows"
BASH = shutil.which("bash") or "bash"
if WINDOWS:
    _usr = BASH.replace("Git\\bin\\bash", "Git\\usr\\bin\\bash")
    if os.path.exists(_usr):
        BASH = _usr

INSTALL_TEXT = (ROOT / "install.sh").read_text(encoding="utf-8")


def to_posix(path):
    if not WINDOWS:
        return str(path)
    return subprocess.run([BASH, "-c", 'cygpath -u "$1"', "_", str(path)],
                          capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout.strip()


def _posix_system_path():
    return subprocess.run([BASH, "-c", "echo $PATH"], capture_output=True, text=True,
                          encoding="utf-8", timeout=30).stdout.strip()


def extract(start_pred, end_pred):
    """Verbatim block from install.sh: the first line matching start_pred through the
    first following line matching end_pred, inclusive."""
    lines = INSTALL_TEXT.splitlines()
    s = next((i for i, l in enumerate(lines) if start_pred(l)), None)
    assert s is not None, "start marker not found in install.sh"
    for j in range(s, len(lines)):
        if end_pred(lines[j]):
            return "\n".join(lines[s:j + 1]) + "\n"
    raise AssertionError("end marker not found in install.sh")


def helper_script():
    """shellQuote .. node_install_linux: the helper block, stopping before the `====`
    terminator comment (the block is comment-led, not code-led)."""
    lines = INSTALL_TEXT.splitlines()
    s = next(i for i, l in enumerate(lines) if l.startswith("# === Linux helper functions"))
    e = next(j for j in range(s + 1, len(lines))
             if lines[j].startswith("# ===") and not lines[j].startswith("# === Linux"))
    return "\n".join(lines[s:e]) + "\n"


@pytest.fixture
def sb(tmp_path):
    for name in ("bin", "state", "home", "tmp"):
        (tmp_path / name).mkdir()
    (tmp_path / "helpers.sh").write_text(helper_script(), encoding="utf-8", newline="\n")
    box = {"tmp": tmp_path, "bin": tmp_path / "bin", "home": tmp_path / "home",
           "state": tmp_path / "state", "helpers": tmp_path / "helpers.sh"}
    # as_root's default assumption away from real root: uid 1000, recording sudo on PATH.
    # Records the ORIGINAL argv (no shift) then runs it, so every elevated call both
    # lands in sudo-calls and reaches the fake package manager underneath.
    fake(box, "id", "#!/bin/sh\necho 1000\n")
    fake(box, "sudo",
         '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$STATE/sudo-calls"\nexec "$@"\n')
    return box


def fake(sb, name, body):
    f = sb["bin"] / name
    f.write_text(body, encoding="utf-8", newline="\n")
    f.chmod(0o755)
    return f


def run_snippet(sb, code, minimal_path=False):
    """Write code to a script (no -c quoting traps), source the helpers, run under BASH
    with the fakes first on a POSIX-format PATH."""
    script = sb["tmp"] / "snippet.sh"
    script.write_text(f'. "{to_posix(sb["helpers"])}"\n' + code,
                      encoding="utf-8", newline="\n")
    fake_bin = to_posix(sb["bin"])
    path = f"{fake_bin}:/usr/bin" if minimal_path else f"{fake_bin}:{_posix_system_path()}"
    env = dict(os.environ, HOME=to_posix(sb["home"]), STATE=to_posix(sb["state"]),
               PATH=path)
    return subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120, stdin=subprocess.DEVNULL)


def run_standalone(sb, code):
    """Run an extracted body block without sourcing the helpers."""
    script = sb["tmp"] / "block.sh"
    script.write_text(code, encoding="utf-8", newline="\n")
    env = dict(os.environ, HOME=to_posix(sb["home"]), STATE=to_posix(sb["state"]),
               PATH=f"{to_posix(sb['bin'])}:{_posix_system_path()}")
    return subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120, stdin=subprocess.DEVNULL)


def calls(sb, name):
    p = sb["state"] / f"{name}-calls"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def fake_pm(sb, name, extra=""):
    fake(sb, name, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$STATE/pm-calls"\n' + extra)


class TestAsRoot:
    def test_sudo_branch_invokes_command_under_sudo(self, sb):
        fake_pm(sb, "dnf")
        r = run_snippet(sb, "as_root dnf install -y git")
        assert r.returncode == 0, r.stdout + r.stderr
        assert any("dnf install -y git" in c for c in calls(sb, "sudo"))
        assert any("install -y git" in c for c in calls(sb, "pm"))

    def test_uid_zero_runs_directly_without_elevation(self, sb):
        fake(sb, "id", "#!/bin/sh\necho 0\n")
        fake(sb, "dangerous-tool",
             "#!/bin/sh\nprintf \'%s\\n\' \"$*\" > \"$STATE/direct-calls\"\n")
        r = run_snippet(sb, 'as_root dangerous-tool one "two words"')
        assert r.returncode == 0, r.stdout + r.stderr
        assert calls(sb, "direct") == ["one two words"]
        assert calls(sb, "sudo") == []

    def test_su_branch_used_when_no_sudo(self, sb):
        fake(sb, "su", "#!/bin/sh\n[ \"$1\" = \"-c\" ] || exit 8\n"
                      "printf '[%s]\\n' \"$2\" > \"$STATE/su-calls\"\n")
        probe = run_snippet(sb, "command -v sudo >/dev/null && echo HAVE || echo NO",
                            minimal_path=True)
        if "HAVE" in probe.stdout:
            pytest.skip("real sudo on the minimal PATH: su branch unreachable")
        r = run_snippet(sb, "as_root dnf install -y git", minimal_path=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert calls(sb, "su") == ["'dnf' 'install' '-y' 'git' ]"]

    def test_stdin_closed_and_debian_front_end(self, sb):
        fake(sb, "sudo",
             "#!/bin/sh\nenv | grep -q '^DEBIAN_FRONTEND=noninteractive$' "
             "&& echo ok > \"$STATE/env-ok\"\n"
             "read -r _l && echo \"LEAKED: $_l\" > \"$STATE/stdin-leak\"\nexit 0\n")
        r = run_snippet(sb, "echo SENTINEL | as_root true")
        assert r.returncode == 0, r.stdout + r.stderr
        assert (sb["state"] / "env-ok").exists()
        assert not (sb["state"] / "stdin-leak").exists()


class TestSuCommand:
    def test_output_is_shellQuoted_per_argument(self, sb):
        """The #163 bug: `su -c "$*"` flattened argv so spaces/quotes in an argument
        re-split. Assert the produced string directly: every argument appears single-
        quoted POSIX-style (a lone ' doubled to '"'"'), separated by single spaces.
        No round-trip through MSYS quoting — that proved unreliable on this host."""
        r = run_snippet(sb, 'suCommand dnf install -y "pak gage" "it\'s" '
                            "'$(echo meta)' 'a\\b' > \"$STATE/su-cmd\"")
        assert r.returncode == 0, r.stdout + r.stderr
        cmd = (sb["state"] / "su-cmd").read_text(encoding="utf-8")
        expected = "'dnf' 'install' '-y' 'pak gage' 'it'\"'\"'s' '$(echo meta)' 'a\\b' "
        assert cmd == expected, (cmd, expected)


class TestPkgInstall:
    @pytest.mark.parametrize("pm,tools,pythons", [
        ("apt-get", ["update -qq", "install -y git ca-certificates"],
         ["install -y python3 python3-venv python3-pip"]),
        ("dnf", ["install -y git"], ["install -y python3 python3-pip"]),
        ("yum", ["install -y git"], ["install -y python3 python3-pip"]),
        ("pacman", ["-Sy --noconfirm --needed git"],
         ["-Sy --noconfirm --needed python"]),
        ("zypper", ["-n install git"],
         ["-n install python3 python3-pip"]),
        ("apk", ["add git"], ["add python3 py3-pip"]),
    ])
    def test_each_manager_installs_distro_names(self, sb, pm, tools, pythons):
        fake_pm(sb, pm)
        r = run_snippet(sb, f"PM={pm}; pkg_install tools && pkg_install python")
        assert r.returncode == 0, r.stdout + r.stderr
        got = calls(sb, "pm")
        for want in tools + pythons:
            assert want in got, (want, got)

    def test_unknown_manager_fails_the_group(self, sb):
        r = run_snippet(sb, 'PM=""; pkg_install tools; echo "rc=$?"')
        assert "rc=1" in r.stdout

    def test_package_failure_propagates(self, sb):
        fake_pm(sb, "dnf", extra="exit 3\n")
        r = run_snippet(sb, 'PM=dnf; pkg_install tools; echo "rc=$?"')
        assert "rc=1" in r.stdout


class TestNodeInstallLinux:
    def test_pacman_installs_nodejs(self, sb):
        fake_pm(sb, "pacman")
        r = run_snippet(sb, "PM=pacman; node_install_linux; echo \"rc=$?\"")
        assert "rc=0" in r.stdout, r.stdout + r.stderr
        assert "-Sy --noconfirm --needed nodejs" in calls(sb, "pm")

    def test_zypper_falls_back_from_nodejs20_to_nodejs(self, sb):
        fake_pm(sb, "zypper",
                extra='[ "$3" = "nodejs20" ] && exit 1\nexit 0\n')
        r = run_snippet(sb, "PM=zypper; node_install_linux; echo \"rc=$?\"")
        assert "rc=0" in r.stdout, r.stdout + r.stderr
        got = calls(sb, "pm")
        assert "-n install nodejs20" in got
        assert "-n install nodejs" in got

    def test_dnf_downloads_nodesource_then_installs(self, sb):
        fake_pm(sb, "dnf")
        fake(sb, "curl",
             "#!/bin/sh\nprintf '[%s]\\n' \"$2\" > \"$STATE/curl-calls\"\n"
             "_last=\"\"\nfor _a in \"$@\"; do _last=\"$_a\"; done\n"
             "printf '#!/bin/sh\\n' > \"$_last\"\nexit 0\n")
        fake(sb, "bash",
             "#!/bin/sh\nprintf '[%s]\\n' \"$*\" > \"$STATE/bash-calls\"\nexit 0\n")
        r = run_snippet(sb, "PM=dnf; node_install_linux; echo \"rc=$?\"")
        assert "rc=0" in r.stdout, r.stdout + r.stderr
        assert any("rpm.nodesource.com/setup_22.x" in c for c in calls(sb, "curl"))
        assert calls(sb, "bash"), "nodesource setup never ran"
        assert "install -y nodejs" in calls(sb, "pm")

    def test_unsupported_pm_fails_cleanly(self, sb):
        r = run_snippet(sb, 'PM=""; node_install_linux; echo "rc=$?"')
        assert "rc=1" in r.stdout


class TestPythonGate:
    """The 3.12+ version gate, extracted verbatim from install.sh's prerequisite block."""

    @staticmethod
    def gate():
        # The gate's own closing "  fi" (indented); the extracted block stays balanced.
        return extract(lambda l: 'PYVER="$("' in l, lambda l: l == "  fi")

    def _gate_with(self, sb, ver):
        fake(sb, "python3", f"#!/bin/sh\necho {ver}\n")
        fake(sb, "python", f"#!/bin/sh\necho {ver}\n")
        code = self.gate().replace('"$PY" -c', '"python3" -c')
        return run_standalone(sb, code)

    def test_python_311_is_rejected(self, sb):
        r = self._gate_with(sb, "3.11")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "ERROR: Python 3.11" in r.stderr

    def test_python_312_passes(self, sb):
        r = self._gate_with(sb, "3.12")
        assert r.returncode == 0, r.stdout + r.stderr

    def test_missing_python_reports_the_gate(self, sb):
        # No python anywhere: `|| echo 0` fires, PYVER=0, and 0 < 3 rejects.
        code = self.gate().replace('"$PY" -c', '"definitely-not-python-xyz" -c')
        r = run_standalone(sb, code)
        assert r.returncode == 1, r.stdout + r.stderr


class TestCrlfStrip:
    @pytest.mark.skipif(WINDOWS, reason="MSYS GNU grep 3.0 cannot see CR bytes in this "
                                        "build, so grep -q $'\\r' never fires here; "
                                        "runs on POSIX CI")
    def test_cr_stripped_from_sh_and_lumo_gateway(self, sb):
        loop = extract(lambda l: l.startswith('for f in "$D"'), lambda l: l == "done")
        d = sb["home"] / "d"
        (d / "scripts").mkdir(parents=True)
        (d / "clean.sh").write_bytes(b"#!/bin/sh\nok\n")
        (d / "dirty.sh").write_bytes(b"#!/bin/sh\r\nok\r\n")
        (d / "scripts" / "s.sh").write_bytes(b"#!/bin/sh\r\nok\r\n")
        (d / "lumo-gateway").write_bytes(b"#!/bin/sh\r\nok\r\n")
        (d / "ignored.py").write_bytes(b"x = 1\r\n")  # outside the glob: untouched
        r = run_standalone(sb, f'D="{to_posix(d)}"\n' + loop)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (d / "clean.sh").read_bytes() == b"#!/bin/sh\nok\n"
        assert (d / "dirty.sh").read_bytes() == b"#!/bin/sh\nok\n"
        assert (d / "scripts" / "s.sh").read_bytes() == b"#!/bin/sh\nok\n"
        assert (d / "lumo-gateway").read_bytes() == b"#!/bin/sh\nok\n"
        assert (d / "ignored.py").read_bytes() == b"x = 1\r\n"
        assert "Fixed line endings" in r.stdout


class TestPathPrepend:
    def test_bin_prepended_once_not_duplicated(self, sb):
        # The guard is one line (case ... esac on the same line); extract needs a
        # same-line end predicate.
        guard = extract(lambda l: l.startswith('case ":$PATH:"'),
                        lambda l: l.rstrip().endswith("esac"))
        assert guard.strip().count("\n") == 0  # the guard is a single case..esac line
        code = (f'BIN="$HOME/.local/bin"\n{guard}\n{guard}\n'
                'case ":$PATH:" in *":$BIN:$BIN:"*) echo DUPLICATED;; esac\n'
                'case "$PATH" in "$BIN":*) echo FIRST;; esac\n')
        r = run_standalone(sb, code)
        assert "DUPLICATED" not in r.stdout, r.stdout
        assert "FIRST" in r.stdout, r.stdout
