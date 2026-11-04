# probe-health.ps1 — periodic health probe for the Lumo stack (#132, #183).
#
# Checks every 30 seconds that both services answer, appending pass/fail lines
# with ISO timestamps to probe.log (next to this script):
#   - LiteLLM    http://127.0.0.1:4000/health/liveliness   (free, unauthenticated)
#   - LiteLLM    http://127.0.0.1:4000/v1/models          (bearer, #183: proves the
#                master key LiteLLM was started with is accepted, not just that the
#                process is up; the key is read fresh from master.key every probe and
#                is never written to the log, the console, or any variable dump)
#   - Lumo proxy http://127.0.0.1:8790/health
#
# NEVER calls LiteLLM /health: that endpoint sends a real request to EVERY
# configured model and bills tokens (#132). Liveliness is the cheap check.
#
# A missing or unreadable master.key logs one line and skips the /v1/models
# probe for that round; liveliness and the proxy probe still run.
#
# Optional: -Once runs a single round (useful as a post-install smoke test)
# instead of looping forever. Usage:
#   powershell -File scripts/probe-health.ps1 [-Once]
param(
    [switch]$Once
)

$ErrorActionPreference = "Continue"   # a failed probe is logged, never fatal

$IntervalSeconds = 30
$ProbeLog = Join-Path $PSScriptRoot "probe.log"
$StateDir = if ($env:CLAUDE_LUMO_STATE_DIR) { $env:CLAUDE_LUMO_STATE_DIR } else { Join-Path $HOME ".claude-lumo" }
$MasterKeyFile = Join-Path $StateDir "master.key"

function Write-ProbeLog([string]$Message) {
    # Synchronous, size-capped: 10 MB then one rollover file, mirroring the
    # proxy's own rotation policy (maintenance.mjs rotateLog).
    try {
        if ((Test-Path $ProbeLog) -and ((Get-Item $ProbeLog).Length -gt 10MB)) {
            Move-Item -Force $ProbeLog "$ProbeLog.1"
        }
        Add-Content -Path $ProbeLog -Value ("{0} {1}" -f ([DateTime]::UtcNow.ToString("o")), $Message)
    } catch {
        # Logging must never crash the probe loop.
    }
}

function Test-Endpoint([string]$Name, [string]$Url) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -Method GET -TimeoutSec 5 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { return $true }
        Write-ProbeLog ("{0} FAIL {1} status={2}" -f $Name, $Url, $resp.StatusCode)
        return $false
    } catch {
        Write-ProbeLog ("{0} FAIL {1} error={2}" -f $Name, $Url, $_.Exception.Message)
        return $false
    }
}

function Test-Authenticated([string]$Name, [string]$Url) {
    # Reads the key fresh each round so a rotated key is picked up without a
    # restart. The key goes straight into the header and nowhere else: not the
    # log, not the console, not the exception text ($_.Exception.Message from
    # Invoke-WebRequest carries the URL, never the Authorization header).
    try {
        $key = (Get-Content -Path $MasterKeyFile -Raw).Trim()
    } catch {
        Write-ProbeLog ("{0} SKIP {1} master.key unreadable" -f $Name, $Url)
        return $false
    }
    if (-not $key) {
        Write-ProbeLog ("{0} SKIP {1} master.key empty" -f $Name, $Url)
        return $false
    }
    try {
        $resp = Invoke-WebRequest -Uri $Url -Method GET -Headers @{Authorization = "Bearer $key"} -TimeoutSec 5 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { return $true }
        Write-ProbeLog ("{0} FAIL {1} status={2}" -f $Name, $Url, $resp.StatusCode)
        return $false
    } catch {
        Write-ProbeLog ("{0} FAIL {1} error={2}" -f $Name, $Url, $_.Exception.Message)
        return $false
    }
}

Write-ProbeLog "probe start interval=${IntervalSeconds}s"
$modeText = if ($Once) { "single round" } else { "every ${IntervalSeconds}s" }
Write-Host "Probing LiteLLM (/health/liveliness, /v1/models) and proxy (/health), $modeText; logging to $ProbeLog."

while ($true) {
    $litellmOk     = Test-Endpoint      -Name "litellm"  -Url "http://127.0.0.1:4000/health/liveliness"
    $litellmAuthOk = Test-Authenticated -Name "litellm-auth" -Url "http://127.0.0.1:4000/v1/models"
    $proxyOk       = Test-Endpoint      -Name "proxy"    -Url "http://127.0.0.1:8790/health"

    if ($litellmOk -and $litellmAuthOk -and $proxyOk) {
        Write-ProbeLog "pass litellm=ok litellm-auth=ok proxy=ok"
    } else {
        # Individual failures were already logged with details by Test-Endpoint.
        Write-ProbeLog ("fail litellm={0} litellm-auth={1} proxy={2}" -f `
            ($(if ($litellmOk) {"ok"} else {"down"})), `
            ($(if ($litellmAuthOk) {"ok"} else {"down"})), `
            ($(if ($proxyOk) {"ok"} else {"down"})))
    }
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
