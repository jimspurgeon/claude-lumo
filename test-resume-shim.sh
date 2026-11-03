#!/bin/bash
# test-resume-shim.sh: validate the resume shim (POSIX shells + PowerShell + profile wiring).
# Run from the repo root: bash test-resume-shim.sh
# Uses a temp CLAUDE_CONFIG_DIR with fake transcripts and stub claude/claude-lumo binaries
# on PATH. No real sessions, no real binaries touched.
set -u
cd "$(dirname "$0")"

FAILURES=0
check() { # check <label> <condition-result>
  if [ "$2" = "0" ]; then echo "PASS: $1"; else echo "FAIL: $1"; FAILURES=$((FAILURES + 1)); fi
}

# Base the temp dir on $TEMP so it is a real Windows path PowerShell can also read.
TMP="$(mktemp -d "${TEMP}/resume-shim-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# --- Stubs: echo which launcher ran and the args; exit 7 when asked to fail ---
mkdir -p "$TMP/bin" "$TMP/config/projects/test" "$TMP/home"
printf '#!/bin/sh\necho "STUB claude $*"\n[ "$1" = "--boom" ] && exit 7\nexit 0\n' > "$TMP/bin/claude"
printf '#!/bin/sh\necho "STUB claude-lumo $*"\n[ "$1" = "--boom" ] && exit 7\nexit 0\n' > "$TMP/bin/claude-lumo"
printf '@echo off\r\necho STUB claude-cmd %%*\r\nexit /b 0\r\n' > "$TMP/bin/claude.cmd"
printf '@echo off\r\necho STUB claude-lumo-cmd %%*\r\nexit /b 0\r\n' > "$TMP/bin/claude-lumo.cmd"
chmod +x "$TMP/bin/claude" "$TMP/bin/claude-lumo"

LUMO_ID="a1b2c3d4-e5f6-7890-abcd-ef1234567890"
REG_ID="b2c3d4e5-f6a7-8901-bcde-f12345678901"
printf '{"type":"assistant","message":{"content":"hi"}}\n{"model":"lumo-max"}\n' > "$TMP/config/projects/test/$LUMO_ID.jsonl"
printf '{"type":"assistant","message":{"content":"hi"}}\n{"model":"claude-sonnet-5-5"}\n' > "$TMP/config/projects/test/$REG_ID.jsonl"

export CLAUDE_CONFIG_DIR="$TMP/config"

# --- POSIX shim (bash) ---
run_sh() { # run_sh <args...>: source the shim in a fresh bash with stubs on PATH
  PATH="$TMP/bin:$PATH" bash -c '. scripts/resume-shim.sh; claude "$@"' _ "$@"
}

out="$(run_sh --resume "$LUMO_ID")";                                    check "bash: lumo transcript -> claude-lumo" "$([ "${out#STUB claude-lumo --resume $LUMO_ID}" != "$out" ] ; echo $?)"
out="$(run_sh -r "$LUMO_ID")";                                          check "bash: lumo transcript via -r" "$([ "${out#STUB claude-lumo -r $LUMO_ID}" != "$out" ]; echo $?)"
out="$(run_sh "--resume=$LUMO_ID")";                                     check "bash: lumo transcript via --resume=" "$([ "${out#STUB claude-lumo --resume=$LUMO_ID}" != "$out" ]; echo $?)"
out="$(run_sh --resume "$REG_ID")";                                      check "bash: regular transcript -> real claude" "$([ "${out#STUB claude --resume $REG_ID}" != "$out" ]; echo $?)"
out="$(run_sh --resume deadbeef-1234-5678-9abc-def012345678)";            check "bash: unknown id -> real claude" "$([ "${out#STUB claude --resume deadbeef}" != "$out" ]; echo $?)"
out="$(run_sh --version)";                                               check "bash: --version -> real claude" "$([ "${out#STUB claude --version}" != "$out" ]; echo $?)"
out="$(run_sh -p "say hi" --flags "kept intact")";                       check "bash: no resume -> real claude, args intact" "$([ "$out" = "STUB claude -p say hi --flags kept intact" ]; echo $?)"
run_sh --boom >/dev/null 2>&1;                                           check "bash: exit code preserved (7)" "$([ $? = 7 ]; echo $?)"
out="$(CLAUDE_CONFIG_DIR= PATH="$TMP/bin:$PATH" bash -c '. scripts/resume-shim.sh; claude --resume "$1"' _ "$LUMO_ID")"; check "bash: id not in any store -> real claude" "$([ "${out#STUB claude --resume $LUMO_ID}" != "$out" ]; echo $?)"

# --- Bare `claude -r` (no id): resume-picker helper (bash) ---
# The Bash shim runs "bash <repo>/scripts/resume-picker.sh" from the real repo checkout, and the
# picker finds stubs through PATH + CLAUDE_CONFIG_DIR. Feed choices via redirected stdin; the
# menu is forced on so the interactive path runs even though stdin is a pipe here.
# Deterministic mtimes: plain transcript is newer, so row 1 = plain, row 2 = lumo in both twins.
touch -d '2026-11-03 10:00' "$TMP/config/projects/test/$LUMO_ID.jsonl"
touch -d '2026-11-03 11:00' "$TMP/config/projects/test/$REG_ID.jsonl"
mkdir -p "$TMP/home"

pick_sh() { # pick_sh <menu-input> <args...>: run `claude <args>` with the shim, feeding input
  local input="$1"; shift
  PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$TMP/config" HOME="$TMP/home" \
    RESUME_PICKER_FORCE_MENU=1 bash -c '. scripts/resume-shim.sh; claude "$@"' _ "$@" <<< "$input"
}
out="$(pick_sh 2 -r --strict)";                                            check "bash: bare -r picks lumo row -> claude-lumo --resume id, args kept" "$([ "${out#*STUB claude-lumo --resume $LUMO_ID --strict}" != "$out" ]; echo $?)"
out="$(pick_sh 2 --resume --strict)";                                       check "bash: bare --resume picks lumo row -> claude-lumo --resume id, args kept" "$([ "${out#*STUB claude-lumo --resume $LUMO_ID --strict}" != "$out" ]; echo $?)"
out="$(pick_sh 1 -r --strict)";                                             check "bash: bare -r picks plain row -> real claude --resume id, args kept" "$([ "${out#*STUB claude --resume $REG_ID --strict}" != "$out" ]; echo $?)"
out="$(pick_sh p -r --extra arg)";                                          check "bash: bare -r input p -> real claude, original args unchanged" "$([ "${out#*STUB claude -r --extra arg}" != "$out" ]; echo $?)"
out="$(pick_sh "" -r --extra arg)";                                         check "bash: bare -r empty input -> real claude, original args unchanged" "$([ "${out#*STUB claude -r --extra arg}" != "$out" ]; echo $?)"
out="$(pick_sh zz9 -r)";                                                   check "bash: bare -r invalid input -> real claude, original args unchanged" "$([ "${out#*STUB claude -r}" != "$out" ]; echo $?)"
out="$(pick_sh 99 -r)";                                                     check "bash: bare -r out-of-range number -> real claude, original args unchanged" "$([ "${out#*STUB claude -r}" != "$out" ]; echo $?)"
out="$(PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$TMP/config" HOME="$TMP/home" bash -c '. scripts/resume-shim.sh; claude -r --extra arg' </dev/null)"; check "bash: bare -r non-tty -> real claude, original args unchanged" "$([ "$out" = "STUB claude -r --extra arg" ]; echo $?)"
out="$(PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$TMP/nonexistent" HOME="$TMP/home" RESUME_PICKER_FORCE_MENU=1 bash -c '. scripts/resume-shim.sh; claude -r' <<< "")"; check "bash: bare -r no transcripts anywhere -> real claude, original args unchanged" "$([ "$out" = "STUB claude -r" ]; echo $?)"
# A literal -r after the "--" end-of-options separator is a positional argument, not a
# resume flag: no picker (choice 1 would have resumed a session had the menu appeared).
out="$(pick_sh 1 -- -r)";                                              check "bash: literal -r after -- -> real claude, no picker" "$([ "$out" = "STUB claude -- -r" ]; echo $?)"
# Transcript-dir dedupe (parity with picker.ps1): with CLAUDE_CONFIG_DIR equal to ~/.claude
# the store must be scanned once, so the menu holds one row and choice 2 falls through to
# plain claude with the original args (without the dedupe the duplicate row 2 would resume
# the session). Separate HOME so no other test's store picks up these transcripts.
mkdir -p "$TMP/home2/.claude/projects/test"
cp "$TMP/config/projects/test/$REG_ID.jsonl" "$TMP/home2/.claude/projects/test/$REG_ID.jsonl"
touch -d '2026-11-03 12:00' "$TMP/home2/.claude/projects/test/$REG_ID.jsonl"
out="$(PATH="$TMP/bin:$PATH" CLAUDE_CONFIG_DIR="$TMP/home2/.claude" HOME="$TMP/home2" RESUME_PICKER_FORCE_MENU=1 bash -c '. scripts/resume-shim.sh; claude -r' <<< "2")"; check "bash: picker dedupes transcript dirs (CLAUDE_CONFIG_DIR=~/.claude listed once)" "$([ "${out#*STUB claude --resume $REG_ID}" = "$out" ]; echo $?)"

# --- Setup script: install twice, uninstall, profiles restored byte-identically ---
PROF="$TMP/home/.bashrc"
mkdir -p "$TMP/home"
echo "# my profile" > "$PROF"
cp "$PROF" "$PROF.orig"
HOME="$TMP/home" bash scripts/resume-shim-setup.sh install >/dev/null
cp "$PROF" "$PROF.once"
HOME="$TMP/home" bash scripts/resume-shim-setup.sh install >/dev/null
cmp -s "$PROF" "$PROF.once";                                            check "setup: double install is idempotent" "$?"
grep -qF '.claude/lumo/scripts/resume-shim.sh' "$PROF";                  check "setup: profile sources the repo copy" "$?"
HOME="$TMP/home" bash scripts/resume-shim-setup.sh uninstall >/dev/null
cmp -s "$PROF" "$PROF.orig";                                            check "setup: uninstall restores profile byte-identically" "$?"
[ ! -f "$TMP/home/Documents/PowerShell/Microsoft.PowerShell_profile.ps1" ]; check "setup: created PowerShell profile removed on uninstall" "$?"
printf 'no trailing newline' > "$PROF"
cp "$PROF" "$PROF.orig2"
HOME="$TMP/home" bash scripts/resume-shim-setup.sh install >/dev/null
HOME="$TMP/home" bash scripts/resume-shim-setup.sh uninstall >/dev/null
cmp -s "$PROF" "$PROF.orig2";                                            check "setup: profile without trailing newline restored byte-identically" "$?"

# --- PowerShell shim (skip quietly where PowerShell is unavailable) ---
PS_BIN="$(command -v powershell.exe || command -v pwsh || true)"
if [ -n "$PS_BIN" ]; then
  ps_call() { # ps_call <args...>: dot-source the ps1 shim with stubs first on PATH
    local quoted="" a
    for a in "$@"; do quoted="$quoted '$a'"; done
    "$PS_BIN" -NoProfile -Command "\$env:PATH='$(cygpath -w "$TMP/bin" 2>/dev/null || echo "$TMP/bin")'+';'+\$env:PATH; . ./scripts/resume-shim.ps1; claude$quoted"
  }
  out="$(ps_call --resume "$LUMO_ID")";                                  check "powershell: lumo transcript -> claude-lumo" "$([ "${out#*STUB claude-lumo-cmd --resume $LUMO_ID}" != "$out" ]; echo $?)"
  out="$(ps_call --resume "$REG_ID")";                                   check "powershell: regular transcript -> real claude" "$([ "${out#*STUB claude-cmd --resume $REG_ID}" != "$out" ]; echo $?)"
  out="$(ps_call --version)";                                             check "powershell: --version -> real claude" "$([ "${out#*STUB claude-cmd --version}" != "$out" ]; echo $?)"
  out="$(ps_call --resume deadbeef-1234-5678-9abc-def012345678)";          check "powershell: unknown id -> real claude" "$([ "${out#*STUB claude-cmd --resume deadbeef}" != "$out" ]; echo $?)"
  # --- Bare `claude -r` (no id): resume-picker helper (PowerShell) ---
  # The PS shim invokes the helper via "powershell -NoProfile -File", which also gets the
  # stubs on PATH; choices are fed through a piped here-string (menu forced on).
  ps_pick() { # ps_pick <menu-input> <args...>: run the picker helper with stdin from input
    local input="$1"; shift
    local quoted="" a
    for a in "$@"; do quoted="$quoted '$a'"; done
    printf '%s\n' "$input" | "$PS_BIN" -NoProfile -Command "\$env:PATH='$(cygpath -w "$TMP/bin" 2>/dev/null || echo "$TMP/bin")'+';'+\$env:PATH; \$env:CLAUDE_CONFIG_DIR='$(cygpath -w "$TMP/config" 2>/dev/null || echo "$TMP/config")'; \$env:HOME='$(cygpath -w "$TMP/home" 2>/dev/null || echo "$TMP/home")'; \$env:RESUME_PICKER_FORCE_MENU='1'; . ./scripts/resume-shim.ps1; claude$quoted"
  }
  # Row order matches the bash block: plain (newest) then lumo.
  out="$(ps_pick 2 -r --strict)";                                           check "powershell: bare -r picks lumo row -> claude-lumo --resume id, args kept" "$([ "${out#*STUB claude-lumo-cmd --resume $LUMO_ID --strict}" != "$out" ]; echo $?)"
  out="$(ps_pick 1 -r --strict)";                                           check "powershell: bare -r picks plain row -> real claude --resume id, args kept" "$([ "${out#*STUB claude-cmd --resume $REG_ID --strict}" != "$out" ]; echo $?)"
  out="$(ps_pick p -r --extra arg)";                                        check "powershell: bare -r input p -> real claude, original args unchanged" "$([ "${out#*STUB claude-cmd -r --extra arg}" != "$out" ]; echo $?)"
  out="$(ps_pick "" -r --extra arg)";                                       check "powershell: bare -r empty input -> real claude, original args unchanged" "$([ "${out#*STUB claude-cmd -r --extra arg}" != "$out" ]; echo $?)"
  out="$(ps_pick zz9 -r)";                                                  check "powershell: bare -r invalid input -> real claude, original args unchanged" "$([ "${out#*STUB claude-cmd -r}" != "$out" ]; echo $?)"
  out="$(ps_pick 99 -r)";                                                   check "powershell: bare -r out-of-range number -> real claude, original args unchanged" "$([ "${out#*STUB claude-cmd -r}" != "$out" ]; echo $?)"
  # Literal -r after the "--" end-of-options separator: positional argument, no picker.
  out="$(ps_pick 1 -- -r)";                                                 check "powershell: literal -r after -- -> real claude, no picker" "$([ "${out#*STUB claude-cmd -- -r}" != "$out" ]; echo $?)"
  # Non-tty stdin (redirected, menu NOT forced): old shim behavior, original args.
  out="$(: | "$PS_BIN" -NoProfile -Command "\$env:PATH='$(cygpath -w "$TMP/bin" 2>/dev/null || echo "$TMP/bin")'+';'+\$env:PATH; \$env:CLAUDE_CONFIG_DIR='$(cygpath -w "$TMP/config" 2>/dev/null || echo "$TMP/config")'; \$env:HOME='$(cygpath -w "$TMP/home" 2>/dev/null || echo "$TMP/home")'; . ./scripts/resume-shim.ps1; claude -r --extra arg")"; check "powershell: bare -r non-tty -> real claude, original args unchanged" "$([ "${out#*STUB claude-cmd -r --extra arg}" != "$out" ]; echo $?)"
else
  echo "SKIP: powershell not found; PowerShell shim not tested"
fi

echo "---"
if [ "$FAILURES" = "0" ]; then echo "All tests passed."; exit 0; else echo "$FAILURES test(s) FAILED."; exit 1; fi
