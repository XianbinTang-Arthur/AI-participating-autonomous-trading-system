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
