[CmdletBinding()]
param(
    [ValidateSet('spot', 'spot-live', 'derivatives', 'derivatives-live', 'derivatives-live-monolith')]
    [string]$Profile = 'derivatives',
    [string]$TaskName = '',
    [int]$DelaySeconds = 30,
    [ValidateRange(1, 1440)]
    [int]$MonitorIntervalMinutes = 5,
    [ValidateRange(0, 1440)]
    [int]$RepairCooldownMinutes = 30,
    [switch]$Remove,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $Remove -and $Profile -in @('spot-live', 'derivatives-live', 'derivatives-live-monolith')) {
    throw "Live profile '$Profile' is disabled while REAL-MONEY PRODUCTION is NO-GO. Use spot or derivatives for local testing."
}

function Write-StartupTaskInfo {
    param([string]$Message)
    Write-Host "[startup-task] $Message"
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Get-EffectiveTaskName {
    param([string]$ResolvedTaskName, [string]$ResolvedProfile)

    if ([string]::IsNullOrWhiteSpace($ResolvedTaskName)) {
        return "AATS-WSL2-Prewarm-$ResolvedProfile"
    }

    return $ResolvedTaskName
}

function Get-DelayIso8601 {
    param([int]$ResolvedDelaySeconds)

    if ($ResolvedDelaySeconds -le 0) {
        return $null
    }

    return ('PT{0}S' -f $ResolvedDelaySeconds)
}

$effectiveTaskName = Get-EffectiveTaskName -ResolvedTaskName $TaskName -ResolvedProfile $Profile
$prewarmScript = Join-Path (Get-RepoRoot) 'scripts\prewarm_wsl2_aats.ps1'

if (-not (Test-Path $prewarmScript)) {
    throw "Prewarm script not found: $prewarmScript"
}

if ($Remove) {
    Write-StartupTaskInfo "task_name=$effectiveTaskName action=remove"
    if ($DryRun) {
        Write-StartupTaskInfo "dry-run: would unregister scheduled task"
        return
    }

    Unregister-ScheduledTask -TaskName $effectiveTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-StartupTaskInfo "scheduled task removed"
    return
}

$repairCooldownSeconds = $RepairCooldownMinutes * 60
$argument = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Profile {1} -RepairCooldownSeconds {2}' -f $prewarmScript, $Profile, $repairCooldownSeconds
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$delayValue = Get-DelayIso8601 -ResolvedDelaySeconds $DelaySeconds
if ($delayValue) {
    $logonTrigger.Delay = $delayValue
}
$periodicStart = (Get-Date).AddSeconds([Math]::Max(1, $DelaySeconds))
# Omitting RepetitionDuration produces an unbounded repetition trigger in the
# Windows Task Scheduler schema. The logon trigger provides immediate recovery
# after a reboot; the time trigger keeps monitoring indefinitely afterwards.
$periodicTrigger = New-ScheduledTaskTrigger -Once -At $periodicStart -RepetitionInterval (New-TimeSpan -Minutes $MonitorIntervalMinutes)
$triggers = @($logonTrigger, $periodicTrigger)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$description = "AATS continuous keepalive + prewarm: keep Ubuntu running, check every $MonitorIntervalMinutes minutes, and repair the $Profile stack through the standard deploy gate"

Write-StartupTaskInfo "task_name=$effectiveTaskName profile=$Profile delay_seconds=$DelaySeconds monitor_interval_minutes=$MonitorIntervalMinutes repair_cooldown_minutes=$RepairCooldownMinutes"
Write-StartupTaskInfo "script=$prewarmScript"

if ($DryRun) {
    Write-StartupTaskInfo "dry-run: would register AtLogOn plus indefinite periodic scheduled task for continuous keepalive + prewarm"
    return
}

$existingTask = Get-ScheduledTask -TaskName $effectiveTaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Unregister-ScheduledTask -TaskName $effectiveTaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $effectiveTaskName -Action $action -Trigger $triggers -Settings $settings -Description $description | Out-Null
Write-StartupTaskInfo "scheduled task registered"
