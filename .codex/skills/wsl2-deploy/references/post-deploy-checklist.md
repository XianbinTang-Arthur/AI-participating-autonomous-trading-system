# Post-Deploy Checklist

Use this after an enabled simulation deployment through `scripts/deploy.sh` or `run-deploy.ps1`. Current live profiles are blocked before side effects.

## 1. Confirm the intended code was deployed
- Windows:
  - `git rev-parse HEAD`
  - `git status --short`
- WSL:
  - `wsl -d Ubuntu bash -lc "cd ~/aats && git rev-parse HEAD && git status --short"`
- Expected:
  - WSL `HEAD` matches the intended committed Windows `HEAD`
  - If `--skip-sync` was used, verify you intentionally deployed the existing WSL checkout

## 2. Confirm gateway liveness
- `curl http://127.0.0.1:<AATS_API_PORT>/healthz`
- Expected:
  - HTTP 200
  - HTTP 200 is liveness only; it does not prove network isolation or trading readiness

## 3. Confirm required containers are healthy
### `spot`
- `aats-gateway`
- `aats-market`
- `aats-decision`
- `aats-execution`
- `aats-rdp-daemon`

### `derivatives`
- all `spot` containers above
- `aats-liquidations-daemon`
- `aats-microstructure-collector`

Check:
```powershell
wsl -d Ubuntu bash -lc "for c in aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector; do docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \$c 2>/dev/null || echo \"\$c missing\"; done"
```

Expected:
- every required container is `running healthy`

## 4. Check compose view

Read `runtime_readiness_generation` from the emitted simulation evidence packet, then pass that exact non-secret value for this read-only view:

```powershell
wsl -d Ubuntu bash -lc "cd ~/aats/deploy/wsl2-dev && AATS_RUNTIME_READINESS_GENERATION='<generation-from-evidence>' docker compose -f docker-compose.yml -f docker-compose.aats.yml -f docker-compose.aats.derivatives.yml --env-file ~/aats/.env.wsl2 --env-file ~/aats/.env.derivatives ps"
```

Expected:
- required app containers present
- no restart loop

## 5. Confirm the simulation evidence boundary
- Open only the emitted JSON path under `deploy/wsl2-dev/runtime/deployment-evidence/`.
- Expected: intended commit, immutable image IDs, selected simulation profile, schema job passed, non-empty `runtime_readiness_generation`, all required containers healthy, and every `gateway_published_bindings[].host_ip` equal to `127.0.0.1` or `::1`.
- Confirm gateway/market/decision/execution logs report the same generation and all-ready peers from that generation. This is startup provisioning evidence, not continuous task freshness.
- Required boundary: `production_ready=false`, `trading_ready=false`, and explicit runtime unknowns.

## 6. If deploy failed
- Gateway down:
  - `docker compose ... logs aats-gateway --tail 100`
- Background slices unhealthy:
  - `docker compose ... logs aats-market --tail 100`
  - `docker compose ... logs aats-decision --tail 100`
  - `docker compose ... logs aats-execution --tail 100`
  - `docker compose ... logs aats-rdp-daemon --tail 100`
  - for `derivatives`, also inspect `aats-liquidations-daemon` and `aats-microstructure-collector`
- Env confusion:
  - verify `~/aats/.env.wsl2`
  - verify `~/aats/.env.<profile>`
  - verify selected profile overlay exists
