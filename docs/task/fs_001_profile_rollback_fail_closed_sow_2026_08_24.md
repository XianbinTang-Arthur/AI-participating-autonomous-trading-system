# FS-001 Profile Rollback 失败关闭收口设计与实施范围

> 文档状态：Phase 3B 实施任务 / 设计冻结  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，已包含尚未提交的 Phase 3A FS-002 变更  
> 核对范围：当前代码、Phase 2 审计证据、隔离替身复现  
> 运行时边界：未读取 `.env.*`，未连接真实账户/交易所，未修改真实数据库  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段只关闭 `FS-001` 已证明的“未回滚有效参数，却返回 `ok=true/status=rolled_back`并持久化终态”错误成功语义。在存在可验证的 reverse saga 前，endpoint 必须 fail-closed：保留认证、token actor 绑定与双人签署校验，但不写任何 rollback 终态，不返回成功。

本阶段不伪造一个“真回滚”，不复用已证明缺少运行时读回契约的 apply saga，不远程改 live 配置，不部署。`FS-001` 只能记为 `PARTIALLY REMEDIATED / OPEN`，直到真实 reverse saga 和 execution/runtime readback 验证完成。

## 2. 当前行为与根因

修复前的隔离复现输出：

```text
result = {
  ok: true,
  recommendation_id: rec_fs001_before,
  status: rolled_back,
  pending_live_rollback: true
}
rolled_back_update_executed = true
commits = 1
```

路由仅把 `governance.recommendations.status` 改为 `rolled_back`；请求的 `to_parameter_set_id` 未被使用，`governance.active_parameter_sets`、`parameter_apply_history`、live payload 与 worker/runtime 状态都没有被回滚或读回。

进一步当前代码追踪还证明：

1. `profile_apply_saga.step3_update_live_payload()` 查询 `payload->>'profile_id'`，而当前 `StrategyProfileActivationState` 持久化字段是 `active_profile_id`；
2. saga 写入的三个 threshold key 不是 `StrategyProfileActivationState` 字段，Postgres repository 通过 Pydantic 重建状态时会忽略这些额外字段；
3. 未找到 execution worker 对这些 live payload threshold 的权威读回/热加载契约；
4. 因此把 apply saga 反向调用不能证明实际 runtime 已回滚，反而可能扩大 research/live/runtime 漂移。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `rdp_profile_routes.rollback_profile_rec` | 执行身份、token、actor 与双人签署校验；明确拒绝尚无安全实现的 rollback |
| `governance.recommendations` | 保留原状态；本阶段不写 `rolled_back` |
| `active_parameter_sets` / apply history | 本阶段不修改，不假设它们与 runtime 已一致 |
| live `strategy_profile_activation` | 本阶段不修改，避免绕过 execution 权威 |
| 审计文档 | 记录虚假成功已收口，以及真回滚仍未实现 |

状态语义：已经 `applied` 的 recommendation 在请求 rollback 后仍保持 `applied`，因为实际参数没有改变。禁止以 `pending_live_rollback=true` 抵消顶层成功语义。

## 4. 输入/输出接口

输入契约保持不变：`rec_id`、可选 `to_parameter_set_id`、`X-Rdp-Apply-Token`、当前 session principal。认证/token/双签校验失败仍使用现有 `403/409`。

通过安全校验后，固定返回 HTTP `501 Not Implemented`：

```json
{
  "detail": {
    "code": "profile_rollback_not_implemented",
    "message": "Profile 参数回滚暂不可用：安全的反向 Saga 与运行时读回尚未实现。",
    "recommendation_id": "...",
    "requested_parameter_set_id": "...",
    "current_status": "applied",
    "retryable": false
  }
}
```

不得返回 `ok=true`、`status=rolled_back` 或任何等价终态。

## 5. 数据库 schema、表、索引与约束

无 schema、table、index、constraint 或 migration 变更。本收口的验收核心是“零数据库写入”。真 reverse saga 若需持久化 operation type、expected from/to digest、worker ack 或 readback，必须在后续独立设计中通过 migration 引入，不复用含混的 `scope` 字段伪装。

## 6. 事务、一致性与并发

本阶段没有跨库事务。路由只读 recommendation 以完成授权判定，然后抛出 HTTPException；不 commit，不改 recommendation 状态。并发 apply/rollback 不会因 rollback endpoint 产生额外写入；真 reverse saga 仍需对 active set 和 runtime generation 实施 CAS/lock。

## 7. 授权、认证与数据安全

保留 `require_write_access`、v2 token 的 `action=rollback/scope=profile/recommendation_id` 绑定、session actor 一致校验和 approver/applier 分离。响应不包含 token、密钥、凭证、DB URL 或 parameter values。

## 8. 错误处理与幂等

- 缺 token/无效 token：现有 `403`。
- actor 不一致：现有 `403`。
- 无双人授权：现有 `409`。
- 安全校验通过：稳定 `501/profile_rollback_not_implemented`。
- 重复请求：每次均是同样的无写入失败，幂等且不会把状态推进到虚假终态。

## 9. 状态转换与生命周期

```text
applied --rollback request--> HTTP 501 + remains applied
released/approved/other --rollback request--> HTTP 501 + remains unchanged
```

本收口不引入 `rollback_pending`，因为当前 schema 和 worker 都没有消费该状态的权威契约。不得使用文档或 UI 标签替代真实运行时转换。

## 10. 缓存与性能

无新缓存，无运行时广播。一次只读 recommendation 查询后立即失败，性能影响可忽略。

## 11. 日志、监控与审计

记录 warning 级事件，字段限于 recommendation id、当前状态、是否显式指定 target 和 actor；不记录 token 或 parameter values。本事件只表示请求被安全拒绝，不表示 rollback 开始或完成。

## 12. 测试策略

新增单元测试覆盖：

1. 合法 token/actor/双签请求返回 `501`；
2. recommendation 未变成 `rolled_back`，没有 UPDATE/commit；
3. 响应不包含 `ok=true` 或虚假终态；
4. 显式 `to_parameter_set_id` 仅用于不含敏感数值的失败诊断，不触发数据写入；
5. 无双签仍先返回原 `409`，不因 fail-closed 收口绕过安全校验；
6. 重复请求不产生副作用。

同时运行 profile route/token/saga 相关回归、全量单测与 Ruff。集成测试只在现有隔离依赖可用时执行。

## 13. 迁移、回滚与兼容

无 DB migration。该 endpoint 的 HTTP 成功契约有意收紧：任何依赖旧 `200/ok=true` 的客户端必须把 `501` 视为“未回滚”，这是修复不可避免的行为变化。不提供恢复旧虚假成功语义的生产回滚方案；若出现兼容问题，保持 NO-GO 并重新设计客户端契约。

## 14. 配置与环境隔离

无新环境变量、feature flag 或凭证。测试仅使用 fake session/Request/Principal，不读真实 DB URL，不访问 live pool。

## 15. 代码组织与依赖

预计只修改：

- `aats/api/rdp_profile_routes.py`
- 新增直接单元测试
- `audit/full_system_2026_08_24` 中的 FS-001 状态/修复证据
- 必要的现行 API/运维文档纠错

不修改 `profile_apply_saga.py`，因为当前模型不能证明运行时回滚；不新增三方依赖。

## 16. 文档、运维手册与验收标准

修正任何把 profile recommendation rollback 说成已可用的现行文档，历史设计保留历史语义。在审计包新增 `22-fs-001-profile-rollback-fail-closed.md`，并只在证据支持时将 FS-001 从 `FAIL` 改为 `PARTIALLY REMEDIATED / OPEN`。

本阶段验收：

- 原虚假 rollback 复现不再得到成功响应；
- 路由不写 recommendation/active set/history/live payload；
- 授权校验未被弱化；
- 直接、相关与全量测试通过；
- 文档明确区分“错误成功已收口”和“真实 rollback 未实现”。

真正关闭 FS-001 仍需：选定 execution-owned runtime authority，建立持久化 reverse operation 与 from/to digest，用 CAS/lock 防并发覆盖，使 active set、live profile revision/payload、history 与 worker readback 达成同一 generation，并在任一步失败时保持 pending/failed 而不是终态。
