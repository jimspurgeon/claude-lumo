# install-supervisor.ps1 — generate supervisor configs for the Lumo stack (#132).
#
# Windows-only. Generates WinSW (2.12) or NSSM service definitions for the two
# long-running processes behind claude-lumo:
#   - LiteLLM gateway on 127.0.0.1:4000
#   - Lumo proxy    on 127.0.0.1:8790
# Both restart on non-zero exit (with a configurable delay) and roll their logs
# by size. Task Scheduler is deliberately NOT used: its "restart on failure"
# fires only when the task fails to START, not when the process exits (#132).
#
# This script DOES NOT INSTALL ANYTHING. It prints the generated config files
# and the exact PowerShell commands to run, then exits 0. Nothing is written to
# disk unless -WriteConfigs is passed with a directory.
#
# Usage:
#   powershell -File scripts/install-supervisor.ps1 [-Tool winsw|nssm]
#          [-WriteConfigs <dir>] [-RestartDelaySeconds <s>]
#
# Environment:
#   LUMO_SUPERVISOR_RESTART_DELAY  restart delay in seconds (default 5)
#   CLAUDE_LUMO_STATE_DIR          claude-lumo state dir (default ~/.claude-lumo)
param(
    [ValidateSet("winsw", "nssm")]
    [string]$Tool = "winsw",
    [string]$WriteConfigs = "",
    [int]$RestartDelaySeconds = 0
)

$ErrorActionPreference = "Stop"

# PowerShell 5.1 has no ??; resolve defaults explicitly.
if (-not $RestartDelaySeconds) {
    if ($env:LUMO_SUPERVISOR_RESTART_DELAY) { $RestartDelaySeconds = [int]$env:LUMO_SUPERVISOR_RESTART_DELAY }
    else { $RestartDelaySeconds = 5 }
}
$StateDir = if ($env:CLAUDE_LUMO_STATE_DIR) { $env:CLAUDE_LUMO_STATE_DIR } else { Join-Path $HOME ".claude-lumo" }
$RepoDir = Split-Path -Parent $PSScriptRoot   # this checkout
$ProxyDir = Join-Path $RepoDir "proxy"         # proxy.mjs / litellm.yaml live here
$LiteLLMExe = Join-Path $StateDir "litellm-venv\Scripts\litellm.exe"
$LiteLLMConfig = Join-Path $ProxyDir "litellm.yaml"
$MasterKeyFile = Join-Path $StateDir "master.key"
$MaxLogBytes = 10 * 1024 * 1024               # 10 MB, one rollover file

function Resolve-Or-Placeholder([string]$Path) {
    if (Test-Path $Path) { return (Resolve-Path $Path).Path }
    return $Path
}

$LiteLLMExeResolved = Resolve-Or-Placeholder $LiteLLMExe
$LiteLLMConfigResolved = Resolve-Or-Placeholder $LiteLLMConfig

Write-Output "claude-lumo supervisor config generator (issue #132)"
Write-Output ""
Write-Output "Tool:              $Tool"
Write-Output "Restart delay:     ${RestartDelaySeconds}s"
Write-Output "Log rotation:      $([math]::Round($MaxLogBytes / 1MB)) MB, one rollover file"
Write-Output "State dir:         $StateDir"
Write-Output "Proxy dir:         $ProxyDir"
Write-Output "LiteLLM exe:       $LiteLLMExeResolved"
Write-Output ""

# --- WinSW (v2.12) -----------------------------------------------------------
# Service wrapper: one WinSW exe + xml per service, named after its id.
# Services run as the installing (current) user; onfailure restarts with a delay.
# size-based rotation via logmode=roll + size threshold, keeping one backup.
$winswLitellm = @"
<service>
  <id>LumoLiteLLM</id>
  <name>Lumo LiteLLM Gateway</name>
  <description>LiteLLM gateway routing Claude Code to the Lumo proxy (127.0.0.1:4000).</description>
  <executable>$LiteLLMExeResolved</executable>
  <arguments>--config "$LiteLLMConfigResolved" --host 127.0.0.1 --port 4000</arguments>
  <workingdirectory>$StateDir</workingdirectory>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <!-- LITELLM_MASTER_KEY: fill in from $MasterKeyFile before installing.
       It is not embedded here so the key never lands in console output. -->
  <logpath>%BASE%\logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>$MaxLogBytes</sizeThreshold>
    <keepFiles>2</keepFiles>
  </log>
  <onfailure action="restart" delay="${RestartDelaySeconds} sec"/>
  <stopparentprocessfirst>false</stopparentprocessfirst>
</service>
"@

$winswProxy = @"
<service>
  <id>LumoProxy</id>
  <name>Lumo Proxy</name>
  <description>OpenAI-compatible proxy injecting Lumo auth into every request (127.0.0.1:8790).</description>
  <executable>node</executable>
  <arguments>--no-warnings proxy.mjs</arguments>
  <workingdirectory>$ProxyDir</workingdirectory>
  <logpath>%BASE%\logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>$MaxLogBytes</sizeThreshold>
    <keepFiles>2</keepFiles>
  </log>
  <onfailure action="restart" delay="${RestartDelaySeconds} sec"/>
</service>
"@

# --- NSSM commands ------------------------------------------------------------
# NSSM restarts a service by default when the process exits non-zero; the delay
# comes from AppRestartDelay. Rotation via AppRotateOnline + AppRotateBytes.
$nssmCommands = @"
nssm install LumoLiteLLM "$LiteLLMExeResolved" "--config `"$LiteLLMConfigResolved`" --host 127.0.0.1 --port 4000"
nssm set LumoLiteLLM AppDirectory "$StateDir"
nssm set LumoLiteLLM AppEnvironmentExtra PYTHONIOENCODING=utf-8
nssm set LumoLiteLLM AppStdout "$StateDir\logs\litellm.log"
nssm set LumoLiteLLM AppStderr "$StateDir\logs\litellm.log"
nssm set LumoLiteLLM AppRotateFiles 1
nssm set LumoLiteLLM AppRotateBytes $MaxLogBytes
nssm set LumoLiteLLM AppRestartDelay $RestartDelaySeconds
nssm install LumoProxy "node" "--no-warnings proxy.mjs"
nssm set LumoProxy AppDirectory "$ProxyDir"
nssm set LumoProxy AppStdout "$ProxyDir\logs\proxy.out.log"
nssm set LumoProxy AppStderr "$ProxyDir\logs\proxy.out.log"
nssm set LumoProxy AppRotateFiles 1
nssm set LumoProxy AppRotateBytes $MaxLogBytes
nssm set LumoProxy AppRestartDelay $RestartDelaySeconds
"@

# --- Output ------------------------------------------------------------------
if ($Tool -eq "winsw") {
    Write-Output "=== WinSW config: LumoLiteLLM (save as LumoLiteLLM.xml next to its WinSW.exe) ==="
    Write-Output $winswLitellm
    Write-Output ""
    Write-Output "=== WinSW config: LumoProxy (save as LumoProxy.xml next to its WinSW.exe) ==="
    Write-Output $winswProxy
    Write-Output ""
    Write-Output "=== Install commands (run as the current user, in PowerShell) ==="
    Write-Output "# 1. Download WinSW 2.12 (x64) and set up one directory per service:"
    Write-Output "    foreach (`$s in 'LumoLiteLLM','LumoProxy') {"
    Write-Output "        New-Item -ItemType Directory -Force "`$HOME\.lumoservices\`$s\logs" | Out-Null"
    Write-Output "        Invoke-WebRequest https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe -OutFile "`$HOME\.lumoservices\`$s\WinSW.exe""
    Write-Output "    }"
    Write-Output "# 2. Save each xml block above as `$HOME\.lumoservices\<id>\<id>.xml"
    Write-Output "    #    (fill in LITELLM_MASTER_KEY in LumoLiteLLM.xml from $MasterKeyFile)"
    Write-Output "# 3. Install and start (services run as the current user):"
    Write-Output "    & `$HOME\.lumoservices\LumoLiteLLM\WinSW.exe install"
    Write-Output "    & `$HOME\.lumoservices\LumoProxy\WinSW.exe install"
    Write-Output "    & `$HOME\.lumoservices\LumoLiteLLM\WinSW.exe start"
    Write-Output "    & `$HOME\.lumoservices\LumoProxy\WinSW.exe start"
} else {
    Write-Output "=== NSSM commands (run each line in an elevated PowerShell) ==="
    Write-Output $nssmCommands
    Write-Output ""
    Write-Output "Then start both services:  nssm start LumoLiteLLM; nssm start LumoProxy"
    Write-Output "Note: set LITELLM_MASTER_KEY for LumoLiteLLM before starting it."
    Write-Output "      Example: nssm set LumoLiteLLM AppEnvironmentExtra LITELLM_MASTER_KEY=<key from $MasterKeyFile>"
}

if ($WriteConfigs) {
    New-Item -ItemType Directory -Force $WriteConfigs | Out-Null
    $file = Join-Path $WriteConfigs "LumoLiteLLM.xml"
    $winswLitellm | Out-File -FilePath $file -Encoding utf8
    Write-Output ""
    Write-Output "Wrote $file"
    $file = Join-Path $WriteConfigs "LumoProxy.xml"
    $winswProxy | Out-File -FilePath $file -Encoding utf8
    Write-Output "Wrote $file"
}

Write-Output ""
Write-Output "Nothing was installed. Run the commands above when ready (issue #132)."
exit 0
