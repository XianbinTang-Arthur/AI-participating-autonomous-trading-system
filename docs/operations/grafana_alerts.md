# Grafana Alert Rules 推荐清单

> **历史建议清单（2026-08-22 核对）**：当前仓库已经通过 `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml`、`contactpoints.yml` 和 `policies.yml` 配置告警。本文保存 2026-04-22 的设计建议，阈值和 LF 状态使用前必须与当前 provisioning/metrics 核对。

> **生成于**：2026-04-22 autonomous session
> **背景**：LF-001 / LF-020 最初被标 "heartbeat 看不到 GIL 卡死 / decision idleness 无告警"，
> 后发现心跳和 decision_cycles metric 都已就位，缺的只是 **Grafana alert rule 配置**。
> **如何使用**：在 Grafana dashboard 的 Alerting → Alert rules 里按以下配置添加

---

## 高优先级告警（建议立刻配置）

### 1. Decision engine idleness（LF-020）

**Query** (Prometheus):
```promql
rate(decision_cycles_total{process_role="decision"}[5m])
```

**Condition**: `WHEN avg() OF query() IS BELOW 0.01`  
即：5 分钟平均速率低于 0.01/s（= 每分钟不到 0.6 次决策）。

**For**: `10m`（持续 10 分钟才告警，避免偶发抖动）

**Severity**: critical

**说明**：
- 正常值 ~2.5 次/分钟（0.042/s），所以 0.01 是"几乎停了"的阈值
- "正常不交易"（信号弱）仍然会跑决策周期得出 hold，所以这条 alert 能区分
  "决策引擎正常运转但信号是 hold" vs "决策引擎挂了"

### 2. PG advisory lock 丢失（单进程保护失效）

**Query**:
```promql
count(pg_locks{mode="advisory"})
```

**Condition**: `WHEN last() IS NOT EQUAL TO 4`

**For**: `2m`

**Severity**: high

**说明**：
- 4 个业务进程（gateway/market/decision/execution）应各持 1 把 session-level
  advisory lock（单实例保护）
- 数量 < 4：进程挂了或 PG 60s safety net 又误杀了
- 数量 > 4：有人跑了第二套（双实例 = 双倍风险）

### 3. 容器非 healthy

**Query** (通过 `cadvisor` 或 `docker_state` exporter):
```promql
count(container_state{state!="healthy", name=~"aats-.*"})
```

**Condition**: `WHEN last() IS ABOVE 0`

**For**: `1m`

**Severity**: critical

---

## 中优先级告警

### 4. Event store 增速异常

**Query**:
```promql
rate(pg_stat_user_tables_n_tup_ins{relname="event_store"}[10m])
```

**Condition**: `WHEN avg() IS ABOVE 100`（正常 ~30 行/秒，> 100 说明有新 bloat 源）

**For**: `15m`

**Severity**: medium

### 5. OKX private WS keepalive 死

**Query**:
```promql
count_over_time({job="aats", logger="aats.okx_private_ws"} |= "keepalive_task died" [5m])
```

**Condition**: `WHEN count() IS ABOVE 0`

**Severity**: high（LF-20260421-A1 watchdog 触发 = 有连接问题）

### 6. Kill switch halted（人工或自动）

**Query**:
```promql
count_over_time({job="aats"} |= "kill_switch_halted" [10m])
```

**Condition**: `WHEN count() IS ABOVE 0`

**Severity**: critical（halt = 系统停交易，operator 必须知道）

### 7. decision_cycle_timeout（LF-003 监控）

**Query**:
```promql
count_over_time({job="aats", logger="aats.decision_engine"} |= "decision_cycle_timeout" [15m])
```

**Condition**: `WHEN count() IS ABOVE 0`

**Severity**: high（run_cycle 超时表示 NATS 背压或 PG 卡死 — 定位根因）

---

## 低优先级（运维参考）

### 8. Guard signal dedup rate

**Query** (post-dedup recovery signal count should be near zero):
```promql
rate({job="aats"} |~ "GuardSignalUpdate.*recovery" [5m])
```

**Condition**: `WHEN avg() IS ABOVE 0.1`（本场 dedup 目标 < 0.1/s）

**Severity**: low

### 9. Parallel fetch slow（前端性能基线）

**Query**:
```promql
count_over_time({job="aats"} |= "parallel_fetch_slow" [5m])
```

**Condition**: `WHEN count() IS ABOVE 0` for 5m

**Severity**: medium

---

## 配置方法

Grafana UI 路径：
1. Alerting → Alert rules → New alert rule
2. 按本文档表格填写 Query / Condition / For / Severity
3. 建议先用 "Pending" 状态观察一周再开正式通知

当前 Grafana Provisioning（IaC）路径：
- 规则：`deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml`
- Contact points：`deploy/wsl2-dev/grafana/provisioning/alerting/contactpoints.yml`
- Policies：`deploy/wsl2-dev/grafana/provisioning/alerting/policies.yml`
- 当前 Stage 9 为 UI-only：全天 mute timing 阻止 notifier 外发；告警状态仍在 UI 内保留。启用真实通知前必须同时移除该 mute timing，并配置、测试经批准的真实 contact point。
- Prometheus 主进程/collector 目标由 Compose profile 挂载的 `deploy/wsl2-dev/prometheus/targets/*.yml` 决定；未部署 collector 的模拟盘不会生成 Microstructure/telemetry 伪告警。
- 修改后通过标准 `scripts/deploy.sh` 重建/验证，不手工重启单个 Compose 服务

---

## 和 LF 清单对应

- LF-001: 本文档的 alert #1 (decision_engine idleness)
- LF-020: 本文档的 alert #1（同一个，合并）
- LF-003 fix 的监控：alert #7 (decision_cycle_timeout)
- LF-004 fix 的监控：alert #6 (kill switch halted)

---

## 下一步

本文档**只是规则规格**，不是实际 Grafana 配置变更。运维人员或 AI agent
可以：
1. 逐条测试（先 Pending 状态看阈值是否合理）
2. Pending → Firing 前先跑 2 周观察误报率
3. 关键告警（critical）接 PagerDuty / Slack webhook 才有意义

**本 session 不自动应用** —— Grafana 配置改动算 ops 层，需要 operator 参与。
