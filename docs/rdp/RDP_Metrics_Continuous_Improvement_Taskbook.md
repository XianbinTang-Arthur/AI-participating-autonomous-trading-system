# RDP Metrics / Continuous Improvement 集成任务书（正式版）

## 1. 任务定位

在《RDP Deployment / Scheduling / Reliability 集成任务书（正式版）》之后，下一阶段不再只关注：

- 能不能稳定跑
- 失败了能不能恢复
- 有没有告警
- 环境是否隔离

而是进入：

> **让 RDP 与主交易系统整合具备“可衡量、可比较、可复盘、可持续优化”的能力。**

这一阶段的目标不是再补一层运行机制，而是回答：

- 整合之后到底有没有带来价值？
- 哪一轮参数升级真的改善了结果？
- 哪些 recommendation 经常被证明无效？
- 哪些 family/timeframe 在长期维度上值得继续投入？
- 哪些 workflow 只是“跑了”，但其实没有贡献？

也就是说，这一阶段解决的是：

> **从“系统可运行”升级到“系统可持续优化”。**

---

## 2. 为什么这是下一阶段

完成前一阶段后，你已经具备：

1. RDP integration workflow 可调度
2. 失败可恢复
3. 有告警
4. 有环境隔离
5. 有长期运行 runbook

但系统仍然缺少一个关键能力：

> **没有统一的 success metrics 和持续改进闭环。**

如果没有这一层，你只能知道：

- 流程有没有跑
- recommendation 有没有 apply
- observation 有没有生成

但你还不能系统性回答：

- apply 之后有没有变好
- 这次升级值不值得保留
- 哪些 phase 最贡献决策质量
- 哪些指标长期在恶化

所以这一步不是补基础设施，而是给整个体系加上：

- 评估指标
- 对比基线
- 版本比较
- 长周期复盘
- 持续改进 backlog

---

## 3. 本阶段目标

本阶段完成后，系统应具备以下能力：

1. 有统一的 integration success metrics
2. 每次 release / observation 都可与 baseline 比较
3. recommendation / apply / rollback 的长期效果可追踪
4. family/timeframe 的长期表现可比较
5. 能自动生成 improvement backlog
6. operator / reviewer / owner 可基于 metrics 做周期复盘

---

## 4. 本阶段不做什么

### 4.1 本阶段必须做
- 指标体系
- baseline / version comparison
- release effectiveness evaluation
- long-horizon review summary
- improvement backlog 生成

### 4.2 本阶段不做
- 新交易策略
- 新数据库架构
- 自动自优化参数
- 自主无人值守调参
- 复杂 BI 平台

---

## 5. 本阶段拆分为 5 个工作包

---

## 工作包 A：统一指标体系（Integration Success Metrics）

### A.1 目标
定义一套统一指标，用来衡量 RDP integration 是否真的改善了主系统。

### A.2 指标分层

#### 1. 研究层指标
- recommendation_count
- approved_recommendation_count
- promoted_parameter_set_count
- evidence_completeness_ratio
- stale_recommendation_ratio

#### 2. 归因层指标
- replay_live_alignment_coverage
- top_failure_mode_concentration
- strategy_blocked_ratio
- risk_rejected_ratio
- execution_blocked_ratio

#### 3. 执行可行性层指标
- full_fill_ratio
- partial_fill_ratio
- mean_total_execution_cost_bps
- positive_adjusted_edge_ratio

#### 4. 运营层指标
- apply_success_count
- rollback_count
- rollback_recommendation_count
- release_observation_completion_ratio
- release_without_gate_ratio

#### 5. 可靠性层指标
- workflow_success_ratio
- retry_success_ratio
- alert_open_count
- alert_resolution_time
- stale_round_count

### A.3 建议新增模块

```text
aats/data_platform/metrics/
  definitions.py
  metric_calculator.py
  metric_registry.py
```

### A.4 建议新增输出

```text
artifacts/metrics/current_metrics_snapshot.json
artifacts/metrics/metrics_history.json
```

### A.5 验收标准

1. 有统一指标清单
2. 至少能生成一次 metrics snapshot
3. 指标可分 phase / family / timeframe 维度查看

---

## 工作包 B：Baseline / Version Comparison

### B.1 目标
让每次 parameter apply / release 都能与历史 baseline 对比，而不是孤立看结果。

### B.2 baseline 定义
第一版至少支持：

- 当前 active parameter set 之前的上一版作为 baseline
- 最近一次 frozen parameter set 作为 baseline
- 同 family/timeframe 的最近稳定 release 作为 baseline

### B.3 需要比较的内容

#### 1. Recommendation 层
- 当前 recommendation vs 上一版 recommendation 的差异

#### 2. Observation 层
- 当前 observation summary vs baseline observation

#### 3. Attribution 层
- failure modes 是否改善 / 恶化

#### 4. Execution Realism 层
- cost-adjusted edge 是否改善 / 恶化

### B.4 建议新增脚本

```text
scripts/rdp_compare_release_to_baseline.py
```

### B.5 建议新增输出

```text
artifacts/metrics/release_comparisons/<release_id>/
  baseline_comparison.json
  baseline_comparison_report.md
```

### B.6 验收标准

1. 每个 release 至少可找到一个 baseline
2. comparison 报告可输出 improvement / regression / neutral 结论
3. operator 能知道“这次比上次到底有没有更好”

---

## 工作包 C：Release Effectiveness Evaluation

### C.1 目标
给每一次 parameter release 一个“是否有效”的后评估结果。

### C.2 需要回答的问题

- 这次 apply 后是否真的带来改善？
- 这次 apply 是否应该保留？
- 是否应该标记为 ineffective / review-needed？

### C.3 第一版评价维度

#### 1. 行为层
- opening 结构是否更合理
- attribution 是否改善

#### 2. 执行层
- execution realism 是否恶化

#### 3. 运营层
- 是否触发 rollback recommendation
- observation 是否顺利完成

#### 4. 治理层
- evidence freshness 是否足够
- 是否存在 unresolved alerts

### C.4 评价结果分类

- `effective`
- `mixed`
- `ineffective`
- `rollback_triggered`
- `insufficient_evidence`

### C.5 建议新增脚本

```text
scripts/rdp_evaluate_release_effectiveness.py
```

### C.6 建议新增 registry

```text
artifacts/metrics/release_effectiveness_registry.json
```

### C.7 验收标准

1. 每个 release 最终可获得 effectiveness 结论
2. effectiveness 可追溯到 comparison 和 observation
3. rollback 情况可反映到 effectiveness

---

## 工作包 D：Long-Horizon Review / 周期复盘

### D.1 目标
让系统支持按周 / 月做长周期复盘，而不是只看单次 release。

### D.2 周期复盘要回答的问题

- 过去一周 / 一月，哪些 family/timeframe 最稳定？
- 哪些 parameter change 经常失败或被回滚？
- 哪些 failure mode 长期占主导？
- recommendation 的命中率如何？
- rollback 是否过于频繁？

### D.3 建议新增脚本

```text
scripts/rdp_run_periodic_review.py
```

支持：

- `--window weekly|monthly`
- `--family`
- `--timeframe`
- `--environment`

### D.4 建议新增输出

```text
artifacts/reviews/weekly/<review_id>/
  review_summary.json
  review_report.md

artifacts/reviews/monthly/<review_id>/
  review_summary.json
  review_report.md
```

### D.5 复盘内容建议

1. Metrics snapshot 汇总
2. release history 汇总
3. rollback history 汇总
4. top alerts / top failure modes
5. family/timeframe ranking
6. 建议的 improvement backlog

### D.6 验收标准

1. 至少能生成 weekly review
2. review 可直接供 operator / owner 阅读
3. review 能给出 improvement 建议

---

## 工作包 E：Improvement Backlog 自动生成

### E.1 目标
把 metrics / review 结果转成下一轮改进任务，而不是只停留在报告。

### E.2 backlog 来源
至少可来自：

- 高失败率的 workflow
- 高频 rollback recommendation
- attribution 长期集中某 failure mode
- execution realism 长期较差
- stale recommendations
- low readiness family/timeframe

### E.3 backlog 项至少包含

- `backlog_id`
- `created_at`
- `source`
- `category`
- `family`
- `timeframe`
- `priority`
- `problem_statement`
- `suggested_action`
- `status` (`open` / `in_progress` / `resolved` / `ignored`)

### E.4 建议新增输出

```text
artifacts/metrics/improvement_backlog.json
```

### E.5 建议新增脚本

```text
scripts/rdp_generate_improvement_backlog.py
```

### E.6 验收标准

1. review 结果可转成 backlog 项
2. backlog 有优先级与状态
3. 后续开发/运营可以直接消费 backlog

---

## 6. 本阶段建议新增/修改的文件

### 6.1 脚本
```text
scripts/rdp_build_metrics_snapshot.py
scripts/rdp_compare_release_to_baseline.py
scripts/rdp_evaluate_release_effectiveness.py
scripts/rdp_run_periodic_review.py
scripts/rdp_generate_improvement_backlog.py
```

### 6.2 新模块
```text
aats/data_platform/metrics/
  definitions.py
  metric_calculator.py
  metric_registry.py
  baseline_comparison.py
  release_effectiveness.py
  periodic_review.py
  backlog_builder.py
```

### 6.3 Registry / Artifact
```text
artifacts/metrics/current_metrics_snapshot.json
artifacts/metrics/metrics_history.json
artifacts/metrics/release_effectiveness_registry.json
artifacts/metrics/improvement_backlog.json
artifacts/reviews/weekly/
artifacts/reviews/monthly/
```

### 6.4 文档
```text
docs/operations/rdp_metrics_framework.md
docs/operations/release_effectiveness_evaluation.md
docs/operations/periodic_review_workflow.md
docs/operations/improvement_backlog_process.md
```

---

## 7. 最小实现范围（MVP）

第一版只要求：

1. 能生成 metrics snapshot
2. 至少能对一次 release 做 baseline comparison
3. 至少能输出一次 release effectiveness evaluation
4. 至少能生成一次 weekly review
5. 能生成 improvement backlog

第一版不要求：

- 复杂可视化 dashboard
- 自动参数优化
- 机器学习驱动的 recommendation re-ranking
- 完全自动闭环调参

---

## 8. 建议实施顺序

### 第 1 步
先做 metrics definitions + snapshot

### 第 2 步
再做 baseline comparison

### 第 3 步
再做 release effectiveness evaluation

### 第 4 步
再做 periodic review

### 第 5 步
最后做 improvement backlog

---

## 9. 风险与注意事项

### 9.1 不要一开始定义过多指标
第一版先选最有业务价值、最能支撑复盘的指标。

### 9.2 baseline 必须可解释
不要引入模糊的“综合评分”替代清晰对比。

### 9.3 effectiveness 评估必须允许 `insufficient_evidence`
不是每次 release 都能立刻判断有效与否。

### 9.4 backlog 不要只是“再观察”
必须尽量转成可执行的问题陈述和建议动作。

---

## 10. 验收标准

本阶段通过条件：

1. 有统一 metrics framework
2. metrics snapshot 可生成
3. release 可做 baseline comparison
4. release 可得 effectiveness 结论
5. weekly review 可生成
6. improvement backlog 可生成

---

## 11. 一句话总结

这个阶段的职责是：

> **把 RDP integration 从“能稳定运行”推进到“能持续衡量、持续比较、持续复盘、持续优化”。**
