# FS-001 Profile Apply 错误成功失败关闭设计与实施范围

> 文档状态：Phase 3M 已实施 / 验证完成；生产门禁仍 OPEN  
> 最后核对：2026-08-24（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3L 整改  
> 核对范围：当前 route、Saga、schema、Postgres repository、runtime active-parameter loader、单元测试与 Phase 2/3 审计证据  
> 运行时边界：未读取 `.env.*`，未连接真实账户、交易所或数据库，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

> 实施证据：[`33-fs-001-profile-apply-fail-closed.md`](../../audit/full_system_2026_08_24/33-fs-001-profile-apply-fail-closed.md)

## 1. 业务目标与边界

本阶段关闭 `FS-001` 相邻但同源的 profile apply 错误成功路径：当前 endpoint 可以在 research/live 数据写入并不等价于交易 runtime 生效、且没有 worker readback 的条件下返回 `ok=true`。在存在 execution-owned 激活协议前，profile apply 必须与 rollback 一样 fail-closed：完成认证、token actor 绑定、recommendation 状态与双人签署检查后，明确返回未实现，且不创建 Saga、不写 research/live 数据、不推进 recommendation 终态。

本阶段不把停用入口描述为“真实激活已修复”，不删除历史 Saga 代码，不尝试在线修改 live profile，不读取生产配置，不部署。`FS-001` 仍是上线硬阻塞，状态只能收敛为“错误成功路径已封闭；真实 apply/rollback 与 runtime readback 未实现”。

## 2. 当前行为与根因

当前 `apply_profile_rec` 在安全校验后执行四步 Saga：写 `governance.active_parameter_sets(scope='profile')`、写 apply history、修改 live `strategy_profile_activation.payload`、写 live history，最后把 recommendation 标记为 `applied` 并返回 `ok=true`。

逐层代码核对证明这不能构成真实生效证据：

1. Saga 用 `payload->>'profile_id'` 定位 live activation，但现行 `StrategyProfileActivationState` 的字段是 `active_profile_id`，repository 也只持久化该模型；
2. Saga 写入的 `strategy_entry_min_signal_edge_bps`、`strategy_entry_alpha_min`、`strategy_min_net_edge_bps` 均不是 activation schema 字段；repository 通过 `StrategyProfileActivationState.model_validate()` 重建时不会把这些额外字段纳入领域状态；
3. runtime active-parameter loader 查询全部 `active_parameter_sets` 后仅以 `family + timeframe` 建键，profile scope 行的这两个字段按 migration 允许且由 Saga 写成 `NULL`；代码没有把 `scope='profile'/scope_ref` 映射到指定 worker/profile 的加载协议；
4. 未找到 apply generation、目标 worker ack、内存态 digest 或 readback 证明；
5. 四步均完成只证明 SQL 路径完成，不能证明交易决策采用目标参数，因此 `ok=true/applied` 是不受证据支持的终态。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `rdp_profile_routes.apply_profile_rec` | 保留身份、token、状态与双人签署校验；明确拒绝尚无安全实现的 apply |
| `governance.recommendations` | 保持 `released`；本阶段不写 `applied` |
| `apply_saga_operations` | 不创建、不续跑；历史行只作为历史证据 |
| `active_parameter_sets` / apply history | 不修改，不声称 profile scope 行已被 runtime 消费 |
| live activation / history | 不修改，避免写入非领域字段或伪造 activation 事件 |
| 审计与现行文档 | 记录错误成功已收口及真实激活仍开放 |

领域状态语义：若交易 runtime 未被证明改变，recommendation 就不能从 `released` 进入 `applied`。research/live SQL 完成、UI 提示或审计标签均不能替代 worker readback。

## 4. 输入/输出接口

输入契约保持：`rec_id`、`X-Rdp-Apply-Token` 与当前 session principal。认证、token、actor、recommendation 状态及双签失败继续使用现有 `403/404/409`。

通过上述只读安全校验后，固定返回 HTTP `501 Not Implemented`：

```json
{
  "detail": {
    "code": "profile_apply_not_implemented",
    "message": "Profile 参数应用暂不可用：安全的运行时激活、代际确认与读回尚未实现。",
    "recommendation_id": "...",
    "current_status": "released",
    "retryable": false
  }
}
```

不得返回 `ok=true`、`status=applied`、Saga operation id、steps completed 或任何暗示 runtime 已采用参数的字段。

## 5. 数据库 schema、表、索引与约束

无 schema、table、index、constraint 或 migration 变更。本阶段验收条件是 profile apply 请求“零数据库写入”。真正实现需单独设计 operation generation、目标 runtime/worker、from/to digest、CAS/version、ack/readback 与失败状态，不能继续用现有四个 SQL step 充当生效证明。

## 6. 事务、一致性与并发

本阶段没有跨库事务。路由只读 recommendation 以校验 `released` 状态和 approver，然后抛出 HTTPException；不检查或打开 live pool，不创建 Saga operation，不 commit。并发请求均保持原状态且无写冲突。

后续真实实现至少需要：同一 profile 的互斥/CAS、不可变目标 digest、单调 generation、execution-owned apply、目标 worker ack、内存态 readback、持久化完成态，以及部分失败后的可恢复 pending/failed 状态。

## 7. 授权、认证与数据安全

保留 `require_write_access`、v2 token 的 `action=apply/scope=profile/recommendation_id` 绑定、session actor 一致性以及 approver/applier 分离。响应和日志不包含 token、凭证、DB URL、parameter values 或账户信息。

失败关闭不是绕过授权的理由：无效请求仍应先得到原有安全错误；只有通过全部只读安全检查的请求得到固定 501。

## 8. 错误处理与幂等

- 缺失/无效 token：现有 `403`；
- token actor 与 principal 不一致：现有 `403`；
- recommendation 不存在：现有 `404`；
- recommendation 非 `released`：现有 `409`；
- approver 与 applier 相同或缺少 approver：现有 `409`；
- 安全检查通过：稳定 `501/profile_apply_not_implemented`；
- 重复请求：每次均无写入、无状态推进、无 Saga 续跑，天然幂等。

不再用 `live_pool_not_ready` 掩盖更根本的“不存在安全激活协议”；501 应与 live pool 是否初始化无关。

## 9. 状态转换与生命周期

```text
released --apply request--> HTTP 501 + remains released
draft/approved/applied/other --apply request--> HTTP 409 + remains unchanged
```

本阶段不引入 `apply_pending`，因为当前没有消费者、目标 worker 或 readback 状态机。历史上已经产生的 `applied` recommendation 和 Saga 数据不在本阶段自动改写；其真实运行时一致性必须在上线前通过独立迁移/对账方案处理。

## 10. 缓存与性能

无新缓存、消息、广播或 runtime reload。请求完成一次 governance recommendation 只读后立即失败，且不再打开 live DB session，减少错误路径的资源占用。

## 11. 日志、监控与审计

记录 warning 级拒绝事件，仅包含 recommendation id、当前状态和 actor。事件名/文本必须表示“请求被失败关闭”，不得表示 apply 已开始、已排队或已完成。审计材料记录代码事实、测试证据和未关闭条件，不把静态测试推断为生产运行状态。

## 12. 测试策略

新增直接路由单元测试覆盖：

1. OpenAPI 声明 501；
2. 合法 token/actor、`released` recommendation、双人授权返回固定 501；
3. 不调用 live pool、parameter-set loader、patch 计算、Saga finder、Saga executor或 live session；
4. governance session 只发生既有 recommendation 读取替身，不出现 execute/commit 写入；
5. recommendation 非 `released` 仍返回 409；
6. 双人签署失败仍优先返回 409；
7. 重复请求不产生副作用；
8. 响应不包含 `ok`、operation id、steps 或 parameter values。

同时运行 FS-001 rollback、profile token/route、旧 Saga 单元回归、受影响相关集和全量单测，并执行 Ruff。旧 Saga 单元测试只证明其内部历史行为，不作为 endpoint 可启用证据。

## 13. 迁移、回滚与兼容

无 DB migration。HTTP 成功契约有意收紧：依赖旧 `200/ok=true` 的调用方必须把 `501` 视为“未应用”。不提供恢复旧错误成功语义的生产回滚方案；若客户端不兼容，保持 NO-GO，先修正客户端和真实激活协议。

保留 `profile_apply_saga.py` 以避免在风险收敛阶段删除历史恢复/审计证据，但 route 不再调用它。后续替换时应在单独迁移中明确废弃或重构。

## 14. 配置与环境隔离

不新增环境变量、feature flag、override 或凭证。测试使用 fake session、Request、Principal 与 mock，不读取 `.env.*`，不访问 research/live DB、Redis、交易所、Docker 或 WSL2。

## 15. 代码组织与依赖

预计修改：

- `aats/api/rdp_profile_routes.py`；
- 新增 `tests/unit/test_fs001_profile_apply_fail_closed.py`；
- `audit/full_system_2026_08_24` 的 FS-001 证据与总状态；
- 声称 profile apply 已可用的现行 README、架构/API/运维文档；
- 本 SOW 与任务索引。

不修改 activation schema、runtime loader 或 Saga 内部实现，不新增第三方依赖，不触碰无关功能。

## 16. 文档、运维手册与验收标准

现行文档必须明确：profile recommendation 的 approve/release 仍是治理工作流；apply 和 rollback 均失败关闭；combo active-parameter 流程不能被错误类推为 profile runtime 激活。历史设计文档保留，但必须由现行入口标明已过时边界。

本阶段验收：

- 合法 apply 请求不再得到成功响应；
- 不创建/续跑 Saga，不写 recommendation、active set、history 或 live activation；
- token、actor、状态和双签校验未弱化；
- 直接、相关、全量测试与 Ruff 通过，或对环境阻塞作准确披露；
- 审计状态只记为风险收敛，不把 FS-001 标为关闭；
- REAL-MONEY PRODUCTION 继续 NO-GO。

真正关闭 FS-001 仍需：确定 execution-owned profile 参数领域模型，明确 profile revision 与参数集的关系；用 migration 固化 operation/generation/digest/ack；由目标 worker 原子加载并回报其实际内存态；将 research、live authority、history 与 runtime readback 对齐到同一 generation；实现同等协议的反向 Saga、并发 CAS、超时失败与恢复验证。
