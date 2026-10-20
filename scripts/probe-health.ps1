# probe-health.ps1 — periodic health probe for the Lumo stack (#132).
#
# Checks every 30 seconds that both services answer, appending pass/fail lines
# with ISO timestamps to probe.log (next to this script):
#   - LiteLLM  http://127.0.0.1:4000/health/liveliness   (free, unauthenticated)
#   - Lumo proxy http://127.0.0.1:8790/health
#
# NEVER calls LiteLLM /health: that endpoint sends a real request to EVERY
# configured model and bills tokens (#132). Liveliness is the cheap check.
#
# Runs forever until stopped (Ctrl+C). Usage:
#   powershell -File scripts/probe-health.ps1

$ErrorActionPreference = "Continue"   # a failed probe is logged, never fatal

$IntervalSeconds = 30
$ProbeLog = Join-Path $PSScriptRoot "probe.log"

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

Write-ProbeLog "probe start interval=${IntervalSeconds}s"
Write-Host "Probing LiteLLM (/health/liveliness) and proxy (/health) every ${IntervalSeconds}s; logging to $ProbeLog. Ctrl+C to stop."

while ($true) {
    $litellmOk = Test-Endpoint -Name "litellm" -Url "http://127.0.0.1:4000/health/liveliness"
    $proxyOk   = Test-Endpoint -Name "proxy"   -Url "http://127.0.0.1:8790/health"

    if ($litellmOk -and $proxyOk) {
        Write-ProbeLog "pass litellm=ok proxy=ok"
    } else {
        # Individual failures were already logged with details by Test-Endpoint.
        Write-ProbeLog ("fail litellm={0} proxy={1}" -f ($(if ($litellmOk) {"ok"} else {"down"})), ($(if ($proxyOk) {"ok"} else {"down"})))
    }
    Start-Sleep -Seconds $IntervalSeconds
}
