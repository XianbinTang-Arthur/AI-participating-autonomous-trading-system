# Task120 Postgres Startup Parent Refresh SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 目标
- 在 Postgres runtime 的启动恢复阶段主动刷新一次 `ExitExecutionIntent` 聚合真相。
- 减少 parent-exit 必须等到首次 `sync_exchange_state()` 或 `reconciliation` 才收敛的时间窗。
- 将 startup refresh 得到的 parent review 结果直接并入启动期 `recovery_status`，而不只是写入 notes。
- 将 startup parent review 额外持久化成一条 state snapshot，供 operator/recovery 面直接审计。
- 保持现有启动流程和 public API 不变，仅补一层启动时的主动 refresh / overlay。

## 当前行为摘要
- parent-exit 已经可以在 `sync_exchange_state()` 和 `reconciliation.validate_now()` 时重算。
- Postgres 重启恢复后，如果还没跑到首次 sync 或 reconciliation，`exit_execution_repo` 中的 parent 可能仍是旧聚合结果。
- 这会让 operator/recovery 面在短时间内看到陈旧 parent 状态。

## 计划改动
1. 在 `aats/services/recovery_control/startup_recovery.py` 增加一个可复用的 startup refresh helper：
   - 输入：`settings`、`execution_repo`、`exit_execution_repo`、runtime scope
   - 输出：已刷新的 parent 列表和 recovery notes
   - 异常时返回显式失败 note，避免静默跳过
   - 基于刷新后的 parent review items 生成启动期 status overlay
2. 在 `aats/bootstrap/config.py` 的 `build_runtime()` 启动恢复路径中：
   - 仅在 Postgres runtime 下调用该 helper
   - 将 refresh note 合并进 `recovery_status.notes`
   - 将 parent review 结果合并进 `recovery_state/review_required/resume_eligible/safe_to_trade/resume_blocked_reasons/unknown_state_details`
   - 将 startup parent review 保存为 `ReconciliationStateSnapshot`
3. 在 `aats/services/governance_engine/recovery_posture.py` 中：
   - 将 parent-exit review blocker 加入持久 blocker 集合
   - 避免 runtime build 后的后续 finalize 又把启动期 review overlay 清掉
4. 补测试：
   - unit：startup helper 会刷新 stale parent，并在异常时留下 failure note
   - unit：startup review overlay 会直接收紧 `RecoveryStatus`
   - unit：startup review 会落一条可审计 state snapshot
   - integration：Postgres restart 后无需等待首次 sync/reconciliation，parent 已主动收敛
   - integration：Postgres restart 后 parent review issue 会直接体现在 `recovery_status`
   - integration：operator/recovery 面会直接看到 `latest_state_snapshot.source=startup_exit_execution_review`

## 非目标
- 不修改 parent-child 聚合规则
- 不新增 operator action
- 不改动自动拆单行为
- 不新增数据库 schema

## 验收标准
- Postgres recovery 启动后，stale parent 会被主动刷新
- `recovery_status.notes` 能记录 refresh 结果
- lint、相关 unit tests、最窄 integration tests 通过
