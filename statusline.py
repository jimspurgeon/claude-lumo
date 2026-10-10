"""Status line: shows which model and backend this Claude Code session is on.

If the personal additions folder (CLAUDE_LUMO_LOCAL, default ~/.claude/lumo-local) has a
statusline-next.sh, it is also run with the same status JSON, for tools that read the status line
themselves (for example Orca); its output is ignored.
"""
import json, os, subprocess, sys
raw = sys.stdin.buffer.read()
try:
    d = json.loads(raw or b"{}")
except ValueError:
    d = {}
m = d.get("model") or {}
name = m.get("display_name") or m.get("id") or "?"
pct = ""
cw = d.get("context_window") or {}
if isinstance(cw, dict) and cw.get("used_percentage") is not None:
    pct = f"  ctx {cw['used_percentage']:.0f}%"
if os.environ.get("CLAUDE_LUMO") == "1":
    sys.stdout.write(f"\x1b[1;35m[LUMO]\x1b[0m {m.get('id') or name} via local proxy{pct}  (Claude: run `claude`)")
else:
    sys.stdout.write(f"\x1b[1;33m[CLAUDE]\x1b[0m {name}{pct}  (Lumo: run `claude-lumo`)")
sys.stdout.flush()

local = os.environ.get("CLAUDE_LUMO_LOCAL") or os.path.join(os.path.expanduser("~"), ".claude", "lumo-local")
nxt = os.path.join(local, "statusline-next.sh")
if os.path.isfile(nxt):
    try:
        subprocess.run(["bash", nxt], input=raw, capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001 - the status line must never fail because of the chained command
        pass
