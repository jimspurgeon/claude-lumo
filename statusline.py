"""Status line: shows which model and backend this Claude Code session is on."""
import json, os, sys
try:
    d = json.loads(sys.stdin.buffer.read() or b"{}")
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
