[CmdletBinding()]
param(
    [ValidateSet('spot', 'spot-live', 'derivatives', 'derivatives-live', 'derivatives-live-monolith')]
    [string]$Profile = 'derivatives-live',
    [string]$TaskName = '',
    [int]$DelaySeconds = 30,
    [switch]$Remove,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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

$argument = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Profile {1}' -f $prewarmScript, $Profile
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$delayValue = Get-DelayIso8601 -ResolvedDelaySeconds $DelaySeconds
if ($delayValue) {
    $trigger.Delay = $delayValue
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$description = "AATS startup keepalive + prewarm: keep Ubuntu running, wait for Docker, and repair the $Profile stack if needed"

Write-StartupTaskInfo "task_name=$effectiveTaskName profile=$Profile delay_seconds=$DelaySeconds"
Write-StartupTaskInfo "script=$prewarmScript"

if ($DryRun) {
    Write-StartupTaskInfo "dry-run: would register AtLogOn scheduled task for keepalive + prewarm"
    return
}

$existingTask = Get-ScheduledTask -TaskName $effectiveTaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    Unregister-ScheduledTask -TaskName $effectiveTaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $effectiveTaskName -Action $action -Trigger $trigger -Settings $settings -Description $description | Out-Null
Write-StartupTaskInfo "scheduled task registered"
