# install-omp-full.ps1: Windows one-liner wrapper for install-omp-full.sh.
#
# PowerShell one-liner:
#   irm https://raw.githubusercontent.com/jimspurgeon/lumo-gateway/master/install-omp-full.ps1 | iex
#
# Requirements: PowerShell + Git for Windows (Git Bash). Downloads the bash script from
# this repo and runs it under Git Bash. Both URLs are printed before anything is executed.
# Nothing here installs services or runs anything elevated.

$ErrorActionPreference = 'Stop'

$rawBase = 'https://raw.githubusercontent.com/jimspurgeon/lumo-gateway/master'
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

$shPath = $null
try {
  # Download straight to a .sh path (the one we invoke); a New-TemporaryFile
  # gives a .tmp file, which the previous version never renamed before use.
  $shPath = Join-Path ([System.IO.Path]::GetTempPath()) ("install-omp-full-{0}.sh" -f ([guid]::NewGuid().ToString('N')))
  Invoke-WebRequest -UseBasicParsing -Uri $bashScriptUrl -OutFile $shPath

  Write-Host "Running install-omp-full.sh under Git Bash (script: $shPath)..."
  # Single invocation, passing the Windows path as a direct argument (Git Bash
  # converts it); a quoted `bash -lc "...$shPath..."` string loses backslashes
  # in MSYS path handling.
  & $bashExe $shPath
  if ($LASTEXITCODE -ne 0) {
    throw "install-omp-full.sh exited with code $LASTEXITCODE."
  }
  Write-Host 'Done.'
}
finally {
  if ($shPath -and (Test-Path $shPath)) {
    Remove-Item $shPath -ErrorAction SilentlyContinue
  }
}
