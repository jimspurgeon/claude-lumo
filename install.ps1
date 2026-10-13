# install.ps1: Windows entry-point — installs Git Bash if needed, then runs install.sh
# Safe to re-run: checks everything, prompts before installing Git, then delegates.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
# Optional: add -ResumeShim to also install the resume shim (see README, "Resume shim").
$ErrorActionPreference = 'Stop'

Write-Host '=== claude-lumo installer (Windows) ==='
Write-Host ''

# --- check/install Git Bash (idempotent, prompt with default Y) ---
$gitExe = "$env:PROGRAMFILES\Git\bin\bash.exe"
if (-not (Test-Path $gitExe)) {
  Write-Host 'Git Bash not found.'
  $ans = Read-Host 'Install Git Bash now? (Y/n)'
  if ($ans -eq '' -or $ans -eq 'Y' -or $ans -eq 'y') {
    Write-Host 'Installing Git Bash...'
    winget install --accept-source-agreements --accept-package-agreements --silent Git.Git
    if (-not (Test-Path $gitExe)) {
      Write-Error 'Git Bash installation appeared to succeed but bash.exe was not found at '$gitExe
      exit 1
    }
  } else {
    Write-Host 'Aborted: Git Bash is required to run claude-lumo. Install it manually and re-run this script.'
    exit 1
  }
} else {
  Write-Host 'Git Bash found: '$gitExe
}

# --- delegate to the canonical install.sh (always installs the latest release tag if run via
# curl; set CLAUDE_LUMO_CHANNEL=master to follow master instead, or local if this folder is a
# clone) ---
Write-Host ''
Write-Host 'Running install.sh (bash)...'

# If this script lives inside a repo clone, run the local install.sh.
# ($PSScriptRoot is empty when piped through `irm ... | iex`, and Join-Path '' would throw.)
$localSh = if ($PSScriptRoot) { Join-Path $PSScriptRoot 'install.sh' } else { $null }
if ($localSh -and (Test-Path $localSh)) {
  & $gitExe -lc '"$0" "$@"' $localSh "$args"
} else {
  # Bootstrap the latest install.sh from GitHub (forward any args, e.g. --separate-data)
  curl.exe -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/install.sh | & $gitExe -s -- "$args"
}

Write-Host ''
Write-Host 'Installation complete!'
Write-Host 'If "claude-lumo" is not recognized, close and reopen this window and run:'
Write-Host '  claude-lumo -p "reply with pong"'
