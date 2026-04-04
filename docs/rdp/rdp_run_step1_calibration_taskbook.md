# `rdp_run_step1_calibration.py` 任务书

## 1. 目标

实现一个 **Step 1 Calibration Orchestrator**，把当前 Phase 2-A 的第一轮参数校准流程固化成**标准化自动流程**。

该脚本不是新的 replay 引擎，也不是新的 parameter scan 平台。  
它的职责是：

> **自动运行 Step 1 规定的 3 个 calibration batch，汇总结果，应用规则化判断，生成 Step 1 结论文档。**

---

## 2. 背景

当前系统已经具备：

- `rdp_run_replay.py`：单实验 replay
- `rdp_run_parameter_scan.py`：正式 parameter scan
- `rdp_run_calibration_batch.py`：轻量批处理校准脚本
- replay / registry / diagnostics / report 主链
- 3 个 Step 1 批处理模板：
  - `independent_scale_calibration_15m.json`
  - `independent_cost_sensitivity_15m.json`
  - `independent_confirm_ticks_15m.json`

当前问题不是能力缺失，而是：

- Step 1 的执行流程仍然需要人工串联
- 结果需要人工汇总
- 结论需要人工整理
- 缺少“Step 1 是否完成”的统一产物

因此，需要新增一个 orchestrator，把 Step 1 变成**可重复执行、可复现、可交付**的研究流程。

---

## 3. 文件位置

新增脚本：

```text
scripts/rdp_run_step1_calibration.py
```

如果需要拆内部模块，建议放到：

```text
aats/data_platform/replay/calibration/
  round_runner.py
  round_summary_builder.py
  recommendation_engine.py
  conclusion_report_builder.py
```

但第一版允许只新增一个脚本：
- `scripts/rdp_run_step1_calibration.py`

前提是：尽量复用现有模块，不复制主链逻辑。

---

## 4. Step 1 的固定范围

第一版 **只支持固定范围**：

- `family = independent`
- `symbol = BTC-USDT-SWAP`
- `timeframe = 15m`

第一版 **不支持**：
- `directional`
- `1H`
- 多 symbol
- 多 family

这些属于 Step 2 以后再扩展。

---

## 5. 固定输入

`rdp_run_step1_calibration.py` 默认运行以下 3 个 batch：

1. `configs/research_batches/independent_scale_calibration_15m.json`
2. `configs/research_batches/independent_cost_sensitivity_15m.json`
3. `configs/research_batches/independent_confirm_ticks_15m.json`

### 第一版原则
- 这 3 个 batch 名称和路径固定
- 不要先做通用 batch 编排平台
- Step 1 的目的是把**已确定的研究流程自动化**

---

## 6. 输出目录

建议输出到：

```text
artifacts/research/calibration_rounds/<round_id>/
```

每次 Step 1 运行生成一个独立 round 目录。

---

## 7. 输出产物

每次 Step 1 运行至少生成以下文件：

```text
artifacts/research/calibration_rounds/<round_id>/
  round_manifest.json
  round_summary.csv
  round_summary.json
  parameter_recommendations.json
  phase2_step1_calibration_conclusion.md
```

### 7.1 `round_manifest.json`
记录本轮 Step 1 的元信息：

- `round_id`
- `started_at`
- `finished_at`
- `family`
- `symbol`
- `timeframe`
- `batch_runs`
- `status`

### 7.2 `round_summary.csv`
把 3 个 batch 的所有实验结果汇总成一张总表。

### 7.3 `round_summary.json`
机器可读版汇总。

### 7.4 `parameter_recommendations.json`
规则化参数建议输出。

### 7.5 `phase2_step1_calibration_conclusion.md`
最终面向人的结论文档。

---

## 8. round_summary 的字段要求

`round_summary.csv` / `round_summary.json` 中，每条实验记录至少包含：

- `batch_name`
- `label`
- `params`
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
- `summary_path`
- `report_path`

这张表是 Step 1 的核心底稿。

---

## 9. 需要复用的现有模块

优先复用，不要复制逻辑。

### 9.1 复用 batch runner
优先复用：
- `scripts/rdp_run_calibration_batch.py`

推荐方式不是 shell 掉 3 次 CLI，而是尽量抽公共函数或模块调用。  
如果当前脚本结构不方便直接 import，可允许第一版先以子进程方式调用，但后续最好模块化。

### 9.2 复用 batch 产物
Step 1 orchestrator 不自己重算 replay，也不自己重算 diagnostics。  
它只负责：
- 调用 batch runner
- 读取 `batch_summary.json` / `batch_summary.csv`
- 进行跨 batch 汇总与规则判断

### 9.3 不允许重写主链
不得重写：
- replay loop
- diagnostics
- registry
- experiment report builder

---

## 10. Step 1 主流程

建议主流程如下：

### 10.1 创建 `round_id`
格式例如：

```text
20260404_153000_ab12cd34
```

### 10.2 运行 3 个固定 batch
按顺序执行：

1. scale calibration
2. cost sensitivity
3. confirm ticks sensitivity

### 10.3 收集每个 batch 的输出路径
至少记录：
- batch artifact dir
- batch_summary.csv
- batch_summary.json
- batch_report.md

### 10.4 构建 `round_summary`
把 3 个 batch 的实验结果拼成一张大表。

### 10.5 运行规则化判断
根据预定义规则生成：
- 推荐默认参数
- 关键观察
- 风险提示
- 信心等级

### 10.6 生成结论文档
输出：
- `phase2_step1_calibration_conclusion.md`

---

## 11. 规则化判断逻辑

这是 Step 1 的核心。

### 11.1 `signal_edge_scale_bps`
观察：
- `opening_count`
- `mean_expected_edge_bps`
- `positive_edge_ratio`

规则建议：

- 若 scale 增大时 opening_count 与 mean_expected_edge_bps 同向改善，则说明默认 scale 偏低
- 若某个 scale 开始导致 opening_count 异常放大，则说明过高
- 优先选择：
  - 提升明显
  - 但未出现异常放大
  - 且 blocking 结构未明显恶化
的 scale 作为推荐值

输出字段建议：
- `recommended_signal_edge_scale_bps`
- `signal_edge_scale_confidence`
- `signal_edge_scale_reason`

### 11.2 cost model
观察：
- `mean_cost_bps`
- `opening_count`
- `mean_expected_edge_bps`
- `top_blocking_reason_1`

规则建议：

- 若 cost 增大后 opening_count 明显下降，说明 edge 对 cost 敏感
- 若稍微提高成本就导致大量实验不触发，说明当前默认 net edge 较脆弱
- 若 `(5,2)` 在 opening 与 net edge 之间最平衡，则可保持默认

输出字段建议：
- `recommended_taker_fee_bps`
- `recommended_slippage_bps`
- `cost_model_confidence`
- `cost_model_reason`

### 11.3 `min_confirm_ticks`
观察：
- `opening_count`
- `blocked_count`
- `top_blocking_reason_1`

规则建议：

- 若 2 太松、4/5 太严，则推荐 3
- 若 3 与 4 差别不大，优先选更保守的一侧，但要避免 opening 接近 0
- 若高 ticks 明显把 top blocking reason 推向 `score_not_stable`，说明门槛过严

输出字段建议：
- `recommended_min_confirm_ticks`
- `min_confirm_ticks_confidence`
- `min_confirm_ticks_reason`

### 11.4 `min_safe_net_edge_bps`
第一版允许两种来源：

#### 方案 A：来自额外已有实验结果
如果后续已有 dedicated batch，可直接纳入 Step 1。

#### 方案 B：如果当前没有 dedicated batch
Step 1 先在结论文档里标记为：

- `pending_additional_validation`
- 或使用人工已有结果作为外部观察输入

第一版不要求强行自动推荐 `min_safe_net_edge_bps`，但要在结论文档里明确写出状态。

输出字段建议：
- `recommended_min_safe_net_edge_bps`
- `min_safe_net_edge_bps_confidence`
- `min_safe_net_edge_bps_reason`

---

## 12. parameter_recommendations.json 格式

建议格式如下：

```json
{
  "round_id": "20260404_153000_ab12cd34",
  "scope": {
    "family": "independent",
    "symbol": "BTC-USDT-SWAP",
    "timeframe": "15m"
  },
  "recommendations": {
    "signal_edge_scale_bps": {
      "value": 15,
      "confidence": "medium",
      "reason": "opening_count and mean_expected_edge_bps improved from 10 to 15 without abnormal explosion"
    },
    "taker_fee_bps": {
      "value": 5,
      "confidence": "medium",
      "reason": "cost sensitivity indicates 5bps remains balanced between overly optimistic and overly punitive assumptions"
    },
    "slippage_bps": {
      "value": 2,
      "confidence": "medium",
      "reason": "2bps keeps cost model realistic while preserving discriminative edge response"
    },
    "min_confirm_ticks": {
      "value": 3,
      "confidence": "high",
      "reason": "2 appears too permissive while 4/5 sharply reduce openings and increase score_not_stable blocks"
    },
    "min_safe_net_edge_bps": {
      "value": null,
      "confidence": "low",
      "reason": "requires dedicated validation batch or broader window evidence"
    }
  }
}
```

---

## 13. 结论文档结构

生成：

```text
phase2_step1_calibration_conclusion.md
```

建议结构：

### 13.1 标题
- Step 1 Calibration Conclusion

### 13.2 范围
- family
- symbol
- timeframe
- window

### 13.3 执行批次
- scale calibration
- cost sensitivity
- confirm ticks sensitivity

### 13.4 关键观察
- 哪个参数最敏感
- 哪个参数会让 opening 明显下降
- 当前 edge 是否偏弱
- 当前 cost 是否偏强/偏弱

### 13.5 默认参数建议
- `signal_edge_scale_bps`
- `taker_fee_bps`
- `slippage_bps`
- `min_confirm_ticks`
- `min_safe_net_edge_bps`

### 13.6 信心等级
- high / medium / low

### 13.7 未解决问题
- 例如 `min_safe_net_edge_bps` 仍需额外验证

### 13.8 下一步
- 扩到 `1H`
- 扩到 `directional`

---

## 14. CLI 设计

建议支持：

```bash
python scripts/rdp_run_step1_calibration.py
```

以及可选参数：

- `--artifact-root <path>`
- `--ensure-schema`
- `--stop-on-error`
- `--no-print-summary`

第一版不需要用户传 family/symbol/timeframe。  
因为 Step 1 范围固定。

---

## 15. 最小实现范围

第一版只要求：

1. 自动运行 3 个固定 batch
2. 自动收集 3 个 batch 的 summary
3. 自动生成 round 级汇总
4. 自动生成 parameter recommendations
5. 自动生成 Step 1 calibration conclusion Markdown

第一版不要求：

- 通用多 family
- 通用多 symbol
- 通用多 timeframe
- 并发执行
- 可视化图表
- Web UI
- 新数据库表
- 复杂统计模型

---

## 16. 实现约束

### 16.1 不允许重写 replay 主链
禁止复制：
- replay runner
- diagnostics
- report builder
- registry

### 16.2 不允许引入新数据库表
Step 1 orchestrator 是工作流层，不是 schema 层。

### 16.3 不允许过度泛化
第一版只服务 Step 1。

### 16.4 不允许把“自动结论”写成黑盒 AI 逻辑
第一版必须是**规则化判断**，透明、可解释。

---

## 17. 验收标准

### 通过条件
1. 能顺序跑完 3 个 calibration batch
2. 能生成：
   - `round_manifest.json`
   - `round_summary.csv`
   - `round_summary.json`
   - `parameter_recommendations.json`
   - `phase2_step1_calibration_conclusion.md`
3. `round_summary` 包含 3 个 batch 的所有实验
4. recommendation 文件里有明确的默认参数建议或明确标注待定
5. 结论文档可直接供人阅读

---

## 18. 一句话总结

`rdp_run_step1_calibration.py` 的职责是：

> **把 Step 1 的三组 calibration batch 自动串起来，自动汇总，自动做规则化判断，并输出一份可交付的参数校准结论。**
