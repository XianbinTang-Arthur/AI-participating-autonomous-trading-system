# Route A / Golden Path Readiness Gate Hardening SoW

## 1. Business objectives and boundaries
- 目标：收紧 Phase 6-D promotion readiness，只认完整、非 replay-only 的归因与 execution 证据，防止半成品研究结果继续被判成 `ready_for_next_live_test`。
- 边界：只修改 `aats/data_platform/decision_system/readiness_evaluator.py` 及直接相关测试；不改 live 策略逻辑、不改部署脚本、不改 family/timeframe decision engine。

## 2. Module responsibilities and domain model
- `readiness_evaluator` 负责回答“当前证据是否足以进入下一轮 live test”。
- 它不是研究工具，也不是执行器；它只消费已有 evidence bundle、upgrade candidates、family/timeframe decisions 并输出 readiness 报告。

## 3. Input/output interfaces
- 输入保持不变：`evaluate_promotion_readiness(evidence_bundle, upgrade_candidates, ft_decisions)`。
- 输出保持现有 schema，避免破坏下游 report builder；仅收紧 checks 判定与 readiness 规则。

## 4. Database schema / tables / indexes / constraints
- 无数据库 schema 变更。

## 5. Transactions, consistency, concurrency
- 纯内存判定，无事务变化。

## 6. Authorization, authentication, data security
- 无认证与凭证改动。

## 7. Error handling and idempotency
- 保持纯函数语义；缺失证据必须明确降级为 failed check，而不是“跳过即通过”。

## 8. State transition and lifecycle
- 新规则：
  - 只有全部 checks 通过时，才允许 `ready_for_next_live_test`。
  - `Phase 3` 证据缺失、`latest_round.replay_only=True`、或 combo/overall status 无法证明成功时，都必须阻塞 readiness。
  - `Phase 4` 证据缺失，或 latest round 缺少任何可用 combo cost_summary 时，必须阻塞 readiness。
- 继续保留现有 not_ready 分类，但不再存在“critical subset pass → medium confidence ready”的中径。

## 9. Caching and performance
- 仅布尔判定与小规模遍历，无性能风险。

## 10. Logging, monitoring, auditing
- 不新增日志；通过 readiness report 的 `checks` 和 `blockers` 输出更明确的 detail。

## 11. Testing strategy
- 更新/新增最窄单测覆盖：
  - Phase 3 无数据 -> readiness fail
  - Phase 3 replay_only -> readiness fail
  - Phase 4 无数据 -> readiness fail
  - critical subset pass 但非全通过 -> 不再 ready
  - 完整证据仍可 ready
- 跑：
  - `tests/test_phase3_phase4_decision_fixes.py`
  - 如需，再补 `tests/unit/test_rdp_pipeline_chain_fixes.py` 中与 readiness 直接相关的断言
  - 全量 `tests/unit/`

## 12. Migration, rollback, compatibility
- 无数据迁移。
- 若回滚，仅回滚该文件和测试即可。

## 13. Configuration and environment isolation
- 无配置变更。

## 14. Code organization and dependencies
- 仅限 `aats/data_platform/decision_system/` 与 tests；避免牵连 `decision_engine`、`candidate_selector` 等其它模块。

## 15. Documentation and operations manual
- 若 behavior 注释与 docstring 不符，同步更新 `readiness_evaluator.py` 顶部说明即可；不扩写新治理文档。

## 16. Deployment and acceptance criteria
- 不 deploy。
- 验收标准：
  - replay_only / missing Phase3 / missing Phase4 均不能再返回 `ready_for_next_live_test`
  - 完整证据场景保持通过
  - 全量 unit 通过
