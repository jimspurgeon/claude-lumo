# install-omp-full.ps1: Windows one-liner wrapper for install-omp-full.sh.
#
# PowerShell one-liner:
#   irm https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master/install-omp-full.ps1 | iex
#
# Requirements: PowerShell + Git for Windows (Git Bash). Downloads the bash script from
# this repo and runs it under Git Bash. Both URLs are printed before anything is executed.
# Nothing here installs services or runs anything elevated.

$ErrorActionPreference = 'Stop'

$rawBase = 'https://raw.githubusercontent.com/jimspurgeon/claude-lumo/master'
$bashScriptUrl = "$rawBase/install-omp-full.sh"

Write-Host '=== omp + lumo-gateway full install (Windows wrapper) ==='
Write-Host "Downloading: $bashScriptUrl"

# Locate Git Bash (only the standard install path; it is the documented prerequisite).
$bashExe = 'C:\Program Files\Git\bin\bash.exe'
if (-not (Test-Path $bashExe)) {
  $bashExe = (Get-Command bash.exe -ErrorAction SilentlyContinue).Source
}
if (-not $bashExe) {
  throw 'Git Bash not found. Install Git for Windows (https://git-scm.com), then re-run.'
}

$tmp = New-TemporaryFile
try {
  Invoke-WebRequest -UseBasicParsing -Uri $bashScriptUrl -OutFile $tmp.FullName
  # Save as .sh; the temp file (.tmp) would still run, but keep it obvious.
  $shPath = "$($tmp.FullName).sh"
  & $bashExe $shPath
  $tmp = $null

  Write-Host "Running install-omp-full.sh under Git Bash (script: $shPath)..."
  & $bashExe -lc "`"$shPath`""
  if ($LASTEXITCODE -ne 0) {
    throw "install-omp-full.sh exited with code $LASTEXITCODE."
  }
  Write-Host 'Done.'
}
finally {
  if ($tmp) { Remove-Item $tmp.FullName -ErrorAction SilentlyContinue }
  if ((Test-Path variable:shPath) -and (Test-Path $shPath)) {
    Remove-Item $shPath -ErrorAction SilentlyContinue
  }
}
