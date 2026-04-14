# AATS WSL2 Deploy Flow

## Scope
This reference is specific to the repository at:
`D:/文件/project/AIParticipatingAutonomousTradingSystem`

It documents the current deploy path implemented by:
- `scripts/deploy.sh`
- `scripts/sync_to_wsl2.sh`
- `deploy/wsl2-dev/docker-compose*.yml`
- `.codex/skills/wsl2-deploy/scripts/run-deploy.ps1`

## Preconditions
- Windows host with WSL2 available
- WSL distro available, usually `Ubuntu`
- WSL native checkout exists, usually `~/aats`
- Repo root contains:
  - `.env.wsl2`
  - `.env.spot` or `.env.spot.live` or `.env.derivatives` or `.env.derivatives.live`
- If `.env.wsl2` still lives at `deploy/wsl2-dev/.env.wsl2`, deployment works but the setup should be migrated

## Sync semantics
- `scripts/sync_to_wsl2.sh pull` syncs the Windows repo current `HEAD`
- It no longer assumes `main`
- It refuses to sync into a dirty WSL checkout
- It does not carry uncommitted Windows working tree changes

Implication:
- To deploy current Windows code, make sure the repo is committed first
- To deploy WSL-only current code, use `--skip-sync`

## Standard deploy pipeline
1. Resolve profile to:
   - overlay compose file
   - profile env file
   - required container set
2. Locate `.env.wsl2`
   - prefer repo root
   - allow legacy fallback under `deploy/wsl2-dev/`
3. Optional commit step
4. Optional sync step
5. `docker compose down`
6. `docker compose build`
7. `docker image prune -f`
8. infra up from `docker-compose.yml`
9. wait for Postgres readiness
10. sync Postgres password from `.env.wsl2`
11. app up with base compose + overlay
12. health gate:
   - gateway `/healthz`
   - every required app container must be `running healthy`
13. emit deploy report

## Profiles
### Four-process profiles
- `spot`
- `spot-live`
- `derivatives`
- `derivatives-live`

Required containers:
- `aats-gateway`
- `aats-market`
- `aats-decision`
- `aats-execution`
- `aats-rdp-daemon`

### Monolith fallback
- `derivatives-live-monolith`

Required containers:
- `aats-gateway`
- `aats-rdp-daemon`

## Recommended commands
### Deploy latest committed Windows HEAD
```powershell
.\scripts\deploy.sh --profile derivatives-live
```

### Deploy through the bundled PowerShell wrapper
```powershell
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives-live
```

### Deploy with auto-commit
```powershell
.\scripts\deploy.sh --profile derivatives-live --commit "deploy message"
```

### Deploy existing WSL checkout only
```powershell
.\scripts\deploy.sh --profile derivatives-live --skip-sync --skip-commit
```

## Validation commands
### Shell syntax
```powershell
wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/deploy.sh
wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/sync_to_wsl2.sh
```

### Unit tests
```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_deploy_scripts.py tests\unit\test_process_lifecycle_and_entries.py -q
```

### Compose config check
Inside WSL:
```bash
cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.yml -f docker-compose.aats.yml -f docker-compose.aats.derivatives-live.yml \
  --env-file ~/aats/.env.wsl2 \
  --env-file ~/aats/.env.derivatives.live \
  config >/dev/null
```

## Common failure modes
### `bash.exe` unavailable in PowerShell
Cause:
- PowerShell resolves `bash` to `C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\bash.exe`
- That file is a zero-length App Execution Alias stub, not Git Bash

Fix:
- prefer WSL `wsl -d Ubuntu bash ...`
- or call the real Git Bash explicitly, for example `D:\Git\Git\bin\bash.exe`
- or put the real Git Bash path ahead of `WindowsApps` in PATH
- or disable the Windows `bash.exe` App Execution Alias

### Deploy succeeded but wrong code is running
Check:
- Windows `git rev-parse HEAD`
- WSL `git -C ~/aats rev-parse HEAD`
- whether `--skip-sync` was used
- whether Windows changes were left uncommitted

### Gateway healthy but background slices failing
Check:
- `docker inspect` health for all required containers
- `docker compose logs aats-market`
- `docker compose logs aats-decision`
- `docker compose logs aats-execution`
- `docker compose logs aats-rdp-daemon`

### Env file confusion
Preferred layout:
- `~/aats/.env.wsl2`
- `~/aats/.env.derivatives.live`

Legacy fallback still accepted:
- `~/aats/deploy/wsl2-dev/.env.wsl2`
