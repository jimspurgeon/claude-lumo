"""CLAUDE_LUMO_KEEP_CHECKOUT=1 keeps the release that update or revert checked out (#100)."""
import os
import subprocess

from test_revert import BASH, HERE, Fixture

# Records whether the caller asked install.sh to keep the checkout.
RECORDING_INSTALLER = """#!/bin/bash
echo "keep=${CLAUDE_LUMO_KEEP_CHECKOUT:-unset}" > "$CLAUDE_LUMO_STATE_DIR/installer-env.txt"
"""


def checkout_block():
    """install.sh's git block (from the channel setting to its closing fi), run on its own."""
    text = open(os.path.join(HERE, "install.sh"), encoding="utf-8").read().replace("\r\n", "\n")
    start = text.index('CHANNEL="${CLAUDE_LUMO_CHANNEL')
    return text[start:text.index("\nfi\n", start) + 4]


def run_block(clone, keep):
    env = dict(os.environ)
    env.pop("CLAUDE_LUMO_CHANNEL", None)
    if keep:
        env["CLAUDE_LUMO_KEEP_CHECKOUT"] = "1"
    else:
        env.pop("CLAUDE_LUMO_KEEP_CHECKOUT", None)
    script = 'D="$1"; SRC="$1"\n' + checkout_block()
    subprocess.run([BASH, "-c", script, "_", clone.replace("\\", "/")], env=env,
                   capture_output=True, timeout=60, check=True)


def test_install_keeps_an_older_checkout_only_when_asked():
    fx = Fixture(checkout="v1.3.0")
    try:
        run_block(fx.clone_dir, keep=True)
        assert fx.clone_tag() == "v1.3.0"
        run_block(fx.clone_dir, keep=False)
        assert fx.clone_tag() == "v1.3.1"
    finally:
        fx.cleanup()


def test_update_and_revert_ask_the_installer_to_keep_the_checkout():
    fx = Fixture(checkout="v1.3.0", installer=RECORDING_INSTALLER)
    try:
        env = dict(os.environ, CLAUDE_LUMO_DIR=fx.clone_dir, CLAUDE_LUMO_STATE_DIR=fx.state_dir)
        record = os.path.join(fx.state_dir, "installer-env.txt")
        subprocess.run([BASH, os.path.join(HERE, "update-command.sh")], env=env,
                       capture_output=True, timeout=60, stdin=subprocess.DEVNULL, check=True)
        assert fx.clone_tag() == "v1.3.1"
        assert open(record).read().strip() == "keep=1"
        os.remove(record)
        code, out = fx.run_revert("v1.3.0")
        assert code == 0, out
        assert open(record).read().strip() == "keep=1"
    finally:
        fx.cleanup()
