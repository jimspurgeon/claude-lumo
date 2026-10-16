"""Status line: shows which model and backend this Claude Code session is on.

If the personal additions folder (CLAUDE_LUMO_LOCAL, default ~/.claude/lumo-local) has a
statusline-next.sh, it is also run with the same status JSON, for tools that read the status line
themselves (for example Orca); its output is ignored.

`--lumo` (passed by settings-lumo.json) marks a claude-lumo session even when CLAUDE_LUMO is not in
the environment, as in a session Claude Code re-hosted after the agents view (#77, #66).
"""
import json, os, re, subprocess, sys
from datetime import datetime

LUMO = "--lumo" in sys.argv[1:] or os.environ.get("CLAUDE_LUMO") == "1"
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
    if tag.startswith("v"):  # a dev checkout is never offered an update, so skip the check
        start_update_check(now)
    return tag


UPDATE_CHECK_EVERY = 3600  # seconds between spawns, shared by every statusline (#120)
SPAWN_LOCK_STALE = 60  # a lock older than this was left by a statusline that died


def spawn_detached(argv):
    """Start argv with no stdio and detached, so the redraw neither waits for it nor shows it."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def start_update_check(now=None):
    """Fire and forget update-check.py, at most once an hour across all statuslines (#120).

    The launcher runs the check only at startup, so a long-running session never learned of a new
    release. This rides on install_version()'s hourly refresh; the check itself rate-limits the
    network call (update-check-state.json) and writes update-notice.txt, which a later redraw
    shows. A lock directory (mkdir is atomic) plus a timestamp file keep concurrent statuslines
    from spawning it together. Respects CLAUDE_LUMO_UPDATE_CHECK=0. True when it spawned."""
    if os.environ.get("CLAUDE_LUMO_UPDATE_CHECK") == "0":  # the launcher's and README's switch
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "update-check.py")
    if not os.path.isfile(script):
        return False
    now = now if now is not None else datetime.now().timestamp()
    stamp = os.path.join(STATE_DIR, "update-check-spawn.stamp")
    lock = stamp + ".lock"
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        os.mkdir(lock)
    except FileExistsError:
        try:
            if now - os.path.getmtime(lock) <= SPAWN_LOCK_STALE:
                return False  # a concurrent statusline holds it
            # Left by a statusline that died: clear it and claim it in this same refresh. Two
            # clearing it at once can both spawn; update-check.py's own rate limit absorbs that.
            os.rmdir(lock)
            os.mkdir(lock)
        except OSError:
            return False
    except OSError:
        return False
    try:
        try:
            if now - os.path.getmtime(stamp) < UPDATE_CHECK_EVERY:
                return False
        except OSError:
            pass  # no stamp yet
        with open(stamp, "w", encoding="utf-8"):
            pass
        os.utime(stamp, (now, now))
        spawn_detached([sys.executable, script, here])
        return True
    except Exception:  # noqa: BLE001 - the status line must never fail because of the update check
        return False
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024  # read only the tail; transcripts grow unboundedly
SYNTHETIC_MODEL = "<synthetic>"  # Claude Code's model id on locally generated error turns


def last_transcript_model(transcript_path, tail_bytes=TRANSCRIPT_TAIL_BYTES):
    """Model id of the last assistant message in the session transcript, or None.

    Transcripts are JSONL, appended as the session progresses, so recent turns live
    in the tail: read only the last `tail_bytes` of the file. Assistant entries
    carry the producing model in message["model"]. A partially written final line
    (the writer can flush mid-record) and any other unparsable lines are skipped.
    Never raises: any problem yields None.
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
        msg = entry.get("message")
        model = (msg.get("model") if isinstance(msg, dict) else None) or entry.get("model")
        # Issue #63: Claude Code writes failed turns (API errors) as assistant entries with the
        # placeholder model "<synthetic>"; no model answered them, so look further back.
        if isinstance(model, str) and model and model != SYNTHETIC_MODEL:
            return model
    return None


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


# Issue #65: Claude Code compacts well before the model's window is full, so a percentage of the
# window (context_window.used_percentage) read "ctx 35%" moments before a compaction. The trigger
# point is not documented and not in the status JSON, so this is an approximation calibrated on
# 35 automatic compactions in lumo sessions (Claude Code 2.1.296, compact_boundary preTokens):
# at autoCompactWindow 100000 they fired between 64K and 81K, mostly 66K to 72K; at 110000 (one
# session so far) between 77K and 84K. That fits window - min(max output, 20000) - 13000 to
# within a few percent, so the bar can read about 96% when a compaction fires. The window is
# derived at render time from context-fit.mjs's CONTEXT_TOKENS minus a 12000 margin (119072),
# so compaction fires well below Lumo's 131072-token window for prompt and
# reply together (the proxy shrinks max_tokens to fit a long prompt, see context-fit.mjs).
COMPACT_OUTPUT_RESERVE_CAP = 20000
COMPACT_BUFFER = 13000


def _int_or_none(v):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def compact_window(settings_path=None):
    """The window auto-compact uses: CLAUDE_CODE_AUTO_COMPACT_WINDOW (it overrides the setting),
    else autoCompactWindow from the settings file the wrapper passes, else None."""
    env = _int_or_none(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
    if env:
        return env
    path = settings_path or os.path.join(STATE_DIR, "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            return _int_or_none(json.load(f).get("autoCompactWindow"))
    except (OSError, ValueError, AttributeError):
        return None


def compact_threshold(window, context_size=None, max_output=None):
    """Token count at which auto-compact fires for `window` (capped at the model's own size)."""
    if not window:
        return None
    if context_size:
        window = min(window, context_size)
    reserve = min(max_output or COMPACT_OUTPUT_RESERVE_CAP, COMPACT_OUTPUT_RESERVE_CAP)
    t = window - reserve - COMPACT_BUFFER
    return t if t > 0 else None


def context_used_tokens(cw):
    """Input tokens of the last request (what compaction measures), or None."""
    cu = cw.get("current_usage")
    if isinstance(cu, dict):
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        vals = [cu.get(k) for k in keys]
        if any(isinstance(v, (int, float)) for v in vals):
            return sum(v for v in vals if isinstance(v, (int, float)))
    pct, size = cw.get("used_percentage"), cw.get("context_window_size")
    if isinstance(pct, (int, float)) and isinstance(size, (int, float)) and size > 0:
        return pct * size / 100
    return None


def context_text(cw, settings_path=None, lumo=True):
    """'  ctx N%' measured against the auto-compact trigger when it is known (100% = compaction
    is due), else Claude Code's own used_percentage; '' when there is nothing to show."""
    if not isinstance(cw, dict):
        return ""
    threshold = lumo and compact_threshold(compact_window(settings_path),
                                  _int_or_none(cw.get("context_window_size")),
                                  _int_or_none(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")))
    used = context_used_tokens(cw)
    if threshold and used is not None:
        return f"  ctx {min(used / threshold * 100, 100):.0f}%"
    if cw.get("used_percentage") is not None:
        return f"  ctx {cw['used_percentage']:.0f}%"
    return ""


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
actual = last_transcript_model(d.get("transcript_path"))
if looks_like_fallback(actual, configured):
    shown = f"{actual} (fallback from {configured})"
else:
    # What to show when nothing fell back: the [LUMO] branch prefers the model
    # id, the [CLAUDE] branch the display name (pre-#42 behaviour preserved).
    shown = configured if LUMO else name
# Lumo installs know their compaction settings; plain Claude keeps its own percentage.
pct = context_text(d.get("context_window") or {}, lumo=LUMO)
ver = install_version() if LUMO else ""  # plain Claude sessions show no claude-lumo version
ver_txt = f"  {ver}" if ver else ""  # tag already starts with 'v'; 'dev' has no prefix
# The launcher records the release the session started on; once the install has moved to another
# release, this session still runs the old files until it is restarted (#35).
started = os.environ.get("CLAUDE_LUMO_SESSION_VERSION", "")
restart_txt = ""
if LUMO and started and ver and ver != "dev" and started != ver:
    restart_txt = f"  \x1b[33mrestart to load {ver}\x1b[0m"
    ver_txt = f"  {started}"  # the version slot names what this session runs, not the install (#117)
if LUMO:
    sys.stdout.write(f"\x1b[1;35m[LUMO]\x1b[0m {shown} via local proxy{pct}{ver_txt}{restart_txt}  (Claude: run `claude`)")
else:
    sys.stdout.write(f"\x1b[1;33m[CLAUDE]\x1b[0m {shown}{pct}  (Lumo: run `claude-lumo`)")
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
    if LUMO and os.path.isfile(notice_path):  # a claude-lumo update means nothing to plain Claude
        mtime = os.path.getmtime(notice_path)
        if datetime.now().timestamp() - mtime < 7 * 24 * 3600:
            with open(notice_path, encoding="utf-8") as f:
                notice = f.read().strip()
            if notice:
                sys.stdout.write(f"  \x1b[33m{notice}\x1b[0m")
except Exception:
    pass
