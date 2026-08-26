[CmdletBinding()]
param(
    [ValidateSet('spot', 'spot-live', 'derivatives', 'derivatives-live', 'derivatives-live-monolith')]
    [string]$Profile = 'derivatives',
    [string]$Distro = 'Ubuntu',
    [int]$DockerTimeoutSeconds = 120,
    [int]$HealthTimeoutSeconds = 120,
    [int]$DeployTimeoutSeconds = 120,
    [switch]$SkipRepairDeploy,
    [switch]$SkipKeepAlive,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-PrewarmInfo {
    param([string]$Message)
    Write-Host "[startup-prewarm] $Message"
}

if ($Profile -in @('spot-live', 'derivatives-live', 'derivatives-live-monolith')) {
    throw "Live profile '$Profile' is disabled while REAL-MONEY PRODUCTION is NO-GO. Use spot or derivatives for local testing."
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Get-DeployWrapperPath {
    $repoRoot = Get-RepoRoot
    return Join-Path $repoRoot '.codex\skills\wsl2-deploy\scripts\run-deploy.ps1'
}

function Get-KeepAliveScriptPath {
    $repoRoot = Get-RepoRoot
    return Join-Path $repoRoot 'scripts\keepalive_wsl2_aats.ps1'
}

function Get-RequiredContainers {
    param([string]$ResolvedProfile)

    switch ($ResolvedProfile) {
        'derivatives-live-monolith' { return @('aats-gateway', 'aats-rdp-daemon', 'aats-liquidations-daemon', 'aats-microstructure-collector') }
        'spot' { return @('aats-gateway', 'aats-market', 'aats-decision', 'aats-execution', 'aats-rdp-daemon') }
        'spot-live' { return @('aats-gateway', 'aats-market', 'aats-decision', 'aats-execution', 'aats-rdp-daemon') }
        'derivatives' { return @('aats-gateway', 'aats-market', 'aats-decision', 'aats-execution', 'aats-rdp-daemon', 'aats-liquidations-daemon', 'aats-microstructure-collector') }
        'derivatives-live' { return @('aats-gateway', 'aats-market', 'aats-decision', 'aats-execution', 'aats-rdp-daemon', 'aats-liquidations-daemon', 'aats-microstructure-collector') }
        default { throw "Unsupported profile: $ResolvedProfile" }
    }
}

function Get-ProfileEnvFileName {
    param([string]$ResolvedProfile)

    switch ($ResolvedProfile) {
        'spot' { return '.env.spot' }
        'spot-live' { return '.env.spot.live' }
        'derivatives' { return '.env.derivatives' }
        'derivatives-live' { return '.env.derivatives.live' }
        'derivatives-live-monolith' { return '.env.derivatives.live' }
        default { throw "Unsupported profile: $ResolvedProfile" }
    }
}

function Get-ApiPort {
    param([string]$ResolvedProfile)

    $repoRoot = Get-RepoRoot
    $envFile = Join-Path $repoRoot (Get-ProfileEnvFileName -ResolvedProfile $ResolvedProfile)
    if (-not (Test-Path $envFile)) {
        return 8000
    }

    foreach ($line in Get-Content $envFile -Encoding utf8) {
        if ($line -match '^\s*AATS_API_PORT=(.+?)\s*$') {
            $value = $Matches[1].Trim().Trim('"')
            $port = 0
            if ([int]::TryParse($value, [ref]$port)) {
                return $port
            }
        }
    }

    return 8000
}

function Get-HealthScheme {
    param([string]$ResolvedProfile)

    switch ($ResolvedProfile) {
        'spot-live' { return 'https' }
        'derivatives-live' { return 'https' }
        'derivatives-live-monolith' { return 'https' }
        default { return 'http' }
    }
}

function Invoke-WslCommand {
    param([string]$Command)
    & wsl.exe -d $Distro bash -lc $Command
}

function Ensure-WslKeepAlive {
    param([string]$ResolvedProfile)

    $scriptPath = Get-KeepAliveScriptPath
    if (-not (Test-Path $scriptPath)) {
        throw "Keepalive script not found: $scriptPath"
    }

    $args = @(
        '-NoProfile'
        '-ExecutionPolicy'
        'Bypass'
        '-File'
        $scriptPath
        '-Action'
        'Start'
        '-Profile'
        $ResolvedProfile
        '-Distro'
        $Distro
    )

    Write-PrewarmInfo "ensuring WSL keepalive is running"
    & powershell.exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start WSL keepalive with exit code $LASTEXITCODE"
    }
}

function Test-DockerReady {
    $null = Invoke-WslCommand -Command "docker info >/dev/null 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Get-ContainerState {
    param([string]$ContainerName)

    $command = "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' '$ContainerName' 2>/dev/null"
    $output = Invoke-WslCommand -Command $command
    if ($LASTEXITCODE -ne 0) {
        return ''
    }
    return ($output | Out-String).Trim()
}

function Test-RequiredContainersHealthy {
    param([string[]]$RequiredContainers)

    foreach ($container in $RequiredContainers) {
        $state = Get-ContainerState -ContainerName $container
        if ($state -ne 'running healthy') {
            return $false
        }
    }
    return $true
}

function Show-ContainerStates {
    param([string[]]$RequiredContainers)

    foreach ($container in $RequiredContainers) {
        $state = Get-ContainerState -ContainerName $container
        if ([string]::IsNullOrWhiteSpace($state)) {
            Write-PrewarmInfo "$container missing"
        }
        else {
            Write-PrewarmInfo "$container $state"
        }
    }
}

function Test-GatewayHealth {
    param(
        [int]$Port,
        [string]$Scheme = 'http'
    )

    # Run curl inside WSL so we share deploy.sh's health-check semantics and
    # sidestep Windows PowerShell 5.1 TLS 1.2 / self-signed cert limitations.
    if ($Scheme -eq 'https') {
        $command = "curl -kfs 'https://127.0.0.1:$Port/healthz' >/dev/null 2>&1"
    }
    else {
        $command = "curl -fs 'http://127.0.0.1:$Port/healthz' >/dev/null 2>&1"
    }

    $null = Invoke-WslCommand -Command $command
    return ($LASTEXITCODE -eq 0)
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSeconds,
        [int]$IntervalSeconds = 3,
        [string]$Description
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) {
            return $true
        }
        Start-Sleep -Seconds $IntervalSeconds
    }

    if ($Description) {
        Write-PrewarmInfo "$Description still not ready after ${TimeoutSeconds}s"
    }
    return $false
}

function Invoke-RepairDeploy {
    param([string]$ResolvedProfile)

    $wrapper = Get-DeployWrapperPath
    if (-not (Test-Path $wrapper)) {
        throw "Deploy wrapper not found: $wrapper"
    }

    $args = @(
        '-NoProfile'
        '-ExecutionPolicy'
        'Bypass'
        '-File'
        $wrapper
        '-Profile'
        $ResolvedProfile
        '-SkipSync'
        '-SkipCommit'
        '-AssumeYes'
        '-Timeout'
        $DeployTimeoutSeconds
    )

    Write-PrewarmInfo "triggering repair deploy via standard wrapper"
    & powershell.exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "Repair deploy failed with exit code $LASTEXITCODE"
    }
}

$requiredContainers = Get-RequiredContainers -ResolvedProfile $Profile
$apiPort = Get-ApiPort -ResolvedProfile $Profile
$healthScheme = Get-HealthScheme -ResolvedProfile $Profile

Write-PrewarmInfo "profile=$Profile distro=$Distro api_port=$apiPort scheme=$healthScheme"
Write-PrewarmInfo "required_containers=$($requiredContainers -join ',')"

if ($DryRun) {
    if (-not $SkipKeepAlive) {
        Write-PrewarmInfo "dry-run: would start or reuse a hidden WSL keepalive process before health checks"
    }
    Write-PrewarmInfo "dry-run: would wake WSL, wait for docker, verify container health, and use run-deploy.ps1 -SkipSync -SkipCommit if repair is needed"
    return
}

if (-not $SkipKeepAlive) {
    Ensure-WslKeepAlive -ResolvedProfile $Profile
}

Write-PrewarmInfo "waking WSL distro"
$null = Invoke-WslCommand -Command "pwd >/dev/null"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to wake WSL distro: $Distro"
}

if (-not (Wait-Until -Condition { Test-DockerReady } -TimeoutSeconds $DockerTimeoutSeconds -Description 'docker')) {
    throw "Docker did not become ready within ${DockerTimeoutSeconds}s"
}

Write-PrewarmInfo "docker is ready"

$healthReady = Wait-Until -Condition {
    (Test-RequiredContainersHealthy -RequiredContainers $requiredContainers) -and (Test-GatewayHealth -Port $apiPort -Scheme $healthScheme)
} -TimeoutSeconds $HealthTimeoutSeconds -Description 'AATS stack'

if ($healthReady) {
    Write-PrewarmInfo "AATS stack already healthy"
    return
}

Show-ContainerStates -RequiredContainers $requiredContainers

if ($SkipRepairDeploy) {
    throw "AATS stack is not healthy and repair deploy is disabled"
}

Invoke-RepairDeploy -ResolvedProfile $Profile

if (-not (Wait-Until -Condition {
    (Test-RequiredContainersHealthy -RequiredContainers $requiredContainers) -and (Test-GatewayHealth -Port $apiPort -Scheme $healthScheme)
} -TimeoutSeconds $HealthTimeoutSeconds -Description 'AATS stack after repair')) {
    Show-ContainerStates -RequiredContainers $requiredContainers
    throw "AATS stack did not recover after repair deploy"
}

Write-PrewarmInfo "AATS stack recovered successfully"
