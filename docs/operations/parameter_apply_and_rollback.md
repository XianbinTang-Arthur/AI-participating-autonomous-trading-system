# Parameter Apply 与 Rollback 操作指南

> 最后核对：2026-08-24（起始 HEAD `00b6df0` + 未提交 Phase 3M 覆盖层）。本页描述 combo active-parameter API；不再使用已禁用的直写 CLI 或 JSON active parameter fallback。

> **Scope 边界：**本页的 `POST /rdp/parameters/apply|rollback` 是 combo
> `family + timeframe` 流程，不能类推到 `profile-recommendations/{id}`。Phase 3M 后
> profile apply 与 rollback 均在授权/状态/双签检查后无写入 `501`；approve/release
> 只推进研究治理状态。真实 execution-owned profile activation/readback 完成前，
> 不得用 profile endpoint 改变运行参数。

## 1. 关键事实

- runtime active parameter 唯一真源：Postgres `governance.active_parameter_sets`。
- recommendation 不会因为 approved 自动生效；只有 release/apply 成功才改变 active set。
- `POST /rdp/parameters/apply` 和 `POST /rdp/parameters/rollback` 同时要求 Operator write access 与 action-bound `X-Rdp-Apply-Token`。
- token actor 在启用认证时必须等于当前 session identity，且 token 受 HMAC、action 和 TTL 约束。
- `POST /rdp/releases/create` 与 `POST /rdp/recommendations/{id}/approve-and-release` 当前只要求 write access + Step2 integrity gate，**没有额外要求 apply token**；这是代码中的真实策略差异，不得在 runbook 中误写为“所有 apply 路径都需要 token”。如果要统一安全策略，应先修改代码和测试。
- `release_cycle` 自动调度已禁用且禁止任务入队。

## 2. 变更前置条件

在任何前向参数变更前逐项确认：

1. recommendation 存在、状态可转换、证据 lineage 完整；
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

当前有三条前向路径：

| 路径 | 认证 | Token | 行为 |
| --- | --- | --- | --- |
| `POST /rdp/parameters/apply` | write access | `action=apply` 必需 | 将已批准 recommendation 应用为 active set |
| `POST /rdp/releases/create` | write access | 当前不要求 | gate + release + apply；支持 observation window |
| `POST /rdp/recommendations/{id}/approve-and-release` | write access | 当前不要求 | approve + gate + release + apply 组合入口 |

运维默认使用 UI 暴露的受控组合入口；如果直接调用 `/parameters/apply`，先由同一 Operator session 申请 token：

```text
POST /rdp/operator-tokens
body: {"action": "apply"}
```

然后在短时有效期内调用：

```text
POST /rdp/parameters/apply
header: X-Rdp-Apply-Token: <sensitive-short-lived-token>
body: {"recommendation_id": "...", "notes": "..."}
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

### 4.1 何时回滚

- gate 后才出现输入或 runtime 漂移；
- observation 建议 rollback；
- 交易行为、费用、滑点、回撤、reconciliation 或 blocker 明显退化；
- active set 与 release/history 不一致；
- Operator 无法解释当前 active 值来源。

安全回滚路径即使 Step2 降级也必须可用；代码不会把前向变更的 Step2 integrity gate套到 rollback。

### 4.2 获取 rollback token

由当前 Operator session 调用：

```text
POST /rdp/operator-tokens
body: {"action": "rollback"}
```

### 4.3 执行

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

### 4.4 回滚后验证

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
| `blocked_by_gate` | 保留 release 记录，修复 gate 原因后重新评估 |
| apply failed | active/history/release 三方核对，禁止盲目重试 |
| DB loader error | runtime 已退化到 profile 参数；停止发布，恢复 DB 真源后重建和核对 |
| no previous rollback target | 不猜测、不从 JSON 导入；人工确认合法 parameter_set_id 后显式回滚 |

## 7. 相关文档

- [平台运行手册](platform_runbook.md)
- [生产参数变更 Runbook](production_parameter_change_runbook.md)
- [Managed Profile 配置说明](../configuration/managed-config-reference.md)
- [RDP 总览](../../aats/data_platform/README.md)
