# Phase 6 任务书（Closed-Loop Decision System / 研究结果回灌生产决策）

## 1. 目标

在 Phase 1 ~ Phase 5 已经完成数据底座、研究、归因、execution realism、以及平台治理之后，进入 **Phase 6：Closed-Loop Decision System**。

Phase 6 的目标不是再增加一个新的分析模块，而是把前面所有 phase 的结果真正整合起来，形成：

> **研究结果 → 参数候选 → 证据整合 → 上线建议 / 降权建议 / 回滚建议**

也就是说，Phase 6 要解决的问题是：

1. 哪一套参数值得进入下一轮 live 测试
2. 哪个 family / timeframe 应该保持 active、降权、暂停
3. 当前研究结果是否足够支持一次参数升级
4. 当前 live 表现不佳时，应该优先：
   - 调参数
   - 调 risk
   - 调 execution 假设
   - 暂停某个 family/timeframe
5. 如何把 replay、attribution、execution realism 三条证据链合成一个统一决策视图

---

## 2. Phase 6 的定位

### 2.1 它不是自动改 live
Phase 6 第一版**不是**：
- 自动把参数写进生产系统
- 自动重启策略
- 自动上线新模型

Phase 6 第一版的定位是：

> **生成高质量、可审查、可追溯的“生产决策建议”。**

### 2.2 它是证据整合层
前面各 phase 分别回答：

- Phase 2：历史研究里什么参数更合理
- Phase 3：live 为什么没下单
- Phase 4：理论机会真实市场里能不能做
- Phase 5：这些结论怎么治理和追踪

Phase 6 则回答：

> **综合这些证据，下一步 production 应该怎么动。**

---

## 3. Phase 6 的核心目标

Phase 6 结束时，平台必须能系统性回答这 4 类问题：

1. **参数升级建议**  
   当前是否存在一套比 active parameter set 更好的候选参数？

2. **family/timeframe 状态建议**  
   某个 family/timeframe 是应该：
   - keep active
   - lower priority
   - pause
   - require review

3. **失败主因归属建议**  
   当前问题更像是：
   - strategy quality problem
   - attribution / permissions problem
   - execution realism problem
   - governance / stale result problem

4. **上线前证据充足性**  
   当前证据是否足以支持一次“进入下一轮 live test”的建议？

---

## 4. Phase 6 的核心交付物

Phase 6 至少产出：

```text
artifacts/decision_system/
  active_decision_registry.json
  recommendation_registry.json
  evidence_bundle_index.json

artifacts/decision_rounds/<round_id>/
  round_manifest.json
  evidence_summary.json
  parameter_upgrade_candidates.json
  family_timeframe_decisions.json
  promotion_readiness_report.json
  phase6_closed_loop_decision_conclusion.md
```

### 文件说明

#### `active_decision_registry.json`
记录当前处于 active / review / paused 状态的 family-timeframe 决策对象。

#### `recommendation_registry.json`
记录历史所有上线建议、降权建议、暂停建议、回滚建议。

#### `evidence_bundle_index.json`
记录 recommendation 使用了哪些 evidence bundle。

#### `evidence_summary.json`
汇总本轮使用的所有研究、归因、execution realism、governance 证据。

#### `parameter_upgrade_candidates.json`
输出当前最值得进入下一轮 live test 的参数候选。

#### `family_timeframe_decisions.json`
输出 family/timeframe 级别的状态建议。

#### `promotion_readiness_report.json`
回答“当前是否可以建议进入下一轮 production test”。

#### `phase6_closed_loop_decision_conclusion.md`
最终结论文档。

---

## 5. Phase 6 分成 5 个子阶段

### Phase 6-A：Evidence Bundle 统一化
目标：
- 把来自 Phase 2 / 3 / 4 / 5 的结果统一整理成 evidence bundle

### Phase 6-B：参数升级候选选择
目标：
- 从 parameter candidates 中筛出最值得推荐的一组

### Phase 6-C：family/timeframe 状态决策
目标：
- 对每个 family/timeframe 给出 keep / lower / pause / review 建议

### Phase 6-D：Promotion Readiness 评估
目标：
- 回答“是否建议进入下一轮 live test”

### Phase 6-E：Recommendation Registry 与闭环文档
目标：
- 把建议变成受治理对象，进入 recommendation registry

---

## 6. 建议新增的脚本

建议新增：

```text
scripts/rdp_run_decision_round.py
scripts/rdp_select_parameter_upgrade.py
scripts/rdp_evaluate_promotion_readiness.py
scripts/rdp_update_decision_registry.py
```

### 脚本职责

#### `rdp_run_decision_round.py`
运行一轮完整的闭环决策分析。

#### `rdp_select_parameter_upgrade.py`
从 parameter candidates 中筛选升级候选。

#### `rdp_evaluate_promotion_readiness.py`
评估当前证据是否足以支持下一轮 live 测试。

#### `rdp_update_decision_registry.py`
把 recommendation / decision 写入 registry 文件。

---

## 7. 建议新增的模块目录

```text
aats/data_platform/decision_system/
  evidence_bundle.py
  candidate_selector.py
  decision_engine.py
  readiness_evaluator.py
  recommendation_registry.py
  report_builder.py
  phase6_round_runner.py
```

---

## 8. 必须复用的现有能力

Phase 6 不是从零分析，而是整合前面 phase 的结果。

必须复用：

### 8.1 Phase 2
- parameter recommendations
- parameter candidates
- scan comparison
- research conclusions

### 8.2 Phase 3
- attribution summary
- top failure modes
- replay/live alignment
- phase3 conclusion

### 8.3 Phase 4
- execution cost summary
- execution realism comparison
- fill feasibility
- phase4 conclusion

### 8.4 Phase 5
- artifact index
- parameter registry
- active round index
- quality monitor summary

---

## 9. Phase 6-A：Evidence Bundle 统一化

### 9.1 目标
把跨 phase 的结果整理成一个统一对象，供后续 decision engine 使用。

### 9.2 Evidence Bundle 至少包含
- `phase2_evidence`
- `phase3_evidence`
- `phase4_evidence`
- `phase5_governance_evidence`

### 9.3 每个 bundle 至少包含
- source round ids
- artifact refs
- dataset version
- parameter set refs
- confidence
- freshness / staleness 信息

### 9.4 输出
```text
artifacts/decision_rounds/<round_id>/evidence_summary.json
```

---

## 10. Phase 6-B：参数升级候选选择

### 10.1 目标
从多个 parameter candidates 中选出：
- 更值得进入下一轮 live test 的参数
- 不值得推荐的参数
- 仍需更多验证的参数

### 10.2 判断依据
至少综合：

#### 来自 Phase 2
- opening_count
- positive_edge_ratio
- mean_expected_edge_bps
- calibration / scan 稳定性

#### 来自 Phase 3
- attribution failure modes
- 是否系统性卡在 strategy / allocator / risk

#### 来自 Phase 4
- cost-adjusted edge
- full_fill_ratio
- execution realism 是否显著恶化

#### 来自 Phase 5
- parameter set 是否 stale
- source round 是否失败或 partial 太多
- artifact 是否完整

### 10.3 输出
```text
parameter_upgrade_candidates.json
```

### 10.4 每个候选至少包含
- `parameter_set_id`
- `source_round_id`
- `family`
- `symbol`
- `timeframe`
- `decision` (`promote_candidate` / `hold` / `reject`)
- `confidence`
- `reason`

---

## 11. Phase 6-C：family/timeframe 状态决策

### 11.1 目标
对每个 family/timeframe 输出 operational status 建议。

### 11.2 推荐状态
- `keep_active`
- `lower_priority`
- `pause`
- `require_review`

### 11.3 决策依据
例如：

#### keep_active
- Phase 2 参数稳定
- Phase 3 未发现显著系统性阻塞
- Phase 4 execution realism 不差
- governance 状态健康

#### lower_priority
- 研究结果一般
- 可运行但证据不够强
- execution realism 偏弱

#### pause
- attribution 显示系统性失效
- execution realism 极差
- governance / data 质量不可信

#### require_review
- 证据冲突
- 参数 stale
- 部分关键 round 失败
- 不足以做明确决策

### 11.4 输出
```text
family_timeframe_decisions.json
```

---

## 12. Phase 6-D：Promotion Readiness 评估

### 12.1 目标
回答一个核心问题：

> 当前是否建议把某个 parameter set 进入下一轮 live test？

### 12.2 结果分类
- `ready_for_next_live_test`
- `not_ready_more_research_needed`
- `not_ready_attribution_issue`
- `not_ready_execution_issue`
- `not_ready_governance_issue`

### 12.3 核心判断逻辑
至少回答：

1. 研究结果是否有稳定提升
2. attribution 是否没有暴露严重结构问题
3. execution realism 是否没有显著吞噬 edge
4. artifact / round / parameter governance 是否健康
5. 推荐参数是否足够新鲜且可追溯

### 12.4 输出
```text
promotion_readiness_report.json
```

---

## 13. Recommendation Registry 设计

### 13.1 目标
让 recommendation 成为一个受治理对象，而不是只存在于结论文档里。

### 13.2 建议 registry 文件
```text
artifacts/decision_system/recommendation_registry.json
```

### 13.3 每条 recommendation 至少包含
- `recommendation_id`
- `created_at`
- `family`
- `symbol`
- `timeframe`
- `recommendation_type`
  - `parameter_upgrade`
  - `keep_active`
  - `lower_priority`
  - `pause`
  - `require_review`
- `target_parameter_set_id`
- `confidence`
- `reason`
- `evidence_bundle_ref`
- `status`
  - `draft`
  - `approved`
  - `rejected`
  - `superseded`

---

## 14. Active Decision Registry 设计

### 14.1 目标
记录当前每个 family/timeframe 的“有效运营状态”。

### 14.2 建议文件
```text
artifacts/decision_system/active_decision_registry.json
```

### 14.3 每项至少包含
- `family`
- `symbol`
- `timeframe`
- `current_status`
- `active_parameter_set_id`
- `last_recommendation_id`
- `last_updated_at`
- `notes`

---

## 15. Phase 6 的最终文档

建议生成：

```text
phase6_closed_loop_decision_conclusion.md
```

### 建议结构

#### 15.1 Scope
- symbol
- families
- timeframes
- evidence windows

#### 15.2 Evidence Summary
- 哪些 round 被纳入
- 哪些证据质量较高
- 哪些证据 stale / partial

#### 15.3 Parameter Upgrade Candidates
- 哪些参数值得 promote
- 哪些不值得

#### 15.4 Family / Timeframe Decisions
- keep / lower / pause / review

#### 15.5 Promotion Readiness
- 哪些对象 ready
- 哪些对象 not ready
- 原因是什么

#### 15.6 Governance Notes
- 哪些 recommendation 已登记
- 哪些 recommendation 仍 draft

#### 15.7 Next Step
- 若 ready：进入下一轮 live test
- 若 not ready：回到 Phase 2 / 3 / 4 哪一层继续补证据

---

## 16. Phase 6 的最小实现范围

第一版只要求：

1. 能从现有 artifact 中构建 evidence summary
2. 能输出 parameter upgrade candidates
3. 能输出 family/timeframe decisions
4. 能输出 promotion readiness report
5. 能更新 recommendation registry / active decision registry
6. 能生成结论文档

第一版不要求：

- 自动改生产参数
- 自动重启 live strategy
- 审批工作流系统
- Web UI
- 实时决策引擎
- 多资产投资组合级决策

---

## 17. 实现约束

### 17.1 不允许直接控制生产交易
Phase 6 第一版只生成建议，不直接执行。

### 17.2 不允许绕过 Phase 5 治理对象
所有 recommendation 必须引用：
- parameter registry
- artifact index
- active round index

### 17.3 不允许只输出 Markdown，不写 registry
Phase 6 必须写入：
- recommendation registry
- active decision registry

### 17.4 不允许黑盒打分
第一版 decision engine 必须是规则化、可解释的。

---

## 18. 建议的规则化决策思路

第一版可用 rule-based decision engine：

### 示例规则 1：promote candidate
如果同时满足：
- Phase 2 candidate confidence >= medium
- Phase 3 主要问题不在 strategy_blocked
- Phase 4 cost-adjusted edge >= 0
- Phase 5 governance 健康

则：
- `recommendation_type = parameter_upgrade`
- `decision = promote_candidate`

### 示例规则 2：pause
如果同时满足：
- attribution failure 持续集中在 risk / execution
- execution realism 明显差
- 且当前 active parameter 已 stale

则：
- `decision = pause`

### 示例规则 3：require review
如果：
- 研究结论与 attribution / execution realism 冲突

则：
- `decision = require_review`

---

## 19. 验收标准

Phase 6 第一版通过条件：

1. 能运行一轮 decision round
2. 能生成：
   - `evidence_summary.json`
   - `parameter_upgrade_candidates.json`
   - `family_timeframe_decisions.json`
   - `promotion_readiness_report.json`
   - `phase6_closed_loop_decision_conclusion.md`
3. 能写入：
   - `recommendation_registry.json`
   - `active_decision_registry.json`
4. 对至少一个 family/timeframe 给出明确 operational decision
5. 对至少一个 parameter set 给出 promote / hold / reject 建议
6. 建议均可追溯到 evidence bundle

---

## 20. 一句话总结

Phase 6 的职责是：

> **把前面各 phase 的研究、归因、execution、治理证据整合成“生产决策建议”，形成从数据到建议的真正闭环，但第一版只做建议生成，不直接控制生产系统。**
