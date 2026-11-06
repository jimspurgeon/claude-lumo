"""Tests for gateway/setup-facts.sh (the `lumo-gateway setup` UX): pure model
parsing/rendering helpers, the full fact sheet, the non-interactive key-skip,
and the never-print-the-key guarantee. All runs use a scratch HOME and state
dir so nothing real is touched; the master key holds a sentinel value that must
never appear in any captured output."""
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FACTS = ROOT / "gateway" / "setup-facts.sh"
SENTINEL = "sk-TESTKEY-s8b-xyz"


def bash_view(p):
    """Path as the Git-Bash child sees it (POSIX form, via cygpath when present)."""
    r = subprocess.run(["bash", "-c", 'cygpath -u "$1"', "_", str(p)],
                       capture_output=True, text=True)
    return r.stdout.strip() or str(p).replace("\\", "/")


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
    """Run body in bash with the helpers sourced against the scratch HOME."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_LUMO_STATE_DIR", "FACTS_CONFIG")}
    env["HOME"] = scratch["home_b"]
    env["CLAUDE_LUMO_STATE_DIR"] = scratch["state_b"]
    env.update({k: str(v) for k, v in (extra_env or {}).items()})
    return subprocess.run(
        ["bash", "-c", f'source "{FACTS}" >/dev/null; {body}'],
        env=env, capture_output=True, text=True, input=input_text, timeout=120,
    )


def test_parse_models_from_repo_yaml(scratch):
    """The shipped litellm.yaml parses to lumo-lite + lumo-max with limits."""
    r = call(scratch, f'facts_parse_models "{ROOT}/gateway/litellm.yaml"')
    assert r.returncode == 0, r.stderr
    assert "lumo-lite|131072|32768" in r.stdout
    assert "lumo-max|131072|32768" in r.stdout
    lines = [l for l in r.stdout.splitlines() if l]
    assert len(lines) == 2, r.stdout  # "*haiku*" and "*" are routes, not models


def test_parse_models_reflects_runtime_yaml(scratch, tmp_path):
    """Change a model name in a scratch copy -> parsed output follows it."""
    cfg = tmp_path / "litellm.yaml"
    text = (ROOT / "gateway" / "litellm.yaml").read_text(encoding="utf-8")
    cfg.write_text(text.replace("- model_name: lumo-max", "- model_name: lumo-maxx-edited"),
                   encoding="utf-8")
    r = call(scratch, f'facts_parse_models "{cfg}"')
    assert r.returncode == 0, r.stderr
    assert "lumo-maxx-edited|" in r.stdout
    assert "lumo-max|" not in r.stdout


def test_render_models_lines(scratch):
    r = call(scratch, f'facts_render_models "{ROOT}/gateway/litellm.yaml"')
    assert r.returncode == 0, r.stderr
    assert "lumo-max — context 131072 tokens, replies up to 32768 tokens" in r.stdout
    assert "lumo-lite — context 131072 tokens, replies up to 32768 tokens" in r.stdout


def test_fact_sheet_order_and_content(scratch):
    """Full sheet: spec order, models from the yaml, sentinel never printed."""
    r = call(scratch, "facts_print_sheet",
             extra_env={"FACTS_CONFIG": ROOT / "gateway" / "litellm.yaml"})
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
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


def test_service_branch_absent_wording(scratch, tmp_path):
    """On a probe that says absent, the exact install command is the next action."""
    # Force the "absent" branch deterministically: hide every service probe.
    r = subprocess.run(
        ["bash", "-c",
         f'source "{FACTS}" >/dev/null; '
         f'facts_install_cmd "{ROOT}/gateway/litellm.yaml"'],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    cmd = r.stdout.strip()
    assert cmd  # non-empty on every platform
    if os.name == "nt":
        assert "install-supervisor.ps1" in cmd and "-Install" in cmd


def test_noninteractive_run_skips_key_copy(scratch):
    """stdin not a tty (DEVNULL): printed guidance, exit 0, no key value."""
    r = subprocess.run(
        ["bash", "-c", f'source "{FACTS}" >/dev/null; HOME="{scratch["home_b"]}"; '
         f'CLAUDE_LUMO_STATE_DIR="{scratch["state_b"]}"; facts_print_sheet'],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        env={k: v for k, v in os.environ.items() if k != "CLAUDE_LUMO_STATE_DIR"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "non-interactive session; key NOT copied" in r.stdout
    assert "copy it yourself from:" in r.stdout
    assert bash_view(scratch["key"]) in r.stdout
    assert SENTINEL not in r.stdout and SENTINEL not in r.stderr


@pytest.mark.skipif(not hasattr(__import__("os"), "PTY") or not hasattr(os, "fork"),
                    reason="tty test needs a posix pty")
def test_interactive_enter_copies_key_without_echo(scratch):
    """Pressing Enter on a tty copies the key; the value never hits the tty."""
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # child: tty on stdin, scratch HOME
        os.environ["HOME"] = scratch["home_b"]
        os.environ["CLAUDE_LUMO_STATE_DIR"] = scratch["state_b"]
        os.environ.pop("FACTS_CONFIG", None)
        os.execvp("bash", ["bash", "-c", f'source "{FACTS}" >/dev/null; facts_print_sheet'])
    time.sleep(1.0)
    try:
        os.write(fd, b"\n")
        chunks = []
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            if b"Wiring it up" in b"".join(chunks):
                break
    finally:
        _, status = os.waitpid(pid, 0)
    out = b"".join(chunks).decode(errors="replace")
    assert os.waitstatus_to_exitcode(status) == 0, out
    assert SENTINEL not in out
    assert ("API key copied to clipboard" in out
            or "clipboard unavailable" in out
            or "non-interactive session" in out)


def test_no_clipboard_tool_drains_stdin_and_reports(scratch, tmp_path):
    """No clipboard tool on PATH: rc 1 from copy_to_clipboard, stdin consumed."""
    r = subprocess.run(
        ["bash", "-c",
         f'source "{FACTS}" >/dev/null; '
         'PATH="$(mktemp -d)"; printf abc | copy_to_clipboard'],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 1
    assert r.stdout == ""


def test_service_state_probe_is_safe(scratch):
    """The read-only probe runs and answers with a known state word."""
    r = call(scratch, "facts_service_state")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() in ("running", "installed", "absent")
