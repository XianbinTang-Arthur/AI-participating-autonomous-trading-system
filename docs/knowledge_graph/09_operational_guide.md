# 09 · 运维指南

> **历史运维快照 / 禁止直接执行**：2026-04-21 版本可能包含手工 Compose、旧端口和旧恢复命令。当前部署/停机以根目录 `DEPLOYMENT.md` 为准。

> **生成于 HEAD=待更新** · 2026-04-21
> **内容**：deploy / 监控 / debug / 诊断工具 / 常见场景处置

---

## TL;DR

- **Deploy**：**永远** `bash scripts/deploy.sh`，绝不手动 `docker compose`
- **监控**：Grafana `localhost:3000` + `scripts/diag/` 工具集
- **日志**：Loki（`localhost:3000` datasource 查）+ 直接 `docker logs aats-<role>`
- **紧急停机**：API `POST /admin/kill_switch` 或 `docker compose stop aats-execution`

---

## Deploy

### 标准部署

```bash
# 已提交代码后
bash scripts/deploy.sh --skip-commit

# 带 commit
bash scripts/deploy.sh --commit "feat: ..."

# 强制重建
bash scripts/deploy.sh --no-cache --skip-commit
```

### `scripts/deploy.sh` 的 7 步流水线

1. 提交或跳过（`--commit` 或 `--skip-commit`）
2. 同步到 WSL2 (`sync_to_wsl2.sh`)
3. 停旧容器
4. 构建镜像
5. 清理旧镜像（保留 retention）
6. 起基础设施
7. 起 4 个业务进程 + rdp-daemon

### 失败恢复

- **Jaeger compaction 耗时**（15~60s）→ 业务进程 health check 失败 → 等 Jaeger healthy 再 retry deploy
- **未提交文件**阻塞 deploy → 决定保留就 commit，不要就 `git stash`
- **WSL2 代码同步出错** → `bash scripts/sync_to_wsl2.sh init/pull`

---

## 监控

### Grafana（`https://localhost:3000`）

登录用 `.env.wsl2` 里的 GRAFANA_ADMIN_PASSWORD。

**重点 dashboard**：
- event_store 增长率
- parallel_fetch_slow 直方图
- PG connection pool 使用
- Kill switch / GuardSignal / Recovery state timeseries

### 诊断工具 `scripts/diag/`

| 脚本 | 用途 | 使用场景 |
|------|------|---------|
| `pg_connection_health.sh` | PG 连接状态分布 | UI 卡 / 怀疑 pool 耗尽 |
| `pg_full_scan_audit.sh` | 找当前 full-table scan active query | 怀疑老慢查询回归 |
| `gateway_slow_queries.sh` | `parallel_fetch_slow` top-N | Dashboard 慢 |
| `recovery_rollback_gap.sh` | idle-in-tx 的 rollback 延迟采样 | 精确测 session.close() 延迟 |
| `panel_latency_histogram.sh` | P50/P95/P99 计算 | 建基线或对比 |
| `event_store_bloat_audit.sh` | event_store 按 type 聚合 size + dedup 潜力 | 空间吃紧时 |
| `table_growth_audit.sh` | 所有 PG 表 size 排序 | 日常巡检 |
| `housekeeping_health.sh` | 归档 / purge 状态 | 检查 retention 生效 |

### 关键指标阈值

| 指标 | 正常 | 警报 |
|------|------|------|
| 4 个 advisory_lock 持有 | == 4 | < 4 表示进程失活 |
| idle_in_transaction > 60s | 应 = 0 (PG safety net) | > 0 表示 safety net 失效 |
| event_store 日增 | 0.5 GB / day | > 1.5 GB 查 dedup |
| parallel_fetch_slow 数 | ~0 per 5 min | > 0 看单次 wall |
| recovery_5m | 0 (100% dedup) | > 0 说明 recovery state 在变 |
| 所有容器 healthy | 16 个（含 2 份进程 status 重复） | < 按 role 数 |

---

## Debug 常见场景

### 场景 1 · UI 显示卡顿

1. `bash scripts/diag/pg_connection_health.sh` — 看 PG 连接是否饱
2. `bash scripts/diag/gateway_slow_queries.sh 5` — 最慢的 5 个
3. `bash scripts/diag/pg_full_scan_audit.sh` — 有没有全表扫
4. 如果都正常：`docker logs aats-gateway --tail 100`

### 场景 2 · 交易所端有仓但系统不知道

1. 触发了 `derivatives_exchange_position_without_local_execution_chain` blocker
2. `docker logs aats-execution | grep recovery_posture` 确认
3. **手动对账**：去 OKX 平台关掉或 SQL 里补 OrderState + FillEvent
4. 下次 reconciliation + recovery 会清掉 blocker

### 场景 3 · 系统不交易（本次 session 的现状）

1. `docker logs aats-decision | grep baseline_regime` — 看 baseline 是不是 flat
2. 查最新 DecisionOutcome payload 里的 `book_expectancy_summary.expected_net_edge_bps`
3. 如果一直为负 → **是设计行为**，策略在避免负期望交易
4. 决策：改策略（lower cost 估算 / increase signal strength）或等市场变化

### 场景 4 · event_store 膨胀

1. `bash scripts/diag/event_store_bloat_audit.sh` — 看哪类 event 占大头
2. `bash scripts/diag/housekeeping_health.sh` — 归档 job 是否在跑
3. 本 session T+5 已修了 `recovery` signal dedup（-70% 增速）

### 场景 5 · 容器意外重启 / OOM

1. `docker ps --filter "name=aats-"` 看 status
2. `docker events --since 10m` 看重启事件
3. `docker logs aats-<role> --tail 200` 看 crash 前最后输出
4. 检查 OOM: `wsl dmesg | grep -i "killed process"` 或容器 memory stats

---

## 紧急停机

### 最快：Kill switch

```bash
# 从 gateway 调 API（需要 operator session）
curl -X POST "https://localhost:8011/admin/kill_switch?reason=manual_halt" \
  -H "Cookie: aats_operator_session_derivatives_live=<session>"
```

### 终极：停整个 execution 进程

```bash
wsl -d Ubuntu -- docker compose \
  -f deploy/wsl2-dev/docker-compose.aats.yml \
  -f deploy/wsl2-dev/docker-compose.aats.derivatives-live.yml \
  stop aats-execution
```

**⚠️ 停 execution 会让 market / decision 继续跑但没出口**。Kill switch 是
更优雅的停法。

---

## 灾难恢复

### 进程 crash 后

- AATS 设计为**可重启幂等**
- advisory_lock 会在旧进程 close 后释放，新进程启动抢
- event_store 是 source of truth，state 从这里重建
- 启动时 `execute_startup_recovery_sequence()` 自动对账 / 恢复

### Redis 失联

- `KillSwitch.bootstrap()` 在 multiprocess 模式下默认 **halt**（fail-safe）
- 等 Redis 恢复后手动 resume 或重启

### Postgres 失联

- 业务进程会 log error 并在下一次 retry 连接
- advisory lock 丢失会让 single-runtime 保护失效 → 有小窗口可能双实例
- 本次 session `23c8e7e` 修了 safety net 副作用

### NATS 失联

- 关键 topic 进 outbox（PG 持久）
- NATS 恢复后 outbox publisher 自动 flush
- observer topic（market / feature）数据暂时丢，不影响核心业务

---

## 关键文件参考

- `scripts/deploy.sh` — 部署入口
- `scripts/sync_to_wsl2.sh` — WSL2 同步（绝对不用 rsync）
- `scripts/diag/` — 诊断工具集
- `aats/bootstrap/config.py` — 启动流程
- `CLAUDE.md` — 硬约束 / 禁令
- `docs/project_positioning.md` — 项目长期目标
- `docs/autonomous_sessions/` — AI 每次迭代的过程日志
