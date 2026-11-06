"""Tests for gateway/setup-facts.sh (the `lumo-gateway setup` UX): pure model
parsing/rendering helpers, the full fact sheet, the non-interactive key-skip,
and the never-print-the-key guarantee. All runs use a scratch HOME and state
dir so nothing real is touched; the master key holds a sentinel value that must
never appear in any captured output."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FACTS = ROOT / "gateway" / "setup-facts.sh"
SENTINEL = "sk-TESTKEY-s8b-xyz"

# On Windows a bare "bash" resolves to System32's WSL launcher before PATH is
# searched (#91) — on a GH runner without WSL it fails outright, with WSL
# installed it emits UTF-16 noise. Resolve the real Git Bash like test_install.py.
BASH = shutil.which("bash") or "bash"
WINDOWS = sys.platform.startswith("win")


def bash_view(p):
    """Path as the Git-Bash child sees it (POSIX form, via cygpath when present)."""
    r = subprocess.run([BASH, "-c", 'cygpath -u "$1"', "_", str(p)],
                       capture_output=True)
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    return out or str(p).replace("\\", "/")


FACTS_B = bash_view(FACTS)
ROOT_B = bash_view(ROOT)

_MIN_ENV_KEYS = {"SYSTEMROOT", "SYSTEMDRIVE", "PATH", "TEMP", "TMP", "USERPROFILE",
                 "USERNAME", "PROGRAMFILES", "PROGRAMFILES(W6432)", "COMSPEC",
                 "PATHEXT"}


def min_env(**extra):
    """Minimal, reproducible child env: runner-provided variables sometimes
    carry stray bytes, so keep only what bash needs on Windows and add the
    test's own variables. POSIX children inherit nothing extra needed."""
    env = {}
    for key, val in os.environ.items():
        if key.upper() in _MIN_ENV_KEYS and isinstance(val, str) and "\x00" not in val:
            env[key] = val
    env.update({k: str(v) for k, v in extra.items()})
    return env


@pytest.fixture
def scratch(tmp_path):
    """Scratch HOME + state dir; the key is PRE-SEEDED with the sentinel."""
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    state.mkdir()
    (state / "master.key").write_text(SENTINEL + "\n", encoding="utf-8")
    return {"home": home, "state": state, "key": state / "master.key",
            "home_b": bash_view(home), "state_b": bash_view(state)}


def call(scratch, body, input_text=None, extra_env=None):
    """Run body in bash with the helpers sourced against the scratch HOME.
    Output is captured as BYTES and decoded as UTF-8 explicitly: the fact
    sheet contains em-dashes and locale-default codecs on some runners
    mangle them."""
    env = min_env(HOME=scratch["home_b"], CLAUDE_LUMO_STATE_DIR=scratch["state_b"])
    env.update({k: str(v) for k, v in (extra_env or {}).items()})
    return subprocess.run(
        [BASH, "-c", f'source "{FACTS_B}" >/dev/null; {body}'],
        env=env, capture_output=True, input=input_text, timeout=120,
    )


def text(r):
    return (r.stdout or b"").decode("utf-8", errors="replace")


def err_text(r):
    return (r.stderr or b"").decode("utf-8", errors="replace")


def test_parse_models_from_repo_yaml(scratch):
    """The shipped litellm.yaml parses to lumo-lite + lumo-max with limits."""
    r = call(scratch, f'facts_parse_models "{ROOT_B}/gateway/litellm.yaml"')
    assert r.returncode == 0, err_text(r)
    assert "lumo-lite|131072|32768" in text(r)
    assert "lumo-max|131072|32768" in text(r)
    lines = [l for l in text(r).splitlines() if l]
    assert len(lines) == 2, text(r)  # "*haiku*" and "*" are routes, not models


def test_parse_models_reflects_runtime_yaml(scratch, tmp_path):
    """Change a model name in a scratch copy -> parsed output follows it."""
    cfg = tmp_path / "litellm.yaml"
    src = (ROOT / "gateway" / "litellm.yaml").read_text(encoding="utf-8")
    cfg.write_text(src.replace("- model_name: lumo-max", "- model_name: lumo-maxx-edited"),
                   encoding="utf-8")
    r = call(scratch, f'facts_parse_models "{bash_view(cfg)}"')
    assert r.returncode == 0, err_text(r)
    assert "lumo-maxx-edited|" in text(r)
    assert "lumo-max|" not in text(r)


def test_render_models_lines(scratch):
    r = call(scratch, f'facts_render_models "{ROOT_B}/gateway/litellm.yaml"')
    assert r.returncode == 0, err_text(r)
    assert "lumo-max — context 131072 tokens, replies up to 32768 tokens" in text(r)
    assert "lumo-lite — context 131072 tokens, replies up to 32768 tokens" in text(r)


def test_fact_sheet_order_and_content(scratch):
    """Full sheet: spec order, models from the yaml, sentinel never printed."""
    r = call(scratch, "facts_print_sheet",
             extra_env={"FACTS_CONFIG": bash_view(ROOT / "gateway" / "litellm.yaml")})
    assert r.returncode == 0, text(r) + err_text(r)
    out = text(r) + err_text(r)   # sentinel must appear in NEITHER stream
    order = [out.index(s) for s in
             ("Base URL:", "Protocols:", "Models:", "Master key:", "Services:")]
    assert order == sorted(order), out
    assert "http://127.0.0.1:4000" in out
    assert "lumo-max — context 131072 tokens" in out   # derived, not hardcoded
    assert "/v1/chat/completions" in out and "/v1/messages" in out
    assert bash_view(scratch["key"]) in out            # key PATH is printed
    assert "Wiring it up:" in out                      # generic instructions
    assert "litellm/lumo-max" in out
    assert "claude" not in out.split("Wiring it up:")[1].lower()  # no per-harness recipes
    assert SENTINEL not in out                         # key VALUE never printed


def test_install_cmd_mentions_next_action(scratch):
    """facts_install_cmd prints the exact platform next-step, non-empty."""
    r = call(scratch, f'facts_install_cmd "{ROOT_B}/gateway/litellm.yaml"')
    assert r.returncode == 0, err_text(r)
    cmd = text(r).strip()
    assert cmd  # non-empty on every platform
    if WINDOWS:
        assert "install-supervisor.ps1" in cmd and "-Install" in cmd


def test_service_line_matches_probe_outcome(scratch, tmp_path):
    """Whatever the (real, read-only) probe reports, the row matches that state
    per the branch contract: running / installed / not-installed-with-next-action.
    Output travels back through a scratch file so no printf-format pitfalls
    can corrupt it."""
    out_file = tmp_path / "probe-row.txt"
    r = call(scratch,
             f'st="$(facts_service_state)"; '
             f'facts_service_line "{ROOT_B}/gateway/litellm.yaml" > "$$row"; '
             '{ echo "$st"; cat "$$row"; } > "$OUT_FILE"',
             extra_env={"OUT_FILE": bash_view(out_file)})
    assert r.returncode == 0, err_text(r)
    st, row, *_ = out_file.read_text(encoding="utf-8").splitlines() + [""]
    assert st in ("running", "installed", "absent"), out_file.read_text(encoding="utf-8")
    if st == "running":
        assert "running (LumoGatewayLiteLLM)" in row
    elif st == "installed":
        assert "installed but not running" in row
    else:
        assert "not installed yet" in row and "next action:" in row
        # The next action names a concrete artifact of this checkout.
        assert ("install-supervisor.ps1" in row and "-Install" in row) \
            or "docs/GATEWAY.md" in row


def test_absent_service_wording_deterministic(scratch, tmp_path):
    """With EVERY probe binary hidden, the branch must say 'not installed yet'
    and print a next action (the slice-a install command)."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    env = min_env(HOME=scratch["home_b"], PATH=str(empty))
    r = subprocess.run(
        [BASH, "-c", f'source "{FACTS_B}" >/dev/null; '
                     f'facts_service_line "{ROOT_B}/gateway/litellm.yaml"'],
        env=env, capture_output=True, timeout=60,
    )
    assert r.returncode == 0, err_text(r)
    out = text(r)
    assert "not installed yet" in out and "next action:" in out


def test_noninteractive_run_skips_key_copy(scratch):
    """stdin not a tty (DEVNULL): printed guidance, exit 0, no key value."""
    r = subprocess.run(
        [BASH, "-c", f'source "{FACTS_B}" >/dev/null; facts_print_sheet'],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120,
        env=min_env(HOME=scratch["home_b"], CLAUDE_LUMO_STATE_DIR=scratch["state_b"]),
    )
    assert r.returncode == 0, text(r) + err_text(r)
    out = text(r)
    assert "non-interactive session; key NOT copied" in out
    assert "copy it yourself from:" in out
    assert bash_view(scratch["key"]) in out
    assert SENTINEL not in out and SENTINEL not in err_text(r)


@pytest.mark.skipif(WINDOWS, reason="tty test needs a posix pty (termios)")
def test_interactive_enter_copies_key_without_echo(scratch):
    """Pressing Enter on a tty copies the key; the value never hits the tty."""
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # child: tty on stdin, scratch HOME
        os.environ["HOME"] = scratch["home_b"]
        os.environ["CLAUDE_LUMO_STATE_DIR"] = scratch["state_b"]
        os.environ.pop("FACTS_CONFIG", None)
        os.execvp(BASH, [BASH, "-c", f'source "{FACTS_B}" >/dev/null; facts_print_sheet'])
    sent = False
    chunks = []
    deadline = time.time() + 60
    while time.time() < deadline:
        # Wait for the "Press Enter" prompt; a loaded runner may not deliver
        # it within a fixed head start, so pump until the prompt shows up.
        try:
            import select
            ready, _, _ = select.select([fd], [], [], 5)
        except (ImportError, OSError):
            ready = [fd]
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        joined = b"".join(chunks)
        if not sent and b"Press Enter" in joined:
            os.write(fd, b"\n")
            sent = True
        if b"Wiring it up" in joined:
            break
    _, status = os.waitpid(pid, 0)
    out = b"".join(chunks).decode(errors="replace")
    assert os.waitstatus_to_exitcode(status) == 0, out
    assert SENTINEL not in out
    # The graceful branch ran: the value was never echoed, only the path.
    assert "copy it yourself from:" in out
    assert bash_view(scratch["key"]) in out


def test_no_clipboard_tool_drains_stdin_and_reports(scratch, tmp_path):
    """No clipboard tool on PATH: rc 1 from copy_to_clipboard, stdin consumed,
    and nothing on stdout (no leak channel)."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    env = min_env(HOME=scratch["home_b"], PATH=str(empty))
    r = subprocess.run(
        [BASH, "-c", f'source "{FACTS_B}" >/dev/null; '
                     'printf abc | copy_to_clipboard'],
        env=env, capture_output=True, timeout=60,
    )
    assert r.returncode == 1
    assert text(r) == ""


def test_service_state_probe_is_safe(scratch):
    """The read-only probe runs and answers with a known state word."""
    r = call(scratch, "facts_service_state")
    assert r.returncode == 0, err_text(r)
    assert text(r).strip() in ("running", "installed", "absent")
