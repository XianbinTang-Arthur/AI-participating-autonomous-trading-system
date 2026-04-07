# Stage 7 WSL2 真跑 Runbook（4 进程拓扑 docker compose 实战验证）

## 文档定位

| 项目 | 内容 |
|---|---|
| 创建日期 | 2026-04-07 |
| 文档作用 | Stage 5d 4 进程拓扑装配完成后，在 WSL2 真跑 docker compose 把"装配完成"升级到"healthcheck 全绿"。包含 5d 装配里发现的 3 个 gap 修复方案 + 真跑命令序列 + 验证标准 + 回滚 |
| 关联 task | `docs/operations/multiprocess_refactor_roadmap.md` 第 2 节阶段 7 |
| 前置 tag | `pre-stage7-wsl2-realrun-v1`（本 runbook 起步前的安全网） |
| 完成 tag | `pre-stage7-wsl2-realrun-complete-v1`（4 容器 healthcheck 全绿后） |
| 维护责任 | 每次真跑后必须把 healthy/unhealthy 截图或日志摘要追加到 §6 实战记录 |

---

## 1. 5d 装配里发现的 3 个 gap

### 1.1 Gap A：Dockerfile runtime 段缺 `curl`

**症状**：`deploy/wsl2-dev/docker-compose.aats.yml` 第 79 行 gateway healthcheck 是 `["CMD", "curl", "-fs", "http://localhost:8000/system/health"]`，但 `deploy/wsl2-dev/Dockerfile` runtime 段（L57-89）只装了 `ca-certificates + tini`。`curl` 只在 builder 段（L37）装了，没复制到 runtime。

**后果**：gateway 容器启动后 healthcheck 永远 fail（exec error: curl not found），状态停留在 `unhealthy`，docker compose status 永远不会绿。

**修复方案 A**：在 runtime 段 apt-get 加 `curl`。代价：runtime 镜像增加约 3MB。

**修复点**：`deploy/wsl2-dev/Dockerfile` L65-69 的 `apt-get install` 列表加一行 `curl \`。

---

### 1.2 Gap B：`/system/health` 在 gateway-only role 下 NPE

**症状**：`aats/api/routes.py:91` 的 `system_health` 调 `aats/services/operator/runtime_queries.py:200 build_system_health`，后者在 L204、L206 直接访问：

```python
market = self.owner.runtime.market_gateway.status()       # L204
execution = self.owner.runtime.execution_adapter.readiness()  # L206
```

但 `aats/bootstrap/config.py:2574 _SLICE_REQUIRED_ROLES` 里 `market` slice 只在 `monolith / market` role 下装、`execution` slice 只在 `monolith / execution` role 下装。**gateway role 下 `runtime.market_gateway` 和 `runtime.execution_adapter` 都是 None**，调用会 AttributeError。

**后果**：即便 Gap A 修了，gateway healthcheck 也会跑出 500，状态仍然 unhealthy。

**修复方案 B**：**新增** lightweight `/healthz` endpoint 给 docker healthcheck 专用，**不动现有** `/system/health`。
- `/healthz` 只返回 `{"status":"ok","process_role":"<role>"}`
- 不依赖任何 service，只读 `_resolved_process_role()` 这种纯环境量
- 200 = 进程活着、FastAPI lifespan 已就绪
- compose healthcheck 改成 curl `/healthz`

**为什么不修 `/system/health`**：那个 endpoint 是给 UI / operator 看的，需要全量诊断信息。给它加 None 守卫会让它在 gateway role 下显示一堆 "n/a"，但 docker healthcheck 真正需要的只是"liveness"——两个目标分两个 endpoint 是 12-factor 标准做法。

**修复点**：
1. `apps/api_gateway/main.py` 加一个 FastAPI 路由（或者塞进 `aats/api/routes.py` 顶层 `@router.get("/healthz")`）
2. `deploy/wsl2-dev/docker-compose.aats.yml` aats-gateway healthcheck `test` 改成 `["CMD", "curl", "-fs", "http://localhost:8000/healthz"]`

---

### 1.3 Gap C：market / decision / execution 3 个 daemon 没有 healthcheck

**症状**：`docker-compose.aats.yml` aats-market / aats-decision / aats-execution 3 个 service 都没写 `healthcheck:` 段。3 个进程都是 `python -m apps.*.main` daemon，没有 HTTP listener。

**后果**：docker 永远显示这 3 个容器 `running`，无法区分"进程在跑且循环健康" vs "进程 hang 住但没退出" vs "GIL 死锁"。

**修复方案 C**：在 `aats/bootstrap/process_lifecycle.py` 加一个 background heartbeat task，每 5 秒 touch 一个 sentinel 文件 `/tmp/aats_<role>_heartbeat`。compose 用 shell healthcheck 检查文件 mtime 与当前时间差是否 < 30s。
- 优点：不需要新端口、不需要新依赖、能检测到 hang（mtime 不动）
- 非 root user `aats` UID 1000 写 `/tmp` 没权限问题
- 心跳频率 5s + 容忍 30s 给 GIL 阻塞 / 慢 GC 留 buffer

**为什么不每个 daemon 各自起一个 HTTP health server**：
- 每个进程多一个 端口意味着 compose 要为每个进程配端口映射 / network alias
- 多一份 server 启动/关闭逻辑要在 process_lifecycle 里维护
- 心跳文件方案 50 行能搞定，HTTP server 方案至少 200 行

**修复点**：
1. `aats/bootstrap/process_lifecycle.py` 加 `_heartbeat_loop(role, stop_event, interval=5)` 协程，run_process 内启动 + 在 finally 取消
2. `deploy/wsl2-dev/docker-compose.aats.yml` 给 aats-market / aats-decision / aats-execution 各自加：

   ```yaml
   healthcheck:
     test: ["CMD-SHELL", "test -f /tmp/aats_<role>_heartbeat && test $(($(date +%s) - $(stat -c %Y /tmp/aats_<role>_heartbeat))) -lt 30"]
     interval: 15s
     timeout: 5s
     retries: 5
     start_period: 30s
   ```

3. 配套单测：`tests/unit/test_process_lifecycle_and_entries.py` 加 1 个 test 验证 heartbeat 文件被周期性更新 + run_process 退出时清理（删文件）

---

### 1.4 流程 gap（无需改代码）：跨 compose project depends_on 不可用

**症状**：基础设施在 `deploy/wsl2-dev/docker-compose.yml`，AATS 在 `deploy/wsl2-dev/docker-compose.aats.yml`，两份 file 通过 `name: aats-dev` + 共享 `aats-dev_aats` external network 连通。但 `depends_on` 不能跨 compose project，所以 AATS compose 无法声明"等 postgres/nats healthy 才启动"。

**后果**：如果用户先 `up -d` AATS 而忘了起基础设施，AATS 容器会立即启动并 fail-fast 退出，然后被 `restart: unless-stopped` 不停拉起来 → 日志刷屏。

**应对**：不改代码，在本 runbook §3 严格强调启动顺序，并提供一个 `scripts/up_aats_4proc.sh` helper 串联两步（不是必须，runbook 里就说明命令序列即可）。

---

## 2. 修复实施清单

| 序号 | 文件 | 改动概述 | 改动量 |
|---|---|---|---|
| 修复 1 | `deploy/wsl2-dev/Dockerfile` | runtime 段 apt-get 加 `curl` | +1 行 |
| 修复 2 | `aats/api/routes.py`（或 `apps/api_gateway/main.py`） | 新增 `@router.get("/healthz")` 返回 `{"status":"ok","process_role":<role>}` | +10 行 |
| 修复 3 | `deploy/wsl2-dev/docker-compose.aats.yml` | aats-gateway healthcheck 改用 `/healthz` | -1+1 行 |
| 修复 4 | `aats/bootstrap/process_lifecycle.py` | 新增 `_heartbeat_loop` 协程 + run_process 内启动 + finally 清理 | +30 行 |
| 修复 5 | `deploy/wsl2-dev/docker-compose.aats.yml` | aats-market/decision/execution 各自加 healthcheck | +24 行 |
| 修复 6 | `tests/unit/test_process_lifecycle_and_entries.py` | 加 1 个 heartbeat test | +20 行 |
| 修复 7 | `tests/unit/test_routes_healthz.py` 或合并入现有 routes test | 加 1 个 /healthz 200 test，覆盖 4 个 process_role | +30 行 |

总改动量：约 116 行新增 + 2 行调整。**没有删除任何现有代码**。

---

## 3. 真跑命令序列（修复落地后由用户在 WSL2 内执行）

### 3.1 前置条件

- WSL2 Ubuntu 22.04 + Docker 28.2.2 + Python 3.12 venv（已就位，见 memory `reference_wsl2_dev_env.md`）
- 工作目录：`~/aats`（git checkout 同步到 `pre-stage7-wsl2-realrun-complete-v1` 之后的 commit）
- `.env.wsl2` 已从 `.env.wsl2.template` 复制并填好（POSTGRES_USER/PASSWORD/DB、NATS_URL、AATS_TIMEZONE 等）

### 3.2 命令序列

```bash
cd ~/aats/deploy/wsl2-dev

# 1) 基础设施先就绪：postgres / nats / redis / loki / jaeger / grafana
docker compose --env-file .env.wsl2 up -d

# 2) 等基础设施 healthy（最长 60s）
for i in {1..12}; do
  if docker compose --env-file .env.wsl2 ps --format json | python3 -c "
import json, sys
ok = True
for line in sys.stdin:
    svc = json.loads(line)
    if svc.get('Health') and svc['Health'] != 'healthy':
        ok = False
        print(svc['Service'], svc['Health'])
sys.exit(0 if ok else 1)
"; then echo OK; break; fi
  sleep 5
done

# 3) build AATS 镜像（约 2-4 分钟，看依赖 cache）
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 build

# 4) 起 4 个 AATS 容器
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d

# 5) 等 4 个 AATS 容器 healthy（最长 120s）
for i in {1..24}; do
  status=$(docker compose -f docker-compose.aats.yml --env-file .env.wsl2 ps --format '{{.Service}} {{.Health}}')
  echo "[$i/24] $status"
  if echo "$status" | grep -q unhealthy; then
    echo "FAIL: at least one container unhealthy"
    break
  fi
  if echo "$status" | grep -vq starting; then
    echo "ALL READY"
    break
  fi
  sleep 5
done

# 6) 最终验证：4 个容器都 healthy
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 ps
```

### 3.3 预期成功输出

```
NAME             IMAGE           COMMAND                  STATUS                   PORTS
aats-decision    aats-base:dev   "/usr/bin/tini -- py…"   Up X minutes (healthy)
aats-execution   aats-base:dev   "/usr/bin/tini -- py…"   Up X minutes (healthy)
aats-gateway     aats-base:dev   "/usr/bin/tini -- uv…"   Up X minutes (healthy)   127.0.0.1:8000->8000/tcp
aats-market      aats-base:dev   "/usr/bin/tini -- py…"   Up X minutes (healthy)
```

外加：
- `curl -s http://127.0.0.1:8000/healthz` 返回 `{"status":"ok","process_role":"gateway"}`
- `curl -s http://127.0.0.1:8000/system/health | jq .runtime_state` 返回 `"healthy"` 或 `"degraded"`（degraded 是预期的：第一次起来还没真有 portfolio 数据）
- 4 个容器 logs 各自最后 100 行无 ERROR / Traceback：
  ```bash
  for svc in aats-gateway aats-market aats-decision aats-execution; do
    echo "=== $svc ==="
    docker logs --tail 100 $svc 2>&1 | grep -iE 'error|traceback|exception' | head -20
  done
  ```

### 3.4 预期失败信号 + 可能原因

| 症状 | 可能原因 | 修法 |
|---|---|---|
| gateway 始终 starting → unhealthy | curl 没装 / `/healthz` 没注册 / FastAPI lifespan 卡住 | `docker logs aats-gateway` 看 traceback；再 `docker exec aats-gateway curl -v http://localhost:8000/healthz` 直接打 |
| 3 个 daemon 容器始终 starting → unhealthy | heartbeat 文件没生成 / process_lifecycle heartbeat task 没启动 | `docker exec aats-market ls -la /tmp/aats_market_heartbeat`；看 logs 有没有 heartbeat task 的初始化日志 |
| 容器频繁重启（restart count 飙升） | NATS 连接 fail-fast 退出 + restart unless-stopped 拉起 | 检查基础设施是否真的 healthy；`docker exec aats-gateway nslookup nats`；NATS_URL 是否对 |
| logs 出现 `OperationalError: connection refused (psycopg)` | postgres 未就绪 / 数据库未初始化 | `docker logs aats-postgres-1`；确认 `.env.wsl2` 的 POSTGRES_USER/PASSWORD/DB 与 compose 里 `${POSTGRES_USER:-aats}` 解析一致 |
| logs 出现 `nats.errors.NoServersError` | NATS 未起 / network 不通 | `docker network inspect aats-dev_aats` 确认 4 个 AATS 容器都连上了 |

---

## 4. 完成判定

满足以下**全部**条件，#1 真跑才算完成：

1. `docker compose -f docker-compose.aats.yml ps` 4 个容器全部 `Up (healthy)`
2. `curl http://127.0.0.1:8000/healthz` 返回 200 + `{"status":"ok","process_role":"gateway"}`
3. `curl http://127.0.0.1:8000/system/health` 返回 200 且 `runtime_state ∈ {healthy, degraded}`（不能是 halted/blocked）
4. 4 个容器各自最近 100 行 logs 无 ERROR / Traceback / Exception
5. 全套 1206 单元测试 + 修复 6/7 新增的测试仍然全过、无退化（在主机 venv 跑 `pytest tests/unit/`）

满足 1-5 后：
- 提交修复 commit
- 打 tag `pre-stage7-wsl2-realrun-complete-v1`
- 把本 runbook §6 实战记录补上当次的截图 / log 摘要
- 把 roadmap 第 2 节阶段 7 从"装配完成（待 docker 真跑）"升级为"完整（含 docker 真跑）"

---

## 5. 回滚

如果 #1 真跑反复失败、修不动，回滚步骤：

```bash
# 1) 容器全停 + 清掉本地 docker 数据卷（只清 AATS 应用，基础设施 volume 保留）
cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 down --remove-orphans
docker image rm aats-base:dev

# 2) 代码回滚到 #1 起步前
git reset --hard pre-stage7-wsl2-realrun-v1

# 3) 确认状态
git log --oneline -5
git status
```

回滚后，`pre-stage7-wsl2-realrun-v1` tag 仍然在，可以重新尝试，不会丢失任何东西。

---

## 6. 实战记录（每次真跑后追加）

### 6.1 第 1 次真跑（待补，由 1.6 步骤填入）

- 时间：
- 环境：
- 命令实际输出：
- 是否 healthcheck 全绿：
- 修复了哪些遗漏：

---

## 7. Changelog

- 2026-04-07：首版。基于 5d 装配 review 发现 3 个 gap（Dockerfile 缺 curl、`/system/health` gateway role NPE、3 daemon 无 healthcheck），列出修复清单 + 真跑命令序列 + 完成判定 + 回滚步骤。等用户审批 §1-§2 修复方案后实施 + 真跑。
