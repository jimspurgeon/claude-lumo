"""Guard test (#183): no tracked file may introduce a new stale reference to
the old proxy/ directory. The gateway (LiteLLM + proxy stack) now lives in
gateway/; the exceptions below are deliberately kept for legacy compatibility
or because they are historical.

Stdlib only; no third-party deps (#78)."""

import os
import re
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A stale reference is "proxy/" followed by a filename (proxy/proxy.mjs,
# proxy/debug/, proxy/litellm.yaml, ...).
STALE_DIR_REF = re.compile(r"\bproxy/[\w.-]+")

# Files entirely exempt: historical text (release notes), the litellm[proxy]
# pins (a package extra, not a directory) and the AGPL's "a proxy" clause.
# This test's own file is exempt too: its own comments and whitelist literals
# mention the very paths it guards against.
EXEMPT_FILE_PREFIXES = (
    "docs/release-notes", "requirements", "LICENSE", "tests/test_gateway_paths.py",
)

# Lines still allowed to mention proxy/<something>, per file, with the reason:
#  - uninstall.sh: pkill the old-layout processes too (#183 keeps legacy).
#  - wipe.py / tests/test_wipe.py: the #134-era repo spill dirs (proxy/debug,
#    now also gateway/debug) are still collected by wipe.
#  - README.md: the historical "Migration" paragraph only.
#  - .gitignore: old clones may still hold a repo-relative proxy/debug/ spill.
ALLOWED_LINES = {
    "uninstall.sh": ('pkill -f "claude.lumo/proxy/proxy.mjs"',
                     "# Stops the local proxy/LiteLLM"),
    "wipe.py": ("repo-relative proxy/debug/",
                "repo's proxy/debug/"),
    "tests/test_wipe.py": ("proxy/debug/ and gateway/debug/",
                           'tmp_path / "repo" / "proxy" / "debug"'),
    "README.md": ("**Migration.**", "`proxy/debug/` dumps",
                  "`proxy/proxy.log` and `proxy.log.1` are not"),
    ".gitignore": ("proxy/debug/",),
}

def tracked_files():
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return [l for l in out.splitlines() if l.strip()]

class TestNoStaleProxyDirRefs(unittest.TestCase):
    def test_no_new_stale_proxy_directory_references(self):
        offenders = []
        for path in tracked_files():
            if path.startswith(EXEMPT_FILE_PREFIXES):
                continue
            full = os.path.join(REPO_ROOT, path)
            if not os.path.isfile(full):
                continue
            try:
                text = open(full, "rb").read().decode("utf-8")
            except UnicodeDecodeError:
                continue
            allowed = ALLOWED_LINES.get(path, ())
            for i, line in enumerate(text.splitlines(), 1):
                if not STALE_DIR_REF.search(line):
                    continue
                if any(a in line for a in allowed):
                    continue
                offenders.append("%s:%d: %s" % (path, i, line.strip()))
        self.assertEqual(
            [], offenders,
            "Stale proxy/ directory references (the component now lives in gateway/):\n"
            + "\n".join(offenders))

if __name__ == "__main__":
    unittest.main()
