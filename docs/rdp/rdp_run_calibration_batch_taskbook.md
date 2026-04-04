# `rdp_run_calibration_batch.py` 任务书

## 1. 目标

新增一个**轻量级校准批处理脚本**，用于批量执行一组**研究型/校准型 replay 实验**，自动汇总关键 diagnostics 指标，生成批次级 summary 和 report。

它的定位不是替代现有的 `rdp_run_parameter_scan.py`，而是补一个更适合当前 Phase 2 阶段的工具：

- 适合少量、人工设计的实验组合
- 重点服务：
  - `signal_edge_scale_bps` 校准
  - cost sensitivity 测试
  - threshold sensitivity 测试
- 强调：
  - 快速迭代
  - 容易读
  - 容易复现
  - 自动汇总

---

## 2. 目录位置

脚本文件放在：

```text
scripts/rdp_run_calibration_batch.py
```

如果需要新增内部模块，建议放在：

```text
aats/data_platform/replay/batch/
  calibration_batch.py
  batch_report_builder.py
```

但**最小实现**允许只新增：

- `scripts/rdp_run_calibration_batch.py`

并尽量复用现有 replay / registry / diagnostics / report 模块。

---

## 3. 适用场景

当前脚本主要用于以下场景：

### 3.1 Signal scale 校准
例如批量跑：

- `signal_edge_scale_bps = 8, 10, 12, 15, 20`

### 3.2 成本敏感性测试
例如批量跑：

- `(taker_fee_bps=3, slippage_bps=1)`
- `(taker_fee_bps=5, slippage_bps=2)`
- `(taker_fee_bps=7, slippage_bps=3)`

### 3.3 threshold 敏感性测试
例如批量跑：

- `min_safe_net_edge_bps = 0, 2, 5, 8, 10`
- `min_confirm_ticks = 2, 3, 4, 5`

---

## 4. 不做什么

这个脚本**不**负责：

- 替代 `rdp_run_parameter_scan.py`
- 做复杂参数网格展开
- 做黑盒优化
- 做分布式调度
- 做 dashboard/UI
- 改 replay 主逻辑
- 改 registry 表结构

它只做：

> **读取一组预定义实验 -> 批量跑 replay -> 收集 diagnostics -> 写 batch 级汇总产物**

---

## 5. 输入格式

支持两种输入方式。

### 5.1 方式 A：JSON 文件输入（推荐）
命令行传一个 JSON 文件路径，例如：

```bash
python scripts/rdp_run_calibration_batch.py --batch-file configs/research_batches/independent_scale_calibration.json
```

#### JSON 文件结构

```json
{
  "batch_name": "independent_scale_calibration_2026q1",
  "description": "Calibrate signal edge scale on BTC-USDT-SWAP 15m",
  "family": "independent",
  "symbol": "BTC-USDT-SWAP",
  "timeframe": "15m",
  "dataset_version": "v1.0",
  "start": "2026-03-31",
  "end": "2026-04-02",
  "experiments": [
    {
      "label": "scale_8",
      "params": {
        "signal_edge_scale_bps": 8
      }
    },
    {
      "label": "scale_10",
      "params": {
        "signal_edge_scale_bps": 10
      }
    },
    {
      "label": "scale_15",
      "params": {
        "signal_edge_scale_bps": 15
      }
    }
  ]
}
```

#### 设计原则
公共字段放顶层：
- `family`
- `symbol`
- `timeframe`
- `dataset_version`
- `start`
- `end`

每个实验只写：
- `label`
- `params`

这样最简洁。

### 5.2 方式 B：内置预设 batch 名称
命令行传一个预设 batch 名称，例如：

```bash
python scripts/rdp_run_calibration_batch.py --preset independent_scale_15m
```

脚本内部可暂时 hardcode 2~3 个常用 batch 模板，例如：

- `independent_scale_15m`
- `independent_cost_15m`
- `independent_confirm_ticks_15m`

#### 说明
这是辅助能力，不是必须。  
**最小实现优先支持 JSON 文件方式**。

---

## 6. 命令行参数

建议支持以下 CLI 参数。

### 必需参数（二选一）
- `--batch-file <path>`
- `--preset <name>`

### 可选参数
- `--artifact-root <path>`
  - 默认：`artifacts/research/calibration_batches`
- `--ensure-schema`
  - 显式执行 migration，默认不执行
- `--stop-on-error`
  - 默认 false
  - 若 true，某个实验失败则整批立即停止
- `--print-summary`
  - 默认 true
  - 批次结束后打印关键汇总

---

## 7. 输出格式

每次 batch 运行都要生成一个独立目录。

### 7.1 目录结构

建议输出到：

```text
artifacts/research/calibration_batches/<batch_run_id>/
  batch_spec.json
  batch_summary.csv
  batch_summary.json
  batch_report.md
  failed_experiments.json
  experiment_refs.json
```

### 7.2 每个文件的职责

#### `batch_spec.json`
保存本次 batch 的输入规格原文，便于复现。

#### `batch_summary.csv`
用于人工快速比较。

每行一组 experiment，至少包含：

- `label`
- `experiment_id`
- `status`
- `opening_count`
- `blocked_count`
- `selectable_ratio`
- `execution_compatible_ratio`
- `mean_signal_edge_proxy_bps`
- `mean_funding_adjustment_bps`
- `mean_cost_bps`
- `mean_expected_edge_bps`
- `positive_edge_ratio`
- `top_blocking_reason_1`
- `top_blocking_reason_2`
- `result_path`
- `report_path`

#### `batch_summary.json`
机器可读版 summary。建议包含：
- batch metadata
- per-experiment summaries
- aggregate comparison
- failures

#### `batch_report.md`
给人看的批次级报告。

至少包含：
- batch 基本信息
- 实验清单
- 每组参数结果表
- 关键变化趋势
- 失败实验列表
- 初步结论

#### `failed_experiments.json`
只记录失败实验，格式例如：

```json
[
  {
    "label": "scale_20",
    "params": {
      "signal_edge_scale_bps": 20
    },
    "error": "...."
  }
]
```

#### `experiment_refs.json`
记录本批次中每个实验对应的 registry 引用。

---

## 8. 需要复用的现有模块

### 8.1 Replay 参数与上下文
复用：
- `aats.data_platform.replay.core.replay_context.ReplayParameterOverrides`

### 8.2 Replay 主执行逻辑
复用：
- `aats.data_platform.replay.core.replay_runner.run_replay`

### 8.3 Adapter
根据 family 复用：
- `IndependentReplayAdapter`
- `DirectionalReplayAdapter`

### 8.4 Diagnostics
复用：
- `aats.data_platform.replay.diagnostics.replay_diagnostics.compute_diagnostics`

### 8.5 Registry
尽量复用：
- `create_experiment`
- `mark_experiment_running`
- `mark_experiment_succeeded`
- `mark_experiment_failed`
- `upsert_experiment_summary`

### 8.6 单实验结果写出
优先复用：
- `write_decisions_csv`
- `write_summary_json`
- `build_experiment_report`

也就是说，**每个 batch 内部实验的产物格式应与 `rdp_run_replay.py` 一致**。

---

## 9. 脚本主流程

建议主流程如下：

### 9.1 解析输入
- 读 `--batch-file`
- 校验 JSON 结构
- 展开公共字段 + 每个实验 params

### 9.2 可选执行 schema 检查
- 若 `--ensure-schema` 则显式跑 migration
- 默认不跑

### 9.3 为每个实验执行
1. 构造 `ReplayParameterOverrides`
2. 创建 adapter
3. 注册 experiment
4. 运行 replay
5. 写单实验 artifacts
6. 计算 diagnostics
7. 生成单实验 report
8. 更新 experiment registry
9. 收集 summary 行

### 9.4 对整个 batch 执行
1. 汇总所有 experiment 的关键指标
2. 写 `batch_summary.csv`
3. 写 `batch_summary.json`
4. 写 `failed_experiments.json`
5. 写 `experiment_refs.json`
6. 生成 `batch_report.md`

---

## 10. Batch Report 的最小内容

`batch_report.md` 至少应包含：

### 10.1 Header
- batch_name
- description
- family
- symbol
- timeframe
- dataset_version
- window

### 10.2 实验结果表
一个 Markdown 表，至少列：
- label
- opening_count
- blocked_count
- exec_compatible_ratio
- mean_expected_edge_bps
- positive_edge_ratio
- top_blocking_reason_1

### 10.3 失败实验
列出失败 label + error

### 10.4 初步发现
可以是简单规则化总结，例如：
- 随着 `signal_edge_scale_bps` 提高，opening_count 上升
- 随着 `min_confirm_ticks` 提高，blocked_count 上升
- 某参数超过某值后 opening 清零

---

## 11. 最小实现范围

第一版**只要求做到最小可用**，不要过度设计。

### 第一版必须支持
1. `--batch-file`
2. 读取 JSON 批次定义
3. 只支持一个 family / symbol / timeframe / window 的公共配置
4. 每个 experiment 只覆盖 `params`
5. 逐个运行 replay
6. 生成 batch summary 和 report
7. 失败实验单独记录
8. 单实验产物复用现有路径和格式

### 第一版不要求
- 多 family 混跑
- 多 symbol 混跑
- 多 timeframe 混跑
- 并发执行
- 新建 batch registry 表
- Web UI
- 自动图表
- 超复杂 findings 生成

---

## 12. 失败处理策略

默认行为建议是：

- 单个 experiment 失败，不中断整批
- 记录到 `failed_experiments.json`
- batch 继续运行其余实验

如果用户传了：

```bash
--stop-on-error
```

则：
- 第一个失败后立即停止 batch

---

## 13. 日志要求

脚本应清晰打印：

- batch 开始
- 当前 experiment 序号 / 总数
- 当前 label
- 当前 params
- 实验成功或失败
- 最终 batch summary 路径

示例风格：

```text
Starting calibration batch: independent_scale_calibration_2026q1
[1/5] Running scale_8 ...
[2/5] Running scale_10 ...
...
Batch completed: 5 succeeded, 0 failed
Summary: artifacts/research/calibration_batches/<batch_run_id>/batch_summary.csv
```

---

## 14. 验收标准

### 验收 batch

```json
{
  "batch_name": "independent_scale_calibration_15m",
  "description": "Calibrate signal edge scale",
  "family": "independent",
  "symbol": "BTC-USDT-SWAP",
  "timeframe": "15m",
  "dataset_version": "v1.0",
  "start": "2026-03-31",
  "end": "2026-04-02",
  "experiments": [
    {"label": "scale_10", "params": {"signal_edge_scale_bps": 10}},
    {"label": "scale_15", "params": {"signal_edge_scale_bps": 15}},
    {"label": "scale_20", "params": {"signal_edge_scale_bps": 20}}
  ]
}
```

### 通过标准
1. 三组实验都能跑完
2. 每组都生成单实验 artifact
3. `batch_summary.csv` 正确写出
4. `batch_report.md` 正确写出
5. `experiment_refs.json` 正确写出
6. 如果有失败，`failed_experiments.json` 正确写出
7. summary 里包含关键 diagnostics 指标

---

## 15. 推荐的第一批 batch 模板

建议顺手准备 3 份 JSON 模板，放到：

```text
configs/research_batches/
```

### 15.1 `independent_scale_calibration_15m.json`
扫：
- `signal_edge_scale_bps = 8,10,12,15,20`

### 15.2 `independent_cost_sensitivity_15m.json`
扫：
- `(taker_fee_bps, slippage_bps)`
- `(3,1)`
- `(5,2)`
- `(7,3)`

### 15.3 `independent_confirm_ticks_15m.json`
扫：
- `min_confirm_ticks = 2,3,4,5`

---

## 16. 实现约束

### 16.1 不允许复制 `rdp_run_replay.py` 全部逻辑
应尽可能抽取公共函数或复用现有函数。

### 16.2 不允许重写 replay / diagnostics / registry 主链
批量脚本是 orchestration 层，不是新主链。

### 16.3 不允许引入新的复杂 batch 表结构
第一版不建 batch registry 表。

### 16.4 不允许在第一版做并发执行
先串行，确保稳定和清晰。

---

## 17. 一句话总结

`rdp_run_calibration_batch.py` 的职责是：

> **读取一组预定义的研究实验配置，复用现有 replay 主链批量运行，自动汇总 diagnostics，生成批次级 summary 和 report，用于 Phase 2 的 signal/cost/threshold 校准。**
