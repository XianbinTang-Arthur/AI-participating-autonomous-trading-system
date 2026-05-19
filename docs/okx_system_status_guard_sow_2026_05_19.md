# OKX 系统状态门禁修复 SOW

## Business objectives and boundaries

目标是修复合约实盘恢复页因 OKX public system/status 中未来维护公告而误判为当前全局事故的问题，并降低人工刷新交易所状态时对 OKX 低频公告接口的重复访问。边界限定在账户状态判定、operator 刷新动作和对应测试，不改变下单、对账、账本和恢复资格的核心安全链路。

## Module responsibilities and domain model

- `OKXAccountService` 负责拉取账户、仓位、风险快照和 OKX 系统公告，并将真正影响执行的系统公告转成账户阻断。
- `OperatorQueryService` 负责 operator 恢复动作中的行情和账户刷新。
- `DerivativesLiveGuard` 继续只消费账户服务的 ready/blockers 结果，不承担 OKX 公告细分逻辑。

## Input/output interfaces

输入包括 OKX `/api/v5/system/status` rows、账户 refresh 请求参数、operator blocker action。输出包括 `status()["ready"]`、`status()["blockers"]`、`system_status_ok`、guard blocker 和 operator action 事件。

## Database schema / tables / indexes / constraints

不涉及数据库 schema、索引或迁移。

## Transactions, consistency, concurrency

账户刷新仍由 `OKXAccountService._lock` 串行化。operator 刷新改用 `force_account_state=True`，保持账户/订单/仓位/风险快照强刷新，同时继续尊重低频 auxiliary cache 和 backoff，避免多个 retry 并发或连续绕过缓存。

## Authorization, authentication, data security

不改变 API 授权。operator action 仍使用现有 admin/session 认证。修复不读取或输出任何密钥、token 或 `.env` 内容。

## Error handling and idempotency

OKX system/status 的 `scheduled` 公告在未到 begin 前不产生硬阻断；无 begin 的 scheduled 公告不硬阻断。`ongoing` 和已到 begin 的核心维护继续阻断。Trailing stop 专项维护不阻断普通执行链路。限流 backoff 继续由 `_cached_aux_payload_optional` 处理。

## State transition and lifecycle

修复后，未来维护公告不会把账户状态从 ready 推到 unready；真正进行中或应已开始的核心维护仍会触发 `okx_system_status_incident`，并由 derivatives live guard 派生 only-reduce 防护。

## Caching and performance

operator 刷新不再使用 full `force=True`，避免每个 retry 强制刷新 instruments、account config、trade fee、system status、bills 等低频数据。账户状态相关的 balance/open orders/fills/positions/account risk 仍会刷新。

## Logging, monitoring, auditing

保持现有 `okx_rate_limited_backoff`、operator action 和 guard snapshot 审计事件。部署后用 Redis guard state、OKX status payload、execution/gateway 日志确认阻断是否收敛。

## Testing strategy

新增/更新测试覆盖：
- 未来 scheduled OKX 维护不阻断账户 ready。
- 已到 begin 的 scheduled 核心维护继续阻断。
- Trailing stop 专项维护不阻断普通执行链路。
- operator refresh 使用 `force_account_state=True`，避免绕过低频公告缓存。

## Migration, rollback, compatibility

不需要迁移。回滚方式是回退本次提交并重新部署。新增状态判定只放宽 false positive，不改变已有 `ongoing` 核心事故的阻断语义。

## Configuration and environment isolation

保留 `okx_system_status_gate_enabled` 和各刷新间隔配置。无需新增环境变量。

## Code organization and dependencies

改动保持在既有服务内，不新增依赖，不改变公共 API。

## Documentation and operations manual

运维判断应区分 OKX 未来公告、专项维护和当前核心维护。风险页如果仍显示 `okx_system_status_incident`，应先看 `system_status_items` 的 state、title、begin/end，再决定是否等待、刷新或人工处理。

## Deployment and acceptance criteria

验收条件：
- 相关单元和集成测试通过。
- 部署后 gateway 和核心容器 healthy。
- 当前 trailing stop scheduled maintenance 不再让 derivatives live guard 因 `okx_system_status_incident` / `account_state_unready` 进入恢复受限。
