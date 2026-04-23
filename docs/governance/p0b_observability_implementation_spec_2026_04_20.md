# P0-b Observability 实施 Spec (2026-04-20)

> 本文件是**实施 spec**, 不是 governance policy.
> 对应的 governance policy 在 `docs/governance/runtime_trading_mode_semantics.md`.

**状态**: Spec 起草完成, **不含代码**. 实施留给 P0-a 完成后的 follow-up.

---

## 1. 发现的事实 (节省重复 grep)

### 1.1 前端已有的

**文件**: `aats/api/static/modules/views/ai-view.js`

- `effectiveMode(runtime)`: 算出 canonical AI mode (baseline_only / ai_assisted / ai_decision_maker)
- `configuredMode(runtime)`: 配置的原始 mode (可能是 legacy alias)
- **L64, L483**: 当两者不同时, UI 有 "effective ≠ configured" 的警示 branch
- **L922**: 已经展示 `humanState(latestOutcome.decision_authority || "reference_only")` 在 AI view 的某个 row

### 1.2 缺失的 (实施 gap)

| 需求 | 现状 | gap |
|---|---|---|
| 主页面顶栏永久可见模式标 | ai-view 里某行有, **不在全局顶栏** | 顶栏缺 badge |
| 模式红/绿颜色分级 | 只在 ai-view 局部 | 全局缺 |
| 点模式 badge 跳 governance doc | 无 | 新加 |
| Grafana 新 panel "Runtime Trading Mode" | 无 | 新加 |
| Prometheus / Loki metric `aats_ai_operating_mode` 可查询 | 未确认 | 需查 decision_outcome payload 是否透传到 OTel |
| ~~"baseline_only 下有 order 提交" 告警~~ | ~~无~~ | **2026-04-23 已废弃**: 原假设 "baseline_only = 不下单" 与代码实际行为不符, baseline_only 下下单合法 (仅 AI 未介入), 该告警会误报. 见 `runtime_trading_mode_semantics.md §8`. |
| "ai_decision_maker 下 24h 零 order" 告警 | 无 | 新加 alert rule |

---

## 2. 实施 Task 清单

### Task 2.1 — 全局顶栏 badge

**文件**:
- `aats/api/static/dashboard-shell.html`: 顶栏 div 结构
- `aats/api/static/modules/shell-renderer.js`: badge 渲染
- `aats/api/static/app.css`: badge 样式 (baseline_only = 灰/蓝, ai_assisted = 橙, ai_decision_maker = 红)

**行为**:
- 数据源: `/api/v1/system/runtime` 的 `operating_mode` 字段 (已有 endpoint; 若无需新建)
- 显示文本 (2026-04-23 勘误后):
  - `baseline_only`: "模式: baseline_only · reference only · 仅 baseline, 不使用 AI" (灰底蓝字)
  - `ai_assisted`: "模式: ai_assisted · advisory · **AI 咨询实盘中**" (橙底白字)
  - `ai_decision_maker`: "模式: ai_decision_maker · final_decision · **AI 主导实盘中**" (红底白字)
- 点击跳 `/api/docs/governance/runtime_trading_mode_semantics.md` 或等价链接

**验收**:
- 任意页面顶栏都看得到 badge
- 切换 mode 后 badge 30s 内刷新
- 颜色和文案符合表格

### Task 2.2 — Grafana panel "Runtime Trading Mode"

**文件**: `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/aats_operations.json`

**新 panel**:
- Type: Stat (大字报)
- Title: "Runtime Trading Mode (当前授权)"
- Query 1 (如 Prometheus metric 已存在):
  `aats_runtime_ai_operating_mode{instance=~"aats-decision.*"}`
- Query 2 (fallback, Postgres datasource):
  ```sql
  SELECT payload::jsonb->>'ai_operating_mode' AS mode
  FROM public.event_store
  WHERE topic = 'strategy.decision_outcome'
  ORDER BY event_timestamp DESC
  LIMIT 1
  ```
- Value mapping (2026-04-23 勘误后):
  - `baseline_only` → 灰底 "REFERENCE ONLY (仅 baseline)"
  - `ai_assisted` → 橙底 "ADVISORY (AI 咨询)"
  - `ai_decision_maker` → 红底 "FINAL DECISION (AI 主导)"

**Grafana row 位置**: 主 dashboard 最顶部, 与"Deploy HEAD" / "系统 healthy 容器数" 并排

### Task 2.3 — Alerting rules

**文件**: `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml`

**2026-04-23 勘误**: 原 spec 里的 `sev2-runtime-baseline-has-orders` 告警已**废弃**.
原假设 "baseline_only 下有 order 提交 → authority_map 被绕过" 与代码实际行为不符 —
`baseline_only` 仅代表不调用 AI, 执行引擎从不按 `decision_authority` 拦订单, baseline
驱动的下单是合法的. 见 `runtime_trading_mode_semantics.md §8 修订记录`. 仅保留 sev3.

```yaml
- uid: "sev3-runtime-ai-decision-no-orders"
  title: "[SEV-3] ai_decision_maker 模式下 24h 内 0 订单"
  condition: A
  data:
    - refId: A
      model:
        expr: |
          (sum(rate(aats_orders_submitted_total{mode="ai_decision_maker"}[24h])) == 0)
          AND
          (sum(aats_runtime_ai_operating_mode{mode="ai_decision_maker"}) > 0)
  for: 24h
  # 若 AI 决策模式跑了 24h 一单没下, 说明 alpha/cost 被阻断
```

**前置依赖**: 需要 OTel 把 `ai_operating_mode` 暴露为 Prometheus metric. 若现在没有, 补一条 `microstructure_silver_etl_metrics` 同款 metric register.

### Task 2.4 — OTel metric for ai_operating_mode

**文件**: 
- `aats/services/decision_engine/target_position.py` 构造 DecisionOutcome 的地方
- 或者 `aats/bootstrap/metrics.py`

**加 metric**:
```python
from aats.bootstrap.metrics import MetricsRegistry
metrics.gauge("aats_runtime_ai_operating_mode", 1, labels={"mode": canonical_mode})
```

每次产出 DecisionOutcome 时更新. Prometheus scrape 后可查询.

**注意**: 这依赖 P1-D 遗留的 Prometheus scrape connection-refused 修复先完成 (见 `docs/design/p1d_phase1a_deferred_items_2026_04_20.md` #4). 若那个未修, 本 metric 也不会被 Prometheus 采. **不硬阻塞** — 先写代码, Prometheus 修好后自动生效.

---

## 3. 实施 Timing

- **Task 2.1 (顶栏 badge)**: P0-a merge 后立即做, 0.5-1 人天
- **Task 2.2 (Grafana panel)**: P0-a merge 后做, 0.5 人天
- **Task 2.3 (Alert rules)**: 依赖 Task 2.4 先完成, 0.25 人天
- **Task 2.4 (OTel metric)**: 依赖 Prometheus scrape 修复, 0.5 人天

**总**: 1.5-2 人天, 可在 P0-a 之后作为第二批 spawn 或人工实施.

## 4. 不在本 Spec 范围的

- 改 `authority_map` 本身 (永远冻结, 见 `frozen_parameters.md`)
- 允许 operator UI 直接切 `ai_operating_mode` (必须走 `.env.*.live` + deploy 审计)
- 修 Prometheus scrape connection-refused (独立 P1 任务)

---

## 5. 签署

- 起草: Claude Opus 4.7 · 2026-04-20
- 触发: P0-b governance doc 的 Observability 要求部分需要具体实施 spec
- 状态: ✅ **全部 4 Task 已实施并 deploy** (2026-04-20):
  - Task 2.1 顶栏 badge — commit 91b860f (文案 2026-04-23 勘误调整)
  - Task 2.2 Grafana panel — commit 7276674 (value mapping 2026-04-23 勘误调整)
  - Task 2.3 Alerting rules — commit 7276674 + sev3 PromQL 修 (d14bd60); **2026-04-23: sev2-runtime-baseline-has-orders 废弃删除** (语义错误)
  - Task 2.4 OTel labeled metric — commit ecc6001 + order submission label (4b2ac2d) — label 保留, sev3 仍需
  - P1 遗留 Prometheus scrape 修复 — commit 3c90c64 (6/6 targets UP)
  - dead-man alert — commit 7c5c5bc (sev1-metrics-scrape-dead-man)
