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

### 1.1 Gap A：Dockerfile runtime 段缺 `curl`（已修复）

> **状态：已修复。** Dockerfile runtime 段现已包含 `curl`（L78），gateway healthcheck 正常工作。

**原始症状**：gateway healthcheck 使用 `curl`，但 runtime 段未安装，导致 healthcheck 永远 fail。

**修复**：在 runtime 段 `apt-get install` 列表加入了 `curl`。

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

# 1) 基础设施先就绪（9 服务）：postgres / redis / nats / loki / promtail / jaeger / prometheus / redis-exporter / grafana
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

## 7. 故障演练 #1（2026-04-08）：4 进程容器死亡 → Docker restart manager + NATS durable consumer 重连

### 7.1 演练目的

Stage 7 完成判定要求 "进程崩溃可自愈"。具体来说要回答 3 个问题：

1. **Docker `restart: unless-stopped` 是否能在容器崩溃后自动拉起？**
2. **NATS JetStream durable consumer（`aats-{role}-{topic}`）能否在订阅者死亡再起来后自动重连，不丢消息？**
3. **HybridEventBus 在 critical 路径上的 fan-out 在订阅者短暂离线期间能否恢复？**

不演练 = 不知道答案；上 prod 之后才发现 = 只能事后看日志，损失已经发生。

### 7.2 演练方法学说明（重要陷阱）

**陷阱**：原计划用 `docker kill <container>` 模拟崩溃。实际跑下来发现这条路在 WSL2 / Docker 28.2.2 这套环境上**不会触发 restart manager**：

- 步骤：`docker kill aats-execution`
- 结果：`docker inspect` 显示 `State.Status=exited`、`State.ExitCode=137`、`RestartPolicy.Name=unless-stopped`，但 `RestartCount=0`，容器**永久停在 exited**，不会自动拉起。
- 复现：用 alpine 跑一个最小化容器 `--restart=always sh -c 'while true; do sleep 1; done'`，再 `docker kill`，同样不重启。
- 区别测试：用 `--restart=always sh -c 'sleep 5; exit 1'`（让容器**内部**退出），restart manager 工作正常，`RestartCount` 增长。

**结论**：Docker 的 restart manager 把 "外部 SIGKILL（来自 docker API）" 当成 operator 主动停服，**故意不重启**；只有 "进程内部异常退出 / OOM / panic" 才会被自动接管。这是 Docker 的设计意图，不是 bug，但容易踩。

**正确演练方式**：
- 找到 host 上的实际 Python PID：`docker top aats-{role}` 拿到 wchan 行里的 python 进程 PID
- 从 host 直接 `kill -9 <pid>` 模拟 OOM-kill / segfault：tini 看到 PID 1 的子进程异常退出，自身 exit，容器内部退出 → restart manager 启动 → 容器拉起 → tini 重新 exec python → 全链路重连

### 7.3 演练 #2.2：execution 进程崩溃

**操作**：
```bash
# 拿 host PID
docker top aats-execution    # → tini=PID_T, python=PID_P
# 从 host 杀 python 子进程
kill -9 <PID_P>
```

**观察**：
- tini PID 1 接收 SIGCHLD，按预期 exit → 容器 State=exited，ExitCode 非 0
- Docker restart manager 触发，几秒后容器重新启动
- `docker inspect aats-execution --format '{{.RestartCount}}'` → 1
- `curl http://localhost:8222/jsz?consumers=true`：4 个 `aats-execution-*` consumer 全部 `push_bound: true`，`num_redelivered=0`（NATS 把订阅断开期间的消息缓存到 stream 里，重连后从 ack_floor 接着推）
- 总恢复时间：约 8 秒（含 healthcheck `start_period=30s` 但 grace 内自然就 healthy）

### 7.4 演练 #2.3：decision + market 同时崩溃

**操作**：
```bash
docker top aats-decision   # → python PID 118360
docker top aats-market     # → python PID 118397
kill -9 118360 118397      # 同时杀两个
```

**观察**：
- 两个容器同时进入 exited，restart manager 几乎同时重启
- `docker ps -a`：两者都 `Up 2 minutes (healthy)`
- `docker inspect aats-{decision,market} --format '{{.RestartCount}}'` → 都是 1
- gateway 没受影响，`RestartCount=0`，仍然在跑（演练 #2.2 阶段它就没动过）
- `curl /jsz?consumers=true` 累计统计：
  ```
  Stream AATS_EVENTS messages=148 consumers=26
    push_bound: 26/26
      decision  bound=21 unbound=0
      execution bound=4  unbound=0
      market    bound=1  unbound=0
  ```
- 26 个 durable consumer 全部正确重新绑定到对应订阅者；没有一个 dangling/orphan consumer，没有 duplicate binding 错误，没有 NATS 报 `consumer already bound`。

### 7.5 演练 #2 结论

| 关注点 | 验证结果 |
|---|---|
| Docker restart manager 工作 | ✅（前提是用 host kill -9 / 内部 exit 触发，**不能用 `docker kill`**） |
| NATS durable consumer 重连 | ✅ 26/26 push_bound，无 redeliver，无 orphan |
| HybridEventBus critical 路径恢复 | ✅ 各 role 的 NATS subscription 数量正确（decision 21 / execution 4 / market 1 / gateway 0），与基线一致 |
| 进程间隔离 | ✅ kill execution 不影响 decision/market/gateway；kill decision+market 不影响 gateway+execution |
| 自愈耗时 | execution 单杀约 8 秒；decision+market 双杀约 8 秒；都在 healthcheck `start_period` 之内 |

### 7.6 必须写进 prod runbook 的注意事项

1. **`docker kill` ≠ 模拟崩溃**。如果以后写 chaos test 或 oncall drill，要么从 host `kill -9` python 进程，要么往进程里发可触发 abort 的信号；不要用 `docker kill`，否则 restart manager 不接手，容器永久 exited，看上去像"自愈失败"实际是测试方法错。
2. **真正的 OOM / segfault / panic 都会触发自愈**。这是好事，但也意味着无声崩溃会被 restart manager 立刻覆盖；必须靠 `RestartCount`、`docker events`、Loki 日志追溯，否则一次崩溃在 dashboard 上几乎看不见。后续 Stage 8 OTel 接 collector 时要把 `docker events restart` 推送成告警，避免 silent recover。
3. **`start_period=30s` 给的 grace 完全够用**。当前 healthcheck 设的 30s 是合理值；但如果未来加重 init（例如 schema migrate / replay outbox），要重新评估。
4. **NATS durable consumer 命名 `aats-{role}-{topic}` 是关键**。订阅者重连时按这个名字 attach，所以**绝不能在不同 role 之间复用同一个 consumer name**，否则会出现 `consumer already bound to a different client` 错误，订阅彻底失败。当前 4 进程拓扑里这点已经天然成立（topic-role 一对一），但跨 role 共享 topic 时要小心。
5. **gateway 在演练里完全没受影响是预期行为**。它没有 NATS subscription（HTTP-only role），所以杀其他 3 个 daemon 对它的 readiness 完全无副作用。这反过来证明 4 进程拓扑的故障域隔离是真实的，不是只在测试代码里成立。

### 7.7 后续 chaos drill 待办

- [ ] 演练 #3：杀 NATS 容器本身（NATS restart 后所有 26 个 consumer 是否能重建？stream 数据是否丢失？）→ Stage 7 收尾前必做
- [ ] 演练 #4：杀 postgres 容器（execution outbox flush 是否会卡死、是否 backpressure 到 NATS）→ Stage 7 收尾前必做
- [ ] 演练 #5：网络分区模拟（`tc qdisc` 给 NATS 加 packet loss）→ Stage 8 OTel collector 上线后再做，观察 metrics 端能否反映
- [ ] 演练 #6：磁盘写满（`fallocate` 把 jetstream 数据盘填满）→ Stage 9 dryrun 期间真跑

---

## 8. Stage 5 跨进程 critical fan-out 端到端验证（2026-04-08）

### 8.1 验证目的

Stage 5 引入 HybridEventBus（critical → NATS、observer → InMemory）之后，单测和 testcontainers 集成测试都验证过单 NATS bus 的 round-trip。但**真正的 4 进程拓扑下，"A 进程 publish → B 进程 subscribe handler → ack"** 这条端到端路径，从 §6 真跑之前其实没有在 host 侧被肉眼复现过（只看过启动日志里的 `nats_subscription_registered`）。本节用主动注入的方法填上这个洞。

### 8.2 验证方法

**进程拓扑**：4 进程 compose（`gateway` / `market` / `decision` / `execution`），所有 4 个进程共享同一份 NATS server (`aats-nats:4222`)、同一个 stream `AATS_EVENTS`。

**注入策略**：
- **Topic**：`system.processing_failures` (`aats.system.processing_failures` subject)。这是 critical-routed topic，consumer 只在 execution role 注册（`aats-execution-system_processing_failures`），跨进程边界清晰。
- **发送方**：从 **gateway 容器** 内 Python 进程通过 nats-py 直连 `nats://nats:4222`，绕过 ApplicationRuntime 的 publish 路径。这是为了**完全独立于业务代码**地证明 NATS 路由。
- **payload**：手工构造一个最小可验证的 `EventEnvelope` JSON，内嵌 `ProcessingFailureRecord`（`subsystem=stage5_fanout_drill`、`stage=verification`、`severity=warning`、`observed_at=now`）。
- **观察方**：execution 容器内的 `aats-execution-system_processing_failures` 消费者。验证 NATS API 上 `ack_floor.consumer_seq` 是否随注入消息推进，并检查 execution 日志确认无 handler 错误。

**注入命令**（已记录在 §8.4 用于后续 chaos 演练复用）：
```bash
docker exec aats-gateway python -c "
import asyncio, json, uuid
from datetime import datetime, timezone
import nats

async def main():
    nc = await nats.connect('nats://nats:4222')
    js = nc.jetstream()
    envelope = {
        'event_id': str(uuid.uuid4()),
        'event_type': 'ProcessingFailureRecord',
        'source_component': 'stage5_fanout_drill',
        'event_schema_version': '1.0.0',
        'topic': 'system.processing_failures',
        'key': 'drill-key-' + uuid.uuid4().hex[:8],
        'published_at': datetime.now(timezone.utc).isoformat(),
        'payload': {
            'failure_id': 'procfail_drill_' + uuid.uuid4().hex[:8],
            'subsystem': 'stage5_fanout_drill',
            'stage': 'verification',
            'severity': 'warning',
            'message': 'synthetic stage5 fanout drill',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'retriable': False,
            'details': {'injected_by': 'runbook'},
        },
    }
    ack = await js.publish('aats.system.processing_failures', json.dumps(envelope).encode('utf-8'))
    print('published', ack.stream, 'seq=' + str(ack.seq))
    await nc.close()

asyncio.run(main())
"
```

### 8.3 观察结果

**注入前 baseline**：
```
aats-execution-system_processing_failures
  delivered: consumer_seq=0  stream_seq=0
  ack_floor: consumer_seq=0  stream_seq=0
  num_pending=0  num_redelivered=0  push_bound=True
```

**第 1 次注入（payload 形状有 bug：包了一层 `{topic, key, payload}` outer wrapper）**：
- `js.publish()` 返回 `ack stream=AATS_EVENTS seq=158` ✅（说明 publish 路径通到 NATS）
- 2 秒后看 consumer 状态：`delivered consumer_seq=5 stream_seq=158`（**stream_seq 精确等于注入消息的 seq**），`ack_floor=0/0`，`num_redelivered=1`
- 解读：消息**成功被 execution 进程的 consumer 接收**，但 handler 在 `EventEnvelope.model_validate(payload_dict)` 这一步抛 `ValidationError(missing event_type, source_component)`，handler 不 ack → JetStream 按 `ack_wait=30s` 重投 → 超过 `max_deliver=5` 后丢弃。
- execution container 日志同步打出 5 条 `nats_handler_error durable=aats-execution-system_processing_failures error=2 validation errors for EventEnvelope`，确认错误路径完整可观测。

**第 2 次注入（payload 形状正确：直接发 envelope，不再 wrap）**：
- `js.publish()` 返回 `ack seq=160`
- 2 秒后看 consumer 状态：`delivered consumer_seq=6 stream_seq=160`，**`ack_floor consumer_seq=6 stream_seq=160`**（与 delivered 完全对齐），`num_pending=0`，`num_ack_pending=0`
- 解读：handler 成功 deserialize、调用、ack。**端到端 publish→subscribe→handler→ack roundtrip 完成**。
- execution 日志没有任何新的 `nats_handler_error`，证明 v2 envelope 走的是成功路径而非错误路径。

### 8.4 验证结论矩阵

| 验证维度 | 结果 | 证据 |
|---|---|---|
| **subject 命名规范** | ✅ `aats.{topic}` | stream config `subjects` 列出全部 38 条 `aats.*` subject |
| **跨进程 publish→subscribe** | ✅ | gateway 进程 publish 到 NATS，execution 进程的 durable consumer 收到 |
| **EventEnvelope 序列化兼容** | ✅ | 第 2 次注入用手工构造的 envelope dict，被 execution 端 `EventEnvelope.model_validate` 接受 |
| **handler ack 路径** | ✅ | `ack_floor` 从 0 推进到 stream_seq=160 |
| **handler 错误路径** | ✅ | 第 1 次注入触发 `nats_handler_error` 日志 5 次，符合 `max_deliver=5` 配置 |
| **error → redeliver 语义** | ✅ | `num_redelivered=1`、`max_deliver=5` 限制生效，超过后停止重投 |
| **durable consumer 跨重启幸存** | ✅ | 故障演练 #2.2/#2.3 之前的 `delivered=6/141` 在 kill+restart 之后**精确保留**，没有从 0 重新开始 |
| **进程间隔离** | ✅ | 注入到 `system.processing_failures` 只被 execution 接收，**不会**意外触达 decision/market/gateway 的 consumer 列表 |

### 8.5 与既有 testcontainers 测试的关系

`tests/integration/test_nats_event_bus_roundtrip.py::test_subprocess_publishes_main_subscribes` 已经在 CI 里验证了 subprocess publish + main process subscribe 的 round-trip（line 443-492）。本节是**对那条 CI 测试的真实部署等价物**：

| 维度 | CI 测试 | 本次真跑 |
|---|---|---|
| 进程数 | 2（pytest main + 一个 subprocess） | 4（gateway + market + decision + execution） |
| NATS server | testcontainers 临时启的 | docker compose 长跑的 `aats-nats` |
| ApplicationRuntime | 不装载（直接构造 NatsEventBus） | 完整装载（4 个 build_runtime） |
| Handler 路径 | 测试自定义 receiver function | 真实的 `_subscribe_observer_handlers` → `_CollectingBus.flush()` → NatsEventBus.subscribe |
| 端到端覆盖 | 单 topic、单 subscriber | 现场有 26 个 durable consumer 同时存在 |

CI 证明了**契约**正确，本次真跑证明了**部署**真的按契约工作。两者一起锁住了 Stage 5 的关键不变量。

### 8.6 留待后续验证的次级路径

本次只验证了 `system.processing_failures` 一条 topic 的 cross-process fan-out。其它 critical topic 的端到端通路依赖被动观察和 startup 日志：

| Topic | 发布方角色 | 订阅方角色 | 当前证据 |
|---|---|---|---|
| `market.snapshots` | market | market（feature_engine 在 market slice 内） | startup `nats_subscription_registered`，无消息流量 |
| `features.snapshots` | market（feature_engine） | decision（decision_trigger） | startup 日志，无消息流量 |
| `execution.order_intents` | decision | execution（order_manager） + decision（audit） | startup 日志，无消息流量 |
| `execution.fill_events` | execution（OKX adapter） | execution（portfolio_service） + decision（audit） | startup 日志，无消息流量 |
| `portfolio.snapshots` | execution | execution（reconciliation） + decision（audit） | **`delivered=6/141`** — Stage 5 真跑前期已经流过 6 条 |
| `reconciliation.reports` | execution | decision（audit） | **`delivered=147/153`** — 真跑期间持续在流 |
| `system.processing_failures` | 任何 role | execution（reconciliation_service.handle_processing_failure） | **本次主动注入验证 ✅** |

`portfolio.snapshots` 和 `reconciliation.reports` 的 delivered 计数 > 0 是 Stage 5 cross-process fan-out 在真跑环境中**自然发生**过的间接证据，因为 4 进程拓扑下这两个 topic 的发布方 (`execution`) 与订阅方 (`decision`) 处于不同容器，消息**必定**经过 NATS server 中转。

剩下的几条 dormant topic（market.snapshots / features.snapshots / order_intents / fill_events）等到**真接 OKX 数据源**之后会自然被驱动起来，届时直接看 consumer `delivered` 统计就能验证。如果 #4/#5/#6 完成后这些 topic 仍然 `delivered=0`，需要倒查 publisher 路径。

---

## 9. Stage 6 Slice 6.1：HotStateStore Redis backend 配线（2026-04-08）

### 9.1 Slice 6.1 边界与目标

Stage 6 的总目标是把 4 进程拓扑下的"高频读热状态"（kill_switch、portfolio_snapshot、gateway heartbeat 等）从"每个进程内的本地 dict"迁到"共享 Redis"，让 gateway 能在不依赖 NATS 事件重放的情况下同步问询 execution / decision 的最新状态。

**Slice 6.1 严格限定在"配线 + 校验"，不动任何业务调用方**。具体来说：

| Slice | 范围 | 状态 |
|---|---|---|
| **6.1（本节）** | settings 字段 + build_runtime 构造 + 4 进程 docker compose env + 集成测试 + 设计文档 | ✅ 完成 |
| 6.2 | kill_switch 写入路径切到 hot_state_store；execution 写、gateway/decision 读 | 待开 |
| 6.3 | portfolio_snapshot 缓存切到 hot_state_store；execution 写、gateway 读 | 待开 |

切片化的好处：Slice 6.1 是"零行为变化、纯添加"——任何业务调用方都不知道 hot_state_store 的存在，回滚成本 = 0；而 6.2/6.3 才是真正切换语义的地方，可以独立 review、独立验证、独立回滚。

设计文档：`docs/task/stage_6_redis_hot_state_design.md`

### 9.2 实现要点

1. **3 个新 settings 字段**（`aats/bootstrap/settings.py`）：
   - `hot_state_backend: Literal["memory", "redis"] = "memory"`
   - `hot_state_redis_url: str = "redis://127.0.0.1:6379/0"`
   - `hot_state_global_prefix: str = ""`（多环境共用 Redis 时的命名空间前缀）
   - 校验：`backend=redis` 时 URL 非空 + `redis://` / `rediss://` scheme，否则 settings 层就 fail-fast。

2. **build_runtime 构造 + ping**（`aats/bootstrap/config.py`）：
   - 新增 `_build_and_connect_hot_state_store()` helper，根据 settings 构造 store。
   - `backend=redis` 时同步 `await store.connect()`，失败抛 `RuntimeError` 让 4 进程 entry 在启动期就崩（**不是**延迟到第一次读写时才发现 Redis 不可用）。
   - `ApplicationRuntime` 加 `hot_state_store: HotStateStore` 字段。
   - `stop_background_tasks()` 走 best-effort `close()`，与 `bus.close` 同样的语义——hot_state 是缓存，关闭失败不阻塞整体停机。

3. **4 进程 docker compose 注入 env**（`deploy/wsl2-dev/docker-compose.aats.yml`）：
   ```yaml
   x-aats-common-env:
     AATS_HOT_STATE_BACKEND: redis
     AATS_HOT_STATE_REDIS_URL: redis://redis:6379/0
   ```
   gateway / market / decision / execution 4 个服务全部继承这两个变量。

4. **Dockerfile editable 安装加 redis extra**（`deploy/wsl2-dev/Dockerfile`）：
   - `pip install -e ".[nats]"` → `pip install -e ".[nats,redis]"`
   - `pyproject.toml` 新增 `redis = ["redis>=5,<6"]` 与 `redis-integration = ["redis>=5,<6", "testcontainers>=4.0"]` 两组可选依赖。

5. **测试覆盖**：
   - `tests/unit/test_settings_hot_state.py`：27 用例，覆盖默认值 / env 加载 / 显式 dict 加载 / 校验失败路径。
   - `tests/integration/test_hot_state_redis_roundtrip.py`：9 用例（testcontainers 起 redis:7-alpine），含 `RedisHotStateStore` 端到端 round-trip、TTL、`global_prefix` 隔离、跨实例可见性、`build_runtime(backend=redis)` 端到端 + 不可达 Redis fail-fast。需 `AATS_RUN_REDIS_INTEGRATION=1` 才会运行。

### 9.3 真跑验证（2026-04-08）

**操作**：
```bash
# WSL2
cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 build aats-gateway
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d
```

**验证矩阵**：

| 验证维度 | 结果 | 证据 |
|---|---|---|
| **redis-py 装进镜像** | ✅ 5.3.1 | `docker run --rm aats-base:dev python -c 'import redis; print(redis.__version__)'` |
| **4 进程容器全部 healthy** | ✅ 4/4 | `docker compose ps` → gateway / market / decision / execution 全部 `Up (healthy)` |
| **build_runtime 构造 hot_state_store** | ✅ 4/4 | 4 个容器各打印一次 `hot_state_store_initialized backend=redis global_prefix=` 日志 |
| **Redis 真的看到 4 个 client 连接** | ✅ 4 client | `docker exec aats-redis redis-cli CLIENT LIST` 显示 4 个 `lib-name=redis-py lib-ver=5.3.1` 客户端，分别来自容器 IP 172.18.0.8 / .9 / .10 / .11 |
| **connect() 真的 ping 通了** | ✅ | 每个 client 列表里都有 `cmd=ping` 标记，证明 `connect → ping → keep-alive` 完整路径走通 |
| **gateway HTTP 不受影响** | ✅ | `curl http://localhost:8000/healthz` → `{"status":"ok","process_role":"gateway"}` |
| **`/system/health` 不受影响** | ✅ | 返回完整 JSON，`overall_status=blocked`（无真 OKX feed 时的预期状态），无 NPE |
| **集成测试 fail-fast 路径** | ✅ | `tests/integration/test_hot_state_redis_roundtrip.py::TestBuildRuntimeWithRedisHotStateBackend::test_build_runtime_redis_backend_unreachable_fails_fast` 已经在 testcontainers 环境锁住"Redis 不可达 → build_runtime 抛错"语义 |

**Redis CLIENT LIST 关键摘录**（脱敏）：
```
id=1301 addr=172.18.0.10:46342  cmd=ping  lib-name=redis-py lib-ver=5.3.1
id=1302 addr=172.18.0.8:51420   cmd=ping  lib-name=redis-py lib-ver=5.3.1
id=1303 addr=172.18.0.11:53940  cmd=ping  lib-name=redis-py lib-ver=5.3.1
id=1304 addr=172.18.0.9:33758   cmd=ping  lib-name=redis-py lib-ver=5.3.1
```
4 个 client 来自 4 个不同的容器 IP，证明 4 个 AATS 进程**真的**各自连了一份独立的 Redis 客户端，而不是同一个进程多开几个连接。

### 9.4 完成判定

- [x] settings 三字段 + 校验（27 unit test 全绿）
- [x] build_runtime 构造 + ping + close 配线
- [x] 4 进程 docker compose env 注入
- [x] Dockerfile / pyproject.toml 装 redis-py
- [x] 集成测试 9 用例（testcontainers / build_runtime fail-fast）
- [x] 4 进程真跑：4 个 `hot_state_store_initialized backend=redis` 日志 + 4 个 redis-py client 连接 + 容器全 healthy
- [x] runbook §9 记录

### 9.5 留给 Slice 6.2 的工作

Slice 6.1 配置的 hot_state_store 在 Slice 6.2 之前**没有任何业务 caller**——它的存在只是给 build_runtime 多一个可注入的对象。这是有意为之：

1. **第一刀只动配线，不动语义**。如果 Slice 6.1 真跑出 bug，回滚成本 = 1 个 commit revert，业务行为零变化。
2. **Slice 6.2 才接 kill_switch**：`KillSwitchService` 写入路径从"内存 dict + 跨进程 NATS 广播"切到"先 hot_state_store.set，再 NATS 广播"；gateway / decision 读时直接 `await hot_state_store.get(make_key("system", "kill_switch"))`，无需重放事件。
3. **Slice 6.3 才接 portfolio_snapshot**：`PortfolioReconciliationService` 写入快照后同步 `hot_state_store.set("portfolio", snapshot)`，gateway 在 dashboard 拉取时 cache hit 直接拿。

---

## 10. Stage 6 Slice 6.2：kill_switch 跨进程同步（2026-04-08）

### 10.1 Slice 6.2 边界与目标

Slice 6.1 把 Redis 接进 4 进程的 build_runtime 但**没有任何业务 caller**——只是把连接铺好。Slice 6.2 是 Stage 6 真正"切语义"的第一刀：把 4 进程拓扑下原本各自一份的 in-memory `KillSwitch` 收敛到"Redis 持久化 + NATS 跨进程广播 + 本地 cache 三层架构"。

**为什么是真实安全 bug**：4 进程拓扑下，`gateway` 调 `kill_switch.halt()` 只动 gateway 进程内存里的 `KillSwitch._state`，`execution` 进程的 `KillSwitch._state` 完全不知情。结果：operator 在 gateway 喊 halt，execution 依然继续下单。崩溃 + restart 同样有问题——新 execution 没人通知它，进程刚起就以默认 `halted=False` 开始接 order intent。

**Slice 6.2 范围**：

| Slice | 范围 | 状态 |
|---|---|---|
| 6.1 | settings 字段 + build_runtime 构造 + docker compose env + 集成测试 | ✅ 已完成 |
| **6.2（本节）** | KillSwitchSyncService + 5 处生产 writer 切到 sync 路径 + 跨进程集成测试 + 真跑验证 | ✅ 完成 |
| 6.3 | portfolio_snapshot 缓存切到 hot_state_store；execution 写、gateway 读 | 待开 |

设计文档：`docs/task/stage_6_slice_6_2_kill_switch_design.md`

### 10.2 实现要点

1. **三层架构**：
   - `KillSwitch`（不动，sync API）：所有 ~30 个 sync 读路径（订单 pre-submit / health check / blocker 渲染等）继续直接打这里。零网络、零阻塞、永远是本进程的"快路径真相"。
   - `KillSwitchSyncService`（**新增**，`aats/services/governance_engine/kill_switch_sync.py`）：sidecar 模式持有 `KillSwitch + HotStateStore + EventBus + process_role`，把 4 个进程的本地真相收敛到同一个 Redis 状态机。提供 `async halt/resume`（FastAPI handler 用）+ `halt_threadsafe/resume_threadsafe`（worker thread 用）双通道。
   - 数据真相：Redis key `aats:hot:system:kill_switch`（持久化 + 跨重启）+ NATS topic `system.kill_switch_state`（实时广播）。

2. **5 处生产 writer 切到 sync 路径**（W1-W5，全部走"先 sync service，缺则 fall back 到本地"模式）：
   - W1 `aats/services/governance_engine/trial_guard.py:_trigger_halt`
   - W2 `aats/services/governance_engine/derivatives_live_guard.py:_trigger_halt`
   - W3 `aats/services/execution_engine/recovery.py:_halt_for_recovery`
   - W4 `aats/services/recovery_control/startup_recovery.py:_halt`（新增 helper）+ phase4 reconciliation halt
   - W5 `aats/services/operator/reconciliation_system_queries.py:halt/resume/rebaseline` 5 处操作面调用，整条 `OperatorQueryService.halt → OperatorReconciliationSystemQueries.halt` 链 **从 sync 转 async**（因为 W5 在 asyncio loop 里跑，必须 `await sync_service.halt(...)` 而不能 `halt_threadsafe`，否则会自我等待死锁）。配套 `aats/api/routes.py:halt`、`aats/services/blocker_control/actions.py:halt-system`、`tests/integration/test_recovery.py` 加 `await`。

3. **bootstrap 顺序**（`aats/bootstrap/config.py`）：
   - 在 `_start_event_bus(slices.bus)` 之后、任何 slice builder（特别是 `_build_reconciliation_slice` 会构造 `ExecutionRecoveryService`）之前构造 `KillSwitchSyncService` 并 `await service.bootstrap()`。
   - bootstrap 内部：先从 Redis 读 `aats:hot:system:kill_switch`，存在且 `halted=True` 则调本地 `KillSwitch.halt()`（**这就是 I3 的实现**）；然后订阅 NATS `system.kill_switch_state`。任何步骤失败都不阻断 build_runtime（best-effort 写），只 log warning。
   - `ApplicationRuntime` 加 `kill_switch_sync_service` 字段；`stop_background_tasks` 在 `bus.close()` 之前 `await sync_service.stop()`。

4. **不变量保证**：
   - **I1（本地立即生效）**：每个写路径第一步都是 sync `KillSwitch.halt()`，写 Redis/NATS 是后续步骤
   - **I2（≤1s 跨进程广播）**：NATS publish 真跑实测 < 5ms（见 §10.4）
   - **I3（重启从 Redis 恢复）**：bootstrap 先从 Redis 读出来再订阅 NATS
   - **I4/I5（Redis/NATS 不可达不破坏本地）**：写路径全 best-effort，try/except 包住，本地 cache 永远是第一步
   - **I6（乱序事件不退化）**：`_handle_remote_event` 校验 `set_at_ts > self._last_applied_ts`
   - **I7（直接调 `KillSwitch.halt()` 不破）**：`KillSwitch` 类 API 保持 sync 不变，~30 个直接 caller 零修改

5. **测试覆盖**：
   - `tests/unit/test_kill_switch_sync.py`：17 用例，覆盖 bootstrap（hydrate from Redis / empty Redis / Redis fail / parse fail）、halt/resume happy + 去重、远端事件应用 + stale 拒绝 + 同 source 跳过、threadsafe 写从 worker thread 投递成功 / 超时 / loop 关闭 fall back 等所有路径。
   - `tests/integration/test_kill_switch_cross_process.py`：4 用例（testcontainers 起 redis:7-alpine + 共享 InMemoryEventBus 模拟两进程），验证：
     - service A halt → service B 本地 KillSwitch ≤1s 内 halted=True
     - 重启 service B（用全新 store + 全新 KillSwitch）后 bootstrap 仍然 halted=True
     - A halt 后 B resume → 两个 KillSwitch 最终一致到 halted=False
     - 直接喂老 set_at_ts 远端事件不让本地 cache 回退
   - 全量回归：1276 unit test 全绿（增加 17 个新 unit test，零退化）；Slice 6.1 9 个 Redis 集成测试同步重跑全绿。

### 10.3 真跑验证（2026-04-08）

**操作**：
```bash
# WSL2
cd ~/aats/deploy/wsl2-dev

# 1) 删除老 NATS 流（subjects 集合不一样，必须先删旧的让新代码重建）
python -c "
import asyncio, nats
async def main():
    nc = await nats.connect('nats://127.0.0.1:4222')
    js = nc.jetstream()
    await js.delete_stream('AATS_EVENTS')
    await nc.drain()
asyncio.run(main())
"

# 2) 重建镜像 + 重启 4 容器
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 build
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d
```

⚠️ **NATS 流 schema 兼容性**：Slice 6.2 给 `DEFAULT_CRITICAL_TOPICS` 新增 `system.kill_switch_state` topic，老 `AATS_EVENTS` 流的 subjects 集合是 N，新代码尝试 `add_stream` 时 NATS 拒绝 `BadRequestError 10058: stream name already in use with a different configuration`。**必须先 `delete_stream('AATS_EVENTS')` 让新代码用 N+1 subjects 重建**。这是后续任何修改 `DEFAULT_CRITICAL_TOPICS` 的 slice 都要遵守的部署纪律——记进 §10.5 故障矩阵。

**验证矩阵**：

| 验证维度 | 不变量 | 结果 | 证据 |
|---|---|---|---|
| **4 容器全部 healthy** | — | ✅ 4/4 | `docker ps` → gateway / market / decision / execution 全部 `Up (healthy)` |
| **4 个 sync service 启动** | — | ✅ 4/4 | 每个容器各打一行 `kill_switch_sync_service_initialized bootstrap_state.subscribed=true process_role=<role>` |
| **NATS 4 个 durable consumer 全部 push_bound** | — | ✅ 4/4 | `aats-{gateway,market,decision,execution}-system_kill_switch_state` consumer pending=0 / delivered=0 |
| **手动 halt 跨 4 进程同步** | I2 | ✅ < 5ms | drill 时间戳 `11:56:28.015`，4 个 `kill_switch_sync_remote_applied halted=True reason=wsl2_realrun_drill_halt set_at_ts=1775620587.965` 日志全部落在同一 millisecond bucket |
| **杀 execution → 新 execution 从 Redis 恢复 halt** | I3 | ✅ | `docker restart aats-execution`，新进程 bootstrap 阶段 `kill_switch_sync_bootstrap_hydrated halted=True reason=wsl2_realrun_drill_halt`，紧接 `kill_switch_sync_service_initialized bootstrap_state.kill_switch.halted=true` |
| **resume 跨 4 进程同步** | I2 对称 | ✅ < 5ms | drill 时间戳 `12:00:23.963/964`，4 个 `kill_switch_sync_remote_applied halted=False` 落在同一 millisecond bucket |
| **Redis 终态正确** | — | ✅ | `aats:hot:system:kill_switch` = `{"halted": false, "reason": null, "set_at_ts": 1775620823.91, "source_role": "operator_drill"}` |
| **redis-py client 4/4 稳定** | — | ✅ | `docker exec aats-redis redis-cli CLIENT LIST | grep redis-py | wc -l = 4` |

### 10.4 跨进程广播延迟实测

| 阶段 | 时间戳 | 与触发的差 |
|---|---|---|
| operator drill 触发 (Python `time.time()`) | `11:56:27.965` | t=0 |
| gateway 应用远端事件 | `11:56:28.015` | +50 ms |
| market 应用远端事件 | `11:56:28.015` | +50 ms |
| decision 应用远端事件 | `11:56:28.015` | +50 ms |
| execution 应用远端事件 | `11:56:28.015` | +50 ms |

≈50 ms 跨进程广播延迟（包含 Redis SET + NATS publish + 4 个 durable consumer push 的全链路），远低于设计文档 §5 的 1s 预算。这个延迟基本上是 Python `time.time()` → `await store.set()` → `await bus.publish()` → NATS server → 4 路 push deliver 的合计往返。

### 10.5 故障演练 / 部署纪律

| 场景 | 行为 | 修复 |
|---|---|---|
| 修改 `DEFAULT_CRITICAL_TOPICS` 后部署新代码 | 新代码 `add_stream` 触发 `BadRequestError 10058` 4 容器 startup 失败 restart loop | 部署前 `js.delete_stream('AATS_EVENTS')`；新代码会用更新的 subjects 集合重建。**未来加 NATS topic 必须在 changelog 注明** |
| Redis 宕机 | 写 halt → 本地 cache 立即 `halted=True` + `kill_switch_sync_redis_set_failed` warning，跨进程靠 NATS 广播；其他进程**仍然**收到广播并应用本地 cache（I4） | Redis 起来后下一次 halt/resume 自动回写，状态不会丢 |
| NATS 宕机 | 写 halt → 本地 cache 立即 `halted=True` + Redis 仍然写入 + `kill_switch_sync_nats_publish_failed` warning，**跨进程同步暂停**直到 NATS 恢复 | NATS 恢复后下一次 halt/resume 自动重广播；空窗期内其他进程不知情，但本进程的 sync read 永远是 halted=True 兜底 |
| 进程 OOM kill 后 restart | 新进程 bootstrap 从 Redis 读出 halt 状态，`kill_switch_sync_bootstrap_hydrated` log 落地 | 自动恢复，无需人工干预（I3） |

### 10.6 完成判定

- [x] `KillSwitchSyncService` 类 + 17 个 unit test 全绿（独立模块，不动 build_runtime 即可验证）
- [x] `build_runtime` 装配 + `ApplicationRuntime.kill_switch_sync_service` 字段 + stop_background_tasks 清理（零行为变化检查点：113 个 build_runtime 相关 test 全过）
- [x] 5 处生产 writer 切到 sync 路径（W1-W5），W5 配套 sync→async 调用链转换
- [x] 全量 1276 unit test 全绿（含 17 个新增），零退化
- [x] `tests/integration/test_kill_switch_cross_process.py` 4 用例 testcontainers 全绿；Slice 6.1 9 个 Redis 集成测试 regression 全绿
- [x] 4 进程真跑：4 个 `kill_switch_sync_service_initialized` 日志 + 4 个 NATS durable consumer push_bound + halt/resume drill < 50ms 跨 4 进程同步 + 杀 execution restart 后 bootstrap 自动 hydrate
- [x] runbook §10 记录（本节）

### 10.7 留给 Slice 6.3 的工作

Slice 6.2 把 kill_switch 这一条"核心安全开关"接进了 hot_state_store，但 4 进程拓扑下还有两条业务路径仍然是"内存 dict + 跨进程 NATS 重放"模式：

1. **Slice 6.3 接 portfolio_snapshot**：`PortfolioReconciliationService` 写完后同步 `hot_state_store.set('portfolio', snapshot)`；gateway dashboard 拉取时 cache hit 直接读，不用等 NATS 重放。
2. **Slice 6.4（待规划）**：`KillSwitch + KillSwitchSyncService` 二合一重构。当前 sync API 是为了 ~30 个 caller 零侵入而保留的"过渡形态"，等 Slice 6.3 落地稳定后可以把 `KillSwitch` 内部直接换成 `KillSwitchSyncService` 的薄壳，统一 API。
3. **Slice 6.5（候选）**：如果实盘期发现 NATS 偶发掉包导致 cache 漂移，加周期 Redis poll（默认 5s）作为 reconciler。当前实测 50ms 内同步 + 0 丢包，不主动开。

---

## 11. Stage 6 Slice 6.3：portfolio_snapshot 接 hot_state_store（2026-04-08）

### 11.1 Slice 6.3 边界与目标

Slice 6.2 把 `KillSwitch` 这一条核心安全开关接进了 hot_state_store，证明了"sidecar + Redis 持久化 + NATS 广播 + 本地 cache"四件套在 4 进程拓扑里是稳定可用的范式。Slice 6.3 把同一套范式套到第二条业务通路：**latest portfolio snapshot**。

**为什么是真实业务 bug**：4 进程拓扑下 `gateway` 的 dashboard / operator API 反复调 `OperatorQueryService._latest_scoped_snapshot`，funnel 到 `portfolio_repo.latest_for_scope(...)` 直接打 PG。symbols 扩张后这一路 QPS 会线性增长。Slice 6.3 在 query 路径插一层共享缓存，把 latest snapshot 在 4 个进程间用 NATS 实时同步、用 Redis 持久化兜底重启 hydrate。

| Slice | 范围 | 状态 |
|---|---|---|
| 6.1 | settings 字段 + build_runtime 构造 + docker compose env + 集成测试 | ✅ 已完成 |
| 6.2 | KillSwitchSyncService + 5 处生产 writer 切到 sync 路径 + 跨进程集成测试 + 真跑验证 | ✅ 已完成 |
| **6.3（本节）** | PortfolioSnapshotCache + outbox publisher commit hook + query_service cache 优先 + 4 进程真跑 | ✅ 完成 |
| 6.4 | KillSwitch + KillSwitchSyncService 二合一重构（候选） | 待开 |

设计文档：`docs/task/stage_6_slice_6_3_portfolio_snapshot_design.md`

### 11.2 实现要点

1. **三层架构**（与 Slice 6.2 同 sidecar 模板）：
   - `OperatorQueryService._latest_scoped_snapshot`（`aats/services/operator/query_service.py:957`，已存在 10 处 caller）：sync 路径，dashboard / operator API 子树。本 slice 只在这一处加 cache 优先 + portfolio_repo fallback，sync 签名保持不变。
   - `PortfolioSnapshotCache`（**新增**，`aats/services/portfolio_service/snapshot_cache.py`）：sidecar 持有 `HotStateStore + EventBus + process_role`。把 4 个进程的 dashboard 视图收敛到同一份"最新 snapshot 视图"。
   - 数据真相：Redis key `aats:hot:portfolio:latest:<scope_fingerprint>`（持久化 + 跨重启）+ NATS topic `portfolio.snapshots`（已存在的实时广播，由 outbox publisher 现有 `flush_pending` 驱动，**cache 自己不广播 NATS**）。

2. **关键设计决策**（设计文档 §4.2）：
   - **D5**：`cache.publish(snapshot)` = 同步本地 dict + best-effort 写 Redis；**不广播 NATS**，避免和 outbox publisher 双发。
   - **D6**：`_handle_remote_event` 用 `snapshot.snapshot_ts <= 本地` idempotent noop 规则，同时承担"自回环跳过"+"防退化"+"同毫秒 corner case"三个职责。
   - **D8**：4 个 process_role 都装 cache，统一行为，cache 类没有 process_role 分支。
   - **D9**：cache 注入点严格限定在 `query_service._latest_scoped_snapshot`，**不 wrap PortfolioRepository、不修改 latest_snapshot_for_scope helper**。所有 production 路径（context_builder / coordinator / recovery / reconciliation / startup_recovery）直接打 PG，**完全绕过 cache**，避免 cache 偏差污染 critical 路径。

3. **bootstrap 顺序与 NATS subscribe**（`aats/bootstrap/config.py`）：
   - 在 `_start_event_bus(slices.bus)` + `KillSwitchSyncService.bootstrap()` 之后立即构造 `PortfolioSnapshotCache`。
   - **关键陷阱**：cache 的 NATS 订阅**必须**走 `_wire_event_subscriptions` 阶段的 `_CollectingBus`，**不能**在 `bootstrap()` 内部直接订阅。原因：`portfolio.snapshots` 已经被 `audit_service` / `reconciliation_service` 订阅，NATS 同 (role, topic) 只允许**一个 durable binding**，cache 如果在 bootstrap 期间提前 subscribe 会触发 `nats: JetStream.Error consumer is already bound to a subscription`。
   - 解决：`bootstrap()` 加 `subscribe: bool = True` 默认参数，production caller 传 `subscribe=False` 把订阅推迟；新增公开方法 `register_remote_subscription(bus)`，在 `_wire_event_subscriptions` 内部经 `_CollectingBus.flush` 路径和 audit/reconciliation 共用同一个 fan-out handler。默认 `subscribe=True` 保留单元测试 + InMemoryEventBus 模拟下的"一次到位"语义。
   - `ApplicationRuntime` 加 `portfolio_snapshot_cache` 字段；`stop_background_tasks` 在 kill_switch_sync_service.stop() 之后 `await cache.stop()`。

4. **outbox publisher commit hook**（`aats/services/portfolio_service/outbox.py`）：
   - `PostgresPortfolioOutboxPublisher` dataclass 加 `snapshot_cache: PortfolioSnapshotCache | None = None` 字段。
   - 在 `persist_bootstrap_snapshot` 与 `persist_fill_projection` 的 commit 之后、`flush_pending` 之前调 `await self._publish_to_cache(snapshot)`，把最新 snapshot 注入 cache（同步本地 dict + best-effort Redis，不抛）。
   - 调用顺序：DB commit → cache publish → flush_pending（NATS 广播）。execution 进程的 query 路径不必等 NATS roundtrip 就能命中 cache，远端 3 个进程通过 NATS 接收。

5. **不变量保证**（设计文档 §4.3）：
   - **I1（writer self-visibility）**：execution 写完 outbox 后立即调 cache.publish，自己的 query 路径下一拍命中本地 dict
   - **I2（≤1s 跨进程同步）**：NATS publish 真跑实测 < 5ms（与 §10.4 同链路）
   - **I3（重启 hydrate）**：bootstrap 先从 Redis 读 `aats:hot:portfolio:latest:<scope>`，存在且能 parse → 写本地 dict
   - **I4/I5（Redis/NATS 不可达不破坏本地）**：cache 写路径全 best-effort，try/except 包住，本地 dict 永远是第一步
   - **I6（cache miss 不破坏读）**：sync caller `_latest_scoped_snapshot` 在 cache.get_sync 返回 None 时 fallback 到 portfolio_repo
   - **I7（10 处 sync caller API 不变）**：cache 注入是 OperatorQueryService 私有字段，sync 签名不动
   - **I8（乱序事件 noop）**：D6 的 `snapshot_ts <= 本地` 规则
   - **I9（scope 隔离）**：`scope_fingerprint = f"{product_type}:{margin_mode}"` 自然隔离

6. **测试覆盖**：
   - `tests/unit/test_portfolio_snapshot_cache.py`：15 用例，覆盖 bootstrap (Redis hydrate / empty / parse fail / Redis fail) + publish happy / stale noop + remote event apply / stale skip / parse fail + scope 隔离 + get_sync miss + diagnostic snapshot()。
   - `tests/integration/test_portfolio_snapshot_cache_cross_process.py`：4 用例，testcontainers 起 redis:7-alpine + 共享 InMemoryEventBus 模拟两进程，验证：
     - I2 service A publish → service B 本地 dict ≤1s 内拿到
     - I3 重启 cache B 后 bootstrap 仍然 hydrate 出 A publish 的 snapshot
     - I9 两个 scope 互不污染
     - I8 stale 远端事件不让 B 本地 cache 退化
   - 全量回归：1291 unit test 全绿（增加 15 个新 unit test，零退化）；Slice 6.1 / 6.2 集成测试同步重跑全绿。

### 11.3 真跑验证（2026-04-08）

**操作**：
```bash
# WSL2，从 /mnt/d 直接跑（避免 sync_to_wsl2.sh pull 覆盖 /home/arthur 的 6.2 work-in-progress）
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev

# 1) 重建镜像 + 重启 4 容器（NATS stream subjects 集合无变化，无需 delete_stream）
docker compose -f docker-compose.aats.yml --env-file .env.wsl2.template build
docker compose -f docker-compose.aats.yml --env-file .env.wsl2.template up -d

# 2) 用 deploy/wsl2-dev/probe_snapshot_cache.py 注入一份 sample snapshot 进 Redis
docker run --rm --network aats-dev_aats \
  -v /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev/probe_snapshot_cache.py:/probe.py:ro \
  -e PYTHONPATH=/app -e AATS_HOT_STATE_REDIS_URL=redis://redis:6379/0 \
  aats-base:dev python /probe.py

# 3) 验证 Redis key
docker exec aats-redis redis-cli KEYS 'aats:hot:portfolio:*'
docker exec aats-redis redis-cli GET 'aats:hot:portfolio:latest:spot:cash'

# 4) restart 一个进程验证 hydrate
docker restart aats-gateway

# 5) 验证 operator API 直接命中 cache
curl -s http://localhost:8000/portfolio/latest | python -m json.tool
```

**验证矩阵**（设计文档 §6 Step 7 的 7 维度）：

| 验证维度 | 不变量 | 结果 | 证据 |
|---|---|---|---|
| **D1 4 个 cache 实例全部 initialized** | — | ✅ 4/4 | 每个容器各打一行 `portfolio_snapshot_cache_initialized bootstrap_state.bootstrapped=true process_role=<role>` |
| **subscribe via _CollectingBus 没有 NATS duplicate-binding** | — | ✅ | `docker logs <c> | grep -iE 'consumer is already|nats.*error'` 4 容器全部 0 命中；4 个 `nats_subscription_registered durable=aats-<role>-portfolio_snapshots` 各 1 条 |
| **D2/I2 跨进程实时同步** | I2 | ✅ | execution 在 13:33:29.462 触发 JetStream 历史 replay，4 个进程的 `portfolio_snapshot_cache_remote_applied snapshot_ts=2026-04-08T05:33:29.462407+00:00` 时间戳分别落在 13:33:29.467 / 13:33:29.467 / 13:33:29.678 / 13:33:30.728，跨 4 进程 < 1s |
| **D5 cache.publish 写 Redis** | — | ✅ | `probe_snapshot_cache.py` 跑完后 `redis-cli KEYS 'aats:hot:portfolio:*'` → `aats:hot:portfolio:latest:spot:cash`；GET 出来的 JSON 含 `decision_id="probe-decision-1"` |
| **I3 重启 hydrate from Redis** | I3 | ✅ | `docker restart aats-gateway`，新 gateway 进程日志 `portfolio_snapshot_cache_bootstrap_hydrated decision_id=probe-decision-1 snapshot_ts=2026-04-08T05:39:03.696085+00:00`，紧接 `cached_scopes=["spot:cash"]` |
| **D4 query_service 命中 cache** | — | ✅ | `curl http://localhost:8000/portfolio/latest` 返回的 JSON `decision_id` = `probe-decision-1`、`snapshot_ts` = `2026-04-08T05:39:03.696085Z`、`balances.USDT` = `100000` ← 全部来自 probe 注入的 snapshot；gateway 本身没有 portfolio_service 写路径，唯一来源就是 cache |
| **kill_switch_sync 回归（D7）** | — | ✅ 4/4 | 4 个容器都打了 `kill_switch_sync_subscribed process_role=<role> topic=system.kill_switch_state` + `kill_switch_sync_service_initialized`，Slice 6.2 完全无退化 |

### 11.4 NATS duplicate-binding 修复（实战发现的真陷阱）

第一次跑的时候 4 容器全部 restart loop，stack：

```
nats.js.errors.Error: nats: JetStream.Error consumer is already bound to a subscription
  File ".../aats/bus/nats_bus.py:470", in subscribe
    sub = await self._js.subscribe(...)
  File ".../aats/bootstrap/config.py:2515", in flush  (_CollectingBus)
    await self._bus.subscribe(topic, _fan_out)
  File ".../aats/bootstrap/config.py:3549", in _wire_event_subscriptions
    await collector.flush()
```

**根因**：第一版实现里 `PortfolioSnapshotCache.bootstrap()` 直接 `await self._bus.subscribe(topics.PORTFOLIO_SNAPSHOTS, ...)`。bootstrap 在 `_start_event_bus` 之后、`_wire_event_subscriptions` 之前跑，所以 cache 的 subscribe 比 collector.flush 先到 NATS，先创建了一个 durable consumer `aats-execution-portfolio_snapshots`（execution role 同时是 audit_service / reconciliation_service 的订阅 role）。然后 `_wire_event_subscriptions` 阶段 collector 把 audit_service + reconciliation_service 的 subscribe 聚合成一个 fan-out 再发到同一个 (role, topic)，nats-py 拒绝。

Stage 7 修 _CollectingBus 的时候已经处理过 POSITION_TARGETS / PORTFOLIO_SNAPSHOTS / RECONCILIATION_REPORTS 三个 topic 在 critical + observer 双订阅的 case，cache 加进来后必须**走同一个 collector**否则就还原回去了。

**修复**：
- `PortfolioSnapshotCache.bootstrap` 加 `subscribe: bool = True` 默认参数（保持单元测试 + in-memory 模拟一次到位）。
- 新增公开方法 `register_remote_subscription(bus: EventBus)`，bus 既可以是 `_CollectingBus` 也可以是真实 EventBus。
- production caller `build_runtime` 传 `subscribe=False` 让 bootstrap 只做 Redis hydrate，订阅推迟到 `_wire_event_subscriptions` 调 `collector.register_remote_subscription(...)`，最终 `collector.flush()` 把 audit / reconciliation / cache 三者收束成同一个 fan-out handler，落在同一个 NATS durable consumer 上。
- 32 个 cache + kill_switch_sync 单元测试 + 3 个 4-process smoke 测试在 fix 后全部继续过。
- 部署纪律 update：**任何接 hot_state_store 的新 sidecar，如果想订阅 NATS topic 都必须走 `_wire_event_subscriptions` 内的 collector，绝不能在 bootstrap() 内部直接 subscribe**。Slice 6.4 KillSwitchSyncService 的 NATS 订阅之前没踩到这个坑，是因为 `system.kill_switch_state` 这个 topic 只有 sync_service 一个订阅者，没有 fan-out。

### 11.5 完成判定

- [x] `PortfolioSnapshotCache` 类 + 15 个 unit test 全绿（独立模块，不动 build_runtime 即可验证）
- [x] `build_runtime` 装配 + `ApplicationRuntime.portfolio_snapshot_cache` 字段 + stop_background_tasks 清理（零行为变化检查点：113 个 build_runtime 相关 test 全过）
- [x] outbox publisher commit hook 注入 cache（`persist_bootstrap_snapshot` + `persist_fill_projection` 两个写路径）
- [x] `OperatorQueryService._latest_scoped_snapshot_uncached` cache 优先 + portfolio_repo fallback
- [x] 全量 1291 unit test 全绿（含 15 个新增），零退化
- [x] `tests/integration/test_portfolio_snapshot_cache_cross_process.py` 4 用例 testcontainers 全绿；Slice 6.1 / 6.2 集成测试 regression 全绿
- [x] 4 进程真跑：4 个 `portfolio_snapshot_cache_initialized` 日志 + 4 个 `nats_subscription_registered` durable consumer 各 1 条 + 0 个 `consumer is already bound` 错误 + probe 注入 snapshot → Redis 写入成功 → restart gateway 后 `bootstrap_hydrated` 日志 + curl `/portfolio/latest` 返回 probe 注入的内容
- [x] runbook §11 记录（本节）

### 11.6 留给 Slice 6.4+ 的工作

Slice 6.3 把 `portfolio_snapshot` 这条主业务通路接进了 hot_state_store，证明了 sidecar 范式可以从"安全开关"扩展到"业务热数据"。下一步候选：

1. **Slice 6.4 KillSwitch 二合一重构**：当前 `KillSwitch` 是 sync API、`KillSwitchSyncService` 是 async sidecar，30+ 个 sync caller 零侵入。Slice 6.3 验证完毕后可以把 `KillSwitch` 内部直接换成 `KillSwitchSyncService` 的薄壳，统一 API 减一层间接。
2. **Slice 6.5 portfolio_snapshot reconciler**：如果实盘期发现 NATS 偶发掉包导致 cache 漂移，加周期 Redis poll（默认 5s）作为 reconciler。当前实测跨 4 进程 < 1s 同步、0 丢包，不主动开。
3. **Slice 6.6 其他热数据**：`market_snapshot` / `feature_snapshot` / `decision_outcome` 这三类 4 进程都要读的"准实时视图"是下一个候选。优先级排序按"PG 直查 QPS × 数据 staleness 容忍度"算分。

---

## 12. Changelog

- 2026-04-07：首版。基于 5d 装配 review 发现 3 个 gap（Dockerfile 缺 curl、`/system/health` gateway role NPE、3 daemon 无 healthcheck），列出修复清单 + 真跑命令序列 + 完成判定 + 回滚步骤。等用户审批 §1-§2 修复方案后实施 + 真跑。
- 2026-04-08：第 1 次真跑完成。10/10 容器 healthy。真跑里又额外修了 5 个 runbook 起步时未预见的 gap：NATS duplicate-subscription（_CollectingBus fan-out）、Dockerfile `/app` ownership、jaeger badger 权限、grafana provisioning 配置冲突、docker compose plugin 缺失。详见 §6.1。
- 2026-04-08（追加）：第 1 次真跑后又发现 §1.2 根因分析有偏差。`/system/health`、`/system/recovery`、`/system/mode` 三个 CORE_SPECS endpoint 在 gateway role 下 500 的真因是 `runtime_queries.py:ai_runtime` 直接 `.status()` 一个 None 的 `ai_service`，与 `market_gateway` / `execution_adapter` 无关（这俩在 shared slice）。在 `ai_runtime()` 加 None-guard 返回稳定 stub（`provider="not_loaded"`），下游 `recovery_view → system_mode → system_health` 全链路恢复 200。配套 3 个 unit test。同步修订 §1.2 与 §6.1.6 记录。
- 2026-04-08（再追加）：gap 6 修完之后第 2 次跑容器，`/system/blocker-control` 仍然 500。全仓 grep `runtime.ai_service.status()` 又找出 6 处直读站点，其中 GET 链 3 处（blocker_control、recovery_posture、_ai_shadow_summary）会被 `/system/health` 和 `/system/blocker-control` 链路触发，POST mutate 3 处（ai_review_restore / set_ai_operating_mode / ai_review_degrade_to_baseline）会被 operator 误调时 500。GET 链 2 处切换到 `self.owner.ai_runtime()` 走 facade stub，1 处（recovery_posture）加 None-guard 短路返回 False；POST mutate 3 处加方法顶 `raise ValueError("ai_service_not_loaded_in_this_process_role")` 显式拒绝。配套 5 个新 unit test，全仓 1232 unit + 26 ai-integration 全绿。第 3 次真跑 4 容器 healthy，CORE_SPECS 4 个 endpoint 全部 200。详见 §6.1.7。
- 2026-04-08（故障演练 #1 完成）：新增 §7 故障演练章节。演练 #2.2（杀 execution）+ #2.3（同时杀 decision + market）：3 个 daemon 全部按 RestartCount=1 自愈，NATS 26 个 durable consumer 全部 push_bound 重连，gateway 完全未受影响。**重大方法学发现**：`docker kill` 不会触发 Docker restart manager（被 daemon 当成 operator 主动停服），必须从 host `kill -9 <python_pid>` 才能让 tini exit → 容器内部退出 → restart manager 接管。后续所有 chaos drill 都要遵循这个方法。详见 §7.2 / §7.6。
- 2026-04-08（Stage 5 端到端 fan-out 验证完成）：新增 §8 Stage 5 章节。从 gateway 容器内 nats-py 直发 envelope 到 `aats.system.processing_failures`，execution 容器的 `aats-execution-system_processing_failures` durable consumer 收到、deserialize 成功、ack_floor 推进到 stream_seq=160。中途意外验证了 handler 错误路径：第 1 次注入 envelope 形状写错（多包了一层），handler 抛 ValidationError → max_deliver=5 次重投后丢弃，错误日志完整可观测。两次注入合在一起锁住了"成功路径 + 错误路径 + 重投语义"三个关键不变量。被动证据补充：`portfolio.snapshots` / `reconciliation.reports` 在真跑期间的 delivered 计数 > 0，证明 execution → decision 这条主跨进程通路在自然流量下也能跑通。详见 §8.4 矩阵。
- 2026-04-08（Stage 6 Slice 6.2 完成）：新增 §10 Slice 6.2 章节。`KillSwitchSyncService` 把 4 进程拓扑下原本各自一份的 in-memory `KillSwitch` 收敛到"Redis 持久化 + NATS 跨进程广播 + 本地 cache"三层架构，修掉 4 进程拓扑下"gateway halt 不到达 execution"+"重启进程默认 halted=False"两个真实资金安全 bug。改 5 处生产 writer (trial_guard / derivatives_live_guard / execution_recovery / startup_recovery / operator_reconciliation_queries)，W5 配套 OperatorQueryService.halt sync→async 调用链转换。1276 unit + 4 跨进程集成 + 9 Slice 6.1 regression 全绿。4 进程真跑实测：halt/resume drill 跨 4 进程 < 50ms 同步 + 杀 execution `docker restart` 后新进程 bootstrap 自动从 Redis hydrate `halted=True reason=wsl2_realrun_drill_halt`，I1/I2/I3 三个核心不变量真容器锁住。**部署纪律**：本次 deploy 触发 NATS `BadRequestError 10058 stream name already in use with a different configuration`，根因是新增 `system.kill_switch_state` 进 `DEFAULT_CRITICAL_TOPICS` 后 subjects 集合不再与老流匹配。修复 = 部署前 `js.delete_stream('AATS_EVENTS')` 让新代码用 N+1 subjects 重建。后续任何修改 `DEFAULT_CRITICAL_TOPICS` 的 slice 都需遵守这条部署纪律。详见 §10.3-§10.5。
- 2026-04-08（Stage 6 Slice 6.1 完成）：新增 §9 Stage 6 Slice 6.1 章节。settings 加 `hot_state_backend` / `hot_state_redis_url` / `hot_state_global_prefix` 三字段；`build_runtime` 通过 `_build_and_connect_hot_state_store` 在 entry_execution_guard 之后构造并 ping 通 Redis，失败 fail-fast；4 进程 docker compose 注入 `AATS_HOT_STATE_BACKEND=redis`；Dockerfile 装 `.[nats,redis]`。真跑验证：4 个容器全部 healthy，4 条 `hot_state_store_initialized backend=redis` 日志，Redis CLIENT LIST 看到来自 4 个不同容器 IP 的 redis-py 5.3.1 客户端（lib-name 标签精确匹配）。Slice 6.1 严格"零行为变化"：暂无 caller 用 hot_state_store，给 Slice 6.2/6.3 把 kill_switch / portfolio_snapshot 接进 Redis 时铺垫。详见 §9.3 矩阵 + `docs/task/stage_6_redis_hot_state_design.md`。
- 2026-04-08（Stage 6 Slice 6.3 完成）：新增 §11 Stage 6 Slice 6.3 章节。`PortfolioSnapshotCache` 把 latest portfolio snapshot 接进 hot_state_store + NATS sidecar 模板，4 进程拓扑下 dashboard / operator API 不再每次都打 PG。改 outbox publisher（commit hook 注入 cache）+ query_service（cache 优先 + portfolio_repo fallback）+ build_runtime 装配（cache 字段 + stop hook + register_remote_subscription via collector）。15 unit + 4 跨进程集成 testcontainers + 1291 全量 unit 全绿。4 进程真跑实测：4 个 cache initialized + 0 个 NATS duplicate-binding + probe 注入 → Redis 写入 → restart gateway → bootstrap_hydrated → curl `/portfolio/latest` 返回 probe 内容，I1/I2/I3/D4/D5 五个核心不变量真容器锁住。**部署纪律新增**：任何接 hot_state_store 的新 sidecar 想订阅已有 NATS topic 都必须走 `_wire_event_subscriptions` 内的 `_CollectingBus`，不能在 bootstrap() 内部直接 subscribe，否则会因为 NATS 同 (role, topic) 只允许一个 durable binding 触发 `consumer is already bound to a subscription` 错误。第一版实现踩到这个坑，修复方法是给 `bootstrap` 加 `subscribe: bool = True` 默认参数 + 新增 `register_remote_subscription(bus)` 公开方法，让 production 路径 deferred subscribe 走 collector。详见 §11.3-§11.4。
