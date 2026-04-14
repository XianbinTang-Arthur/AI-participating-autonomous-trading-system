---
name: wsl2-deploy
description: Use this when deploying, redeploying, or troubleshooting the AATS WSL2 Docker Compose stack in this repository, including scripts/deploy.sh, scripts/sync_to_wsl2.sh, env-file placement, profile selection, and post-deploy health validation.
---

# Purpose
Run the repository's WSL2 deployment flow safely and predictably.

# Use this skill for
- Running the standard WSL2 deploy flow for `spot`, `spot-live`, `derivatives`, `derivatives-live`, or `derivatives-live-monolith`
- Troubleshooting `scripts/deploy.sh` or `scripts/sync_to_wsl2.sh`
- Verifying whether `.env.wsl2` / profile env files are in the right place
- Validating that all required app containers are healthy after deploy

# Current repo conventions
- `.env.wsl2` single source of truth is the repo root: `~/aats/.env.wsl2`
- `scripts/deploy.sh` still accepts the legacy fallback `deploy/wsl2-dev/.env.wsl2`, but do not create new setups that way
- Profile env files live in the repo root, for example `~/aats/.env.derivatives.live`
- WSL2 native checkout is expected at `~/aats` unless `AATS_WSL2_PROJECT` overrides it
- For one-click PowerShell deploys, prefer the bundled wrapper:
  - [scripts/run-deploy.ps1](D:/文件/project/AIParticipatingAutonomousTradingSystem/.codex/skills/wsl2-deploy/scripts/run-deploy.ps1)

# Default workflow
1. Read the current deploy entrypoints before acting:
   - [scripts/deploy.sh](D:/文件/project/AIParticipatingAutonomousTradingSystem/scripts/deploy.sh)
   - [scripts/sync_to_wsl2.sh](D:/文件/project/AIParticipatingAutonomousTradingSystem/scripts/sync_to_wsl2.sh)
   - [deploy/wsl2-dev/README.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev/README.md)
   - [deploy/wsl2-dev/RUNBOOK.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev/RUNBOOK.md)
2. Confirm what profile the user actually wants. Default to `derivatives-live` only if the request is ambiguous and there is no safer local context.
3. Check whether the Windows repo has uncommitted changes.
4. If deployment should include current Windows changes, require a clean committed `HEAD` first.
5. If deploying WSL2's existing checkout only, use `--skip-sync` and state clearly that Windows working tree changes will not be included.
6. Run the deploy command.
7. Validate the result with the narrowest checks that match the chosen profile.

# Hard rules
- Do not claim that uncommitted Windows changes were deployed. They are not synced by design.
- Do not assume `main` is the branch to deploy. `scripts/sync_to_wsl2.sh pull` follows the current Windows `HEAD`.
- Do not treat gateway `/healthz` alone as sufficient success. For deploy success, all required app containers for the chosen profile must be healthy.
- Do not silently switch env-file locations. If the setup still uses legacy `deploy/wsl2-dev/.env.wsl2`, call that out.

# Required container sets by profile
- `spot`, `spot-live`, `derivatives`, `derivatives-live`
  - `aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon`
- `derivatives-live-monolith`
  - `aats-gateway aats-rdp-daemon`

# Canonical deploy commands
From the Windows repo root:

```powershell
.\scripts\deploy.sh --profile derivatives-live
.\scripts\deploy.sh --profile spot-live
.\scripts\deploy.sh --profile derivatives-live-monolith
```

With an auto-commit:

```powershell
.\scripts\deploy.sh --profile derivatives-live --commit "your message"
```

Deploy WSL2's existing checkout only:

```powershell
.\scripts\deploy.sh --profile derivatives-live --skip-sync --skip-commit
```

Using the bundled one-click wrapper:

```powershell
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives-live
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives-live -CommitMessage "deploy message"
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives-live -SkipSync -SkipCommit
```

# Manual verification after deploy
- Gateway liveness:
  - `curl http://127.0.0.1:<AATS_API_PORT>/healthz`
- Container health:
  - `wsl -d Ubuntu bash -lc "for c in <containers>; do docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $c; done"`
- Compose view:
  - `wsl -d Ubuntu bash -lc "cd ~/aats/deploy/wsl2-dev && docker compose ... ps"`

# When debugging failures
- If sync did not pick up expected code, inspect Windows `git status`, Windows `git rev-parse HEAD`, and WSL2 `git log -1`.
- If deploy passes gateway but workers fail, inspect per-container health and logs for `aats-market`, `aats-decision`, `aats-execution`, and `aats-rdp-daemon`.
- If env lookup fails, verify:
  - `~/aats/.env.wsl2`
  - `~/aats/.env.<profile>`
  - overlay compose file under `deploy/wsl2-dev/`
- If shell syntax checks are needed, prefer WSL:
  - `wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/deploy.sh`
  - `wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/sync_to_wsl2.sh`

# Validation after changing deploy logic
Run:
- `wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/deploy.sh`
- `wsl -d Ubuntu bash -n /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/scripts/sync_to_wsl2.sh`
- `.\.venv\Scripts\python.exe -m pytest tests\unit\test_deploy_scripts.py tests\unit\test_process_lifecycle_and_entries.py -q`
- a narrow compose config check in WSL for the affected profile

# References
- For the exact repo deploy flow and troubleshooting notes, read [references/deploy-flow.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/.codex/skills/wsl2-deploy/references/deploy-flow.md)
- For the post-deploy operator checks, read [references/post-deploy-checklist.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/.codex/skills/wsl2-deploy/references/post-deploy-checklist.md)
