# Phase 2 Step 2 任务书（正式研究闭环）

## 1. 目标

在 Step 1 完成 `independent / BTC-USDT-SWAP / 15m` 的第一轮参数校准后，进入 **Step 2：正式研究闭环**。

Step 2 的目标不是继续修工具，而是把当前能力推进到：

> **可以对 independent 与 directional，在 15m / 1H 上做结构化、可复现、可比较的正式研究。**

Step 2 要解决的问题：

1. 把 Step 1 的结论从 `15m / independent` 扩到更完整的研究范围
2. 让 `directional` 进入同一研究框架
3. 从 calibration 进入正式 parameter scan
4. 形成可交付的比较报告，而不只是单批次结果

---

## 2. Step 2 的范围

### 2.1 固定范围
第一版 Step 2 只支持：

- `symbol = BTC-USDT-SWAP`
- `timeframes = 15m, 1H`
- `families = independent, directional`

### 2.2 暂不包含
Step 2 第一版不包含：

- ETH
- spot
- 多 symbol 并行
- live attribution
- trades / orderbook / execution realism

---

## 3. Step 2 的核心目标

Step 2 结束时，必须能回答：

1. 在 `15m` 上，independent 和 directional 的结构差异是什么
2. 在 `1H` 上，independent 和 directional 的结构差异是什么
3. Step 1 推荐参数在 `1H` 上是否仍然合理
4. 哪些参数适合进入正式默认值候选
5. 哪些参数仍需要更多窗口或更多市场验证

---

## 4. Step 2 的核心交付物

Step 2 至少产出以下交付物：

```text
artifacts/research/step2_rounds/<round_id>/
  round_manifest.json
  family_timeframe_summary.csv
  family_timeframe_summary.json
  scan_comparison_summary.csv
  scan_comparison_summary.json
  parameter_candidates.json
  phase2_step2_research_conclusion.md
```

其中：

- `family_timeframe_summary.*`：按 family + timeframe 汇总
- `scan_comparison_summary.*`：正式 parameter scan 结果
- `parameter_candidates.json`：默认参数候选
- `phase2_step2_research_conclusion.md`：最终结论文档

---

## 5. Step 2 分成 4 个子阶段

### Step 2-A：扩到 `1H`
目标：
- 让 Step 1 的校准流程能在 `1H` 上跑通
- 判断 `15m` 的推荐参数是否迁移到 `1H`

交付：
- `independent / 1H` 的 calibration round
- 1H 初步参数建议

---

### Step 2-B：接入 `directional`
目标：
- 在 `15m` 上对 directional 完成第一轮 calibration
- 判断 directional 的 edge / cost / confirm sensitivity

交付：
- `directional / 15m` calibration round
- directional 初步参数建议

---

### Step 2-C：正式 parameter scan
目标：
- 在固定 family + timeframe 下做正式 scan
- 不再只是 calibration batch，而是更完整的组合扫描

交付：
- independent 15m 正式 scan
- independent 1H 正式 scan
- directional 15m 正式 scan
- directional 1H 正式 scan

---

### Step 2-D：比较与收口
目标：
- 做 family 间、timeframe 间比较
- 形成默认参数候选与待验证项清单

交付：
- `parameter_candidates.json`
- `phase2_step2_research_conclusion.md`

---

## 6. Step 2 需要新增的脚本

建议新增：

```text
scripts/rdp_run_step2_research.py
```

如果需要拆模块，建议放到：

```text
aats/data_platform/replay/research/
  step2_round_runner.py
  family_timeframe_summary_builder.py
  scan_result_aggregator.py
  candidate_selector.py
  step2_conclusion_builder.py
```

第一版允许只新增：
- `scripts/rdp_run_step2_research.py`

---

## 7. Step 2 需要复用的现有能力

必须复用，不允许重写主链。

### 7.1 复用的脚本/流程
- `rdp_run_calibration_batch.py`
- `rdp_run_step1_calibration.py`
- `rdp_run_parameter_scan.py`

### 7.2 复用的模块
- replay runner
- adapters
- diagnostics
- experiment registry
- markdown report builder

### 7.3 不允许重写
- replay loop
- diagnostics 计算逻辑
- experiment registry schema
- Step 1 orchestration 主逻辑

---

## 8. Step 2 的固定执行顺序

Step 2 不允许乱序推进，必须按下面顺序。

### 8.1 先做 `independent / 1H`
原因：
- 与 Step 1 最接近
- 风险最低
- 能最快验证 timeframe 迁移性

### 8.2 再做 `directional / 15m`
原因：
- family 扩展比 timeframe 扩展风险更高
- 先在你更熟悉的 `15m` 上验证 directional

### 8.3 然后做 `directional / 1H`
原因：
- 这是 Step 2 才真正完成 family + timeframe 双扩展的标志

### 8.4 最后才做正式 scan 汇总
原因：
- 如果前面的 calibration 还没收口，scan 只会产生更多噪音结果

---

## 9. Step 2 的输入配置

建议新增一组 Step 2 配置文件，放到：

```text
configs/research_rounds/
```

建议至少包含：

```text
configs/research_rounds/
  step2_independent_1h.json
  step2_directional_15m.json
  step2_directional_1h.json
  step2_formal_scan_matrix.json
```

---

## 10. Step 2-A：`independent / 1H` 任务

### 10.1 目标
验证 Step 1 推荐参数在 `1H` 上是否仍然合理。

### 10.2 要做的事
1. 复制 Step 1 的 3 个 calibration batch 模板到 `1H`
2. 运行：
   - scale calibration
   - cost sensitivity
   - confirm ticks sensitivity
3. 生成一份 `independent / 1H` calibration conclusion
4. 和 `independent / 15m` 做差异比较

### 10.3 输出
- `independent_1h_round_summary.csv`
- `independent_1h_recommendations.json`
- `independent_1h_conclusion.md`

### 10.4 验收标准
- 3 个 batch 跑通
- 推荐参数产生
- 能说清楚 `15m` 与 `1H` 是否需要不同默认值

---

## 11. Step 2-B：`directional / 15m` 任务

### 11.1 目标
让 directional 进入与 independent 同等的研究流程。

### 11.2 要做的事
1. 新增 directional 的 calibration batch 模板：
   - scale / weighting calibration
   - cost sensitivity
   - confirm / threshold sensitivity
2. 跑 `directional / 15m` calibration round
3. 产出 directional 推荐参数
4. 与 independent / 15m 做结构比较

### 11.3 关键点
directional 不是复制 independent 的参数空间。  
需要针对 directional 当前已有参数暴露，选最小必要集合。

建议第一版关注：
- trend weighting
- return clamp / signal weighting
- confirm ticks
- cost model

### 11.4 输出
- `directional_15m_round_summary.csv`
- `directional_15m_recommendations.json`
- `directional_15m_conclusion.md`

### 11.5 验收标准
- directional / 15m 可完整跑通 calibration
- recommendation 文件生成
- 与 independent 的对比至少能解释 opening / edge / blocking 结构差异

---

## 12. Step 2-C：`directional / 1H` 任务

### 12.1 目标
补全 Step 2 的 family + timeframe 矩阵。

### 12.2 要做的事
- 复用 directional / 15m 的 batch 模板，适配到 1H
- 跑 directional / 1H calibration round
- 生成 directional / 1H 推荐参数

### 12.3 输出
- `directional_1h_round_summary.csv`
- `directional_1h_recommendations.json`
- `directional_1h_conclusion.md`

### 12.4 验收标准
- directional / 1H 可运行
- 能与 directional / 15m 做差异比较

---

## 13. Step 2-D：正式 parameter scan 任务

### 13.1 目标
把 calibration 的“局部敏感性研究”推进到“正式候选参数比较”。

### 13.2 family/timeframe 组合
至少做这 4 组：

- independent / 15m
- independent / 1H
- directional / 15m
- directional / 1H

### 13.3 输出
每组都要有：

- scan summary
- comparison report
- top candidates

### 13.4 要关注的指标
- opening_count
- blocked_count
- execution_compatible_ratio
- mean_expected_edge_bps
- positive_edge_ratio
- top_blocking_reason_1
- top_blocking_reason_2

### 13.5 验收标准
- 4 组 scan 能跑通至少 1 轮
- 结果能汇总成统一比较表

---

## 14. Step 2 的推荐输出：`parameter_candidates.json`

建议格式：

```json
{
  "scope": {
    "symbol": "BTC-USDT-SWAP"
  },
  "candidates": {
    "independent_15m": {
      "signal_edge_scale_bps": 15,
      "taker_fee_bps": 5,
      "slippage_bps": 2,
      "min_confirm_ticks": 3,
      "min_safe_net_edge_bps": null
    },
    "independent_1h": {
      "signal_edge_scale_bps": 12,
      "taker_fee_bps": 5,
      "slippage_bps": 2,
      "min_confirm_ticks": 2,
      "min_safe_net_edge_bps": null
    },
    "directional_15m": {
      "trend_weight": 0.7,
      "return_clamp_bps": 15,
      "taker_fee_bps": 5,
      "slippage_bps": 2,
      "min_confirm_ticks": 3
    },
    "directional_1h": {
      "trend_weight": 0.8,
      "return_clamp_bps": 12,
      "taker_fee_bps": 5,
      "slippage_bps": 2,
      "min_confirm_ticks": 2
    }
  },
  "pending_validation": [
    "min_safe_net_edge_bps across broader windows",
    "cross-window stability for directional on 1H"
  ]
}
```

---

## 15. 最终结论文档：`phase2_step2_research_conclusion.md`

建议结构：

### 15.1 Scope
- families
- timeframes
- symbol
- windows

### 15.2 What was executed
- which calibration rounds
- which scans

### 15.3 Key comparisons
- independent vs directional on 15m
- independent vs directional on 1H
- 15m vs 1H within independent
- 15m vs 1H within directional

### 15.4 Parameter candidates
- by family/timeframe

### 15.5 Stable conclusions
- 哪些结论信心高

### 15.6 Pending items
- 哪些参数仍需更多窗口或更多市场验证

### 15.7 Next step
- Step 3: live attribution

---

## 16. Step 2 的最小实现范围

第一版只要求：

1. 支持 `independent / 1H`
2. 支持 `directional / 15m`
3. 支持 `directional / 1H`
4. 生成统一 round summary
5. 生成 parameter candidates
6. 生成 Step 2 结论文档

第一版不要求：

- 多 symbol
- 多 market
- dashboard
- 图表可视化
- execution realism
- 新数据库表

---

## 17. 实现约束

### 17.1 不允许重写 replay 主链
禁止复制：
- replay runner
- diagnostics
- registry
- report builder

### 17.2 不允许引入新数据库表
Step 2 仍然是 orchestration / research layer，不是 schema phase。

### 17.3 不允许过早扩展到 ETH / spot / live
Step 2 只做：
- BTC-USDT-SWAP
- 15m / 1H
- independent / directional

### 17.4 不允许跳过 calibration 直接做全部正式 scan
必须先完成：
- independent / 1H calibration
- directional / 15m calibration
- directional / 1H calibration

---

## 18. 验收标准

Step 2 通过条件：

1. `independent / 1H` calibration 完成
2. `directional / 15m` calibration 完成
3. `directional / 1H` calibration 完成
4. 4 组 family/timeframe 至少各有 1 轮正式 scan
5. 生成：
   - `family_timeframe_summary.csv`
   - `scan_comparison_summary.csv`
   - `parameter_candidates.json`
   - `phase2_step2_research_conclusion.md`
6. 能明确给出 family/timeframe 级别的参数候选或待验证项

---

## 19. 一句话总结

Step 2 的职责是：

> **把 Step 1 的独立单范围参数校准，推进成覆盖 independent + directional、15m + 1H 的正式研究闭环，并输出可比较、可交付的参数候选结论。**
