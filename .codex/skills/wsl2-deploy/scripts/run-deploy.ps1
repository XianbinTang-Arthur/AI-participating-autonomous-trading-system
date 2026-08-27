[CmdletBinding()]
param(
    [ValidateSet('spot', 'spot-live', 'derivatives', 'derivatives-live', 'derivatives-live-monolith')]
    [string]$Profile = 'derivatives',
    [string]$CommitMessage,
    [switch]$SkipSync,
    [switch]$SkipCommit,
    [switch]$NoCache,
    [switch]$AssumeYes,
    [int]$Timeout = 90,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Profile -in @('spot-live', 'derivatives-live', 'derivatives-live-monolith')) {
    throw "Live profile '$Profile' is disabled while REAL-MONEY PRODUCTION is NO-GO. Use spot or derivatives for local testing."
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..\..\..\..')).Path
}

function Get-PreferredBashPath {
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    $rejectedLaunchers = @(
        '*\WindowsApps\bash.exe',
        '*\Windows\System32\bash.exe',
        '*\Windows\Sysnative\bash.exe',
        '*\Windows\SysWOW64\bash.exe'
    )
    $commandIsUsable = $cmd -and $cmd.Source
    foreach ($pattern in $rejectedLaunchers) {
        if ($commandIsUsable -and $cmd.Source -like $pattern) {
            $commandIsUsable = $false
        }
    }
    if ($commandIsUsable) {
        return $cmd.Source
    }

    $candidates = @(
        'D:\Git\Git\bin\bash.exe',
        'D:\Git\Git\usr\bin\bash.exe',
        'C:\Program Files\Git\bin\bash.exe',
        'C:\Program Files\Git\usr\bin\bash.exe'
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    throw 'No usable bash.exe was found. Install Git Bash or fix PATH.'
}

$repoRoot = Get-RepoRoot
$deployScript = Join-Path $repoRoot 'scripts\deploy.sh'
$bashPath = Get-PreferredBashPath

if (-not (Test-Path $deployScript)) {
    throw "Deploy script not found: $deployScript"
}

if ($CommitMessage -and $SkipCommit) {
    throw 'Do not pass -CommitMessage and -SkipCommit together.'
}

$deployArgs = @(
    $deployScript
    '--profile', $Profile
    '--timeout', $Timeout
)

if ($CommitMessage) {
    $deployArgs += @('--commit', $CommitMessage)
}
if ($SkipSync) {
    $deployArgs += '--skip-sync'
}
if ($SkipCommit) {
    $deployArgs += '--skip-commit'
}
if ($NoCache) {
    $deployArgs += '--no-cache'
}
if ($AssumeYes) {
    $deployArgs += '--yes'
}

Write-Host "[wsl2-deploy] repo root: $repoRoot"
Write-Host "[wsl2-deploy] bash: $bashPath"
Write-Host "[wsl2-deploy] profile: $Profile"
Write-Host "[wsl2-deploy] command: $bashPath $($deployArgs -join ' ')"

if ($DryRun) {
    return
}

& $bashPath @deployArgs
exit $LASTEXITCODE
