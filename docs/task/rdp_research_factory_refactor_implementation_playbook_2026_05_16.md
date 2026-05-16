# RDP Research Factory 重构实施 Playbook

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：执行指导稿
> 上级文档：[RDP 研究工厂重构计划书](../rdp_research_factory_refactor_sow_2026_05_16.md)
> 适用范围：后续人工开发与 Codex automation 自动开发
> 核心纪律：每次只完成一个编号 task card，未满足验收前不得跳到下一阶段

---

## 1. 为什么需要本 Playbook

上级 SOW 已经定义方向：把 Qlib 的 research substrate 和 RD-Agent 的研发闭环吸收到 AATS RDP，但不把两个外部项目整包搬进 live runtime。

本 Playbook 解决另一个问题：**防止后续连续开发漂移**。任何后续开发任务必须按本文的编号、边界、文件范围、测试范围和停机条件推进。

如果本文与上级 SOW 冲突，以更保守、更靠近 live trading 安全边界的一方为准。

---

## 2. 全局执行协议

### 2.1 每次自动开发运行前必须读取

按顺序读取：

1. `AGENTS.md`
2. `CLAUDE.md`
3. `docs/rdp_research_factory_refactor_sow_2026_05_16.md`
4. 本文件
5. `docs/rdp/module_reference.md`
6. `docs/operations/artifact_conventions.md`
7. `docs/operations/parameter_governance.md`
8. 当前 `git status --short`

### 2.2 每次只能选择一个 task card

选择规则：

1. 从最小编号开始找未完成 task card。
2. 如果一个 task card 的文件已存在，先读现状和测试，再决定是补齐还是跳过。
3. 不允许同一轮跨两个 phase 写代码。
4. 不允许借机重构 live execution、OKX adapter、ledger、reconciliation、risk guard、Operator write API。
5. 如果 task card 需要这些 live 模块，必须停止并起草单独 SOW。

### 2.3 每次运行的固定输出

每次自动开发结束必须报告：

```text
Selected task card:
Current behavior:
Files changed:
Tests run:
Exact test results:
Skipped validations and reason:
Residual risks:
Next unfinished task card:
```

### 2.4 完成定义

一个 task card 只有同时满足以下条件才算完成：

- 代码实现存在且在预定文件范围内。
- 单元测试覆盖正常路径、失败路径和边界条件。
- `ruff check aats/ --fix` 已运行，或说明本次只改 docs/tests 且为何不需要。
- 相关 targeted tests 已运行。
- 若改动影响 DB、API、scheduler、WSL2 行为，必须运行最窄 WSL2 集成测试。
- 没有引入生产 runtime 依赖。
- 没有读取、打印或复制 `.env*`、key、token、password。
- final report 明确下一张未完成卡片。

### 2.5 必须停下来的条件

遇到以下情况不得继续实现：

- 需要新增数据库 migration，但没有单独 migration SOW。
- 需要改 live execution / OKX adapter / ledger / reconciliation / risk guard。
- 需要把 qlib 或 rdagent 加到生产依赖。
- 需要访问生产凭证或 `.env.*.live` 内容。
- 需要自动 apply active parameter。
- 现有 dirty files 与本 task card 写入范围冲突且无法判断是否用户改动。
- 测试失败原因不清楚，且继续改动会扩大范围。

---

## 3. 目标目录和命名约定

Research Factory 第一阶段代码目录：

```text
aats/data_platform/research_factory/
  __init__.py
  status.py
  specs.py
  artifacts.py
  datasets/
    __init__.py
    segments.py
    gold_bars.py
  features/
    __init__.py
    expressions.py
    functions.py
  experiments/
    __init__.py
    recorder.py
    runner.py
  metrics/
    __init__.py
    snapshots.py
    gates.py
  benchmarks/
    __init__.py
    baseline.py
  allocation/
    __init__.py
    policy.py
  sandbox/
    __init__.py
    proposal.py
    guardrails.py
```

测试目录：

```text
tests/unit/data_platform/research_factory/
  test_specs.py
  test_artifacts.py
  test_segments.py
  test_gold_bars_dataset.py
  test_factor_expressions.py
  test_experiment_recorder.py
  test_metrics_snapshots.py
  test_promotion_gates.py
  test_allocation_policy.py
  test_sandbox_guardrails.py
```

配置目录：

```text
configs/research_factory/
  baseline_workflow.json
  sandbox_policy.json
```

禁止新增：

```text
aats/services/execution_engine/*
aats/services/okx*
aats/services/reconciliation*
aats/services/risk*
apps/api_gateway/* 写端点
scripts/* production apply 写脚本
```

---

## 4. 数据模型契约

第一阶段优先使用 AATS-native model。实现时先检查仓库现有 Pydantic/dataclass 习惯；若没有强制统一，优先使用标准库 dataclass + 显式 validation，避免新增依赖。

### 4.1 `ResearchStatus`

文件：`aats/data_platform/research_factory/status.py`

合法值：

```text
draft
pending
running
succeeded
partial_success
failed
cancelled
```

要求：

- status 字符串必须集中定义，不允许各模块硬编码散落。
- 提供 `is_terminal_status(status)`。
- 提供 `require_valid_status(status)`，非法时抛 `ValueError`。

### 4.2 `DatasetSpec`

文件：`aats/data_platform/research_factory/specs.py`

必须字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `dataset_id` | string | 稳定 id，由 symbol/timeframe/window/version 派生或显式传入 |
| `symbol` | string | 例如 `BTC-USDT-SWAP` |
| `timeframe` | string | 例如 `1m` / `15m` / `1h` |
| `dataset_version` | string | 研究数据版本 |
| `window_start` | datetime UTC | 总窗口开始 |
| `window_end` | datetime UTC | 总窗口结束，必须大于 start |
| `segments` | list[`SegmentSpec`] | train/valid/test/replay 切片 |
| `source_refs` | dict | gold/funding/orderbook 来源引用 |

验证要求：

- 所有 datetime 必须 timezone-aware。
- segment 不得越过 window。
- train/valid/test 默认不得重叠。
- test segment 不得早于 train segment。
- dataset_id 不得包含路径分隔符。

### 4.3 `SegmentSpec`

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | `train` / `valid` / `test` / `replay` |
| `start` | datetime UTC | segment 开始 |
| `end` | datetime UTC | segment 结束 |
| `purpose` | string | 解释用途 |

验证要求：

- `end > start`。
- name 必须白名单。
- replay 可与 test 同窗口，但必须显式声明 purpose。

### 4.4 `ProcessorSpec`

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | processor 名称 |
| `params` | dict | 参数 |
| `version` | string | processor 版本 |

第一批允许 processor：

```text
drop_missing
forward_fill_limited
winsorize
zscore
minmax
leakage_guard
```

禁止 processor：

- 任意 Python callable 字符串。
- eval/exec。
- 访问文件系统或网络的 processor。

### 4.5 `LabelSpec`

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | label 名称 |
| `horizon_bars` | int | 预测窗口，必须 > 0 |
| `return_kind` | string | `simple_return` / `log_return` |
| `net_of_fee` | bool | 是否扣手续费 |
| `net_of_slippage` | bool | 是否扣滑点 |
| `include_funding` | bool | 是否包含 funding |
| `fee_bps` | float | fee 假设 |
| `slippage_bps` | float | slippage 假设 |

第一版默认 label：

```text
future_net_return_h{horizon_bars}
```

不得直接沿用 Qlib 股票默认 label。必须表达 OKX 衍生品的成本和 funding 语义。

### 4.6 `ExperimentSpec`

字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `experiment_id` | string | 唯一 id |
| `dataset` | `DatasetSpec` | 数据定义 |
| `features` | list[`FeatureSpec`] | 特征定义 |
| `label` | `LabelSpec` | 目标定义 |
| `model_ref` | string | baseline/model id |
| `metrics` | list[string] | 必算指标 |
| `artifact_root` | path string | artifact 根路径 |
| `governance_mode` | string | `candidate_only` 第一版唯一允许 |

验证要求：

- `governance_mode` 第一版只能是 `candidate_only`。
- artifact_root 必须位于 `artifacts/research/` 下，测试可用 tmp path。
- experiment_id 不得覆盖已有目录，除非显式 attempt id。

### 4.7 `MetricsSnapshot`

必须覆盖四类：

| 类别 | 第一版字段 |
|------|------------|
| signal | `ic`, `rank_ic`, `icir`, `rank_icir` |
| return | `annualized_return`, `net_annualized_return`, `information_ratio`, `sharpe`, `max_drawdown` |
| cost | `turnover`, `fee_bps_mean`, `slippage_bps_mean`, `funding_bps_mean` |
| execution | `fillable_ratio`, `partial_fill_ratio`, `cost_adjusted_edge_bps_mean` |

字段可为 null，但 null 必须有 `missing_reasons`。

### 4.8 `ArtifactManifest`

必须兼容现有 round manifest 约定：

| 字段 | 说明 |
|------|------|
| `artifact_id` | experiment 或 round id |
| `artifact_type` | `experiment` / `dataset` / `benchmark` / `proposal` |
| `status` | ResearchStatus |
| `started_at` / `finished_at` | UTC |
| `input_refs` | dataset/config/code refs |
| `output_refs` | 文件相对路径 |
| `metrics_ref` | metrics snapshot 路径 |
| `code_version` | git commit 或 dirty marker |
| `notes` | 非敏感备注 |

---

## 5. 编号 Task Cards

### RF-P0-01: 创建 research_factory 包和 status 契约

目标：建立最小模块边界。

允许写入：

- `aats/data_platform/research_factory/__init__.py`
- `aats/data_platform/research_factory/status.py`
- `tests/unit/data_platform/research_factory/test_status.py`

必须实现：

- `VALID_RESEARCH_STATUSES`
- `TERMINAL_RESEARCH_STATUSES`
- `is_terminal_status(status: str) -> bool`
- `require_valid_status(status: str) -> str`

必须测试：

- 所有合法状态通过。
- 非法状态抛 `ValueError`。
- terminal status 判定准确。

禁止：

- 新增 DB migration。
- 接触 artifacts。
- 接触 RDP workflow scheduler。

完成后下一步：RF-P0-02。

### RF-P0-02: 实现基础 spec dataclass/model

允许写入：

- `aats/data_platform/research_factory/specs.py`
- `tests/unit/data_platform/research_factory/test_specs.py`

必须实现：

- `SegmentSpec`
- `DatasetSpec`
- `ProcessorSpec`
- `LabelSpec`
- `FeatureSpec`
- `ExperimentSpec`
- `MetricsSnapshot`

必须测试：

- timezone-naive datetime 被拒绝。
- window_end <= window_start 被拒绝。
- segment 越界被拒绝。
- train/valid/test 重叠被拒绝。
- artifact_root 越界被拒绝。
- governance_mode 非 `candidate_only` 被拒绝。

禁止：

- 引入 qlib/rdagent。
- 直接查询 DB。
- 生成真实 artifacts。

完成后下一步：RF-P0-03。

### RF-P0-03: Artifact manifest writer

允许写入：

- `aats/data_platform/research_factory/artifacts.py`
- `tests/unit/data_platform/research_factory/test_artifacts.py`

必须实现：

- `build_artifact_manifest(...)`
- `write_artifact_manifest_atomic(path, manifest)`
- `validate_artifact_manifest(manifest)`
- 相对路径规范化工具，禁止 `..` 跳出 artifact root。

必须测试：

- 原子写入成功。
- manifest 缺必填字段被拒绝。
- output_refs 包含 `..` 被拒绝。
- status 非法被拒绝。
- JSON 输出稳定排序，便于 diff。

完成后下一步：RF-P0-04。

### RF-P0-04: Research workflow spec 与 P0 集成测试

允许写入：

- `aats/data_platform/research_factory/specs.py`
- `tests/unit/data_platform/research_factory/test_specs.py`

必须实现：

- `ResearchWorkflowSpec`
- workflow stages 白名单：`dataset`, `feature`, `experiment`, `benchmark`, `governance`, `sandbox`
- `choose_next_stage(workflow_state)` 可选，不做 scheduler。

必须测试：

- 缺少 dataset stage 被拒绝。
- sandbox stage 出现在 governance apply 后被拒绝。
- workflow 只允许 research-only output。

完成后下一步：RF-P1-01。

### RF-P1-01: Segment split helper

允许写入：

- `aats/data_platform/research_factory/datasets/__init__.py`
- `aats/data_platform/research_factory/datasets/segments.py`
- `tests/unit/data_platform/research_factory/test_segments.py`

必须实现：

- `build_time_segments(window_start, window_end, train_ratio, valid_ratio, test_ratio)`
- `assert_no_leakage(segments)`
- `segment_for_timestamp(ts, segments)`

必须测试：

- 边界时间归属。
- ratio 合计不为 1 被拒绝。
- segment 空窗口被拒绝。
- replay segment 显式允许重叠，其他不允许。

完成后下一步：RF-P1-02。

### RF-P1-02: Gold bar dataset handler V1

允许写入：

- `aats/data_platform/research_factory/datasets/gold_bars.py`
- `tests/unit/data_platform/research_factory/test_gold_bars_dataset.py`

必须实现：

- `GoldBarRecord` 轻量数据对象。
- `GoldBarDatasetHandler.prepare(records, dataset_spec)`。
- 输出按 segment 分组的 rows。
- 校验 symbol/timeframe/window。
- funding 字段允许缺失，但必须记录 missing reason。

必须测试：

- records 乱序时输出按 timestamp 排序。
- symbol/timeframe 不匹配被拒绝或过滤策略明确。
- 重复 timestamp 行为明确，第一版建议拒绝。
- segment 外数据不进入输出。
- 空 segment 返回 failed validation，不静默成功。

禁止：

- 第一版不要直接连 DB；用 records 输入，DB adapter 后续单独做。

完成后下一步：RF-P1-03。

### RF-P1-03: Dataset fingerprint and cache key

允许写入：

- `aats/data_platform/research_factory/datasets/gold_bars.py`
- `aats/data_platform/research_factory/specs.py`
- `tests/unit/data_platform/research_factory/test_gold_bars_dataset.py`

必须实现：

- `dataset_fingerprint(dataset_spec, source_watermark, processor_versions)`
- fingerprint 包含 symbol/timeframe/window/dataset_version/source refs/processor versions。

必须测试：

- 任一关键字段变化，fingerprint 改变。
- source_watermark 缺失时拒绝 production cache。
- fingerprint 不包含绝对本地路径。

完成后下一步：RF-P2-01。

### RF-P2-01: Factor DSL 安全表达式模型

允许写入：

- `aats/data_platform/research_factory/features/__init__.py`
- `aats/data_platform/research_factory/features/expressions.py`
- `tests/unit/data_platform/research_factory/test_factor_expressions.py`

必须实现：

- `FactorExpression`
- `parse_factor_expression(expr: str)`
- 白名单字段：`open`, `high`, `low`, `close`, `volume`, `vwap`, `funding_rate`
- 白名单函数：`Ref`, `Return`, `Mean`, `Std`, `ZScore`, `Max`, `Min`, `Rank`, `Delta`

安全要求：

- 不使用 Python `eval` / `exec`。
- 不允许 attribute access。
- 不允许 import、lambda、comprehension、dunder name。
- 未知字段或函数直接拒绝。

必须测试：

- 合法表达式解析成功。
- `__import__("os")` 被拒绝。
- `close.__class__` 被拒绝。
- `Ref(close, -1)` 是否允许必须按 label/feature 语义区分；第一版 feature 禁止未来引用。

完成后下一步：RF-P2-02。

### RF-P2-02: Factor evaluator V1

允许写入：

- `aats/data_platform/research_factory/features/functions.py`
- `aats/data_platform/research_factory/features/expressions.py`
- `tests/unit/data_platform/research_factory/test_factor_expressions.py`

必须实现：

- 对 list/dict rows 的纯 Python evaluator。
- `Ref(field, n)`：n > 0 表示过去 n 根，n = 0 当前，feature 中禁止 n < 0。
- rolling 函数窗口不足时返回 null 并记录 missing reason。

必须测试：

- Return/Mean/Std/Delta 基础数值。
- 窗口不足。
- 除零/缺值处理。
- 不修改输入 rows。

完成后下一步：RF-P2-03。

### RF-P2-03: Baseline benchmark harness V1

允许写入：

- `aats/data_platform/research_factory/benchmarks/__init__.py`
- `aats/data_platform/research_factory/benchmarks/baseline.py`
- `tests/unit/data_platform/research_factory/test_baseline_benchmark.py`

必须实现：

- `run_factor_baseline(dataset, factor_values, label_values, cost_config)`
- IC / Rank IC 计算。
- 简单 long/flat signal 回测的 net return proxy。
- 输出 `MetricsSnapshot`。

必须测试：

- 完美相关时 IC 接近 1。
- 反向相关时 IC 接近 -1。
- fee/slippage/funding 进入 net return。
- 全 null factor 返回 failed metrics，不生成 candidate。

禁止：

- 第一版不要引入 LightGBM/Torch。
- 不做 portfolio optimizer。

完成后下一步：RF-P3-01。

### RF-P3-01: Experiment recorder

允许写入：

- `aats/data_platform/research_factory/experiments/__init__.py`
- `aats/data_platform/research_factory/experiments/recorder.py`
- `tests/unit/data_platform/research_factory/test_experiment_recorder.py`

必须实现：

- `ExperimentRecorder.start(experiment_spec)`
- `ExperimentRecorder.record_metrics(experiment_id, metrics)`
- `ExperimentRecorder.finish(experiment_id, status)`
- `ExperimentRecorder.fail(experiment_id, reason)`

产物结构：

```text
artifacts/research/research_factory/experiments/{experiment_id}/
  experiment_manifest.json
  experiment_spec.json
  metrics_snapshot.json
  failure.json
```

必须测试：

- start 后 manifest status 为 running。
- finish 后 status terminal。
- fail 写 failure.json 且不含 secret。
- 重复 experiment_id 默认拒绝覆盖。
- output_refs 均为相对路径。

完成后下一步：RF-P3-02。

### RF-P3-02: Candidate artifact bridge

允许写入：

- `aats/data_platform/research_factory/experiments/recorder.py`
- `aats/data_platform/research_factory/metrics/gates.py`
- `tests/unit/data_platform/research_factory/test_promotion_gates.py`

必须实现：

- `CandidateArtifact`
- `evaluate_candidate_gate(metrics_snapshot, thresholds)`
- 第一版只生成 candidate JSON，不写 governance DB。

默认 gate：

```text
net_annualized_return > 0
max_drawdown <= configured limit
cost_adjusted_edge_bps_mean > 0
missing critical metrics == false
```

必须测试：

- 成本后收益为负拒绝。
- MDD 超限拒绝。
- 缺 critical metric 拒绝。
- gate pass 只生成 candidate，不生成 active parameter。

完成后下一步：RF-P4-01。

### RF-P4-01: Metrics taxonomy

允许写入：

- `aats/data_platform/research_factory/metrics/__init__.py`
- `aats/data_platform/research_factory/metrics/snapshots.py`
- `tests/unit/data_platform/research_factory/test_metrics_snapshots.py`

必须实现：

- signal metrics、return metrics、cost metrics、execution metrics 分组。
- `missing_reasons` 结构。
- `merge_metric_snapshots`，同名字段冲突时拒绝，除非显式 strategy。

必须测试：

- null metric 必须带 missing reason。
- merge 冲突拒绝。
- JSON serialization 稳定。

完成后下一步：RF-P4-02。

### RF-P4-02: Execution realism metric adapter

允许写入：

- `aats/data_platform/research_factory/metrics/snapshots.py`
- `tests/unit/data_platform/research_factory/test_metrics_snapshots.py`

必须实现：

- 从现有 Phase 4 `execution_cost_summary.json` 结构提取 Research Factory execution metrics。
- 只读文件输入，不改 Phase 4 runner。

必须测试：

- full_fill_ratio 映射。
- slippage mean 映射。
- cost_adjusted_edge mean 映射。
- 缺文件/缺字段返回 missing reason。

完成后下一步：RF-P5-01。

### RF-P5-01: Deterministic research allocation policy V1

允许写入：

- `aats/data_platform/research_factory/allocation/__init__.py`
- `aats/data_platform/research_factory/allocation/policy.py`
- `tests/unit/data_platform/research_factory/test_allocation_policy.py`

必须实现：

- arms：`factor`, `model`, `execution_policy`, `risk_budget`, `regime_classifier`, `validation`
- `ResearchAllocationInput`
- `choose_next_research_action(input) -> AllocationDecision`
- reward 只来自 deterministic metrics，不调用 LLM。

默认权重：

```text
ic: 1.0
rank_ic: 1.0
net_annualized_return: 1.5
information_ratio: 1.0
max_drawdown: -1.5
turnover: -0.5
missing_critical_metrics: -2.0
```

必须测试：

- 高 MDD 方向降权。
- 缺 critical metrics 降权。
- 低样本方向不会永久饿死，第一版可用 epsilon floor。
- decision 包含 reason trace。

完成后下一步：RF-P6-01。

### RF-P6-01: Sandbox proposal schema and guardrails

允许写入：

- `aats/data_platform/research_factory/sandbox/__init__.py`
- `aats/data_platform/research_factory/sandbox/proposal.py`
- `aats/data_platform/research_factory/sandbox/guardrails.py`
- `configs/research_factory/sandbox_policy.json`
- `tests/unit/data_platform/research_factory/test_sandbox_guardrails.py`

必须实现：

- `SandboxProposal`
- proposal types：`factor`, `model`, `parameter`, `execution_policy`, `risk_budget`, `regime_classifier`
- `SandboxPolicy`
- denied env patterns：`.env`, `OKX`, `SECRET`, `TOKEN`, `PASSWORD`, `KEY`
- denied path patterns：`.env*`, `deploy/*`, live credential templates
- `validate_sandbox_proposal(proposal, policy)`

必须测试：

- proposal 试图写 live execution 文件被拒绝。
- proposal 试图读取 `.env` 被拒绝。
- proposal 输出 active parameter 被拒绝。
- proposal 只写 research_factory tmp path 通过。

禁止：

- 不在此阶段执行 LLM 代码。
- 不运行 Docker。
- 不接入 RD-Agent 依赖。

完成后下一步：RF-P6-02。

### RF-P6-02: Sandbox static scan V1

允许写入：

- `aats/data_platform/research_factory/sandbox/guardrails.py`
- `tests/unit/data_platform/research_factory/test_sandbox_guardrails.py`

必须实现：

- `scan_candidate_patch(changed_paths, text_blobs, policy)`
- 检测 secret pattern、forbidden path、forbidden import、network call hint。

必须测试：

- `import os; os.environ` 被拒绝。
- `requests.post("https://www.okx.com")` 被拒绝。
- 修改 `aats/services/execution_engine/recovery.py` 被拒绝。
- 修改 `aats/data_platform/research_factory/features/foo.py` 通过。

完成后下一步：RF-P7-01。

### RF-P7-01: Automation run state and closure report

允许写入：

- `configs/research_factory/baseline_workflow.json`
- `docs/task/rdp_research_factory_refactor_closure_template.md`
- 可选：`aats/data_platform/research_factory/experiments/recorder.py`

必须实现：

- baseline workflow 配置示例。
- closure report 模板，包含所有 task card 勾选项。
- 不新增自动调度器；Codex automation 仍是外部触发者。

必须测试：

- 如果新增 JSON 配置，写 JSON schema 或最小 parser test。

完成后下一步：整体 SOW 验收复查。

---

## 6. 版本推进门

### 6.1 P0 完成门

- RF-P0-01 至 RF-P0-04 全部完成。
- `tests/unit/data_platform/research_factory/test_status.py`
- `tests/unit/data_platform/research_factory/test_specs.py`
- `tests/unit/data_platform/research_factory/test_artifacts.py`
- ruff 通过。
- 没有新增生产依赖。

### 6.2 P1 完成门

- RF-P1-01 至 RF-P1-03 全部完成。
- GoldBarDatasetHandler 不直接连 DB。
- dataset fingerprint deterministic。
- leakage guard 有测试。

### 6.3 P2 完成门

- Factor DSL 不使用 eval/exec。
- feature 禁止未来引用。
- baseline metrics 能明确区分 gross vs net。
- 全 null factor 不生成 candidate。

### 6.4 P3 完成门

- recorder 原子写入。
- failed experiment 也可审计。
- candidate artifact 不写 active parameter。

### 6.5 P4 完成门

- metrics snapshot 包含 signal/return/cost/execution 四类。
- Phase 4 产物 adapter 缺字段时 fail closed。

### 6.6 P5 完成门

- allocation policy deterministic。
- reason trace 可审计。
- 不调用 LLM。

### 6.7 P6 完成门

- sandbox 只有 proposal/schema/scan。
- 不执行生成代码。
- no-secret/no-live-write guardrails 有测试。

### 6.8 P7 完成门

- baseline workflow 和 closure template 存在。
- 总 SOW 的所有 acceptance criteria 可逐项核对。

---

## 7. Drift Control Checklist

后续开发每次结束前必须自查：

- [ ] 本轮是否只完成了一个 task card？
- [ ] 是否触碰了 task card 未允许的文件？
- [ ] 是否引入了 qlib/rdagent 生产依赖？
- [ ] 是否读取或打印了 `.env*`？
- [ ] 是否生成或修改了 active parameter？
- [ ] 是否改了 live execution、OKX adapter、ledger、reconciliation、risk guard？
- [ ] 是否存在未解释的测试失败？
- [ ] 是否报告了下一张未完成 task card？

任何一项答案异常，都不能声明该 task card 完成。
