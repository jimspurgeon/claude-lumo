"""PreToolUse hook for Bash: keep Claude attribution out of commits, PRs, issues and releases.

claude-lumo runs on Lumo models, so "Generated with Claude Code" and "Co-Authored-By: Claude"
lines are false. The settings already turn Claude Code's own attribution off, but Lumo models still
write the lines from habit. A message file passed with -F, --file, --body-file or --notes-file has
the lines removed in place, and the command runs. A command with the lines inline is blocked with a
reason, so the model rewrites it. Silent otherwise; any error lets the command through unchanged.
"""
import json, os, re, sys

ATTRIBUTION = re.compile(
    r"^.*(?:Generated with \[?Claude Code|Co-Authored-By:[^\n]*(?:Claude|anthropic\.com)).*$\n?",
    re.IGNORECASE | re.MULTILINE)
WRITES = re.compile(r"\b(?:git\s+(?:commit|tag|merge)|gh\s+(?:pr|issue|release|api|gist))\b")
FILE_ARG = re.compile(r"(?:^|\s)(?:-F|--file|--body-file|--notes-file)(?:\s+|=)(\"[^\"]+\"|'[^']+'|\S+)")


def local_path(arg):
    p = os.path.expanduser(os.path.expandvars(arg.strip("'\"")))
    m = re.match(r"^/([a-zA-Z])/(.*)", p)  # Git Bash /c/... -> C:/...
    return f"{m.group(1).upper()}:/{m.group(2)}" if m and os.name == "nt" else p


def strip_file(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        clean = ATTRIBUTION.sub("", text)
        if clean != text:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(clean.rstrip() + "\n")
    except OSError:
        pass  # a stdin "-", a missing file or an unreadable path: leave it to the command


def main():
    try:
        cmd = json.loads(sys.stdin.buffer.read().decode("utf-8")).get("tool_input", {}).get("command") or ""
    except Exception:  # noqa: BLE001 - never block a command because the hook failed
        return
    if not WRITES.search(cmd):
        return
    for arg in FILE_ARG.findall(cmd):
        if arg != "-":
            strip_file(local_path(arg))
    if ATTRIBUTION.search(cmd):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "Blocked: this command adds Claude attribution (a \"Generated with Claude Code\" or "
                "\"Co-Authored-By: Claude\" line). This session runs on Lumo models, not Claude, so the line "
                "is false. Remove those lines and run the command again."}}))


if __name__ == "__main__":
    main()
