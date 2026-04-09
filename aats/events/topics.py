MARKET_SNAPSHOTS = "market.snapshots"
FEATURE_SNAPSHOTS = "features.snapshots"
HEALTH_SNAPSHOTS = "system.health_snapshots"
ACCOUNT_BASELINES = "account.baselines"
DECISION_CONTEXTS = "strategy.decision_context"
BASELINE_ASSESSMENTS = "strategy.baseline_assessment"
AI_ASSESSMENTS = "strategy.ai_assessment"
AI_DECISION_BRIEFS = "strategy.ai_decision_brief"
AI_SHADOW_DECISIONS = "strategy.ai_shadow_decision"
AI_SHADOW_EVALUATIONS = "strategy.ai_shadow_evaluation"
AI_PERFORMANCE_REPORTS = "strategy.ai_performance_report"
AI_DEGRADATION_EVENTS = "strategy.ai_degradation"
STRATEGY_COORDINATOR_SNAPSHOTS = "strategy.coordinator_snapshots"
STRATEGY_SLEEVE_INTENTS = "strategy.sleeve_intents"
PORTFOLIO_ALLOCATION_DECISIONS = "strategy.portfolio_allocation_decisions"
STRATEGY_EXECUTION_BUNDLES = "strategy.execution_bundles"
POSITION_TARGETS = "strategy.position_target"
OVERLAY_PARENT_EXPOSURES = "strategy.overlay_parent_exposure"
DECISION_OUTCOMES = "strategy.decision_outcome"
POLICY_DECISIONS = "policy.decisions"
RISK_DECISIONS = "risk.decisions"
EXECUTION_PLANS = "execution.plans"
ORDER_INTENTS = "execution.order_intents"
ORDER_UPDATES = "execution.order_updates"
# Stage 6 Slice 6.5：跨进程 obligation 缓存广播。execution 在每次 save_obligation
# 之后 best-effort 广播 OrderObligation payload，decision/gateway/market 进程的
# ObligationHotStateCache 订阅本 topic 更新本地缓存。丢一条不致命（读路径会 fall
# back 到 obligation_repo Postgres SELECT），但会让 cache 短暂 stale。详见
# docs/task/stage_6_slice_6_5_obligation_hot_state_design.md。
OBLIGATION_UPDATES = "execution.obligation_updates"
FILL_EVENTS = "execution.fill_events"
PORTFOLIO_BALANCE_DELTAS = "portfolio.balance_deltas"
PORTFOLIO_SNAPSHOTS = "portfolio.snapshots"
RECONCILIATION_REPORTS = "reconciliation.reports"
AUDIT_RECORDS = "system.audit_records"
BLOCKER_SNAPSHOTS = "system.blocker_snapshots"
OPERATOR_ACTIONS = "system.operator_actions"
# Slice 4-proc operator command proxy: gateway→execution 请求-响应代理。
# 设计文档：docs/task/slice_4proc_operator_command_proxy_fix_design.md
# rebaseline / resume 这类依赖 portfolio_service / reconciliation_service 的
# operator 命令在 4 进程 gateway role 下无法本地执行（slice 门控导致这两个
# service 在 gateway 为 None），必须通过 NATS 代理到 execution 进程上跑。
# 两条 topic 都归 critical（丢包会让 HTTP 超时、系统卡在 blocker）。
OPERATOR_COMMAND_REQUESTS = "system.operator_command_requests"
OPERATOR_COMMAND_RESPONSES = "system.operator_command_responses"
EXECUTION_ERROR_SUMMARIES = "execution.error_summaries"
PROCESSING_FAILURES = "system.processing_failures"
# Stage 6 Slice 6.2：跨进程 kill_switch 状态广播。critical 路径，丢一条会让某个
# 进程错过 halt → 资金风险。详见 docs/task/stage_6_slice_6_2_kill_switch_design.md。
KILL_SWITCH_STATE = "system.kill_switch_state"
RECONCILIATION_VALIDATIONS = "reconciliation.validations"
REPLAY_VALIDATIONS = "replay.validations"
STRATEGY_PROFILE_RECOMMENDATIONS = "strategy.profile_recommendations"
STRATEGY_PROFILE_ACTIVATIONS = "strategy.profile_activations"
STRATEGY_PROFILE_REJECTIONS = "strategy.profile_rejections"
STRATEGY_PROFILE_EVALUATIONS = "strategy.profile_evaluations"
STRATEGY_PROFILE_COMPARISON_REPORTS = "strategy.profile_comparison_reports"
STRATEGY_PROFILE_OPTIMIZATION_REPORTS = "strategy.profile_optimization_reports"
STRATEGY_PROFILE_SELECTION_DECISIONS = "strategy.profile_selection_decisions"
STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES = "strategy.profile_auto_rollback_policies"
STRATEGY_PROFILE_ACTIVATION_POLICIES = "strategy.profile_activation_policies"
