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
1. Require an explicit simulation profile and reject all live profiles before side effects.
2. Resolve profile to:
   - overlay compose file
   - profile env file
   - required container set
3. Locate `.env.wsl2`
   - prefer repo root
   - allow legacy fallback under `deploy/wsl2-dev/`
4. Optional commit step
5. Optional sync step
6. Generate one non-secret runtime readiness generation from the deployed WSL HEAD, UTC time, process id, and random nonce
7. `docker compose build` before stopping the old stack; required generation interpolation is already present
8. `docker compose down`; nonzero is fatal
9. `docker image prune -f`
10. infra up with Compose `--wait`; nonzero is fatal
11. wait for Postgres readiness and sync its password without printing it
12. run the explicit root + RDP schema job; nonzero is fatal
13. app up with base compose + overlay; nonzero is fatal
14. health gate:
   - gateway `/healthz`
   - every required app container must be `running healthy`
15. write a non-overwriting, no-secret simulation evidence packet containing the same readiness generation
16. emit a simulation-only report; never claim trading-ready or production GO

## Profiles
### Enabled four-process profiles
- `spot`
- `derivatives`

Required containers:
- `aats-gateway`
- `aats-market`
- `aats-decision`
- `aats-execution`
- `aats-rdp-daemon`

All live profiles and the monolith live fallback are currently rejected with no override. Their Compose files remain future validation inputs, not deployable profiles.

## Recommended commands
### Deploy latest committed Windows HEAD
```powershell
.\scripts\deploy.sh --profile derivatives
```

### Deploy through the bundled PowerShell wrapper
```powershell
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives
```

### Deploy with auto-commit
```powershell
.\scripts\deploy.sh --profile derivatives --commit "deploy message"
```

### Deploy existing WSL checkout only
```powershell
.\scripts\deploy.sh --profile derivatives --skip-sync --skip-commit
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
AATS_RUNTIME_READINESS_GENERATION=static-config-check \
  docker compose -f docker-compose.yml -f docker-compose.aats.yml -f docker-compose.aats.derivatives.yml \
  --env-file ~/aats/.env.wsl2 \
  --env-file ~/aats/.env.derivatives \
  config >/dev/null
```

`static-config-check` is only for template parsing. Never use it to start or restart application containers; real simulation deployment generations are created by `scripts/deploy.sh` and recorded in evidence.

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
- `~/aats/.env.derivatives`

Legacy fallback still accepted:
- `~/aats/deploy/wsl2-dev/.env.wsl2`
