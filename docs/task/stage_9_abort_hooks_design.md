# Stage 9 — Drift Score + Abort Hooks 设计文档

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


> **本文档定位**：`docs/task/stage_9_dryrun_checklist.md`（checklist-1）规定了
> "跑到哪一步必须看什么、什么情况必须停"，但把两件事留作 TODO：
>
> 1. §4.4 的 drift score 怎么算（目前只说了一句 "返回 score ≤ 1"）
> 2. §4.3 的系统健康指标怎么在 SEV2+ 命中时自动 halt（目前只说了一句
>    "不再依赖人工抽检"）
>
> 本文档是 **checklist-2**，专门设计这两件事的实现蓝图。先写设计获批之后再
> 进入 checklist-3（实现 `scripts/compute_drift_score.py`）和 checklist-4
> （实现 `aats/services/governance_engine/abort_hooks.py`）。

## 1 背景与动机

### 1.1 为什么要 drift score

Stage 9 阶梯制要求每次上调（T1→T2→T3→T4）必须满足一个客观的"系统与预期
一致"的度量。历史上我们有三类工具：

- **Trial guard**（`aats/services/governance_engine/trial_guard.py`）：盯 24h
  日内累计 pnl / 连续亏损笔数 / 费用率 / 滑点率 / 成交延迟率。这是**硬阈值**
  守护，一旦命中就立即 halt。问题：阈值是绝对值，T1（1U）和 T4（1000U）需要
  的阈值完全不一样，静态 config 无法覆盖。
- **RDP observation_window**（`aats/data_platform/production_workflow/
  observation_window.py`）：release 后 24h 对比 quality_monitor / attribution
  / execution_realism。这是**离线批处理**，跑的是 artifact 上的静态 JSON，
  跟当前 live 系统的真实状态有分钟级延迟，而且只对单次参数 release 有意义，
  对"系统长期健康"没有直接输出。
- **Reconciliation**（`aats/services/reconciliation_service/comparator.py`）：
  只比对"交易所 vs 本地快照"这一个维度，不包含决策 / 执行 / 数据链路的健康。

Stage 9 需要的是第 4 类工具：**一个连续的、小时级更新的、跨多维度归一化的
实时漂移度量**，专门服务于阶梯上调决策。它不替代上面任何一种工具，而是把
它们的结论压缩成一个 0-8 的整数评分，让 operator 可以一眼判断"现在能不能
升到下一档"。

### 1.2 为什么要 abort hooks

Stage 9 checklist-1 §3.2 规定了"人工抽检节奏"（T1=每 2 小时，T4=每 24 小时）。
问题是：**抽检窗口之间是无人看守的**。T4 的 24h 抽检意味着一旦半夜 23:00
系统进入降级状态，最坏要到第二天 23:00 才被发现。14 天的 T4 观察期乘以平均
降级反应延迟，就算只有 1% 的时间窗发生问题也是几小时的无保护运行。

trial_guard 提供了一个兜底——它 60s 一次评估。但是：

1. 它只盯财务指标，盯不到 NATS handler 错误率 / OKX rate_limit 频率 /
   reconciliation mismatch 频率这种系统层信号
2. 它的阈值是"绝对值 USDT"，T1 的阈值如果抄到 T4 就毫无意义
3. 它的 halt reason 字符串是 `trial_guard_threshold_breached`，无法区分
   是哪一类指标命中

Abort hooks 要补的是：

1. **系统层**监测 handler/RPC/reconciliation 的错误率（trial_guard 不盯）
2. 直接读 drift score，score ≥ 3（定义见 §4）持续 2 个评估窗口则 halt
3. halt reason 带明确的 breakdown，operator 看日志一眼知道是哪一项出问题
4. 与 KillSwitch 的 sidecar 架构对齐：fail-soft、不阻塞主 loop、跨进程 halt

## 2 现状清点

### 2.1 已就位

- `aats/services/governance_engine/kill_switch.py`：Stage 6 Slice 6.4 的
  二合一 KillSwitch，`halt(reason)` 本地立即生效 + 自动跨进程广播
- `aats/services/governance_engine/trial_guard.py`：60s 评估一次的 forward
  trial guard，已经有 `anomaly_provider` / `profitability_provider` 的回调
  接口，abort_hooks 可以复用类似模式
- `aats/bootstrap/metrics.py` + `aats/services/governance_engine/health.py`：
  内存里已经维护 handler_error / reconciliation_mismatch 等计数器
- `aats/data_platform/metrics/metric_calculator.py`：5 层指标的离线计算函数
  （research / attribution / execution / operations / reliability），
  drift score 的**离线模式**可以直接复用
- `aats/data_platform/metrics/baseline_comparison.py`：两组 parameter set
  指标的比对工具，drift score 的**历史对比模式**可以借鉴
- `aats/events/topics.py`：已经有 `GOVERNANCE_EVENTS` 等 topic（检查一下是否
  需要新增一个 `governance.abort_hook_events`）
- Jaeger span + Loki 日志的全链路可观测性（Stage 8 已验）

### 2.2 缺口

- 没有一个能"把多个维度压缩成单一分数"的模块
- 没有一个"实时 sidecar 监测 handler 错误率 + 自动 halt"的 hook（trial_guard
  只覆盖财务维度）
- 没有把 drift score 暴露成 HTTP endpoint（运行中的系统 introspection）
- 没有把 abort hook 的 halt 事件单独分类（目前所有 halt 都塞 kill_switch
  reason 字符串里，检索困难）
- `scripts/` 下没有独立的 drift_score CLI（dryrun 人工抽检时希望命令行
  一条命令出分数）

## 3 目标与非目标

### 3.1 目标（Stage 9 checklist-2 收尾条件）

**设计阶段**（本文档）:

1. 定义 drift score 的**指标集合**、**归一化方式**、**聚合公式**
2. 定义 abort hook 的**状态机**、**评估周期**、**halt reason 编码**
3. 定义 drift score CLI 的**入参**、**输出 JSON schema**、**退出码映射**
4. 定义 abort hook sidecar 与 KillSwitch / bus / hot_state_store 的**配线方式**
5. 明确 fail-soft 边界：drift 计算 / abort hook 的任何异常都**不能**阻塞主
   trading loop

**实现阶段**（checklist-3 / checklist-4，不在本文档范围）:

6. `scripts/compute_drift_score.py` 落地，能在 dryrun 人工抽检时一条命令出分数
7. `aats/services/governance_engine/abort_hooks.py` 落地，与 kill_switch 一样
   作为 build_runtime 里的 sidecar 被启动
8. 单元测试覆盖 drift score 的边界条件（score=0 / score=1 / score=4 / 缺数据）
   + 覆盖 abort hook 的状态机（未命中 / 首次命中 / 连续命中 halt / clear）
9. runbook 新增 §9.6 "drift score 人工抽检" 与 §9.7 "abort hooks drill"

### 3.2 非目标（明确不做）

- 不做机器学习 drift detection（ADWIN / Page-Hinkley 等）。Stage 9 的目的是
  让 operator 快速 go/no-go，不是做学术漂移检测
- 不做实时 push-based 监控。abort hook 是 pull-based 的（60s 评估一次），
  与 trial_guard 对齐，避免引入额外的事件总线拓扑
- 不做"自动降阶"（T3 → T2）。abort hook 只能 halt，降阶必须人工执行，
  因为涉及账户余额挪动
- 不做跨账户聚合。Stage 9 是单一子账户的阶梯，drift score 的输入只看当前
  runtime scope 的数据
- 不做外部告警通道（email / slack / telegram webhook）。Stage 9 只写日志 +
  Grafana alert 面板即可，外部通道留到 Stage 10 运营阶段

## 4 Drift Score 设计

### 4.1 输入数据源

drift score 的**离线模式**（CLI）和**在线模式**（abort hook sidecar）共享
同一套计算函数，差别只在数据源：

| 维度 | 离线源（CLI） | 在线源（sidecar） |
|------|----------------|-------------------|
| 财务 | `portfolio_snapshot` JSON + OKX 历史账户流水 | `portfolio_service.latest_snapshot()` + `ledger` in-memory |
| 执行 | `execution_rounds/*/anomaly_report.json` | `execution_engine.health.snapshot()` in-memory counters |
| 决策 | `decision_registry.json` + `decision_rounds` | `decision_engine.metrics.cycle_count_last_1h` |
| 数据链路 | `quality_monitor_summary.json` | `governance_engine.health.GovernanceHealth` |
| 基线对照 | `baseline_comparison.find_baseline_for_release()` 的结果 | `settings.stage9_dryrun_baseline_*` 配置值 |

### 4.2 指标集合（4 类 × 共 10 项）

#### 4.2.1 财务类（最高权重，占总分 1/3）

| 指标 | 说明 | 归一化 |
|------|------|-------|
| `balance_drift_ratio` | abs(余额变化 − 期望 pnl) / 当前阶梯名义规模 | 0 if ≤1%，1 if ≤5%，2 if >5% |
| `max_drawdown_ratio` | (峰值 − 当前) / 当前阶梯名义规模 | 0 if ≤3%，1 if ≤5%，2 if >5% |
| `fee_to_pnl_ratio` | 24h 累计手续费 / max(abs(24h realized pnl), 阶梯规模×1%) | 0 if ≤30%，1 if ≤60%，2 if >60% |

#### 4.2.2 执行类（占总分 1/4）

| 指标 | 说明 | 归一化 |
|------|------|-------|
| `fill_success_ratio` | fill_events / order_intents（滚动 1h） | 0 if ≥98%，1 if ≥90%，2 if <90% |
| `adverse_slippage_ratio` | 高滑点 fill / 总 fill（滚动 1h） | 0 if ≤2%，1 if ≤10%，2 if >10% |

#### 4.2.3 决策类（占总分 1/4）

| 指标 | 说明 | 归一化 |
|------|------|-------|
| `decision_cycle_cadence_ratio` | 实际完成 decision_cycle 数 / 期望数（基于 profile 的 cycle_interval） | 0 if ≥95%，1 if ≥80%，2 if <80% |
| `decision_error_ratio` | `decision_cycle_error` 日志 / 总 `decision_cycle_*` 日志 | 0 if ≤1%，1 if ≤5%，2 if >5% |

#### 4.2.4 数据链路类（占总分 1/6）

| 指标 | 说明 | 归一化 |
|------|------|-------|
| `reconciliation_mismatch_count` | 最近 24h `reconciliation_mismatch` 事件数 | 0 if =0，1 if ≤2，2 if >2 |
| `nats_handler_error_ratio` | handler_error 数 / 总消息数 | 0 if ≤0.1%，1 if ≤1%，2 if >1% |
| `okx_rate_limit_count` | 最近 1h `okx_rest_rate_limited` 次数 | 0 if =0，1 if ≤3，2 if >3 |

### 4.3 聚合公式

每项指标归一化到 `{0, 1, 2}` 之后，按类别做加权平均（权重在 §4.2 的"占总分"
列给出），再四舍五入成整数总分：

```
financial_subscore = mean([balance_drift, drawdown, fee_pnl])  # ∈ [0, 2]
execution_subscore = mean([fill_success, adverse_slippage])    # ∈ [0, 2]
decision_subscore  = mean([cadence, error_ratio])              # ∈ [0, 2]
data_subscore      = mean([mismatch, nats_err, rate_limit])    # ∈ [0, 2]

total_score = round(
    financial_subscore * (1/3) * 4
  + execution_subscore * (1/4) * 4
  + decision_subscore  * (1/4) * 4
  + data_subscore      * (1/6) * 4
)

# 理论上限 = (2 * 4/3) + (2 * 1) + (2 * 1) + (2 * 2/3) = 2.67 + 2 + 2 + 1.33 = 8
# 归一到 [0, 8] 的整数
```

> **为什么放大 4 倍**：保持每个子项贡献值是"小数"太难让 operator 一眼读懂，
> 放大到整数 0-8 区间之后 score=0 全绿 / score=1-2 小瑕疵 / score=3-4 可疑 /
> score ≥5 危险，和 checklist-1 §4.4 的 "score ≤ 1 才能升阶梯"对齐。

### 4.4 分数到动作的映射

| 总分 | 状态 | checklist-1 阶梯升级 | abort hook 动作 |
|------|------|-----------------------|------------------|
| 0 | clean | 允许 | 无 |
| 1 | minor_drift | 允许（但 operator 要记录） | 无 |
| 2 | noticeable_drift | 禁止升阶梯，但继续观察 | 无 |
| 3 | significant_drift | 禁止升阶梯 | 进入 warning 状态 |
| 4 | severe_drift | 禁止升阶梯 | 连续 2 次命中 → halt |
| ≥5 | critical_drift | 禁止升阶梯 | **立即** halt |

### 4.5 DriftReport JSON schema

`compute_drift_score` 统一返回以下结构：

```json
{
  "schema_version": "stage9.drift_score/v1",
  "evaluated_at": "2026-04-08T11:30:00+00:00",
  "stage": "T2",
  "nominal_scale_usdt": 10,
  "window": { "start": "2026-04-07T11:30:00+00:00", "end": "2026-04-08T11:30:00+00:00" },
  "subscores": {
    "financial": { "value": 0.33, "indicators": [
      { "name": "balance_drift_ratio",  "raw": 0.008, "normalized": 0 },
      { "name": "max_drawdown_ratio",   "raw": 0.021, "normalized": 0 },
      { "name": "fee_to_pnl_ratio",     "raw": 0.42,  "normalized": 1 }
    ]},
    "execution": { "value": 0.0, "indicators": [
      { "name": "fill_success_ratio",   "raw": 0.992, "normalized": 0 },
      { "name": "adverse_slippage_ratio","raw": 0.01, "normalized": 0 }
    ]},
    "decision":  { "value": 0.0, "indicators": [ /* ... */ ]},
    "data":      { "value": 0.0, "indicators": [ /* ... */ ]}
  },
  "total_score": 1,
  "state": "minor_drift",
  "allow_ladder_upgrade": true,
  "abort_hook_action": "none",
  "notes": [
    "fee_to_pnl_ratio 偏高但未越线，观察 1~2 天再决定是否调 exec.price_grid",
    "financial/execution/decision/data 子项都在 clean 区间"
  ]
}
```

## 5 Abort Hook 设计

### 5.1 状态机

```
           evaluate_cycle (60s)
                │
                ▼
          ┌──────────┐  score<3    ┌──────────┐
          │ monitoring│─────────▶│ monitoring │
          └──────────┘             └──────────┘
                │ score in [3,4]
                ▼
          ┌──────────┐  score<3 连续 2 次    ┌──────────┐
          │ warning  │──────────────────────▶│ monitoring │
          └──────────┘                        └──────────┘
                │ score in [3,4] 再次命中
                │ (连续 2 次命中)
                ▼
          ┌──────────┐
          │ halting  │── kill_switch.halt(reason="stage9_abort_hook:<code>")
          └──────────┘
                │ operator 手动 resume
                ▼
          ┌──────────┐
          │ resumed  │── 进入 cooldown 30 分钟
          └──────────┘
                │ cooldown 结束
                ▼
          monitoring
```

- `score ≥ 5` 绕过 warning，直接 halting
- `halting → resumed` 只能由 operator 人工 `probe_kill_switch.py resume`
  触发，abort hook 不会自动复位
- resumed 后进入 30 分钟 cooldown，期间即使再次触发也只记 warning 不 halt，
  避免一个 transient OKX 事故把系统反复 halt/resume 打脏 event store

### 5.2 Halt reason 编码

统一前缀 `stage9_abort_hook:`，后面跟 breakdown 代码：

| reason 字符串 | 触发条件 |
|----------------|----------|
| `stage9_abort_hook:score_ge_5` | 单次评估 total_score ≥ 5 |
| `stage9_abort_hook:score_3_4_consecutive_2` | 连续 2 次 total_score ∈ [3,4] |
| `stage9_abort_hook:subscore_financial_2` | financial_subscore 为 2 (critical) |
| `stage9_abort_hook:subscore_data_2` | data_subscore 为 2 (critical) |

operator 看 halt reason 即知哪一类问题，配合 drift_report JSON 可直接定位
具体指标。

### 5.3 AbortHookService 接口

```python
# aats/services/governance_engine/abort_hooks.py

@dataclass
class AbortHookConfig:
    enabled: bool
    evaluate_interval_seconds: int  # 默认 60
    consecutive_warning_threshold: int  # 默认 2
    cooldown_after_resume_seconds: int  # 默认 1800
    stage_nominal_scale_usdt: Decimal  # 从 settings.stage9_current_stage_scale 读
    baseline_snapshot_path: Path | None  # 从 T0 DRY 跑完后冻结的 baseline


class AbortHookService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        kill_switch: KillSwitch,
        metrics: MetricsRegistry,
        event_store: EventStore,
        portfolio_service: PortfolioService,
        execution_health: ExecutionHealth,
        governance_health: GovernanceHealth,
        config: AbortHookConfig,
        logger: logging.Logger,
    ) -> None: ...

    async def start(self) -> None:
        """启动后台 task，每 evaluate_interval_seconds 调用一次 evaluate。"""

    async def stop(self) -> None:
        """优雅停止后台 task（用于 build_runtime.stop_background_tasks）。"""

    async def evaluate_once(self) -> DriftReport:
        """手动触发一次评估，供 probe / test 使用。返回最新 report。"""

    def snapshot(self) -> dict[str, Any]:
        """供 /system/abort_hook/state endpoint 返回 JSON 的 introspection。"""

    # 内部
    async def _loop(self) -> None: ...
    async def _evaluate_and_act(self) -> None: ...
    def _transition(self, new_state: AbortHookState, reason: str) -> None: ...
```

fail-soft 约束：

- `_evaluate_and_act` 的**任何**异常都要 `try/except` 吞掉并走
  `log_event("abort_hook_evaluation_failed", level="error", ...)`
- 评估失败**不计入** consecutive_warning_threshold（避免因为数据源短暂不可用
  误触发 halt）
- start 时如果 `config.enabled is False` → 不开后台 task，`snapshot()` 返回
  `{"enabled": False, "state": "disabled"}`
- 与 trial_guard 一样，AbortHookService 是 **decision/execution** 进程的
  sidecar，gateway / market 进程不启动（gateway 本身不做业务决策，market
  只推行情；真正需要 halt 的 decision/execution 自己检测即可，跨进程靠
  KillSwitch 自动广播）

### 5.4 与 KillSwitch 的配线

```python
# aats/bootstrap/config.py build_runtime 内

if effective_process_role in {PROCESS_ROLE_DECISION, PROCESS_ROLE_EXECUTION, PROCESS_ROLE_MONOLITH}:
    abort_hook_cfg = AbortHookConfig.from_settings(settings)
    abort_hooks = AbortHookService(
        settings=settings,
        kill_switch=kill_switch,
        metrics=metrics_registry,
        event_store=event_store,
        portfolio_service=portfolio_service,
        execution_health=execution_health_service,
        governance_health=governance_health_service,
        config=abort_hook_cfg,
        logger=get_logger("aats.governance.abort_hooks"),
    )
    runtime._background_services.append(abort_hooks)
```

与 kill_switch 不同，AbortHookService **不需要** bootstrap hydration——它
每一次 evaluate 都是从当前 live 数据重新算分，不持有长期状态。唯一需要
在重启之间保留的是 cooldown 窗口结束时间戳，这个可以写入
`aats:hot:governance:abort_hook_cooldown_until` 的 hot state key（可选，
MVP 可以先不做，重启即重置 cooldown）。

## 6 CLI `scripts/compute_drift_score.py` 设计

### 6.1 入参

```
python scripts/compute_drift_score.py \
  --stage T1|T2|T3|T4 \
  --window-hours 24 \
  [--source live|offline] \
  [--baseline artifacts/stage9/baseline_t0_dry.json] \
  [--output report.json] \
  [--json] [--verbose]
```

- `--stage` 必选：决定 `nominal_scale_usdt` 如何映射
- `--window-hours` 默认 24，T1 建议用 48（样本太小）
- `--source` 默认 `live`（读 http://localhost:8080/system/abort_hook/state），
  `offline` 则读 artifacts 目录
- `--baseline` 指向 T0 DRY 冻结的 baseline 文件（对比历史用）
- `--output` 把 DriftReport JSON 落盘，默认不落盘只 print
- `--json` print JSON，默认 print 人类可读表格
- `--verbose` 打印每个 indicator 的 raw 值和归一化过程

### 6.2 退出码

| 退出码 | 含义 | 用途 |
|-------|------|------|
| 0 | score ≤ 1 | dryrun 升阶梯 gate 通过 |
| 1 | 运行错误（数据缺失、网络失败） | CI / 脚本编排 |
| 2 | score = 2 | 禁止升阶梯，但可以继续观察 |
| 3 | score ∈ [3, 4] | 禁止升阶梯，要人工复盘 |
| 4 | score ≥ 5 | 禁止升阶梯，建议立即 halt |

dryrun ladder 升级脚本可以用 `if ! compute_drift_score.py --stage T1; then echo "BLOCKED"; exit 1; fi` 做自动化 gate。

### 6.3 输出格式（人类可读）

```
Stage 9 Drift Score — T2 (nominal 10 USDT)
Window: 2026-04-07 11:30 → 2026-04-08 11:30 UTC

Financial    0.33  (balance=0  drawdown=0  fee/pnl=1)
Execution    0.00  (fill=0  slippage=0)
Decision     0.00  (cadence=0  error=0)
Data link    0.00  (mismatch=0  nats_err=0  rate_limit=0)

TOTAL SCORE  1    ── minor_drift

 → Ladder upgrade: ALLOWED (with notes)
 → Abort hook action: none

Notes:
 • fee_to_pnl_ratio 偏高但未越线，观察 1~2 天再决定是否调 exec.price_grid
```

## 7 实现路线图

### 7.1 Slice checklist-3（compute_drift_score.py）

优先做纯函数版本，**不接** live 数据源，先跑 offline 模式：

1. `aats/services/governance_engine/drift_score.py`：纯函数 `compute_drift_score(inputs: DriftInputs) -> DriftReport`
2. `aats/services/governance_engine/drift_inputs.py`：`DriftInputs` dataclass +
   `DriftInputs.from_artifacts(root: Path, stage: str, window_hours: int)`
3. `scripts/compute_drift_score.py`：薄封装 → 调 `DriftInputs.from_artifacts` →
   调 `compute_drift_score` → print / exit
4. `tests/unit/governance/test_drift_score.py`：20+ 测试
   - all-zeros 输入 → score=0
   - 单一 critical 指标 → 对应子项 subscore=2
   - 缺数据（指标 None）→ 归一化为 0 但 notes 里加 "missing data"
   - 聚合公式的边界（子项全 1 → total=4，子项全 2 → total=8）

### 7.2 Slice checklist-4（abort_hooks.py）

在 checklist-3 落地后，复用 `drift_score.py` 的纯函数：

1. `aats/services/governance_engine/abort_hooks.py`：`AbortHookConfig` +
   `AbortHookService` + 状态机 + fail-soft 封装
2. `aats/services/governance_engine/drift_inputs.py` 里补 `DriftInputs.from_live(
   portfolio_service, execution_health, ...)`，供 sidecar 使用
3. `aats/bootstrap/config.py` 里接 AbortHookService 到 runtime
4. `aats/api/routes.py` 里加 `GET /system/abort_hook/state` endpoint（read-only，
   需要 `require_read_access` dependency）
5. `tests/unit/governance/test_abort_hooks.py`：状态机测试
   - clean → clean 循环
   - clean → warning (score 3)
   - warning → clean (score 回落)
   - warning → halting (连续 2 次 score 3)
   - clean → halting (直接 score 5)
   - halting 之后 operator resume → cooldown → monitoring
   - cooldown 内再次 score 3 只记 warning 不 halt
   - `_evaluate_and_act` 抛异常 → 不计 consecutive / 不 halt / log 一次
6. `tests/integration/test_abort_hooks_integration.py`：完整 build_runtime
   场景，mock portfolio/execution/governance health 返回构造好的 drift 输入，
   断言 KillSwitch 被调用一次且 reason 编码正确

### 7.3 Slice checklist-5（runbook + drill）

1. `deploy/wsl2-dev/RUNBOOK.md` 新增：
   - §9.6 "Drift score 人工抽检"：跑 `compute_drift_score.py --stage T1 --verbose`
     的预期输出与常见问题排查
   - §9.7 "Abort hook drill"：用 probe 注入 mock 高 score 指标（或直接通过
     feature flag 切到 `mock_drift=True` 模式），验证 60s 内 halt 生效，
     operator resume 后 cooldown 正确
2. 设置 `AATS_STAGE9_ABORT_HOOK_ENABLED` 环境变量 gate（默认 False，
   T0 DRY 浸泡期打开跑一次验证，T1 上线时开为 True）

## 8 风险与缓解

| 风险 | 缓解 |
|------|------|
| drift score 算错导致假警报 halt | 1) CLI 模式提前跑 T0 DRY 数据一次，手动校准阈值；2) `consecutive_warning_threshold=2` 避免单次误判；3) cooldown 防止反复 halt |
| drift 计算本身抛异常拖死 main loop | AbortHookService._loop 整个 body 全部包在 try/except 里，每次异常独立记录，永不 propagate |
| sidecar 读 live 数据源产生 race condition | 所有 getter 走 `portfolio_service.snapshot()` 等 immutable 返回值，不直接访问 mutable state |
| score=0 假象（数据源本身不可用返回 None） | 归一化时 None 算 0 但 notes 里写 "missing data"，drift report `allow_ladder_upgrade` 为 False 且人类报告里红字提示 |
| dryrun ladder gate 被意外跳过 | `compute_drift_score.py` 的退出码必须被 ladder 升级脚本硬编码检查，不允许 `|| true` |
| 多个进程同时算 drift score 浪费资源 | AbortHookService 只在 decision + execution 进程启动，gateway/market 不跑；sidecar 与 trial_guard 分别承担不同维度不冲突 |
| halt reason 字符串过长影响日志 | reason 限制 ≤ 80 字符（`stage9_abort_hook:<code>`），breakdown 详情写 drift_report JSON 而不是塞 reason |

## 9 测试与验收标准

### 9.1 单元测试覆盖率目标

- `drift_score.py`：≥ 95%（纯函数，好写）
- `abort_hooks.py`：≥ 90%（状态机 + fail-soft 分支全覆盖）
- `drift_inputs.py` 的 `from_live` 走 integration test，`from_artifacts` 走
  unit test

### 9.2 集成测试场景

1. **full-green 场景**：mock 所有 health service 返回正常值 → 60s 后 abort
   hook 状态仍是 `monitoring`，kill_switch 未被调用
2. **financial-critical 场景**：mock portfolio_snapshot 让 `balance_drift_ratio=10%` →
   financial_subscore=2 → total_score ≥ 5 → 60s 内 halt（score_ge_5 reason）
3. **decision-slow 场景**：mock decision_cycle_cadence_ratio=75% (1 分) + decision_error_ratio=6% (2 分) → decision_subscore 平均 1.5 round 到 2 → 结合 score 可能落在 [3,4] → 连续 2 次命中后 halt
4. **transient 场景**：第 1 次评估 score=4，第 2 次评估 score=0 → warning 状态
   一次后回到 monitoring，kill_switch 未被调用
5. **cooldown 场景**：halt → operator resume → 10 分钟后 score=5 → 不 halt
   （still in cooldown），30 分钟后 score=5 → halt

### 9.3 WSL2 真跑验收

1. 所有 4 进程 rebuild 后 `healthy`，decision 和 execution 日志里各有一条
   `abort_hook_service_started`
2. `curl http://localhost:8080/system/abort_hook/state` 返回 `{"state": "monitoring", ...}`
3. `docker exec aats-decision python /tmp/probe_kill_switch.py halt manual_test` →
   abort hook 日志里要看到 `kill_switch_observed_halt source=external`（hook
   本身不触发，但要能观察到外部 halt）
4. `docker exec aats-decision python /tmp/probe_abort_hook_trigger.py financial_critical`
   （需要写一个 mock probe）→ 60s 内看到 `abort_hook_fired reason=stage9_abort_hook:score_ge_5`
   并且 4 个容器都应用 halt
5. operator `probe_kill_switch.py resume` 后，decision 日志里在 30 分钟后
   看到 `abort_hook_cooldown_ended`

## 10 开放问题（Follow-up）

以下问题在 MVP 范围外，留到 checklist-6 再决定：

1. 是否把 drift score 暴露到 Prometheus 作为 gauge（`aats_stage9_drift_score{process_role="decision"}`）
2. 是否把 drift_report 序列化进 NATS `governance.drift_reports` topic，供
   RDP 离线批处理消费
3. cooldown 结束时间戳是否持久化到 Redis hot state（重启能恢复）
4. 是否允许 operator 通过 `/system/abort_hook/reset_cooldown` endpoint 手动
   结束 cooldown（方便 drill）
5. abort hook 是否需要独立的 `/healthz` 子路径供 docker healthcheck

## 11 变更影响范围

### 11.1 新增文件

- `aats/services/governance_engine/drift_score.py`（纯函数，~250 行）
- `aats/services/governance_engine/drift_inputs.py`（数据收集，~200 行）
- `aats/services/governance_engine/abort_hooks.py`（sidecar，~350 行）
- `scripts/compute_drift_score.py`（CLI，~120 行）
- `tests/unit/governance/test_drift_score.py`（~400 行，20+ 用例）
- `tests/unit/governance/test_abort_hooks.py`（~350 行，15+ 用例）
- `tests/integration/test_abort_hooks_integration.py`（~250 行）

### 11.2 修改文件

- `aats/bootstrap/config.py`：build_runtime 里接 AbortHookService
- `aats/bootstrap/settings.py`：新增 `stage9_abort_hook_enabled` /
  `stage9_current_stage_scale` / `stage9_abort_hook_evaluate_interval` 等配置
- `aats/api/routes.py`：新增 `GET /system/abort_hook/state`
- `deploy/wsl2-dev/RUNBOOK.md`：新增 §9.6 和 §9.7

### 11.3 不修改（零侵入）

- `aats/services/governance_engine/trial_guard.py`：保持原状，两者独立
- `aats/services/governance_engine/kill_switch.py`：保持原状，abort hook 只
  通过公开 API (`halt(reason)`) 调用
- 所有 `aats/services/{decision_engine,execution_engine,market_gateway}/`
  业务代码：零侵入（abort hook 只读不写）

---

## 12 回顾 checklist

写完这份设计后，checklist-2 的验收项是：

- [x] drift score 的指标清单（§4.2）
- [x] 归一化与聚合公式（§4.3 / §4.4）
- [x] DriftReport JSON schema（§4.5）
- [x] Abort hook 状态机（§5.1）
- [x] halt reason 编码（§5.2）
- [x] AbortHookService 接口（§5.3）与 runtime 配线（§5.4）
- [x] compute_drift_score CLI 设计（§6）
- [x] 实现路线图（§7）
- [x] 风险清单（§8）
- [x] 测试矩阵（§9）

接下来进入 Stage 9 checklist-3：落地 `drift_score.py` 纯函数 +
`scripts/compute_drift_score.py` CLI + 单元测试。

本文档完。
