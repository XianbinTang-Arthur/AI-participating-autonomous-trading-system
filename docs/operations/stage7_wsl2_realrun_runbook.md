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

> 2026-04-08 真跑修正：上面的根因分析只对了一半。实际跑通后看到 `market_gateway` /
> `execution_adapter` / `health_service` 这三个其实都在 **shared slice**（`_build_shared_slice`）
> 里装，所有 role 都有；`build_system_health` 真正会 NPE 的地方是在
> `recovery_view → ai_runtime → runtime.ai_service.status()` 链路上 ——
> `ai_service` 才是 gateway role 下 None 的那个（属于 decision slice）。
> 这导致 /system/health、/system/recovery、/system/mode 三个 CORE_SPECS endpoint 在
> gateway role 下全部 500，UI 完全打不开。详细处理见 §6.1 第 6 个 gap。

**修复方案 B**（runbook 起步时）：**新增** lightweight `/healthz` endpoint 给 docker healthcheck 专用，**不动现有** `/system/health`。
- `/healthz` 只返回 `{"status":"ok","process_role":"<role>"}`
- 不依赖任何 service，只读 `_resolved_process_role()` 这种纯环境量
- 200 = 进程活着、FastAPI lifespan 已就绪
- compose healthcheck 改成 curl `/healthz`

**为什么这样做**：
- docker healthcheck 真正需要的是 "liveness"，与 UI 看的全量诊断信息是两个目标 ——
  两个目标分两个 endpoint 是 12-factor 标准做法。这一点没变。
- 不让 docker healthcheck 依赖 `/system/health` 也避免了 UI 数据壳坏掉直接拖垮容器
  健康状态的二次故障耦合。

**真跑后追加修复**（2026-04-08，详见 §6.1.6）：
- 把 `aats/services/operator/runtime_queries.py:36 ai_runtime()` 加 None-guard，
  当 `runtime.ai_service is None` 时返回稳定 stub（`provider="not_loaded"`、
  `ai_service_loaded=False`、所有计数 0、所有嵌套结构存在），让 recovery_view /
  system_mode / system_health 三个调用链能在 gateway role 下完整跑通。
- UI 消费者用 `?? 0` / `|| "unknown"` / `?.foo` 安全访问 stub，整个 UI 加载链路恢复。

**修复点**：
1. `apps/api_gateway/main.py` 加一个 FastAPI 路由（已落地）
2. `deploy/wsl2-dev/docker-compose.aats.yml` aats-gateway healthcheck `test` 改成 `["CMD", "curl", "-fs", "http://localhost:8000/healthz"]`（已落地）
3. `aats/services/operator/runtime_queries.py:ai_runtime()` 加 None-guard stub（**真跑后追加**）
4. 配套单测 `tests/unit/test_runtime_queries.py::TestAiRuntimeStubWhenServiceMissing` 3 个 case

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

### 6.1 第 1 次真跑（2026-04-07/08）

- **时间**：2026-04-07 → 2026-04-08（跨 UTC 0 点完成）
- **环境**：WSL2 Ubuntu，docker 28.2.2，docker compose v2.40.3 binary，AATS image `aats-base:dev`
- **基础设施 6/6 healthy**：postgres / redis / nats / loki / grafana / jaeger
- **AATS 4/4 healthy**：gateway / market / decision / execution

- **真跑过程修了 5 个新 gap**（runbook 起步时未预见到）：

  1. **NATS duplicate-subscription bug**（最大 blocker）
     - 症状：`aats-decision` restart-loop，logs `nats.js.errors.Error: nats: JetStream.Error consumer is already bound to a subscription`
     - 根因：`aats/bootstrap/config.py` 里 `_subscribe_critical_handlers` 与 `_subscribe_observer_handlers` 同时给 critical-routed topic 各订一个 handler。HybridEventBus 路由这俩都进同一 NATS critical bus；NATS durable_name 由 `(consumer_role, topic)` 派生，第二次 subscribe 同一 (role, topic) 触发 binding 冲突。
     - 受影响 topic：`POSITION_TARGETS`、`PORTFOLIO_SNAPSHOTS`、`RECONCILIATION_REPORTS`
     - 为何 monolith 没暴露：InMemoryEventBus 容忍多 handler / topic（内部 list），路径完全不进 NATS。
     - 修复：在 `_wire_event_subscriptions` 里加 `_CollectingBus` 适配器——所有 subscribe 调用 buffer 进 dict，flush 时按 topic 聚合：单 handler 直通、多 handler 包成 fan-out 后只 subscribe 一次。`_subscribe_critical_handlers` / `_subscribe_observer_handlers` 零改动。
     - 单测：`tests/unit/test_collecting_bus.py` 6 个 case，覆盖单/多 handler、多 topic closure capture、flush 幂等、publish 直通、异常向上传播。

  2. **Dockerfile `WORKDIR /app` 默认 root:root**
     - 症状：aats-* 容器启动 `PermissionError: [Errno 13] Permission denied: 'logs'`
     - 根因：`aats/bootstrap/logging.py:_ensure_log_directories` 在 `/app` 下 mkdir `logs/runtime|debug|info|warning|error`，但 Dockerfile WORKDIR `/app` 是 docker 自动创建的 root:root 目录，aats user (UID 1000) 没写权限。
     - 修复：Dockerfile runtime 段 `USER aats` 之前加 `RUN chown aats:aats /app`，覆盖未来所有 runtime-created 子目录。

  3. **Jaeger badger volume 权限**
     - 症状：jaeger 启动 `Failed to init storage factory: Error Creating Dir: "/badger/key" error: mkdir /badger/key: permission denied`
     - 根因：bind mount `./jaeger/badger:/badger`，host 目录是 root:root，但 jaeger 镜像 runs as UID 10001。
     - 修复：`scripts/wsl_sudo.sh chown -R 10001:10001 deploy/wsl2-dev/jaeger/badger`

  4. **Grafana provisioning 配置冲突**
     - 症状：grafana restart-loop，`'folder' and 'folderUID' should be empty using 'foldersFromFilesStructure' option`
     - 根因：`grafana/provisioning/dashboards/dashboards.yml` 同时设了 `folder: AATS` + `folderUid` + `foldersFromFilesStructure: true`，Grafana 10.4.4 拒绝。
     - 修复：删掉 `folder` / `folderUid`，保留 `foldersFromFilesStructure: true`，按 files 子目录组织 folder。

  5. **docker compose plugin 缺失**
     - 症状：WSL2 docker 没装 compose plugin，`docker compose` 命令未找到。
     - 修复：下载 v2.40.3 binary 到 `~/.docker/cli-plugins/docker-compose`（user-local，无需 sudo）。
     - 副产物：创建 `scripts/wsl_sudo.sh` 把"从凭证文件读密码 → 注入 sudo stdin"封装成可复用脚本，避免密码在 bash 命令文本里出现。

  6. **`/system/health` 在 gateway-only role 下 500（runbook §1.2 修正）**
     - 症状：10 容器 healthy 之后 `curl http://localhost:8000/system/health` 返回
       500，traceback 顶到 `runtime_queries.py:37 ai_runtime → runtime.ai_service.status() → AttributeError 'NoneType'`。
     - 受牵连 endpoint：`/system/health`、`/system/recovery`、`/system/mode`（都通过
       `recovery_view → ai_runtime` 链路触发同一行）。这三个都在 UI `CORE_SPECS`
       （`aats/api/static/modules/store.js:97`），UI 在 gateway role 下整体加载失败。
     - 根因修正：runbook §1.2 起步时把根因写成 `market_gateway` / `execution_adapter`
       为 None，但实际跑通后看到这两个都在 shared slice 里装，所有 role 都有。真正
       为 None 的是 `ai_service`（属于 decision slice，在 gateway/market/execution role 下不装）。
     - 为何 monolith 没暴露：monolith 把所有 slice 都装，`ai_service` 永远不为 None。
     - 修复（最小改动）：`aats/services/operator/runtime_queries.py:36 ai_runtime()`
       开头加 None-guard。`runtime.ai_service is None` 时返回稳定 stub（`provider="not_loaded"`、
       `ai_service_loaded=False`、所有计数 0、嵌套 `failure_budget` / `outcome_policy` /
       `legacy_modes` 结构齐备）。下游 `recovery_view` 内嵌 + UI ai-view 全部用
       `.get()` / `?? 0` / `|| "unknown"` 安全访问 stub 字段，调用链路无 KeyError、
       无 NPE。
     - 同时给 loaded 路径也加 `ai_service_loaded=True` + `process_role` 字段，对称
       命名让 UI/审计能统一判断"AI 子系统是否在本进程"。
     - 单测：`tests/unit/test_runtime_queries.py::TestAiRuntimeStubWhenServiceMissing`
       3 个 case：stub 字段完整性、process_role 标签透传、settings 字段缺失兜底。
     - 防退化：跑了 `tests/integration/test_operator_api.py` 里 3 个直接 `client.get("/ai/runtime")`
       的 integration test（blocker_action_degrade_to_baseline、blocker_action_restore_ai、
       admin_can_select_ai_operating_mode），新增字段 `ai_service_loaded` / `process_role`
       不破任何已有契约。
     - **为什么不在 routes.py 路由层短路 limited 模式**：拦截点更高一层意味着每个 4 个
       slice-dependent endpoint（`/system/health` / `/system/recovery` / `/system/mode` /
       `/ai/runtime`）都要各自加 process_role 检测，而且要凭空发明一个 "limited" 数据
       壳。在根因 ai_runtime() 里加 stub 是单一改动点，覆盖全部 4 个 endpoint，stub
       字段语义诚实（"not_loaded" 而不是假装的 "limited"）。

  7. **`/system/blocker-control` 在 gateway-only role 下 500（gap 6 之后的二次发现）**
     - 症状：gap 6 修了 `runtime_queries.py:ai_runtime` 之后第 2 次跑容器，`curl /system/blocker-control`
       仍然 500，traceback 顶到 `blocker_control/service.py:83 _build_items →
       runtime.ai_service.status() → AttributeError 'NoneType'`。
     - 根因：直接读 `runtime.ai_service.status()` 的代码站点不止 `ai_runtime()` 一处。
       全仓 grep 拿到 6 个：
       - `aats/services/blocker_control/service.py:83`（GET 链，/system/blocker-control 直接受牵连）
       - `aats/services/governance_engine/recovery_posture.py:59` `_ai_requires_manual_review`（GET 链，
         `recovery_view` → `recovery_posture.assess()` → `_ai_requires_manual_review` → `.status()`）
       - `aats/services/operator/query_service.py:3911` `_ai_shadow_summary`（`/ai/overview` /
         `/ai/config-summary` 链）
       - `aats/services/operator/query_service.py:10046` `ai_review_restore`（POST mutate）
       - `aats/services/operator/query_service.py:10095` `set_ai_operating_mode`（POST mutate）
       - `aats/services/operator/query_service.py:10166` `ai_review_degrade_to_baseline`（POST mutate）
     - 修复策略：
       - **GET 链 3 处**（blocker_control / _ai_shadow_summary）：从 `runtime.ai_service.status()`
         切换到 `self.owner.ai_runtime()` / `self.ai_runtime()`，走已修过的 `RuntimeQueryFacade.ai_runtime()`
         拿 stub，所有 `.get()` / `bool(...)` 安全。`recovery_posture._ai_requires_manual_review`
         没有 facade 入口，加 `getattr(runtime, 'ai_service', None) is None → return False` 直接
         保证 gateway 进程对 AI 状态没有可见性时不报告 `ai_degraded_requires_manual_review`。
       - **POST mutate 3 处**：在方法顶加 `if self.runtime.ai_service is None: raise
         ValueError("ai_service_not_loaded_in_this_process_role")`。这些是 operator 主动操作
         endpoint，gateway role 下被调到本身就是误用，不应静默 stub，而应清晰拒绝。
     - 单测：
       - `tests/unit/test_recovery_posture.py::TestAiRequiresManualReviewNoneGuard` 4 个 case
         （None 短路、baseline_only fast-path、degraded 真路径、auto_downgraded 真路径）。
       - `tests/unit/test_blocker_control.py::test_build_items_does_not_crash_when_ai_service_is_missing`
         1 个 case（fake owner 模拟 gateway role）。
       - 既有 3 个 blocker_control 测试同步加 `ai_runtime=lambda: {}` 字段保持兼容。
     - 真跑验证：第 3 次 `docker compose up -d --force-recreate` 后 4 容器全 healthy，
       `curl http://127.0.0.1:8000/system/health` / `/system/mode` / `/system/recovery` /
       `/system/blocker-control` 全部 200，gateway 日志里没有任何 traceback / AttributeError。
     - 教训：第一次只看到 `runtime_queries.py` 那一行就动手修，没全仓 grep `runtime\.ai_service\.`。
       往后这种"调下游 status() 的 None 安全"修复必须全仓 grep + 逐个处理，不能局部修了
       就以为全链路都通了。

- **healthcheck 全绿验证**（10/10）：
  ```text
  NAME             STATUS                    HEALTH
  aats-decision    Up 21 seconds             healthy
  aats-execution   Up 21 seconds             healthy
  aats-gateway     Up 21 seconds             healthy
  aats-grafana     Up 23 minutes             healthy
  aats-jaeger      Up 29 minutes             healthy
  aats-loki        Up 30 minutes             healthy
  aats-market      Up 21 seconds             healthy
  aats-nats        Up 16 minutes             healthy
  aats-postgres    Up 30 minutes             healthy
  aats-redis       Up 30 minutes             healthy
  ```
- **`curl http://localhost:8000/healthz`**：`{"status":"ok","process_role":"gateway"}` (200)
- **subscription 健康度**：market 1 sub、decision 22 subs、execution 4 subs、gateway 0 subs（全部 `nats_subscription_registered`，每 (role, topic) 各一次，无重复 binding 错误）。所有 4 进程都到 `process_lifecycle_ready` + `process_lifecycle_heartbeat_started`。
- **`curl http://localhost:8000/system/health`**（gap 6 修复后）：200，`runtime_state="degraded"`（不是 `halted`/`blocked`），`subsystems.market_data` / `subsystems.execution_adapter` / `subsystems.account_state` 都是真实状态而非 stub。
- **结论**：#1 完成判定 §4 第 1、2、3、4 项全部达标。第 5 项（单测全套无退化）留给 1.7d 验证。

---

## 7. Changelog

- 2026-04-07：首版。基于 5d 装配 review 发现 3 个 gap（Dockerfile 缺 curl、`/system/health` gateway role NPE、3 daemon 无 healthcheck），列出修复清单 + 真跑命令序列 + 完成判定 + 回滚步骤。等用户审批 §1-§2 修复方案后实施 + 真跑。
- 2026-04-08：第 1 次真跑完成。10/10 容器 healthy。真跑里又额外修了 5 个 runbook 起步时未预见的 gap：NATS duplicate-subscription（_CollectingBus fan-out）、Dockerfile `/app` ownership、jaeger badger 权限、grafana provisioning 配置冲突、docker compose plugin 缺失。详见 §6.1。
- 2026-04-08（追加）：第 1 次真跑后又发现 §1.2 根因分析有偏差。`/system/health`、`/system/recovery`、`/system/mode` 三个 CORE_SPECS endpoint 在 gateway role 下 500 的真因是 `runtime_queries.py:ai_runtime` 直接 `.status()` 一个 None 的 `ai_service`，与 `market_gateway` / `execution_adapter` 无关（这俩在 shared slice）。在 `ai_runtime()` 加 None-guard 返回稳定 stub（`provider="not_loaded"`），下游 `recovery_view → system_mode → system_health` 全链路恢复 200。配套 3 个 unit test。同步修订 §1.2 与 §6.1.6 记录。
- 2026-04-08（再追加）：gap 6 修完之后第 2 次跑容器，`/system/blocker-control` 仍然 500。全仓 grep `runtime.ai_service.status()` 又找出 6 处直读站点，其中 GET 链 3 处（blocker_control、recovery_posture、_ai_shadow_summary）会被 `/system/health` 和 `/system/blocker-control` 链路触发，POST mutate 3 处（ai_review_restore / set_ai_operating_mode / ai_review_degrade_to_baseline）会被 operator 误调时 500。GET 链 2 处切换到 `self.owner.ai_runtime()` 走 facade stub，1 处（recovery_posture）加 None-guard 短路返回 False；POST mutate 3 处加方法顶 `raise ValueError("ai_service_not_loaded_in_this_process_role")` 显式拒绝。配套 5 个新 unit test，全仓 1232 unit + 26 ai-integration 全绿。第 3 次真跑 4 容器 healthy，CORE_SPECS 4 个 endpoint 全部 200。详见 §6.1.7。
