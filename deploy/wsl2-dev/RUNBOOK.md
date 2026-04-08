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

> 这一节记录 Slice 6.1/6.2/6.3/6.4 合入后真跑 4 进程拓扑（gateway/market/
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
    "kill_switch_initialized|portfolio_snapshot_cache_initialized|portfolio_repo_cache_listener_attached" \
    | head -5
done
```

期望每个容器都有三条：
- `kill_switch_initialized bootstrap_state={... subscribed: true ...} process_role=<role>`
  （Slice 6.4：合并后的 KillSwitch sidecar 边车订阅成功）
- `portfolio_snapshot_cache_initialized bootstrap_state={... bootstrapped: true ...}`
  （Slice 6.3：snapshot_cache 从 Redis hydrate 完成）
- `portfolio_repo_cache_listener_attached process_role=<role>`
  （Slice 6.3 Cache Fix：save_snapshot → cache listener hook 已接线）

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
