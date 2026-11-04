#!/bin/bash
# watchdog.sh: monitor the Lumo stack after launch (issues #171, #183).
#
# Probes the proxy (:8790) and LiteLLM (:4000) every $INTERVAL seconds and
# relaunches whichever one died, the same way claude-lumo.sh does, with
# exponential backoff so a crash-looping service cannot hammer the machine.
# It never kills or restarts a healthy service, and it never touches a port
# other than those two.
#
# #183: when the gateway runs as the WinSW services (LumoGatewayLiteLLM,
# LumoGatewayProxy; installed by gateway/install-supervisor.ps1), Windows
# itself owns restarting them (onfailure/restart), so the watchdog stops
# relaunching a service whose WinSW directory (~/.lumoservices/<id>) exists
# and only reports the failure in this log. That keeps one supervisor per
# service instead of two fighting over it. Cookie-expiry (401) handling is
# unaffected: it lives in gateway/upstream.mjs, which re-reads credentials and
# retries, exactly as before. Override the respawn decision with
# CLAUDE_LUMO_WATCHDOG_RESPAWN: "1" forces the old relaunch-everything
# behaviour, "0" disables relaunching entirely (report-only watchdog);
# unset (the default) auto-detects per service as described above.
#
# Singleton: a pidfile in the state dir; a second invocation exits 0 immediately.
# claude-lumo.sh starts this at launch unless CLAUDE_LUMO_WATCHDOG=0.
#
# Test hooks (internal, no effect in normal use): WATCHDOG_DRYRUN=1 logs a relaunch
# instead of performing it; WATCHDOG_PROXY_URL / WATCHDOG_LITELLM_URL override the probe
# targets; WATCHDOG_GRACE overrides the post-relaunch grace period in seconds;
# WATCHDOG_BACKOFF_CAP and WATCHDOG_MAX_LOG_BYTES override their constants;
# CLAUDE_LUMO_WATCHDOG_INTERVAL overrides the base interval.

D="$HOME/.claude/lumo"
PROXY="$D/gateway"
S="${CLAUDE_LUMO_STATE_DIR:-$HOME/.claude-lumo}"
VENV="$S/litellm-venv"
LOG_DIR="$S/logs"
WATCHDOG_LOG="$LOG_DIR/watchdog.log"
PIDFILE="$S/watchdog.pid"
LITELLM_LOG="$S/litellm.log"
WINSW_ROOT="${WATCHDOG_WINSW_ROOT:-$HOME/.lumoservices}"   # per-service WinSW dirs (#183); override for tests

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) LITELLM="$VENV/Scripts/litellm.exe"; PY=python; WIN=1; native() { cygpath -m "$1"; };;
  *) LITELLM="$VENV/bin/litellm"; PY=$(command -v python3 >/dev/null && echo python3 || echo python); native() { echo "$1"; };;
esac

INTERVAL="${CLAUDE_LUMO_WATCHDOG_INTERVAL:-30}"
BACKOFF_CAP="${WATCHDOG_BACKOFF_CAP:-600}"   # 10 minutes
GRACE="${WATCHDOG_GRACE:-45}"                # LiteLLM takes up to ~45s to boot
MAX_LOG_BYTES="${WATCHDOG_MAX_LOG_BYTES:-10485760}"
PROXY_URL="${WATCHDOG_PROXY_URL:-http://127.0.0.1:8790/v1/health}"
LITELLM_URL="${WATCHDOG_LITELLM_URL:-http://127.0.0.1:4000/health/liveliness}"

# --- pure helpers, unit-tested through --selftest ---------------------------------------

# rotate_log <path> [max_bytes]: rename to <path>.1 when the file exceeds max_bytes, one
# generation, overwriting an older .1. Same policy as rotateLog in gateway/maintenance.mjs.
# A failure is logged (it usually means the file is locked, and staying silent hides why
# crash evidence is not being rotated) but never fatal.
rotate_log() {
  local path="$1" max="${2:-$MAX_LOG_BYTES}" size
  [ -f "$path" ] || return 0
  size=$(wc -c < "$path" 2>/dev/null | tr -d '[:space:]')
  [ -n "$size" ] || return 0
  [ "$size" -gt "$max" ] || return 0
  mv -f "$path" "$path.1" 2>/dev/null || printf '[%s] could not rotate %s; continuing\n' "$(iso_now)" "$path" >> "$WATCHDOG_LOG" 2>/dev/null || true
}

# next_interval <consecutive_failures>: the base interval doubled once per consecutive
# failure, capped at BACKOFF_CAP.
next_interval() {
  local n="${1:-0}" iv="$INTERVAL" i=0
  while [ "$i" -lt "$n" ] && [ "$iv" -lt "$BACKOFF_CAP" ]; do
    iv=$((iv * 2)); i=$((i + 1))
  done
  [ "$iv" -gt "$BACKOFF_CAP" ] && iv="$BACKOFF_CAP"
  printf '%s\n' "$iv"
}

case "${1:-}" in
  --selftest)
    shift
    case "${1:-}" in
      rotate) shift; rotate_log "$@"; exit $?;;
      next-interval) shift; next_interval "$@"; exit 0;;
      *) echo "watchdog: unknown selftest '${1:-}'" >&2; exit 2;;
    esac;;
esac

# --- runtime ----------------------------------------------------------------------------

iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date +%Y-%m-%dT%H:%M:%SZ; }

log_event() {
  rotate_log "$WATCHDOG_LOG"
  printf '[%s] %s\n' "$(iso_now)" "$*" >> "$WATCHDOG_LOG" 2>/dev/null || true
}

# Cheap liveness probe: HTTP 200 only, 2s timeout, same check the launcher uses.
probe_ok() {
  [ "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$1" 2>/dev/null)" = "200" ]
}

# may_respawn <winsw-dir-name>: may this watchdog relaunch this service?
# #183: a service whose WinSW dir exists is restarted by Windows itself
# (onfailure/restart), so the watchdog only reports it. The explicit override
# wins either way: 1 = always relaunch, 0 = never (report-only).
may_respawn() {
  case "${CLAUDE_LUMO_WATCHDOG_RESPAWN:-}" in
    1) return 0;;
    0) return 1;;
  esac
  [ ! -d "$WINSW_ROOT/$1" ]
}

start_proxy() {
  [ "${WATCHDOG_DRYRUN:-0}" = "1" ] && return 0
  # On Windows `nohup ... &` ties the service to this shell's console, so closing it kills
  # the service for everyone (#164); service_restart.py starts it detached instead. When
  # it is missing (an old install), fall back to nohup like the launcher does.
  if [ -n "$WIN" ] && [ -f "$D/service_restart.py" ]; then
    CLAUDE_LUMO_STATE_DIR="$(native "$S")" "$PY" "$(native "$D")/service_restart.py" \
      --install-dir "$(native "$D")" --start proxy >/dev/null 2>&1
  else
    (cd "$PROXY" && nohup node --no-warnings proxy.mjs >/dev/null 2>&1 &)
  fi
}

start_litellm() {
  local key
  [ "${WATCHDOG_DRYRUN:-0}" = "1" ] && return 0
  if [ -n "$WIN" ] && [ -f "$D/service_restart.py" ]; then
    # Crash evidence must survive relaunch (#171/#173): rotate before appending.
    rotate_log "$LITELLM_LOG"
    CLAUDE_LUMO_STATE_DIR="$(native "$S")" "$PY" "$(native "$D")/service_restart.py" \
      --install-dir "$(native "$D")" --start litellm >/dev/null 2>&1
    return
  fi
  key=$(cat "$S/master.key" 2>/dev/null || true)
  # Crash evidence must survive relaunch (#171): rotate an oversized log, then append.
  rotate_log "$LITELLM_LOG"
  (LITELLM_MASTER_KEY="$key" PYTHONIOENCODING=utf-8 nohup "$LITELLM" --config "$PROXY/litellm.yaml" \
    --host 127.0.0.1 --port 4000 >> "$LITELLM_LOG" 2>&1 &)
}

# Take the pidfile, or exit 0 when another watchdog already holds it. A pidfile naming a
# dead process is stale and gets replaced.
acquire_pidfile() {
  local old
  if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
      exit 0
    fi
    rm -f "$PIDFILE"
  fi
  printf '%s\n' "$$" > "$PIDFILE"
}

main() {
  mkdir -p "$LOG_DIR" 2>/dev/null || true
  acquire_pidfile
  trap 'rm -f "$PIDFILE"' EXIT
  trap 'exit 143' INT TERM HUP
  log_event "watchdog started (interval=${INTERVAL}s, pid=$$, state=$S)"

  local consec_proxy=0 consec_litellm=0
  local grace_proxy=0 grace_litellm=0
  local last_iv=""

  while :; do
    local now iv
    now=$(date +%s)

    # Proxy on :8790
    if [ "$now" -lt "$grace_proxy" ]; then
      : # relaunched a moment ago: let it boot before probing again
    elif probe_ok "$PROXY_URL"; then
      [ "$consec_proxy" -gt 0 ] && log_event "proxy healthy again after $consec_proxy failure(s); backoff reset"
      consec_proxy=0
    else
      consec_proxy=$((consec_proxy + 1))
      if may_respawn LumoGatewayProxy; then
        log_event "proxy down at $PROXY_URL (consecutive failures: $consec_proxy); relaunching"
        start_proxy
        grace_proxy=$((now + GRACE))
      else
        log_event "proxy down at $PROXY_URL (consecutive failures: $consec_proxy); WinSW service owns restarts, not relaunching (#183)"
      fi
    fi

    # LiteLLM on :4000. Never /health, which bills tokens.
    if [ "$now" -lt "$grace_litellm" ]; then
      :
    elif probe_ok "$LITELLM_URL"; then
      [ "$consec_litellm" -gt 0 ] && log_event "LiteLLM healthy again after $consec_litellm failure(s); backoff reset"
      consec_litellm=0
    else
      consec_litellm=$((consec_litellm + 1))
      if may_respawn LumoGatewayLiteLLM; then
        log_event "LiteLLM down at $LITELLM_URL (consecutive failures: $consec_litellm); relaunching"
        start_litellm
        grace_litellm=$((now + GRACE))
      else
        log_event "LiteLLM down at $LITELLM_URL (consecutive failures: $consec_litellm); WinSW service owns restarts, not relaunching (#183)"
      fi
    fi

    if [ "$consec_proxy" -ge "$consec_litellm" ]; then
      iv=$(next_interval "$consec_proxy")
    else
      iv=$(next_interval "$consec_litellm")
    fi
    if [ "$iv" != "$last_iv" ] && [ "$iv" -gt "$INTERVAL" ]; then
      log_event "backoff engaged: probing again in ${iv}s"
      last_iv="$iv"
    fi
    sleep "$iv"
  done
}

main "$@"
