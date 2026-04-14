# Post-Deploy Checklist

Use this after `scripts/deploy.sh` or `scripts/run-deploy.ps1` finishes.

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

## 3. Confirm required containers are healthy
### Four-process profiles
- `aats-gateway`
- `aats-market`
- `aats-decision`
- `aats-execution`
- `aats-rdp-daemon`

### Monolith fallback
- `aats-gateway`
- `aats-rdp-daemon`

Check:
```powershell
wsl -d Ubuntu bash -lc "for c in aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon; do docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \$c 2>/dev/null || echo \"\$c missing\"; done"
```

Expected:
- every required container is `running healthy`

## 4. Check compose view
```powershell
wsl -d Ubuntu bash -lc "cd ~/aats/deploy/wsl2-dev && docker compose -f docker-compose.yml -f docker-compose.aats.yml -f docker-compose.aats.derivatives-live.yml --env-file ~/aats/.env.wsl2 --env-file ~/aats/.env.derivatives.live ps"
```

Expected:
- required app containers present
- no restart loop

## 5. If deploy failed
- Gateway down:
  - `docker compose ... logs aats-gateway --tail 100`
- Background slices unhealthy:
  - `docker compose ... logs aats-market --tail 100`
  - `docker compose ... logs aats-decision --tail 100`
  - `docker compose ... logs aats-execution --tail 100`
  - `docker compose ... logs aats-rdp-daemon --tail 100`
- Env confusion:
  - verify `~/aats/.env.wsl2`
  - verify `~/aats/.env.<profile>`
  - verify selected profile overlay exists
