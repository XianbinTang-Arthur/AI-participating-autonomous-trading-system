# Task 170 - Recovery、Fee Quarantine、认证计数与 Cookie 默认安全收口

## Business objectives and boundaries
- 为 phase4 启动恢复补齐 `SENT` 但未获交易所确认的 submit 命令专门阻断语义。
- 为未知手续费币种提供单条坏数据隔离路径，避免查询和投影链路被单个 fill 直接打断。
- 修正 Postgres 操作员登录失败计数的并发丢增量风险。
- 将 operator session cookie 改成 secure-by-default，同时保留本地开发 HTTP 可测性。

## Module responsibilities and domain model
- `startup_recovery.py` 负责恢复分型与阻断原因落地。
- `recovery_posture.py` 负责把持久化恢复阻断映射到恢复姿态。
- `accounting.py` 负责手续费币种解析与安全 helper。
- `lot_projection.py`、`query_service.py`、`trial_guard.py`、`strategy_profile_context.py`、`strategy_execution_health.py` 负责消费 safe fee helper。
- `operator_repo_postgres.py` 负责数据库侧认证计数更新。
- `settings.py` 与配置 YAML 负责默认安全策略。

## Input/output interfaces
- Recovery 新增 `stuck_sent_submit_order_count`，并输出 `stuck_sent_submit_commands` blocker。
- Lot projection 新增：
  - `quarantined_fill_ids`
  - `processing_failures`
- Strategy profile performance 新增：
  - `unsupported_fee_fill_count`

## Database schema / tables / indexes / constraints
- 本任务未新增数据库表与列。
- 登录失败计数修复基于现有 `operator_users` 行级锁。

## Transactions, Consistency, Concurrency
- `record_login()` 与 `record_login_failure()` 改为 `SELECT ... FOR UPDATE`，避免并发读改写丢增量。
- 启动恢复对 `stale SENT submit` 单独识别，但仍保留 `pending_execution_commands` 总阻断。

## Authorization, Authentication, Data Security
- `operator_session_cookie_secure` 默认改为 `True`。
- `dev.yaml` 与 `local_demo.yaml` 显式覆写为 `False`，仅用于本地 HTTP 调试。

## Error Handling and Idempotency
- `try_fill_fee_delta_in_quote()` / `try_fill_fee_cost_in_quote()` 提供非抛异常读取通道。
- authoritative fill ingestion 仍 fail-closed，并写 `ProcessingFailureRecord.details`。
- projection/query 路径改为隔离坏 fill，而不是整条链直接崩溃。

## State Transition and Lifecycle
- `CREATED/SUBMITTING + no venue_order_id + submit command stuck in SENT` 现在归类为独立 stuck pre-submit 状态。
- 该状态在 phase4 启动时会直接进入 `resume_blocked`。

## Caching and Performance
- safe fee helper 为轻量包装，不引入额外持久化查询。
- `stuck_sent_submit` 识别复用现有命令 lookup key，不新增全表扫描。

## Logging, Monitoring, Auditing
- unsupported fee ingestion 继续发布 `PROCESSING_FAILURES`，并补充 fee 细节。
- lot projection quarantine 会保留 `processing_failures` 供后续 operator 面使用。

## Testing Strategy
- unit:
  - `test_round2_safety_regressions.py`
  - `test_task57_lot_projection_and_convergence.py`
  - `test_settings.py`
- integration:
  - `test_phase4_recovery_reconciliation_runtime.py`
  - `test_operator_api.py`

## Migration, Rollback, Compatibility
- 所有对外变更均为向后兼容新增字段。
- `operator_session_cookie_secure` 默认收紧，但 dev/local profile 显式保留旧行为。

## Configuration and Environment Isolation
- `base.yaml` 切到 secure-by-default。
- `dev.yaml`、`local_demo.yaml` 显式保留本地不安全 cookie，避免误伤 HTTP 测试。

## Code Organization and Dependencies
- 仅在已有模块内做最小收口，没有新增跨层公共服务。
- safe fee helper 保持在 `accounting.py`，避免多处重复 try/except。

## Documentation and Operations Manual
- 启动恢复如果出现 `stuck_sent_submit_commands`，操作员应按现有 stuck submission 流程处理。
- 若 operator 看到 unsupported fee quarantine，应优先核对该 fill 的 `fee_currency` 与 instrument 配置。

## Deployment and Acceptance Criteria
- lint 通过。
- 受影响 unit tests 通过。
- 受影响 integration tests 通过。
- `stuck SENT submit` 能在 phase4 启动时被明确阻断。
- 单条未知手续费 fill 不再让 lot projection / query / trial guard / strategy health 整体崩溃。
