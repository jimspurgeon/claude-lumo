# uninstall.ps1: Windows entry-point — installs Git Bash if needed, then runs uninstall.sh
#   powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1
$ErrorActionPreference = 'Stop'

Write-Host '=== claude-lumo uninstaller (Windows) ==='
Write-Host ''

# --- ensure Git Bash is available (same path as install.ps1) ---
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
    Write-Host 'Aborted.'
    exit 1
  }
}

# --- delegate to uninstall.sh ---
Write-Host ''
Write-Host 'Running uninstall.sh (bash)...'

if (Test-Path (Join-Path $PSScriptRoot 'uninstall.sh')) {
  $script = Join-Path $PSScriptRoot 'uninstall.sh'
  & $gitExe -lc '"$0" "$@"' $script "$args"
} else {
  curl.exe -sL https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/uninstall.sh | & $gitExe -
}

Write-Host ''
Write-Host 'Uninstallation complete! Close any open claude-lumo windows and delete the repo folder if you kept one.'
