# P1-D Phase 1A Pre-Deploy Checklist (Stage 4 runbook)

> **历史首次上线清单（2026-08-22 核对）**：只适用于 2026-04 P1-D Phase 1A 首次部署，不是当前通用 pre-deploy checklist。commit、容器、scheduler 和 gate 状态可能已变化；当前操作以根目录 `DEPLOYMENT.md` 与 `operator_checklist.md` 为准。

> **目的**: 在用户按下 `bash scripts/deploy.sh --profile derivatives-live --skip-commit` 之前,机械化地跑完这份清单,排除 6 类 known risk (OKX 连接上限 / scheduler 识别 / migration 就绪 / monitoring 就绪 / DB 连通 / 滚回 SOP)。
>
> **适用**: P1-D Phase 1A (Stage 1-4 全量),本文档仅覆盖首次上线的预检;deploy 之后的 48h 监控见 `p1d_phase1a_48h_stability_runbook.md`。
>
> **作者**: P1-D Phase 1A Stage 4 实施 agent · 2026-04-20
> **前置**: `docs/design/p1d_phase1a_implementation_design_2026_04_20.md` §10 + §11 + 附录 E
> **前置**: `docs/review/p1d_phase1a_stage3_completion_2026_04_20.md` §8.3 (scheduler 遗留)

---

## 0. 启动条件

- [ ] Worktree 已 merge 到 `main` (Stage 1-4 4 次 merge PR)
- [ ] `main` 分支的最近 HEAD 含:
  - `5c81aa4` (Stage 3 merge)
  - Stage 4 的 metrics wiring + dashboard + alerts + E2E + 3 份文档 + 完工报告 commit
- [ ] 当前 live deploy 健康: `curl -s http://localhost:8011/healthz | head` 返回 200
- [ ] `git status -uno` 无 uncommitted 改动,`git diff origin/main..HEAD` 与预期一致

**如果上面任一项不满足**: 停下来,先修复,不要继续。

---

## 1. OKX 3 WS/IP 限制 — 最重要的预检 (§10.2)

### 1.1 当前已有公共连接盘点

| 来源 | 连接数 | 频道 |
|------|--------|------|
| `aats-market` | 2 | public + business |
| `aats-liquidations-daemon` | 1 | liquidation-orders |
| **Stage 2 新增 `aats-microstructure-collector`** | **1** | trades-all + bbo-tbt + books5 + open-interest + funding-rate + mark-price |
| **合计(deploy 后)** | **4** | — |

OKX 官方文档限制: **每 IP 3 个 public WebSocket 并发**。WSL2 Docker 容器 NAT 到 WSL2 单 IP,从 OKX 视角看是同一出口 IP → **deploy 后理论上超限 1 条连接**。

### 1.2 决策路径 (三选一)

#### 方案 A — 合并 microstructure + liquidations daemon (推荐, 但附录 E #8 决策保留两个独立)

✅ **优点**: 合并后 public 连接降到 3,不超限
❌ **缺点**: 违反附录 E #8 决策 ("不合并独立 daemon")

**实施草案(不在 Stage 4 做, 仅文档化)**:
1. 新建 `scripts/raw_ingest_ws_daemon.py` 替代两个旧 daemon
2. 内部用 `MicrostructureCollector` 的 WS client + 追加 `liquidation-orders` subscription
3. 在一个 `OKXWebSocketConsumerBase` 连接里订阅 7 个频道 (6 existing microstructure + 1 liquidation-orders)
4. 修改 `docker-compose.aats.derivatives-live.yml`: 删 `aats-liquidations-daemon`,把 `aats-microstructure-collector` 改命令
5. 在 daemon 里同时写两套 bronze/staging 表

**工期**: 1 人天。Phase 1A 完工后若真发 60004 错误再做。

#### 方案 B — VPN / 代理改出口 IP

把 WSL2 容器网络通过另外一个 IP 出口 (例如 VPN 或代理),绕过 "单 IP 3 连接" 的限制。

❌ **不推荐**:
- 增加一跳额外延迟 (WS 对延迟敏感,会影响 bbo 1Hz 采样质量)
- 增加故障点 (VPN 断则所有 4 个容器的 WS 都断)
- 涉及凭证管理 (VPN 账号/证书)

#### 方案 C — 联系 OKX 申请 VIP 高频额度

如果用户是 VIP 用户,可申请提升 per-IP 连接数上限 (据 OKX 文档 VIP5+ 可到 10 连接)。

❌ **不推荐**: 需要提前 1 周申请审批,Phase 1A 不等。

### 1.3 首选实际操作(Phase 1A 推荐)

**推荐先 deploy 方案 0 ("观察上线再决策")**,不做 A/B/C,原因:

1. OKX "每 IP 3 连接" 可能是 public **频道订阅** 数而非物理连接数的软限制 (官方文档含糊)
2. 实测数据不足 (Stage 2 未实测,附录 E #2 也承认此风险已标记)
3. 若真超限会触发 error code **60004 Too many connections**,方案 A 可在 1 小时内回滚 + 合并 daemon,不是不可逆的灾难

### 1.4 预检命令

```bash
# 部署前: 检查当前 public 连接数 (liquidations-daemon + aats-market)
docker exec aats-liquidations-daemon python -c "print('liquidations running')" 2>&1 | head
docker exec aats-market curl -s http://localhost:9464/metrics | grep -E 'okx_ws_messages|okx_ws_connect' | head -5

# 部署后 10 min: 观察 microstructure-collector 的 last_error 字段
docker logs aats-microstructure-collector 2>&1 | tail -50 | grep -E "60004|too many|subscription_error"
```

如果命中 `60004` 错误,立即 `docker stop aats-microstructure-collector` + 启动方案 A (合并 daemon)。

---

## 2. Workflow scheduler 识别 `frequency=custom + interval_minutes`

### 2.1 识别现状 — **不识别**

- 文件: `aats/data_platform/operations/workflow_scheduler.py` line 137
- 代码:
  ```python
  frequency = str(schedule.get("frequency") or "").strip().lower()
  if frequency not in {"daily", "weekly", "hourly"}:
      return None
  ```

Stage 3 的 `configs/rdp_workflows/microstructure_silver_15m.json` 里 `"frequency": "custom"` **会被 ignore**,`microstructure_silver_15m` 任务**不会被自动入队**,意味着 deploy 后:

- `aats-rdp-daemon` 启动正常
- 但 Silver 表**始终没有新 row**
- Grafana 大盘 Panel 4 "Silver 15m bars produced (24h)" 会始终为 0
- §11 Gate 1 "连续 48h 无间断采集" **将失败**

### 2.2 Stage 4 不修改 workflow_scheduler.py (边界外)

Stage 4 严格 scope:**不改 `aats/services/**`** → `aats/data_platform/operations/workflow_scheduler.py` 虽不在 services 下,但这是 scheduler **core logic**, Stage 3 完工报告 §8.3 明文说 "维护性改动,交 Stage 4 评估,但不属于 Stage 4 新增 scope"。

我的判断: Stage 4 不直接改,给 2 个 fallback 让用户选。

### 2.3 Fallback 1 — 5 行 patch (推荐)

在 `workflow_scheduler.py` 加 `custom` frequency 分支。Diff 示意 (**用户 deploy 前自行 apply + 提交**):

```python
# Line 137 附近
def get_workflow_schedule(config: dict[str, Any]) -> dict[str, Any] | None:
    schedule = config.get("schedule")
    if not isinstance(schedule, dict):
        return None
    if schedule.get("enabled", True) is False:
        return None
    frequency = str(schedule.get("frequency") or "").strip().lower()
-    if frequency not in {"daily", "weekly", "hourly"}:
+    if frequency not in {"daily", "weekly", "hourly", "custom"}:
        return None
    return schedule


def _latest_slot_for_schedule(
    schedule: dict[str, Any],
    *, now: datetime,
) -> datetime | None:
    frequency = str(schedule.get("frequency") or "").strip().lower()
    minute = int(schedule.get("minute_utc", 0))
    ...
+    if frequency == "custom":
+        # custom + interval_minutes=N: N min 对齐的最近一个 slot
+        interval = max(int(schedule.get("interval_minutes", 15)), 1)
+        ts = now.replace(second=0, microsecond=0)
+        # 对齐到 interval 格点 (N=15 → 12:00, 12:15, 12:30, 12:45)
+        minute_aligned = (ts.minute // interval) * interval
+        return ts.replace(minute=minute_aligned)

    interval_hours = max(int(schedule.get("interval_hours", 1)), 1)
    ...
```

### 2.4 Fallback 2 — 外挂 cron (不改代码)

在 `deploy/wsl2-dev/docker-compose.aats.yml` 的 `aats-rdp-daemon` service 里 (or 加一个 `aats-cron` sidecar),加 cron 每 15 min 跑:

```yaml
# 伪代码: compose 不原生支持 cron,需用 ofelia/supercronic/alpine-cron sidecar
# 或直接让 aats-rdp-daemon 进程用 asyncio 自带 timer
```

❌ 不推荐:增加 sidecar 维护成本,违背 Phase 1A "零额外容器" 原则。

### 2.5 决策

**建议方案 1**: Phase 1A 首次 deploy **前**,让用户 apply Fallback 1 的 5 行 patch (`workflow_scheduler.py`) 并提交。否则 Silver ETL 不会自动跑 → Phase 1A 核心价值落空。

**验证命令 (deploy 后 30 min)**:
```bash
# 看 rdp-daemon 是否把 microstructure_silver_15m 任务入了 queue
docker exec aats-postgres psql -U admin -d aats_live_derivatives \
  -c "SELECT workflow, status, created_at FROM governance.rdp_task_queue
      WHERE workflow = 'microstructure_silver_15m'
      ORDER BY created_at DESC LIMIT 5;"

# 看 Silver 表有没有行进来
docker exec aats-postgres psql -U admin -d aats_live_derivatives \
  -c "SELECT COUNT(*), MAX(ts) FROM silver.market_orderbook_metrics_15m;"
```

如果 30 min 后队列为空 + Silver 表为空 → Fallback 1 未正确 apply,重检代码 diff。

---

## 3. Migration 就绪 (batch_b_05 + batch_b_06)

Deploy.sh 会触发 `rdp_init_db` 路径,该路径**不走** `run_batch_b_migrations`,而是走 ORM metadata `create_all`。但 Stage 1-3 的 ORM class 已注册,首次 deploy 会**一并**建 8 张 microstructure 表。

### 3.1 预检命令

```bash
# 首次 deploy 前确认旧 DB 无残留 (在 live DB 上不会有,但 staging / 其他 DB 可能残留)
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT schemaname, tablename FROM pg_tables
  WHERE (schemaname = 'bronze' AND tablename LIKE 'market_%')
     OR (schemaname = 'silver' AND tablename LIKE 'market_%15m')
     OR (schemaname = 'staging' AND tablename = 'market_oi_funding_ticks')
  ORDER BY 1, 2;
"
# 期望: 零行 (deploy 前没建过)
```

### 3.2 Deploy 后的验证

```bash
# 跑同一条 SQL,期望 9 行
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT schemaname, tablename FROM pg_tables
  WHERE (schemaname = 'bronze' AND tablename LIKE 'market_%')
     OR (schemaname = 'silver' AND tablename LIKE 'market_%15m')
     OR (schemaname = 'staging' AND tablename = 'market_oi_funding_ticks')
  ORDER BY 1, 2;
"
# 期望 9 行:
#   bronze.market_trades
#   bronze.market_orderbook_bbo
#   bronze.market_orderbook_books5
#   staging.market_oi_funding_ticks
#   silver.market_orderbook_metrics_15m
#   silver.market_trade_flow_15m
#   silver.market_oi_funding_metrics_15m
#   silver.market_volume_profile_15m
#   silver.market_liquidation_metrics_15m
```

---

## 4. Monitoring 就绪 (Prometheus / Grafana / Alert rules)

### 4.1 Prometheus target

```bash
# Prometheus 能看到 aats-microstructure-collector:9465
curl -s http://localhost:9090/api/v1/targets | \
  grep -E '"job":"aats-microstructure"|9465' | head -5

# 应看到 job="aats-microstructure" / target="aats-microstructure-collector:9465"
# health 期望 "up" (前 5 min 可能 "down" 因 OTel 启动要几秒)
```

### 4.2 Grafana dashboard 可访问

- 打开 `http://localhost:3000/d/p1d-microstructure`
- 应看到 4 个 row: WS Health / Bronze Ingest Throughput / Silver ETL / Storage Growth
- 部分 panel 首 15 min 会是空 (数据还没落进来);可接受

### 4.3 Alert rules loaded

```bash
# Grafana 加载了 11 条 rules (5 existing + 6 new)
curl -s http://localhost:3000/api/ruler/grafana/api/v1/rules | \
  python3 -c "import json, sys; d = json.load(sys.stdin); print(f'loaded rules: {sum(len(r[\"rules\"]) for g in d.values() for r in g)}')"
# 期望: 11 (6 Stage 4 new + 5 Stage 9 existing)
```

### 4.4 接警链路 (Stage 9 决策: log-only, 不接 SMTP)

Stage 9 dryrun 决定:告警先 log-only,不发邮件/Telegram。Stage 4 沿用。如需在 Phase 1B / Phase 2A 接 SMTP, 修 `deploy/wsl2-dev/grafana/provisioning/alerting/contactpoints.yml`。

---

## 5. DB 连通性

```bash
# microstructure-collector 容器能连 postgres
docker exec aats-microstructure-collector python -c "
from aats.data_platform.db import get_session
with get_session() as s:
    from sqlalchemy import text
    n = s.execute(text('SELECT 1')).scalar()
    print('db_ok n=' + str(n))
"

# rdp-daemon 能看到 microstructure_silver_15m workflow
docker exec aats-rdp-daemon python -c "
from aats.data_platform.governance.rdp_task_db import VALID_WORKFLOWS
assert 'microstructure_silver_15m' in VALID_WORKFLOWS, 'VALID_WORKFLOWS missing'
print('workflow registered')
"
```

---

## 6. Deploy 命令 + 回滚 SOP

### 6.1 首次 deploy 命令

```bash
# 标准
bash scripts/deploy.sh --profile derivatives-live --skip-commit

# 如需 rebuild container image (Stage 2 新 service):
bash scripts/deploy.sh --profile derivatives-live --no-cache --skip-commit
```

### 6.2 Deploy 完成后的 10-min 验收

```bash
# 所有容器 healthy
docker ps --format "{{.Names}}  {{.Status}}" | grep -E "aats-(microstructure|liquidations|rdp|market|decision|execution|gateway)"
# 期望全部 "healthy" (aats-microstructure-collector 可能 starting 前 30s)

# Bronze 表 5 min 有数据
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c \
  "SELECT (SELECT COUNT(*) FROM bronze.market_trades) AS trades,
          (SELECT COUNT(*) FROM bronze.market_orderbook_bbo) AS bbo,
          (SELECT COUNT(*) FROM bronze.market_orderbook_books5) AS books5,
          (SELECT COUNT(*) FROM staging.market_oi_funding_ticks) AS oif;"
# 期望: 5 min 内 trades > 1000, bbo > 100, books5 > 200, oif > 30
```

### 6.3 Rollback SOP

如果上线后 10 min 发现严重问题 (60004 错误 / OOM / PG 高 CPU),按这个顺序回滚:

**Level 1** - 仅停 microstructure collector (最小化影响):
```bash
docker stop aats-microstructure-collector
# 其他 4 个 AATS 进程 + liquidations-daemon 继续正常跑
# 交易主链路不受影响
```

**Level 2** - 回滚 Silver migration (batch_b_06):
```bash
# DB 中保留 batch_b_05 的 Bronze/staging (可重建 Silver), 只 drop Silver 5 张表
docker exec aats-rdp-daemon python -m aats.data_platform.migrations._batch_b \
  rollback --stages batch_b_06_silver_microstructure
```

**Level 3** - 完全回滚 (drop 全部 9 张表 + 删 service):
```bash
# 1. Drop migration
docker exec aats-rdp-daemon python -m aats.data_platform.migrations._batch_b \
  rollback --stages batch_b_06_silver_microstructure batch_b_05_microstructure

# 2. Git revert Stage 4 merge + Stage 3 merge + Stage 2 merge + Stage 1 merge
# 然后重新 deploy
```

**不建议 Level 3** 除非 Phase 1A 被完全放弃;Level 1 足以让主交易链路恢复。

---

## 7. Pre-deploy CLI 一口气跑完

把上面命令拼在一起 (`less`/`bash -x` 审查,不要直接执行):

```bash
#!/bin/bash
set -eu

echo "=== Phase 1A Pre-deploy Checklist ==="

# § 0. Git 状态
git log --oneline main -5
git status -uno

# § 1. OKX 连接数 (deploy 前)
docker ps --filter name=aats-liquidations-daemon --format "{{.Names}} {{.Status}}"
docker exec aats-market curl -s http://localhost:9464/metrics 2>&1 | grep okx_ws_messages_total | head -3

# § 3. DB 残留
docker exec aats-postgres psql -U admin -d aats_live_derivatives -c "
  SELECT schemaname || '.' || tablename AS table FROM pg_tables
  WHERE (schemaname = 'bronze' AND tablename LIKE 'market_%')
     OR (schemaname = 'silver' AND tablename LIKE 'market_%15m')
     OR (schemaname = 'staging' AND tablename = 'market_oi_funding_ticks')
  ORDER BY 1;
"

echo "=== checklist done; review above then run deploy.sh ==="
```

---

## 8. 签署

- **作者**: P1-D Phase 1A Stage 4 agent · 2026-04-20
- **前置**: 设计文档 §10 + §11, Stage 3 完工报告 §8.3
- **适用**: Phase 1A 首次 deploy (derivatives-live profile)
- **下一步**: Phase 1A 48h 稳定性 runbook (`p1d_phase1a_48h_stability_runbook.md`)
