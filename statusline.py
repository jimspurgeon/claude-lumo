"""Status line: shows which model and backend this Claude Code session is on.

If the personal additions folder (CLAUDE_LUMO_LOCAL, default ~/.claude/lumo-local) has a
statusline-next.sh, it is also run with the same status JSON, for tools that read the status line
themselves (for example Orca); its output is ignored.
"""
import json, os, re, subprocess, sys
from datetime import datetime

STATE_DIR = os.environ.get("CLAUDE_LUMO_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude-lumo")

def install_version():
    """Release tag of the install this statusline runs from, or ''. Cached per hour so the
    git subprocess is not spawned on every redraw. Untagged checkouts show 'dev'."""
    cache = os.path.join(STATE_DIR, "statusline-version.cache")
    now = datetime.now().timestamp()
    try:
        if os.path.isfile(cache) and now - os.path.getmtime(cache) < 3600:
            with open(cache, encoding="utf-8") as f:
                return f.read().strip()
    except OSError:
        pass
    tag = ""
    try:
        r = subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                            "describe", "--tags", "--exact-match", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        tag = r.stdout.strip() if r.returncode == 0 else ""
        if not tag:
            # In a worktree .git is a pointer file, not a directory, so probe with git itself.
            p = subprocess.run(["git", "-C", os.path.dirname(os.path.abspath(__file__)),
                                 "rev-parse", "--git-dir"],
                               capture_output=True, text=True, timeout=5)
            tag = "dev" if p.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - the status line must never fail because of the version probe
        return ""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(tag)
        os.replace(tmp, cache)  # atomic: a concurrent reader never sees a truncated file
    except OSError:
        pass
    return tag


TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024  # read only the tail; transcripts grow unboundedly


def last_assistant_info(transcript_path, tail_bytes=TRANSCRIPT_TAIL_BYTES):
    """Details of the last main-chain assistant entry in the transcript tail,
    or None: {"model": str, "ts": epoch-ms int}.

    Sidechain (subagent) entries are skipped: subagents legitimately run other
    models, so the last one must not paint the main session as fallen back.
    Same tail-reading rules as last_transcript_model() (partial lines skipped,
    never raises). Used both for the transcript comparison (issue #42) and to
    correlate the session's activity against the proxy's request history
    (issue #63): the entry is written moments after the serving stream ended,
    so its timestamp identifies which proxy request carried it.
    """
    if not transcript_path or not isinstance(transcript_path, str):
        return None
    try:
        with open(transcript_path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size == 0:
                return None
            f.seek(max(0, size - tail_bytes))
            chunk = f.read()
    except OSError:
        return None
    lines = chunk.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # the first line was cut mid-record by the tail read
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain"):
            continue
        msg = entry.get("message")
        model = (msg.get("model") if isinstance(msg, dict) else None) or entry.get("model")
        if not (isinstance(model, str) and model):
            continue
        ts = None
        if isinstance(entry.get("timestamp"), str):
            try:
                ts = int(datetime.fromisoformat(
                    entry["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                ts = None
        return {"model": model, "ts": ts}
    return None


def last_transcript_model(transcript_path, tail_bytes=TRANSCRIPT_TAIL_BYTES):
    """Model id of the last assistant message in the session transcript, or None.

    Transcripts are JSONL, appended as the session progresses, so recent turns live
    in the tail: read only the last `tail_bytes` of the file. Assistant entries
    carry the producing model in message["model"]. A partially written final line
    (the writer can flush mid-record) and any other unparsable lines are skipped.
    Never raises: any problem yields None.
    """
    info = last_assistant_info(transcript_path, tail_bytes)
    return info["model"] if info else None


def normalize_model(model_id):
    """Canonical form of a model id for comparison: lowercase, without a 'claude-'
    prefix or a trailing '-<numbers>' version suffix. Transcripts record full ids
    like 'claude-opus-5-5' while the configured model may be just 'opus'; both
    normalize to 'opus'. Unknown shapes pass through with only case folded."""
    s = (model_id or "").strip().lower()
    if s.startswith("claude-"):
        s = s[len("claude-"):]
    return re.sub(r"-\d+(-\d+)*$", "", s)


def looks_like_fallback(actual, configured):
    """True when the transcript model is a different model than the configured
    one, comparing normalized forms so 'claude-opus-5-5' vs 'opus' is NOT a
    fallback but 'lumo-lite' vs 'lumo-max' is."""
    if not actual or not configured:
        return False
    return normalize_model(actual) != normalize_model(configured)


# Issue #63: on Lumo the transcript cannot reveal a fallback - LiteLLM echoes the
# requested model name back to Claude Code, so every transcript entry of a
# lumo-max session that was served lumo-lite still says lumo-max. The proxy is
# the only component that sees the model actually sent downstream; it records a
# rolling history of served requests (proxy/model-history.json, see
# proxy/model-history.mjs). The session is attributed by time: the transcript's
# last main-chain assistant entry is written moments after its stream ended.
PROXY_HISTORY_PATH = os.path.join(STATE_DIR, "proxy", "model-history.json")
HISTORY_MATCH_BEFORE_MS = 30 * 1000   # entry ts may lag the request end by this much
HISTORY_MATCH_AFTER_MS = 5 * 1000     # ... or lead it slightly (write-order jitter)


def load_proxy_history(path=PROXY_HISTORY_PATH):
    """Entries of the proxy's model request history, validated; [] on any problem."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    if isinstance(data, list):
        for e in data:
            if (isinstance(e, dict) and isinstance(e.get("model"), str) and e["model"]
                    and isinstance(e.get("t0"), (int, float))
                    and isinstance(e.get("tEnd"), (int, float))):
                out.append({"model": e["model"], "t0": e["t0"], "tEnd": e["tEnd"]})
    return out


def served_model_for(info, history, before_ms=HISTORY_MATCH_BEFORE_MS,
                     after_ms=HISTORY_MATCH_AFTER_MS):
    """Model the proxy served for the session behind `info`, or None.

    An assistant entry is written moments after its stream ends, so the request
    that produced it is the most recently *completed* one at the entry's
    timestamp - not merely the closest [t0, tEnd] window, which would let a
    concurrently streaming subagent request (it legitimately uses lumo-lite)
    shadow the session's own already-finished request. Requests completing
    within `before_ms` before or `after_ms` after the entry ts are candidates;
    among them the latest tEnd wins. A sidechain (subagent) entry or one
    without a timestamp is never attributed; no candidate means None (the
    history is stale or predates this fix being deployed).
    """
    if not info or info.get("sidechain") or not isinstance(info.get("ts"), int):
        return None
    ts = info["ts"]
    best = None  # (tEnd, model)
    for e in history:
        if ts - before_ms <= e["tEnd"] <= ts + after_ms:
            if best is None or e["tEnd"] > best[0]:
                best = (e["tEnd"], e["model"])
    return best[1] if best else None


raw = sys.stdin.buffer.read()
try:
    d = json.loads(raw or b"{}")
except ValueError:
    d = {}
m = d.get("model") or {}
name = m.get("display_name") or m.get("id") or "?"
configured = m.get("id") or name
# Issue #42: after a fallback the configured model lies about who is answering.
# Compare the configured id against the model that produced the last assistant
# message in the transcript; when they differ, show the actual one with a note.
info = last_assistant_info(d.get("transcript_path"))
actual = info["model"] if info else None
# Issue #63: on Lumo the transcript cannot reveal a fallback - it records the
# model name LiteLLM echoed back, not what served the request. When the proxy's
# history can attribute this session, that model is the truth and wins.
served = served_model_for(info, load_proxy_history()) if info else None
if served:
    actual = served
if looks_like_fallback(actual, configured):
    shown = f"{actual} (fallback from {configured})"
else:
    # What to show when nothing fell back: the [LUMO] branch prefers the model
    # id, the [CLAUDE] branch the display name (pre-#42 behaviour preserved).
    shown = configured if os.environ.get("CLAUDE_LUMO") == "1" else name
pct = ""
cw = d.get("context_window") or {}
if isinstance(cw, dict) and cw.get("used_percentage") is not None:
    pct = f"  ctx {cw['used_percentage']:.0f}%"
ver = install_version()
ver_txt = f"  {ver}" if ver else ""  # tag already starts with 'v'; 'dev' has no prefix
if os.environ.get("CLAUDE_LUMO") == "1":
    sys.stdout.write(f"\x1b[1;35m[LUMO]\x1b[0m {shown} via local proxy{pct}{ver_txt}  (Claude: run `claude`)")
else:
    sys.stdout.write(f"\x1b[1;33m[CLAUDE]\x1b[0m {shown}{pct}{ver_txt}  (Lumo: run `claude-lumo`)")
sys.stdout.flush()

local = os.environ.get("CLAUDE_LUMO_LOCAL") or os.path.join(os.path.expanduser("~"), ".claude", "lumo-local")
nxt = os.path.join(local, "statusline-next.sh")
if os.path.isfile(nxt):
    try:
        subprocess.run(["bash", nxt], input=raw, capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001 - the status line must never fail because of the chained command
        pass

# Update notice (v1.3.0+): append if within 7 days.
notice_path = os.path.join(STATE_DIR, "update-notice.txt")
try:
    if os.path.isfile(notice_path):
        mtime = os.path.getmtime(notice_path)
        if datetime.now().timestamp() - mtime < 7 * 24 * 3600:
            with open(notice_path, encoding="utf-8") as f:
                notice = f.read().strip()
            if notice:
                sys.stdout.write(f"  \x1b[33m{notice}\x1b[0m")
except Exception:
    pass
