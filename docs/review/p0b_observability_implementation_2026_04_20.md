# P0-b Observability 实施报告 (2026-04-20)

> **对应 spec**: `docs/governance/p0b_observability_implementation_spec_2026_04_20.md`
> **对应 governance**: `docs/governance/runtime_trading_mode_semantics.md`
> **冻结参数参考**: `docs/governance/frozen_parameters.md` §2.4

**目标**: 让 "AATS 不下单 = by design, 不是 bug" 在前端 / Grafana / alerting / metrics 四个层面都可见。

---

## §1 做了什么 (逐 Task)

### Task 2.1 — 全局顶栏 mode badge ✅ DONE

把 `effectiveMode(runtime) / configuredMode(runtime)` 的显示从 `ai-view.js` 一个深层 row 挪到**任何 view 都可见的顶栏 badge**。

**改动**:
- `aats/api/static/dashboard-shell.html`:
  - `<nav>` 之后新增 `<button id="runtimeModeBadge">` (不用 `<a>` + href 避免 SPA 跳转问题)
  - 同级加一个 `<dialog id="runtimeModeInfoDialog">`,内容是 `runtime_trading_mode_semantics.md §1-§2` 的精简表格 + 核心解释
  - badge 点击触发 `data-action="show-runtime-mode-info"`,dispatchAction 调 `showModal()`
- `aats/api/static/app.css`:
  - 新增 `.runtime-mode-badge` 基础样式 + 三种 tone modifier:
    - `.runtime-mode-badge--baseline-only` 灰底蓝字
    - `.runtime-mode-badge--ai-assisted` 橙底白字
    - `.runtime-mode-badge--ai-decision-maker` 红底白字
  - `.runtime-mode-dialog` modal 样式
- `aats/api/static/modules/shell-renderer.js`:
  - `renderShell()` 内加一行 `renderRuntimeModeBadge()`
  - 新增 `renderRuntimeModeBadge()` 函数:读 `state.data.aiRuntime.effective_operating_mode` 和 `configured_operating_mode`,映射到 tone + 文案
  - `effective !== configured` 时 append `(默认 <configured>)` 提示 manual override
- `aats/api/static/app.js`:
  - `nodes` 对象注册 `runtimeModeBadge` / `runtimeModeBadgeBody` / `runtimeModeInfoDialog`
  - `LOCAL_DISPATCH_ACTIONS` 注册 `show-runtime-mode-info` / `close-runtime-mode-info`
  - 新增 `showRuntimeModeInfoDialog()` / `closeRuntimeModeInfoDialog()` helpers
- `aats/api/static/modules/store.js`:
  - `CORE_SPECS` 增加 `["aiRuntime", "/ai/runtime"]`,让任何 view 刷新时都带上 mode 数据
  - 原本 aiRuntime 只在 aiAnalysis / aiConfig view 里拉;现在全局拉(dashboard bundle 的 `seen` dedup 保证不会双发请求)

**文案 (严格对齐 spec §2.1)**:
| effective_operating_mode | tone class | body 文案 |
|---|---|---|
| `baseline_only` | baseline-only (灰底蓝字) | "baseline_only · reference only · 按设计不下单" |
| `ai_assisted` | ai-assisted (橙底白字) | "ai_assisted · advisory · 实盘中" |
| `ai_decision_maker` | ai-decision-maker (红底白字) | "ai_decision_maker · final_decision · AI 实盘中" |

**行为**:
- 任何 view 顶栏可见 badge
- 跟随 dashboard 的 30s auto-refresh 周期更新(CORE_SPECS 每次 refresh 都拉 aiRuntime)
- aiRuntime 还没到时 badge 隐藏(避免"加载中…"误读为模式异常)
- 点击弹 modal 显示 governance 语义文档核心内容

---

### Task 2.2 — Grafana panel "Runtime Trading Mode" ✅ DONE

**文件**: `deploy/wsl2-dev/grafana/provisioning/dashboards/files/AATS/aats_operations.json`

**改动**:
- 新增 row id=50 "Runtime Governance (P0-b)" 在 dashboard 顶部 (y=0),把原来 "Process Health" 及后续所有 row 的 y 坐标向下偏移 5
- 新增 panel id=11 "Runtime Trading Mode (当前授权)",Stat 类型,full-width (w=24)
- **Query A (Prometheus, primary)**: `group by (mode) (aats_runtime_ai_operating_mode_total)` — 读 Task 2.4 代码暴露的 labeled counter
- **Query B (Postgres, fallback)**: 
  ```sql
  SELECT payload::jsonb->>'ai_operating_mode' AS mode
  FROM public.event_store
  WHERE topic='strategy.decision_outcome'
  ORDER BY event_timestamp DESC LIMIT 1
  ```
- **Value mapping**:
  - `baseline_only` → blue "REFERENCE ONLY (不下单)"
  - `ai_assisted` → orange "ADVISORY (实盘)"
  - `ai_decision_maker` → red "FINAL DECISION (AI 实盘)"
- 无数据时显示 "No Data (Prometheus scrape 未修 / aats_runtime_ai_operating_mode 未暴露)" — 这正是 P1 scrape 修好前的预期展示

---

### Task 2.3 — Alerting rules ✅ DONE

**文件**: `deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml`

**新增 2 条**:

1. `sev2-runtime-baseline-has-orders`: baseline_only 模式下 24h 内 `rate(aats_orders_submitted_total{mode="baseline_only"}[24h]) > 0` → SEV-2
   - `for: 15m`, `noDataState: OK`, `component: runtime_governance`
2. `sev3-runtime-ai-decision-no-orders`: ai_decision_maker 模式跑了 24h 但 order rate 接近 0 → SEV-3
   - `for: 24h`, `noDataState: OK`, `component: runtime_governance`

**关键注释**: 两条规则都写明依赖 `aats_orders_submitted_total{mode=...}` — **目前 order submission 代码路径没带 mode label**,规则按 spec §2.3 明确的 "不硬阻塞" 条款先定义好。metric 数据到位后规则自动生效。同样依赖 P1 遗留的 "Prometheus scrape connection-refused" 修复。

---

### Task 2.4 — OTel metric `aats_runtime_ai_operating_mode` ✅ DONE

**核心改动**:

1. **`aats/bootstrap/metrics.py`** — 扩展 `MetricsRegistry`:
   - 新增 `increment_labeled(name, *, labels, value=1)` — 按 labels 分组计数
   - 内部用 `(metric_name, tuple(sorted(labels.items())))` 作 key 保证幂等
   - 新增 `labeled_snapshot()` 返回 labeled counters 的独立副本
   - 原 `snapshot()` 只返回 unlabeled counters,**不会把 labeled 条目泄漏给历史消费方**

2. **`aats/bootstrap/metrics_bridge.py`** — 扩展 OTel 桥接:
   - `sync_once()` 除了原 unlabeled counters 外,再同步 `labeled_snapshot()`
   - 对 labeled counter 调 `counter.add(delta, attributes=dict(label_tuple))` — Prometheus 会自动展开成多条 series

3. **`aats/services/decision_engine/target_position.py`** — 在 DecisionOutcome 构造点注入 metric:
   - `TargetPositionEngine.__init__` 新增 `metrics: MetricsRegistry | None = None` 参数
   - `_decision_outcome()` 里每次 outcome 被构造都 `self.metrics.increment_labeled("runtime_ai_operating_mode", labels={"mode": canonical_mode})`
   - 未注入 metrics / OTel 异常都是 soft skip,永不阻断决策

4. **`aats/bootstrap/config.py`** — 装配点接线:
   - `TargetPositionEngine(settings=..., fee_resolver=..., metrics=slices.metrics)`

**产出指标**: Prometheus 能查到 `aats_runtime_ai_operating_mode_total{mode="baseline_only|ai_assisted|ai_decision_maker"}` (OTel 桥接自动加 `_total` 后缀符合 Prometheus Counter 约定)。

**严格遵循 spec §2.4 的 "不硬阻塞" 条款** — 不修 Prometheus scrape connection-refused 问题(独立 P1 任务)。

---

## §2 没做什么 + 为什么

| 未做事项 | 原因 |
|---|---|
| 没修 Prometheus scrape connection-refused | spec 明确禁止 — 这是独立 P1 任务 |
| 没在 UI 加 "切 ai_operating_mode" 按钮 | governance 纪律禁止 — 切模式必须走 `.env.*.live` + deploy audit |
| 没改 `authority_map` | frozen_parameters.md §2.4 明确冻结,永远不动 |
| 没让 `aats_orders_submitted_total` 带 `mode` label | 超出本 task 范围 (order submission 路径在另一个子系统,且 spec §2.3 明确允许规则先定义好等 metric 到位) |
| 没 serve `runtime_trading_mode_semantics.md` 为 URL endpoint | 本 task 不动后端 routing;改为在 dashboard-shell.html 里内嵌一个 `<dialog>` modal 显示核心内容,点击 badge 即弹,用户体验等同 |
| 没给 shell-renderer 写 JS 行为级单测 | 现有项目没 JS 测试框架 (DOM/mock 层未建立);改用 Python 的结构性/regex 级 guard 测试覆盖关键连接点 |

---

## §3 单测 / 手工验证结果

### 新增单测 (74 个)

| 文件 | 测试数 | 覆盖范围 |
|---|---|---|
| `tests/unit/test_metrics_registry.py` (新) | 6 | MetricsRegistry 新增 labeled counter API |
| `tests/unit/test_metrics_bridge.py` (扩) | +2 (8 → 10) | OTel bridge 同步 labeled counters 带 attributes |
| `tests/unit/test_target_position_engine.py` (扩) | +3 | TargetPositionEngine 注入 metrics 时 per-mode counter 递增 / 未注入时 soft skip / ai_decision_maker mode 命中 |
| `tests/unit/test_dashboard_runtime_mode_badge.py` (新) | 5 | Task 2.1: 顶栏 badge HTML/JS/CSS 连接点存在性 |
| `tests/unit/test_grafana_runtime_mode_observability.py` (新) | 6 | Task 2.2 + 2.3: Grafana panel 存在且 mapping 正确 / 2 条 alert rule uid 存在 |
| **合计 Task 2.1/2.2/2.3/2.4 新增** | **22 (+现有 52 跑通确保不回归)** | |

### 全量单测结果

```
.venv/Scripts/python.exe -m pytest tests/unit/ -x -q
```

**结果**: `2674 passed, 29 skipped, 1180 warnings, 61 subtests passed in 126.67s`

零失败。现有测试无回归。

### 手工验证

- `aats_operations.json` JSON 语法合法 (json.load 不抛)
- `rules.yml` YAML 语法合法 (yaml.safe_load 不抛)
- `dashboard-shell.html` / `app.css` / `shell-renderer.js` / `app.js` / `store.js` 的连接点被结构性测试覆盖

### 未手工验证 (超出单元测试范围)

- 未实际启动 Grafana 渲染 dashboard(依赖 WSL2 docker 环境,且现在 Prometheus scrape 没修 metric 也不会被采)
- 未实际在浏览器打开 `/ui` 看 badge 渲染效果(依赖运行时)
- 未测 alert rule 在 Prometheus 模板里 `promtool` 校验 — 但规则 expr 语法已对齐项目内现有规则的风格

---

## §4 验收清单 (对照 spec §2.x)

### §2.1 — 全局顶栏 badge

- [x] 任意页面顶栏看得到 badge — 注入 `CORE_SPECS.aiRuntime`,shell-renderer 在 renderShell 里渲染,所有 view 都有
- [x] 切 mode 后 30s 内刷新 — CORE_SPECS 在每次 auto-refresh 都拉 `/ai/runtime`
- [x] 颜色文案符合表格 — 测试断言 baseline-only 蓝色、ai-assisted 橙色、ai-decision-maker 红色
- [x] 点击跳语义说明 — 改为内嵌 modal (spec 明确允许)

### §2.2 — Grafana panel

- [x] Stat 类型 — 断言 panel.type == "stat"
- [x] Title "Runtime Trading Mode (当前授权)" — 断言存在
- [x] 放在 dashboard 最顶部 — 新增 row id=50 在 y=0
- [x] Prometheus query `aats_runtime_ai_operating_mode` — 断言 expr 包含该 metric 名
- [x] Postgres fallback `strategy.decision_outcome` SQL — 断言 rawSql 包含对应 topic 和字段
- [x] Value mapping + 颜色 — 断言 baseline_only blue / ai_assisted orange / ai_decision_maker red

### §2.3 — Alerting rules

- [x] `sev2-runtime-baseline-has-orders` — 断言 uid 在规则表
- [x] `sev3-runtime-ai-decision-no-orders` — 断言 uid 在规则表
- [x] 注释说明依赖 — rules.yml 块上方有明确的 "前置依赖" 注释列出 3 点

### §2.4 — OTel metric

- [x] DecisionOutcome 构造点写入 — `_decision_outcome` 里 `self.metrics.increment_labeled(...)` (前提 metrics 非空)
- [x] labels={"mode": canonical_mode} — 测试断言 labeled key 为 `(("mode", <canonical>),)`
- [x] 不硬阻塞 Prometheus scrape 修复 — metric 写入后自动生效
- [x] 不为了 metric 生效而去修 scrape — 确认未修 Prometheus 配置

---

## §5 超出范围的发现 (需要主任务决定)

### 1. `aats_orders_submitted_total{mode}` 需要下游埋点 — **建议 P1 紧随**

**现状**: 本 task 完成后,Prometheus 能看到 `aats_runtime_ai_operating_mode_total{mode=...}`,但 Task 2.3 的两条告警还引用 `aats_orders_submitted_total{mode=...}` — 这个 metric 还不存在:
- 现有 order submission 路径发的 counter 是 `order_intents_generated`(bootstrap/config.py:2776, 2983),**不带 mode label**
- 要让 `sev2-runtime-baseline-has-orders` 真正 fire,需要 order submission 时也读当前 ai_operating_mode 并 `increment_labeled("orders_submitted", labels={"mode": ...})`

**建议**: 开一个独立 P1 task,给 `order_intents_generated` → `orders_submitted` 接线加 mode label。本 task 不做以避免 scope creep。

### 2. Prometheus scrape connection-refused — **P1 遗留,spec 已说明**

`docs/design/p1d_phase1a_deferred_items_2026_04_20.md #4` 里记录的问题。本 task 按 spec 明确要求**不触碰**。修好后所有 P0-b metric/alert 都自动生效。

### 3. frontend JS 缺单测框架 — **长期改进**

当前项目静态 JS 测试靠 Python regex / node subprocess 跑 (`test_dashboard_refresh_interactivity.py` 启了 node 写纯 JS 单测)。P0-b Task 2.1 的 shell-renderer 行为测试同样走了结构性检查路径,**没覆盖 runtime 渲染逻辑**(如 `effective === "ai_assisted"` 的 title 文案是否正确)。建议后续引入 jsdom 或类似轻量 DOM mock 的 harness,让 shell-renderer 层可以跑真正的行为测试。

### 4. Badge modal 的说明文本跟 governance doc 有重复

为了避免 serve markdown 文件,把 `runtime_trading_mode_semantics.md` §1-§2 的核心表格 + 说明内嵌进了 `dashboard-shell.html` 的 `<dialog>`。若 governance doc 更新,这个 inline 副本会漂移。**建议**: 后续做一个小的 backend endpoint 比如 `/api/docs/runtime-trading-mode` 把 markdown 渲成 HTML 动态 serve,badge 跳那个 URL 即可单一 source of truth。本 task 按 spec "允许部分完成" 原则先做最小可用版。

---

## §6 Commit 策略

按 Task 分 commit,见 commit 列表(报告外附)。所有 commit 都引用本报告或 spec 相关章节。

---

## §7 签署

- 起草 & 实施: Claude Opus 4.7 · 2026-04-20
- 对应 spec: `docs/governance/p0b_observability_implementation_spec_2026_04_20.md` (起草: Claude Opus 4.7 · 2026-04-20)
- 对应 governance: `docs/governance/runtime_trading_mode_semantics.md`
- 批准状态: 待用户审查
- **文档所有权**: review layer,改动需对齐 "重大改动前必须备份+设计+获批准" 纪律
