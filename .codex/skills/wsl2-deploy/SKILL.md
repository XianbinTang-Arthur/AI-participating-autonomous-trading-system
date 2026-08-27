---
name: wsl2-deploy
description: Use this when deploying, redeploying, or troubleshooting the AATS WSL2 Docker Compose stack in this repository, including scripts/deploy.sh, scripts/sync_to_wsl2.sh, env-file placement, profile selection, and post-deploy health validation.
---

# Purpose
Run the repository's WSL2 deployment flow safely and predictably.

# Use this skill for
- Running the current standard WSL2 deploy flow for simulation `spot` or `derivatives`
- Explaining why `spot-live`, `derivatives-live`, and `derivatives-live-monolith` are blocked while production is NO-GO
- Troubleshooting `scripts/deploy.sh` or `scripts/sync_to_wsl2.sh`
- Verifying whether `.env.wsl2` / profile env files are in the right place
- Validating that all required app containers are healthy after deploy

# Current repo conventions
- `.env.wsl2` single source of truth is the repo root: `~/aats/.env.wsl2`
- `scripts/deploy.sh` still accepts the legacy fallback `deploy/wsl2-dev/.env.wsl2`, but do not create new setups that way
- Profile env files live in the repo root; current deployment uses only the selected simulation profile file
- `scripts/deploy.sh` has no default profile and rejects every live profile before external side effects; there is no override
- Each standard simulation deploy generates one non-secret runtime readiness generation after sync; every required app process for the selected profile, Compose interpolation, Redis ready keys, and evidence packet must agree on it
- WSL2 native checkout is expected at `~/aats` unless `AATS_WSL2_PROJECT` overrides it
- For one-click PowerShell deploys, prefer the bundled wrapper:
  - [scripts/run-deploy.ps1](D:/文件/project/AIParticipatingAutonomousTradingSystem/.codex/skills/wsl2-deploy/scripts/run-deploy.ps1)

# Default workflow
1. Read the current deploy entrypoints before acting:
   - [scripts/deploy.sh](D:/文件/project/AIParticipatingAutonomousTradingSystem/scripts/deploy.sh)
   - [scripts/sync_to_wsl2.sh](D:/文件/project/AIParticipatingAutonomousTradingSystem/scripts/sync_to_wsl2.sh)
   - [deploy/wsl2-dev/README.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev/README.md)
   - [deploy/wsl2-dev/RUNBOOK.md](D:/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev/RUNBOOK.md)
2. Confirm what simulation profile the user wants. If ambiguous, default to `derivatives`; never infer or select live.
3. Check whether the Windows repo has uncommitted changes.
4. If deployment should include current Windows changes, require a clean committed `HEAD` first.
5. If deploying WSL2's existing checkout only, use `--skip-sync` and state clearly that Windows working tree changes will not be included.
6. If the user requested live, stop and report the repository NO-GO gate; do not bypass it. Otherwise run the simulation deploy command.
7. Validate the result with the narrowest checks that match the chosen profile.

# Hard rules
- Do not claim that uncommitted Windows changes were deployed. They are not synced by design.
- Do not assume `main` is the branch to deploy. `scripts/sync_to_wsl2.sh pull` follows the current Windows `HEAD`.
- Do not treat gateway `/healthz` alone as sufficient success. For deploy success, all required app containers for the chosen profile must be healthy.
- Do not silently switch env-file locations. If the setup still uses legacy `deploy/wsl2-dev/.env.wsl2`, call that out.
- Do not remove, bypass, or reinterpret the live NO-GO gate. `--yes` only confirms a dirty Windows/WSL mismatch case and is not live approval.
- A successful simulation evidence packet always has `production_ready=false` and `trading_ready=false`.
- Do not manually invent a persistent readiness generation. Read-only Compose inspection must use the generation recorded by the current deployment evidence packet; a static config-only check may use an explicit disposable value.

# Required container sets by profile
- `spot`
  - `aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon`
- `derivatives`
  - `aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector`
- Future `derivatives-live` contract, currently unreachable
  - `aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector`
- Future `derivatives-live-monolith` contract, currently unreachable
  - `aats-gateway aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector`

# Canonical deploy commands
From the Windows repo root:

```powershell
.\scripts\deploy.sh --profile derivatives
.\scripts\deploy.sh --profile spot
```

With an auto-commit:

```powershell
.\scripts\deploy.sh --profile derivatives --commit "your message"
```

Deploy WSL2's existing checkout only:

```powershell
.\scripts\deploy.sh --profile derivatives --skip-sync --skip-commit
```

Using the bundled one-click wrapper:

```powershell
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives -CommitMessage "deploy message"
.\.codex\skills\wsl2-deploy\scripts\run-deploy.ps1 -Profile derivatives -SkipSync -SkipCommit
```

# Manual verification after deploy
- Gateway liveness:
  - `curl http://127.0.0.1:<AATS_API_PORT>/healthz`
- Container health:
  - `wsl -d Ubuntu bash -lc "for c in <containers>; do docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $c; done"`
- Compose view:
  - pass `AATS_RUNTIME_READINESS_GENERATION=<value from the emitted evidence packet>` to the read-only `docker compose ... ps` invocation; required interpolation otherwise fails by design
- Evidence packet:
  - verify the emitted file under `deploy/wsl2-dev/runtime/deployment-evidence/` has the intended commit/image/profile/readiness generation, Gateway loopback published binding, and explicitly remains non-production

# When debugging failures
- If sync did not pick up expected code, inspect Windows `git status`, Windows `git rev-parse HEAD`, and WSL2 `git log -1`.
- If deploy passes gateway but workers fail, inspect per-container health and logs for every required container; for `derivatives` this includes `aats-liquidations-daemon` and `aats-microstructure-collector` in addition to the core processes.
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
