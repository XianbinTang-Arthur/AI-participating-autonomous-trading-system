[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Start',
    [ValidateSet('spot', 'spot-live', 'derivatives', 'derivatives-live', 'derivatives-live-monolith')]
    [string]$Profile = 'derivatives',
    [string]$Distro = 'Ubuntu',
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Action -eq 'Start' -and $Profile -in @('spot-live', 'derivatives-live', 'derivatives-live-monolith')) {
    throw "Live profile '$Profile' is disabled while REAL-MONEY PRODUCTION is NO-GO. Stop and Status remain available for legacy cleanup."
}

function Write-KeepAliveInfo {
    param([string]$Message)
    Write-Host "[wsl-keepalive] $Message"
}

function Get-StateRoot {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        return Join-Path $env:LOCALAPPDATA 'AATS\startup-prewarm'
    }

    return Join-Path $env:TEMP 'AATS-startup-prewarm'
}

function Get-StateFilePath {
    $safeDistro = ($Distro -replace '[^A-Za-z0-9_.-]', '_').ToLowerInvariant()
    return Join-Path (Get-StateRoot) ("wsl-keepalive-{0}.json" -f $safeDistro)
}

function Get-KeepAliveSentinel {
    $safeDistro = ($Distro -replace '[^A-Za-z0-9_]', '_').ToUpperInvariant()
    return "AATS_WSL_KEEPALIVE_$safeDistro"
}

function Ensure-StateRoot {
    $path = Get-StateRoot
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
    return $path
}

function Read-StateRecord {
    $path = Get-StateFilePath
    if (-not (Test-Path $path)) {
        return $null
    }

    try {
        return Get-Content $path -Raw -Encoding utf8 | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Write-StateTextWithRetry {
    param(
        [string]$Path,
        [string]$Content,
        [int]$RetryCount = 5,
        [int]$DelayMilliseconds = 200
    )

    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        try {
            [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
            return
        }
        catch [System.IO.IOException] {
            if ($attempt -ge $RetryCount) {
                throw
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Write-StateRecord {
    param(
        [int]$ProcessId,
        [string]$CommandLine
    )

    $path = Get-StateFilePath
    Ensure-StateRoot | Out-Null
    $payload = @{
        pid = $ProcessId
        distro = $Distro
        profile = $Profile
        sentinel = Get-KeepAliveSentinel
        command_line = $CommandLine
        updated_at = [DateTimeOffset]::Now.ToString('o')
    }
    Write-StateTextWithRetry -Path $path -Content ($payload | ConvertTo-Json -Depth 4)
}

function Clear-StateRecord {
    $path = Get-StateFilePath
    if (Test-Path $path) {
        Remove-Item -Path $path -Force
    }
}

function Test-ProcessMatchesSentinel {
    param([object]$ProcessRecord)

    if ($null -eq $ProcessRecord) {
        return $false
    }

    if ([string]$ProcessRecord.Name -ne 'wsl.exe') {
        return $false
    }

    $commandLine = [string]$ProcessRecord.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }

    return ($commandLine -like ('*{0}*' -f (Get-KeepAliveSentinel)))
}

function Get-KeepAliveProcessByPid {
    param([int]$ProcessId)

    try {
        $processRecord = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId)
    }
    catch {
        return $null
    }

    if (-not (Test-ProcessMatchesSentinel -ProcessRecord $processRecord)) {
        return $null
    }

    return $processRecord
}

function Find-KeepAliveProcess {
    $stateRecord = Read-StateRecord
    if ($null -ne $stateRecord -and $stateRecord.pid) {
        $fromState = Get-KeepAliveProcessByPid -Pid ([int]$stateRecord.pid)
        if ($null -ne $fromState) {
            return $fromState
        }
    }

    try {
        $candidates = Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'"
    }
    catch {
        return $null
    }

    foreach ($candidate in $candidates) {
        if (Test-ProcessMatchesSentinel -ProcessRecord $candidate) {
            return $candidate
        }
    }

    return $null
}

function Get-KeepAliveCommand {
    $sentinel = Get-KeepAliveSentinel
    return ("export {0}=1; trap 'exit 0' TERM INT; while true; do sleep 3600; done" -f $sentinel)
}

function Start-KeepAlive {
    $existing = Find-KeepAliveProcess
    if ($null -ne $existing) {
        Write-StateRecord -Pid ([int]$existing.ProcessId) -CommandLine ([string]$existing.CommandLine)
        Write-KeepAliveInfo ("already running pid={0} distro={1}" -f $existing.ProcessId, $Distro)
        return
    }

    $command = Get-KeepAliveCommand
    if ($DryRun) {
        Write-KeepAliveInfo ("dry-run: would start hidden keepalive for distro={0} profile={1}" -f $Distro, $Profile)
        Write-KeepAliveInfo ("command=bash -lc ""{0}""" -f $command)
        return
    }

    $process = Start-Process -WindowStyle Hidden -FilePath 'wsl.exe' -ArgumentList @('-d', $Distro, 'bash', '-lc', $command) -PassThru
    Start-Sleep -Seconds 1

    $running = Find-KeepAliveProcess
    if ($null -eq $running) {
        throw "WSL keepalive process did not stay alive for distro: $Distro"
    }

    Write-StateRecord -Pid ([int]$running.ProcessId) -CommandLine ([string]$running.CommandLine)
    Write-KeepAliveInfo ("started pid={0} distro={1}" -f $running.ProcessId, $Distro)
}

function Stop-KeepAlive {
    $existing = Find-KeepAliveProcess
    if ($null -eq $existing) {
        Clear-StateRecord
        Write-KeepAliveInfo ("no keepalive process found for distro={0}" -f $Distro)
        return
    }

    if ($DryRun) {
        Write-KeepAliveInfo ("dry-run: would stop keepalive pid={0} distro={1}" -f $existing.ProcessId, $Distro)
        return
    }

    Stop-Process -Id ([int]$existing.ProcessId) -Force
    Start-Sleep -Milliseconds 500
    Clear-StateRecord
    Write-KeepAliveInfo ("stopped pid={0} distro={1}" -f $existing.ProcessId, $Distro)
}

function Show-KeepAliveStatus {
    $existing = Find-KeepAliveProcess
    if ($null -eq $existing) {
        Clear-StateRecord
        Write-KeepAliveInfo ("status=stopped distro={0}" -f $Distro)
        return
    }

    Write-StateRecord -Pid ([int]$existing.ProcessId) -CommandLine ([string]$existing.CommandLine)
    Write-KeepAliveInfo ("status=running pid={0} distro={1}" -f $existing.ProcessId, $Distro)
}

switch ($Action) {
    'Start' { Start-KeepAlive }
    'Stop' { Stop-KeepAlive }
    'Status' { Show-KeepAliveStatus }
    default { throw "Unsupported action: $Action" }
}
