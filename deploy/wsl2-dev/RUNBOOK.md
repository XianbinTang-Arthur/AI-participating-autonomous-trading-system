# AATS WSL2 部署 Runbook（从零到全套）

> 本文档面向正在部署 AATS 多进程版本的运维人员（即你自己）。
> 假设硬件：Windows 11 + WSL2 Ubuntu，无 GPU 要求；最低 16GB 内存推荐 32GB。
> 所有组件零云费用，全部本地运行。

---

## 0. 前置检查（Day 0）

### 0.1 WSL2 已启用
```bash
# 在 PowerShell 里
wsl --status
wsl --list --verbose
```
要求：Default Version 至少为 2，且至少有一个 Ubuntu 发行版处于 Running。

### 0.2 进入 WSL2
```bash
wsl -d Ubuntu
```
后续所有命令默认在 WSL2 Ubuntu shell 内执行。

### 0.3 检查 Docker
```bash
docker --version
docker compose version
```
如果两个命令都正常输出版本号即可；否则按 docker.com 官方文档安装 docker engine + compose plugin。

### 0.4 检查 Python
```bash
python3 --version       # 期望 3.11+
which python3
```

### 0.5 检查 git 仓库位置
本项目源码假设位于 Windows 路径 `D:\文件\project\AIParticipatingAutonomousTradingSystem`，
WSL2 内对应 `/mnt/d/文件/project/AIParticipatingAutonomousTradingSystem`。
直接在 `/mnt/d/...` 下运行 docker compose 是可以的；但如果 I/O 太慢可以
把 deploy 目录单独 rsync 到 `~/aats-deploy/` 内运行。

---

## 1. 启动基础设施（Day 1）

### 1.1 准备 .env.wsl2
```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev
cp .env.wsl2.template .env.wsl2
# 用编辑器把 POSTGRES_PASSWORD / GRAFANA_ADMIN_PASSWORD 改成长随机串
nano .env.wsl2
```
**绝对不要把 .env.wsl2 提交进 git**（.gitignore 已经配置好排除规则）。

### 1.2 启动全部服务
```bash
docker compose --env-file .env.wsl2 up -d
```
首次启动会拉镜像，可能 5~10 分钟。

### 1.3 验证服务健康
```bash
docker compose --env-file .env.wsl2 ps
```
每个服务的 STATUS 应该是 `Up X seconds (healthy)` 或 `Up X seconds`。

逐项 ping：
```bash
# Postgres
docker compose --env-file .env.wsl2 exec postgres pg_isready -U aats
# Redis
docker compose --env-file .env.wsl2 exec redis redis-cli ping
# NATS
curl -s http://127.0.0.1:8222/healthz
# Loki
curl -s http://127.0.0.1:3100/ready
# Jaeger UI
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:16686/
# Grafana
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3000/api/health
```
全部返回 200 / PONG / OK 即可。

### 1.4 初始化 Postgres schema 和 migrations
```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem
export AATS_DATABASE_URL="postgresql+psycopg2://aats:$(grep POSTGRES_PASSWORD deploy/wsl2-dev/.env.wsl2 | cut -d= -f2)@127.0.0.1:5432/aats"
python3 -m aats.scripts.bootstrap_database  # 假设有这个脚本；如果没有就走 build_runtime 启动一次
```
（如果项目里没有 bootstrap_database 脚本，跳过这一步：第一次 build_runtime
启动 monolith 时会自动 create_all + apply_current_migrations。）

---

## 2. 单进程冒烟测试（Day 1）

在切到多进程之前，先用 monolith 跑通基础能力。

### 2.1 用真实 Postgres 跑 monolith
```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem
export AATS_DATABASE_URL="..."  # 同上
export AATS_STORAGE_MODE=postgres
unset AATS_PROCESS_ROLE  # monolith 模式
python3 -m aats.api.main  # 或者项目的主入口
```
观察日志没有 traceback，dashboard 在 http://127.0.0.1:8000 能打开（端口
按项目 settings.api_port 为准）。

### 2.2 验证 advisory lock 拿到了 monolith 的 lock_key
```bash
docker compose --env-file deploy/wsl2-dev/.env.wsl2 exec postgres \
  psql -U aats -d aats -c "SELECT * FROM pg_locks WHERE locktype='advisory';"
```
应当能看到一条记录。

### 2.3 关掉 monolith
Ctrl+C 即可。lock 会自动释放。

---

## 3. 多进程切片化启动（Day 2）

⚠️ 这一步只有在 Stage 4-6 的 NATS / Redis 接入代码完成后才有意义。
当前（2026-04-07）4 个进程能同时启动且 advisory lock 互不冲突，但
gateway → market → decision → execution 的事件流还是走 in-process 内存
（HybridEventBus 的 NATS 通路尚未在 build_runtime 中接通）。

### 3.1 一进程一终端
开 4 个 WSL2 终端，分别 cd 到项目根目录，分别 export 环境变量：

终端 A — gateway：
```bash
export AATS_DATABASE_URL="..."
export AATS_STORAGE_MODE=postgres
export AATS_PROCESS_ROLE=gateway
python3 -m aats.api.main
```

终端 B — market：
```bash
export AATS_DATABASE_URL="..."
export AATS_STORAGE_MODE=postgres
export AATS_PROCESS_ROLE=market
python3 -m aats.api.main
```

终端 C — decision：
```bash
export AATS_DATABASE_URL="..."
export AATS_STORAGE_MODE=postgres
export AATS_PROCESS_ROLE=decision
python3 -m aats.api.main
```

终端 D — execution：
```bash
export AATS_DATABASE_URL="..."
export AATS_STORAGE_MODE=postgres
export AATS_PROCESS_ROLE=execution
python3 -m aats.api.main
```

### 3.2 验证 4 把 advisory lock 互不冲突
```bash
docker compose --env-file deploy/wsl2-dev/.env.wsl2 exec postgres \
  psql -U aats -d aats -c "SELECT objid, granted FROM pg_locks WHERE locktype='advisory';"
```
应当看到 4 条 granted=true 的记录，objid 各不相同（因为 per-role hash
派生）。

### 3.3 安全顺序停机
按相反顺序：execution → decision → market → gateway。每次 Ctrl+C 后等
5 秒确认 lock 已释放再停下一个。

---

## 4. 备份与恢复（Day 2+）

### 4.1 手动备份
```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev
RETENTION_DAYS=14 ./scripts/backup_postgres.sh
```
备份产出落到 `deploy/wsl2-dev/backups/`，文件名 `aats-YYYYmmdd-HHMMSS.dump`。
脚本会自动清理超过 14 天的旧备份。

### 4.2 设置每日定时备份（cron）
```bash
crontab -e
# 加一行：每天凌晨 03:00 备份
0 3 * * * cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev && RETENTION_DAYS=14 ./scripts/backup_postgres.sh >> /tmp/aats-backup.log 2>&1
```

### 4.3 恢复演练（建议每月一次）
```bash
cd /mnt/d/文件/project/AIParticipatingAutonomousTradingSystem/deploy/wsl2-dev
./scripts/restore_postgres.sh latest
# 脚本会要求输入 yes 确认
```
**演练前一定要先做一次最新备份**，恢复会清除当前数据库内容。

---

## 5. 故障排查 Cheat Sheet

### 5.1 服务起不来
```bash
docker compose --env-file .env.wsl2 logs --tail=100 <service_name>
```
常见原因：
- 端口被占（5432/6379/4222 等）→ 关掉宿主 Postgres / 改 compose 端口映射
- 内存不足 → `docker stats` 看哪个容器在喊 OOM；调小 deploy 限额
- 数据卷权限 → `docker compose down -v` 然后重新 up（注意：会清空数据）

### 5.2 monolith 起来后立刻退出
查日志找 `database_single_runtime_guard_failed`：
- 上一个进程没有干净退出，advisory lock 还在
- 解决：`SELECT pg_advisory_unlock_all();` 在同一会话内不行，需要把对应
  pg client backend 杀掉：
  ```sql
  SELECT pid, usename, application_name FROM pg_stat_activity WHERE datname='aats';
  SELECT pg_terminate_backend(<pid>);
  ```

### 5.3 多进程 advisory lock 冲突
症状：4 个进程里有一两个起不来，日志报 `database_single_runtime_guard_failed`。
原因：可能两个进程的 AATS_PROCESS_ROLE 取了同样的值。
解决：检查 `env | grep AATS_PROCESS_ROLE`，确认 4 个终端各自不同。

### 5.4 NATS JetStream stream 没创建
等到 Stage 4 把 build_runtime 接通后，第一次启动会自动 ensure_stream。
如果看不到 stream，手动创建：
```bash
docker compose --env-file .env.wsl2 exec nats nats stream add AATS_EVENTS \
  --subjects "aats.*" --storage file --retention limits --discard old --max-age 7d
```

### 5.5 Grafana 看不到日志/trace
- 检查 datasource：Configuration → Data sources，应当有 Loki / Jaeger / Postgres
- Loki 数据源 URL 应该是 `http://loki:3100`（容器内 DNS）
- Jaeger 数据源 URL 应该是 `http://jaeger:16686`
- 都应该能 "Save & test" 通过

### 5.6 备份脚本失败
```bash
cd deploy/wsl2-dev
bash -x ./scripts/backup_postgres.sh 2>&1 | tee /tmp/backup-debug.log
```
最常见：`POSTGRES_PASSWORD` 没正确读到（.env.wsl2 没有 export）。
解决：脚本内 `set -a; source .env.wsl2; set +a` 应该能加载。

---

## 6. 升级 / 迁移（Day N）

### 6.1 跑新 SQL migration
项目使用 `migrations/*.sql`，每次 build_runtime 会自动 apply。
如果要单独触发，写一个临时脚本：
```python
from aats.bootstrap.config import build_runtime
import asyncio
asyncio.run(build_runtime())
```
启动后查 `schema_migrations` 表确认新版本号已记录。

### 6.2 升级 docker 镜像
```bash
docker compose --env-file .env.wsl2 pull
docker compose --env-file .env.wsl2 up -d
```
**升级前一定要先备份**：`./scripts/backup_postgres.sh`。

### 6.3 添加新 process role
如果未来需要拆出第 5 个进程（例如 dashboard_proc）：
1. 在 `aats/bootstrap/settings.py` 里加 `PROCESS_ROLE_DASHBOARD = "dashboard"`
   到 `ALLOWED_PROCESS_ROLES` 集合
2. 在 `aats/bootstrap/config.py` 的 `_build_*_slice` 里抽出对应切片
3. 跑 `tests/unit/test_process_role_settings.py` 验证 lock_key 派生
4. 更新本 runbook 第 3 节

---

## 7. 日常体检清单（每周一次）

- [ ] `docker compose ps` 全部 healthy
- [ ] `du -sh deploy/wsl2-dev/backups/` 没有失控膨胀
- [ ] 最近 7 天有备份产出
- [ ] Grafana → Loki 查询 `{app="aats"} |= "ERROR"` 无未读告警
- [ ] Jaeger → Service `aats-decision` 平均 span duration 没异常
- [ ] Postgres `pg_stat_activity` 没有长事务（`state='idle in transaction'` >5min）
- [ ] 主机磁盘剩余 > 20GB

---

## 8. 紧急停机

### 8.1 安全停机（推荐）
```bash
# 1) 反向顺序停 4 个进程：execution → decision → market → gateway
#    每个进程 Ctrl+C，等 lock 释放后再停下一个
# 2) 停基础设施
cd deploy/wsl2-dev
docker compose --env-file .env.wsl2 stop
```

### 8.2 强制停机（数据丢失风险）
```bash
docker compose --env-file .env.wsl2 down  # 注意：不带 -v，数据卷保留
```
绝对不要用 `down -v`，会清空 Postgres / Redis / NATS 数据。

### 8.3 紧急 kill 所有 AATS 进程
```bash
pkill -f "python.*aats.api.main"
```
然后等 30 秒后检查 `pg_locks` 确认 advisory lock 已释放。

---

## 9. 4 进程拓扑验证（每次大版本升级后跑一次）

> 这一节记录 Slice 6.1/6.2/6.3/6.4/6.5 合入后真跑 4 进程拓扑（gateway/market/
> decision/execution）的标准验收步骤。用来防止 bootstrap wiring 回归。
>
> 依赖：§1 基础设施已 up；`docker compose --env-file .env.wsl2 -f
> docker-compose.yml -f docker-compose.aats.yml up -d` 已启动 4 个 AATS 容器。

### 9.1 bootstrap log 冒烟

每次 rebuild 镜像后，4 个容器都应该打印下面这组 bootstrap 事件。缺一个都
需要回到 build_runtime 排查 wiring：

```bash
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep -E \
    "kill_switch_initialized|portfolio_snapshot_cache_initialized|portfolio_repo_cache_listener_attached|obligation_hot_state_cache_initialized" \
    | head -5
done
```

期望每个容器都有四条：
- `kill_switch_initialized bootstrap_state={... subscribed: true ...} process_role=<role>`
  （Slice 6.4：合并后的 KillSwitch sidecar 边车订阅成功）
- `portfolio_snapshot_cache_initialized bootstrap_state={... bootstrapped: true ...}`
  （Slice 6.3：snapshot_cache 从 Redis hydrate 完成）
- `portfolio_repo_cache_listener_attached process_role=<role>`
  （Slice 6.3 Cache Fix：save_snapshot → cache listener hook 已接线）
- `obligation_hot_state_cache_initialized bootstrap_state={... bootstrapped: true ...}`
  （Slice 6.5：obligation_cache 从 Redis hydrate 完成；随后 `_wire_event_subscriptions`
  会经 `_CollectingBus` 调 `register_remote_subscription`，成功后再打一条
  `obligation_cache_subscribed topic=execution.obligation_updates`）

### 9.2 kill_switch 跨进程 halt/resume 真跑 drill

用 `probe_kill_switch.py` 作为"第 5 个临时进程"注入 halt/resume，然后读 4 个
运行时容器的日志，验证 ``kill_switch_remote_applied`` 被所有 4 个容器收到。

```bash
# 把 probe 脚本扔进 gateway 容器（任一容器都行）
docker cp deploy/wsl2-dev/probe_kill_switch.py aats-gateway:/tmp/probe_kill_switch.py

# 查当前 Redis 状态（基准）
docker exec aats-gateway python /tmp/probe_kill_switch.py status

# 注入 halt
docker exec aats-gateway python /tmp/probe_kill_switch.py halt "runbook-drill"

# 验证 4 个容器都收到 halt=true 广播
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep "kill_switch_remote_applied" | tail -2
done

# 注入 resume
docker exec aats-gateway python /tmp/probe_kill_switch.py resume

# 再次验证 4 个容器都收到 halted=false 广播
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep "kill_switch_remote_applied" | tail -2
done
```

每个容器都应该看到两条 `kill_switch_remote_applied`——一条 `halted=True`，
一条 `halted=False`，`source_role=drill_probe`，时间戳相差数十毫秒内（都是
同一次 NATS 广播）。

### 9.3 portfolio_repo → cache listener 真跑验证

用 `probe_repo_cache_listener.py` 在容器内独立构造 repo+cache，验证 Slice 6.3
Cache Fix 的 listener hook 在运行时 env 下能正常工作：

```bash
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker cp deploy/wsl2-dev/probe_repo_cache_listener.py "$c":/tmp/probe_repo_cache_listener.py
  docker exec "$c" python /tmp/probe_repo_cache_listener.py | tail -5
done
```

每个容器都应该打印 `[probe] OK: cache hit decision_id=probe-cache-fix-<ts>`。
任何一个失败都说明 build_runtime 注入 listener 的 wiring 被改坏了，回到
`aats/bootstrap/config.py` 的 `portfolio_repo_cache_listener_attached` 日志行
附近排查。

### 9.4 清理 drill 痕迹

`probe_kill_switch.py` 的 bootstrap 会在 NATS JetStream 里创建
`aats-drill_probe-system_kill_switch_state` 这个 durable consumer；多次跑
drill 会越攒越多。定期清一下：

```bash
docker exec aats-nats nats consumer ls AATS_EVENTS | grep drill_probe | \
  awk '{print $1}' | xargs -I{} docker exec aats-nats nats consumer rm AATS_EVENTS {}
```

Redis 里的 `aats:hot:system:kill_switch` 在 resume 后会回到 `halted=false`，
不需要专门清。

### 9.5 Stage 8 OpenTelemetry + Jaeger 4 进程 trace 链路验证

Stage 8 的落地验收：4 个 AATS 进程全部开启 OTel，一条跨进程事件的 trace
能够在 Jaeger UI 里展开成"producer 的 `nats.publish.<topic>` → 多个
consumer 的 `nats.receive.<topic>`"的树状结构。本节只跑验证，不改数据。

#### 9.5.1 前置：镜像已带 `.[otel]` extras

```bash
docker run --rm aats-base:dev python -c \
  "import opentelemetry; import opentelemetry.sdk; \
   import opentelemetry.exporter.otlp.proto.grpc; print('OK')"
```

期望输出：`OK`。
镜像若没有 opentelemetry，回到 `docker compose -f docker-compose.aats.yml
--env-file .env.wsl2 build` 重打一次 —— Dockerfile 里已经改成 `pip install
-e ".[nats,redis,otel]"`。

#### 9.5.2 每个 AATS 容器启动时都要打出 `telemetry_configured`

```bash
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep -E 'telemetry_configured|telemetry_otel_not_installed|telemetry_bootstrap_failed' | head -2
done
```

期望每个容器都有一行 `telemetry_configured endpoint=http://jaeger:4317
process_role=<role> service_name=aats-<role>`。没有 `otel_not_installed`
或 `bootstrap_failed`。

#### 9.5.3 Jaeger 里 4 个 service 都已注册

```bash
curl -s 'http://127.0.0.1:16686/api/services'
```

期望 data 字段包含 `["aats-decision","aats-execution","aats-gateway",
"aats-market"]` 四个条目。

#### 9.5.4 生成 gateway HTTP ingress span

```bash
for i in 1 2 3 4 5; do curl -sf http://127.0.0.1:8000/ > /dev/null; done
sleep 8
curl -s 'http://127.0.0.1:16686/api/traces?service=aats-gateway&lookback=2m&limit=5' | \
  python3 -c 'import sys, json; d = json.load(sys.stdin); \
  print([t["spans"][0]["operationName"] for t in d["data"][:5]])'
```

期望输出里至少包含 `gateway.http.GET /`。

#### 9.5.5 跨进程 trace 树（核心验证）

让 execution → portfolio snapshot 自然触发一次 fan-out（等 1-2 分钟即可,
execution 的 reconciliation 后台周期会做）：

```bash
sleep 90
curl -s 'http://127.0.0.1:16686/api/traces?service=aats-decision&lookback=3m&limit=20' | \
  python3 -c '
import json, sys
d = json.load(sys.stdin)
multi = [t for t in d["data"] if len(t["spans"]) > 2]
for t in multi[:3]:
    print("trace", t["traceID"][:16], "processes",
          sorted({p["serviceName"] for p in t["processes"].values()}))
    for s in sorted(t["spans"], key=lambda x: x["startTime"]):
        svc = t["processes"][s["processID"]]["serviceName"]
        pref = [r for r in s["references"] if r["refType"] == "CHILD_OF"]
        parent = pref[0]["spanID"][:8] if pref else "(root)"
        print("  ", svc, s["operationName"], "parent=" + parent)
'
```

期望看到类似这样的树结构（Stage 8-4b 之后修好）：

```
trace e264c950cae0d2aa  processes ['aats-decision','aats-execution','aats-gateway','aats-market']
   aats-execution  nats.publish.portfolio.snapshots        (root)
   aats-decision   nats.receive.portfolio.snapshots        parent=<publish>
   aats-market     nats.receive.portfolio.snapshots        parent=<publish>
   aats-execution  nats.receive.portfolio.snapshots        parent=<publish>
   aats-gateway    nats.receive.portfolio.snapshots        parent=<publish>
   aats-execution  nats.publish.reconciliation.reports     parent=<execution receive>
   aats-decision   nats.receive.reconciliation.reports     parent=<execution publish>
```

核心断言：
- **同一个 trace_id 跨至少 3 个 service**（4 个最理想）
- `nats.receive.<topic>` span 的 parent 是 `nats.publish.<topic>`（不是再
  往上的 handler span；如果是 handler span 说明 inject 被放在了 publish
  span 外面，Stage 8-4b 之前就是这样的 bug，看到就回退到 8-4b 的 commit
  hash 重新部署）
- 所有 `nats.publish.*` 与 `nats.receive.*` span 都带
  `messaging.system="nats"` / `aats.topic=<topic>` / `aats.event_id=<uuid>`
  attrs（用 Jaeger UI 点开 span 详情能看到）

#### 9.5.6 Jaeger UI 人工验证（可选）

```
打开浏览器：http://localhost:16686/
Service 选 aats-execution
Operation 选 nats.publish.portfolio.snapshots
点 Find Traces，在 trace 列表里挑 1 条 span 数 ≥ 4 的
点开看 timeline，确认 receive span 紧贴 publish span，不是跨 handler 错位
```

#### 9.5.7 生产灰度前降采样率

docker-compose.aats.yml 默认 `AATS_OTEL_SAMPLE_RATIO=1.0`（全采样适合
drill/dryrun），实盘 1U/10U 阶段保持 1.0 便于排障，到 100U/1000U 再降到
0.1 或 0.05：

```yaml
# deploy/wsl2-dev/docker-compose.aats.yml 的 x-aats-common-env 里改
AATS_OTEL_SAMPLE_RATIO: "0.1"
```

修改后 `docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d`
重建即可，不用 rebuild 镜像。

### 9.6 Stage 9 drift score CLI 真跑验证

Stage 9 checklist-3 落地的 `scripts/compute_drift_score.py` 是 dryrun
升阶梯 gate 的自动化入口（见 `docs/task/stage_9_dryrun_checklist.md` §4.4）。
本节是大版本升级后必须跑一遍的冒烟：CLI 能正确读 artifact → 算分 →
退出码符合设计 §6.2。

#### 9.6.1 mock 源冒烟（不依赖 artifacts）

```bash
python scripts/compute_drift_score.py --stage T1 --source mock
echo $?
```

期望：
- 人类可读报告里能看到 `Stage 9 Drift Score — T1 (nominal 1 USDT)`
- TOTAL SCORE 行显示 0 或接近 0
- `Ladder upgrade: ALLOWED`
- 退出码 0

`--source mock` 只用于 CLI 管道自检，**不要**在升阶梯决策时使用。

#### 9.6.2 offline 源（真 artifact 目录）

```bash
# 生成或更新 quality_monitor_summary / trial_guard_snapshot / portfolio
# snapshot 之后再跑
python scripts/compute_drift_score.py --stage T2 --source offline --verbose
echo $?
```

期望：
- 报告里显示每个子类的 subscore + 每个 indicator 的 raw / normalized
- 找不到的指标标 `*`（missing）
- 如果有 missing → `Ladder upgrade: BLOCKED`
- 退出码 0（clean）/ 2（noticeable or missing-data 阻断）/ 3（significant）
  / 4（critical）/ 1（运行错误）

> 具体 exit code 映射见 `docs/task/stage_9_abort_hooks_design.md` §6.2
> 与 `tests/unit/test_stage9_drift_score_cli.py::test_exit_code_*`。

#### 9.6.3 JSON 输出落盘

```bash
python scripts/compute_drift_score.py --stage T1 --source offline \
  --output artifacts/governance/drift_score_$(date +%Y%m%d_%H%M).json --json
```

落盘的 JSON 可以 diff 上次同阶梯的报告，快速看"哪个 subscore 变坏了"。
可以塞进 cron，每小时跑一次，落盘到 `artifacts/governance/drift_history/`
然后 grafana 面板读。

#### 9.6.4 单元测试回归（每次改了 CLI 或 drift_score 必跑）

```bash
pytest tests/unit/test_stage9_drift_score.py \
       tests/unit/test_stage9_drift_score_cli.py -q
```

期望：60 个测试全绿（41 + 19）。如果挂，回到对应测试函数看是哪个不变
量被破坏了，禁止在不修测试的情况下改实现代码。

### 9.7 Stage 9 AbortHookService sidecar halt 真跑 drill

Stage 9 checklist-4 的 `AbortHookService` 是定期评估 drift score 并在
命中时自动 `kill_switch.halt(reason=stage9_abort_hook:<code>)` 的后台
sidecar。验证它与 Redis + NATS 跨进程广播链路的 end-to-end：和
`probe_kill_switch.py` 完全同构，但 halt 触发源从"手动 probe"换成
"critical DriftInputs 驱动的 AbortHookService"。

> 本节要真动 kill_switch 状态，跑完**必须**执行 §9.7.5 resume 收尾，
> 否则 4 个运行时容器会一直 halted=true。

#### 9.7.1 前置：把 probe 脚本放进容器

```bash
docker cp deploy/wsl2-dev/probe_abort_hook.py aats-gateway:/tmp/probe_abort_hook.py
```

#### 9.7.2 self-check（纯 in-memory 冒烟，不动 Redis/NATS）

```bash
docker exec aats-gateway python /tmp/probe_abort_hook.py self-check
```

期望：
```
[probe] self-check: total_score=8 state=critical_drift action=halt_immediate
[probe] self-check: state=halting halts=1
[probe] self-check: kill_switch reason='stage9_abort_hook:score_ge_5'
[probe] OK: self-check passed
```

这一步失败说明 `AbortHookService` + `compute_drift_score` + `KillSwitch`
的 **本地配线** 坏了（import 路径、状态机、halt reason 生成之一），
看日志里 `abort_hook_state_transition` 有没有出现即可定位。

#### 9.7.3 halt drill（score_ge_5 critical 路径）

```bash
# 执行前确认当前 kill_switch 没有 halt（从之前 drill 残留的话先跑 resume）
docker exec aats-gateway python /tmp/probe_abort_hook.py status

# 触发 halt：probe 构造一个独立 AbortHookService + 真 Redis/NATS
# KillSwitch，喂进 total_score=8 的 critical inputs
docker exec aats-gateway python /tmp/probe_abort_hook.py halt-critical

# 验证 4 个运行时容器都收到 halt 广播
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep "kill_switch_remote_applied" | tail -2
done
```

期望每个容器都打印一条 `kill_switch_remote_applied halted=True
reason=stage9_abort_hook:score_ge_5 source_role=stage9_probe`。

如果某个容器**没看到**这条日志 → 4 进程 NATS 广播链路坏了（或该容器
没起来），回去看 §9.2 kill_switch drill 能不能过。两个 drill 都挂说
明是广播层问题，只有 §9.7 挂说明 `_trigger_halt` 路径坏了。

#### 9.7.4 halt drill（subscore_financial_2 与连续 warning 路径）

score_ge_5 覆盖主路径，再跑两条支路验证 halt reason 编码：

```bash
# 先 resume 清掉上一次的 halt
docker exec aats-gateway python /tmp/probe_abort_hook.py resume

# 支路 A：仅 financial 子类全 critical（subscore=2），total=3
docker exec aats-gateway python /tmp/probe_abort_hook.py halt-subscore-financial
for c in aats-gateway aats-market aats-decision aats-execution; do
  docker logs "$c" 2>&1 | grep "subscore_financial_2" | tail -1
done

docker exec aats-gateway python /tmp/probe_abort_hook.py resume

# 支路 B：连续 2 次 warning（score=4 两次）
docker exec aats-gateway python /tmp/probe_abort_hook.py halt-consecutive
for c in aats-gateway aats-market aats-decision aats-execution; do
  docker logs "$c" 2>&1 | grep "score_3_4_consecutive_2" | tail -1
done
```

期望：支路 A 的 reason 含 `stage9_abort_hook:subscore_financial_2`，
支路 B 的 reason 含 `stage9_abort_hook:score_3_4_consecutive_2`。

#### 9.7.5 drill 收尾：resume + 清理 probe durable consumer

```bash
# 把 kill_switch 从 halted 状态解出来
docker exec aats-gateway python /tmp/probe_abort_hook.py resume

# 确认 4 进程都收到 halted=false
for c in aats-gateway aats-market aats-decision aats-execution; do
  docker logs "$c" 2>&1 | grep "kill_switch_remote_applied" | tail -1
done

# 清掉 probe 在 NATS JetStream 里留下的 stage9_probe durable consumer
docker exec aats-nats nats consumer ls AATS_EVENTS 2>/dev/null | \
  grep stage9_probe | awk '{print $1}' | \
  xargs -I{} docker exec aats-nats nats consumer rm AATS_EVENTS {} -f || true

# Redis 里 kill_switch 回到 halted=false 不用专门清
docker exec aats-gateway python /tmp/probe_abort_hook.py status
```

期望：最后一次 `status` 打印 `halted=False`。

#### 9.7.6 打开运行时 sidecar 的环境变量（T0 DRY 浸泡前）

T0 DRY 24h 浸泡期间开启 abort_hook sidecar 跑一次，**不**关 kill_switch
（让它只记 log 不真 halt），回到 dryrun 清单 §1.6 之前必须确认：

在 `.env.wsl2` 里加：

```bash
# Stage 9 AbortHookService 配置
AATS_STAGE9_ABORT_HOOK_ENABLED=true
AATS_STAGE9_ABORT_HOOK_EVALUATE_INTERVAL_SECONDS=60.0
AATS_STAGE9_CURRENT_STAGE=T0
AATS_STAGE9_ABORT_HOOK_CONSECUTIVE_WARNINGS=2
AATS_STAGE9_ABORT_HOOK_COOLDOWN_SECONDS=1800.0
```

重启后：

```bash
cd ~/aats/deploy/wsl2-dev
docker compose -f docker-compose.aats.yml --env-file .env.wsl2 up -d --force-recreate

# 每个进程都应该打印一条 abort_hook_service_started
for c in aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep -E "abort_hook_service_(started|disabled)" | tail -2
done
```

checklist-4 的配线是 `_slice_active("startup_recovery")`，所以只有
decision + execution + monolith role 下会启动 AbortHookService；gateway
与 market 容器应该**没有** `abort_hook_service_started` 日志（也没有
`abort_hook_service_disabled` —— sidecar 根本不会构造）。

24h 浸泡后：

```bash
# 确认 T0 DRY 期间没有意外 halt
for c in aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep -E "abort_hook_state_transition" | wc -l
  docker logs "$c" 2>&1 | grep -E "abort_hook_state_transition.*new_state=halting" | tail -5
done
```

- 第一行是 state transition 总次数 —— 开头应该只有 1 次（disabled →
  monitoring 或 monitoring 初始化），然后稳定
- 第二行应该为空 —— `new_state=halting` 即 sidecar 决定 halt。checklist-4
  收集的 inputs 几乎全 missing，baseline 跑出来 score=0 action=none，
  理论上 24h 内不应有任何 halting transition
- 如果看到 halting → 立刻人工复盘：要么 inputs_collector 拿到了"假阳性"
  数据（去看 trial_guard 那几个字段的 raw），要么 checklist-5 的真
  inputs collector 接进来之后被 drift score threshold 当场打中

### 9.8 Stage 6 Slice 6.5 ObligationHotStateCache 跨进程真跑验证

Slice 6.5 给 obligation 读写路径插了一层跨进程 cache（本地 dict + Redis
per-coid + NATS `execution.obligation_updates`）。本节验证 4 个容器全部
bootstrap 成功、writer→reader 跨进程广播在 1 秒内可见、`active_sync()`
读路径收敛。设计文档见
`docs/task/stage_6_slice_6_5_obligation_hot_state_design.md`。

> 本节不真动 obligation 数据，只读 Redis / NATS 状态和 `/__internal/runtime`
> dashboard，跑完不需要清理。如果想手动 publish 一条假 obligation 进去测
> 广播，可以跑 §9.8.3 里的 `probe_obligation_cache.py`（如果还没写就走
> §9.8.2 的"等一次真 fill"被动验证路径）。

#### 9.8.1 bootstrap log 冒烟（与 §9.1 配合）

```bash
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" 2>&1 | grep -E \
    "obligation_hot_state_cache_initialized|obligation_cache_subscribed" \
    | head -3
done
```

期望每个容器各打印两条：
- `obligation_hot_state_cache_initialized bootstrap_state={... bootstrapped: true,
  subscribed: false ...}` ——cache 构造完成并从 Redis hydrate 了 index
  （第一次启动 / Redis 清空 → cached_count=0 正常）
- `obligation_cache_subscribed topic=execution.obligation_updates
  process_role=<role>` ——`_wire_event_subscriptions` 经 `_CollectingBus`
  聚合后成功把 `_handle_remote_event` 挂到 NATS JetStream durable consumer
  上。**缺任何一条**说明 bootstrap wiring 被改坏了或 NATS stream 没
  创建对应 subject，先看 `obligation_cache_subscribe_failed` 的 warning
  行有没有带 error。

I3 restart-safe 快速验证：重启任何一个容器后这两行都应再次出现，且
`obligation_hot_state_cache_initialized` 的 `bootstrap_state.cached_count`
在 Redis 有数据的情况下会是 ≥1（从 `aats:hot:obligation:index` 的
`all_coids` 列表 + get_many 一次性拉回）。

#### 9.8.2 Redis 侧状态巡检

Slice 6.5 的 Redis 布局与 6.3 snapshot_cache 独立，两套 namespace 互不
干扰：

```bash
# Redis 里的 obligation namespace 内容
docker exec aats-redis redis-cli --no-raw KEYS 'aats:hot:obligation:*' | head

# index key 里的 all_coids / active_coids / version
docker exec aats-redis redis-cli GET 'aats:hot:obligation:index'
```

期望：
- 4 个容器都起来之后，`KEYS` 应该能看到 `aats:hot:obligation:index` 和
  若干 `aats:hot:obligation:by_coid:<client_order_id>` per-coid key
  （第一次全新启动时可能只有 index 一个空表）
- `GET aats:hot:obligation:index` 返回 JSON 结构 `{"all_coids": [...],
  "active_coids": [...], "version": N}`。version 单调递增即正常；
  4 个容器读到的是同一份（Redis 是 source of truth），不同容器同时跑
  读不会看到回退

**⚠️ 禁止手动 DEL 这些 key**：cache bootstrap 会重新 hydrate，但
obligation_repo（Postgres）才是 source of truth，误删 index 会让
`_handle_remote_event` 启动瞬间短暂"miss→fallback PG"。如果真要清，
先在业务闲时、确认 4 个容器都停了再干。

#### 9.8.3 跨进程广播被动验证（等一次 fill）

4 个容器都 up、paper/live 数据在跑之后，随便下一单成交走完 obligation
reserve → consume → finalize 全链路：

```bash
# 1. 先记录 execution 容器当前的 obligation publish 计数
docker exec aats-execution python - <<'PY'
import json
from aats.bootstrap.logging import get_logger
# dashboard 走 OperatorQueryService → runtime.obligation_hot_state_cache
# 这里直接读 runtime 内存结构是不可能的（runtime 在 PID 1 里跑），所以
# 改成拉 operator /__internal/runtime 的 JSON 输出
import urllib.request, urllib.error
try:
    with urllib.request.urlopen("http://localhost:8080/__internal/runtime/summary", timeout=2) as r:
        j = json.load(r)
    print(json.dumps(j.get("obligation_hot_state_cache"), ensure_ascii=False))
except urllib.error.URLError as e:
    print("operator HTTP not up:", e)
PY

# 2. 触发一次 paper fill（或者等真单成交），下面以 sample_intent 脚本为例
docker exec aats-decision python /app/scripts/sample_intent.py --symbol BTC-USDT --amount 0.0001

# 3. 4 个容器应该在 ≤1s 内都看到同一个 client_order_id 的
#    obligation_cache_handle_remote_event 日志
for c in aats-gateway aats-market aats-decision aats-execution; do
  echo "=== $c ==="
  docker logs "$c" --since 30s 2>&1 | grep \
    "obligation_cache_handle_remote_event" | tail -3
done
```

期望：
- execution 容器自己是 writer，会在 fill commit 后打
  `obligation_cache_publish_ok client_order_id=<coid>` 或
  `obligation_cache_local_apply_skip reason=stale_or_equal_ts`
  （D9 idempotent：同一条事件 apply 第二次 noop）
- 另外 3 个容器（gateway / market / decision）都会在 ≤1 秒内看到
  `obligation_cache_handle_remote_event client_order_id=<coid>
  result=applied`。**任何一个容器收不到** → NATS stream 缺 subject
  或 consumer binding 没起：先去 §9.2 的 `kill_switch_remote_applied`
  对照检查 NATS 链路本身是否健康；如果 kill_switch 跨进程链路正常但
  obligation 跨不过去 → 检查 9.1 步里 `obligation_cache_subscribed`
  是否缺某个容器、再去看 `obligation_cache_subscribe_failed` warning。

#### 9.8.4 risk_engine / dashboard 读路径走 cache 验证

Slice 6.5 的主要收益点是 decision 的 `risk._active_local_obligations()`
从每次打 Postgres `obligation_repo.active_obligations()` 改成先
`cache.active_sync()` → fallback PG。验证 cache 确实被走到：

```bash
# decision 容器里 probe 一下 cache 的 snapshot
docker exec aats-decision python - <<'PY'
import json, urllib.request, urllib.error
try:
    with urllib.request.urlopen("http://localhost:8080/__internal/runtime/summary", timeout=2) as r:
        j = json.load(r)
    obl = j.get("obligation_hot_state_cache") or {}
    print("process_role:", obl.get("process_role"))
    print("bootstrapped:", obl.get("bootstrapped"))
    print("subscribed:", obl.get("subscribed"))
    print("cached_count:", obl.get("cached_count"))
    print("active_count:", obl.get("active_count"))
    print("index_version:", obl.get("index_version"))
except urllib.error.URLError as e:
    print("operator HTTP not up:", e)
PY
```

期望：
- `bootstrapped=True` / `subscribed=True`
- 业务跑一段时间之后 `cached_count` 与 `active_count` 非 0（与
  obligation_repo 的 count_obligations / count_active 对齐就行，
  可能差 ±1 因为异步广播有 lag）
- `index_version` 单调递增，4 个容器读到的 version 不必相等（每个进
  程本地记录自己接收到的 bump 次数）

I5 miss-不破坏读的快速验证：临时 stop Redis，decision 容器的 risk
pre-check 应当继续跑（日志里会出现 `obligation_cache_active_sync_miss
fallback=obligation_repo` 这条 warning，然后正常走 `active_obligations()`
PG 查询），**不能**看到 RiskEngine 自己 500 或 trigger halt。恢复
Redis 之后下一次 publish 就会重新 hydrate cache。

> ⚠️ stop Redis 的 drill 会让 portfolio_snapshot_cache 同时走 fallback
> 路径；同时 kill_switch sidecar 订阅的 Redis `kill_switch` key 也会
> short-term 读不到。跑之前先用 §9.2 确认手动 halt/resume 路径可用，
> 并且 §9.7.5 把 drill 痕迹清干净，不要和 Stage 9 probe 同时跑。

#### 9.8.5 D9 idempotent + I4 乱序事件验证（可选）

实在要手动注一条 obligation 广播事件验证 D9 的乱序 noop，可以走：

```bash
# 用 python -m aats.scripts.probe_obligation_cache（如果脚本还没写，
# 先跳过这一步；cache 单测 tests/unit/test_obligation_hot_state_cache.py
# 已经覆盖了 D9 + I4 的所有乱序场景，真跑主要是防 NATS wiring 回归）
docker exec aats-decision python -m aats.scripts.probe_obligation_cache --help 2>&1 | head -5 || true
```

如果 `probe_obligation_cache` 尚未实装，本条占位；Slice 6.5 的 D9/I4
回归已经由 `tests/unit/test_obligation_hot_state_cache.py` 的
`TestObligationHotStateCacheIdempotent` / `TestObligationHotStateCacheRemoteEvent`
32 条单测覆盖，真跑不必重复验证幂等语义，只需关注 §9.8.3 的广播可见性
与 §9.8.4 的 fallback 路径。
