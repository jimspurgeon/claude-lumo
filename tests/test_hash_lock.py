"""Tests for the hash-locked requirements.txt and the Node engines pin (#135)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQ = ROOT / "requirements.txt"
PKG = ROOT / "package.json"
INSTALL_SH = ROOT / "install.sh"


def parse_pins():
    """Yield (pin_line, [continuation lines]) for every requirement stanza in requirements.txt."""
    stanzas, current = [], None
    for raw in REQ.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if current is None:
            current = [line]
            stanzas.append(current)
        else:
            current.append(line)
        # A stanza ends on a line with no trailing backslash.
        if not line.endswith("\\"):
            current = None
    return stanzas


def test_requirements_are_hash_locked():
    """Every requirement pin in requirements.txt carries at least one sha256 hash (#135)."""
    stanzas = parse_pins()
    assert stanzas, "no requirement pins found in requirements.txt"
    offenders = []
    for stanza in stanzas:
        head = stanza[0].split()[0]
        if "==" not in head:
            continue  # e.g. a bare option line; not expected but not a pin
        if not any(l.strip().startswith("--hash=sha256:") for l in stanza[1:]):
            offenders.append(head)
    assert not offenders, f"pins without a sha256 hash: {offenders}"


def test_requirements_litellm_pinned():
    """The top-level litellm[proxy] pin survives hash-locking (#135)."""
    text = REQ.read_text(encoding="utf-8")
    assert re.search(r"^litellm\[proxy\]==1\.104\.2\s*\\", text, re.MULTILINE), (
        "litellm[proxy]==1.104.2 pin missing from requirements.txt"
    )


def test_package_json_engines_parses():
    """package.json is minimal and engines.node covers the tested majors (22 and 24)."""
    pkg = json.loads(PKG.read_text(encoding="utf-8"))
    assert set(pkg.keys()) == {"engines"}, f"unexpected keys: {sorted(pkg.keys())}"
    node = pkg["engines"].get("node")
    m = re.fullmatch(r">=(\d+)\.(\d+)\s*<(\d+)", node or "")
    assert m, f"engines.node missing or not in '>=X.Y <Z' form: {node!r}"
    lo_major, lo_minor, hi_excl = (int(g) for g in m.groups())
    assert (lo_major, lo_minor, hi_excl) == (22, 13, 25), (
        f"engines.node {node!r} does not cover exactly majors 22.13+ through 24"
    )


def test_install_sh_uses_require_hashes_and_warns():
    """install.sh installs with --require-hashes and warns outside the engines range (#135)."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert re.search(r"pip install[^&|;]*--require-hashes[^&|;]*-r \"\$D/requirements\.txt\"", sh), (
        "--require-hashes missing from the requirements.txt install command"
    )
    assert re.search(r"WARNING.*outside the engines range", sh), (
        "no non-fatal Node engines warning in install.sh"
    )
