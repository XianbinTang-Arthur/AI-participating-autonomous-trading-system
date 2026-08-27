# Parameter Apply 与 Rollback 操作指南

> 文档状态：现行操作说明
> 最后核对：2026-08-27（起始 HEAD `9c4112c6`，含当前 RDP 控制面收口候选；以本文档所在 HEAD 为准）
> 核对范围：当前 API、治理数据库 writer、观察/效果评估/风险收敛路径和测试；不证明现场数据库、容器或参数状态

> **Scope 边界：**本页的 `POST /rdp/parameters/apply|rollback` 是 combo
> `family + timeframe` 流程，不能类推到 `profile-recommendations/{id}`。Phase 3M 后
> profile apply 与 rollback 均在授权/状态/双签检查后无写入 `501`；approve/release
> 只推进研究治理状态。真实 execution-owned profile activation/readback 完成前，
> 不得用 profile endpoint 改变运行参数。

## 1. 关键事实

- runtime active parameter 唯一真源：Postgres `governance.active_parameter_sets`。
- recommendation 不会因为 approved 自动生效；只有 release/apply 成功才改变 active set。
- 参数前向写入必须建立 canonical release。`POST /rdp/parameters/apply` 已停用：它会先校验 apply token，随后固定返回 `ok=false, code=release_required`，不写 active set。
- 当前可执行的前向入口只有 `POST /rdp/releases/create` 和 `POST /rdp/recommendations/{id}/approve-and-release`；`skip_apply=false` 时都要求 Operator write access 与 action-bound `apply` token。
- Operator 直接回滚 API 要求独立的 action-bound `rollback` token。内部风险收敛任务不是 Operator API，不消费浏览器 token；其授权来自精确 release/provenance、combo lock、attempt 状态机和数据库终态证明。
- 组合审批发布只接受进程内、完整 recommendation 身份绑定的晋级 capability；其有效期取“签发后 5 分钟”和“精确 Phase 6 证据满 168 小时”二者较早值，过期或时间结构异常时必须在参数读取和写入前拒绝。
- token actor 在启用认证时必须等于当前 session identity，且 token 受 HMAC、action 和 TTL 约束。
- `POST /rdp/releases/create` 与 `POST /rdp/recommendations/{id}/approve-and-release` 在 `skip_apply=false` 时同样要求 `action=apply` 的短时 token；token 校验发生在 release/approve 写入之前。`skip_apply=true` 只创建治理记录，不要求 apply token。
- `release_cycle` 自动调度已禁用且禁止任务入队。

## 2. 变更前置条件

在任何前向参数变更前逐项确认：

1. recommendation 存在、状态可转换；apply-capable recommendation 必须绑定精确 `evidence_bundle_ref`，对应 Phase 6 成功 round、当前资格策略和有效期均通过；
2. Step2 integrity 没有 blocking reason；
3. attribution、execution realism、readiness 支持该变更；
4. `/rdp/health`、`/system/health`、recovery、reconciliation 可接受；
5. pre-apply gate 允许；
6. actor、notes、observation window 和 rollback target 已记录；
7. 当前 active set 已备份为可审计数据库状态，不依赖本地 JSON。

## 3. 推荐的 Operator 流程

### 3.1 审批

通过 Operator UI 或认证 API 调用：

```text
POST /rdp/recommendations/{recommendation_id}/approve
```

request body 的 actor 不是认证开启时的审计真源；服务端会使用 session principal identity。

### 3.2 Gate

```text
POST /rdp/gates/run
```

Body 使用 `recommendation_id`。Gate 返回 block 时停止；不得通过 `skip_gate=true` 把生产 gate 变成可选项。

### 3.3 选择发布入口

当前只有两条可执行的前向路径：

| 路径 | 认证 | Token | 行为 |
| --- | --- | --- | --- |
| `POST /rdp/releases/create` | write access | `action=apply`（`skip_apply=false`） | gate + release + apply；支持 observation window |
| `POST /rdp/recommendations/{id}/approve-and-release` | write access | `action=apply`（`skip_apply=false`） | approve + gate + release + apply 组合入口 |

UI 会在 release 或 approve-and-release 前由当前 Operator session 自动申请 token。若直接调用任一会执行 apply 的组合端点，先申请 token：

```text
POST /rdp/operator-tokens
body: {"action": "apply"}
```

然后在短时有效期内调用 `POST /rdp/releases/create` 或
`POST /rdp/recommendations/{id}/approve-and-release`。不要调用
`POST /rdp/parameters/apply`：该兼容端点只会返回 `release_required`，不会产生写入。

```text
POST /rdp/releases/create
header: X-Rdp-Apply-Token: <sensitive-short-lived-token>
body: {"recommendation_id": "...", "notes": "...", "skip_apply": false}
```

不要把 token 复制进文档、日志、工单或持久化 shell history。

### 3.4 发布后验证

- `GET /rdp/parameters/active`：combo、parameter_set_id、version 符合预期；
- `GET /rdp/parameters/apply-history`：actor、action、from/to、notes 可追踪；
- `GET /rdp/releases/latest`：gate/apply/observation 状态一致；
- 主交易进程重建后 Settings Provenance 显示 active parameter 注入；
- `/system/health`、recovery、reconciliation、决策频率、订单、fee/slippage 无异常；
- observation window 到期后运行/检查 observation。

## 4. Rollback

### 4.1 两类回滚路径

- **Operator 直接回滚**：由当前认证 session 申请 `rollback` token，再调用
  `POST /rdp/parameters/rollback`。这是人工处置入口。
- **内部定时风险收敛**：启用的 `observation_cycle` 在持久化模式下会调用
  `enforce_pending_rollbacks()`；`scripts/rdp_evaluate_release_effectiveness.py`
  只有显式传入 `--enforce` 才会调用同一 enforcer，并严格限制为 `--release-id` 指定的
  单个 release。CLI 默认只保存评估，`--dry-run` 则不保存也不执行。风险收敛不等于
  自动发布，也不绕过资本门禁。

内部 enforcer 仅在以下事实同时成立时执行：release 与原 apply history 精确匹配、
observation/rollback evidence 使用可验证的 post-apply provenance、目标 combo 锁可取得，
且当前记录是从未尝试过的 clean `pending`。动作先写
`pending -> in_progress` attempt 锚点，再执行以下三种之一：

1. 精确回滚到 release 的 previous parameter set；
2. 若 active set 已被其他合法动作改变，以零资本写入取消旧意图；
3. 无合法回滚目标时写入 combo soft pause。

任何 legacy、缺 provenance、畸形、重复/中断 attempt 或结果无法证明的记录都会进入
`reconciliation_required`，不会自动重放。终态必须由数据库 writer 重新推导资本事实，
并在同一事务写入应用层 insert-once 的
`governance.release_effectiveness_action_proofs`；仅有 JSON boolean、release 的
`observation_status=rolled_back` 或调用方自报 `proof_verified` 都不能解除 apply veto。
从 raw risk 出现到精确终态证明落库期间，同 combo 的新 apply 必须持续阻断。

若 Operator 已先完成精确回滚，下一轮 enforcer 不会再次执行资本动作，也不会把它误标成
`active_parameter_changed` cancellation。只有 release 为 `success/rolled_back`、release 中
rollback operation/target、对应 rollback history 的 from/to/family/timeframe/actor/time 以及
当前 active=target 全部一致，才会把待处理 effectiveness 收口为
`enforced + proof_kind=rollback`；任一事实缺失均进入 reconciliation 并继续阻断 apply。

### 4.2 何时人工回滚

- gate 后才出现输入或 runtime 漂移；
- observation 建议 rollback；
- 交易行为、费用、滑点、回撤、reconciliation 或 blocker 明显退化；
- active set 与 release/history 不一致；
- Operator 无法解释当前 active 值来源。

安全回滚路径即使 Step2 降级也必须可用；代码不会把前向变更的 Step2 integrity gate套到 rollback。

### 4.3 获取 rollback token

由当前 Operator session 调用：

```text
POST /rdp/operator-tokens
body: {"action": "rollback"}
```

### 4.4 执行

```text
POST /rdp/parameters/rollback
header: X-Rdp-Apply-Token: <sensitive-short-lived-token>
body:
  family: independent | directional
  timeframe: 15m | 1h
  to_parameter_set_id: <optional-explicit-target>
  notes: <required-operational-context>
```

不指定 target 时服务会尝试上一版本。以下情况返回 422：validation failed、无上一目标、无 active set、环境禁止。

### 4.5 回滚后验证

1. active set 已指向预期 target；
2. apply history 出现 rollback 且 actor 正确；
3. release/observation/rollback recommendation 关联可追踪；
4. runtime 重建后实际生效值与数据库一致；
5. 主交易 health、reconciliation、订单和风险恢复；
6. 记录触发原因、影响窗口和后续修复。

## 5. 已禁用入口

以下脚本当前是硬禁用兼容桩，执行会打印替代路径并退出 2：

- `scripts/apply_active_parameter_set.py`
- `scripts/approve_recommendation_and_apply.py`
- `scripts/rdp_rollback_active_parameter_set.py`
- `scripts/rdp_freeze_parameter_set.py`
- `scripts/rdp_run_release_cycle.py`

不得把它们的旧参数写入现行命令示例。`configs/active_parameter_sets/active_parameter_registry.json` 也不是 runtime fallback 或人工恢复入口。

## 6. 失败语义

| 症状 | 处理 |
| --- | --- |
| `missing_apply_token` / `invalid_apply_token` | 重新由当前 session 申请正确 action 的 token；不要复用他人 token |
| `actor_mismatch` | token actor 与 session identity 不一致，停止并重新签发 |
| `integrity_blocked=true` | 修复 Step2 evidence/integrity，不能继续前向 apply |
| `promotion_qualification` 阻断 | 使用 recommendation 精确引用的 Phase 6 round 修复证据；不能用“最新 round”替旧 recommendation 背书 |
| `blocked_by_gate` | 保留 release 记录，修复 gate 原因后重新评估 |
| `release_required` | direct apply 已停用；改用 release/approve-and-release 建立 canonical release |
| apply failed | active/history/release 三方核对，禁止盲目重试 |
| `mirror_status=degraded` | DB CAS 可能已经成功；以 canonical DB 状态为准，修复 JSON 审计镜像，不能重复提交状态迁移 |
| pending rollback / `reconciliation_required` | 保持同 combo 前向 apply 阻断；核对 release、apply history、active set、attempt 和 proof ledger，不得手改 boolean 或重放资本动作 |
| DB loader error | runtime 已退化到 profile 参数；停止发布，恢复 DB 真源后重建和核对 |
| no previous rollback target | 不猜测、不从 JSON 导入；人工确认合法 parameter_set_id 后显式回滚 |

## 7. 相关文档

- [平台运行手册](platform_runbook.md)
- [生产参数变更 Runbook](production_parameter_change_runbook.md)
- [Managed Profile 配置说明](../configuration/managed-config-reference.md)
- [RDP 总览](../../aats/data_platform/README.md)
