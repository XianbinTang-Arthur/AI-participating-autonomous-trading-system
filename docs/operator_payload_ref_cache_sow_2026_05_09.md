# Operator Payload Ref Cache SOW - 2026-05-09

## Business Objectives and Boundaries

继续优化 operator dashboard 读侧。目标是减少 `latestDecision`、`recentDecisions` 和相关 snapshot loader 对 event store 的重复 `get_many(event_id in ...)` 读取。本轮只缓存 immutable event payload by ref，不改变交易决策、风控、下单、审计写入语义、DB schema 或前端展示字段。

## Module Responsibilities and Domain Model

`OperatorQueryService` 负责把 audit refs 转成 dashboard payload。event id 对应的 event payload 在系统语义上不可变，因此适合作为 runtime-scoped read-through cache。event store 仍是 source of truth；cache 只作为重复读取加速层。

## Input/Output Interfaces

`payload_by_ref()` 与 `payloads_by_ref_map()` 的公开返回结构保持不变：返回带 `_event_id`、`_topic` 的 payload dict。调用方仍按原 schema 消费，不新增 API 字段。

## Database Schema / Tables / Indexes / Constraints

不修改 schema、索引或 migration。不引入 `decision_summary_json` 写路径。本轮优化发生在 DB 读取之后、operator read model 之内。

## Transactions, Consistency, Concurrency

不新增事务。cache 使用 `OperatorQueryService` 已有 runtime 级锁保护。多个请求同时 miss 同一 ref 时最多产生一次短暂重复读取，结果一致，因为 event payload 不可变。

## Authorization, Authentication, Data Security

不改变鉴权。cache 只保存系统已可读取的 event payload，不输出凭证，不读取 `.env`。

## Error Handling and Idempotency

event store 读取失败时保持原异常行为，不写入 cache。重复调用同一 refs 返回等价 payload，并给调用方返回副本，避免下游 normalize/mutate 污染 cache。

## State Transition and Lifecycle

不新增业务状态。cache 生命周期绑定 Python runtime；部署或进程重启后自然清空。

## Caching and Performance

新增 runtime-scoped LRU cache，限制条目数和单 payload 粗略大小，避免把超大 AI/strategy payload 长期驻留内存。命中后跳过 event store `get_many`，只对缺失 refs 执行补读。

## Logging, Monitoring, Auditing

不新增日志。验收看 dashboard snapshot 刷新耗时、gateway 错误日志、Postgres active 长查询和容器健康。

## Testing Strategy

新增 unit tests 覆盖：第二次读取只补 missing refs；返回 payload 是副本，调用方 mutation 不污染 cache；LRU 超上限会淘汰旧 ref。

## Migration, Rollback, Compatibility

无 migration。回滚方式为 revert commit 后重新部署。API 输出兼容。

## Configuration and Environment Isolation

不新增配置。Windows 验证使用 `.venv\Scripts\python.exe`；WSL2 integration 使用 `~/aats-venv`；部署使用 `bash scripts/deploy.sh --profile derivatives-live --skip-commit`。

## Code Organization and Dependencies

只修改 `aats/services/operator/query_service.py` 与相关单测；新增标准库 `OrderedDict` / `deepcopy`，不新增第三方依赖。

## Documentation and Operations Manual

本文记录本轮优化边界。`decision_summary_json` 和 RDP aggregate loader 仍应作为单独任务评估。

## Deployment and Acceptance Criteria

验收标准：lint 通过；相关单测通过；全量 unit 通过；受影响 WSL2 integration 通过；commit 完成；标准部署成功；`/healthz` 200；核心容器 healthy；无 active Postgres 查询超过 5 秒；gateway 近窗口无 recurring `Traceback` / `ERROR` / `CRITICAL`。
