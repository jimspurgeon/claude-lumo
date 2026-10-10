# install.ps1: Windows entry-point — installs Git Bash if needed, then runs install.sh
# Safe to re-run: checks everything, prompts before installing Git, then delegates.
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
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

# --- delegate to the canonical install.sh (always pulls latest if run via curl, or local if this folder is a clone) ---
Write-Host ''
Write-Host 'Running install.sh (bash)...'

# If this script lives inside a repo clone, run the local install.sh
if (Test-Path (Join-Path $PSScriptRoot 'install.sh')) {
  $script = Join-Path $PSScriptRoot 'install.sh'
  & $gitExe -lc '"$0" "$@"' $script "$args"
} else {
  # Bootstrap the latest install.sh from GitHub
  curl.exe -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/main/install.sh | & $gitExe -
}

Write-Host ''
Write-Host 'Installation complete!'
Write-Host 'If "claude-lumo" is not recognized, close and reopen this window and run:'
Write-Host '  claude-lumo -p "reply with pong"'
