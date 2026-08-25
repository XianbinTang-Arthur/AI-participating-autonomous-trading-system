# AATS Safe Shutdown 脚本设计 (2026-04-20)

> **状态（2026-08-22 核对）**：历史设计，已经由 `scripts/ops/safe_shutdown.sh` 实现。当前操作命令以根目录 `DEPLOYMENT.md` 为准；本文的“没有脚本”和设计阶段措辞只描述 2026-04-20 当时背景。

> **当前禁止执行**：本文中的旧 restart/deploy 示例没有必填模拟 profile，且不满足 Phase 3F live 硬禁用与 evidence 契约；只可作为历史证据阅读。

**背景（历史）**: 2026-04-20 用户指令 "关闭所有服务, 等路线稳定了再发布". 当时项目没有安全停机脚本, 之前用法是 `scripts/deploy.sh` (启动) 和 `docker compose down --timeout 5` (粗暴停). 后者对**真金白银系统**不安全.

**目标**: 一个 operator-friendly 的停机脚本, 不让 OKX 端不一致, 不丢未落盘数据, 可审计, 可恢复.

---

## 1. 安全停机必须满足的 5 条

### 1.1 OKX 端一致性
- **停 `aats-execution` 前**: 确认没有 **in-flight order submission / cancel / fill ack** 在网络上飞
- **停 `aats-execution` 后**: OKX 端可能仍有**挂单 (open orders)** 和**仓位 (positions)** — 脚本**只停进程, 不动 OKX 端**
- **Operator 可见性**: 停机前必须打印 `open_orders_count / open_positions_count / total_notional_usd`, operator 自行决定手动 cancel/close 还是带持仓停机

### 1.2 数据完整性
- **Postgres**: 最后停. 停之前让所有 app 先停, 等已提交事务 flush. 用 `pg_ctl stop -m fast` (等 active transactions) 而非 `-m immediate`
- **Redis**: 停之前触发 `BGSAVE` 把内存快照落到 `dump.rdb`
- **NATS JetStream**: 流式数据已持久化到磁盘, in-memory consumer state 丢失可接受 (重启后 replay)
- **Microstructure collector buffer**: 进程收 SIGTERM 后应有 graceful flush hook 把 buffer 里未 commit 的 rows 刷到 Bronze. **若无 → 可接受数据损失 (≤5s Bronze 数据)**, 记录在 shutdown snapshot

### 1.3 信号不 limbo
- Market Gateway → NATS → Decision → Execution → OKX 数据流
- **逆流关闭**: Execution 先停 (no new orders), Decision 次之 (no new signals), Market Gateway 再次 (OKX WS 优雅 close)
- 每步之间 **2-3s grace** 让 in-flight 消息 drain

### 1.4 Observable + Auditable
- 停机前 dump **shutdown snapshot JSON** 到 `artifacts/shutdown_snapshots/YYYYMMDD_HHMMSS_<reason>.json`, 含:
  - git HEAD
  - 所有容器状态 + runtime
  - OKX open orders / positions (from last cached snapshot in Postgres)
  - 最近 5 min decision_outcome 样本
  - Silver 最新 ts (per 表)
  - 操作人 + reason + dry-run/apply flag
- 操作到 operator audit stream (`public.event_store` topic `system.operator_actions`, 若 Postgres 还开)
- **stdout 结构化** (便于 tee 到 log 文件)

### 1.5 可恢复
- 停机不改 compose / config / env 任何文件 (无副作用)
- 重启流程: 用户手动 `bash scripts/deploy.sh --skip-commit` 即可恢复
- 不清理 volumes (Postgres / Redis / NATS 数据全保留)

---

## 2. 三阶段流程

### Phase 0 — Preflight (read-only, 强制)

**检查项** (全部只读, 不依赖生产 code path):

1. **容器现状**: `docker ps` 列出所有 aats-* 容器 + status
2. **Postgres 可达性**: `docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "SELECT 1"` 成功
3. **OKX 端快照** (从 Postgres cache 读, 不发 OKX REST):
   - `public.event_store` 最新 `account.snapshots`: balances / open_orders / positions / fills
   - 若 `open_order_count > 0` → WARN, 停机后订单留在 OKX, 可能被 OKX 撮合
   - 若 `position_count > 0` 且 `net_notional_usd > 0` → WARN, 带仓停机
4. **In-flight reconciliation**: `SELECT COUNT(*) FROM execution_orders WHERE state IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')` — 非 0 → WARN
5. **最近 decision outcome**: 最后 3 条 `strategy.decision_outcome` 的 `final_action` — 若 ≠ hold, 有实际下单迹象, WARN
6. **当前 runtime mode**: `ai_operating_mode` — 若 ≠ `baseline_only`, WARN (实盘中停机)

**Preflight 结果决定下一步**:
- **Clean** (无 open orders / 无 positions / 无 in-flight / baseline_only): 直接进 Phase 1
- **With money** (any warning): 要求 `--force-with-money` 明确 flag, 或 operator 中止并手动处理
- **Abort**: 若 Postgres 不可达或其他关键 precondition 失败, 打印原因不继续

### Phase 1 — App-layer graceful shutdown (逆流关闭)

**步骤 (每步停完后 2-3s grace)**:

| Step | 容器 | SIGTERM 后等待 | 理由 |
|---|---|---|---|
| 1.1 | `aats-execution` | 10s | 停发新订单, flush in-flight OKX REST |
| 1.2 | `aats-decision` | 5s | 停 decision tick, 在 execution 之后确保最后一个 decision 已被 execution 处理 |
| 1.3 | `aats-rdp-daemon` | 10s | 等当前 workflow task 结束 (silver ETL 5s 级, catch-up 长些) |
| 1.4 | `aats-microstructure-collector` | 5s | SIGTERM 触发 buffer flush (若已实现) |
| 1.5 | `aats-liquidations-daemon` | 5s | 同上 |
| 1.6 | `aats-market` | 5s | OKX WS close frame 发出 |
| 1.7 | `aats-gateway` | 5s | HTTP listener close |

每步 `docker stop --time <N> <container>`, 失败但 container 仍 running → 升级 `docker kill` + 记录 force_kill 标志到 snapshot.

### Phase 2 — Infra shutdown

| Step | 容器 | 等待 | 理由 |
|---|---|---|---|
| 2.1 | `aats-grafana` | 5s | UI 层 |
| 2.2 | `aats-prometheus` | 5s | Scraper 先停 |
| 2.3 | `aats-promtail` | 5s | Log shipper |
| 2.4 | `aats-loki` | 10s | Log store, 等 write buffer flush |
| 2.5 | `aats-jaeger` | 5s | 追踪 |
| 2.6 | `aats-redis-exporter` | 3s | Metrics |
| 2.7 | `aats-redis` | 10s | **停前触发 `BGSAVE`**, 等 dump.rdb 写完 |
| 2.8 | `aats-nats` | 10s | JetStream flush |
| 2.9 | `aats-postgres` | 30s | **最后停**, 用 `pg_ctl stop -m fast` 等 active txn |

### Phase 3 — 验证 + 报告

1. `docker ps` 确认所有 aats-* 都不在 running 列表
2. 打印最终 snapshot JSON path
3. 打印重启命令: `bash scripts/deploy.sh --skip-commit`
4. 退出码: 0 (全部 graceful) / 1 (有 force_kill) / 2 (部分容器未停) / 3 (preflight abort)

---

## 3. CLI 接口

```
scripts/ops/safe_shutdown.sh [OPTIONS]

OPTIONS:
    --dry-run              (默认) 只跑 preflight + 打印计划, 不实际停
    --apply                实际执行停机
    --confirm              与 --apply 配对, 必须显式加
    --force-with-money     允许在 open orders / positions 非空时继续
    --skip-preflight       紧急停机, 跳过 preflight (不推荐)
    --reason <TEXT>        停机理由 (默认 "manual_shutdown", 会写入 snapshot)
    --preserve-postgres    停 apps + infra 但保留 Postgres 运行 (便于停机后查数据)
    --timeout-app-layer N  覆盖 Phase 1 默认超时 (默认 10s)
    --timeout-postgres N   覆盖 Postgres graceful 超时 (默认 30s)
```

**示例**:

```bash
# 默认 dry-run, 看会做什么
bash scripts/ops/safe_shutdown.sh --reason "war_room"

# 真正执行
bash scripts/ops/safe_shutdown.sh --apply --confirm --reason "war_room"

# 紧急带仓停机 (operator 已知风险)
bash scripts/ops/safe_shutdown.sh --apply --confirm --force-with-money --reason "emergency"

# 只停 apps 保留 DB 查数据
bash scripts/ops/safe_shutdown.sh --apply --confirm --preserve-postgres --reason "data_audit"
```

---

## 4. 安全 guardrails (不可绕过)

1. **必须 `--apply --confirm` 双 flag 才真停** (防手抖)
2. **preflight 告警 + 未加 `--force-with-money` → 脚本自 abort, 不继续**
3. **不改任何文件** (env / config / compose / code)
4. **不清 volumes** (`docker volume rm` 绝对不做)
5. **不 pull / 不 push / 不 commit**
6. **必有 audit trail** (shutdown snapshot 写到 artifacts/, 若 Postgres 还开, 同时写 event_store)
7. **运行在 operator 主机** (Windows host via bash wsl), 不在容器内

---

## 5. 失败模式处理

### 5.1 Preflight 失败

- Postgres 不可达 → abort (不知道 OKX 端状态)
- 例外: `--skip-preflight` 可绕过, 但记录 in snapshot

### 5.2 某步 docker stop 超时

- 升级 `docker kill <container>` (SIGKILL)
- 记录 `force_kill: true` 到 snapshot
- 继续下一步 (不 abort, 因为下一步不会因前一步 force kill 而更糟)

### 5.3 Postgres 拒绝 `pg_ctl stop -m fast`

- 说明还有长事务未完成
- 选项: 等更长超时 (`--timeout-postgres`) / force kill / 中止操作
- 默认 30s 超时后升级 force kill, 警告数据层面可能不一致

### 5.4 Snapshot 写入失败

- stdout 直接 dump JSON (operator 可人工保存)
- 退出码 +warning, 不 abort shutdown

---

## 6. 不在本设计范围的

- **自动 cancel OKX 挂单** (operator 决定)
- **自动 close OKX 仓位** (operator 决定)
- **Kill switch 触发** (独立路径, 已有 `scripts/operator_kill_switch.sh` 等价物, 若无单独立项)
- **重启自动化** (`deploy.sh` 负责启动, 已有)
- **数据备份** (独立 backup 流程)

---

## 7. 测试

### 7.1 Dry-run 测试

- `--dry-run` 模式下跑一次, 验证 preflight 读取正确 + 计划合理
- 不预期副作用 (docker ps 前后一致)

### 7.2 Apply 测试 (需 test env, 不在本设计)

- 干净环境 (无 open orders / positions) → 全 graceful stop
- 带仓环境 → preflight warn → 需 `--force-with-money`
- Postgres 慢事务环境 → 验证 pg_ctl fast 模式等 active txn

---

## 8. 实施路径

1. **Step 1**: 写 `scripts/ops/safe_shutdown.sh` (bash, ~300-400 行)
2. **Step 2**: 本文档 + 脚本 commit 到 main
3. **Step 3**: 用户 review + dry-run 一次
4. **Step 4**: 若 dry-run 结果符合预期, `--apply --confirm` 真正停机
5. **Step 5**: 停完后验证所有容器 down + snapshot 完整

---

## 9. 签署

- 起草: Claude Opus 4.7 · 2026-04-20
- 触发: 用户 2026-04-20 "关闭所有服务, 等路线稳定了再发布" directive
- 批准状态: 待用户 review 设计 → 批准实施 → 批准 dry-run → 批准 apply
- 文档所有权: operations layer
