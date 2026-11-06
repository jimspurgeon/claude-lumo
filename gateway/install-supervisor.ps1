# install-supervisor.ps1 — supervisor setup for the Lumo stack (#132, #183).
#
# Windows-only. Manages the two long-running processes behind claude-lumo as
# WinSW (2.12) Windows services:
#   - LumoGatewayLiteLLM   LiteLLM gateway on 127.0.0.1:4000
#   - LumoGatewayProxy     Lumo proxy on 127.0.0.1:8790
# Both start at boot (startmode Automatic), restart on non-zero exit after a
# configurable delay, and roll their logs by size. Task Scheduler is
# deliberately NOT used: its "restart on failure" fires only when the task
# fails to START, not when the process exits (#132).
#
# Modes:
#   -Install      Write both service XMLs, get WinSW (reuse an existing copy,
#                 download 2.12.0 otherwise), install and start both services.
#                 Needs an elevated PowerShell (registering services talks to
#                 the Windows service manager); refuses to run otherwise,
#                 before the key is read. Aborts before touching the service
#                 manager if something already listens on :4000 or :8790, so
#                 it can never fight a live gateway.
#   -Uninstall    Stop and remove both services; an absent service is
#                 reported and skipped. Also needs elevation.
#   (no mode)     The old offline generator (#132): print the configs and the
#                 manual commands, install nothing. -WriteConfigs writes the
#                 XMLs to a directory; -Tool nssm prints the NSSM commands.
#
# What -Install bakes into the XMLs, and the trade-offs:
#   - WinSW 2.12 services run as LocalSystem by default. Running them as the
#     installing user would need that account's password in cleartext inside
#     the XML, so LocalSystem stays. Consequence: the proxy cannot decrypt
#     Chromium cookies (their AES key is DPAPI-wrapped per user account and
#     unreadable from LocalSystem). Firefox cookies, ~/.claude-lumo/login.json
#     and LUMO_TOKEN/LUMO_UID still work; a Chrome user logs in via login.json.
#   - USERPROFILE/APPDATA/LOCALAPPDATA (and TMP/TEMP) are set to the
#     installing user's profile paths so cookie discovery and os.homedir()
#     still resolve, and CLAUDE_LUMO_STATE_DIR is passed explicitly.
#   - LITELLM_MASTER_KEY is read from master.key and written into the LiteLLM
#     XML in cleartext (WinSW 2 has no secret store). That XML is exactly as
#     secret as master.key itself; the key is never printed to the console.
#   - Working directories point INTO THIS CHECKOUT (proxy.mjs and litellm.yaml
#     live next to this script). Uninstall the services (-Uninstall, or
#     uninstall.sh) before deleting or moving the checkout.
#
# Test seam: LUMO_INSTALL_SUPERVISOR_TEST=1 makes -Install write both XMLs
# with a placeholder master key, then exit before the port probe, the WinSW
# download, the service installation and the service start. The offline tests
# use this; nothing is probed, downloaded, installed or started in that mode.
#
# Usage:
#   powershell -File gateway/install-supervisor.ps1 -Install
#   powershell -File gateway/install-supervisor.ps1 -Uninstall
#   powershell -File gateway/install-supervisor.ps1 [-Tool winsw|nssm]
#          [-WriteConfigs <dir>] [-RestartDelaySeconds <s>]
#
# Environment:
#   LUMO_SUPERVISOR_RESTART_DELAY  restart delay in seconds (default 5)
#   CLAUDE_LUMO_STATE_DIR          claude-lumo state dir (default ~/.claude-lumo)
#   LUMO_SERVICES_DIR              service root dir (default ~/.lumoservices)
#   LUMO_INSTALL_SUPERVISOR_TEST   1 = offline test mode (see above)
param(
    [switch]$Install,
    [switch]$Uninstall,
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
$ProxyDir = $PSScriptRoot                      # proxy.mjs / litellm.yaml live here
$ServicesRoot = if ($env:LUMO_SERVICES_DIR) { $env:LUMO_SERVICES_DIR } else { Join-Path $HOME ".lumoservices" }
$LiteLLMExe = Join-Path $StateDir "litellm-venv\Scripts\litellm.exe"
$LiteLLMConfig = Join-Path $ProxyDir "litellm.yaml"
$MasterKeyFile = Join-Path $StateDir "master.key"
$MaxLogBytes = 10 * 1024 * 1024               # 10 MB, one rollover file
$TestMode = ($env:LUMO_INSTALL_SUPERVISOR_TEST -eq "1")
$WinswUrl = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"

$SvcIds = @("LumoGatewayLiteLLM", "LumoGatewayProxy")

# The proxy service needs an absolute node path: the service runs as
# LocalSystem, whose PATH does not include the user's node install.
$NodeExe = "node"
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCmd) { $NodeExe = $nodeCmd.Source }

# Profile paths redirected into the LocalSystem services (see header).
$UserProfile = $env:USERPROFILE
$AppData = $env:APPDATA
$LocalAppData = $env:LOCALAPPDATA
$TmpDir = if ($env:TEMP) { $env:TEMP } elseif ($LocalAppData) { Join-Path $LocalAppData "Temp" } else { $UserProfile }

function Resolve-Or-Placeholder([string]$Path) {
    if (Test-Path $Path) { return (Resolve-Path $Path).Path }
    return $Path
}

function Escape-Xml([string]$Text) {
    return [System.Security.SecurityElement]::Escape($Text)
}

# One <env> line; empty values are skipped rather than written as empty attrs.
function New-EnvLine([string]$Name, [string]$Value) {
    if (-not $Value) { return "" }
    return ('  <env name="' + $Name + '" value="' + (Escape-Xml $Value) + '"/>')
}

$LiteLLMExeResolved = Resolve-Or-Placeholder $LiteLLMExe
$LiteLLMConfigResolved = Resolve-Or-Placeholder $LiteLLMConfig

# Shared <env> block: profile redirect plus explicit state dir. All values are
# single-line, so the joined block splices cleanly into the here-string below.
$EnvBlockShared = @(
    (New-EnvLine "USERPROFILE" $UserProfile)
    (New-EnvLine "APPDATA" $AppData)
    (New-EnvLine "LOCALAPPDATA" $LocalAppData)
    (New-EnvLine "TMP" $TmpDir)
    (New-EnvLine "TEMP" $TmpDir)
    (New-EnvLine "CLAUDE_LUMO_STATE_DIR" $StateDir)
) | Where-Object { $_ }

function New-LiteLLMServiceXml([string]$MasterKeyValue) {
    # Generator mode (empty key) leaves the key out so it never lands in
    # console output; -Install fills in the real value, read from master.key.
    $keyLine = '  <env name="LITELLM_MASTER_KEY" value="' + (Escape-Xml $MasterKeyValue) + '"/>'
    if (-not $MasterKeyValue) {
        $keyLine = "  <!-- LITELLM_MASTER_KEY: fill in from $MasterKeyFile before installing.`n       It is not embedded here so the key never lands in console output. -->"
    }
    $lines = @($keyLine) + $EnvBlockShared + @((New-EnvLine "PYTHONIOENCODING" "utf-8"))
    $envXml = $lines -join "`n"
    return @"
<service>
  <id>LumoGatewayLiteLLM</id>
  <name>Lumo LiteLLM Gateway</name>
  <description>LiteLLM gateway routing Claude Code to the Lumo proxy (127.0.0.1:4000).</description>
  <executable>$(Escape-Xml $LiteLLMExeResolved)</executable>
  <arguments>--config "$(Escape-Xml $LiteLLMConfigResolved)" --host 127.0.0.1 --port 4000</arguments>
  <workingdirectory>$(Escape-Xml $StateDir)</workingdirectory>
  <startmode>Automatic</startmode>
$envXml
  <logpath>%BASE%\logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>$MaxLogBytes</sizeThreshold>
    <keepFiles>2</keepFiles>
  </log>
  <onfailure action="restart" delay="${RestartDelaySeconds} sec"/>
  <stopparentprocessfirst>false</stopparentprocessfirst>
</service>
"@
}

function New-ProxyServiceXml {
    $envXml = $EnvBlockShared -join "`n"
    return @"
<service>
  <id>LumoGatewayProxy</id>
  <name>Lumo Proxy</name>
  <description>OpenAI-compatible proxy injecting Lumo auth into every request (127.0.0.1:8790).</description>
  <executable>$(Escape-Xml $NodeExe)</executable>
  <arguments>--no-warnings proxy.mjs</arguments>
  <workingdirectory>$(Escape-Xml $ProxyDir)</workingdirectory>
  <startmode>Automatic</startmode>
$envXml
  <logpath>%BASE%\logs</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>$MaxLogBytes</sizeThreshold>
    <keepFiles>2</keepFiles>
  </log>
  <onfailure action="restart" delay="${RestartDelaySeconds} sec"/>
</service>
"@
}

# "Connect any harness" card: the three facts any Anthropic- or OpenAI-protocol
# client needs (base URL, key, model ids). Plain ASCII so it survives any console.
# NEVER print the key value: $MasterKeyFile is a path, and the card points at
# gateway/api-key.py as the way to get the value without echoing it here.
function Show-ConnectCard {
    $RepoRoot = Split-Path $ProxyDir -Parent
    Write-Output ""
    Write-Output "=== Connect any harness (per-harness recipes: docs/GATEWAY.md) ==="
    Write-Output "Base URL:      http://127.0.0.1:4000"
    Write-Output "Endpoints:     /v1/messages                          (Anthropic protocol)"
    Write-Output "               /v1/chat/completions, /v1/models      (OpenAI protocol)"
    Write-Output "Models:        lumo-max, lumo-lite"
    Write-Output "Routing:       unknown claude-* names go to lumo-max;"
    Write-Output "               names containing haiku go to lumo-lite"
    Write-Output "API key file:  $MasterKeyFile (the key value is never printed here)"
    Write-Output "Print the key: python `"$ProxyDir\api-key.py`" `"$StateDir`""
    Write-Output "Docs:          $(Join-Path $RepoRoot 'docs\GATEWAY.md')"
}

# --- Uninstall -----------------------------------------------------------------
if ($Uninstall) {
    foreach ($id in $SvcIds) {
        $exe = Join-Path $ServicesRoot "$id\WinSW.exe"
        if (-not (Test-Path $exe)) {
            Write-Output "Service $id : no WinSW at $exe; skipping."
            continue
        }
        Write-Output "Stopping service $id ..."
        & $exe stop
        Write-Output "Removing service $id ..."
        & $exe uninstall
    }
    Write-Output "Done. The $ServicesRoot tree and the state dir remain; delete them manually if wanted."
    exit 0
}

# --- Install -------------------------------------------------------------------
if ($Install) {
    # Elevation pre-check: -Install registers LocalSystem services, which an
    # unelevated shell cannot do. Refuse before anything is probed or read,
    # so an unelevated run never even touches master.key (let alone prints a
    # false "Done"). Skipped in test mode, which installs nothing.
    if (-not $TestMode) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        $elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $elevated) {
            Write-Output "ABORT: this PowerShell is not elevated."
            Write-Output "Re-run -Install from an elevated PowerShell (Run as administrator)."
            exit 1
        }
    }

    # Preflight: never touch a live gateway. Abort before the service manager
    # sees anything if :4000 or :8790 already answers.
    if (-not $TestMode) {
        foreach ($namePort in @{ "LiteLLM" = 4000; "proxy" = 8790 }.GetEnumerator()) {
            $client = New-Object System.Net.Sockets.TcpClient
            $inUse = $false
            try {
                $task = $client.ConnectAsync("127.0.0.1", $namePort.Value)
                $task.Wait(1000) | Out-Null
                $inUse = ($task.Status -eq "RanToCompletion") -and $client.Connected
            } catch { $inUse = $false } finally { $client.Close() }
            if ($inUse) {
                Write-Output "ABORT: something already listens on 127.0.0.1:$($namePort.Value) ($($namePort.Key))."
                Write-Output "Stop it before installing the services; the new service would fail to bind."
                exit 1
            }
        }
    }

    # Read the master key. Never echoed; goes only into the XML on disk.
    $MasterKeyValue = ""
    if ($TestMode) {
        $MasterKeyValue = "PLACEHOLDER-TEST-KEY"
    } elseif (Test-Path $MasterKeyFile) {
        $MasterKeyValue = (Get-Content -Path $MasterKeyFile -Raw).Trim()
    } else {
        Write-Output "ABORT: no master key at $MasterKeyFile."
        Write-Output "Start the gateway once with claude-lumo (it creates the key), then rerun -Install."
        exit 1
    }

    # Per-service dirs, XMLs, WinSW exes.
    foreach ($id in $SvcIds) {
        $dir = Join-Path $ServicesRoot $id
        New-Item -ItemType Directory -Force (Join-Path $dir "logs") | Out-Null
        $xml = if ($id -eq "LumoGatewayLiteLLM") { New-LiteLLMServiceXml $MasterKeyValue } else { New-ProxyServiceXml }
        $xml | Out-File -FilePath (Join-Path $dir "$id.xml") -Encoding utf8
        Write-Output "Wrote $(Join-Path $dir "$id.xml")"
    }
    if ($TestMode) {
        Write-Output "LUMO_INSTALL_SUPERVISOR_TEST=1: stopping before download/install/start."
        Show-ConnectCard
        exit 0
    }

    # WinSW: reuse an existing copy, download 2.12.0 otherwise.
    $candidates = @(
        (Join-Path $ServicesRoot "WinSW-x64.exe"),
        (Join-Path $ProxyDir "WinSW-x64.exe"),
        (Join-Path $ServicesRoot "LumoGatewayLiteLLM\WinSW.exe")
    )
    $winsw = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $winsw) {
        Write-Output "Downloading WinSW 2.12.0 (x64) to $ServicesRoot ..."
        Invoke-WebRequest $WinswUrl -OutFile (Join-Path $ServicesRoot "WinSW-x64.exe")
        $winsw = Join-Path $ServicesRoot "WinSW-x64.exe"
    }
    foreach ($id in $SvcIds) {
        Copy-Item -Force $winsw (Join-Path $ServicesRoot "$id\WinSW.exe")
        # WinSW reads <exename>.xml next to the exe; ours is <id>.xml.
        Copy-Item -Force (Join-Path $ServicesRoot "$id\$id.xml") (Join-Path $ServicesRoot "$id\WinSW.xml")
    }

    # Install and start. WinSW is a native exe, so & $exe sets
    # $LASTEXITCODE; check it after every call and bail out on the first
    # failure rather than printing a false "Done" (#192).
    foreach ($id in $SvcIds) {
        $exe = Join-Path $ServicesRoot "$id\WinSW.exe"
        Write-Output "Installing service $id ..."
        & $exe install 2>&1 | Write-Output
        if ($LASTEXITCODE -ne 0) {
            Write-Output "ABORT: WinSW failed to install service $id (exit code $LASTEXITCODE)."
            Write-Output "Fix the cause and re-run -Install; already-installed services are replaced."
            exit 1
        }
        Write-Output "Starting service $id ..."
        & $exe start 2>&1 | Write-Output
        if ($LASTEXITCODE -ne 0) {
            Write-Output "ABORT: WinSW failed to start service $id (exit code $LASTEXITCODE)."
            Write-Output "Check the service's log under $(Join-Path $ServicesRoot "$id\logs"), then re-run -Install."
            exit 1
        }
    }
    Write-Output "Done. Both services are installed and started; they come back at next boot."
    Show-ConnectCard
    exit 0
}

# --- Offline generator (legacy modes, #132) -------------------------------------
Write-Output "claude-lumo supervisor config generator (issues #132, #183)"
Write-Output ""
Write-Output "Tool:              $Tool"
Write-Output "Service ids:       $($SvcIds -join ', ')"
Write-Output "Restart delay:     ${RestartDelaySeconds}s"
Write-Output "Log rotation:      $([math]::Round($MaxLogBytes / 1MB)) MB, one rollover file"
Write-Output "State dir:         $StateDir"
Write-Output "Proxy dir:         $ProxyDir"
Write-Output "LiteLLM exe:       $LiteLLMExeResolved"
Write-Output ""

if ($Tool -eq "winsw") {
    $winswLitellm = New-LiteLLMServiceXml ""
    $winswProxy = New-ProxyServiceXml
    Write-Output "=== WinSW config: LumoGatewayLiteLLM (save as LumoGatewayLiteLLM.xml next to its WinSW.exe) ==="
    Write-Output $winswLitellm
    Write-Output ""
    Write-Output "=== WinSW config: LumoGatewayProxy (save as LumoGatewayProxy.xml next to its WinSW.exe) ==="
    Write-Output $winswProxy
    Write-Output ""
    Write-Output "=== Install commands (elevated PowerShell) ==="
    Write-Output "# Or skip all of this:  powershell -File `"$PSCommandPath`" -Install"
    Write-Output "# 1. Download WinSW 2.12 (x64) and set up one directory per service:"
    # The two foreach-body lines below use single-quoted Write-Output strings
    # so the emitted lines keep their inner double quotes when pasted back
    # into PowerShell; $HOME and $s stay literal and expand in the user's own
    # shell (#143).
    Write-Output "    foreach (`$s in 'LumoGatewayLiteLLM','LumoGatewayProxy') {"
    Write-Output '        New-Item -ItemType Directory -Force "$HOME\.lumoservices\$s\logs" | Out-Null'
    Write-Output '        Invoke-WebRequest https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe -OutFile "$HOME\.lumoservices\$s\WinSW.exe"'
    Write-Output "    }"
    Write-Output "# 2. Save each xml block above as `$HOME\.lumoservices\<id>\<id>.xml"
    Write-Output "    #    (fill in LITELLM_MASTER_KEY in LumoGatewayLiteLLM.xml from $MasterKeyFile)"
    Write-Output "# 3. Install and start (services run as LocalSystem; see the script header):"
    Write-Output "    & `$HOME\.lumoservices\LumoGatewayLiteLLM\WinSW.exe install"
    Write-Output "    & `$HOME\.lumoservices\LumoGatewayProxy\WinSW.exe install"
    Write-Output "    & `$HOME\.lumoservices\LumoGatewayLiteLLM\WinSW.exe start"
    Write-Output "    & `$HOME\.lumoservices\LumoGatewayProxy\WinSW.exe start"
} else {
    # NSSM: unchanged apart from the ids (restart via AppRestartDelay,
    # rotation via AppRotateOnline + AppRotateBytes; inner quotes single-
    # quoted so pasted lines survive #140).
    $nssmCommands = @"
nssm install LumoGatewayLiteLLM '$LiteLLMExeResolved' '--config "$LiteLLMConfigResolved" --host 127.0.0.1 --port 4000'
nssm set LumoGatewayLiteLLM AppDirectory "$StateDir"
nssm set LumoGatewayLiteLLM AppEnvironmentExtra PYTHONIOENCODING=utf-8
nssm set LumoGatewayLiteLLM AppStdout "$StateDir\logs\litellm.log"
nssm set LumoGatewayLiteLLM AppStderr "$StateDir\logs\litellm.log"
nssm set LumoGatewayLiteLLM AppRotateFiles 1
nssm set LumoGatewayLiteLLM AppRotateBytes $MaxLogBytes
nssm set LumoGatewayLiteLLM AppRestartDelay $RestartDelaySeconds
nssm install LumoGatewayProxy 'node' '--no-warnings proxy.mjs'
nssm set LumoGatewayProxy AppDirectory "$ProxyDir"
nssm set LumoGatewayProxy AppStdout "$ProxyDir\logs\proxy.out.log"
nssm set LumoGatewayProxy AppStderr "$ProxyDir\logs\proxy.out.log"
nssm set LumoGatewayProxy AppRotateFiles 1
nssm set LumoGatewayProxy AppRotateBytes $MaxLogBytes
nssm set LumoGatewayProxy AppRestartDelay $RestartDelaySeconds
"@
    Write-Output "=== NSSM commands (run each line in an elevated PowerShell) ==="
    Write-Output $nssmCommands
    Write-Output ""
    Write-Output "Then start both services:  nssm start LumoGatewayLiteLLM; nssm start LumoGatewayProxy"
    Write-Output "Note: set LITELLM_MASTER_KEY for LumoGatewayLiteLLM before starting it."
    Write-Output "      Example: nssm set LumoGatewayLiteLLM AppEnvironmentExtra LITELLM_MASTER_KEY=<key from $MasterKeyFile>"
}

if ($WriteConfigs) {
    New-Item -ItemType Directory -Force $WriteConfigs | Out-Null
    $file = Join-Path $WriteConfigs "LumoGatewayLiteLLM.xml"
    New-LiteLLMServiceXml "" | Out-File -FilePath $file -Encoding utf8
    Write-Output ""
    Write-Output "Wrote $file"
    $file = Join-Path $WriteConfigs "LumoGatewayProxy.xml"
    New-ProxyServiceXml | Out-File -FilePath $file -Encoding utf8
    Write-Output "Wrote $file"
}

Show-ConnectCard
Write-Output ""
Write-Output "Nothing was installed. Run -Install, or the commands above, when ready (issues #132, #183)."
exit 0
