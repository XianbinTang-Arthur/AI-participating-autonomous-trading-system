# Rebaseline Timeout Root Cause SOW

## Objective

定位并收敛 operator 点击“确认为新基线”后前端超时的真实原因，而不是简单提高请求超时上限。

## Findings

- Gateway 请求已到达 execution 进程；前端的 `signal is aborted without reason` 是超时后的表层错误。
- `operator_command_client` 在 90 秒后超时并丢弃 pending correlation；execution 在超时后才发布响应，因此 gateway 记录 late response。
- 运行时证据显示当前库里 `exit_execution_intents` 共 319 条，其中 318 条是终态，只有 1 条非终态。
- 旧刷新路径会在 rebaseline/reconciliation 中重写历史终态 exit parent，且 rebaseline 即使 reservation 已追平也会同步所有 419 条 obligation。

## Changes

- Exit-execution truth refresh 跳过终态 parent，只处理仍可能影响恢复资格的非终态退出任务。
- Rebaseline shadow obligation sync 在 reservation 数量已追平 obligation 数量时跳过全量同步。
- Frontend abort 文案保留为诊断友好改动：请求超时或取消时显示中文，不再直接暴露浏览器原始 abort 文本。

## Boundaries

- 不修改策略、风控门、下单路径、AI provider、symbol、venue、schema 或 release/promotion/tuning。
- 不绕过 kill switch、对账、恢复资格或 operator review。
- 不读取或输出凭证。

## Validation

- Focused unit tests cover terminal exit parent skip and shadow sync skip/sync planning.
- Frontend unit tests cover localized timeout/cancel abort errors.
- Full repository lint/unit validation must pass before deployment.

## Rollback

Revert the code changes in:

- `aats/services/execution_engine/exit_intent_aggregator.py`
- `aats/services/operator/reconciliation_system_queries.py`
- `aats/api/static/modules/api-client.js`
- related focused tests
