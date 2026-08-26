# 参数治理（Parameter Governance）

> 最后核对：2026-08-22（代码基线 `be9179e`）。本页区分候选参数 registry 与 live active parameter 真源，避免旧文档把 frozen、JSON 副本和 runtime 生效混为一谈。

## 1. 三层对象

| 层 | 对象 | 是否影响 runtime |
| --- | --- | --- |
| 研究证据 | replay、round、candidate、artifact、verdict | 否 |
| 候选治理 | parameter set、recommendation、active decision、readiness | 否；只是可审批对象 |
| 生产生效 | `governance.active_parameter_sets` + apply history/release | 是；下次 `build_runtime()` 从 DB 注入 |

`frozen` 只表示候选参数版本被冻结，绝不等价于 live 已生效。判断当前值只能查 `GET /rdp/parameters/active`、数据库 active set、apply history 和 runtime provenance。

## 2. Parameter set 生命周期

```text
draft -> candidate -> frozen -> released/deprecated
```

具体状态约束以 `governance.parameter_sets` 的迁移/代码 allowlist 为准。候选版本应包含：family、timeframe、values、source round/evidence、confidence、创建/冻结/废弃信息。

旧的 `scripts/rdp_freeze_parameter_set.py` 已被硬禁用，当前没有受支持的手工 import/freeze/deprecate CLI。若 UI/API 尚未覆盖需要的 registry 维护动作，不得直接改 DB；应先实现受控入口、权限、审计和测试。

## 3. Recommendation 生命周期

Recommendation 聚合 parameter set 与跨阶段证据，常见动作：

- draft → approve；
- draft → reject；
- draft/approved → supersede；
- approved → release/apply。

当前 Operator API：

- `POST /rdp/recommendations/{id}/approve`
- `POST /rdp/recommendations/{id}/reject`
- `POST /rdp/recommendations/{id}/supersede`
- `POST /rdp/recommendations/{id}/approve-and-release`

认证开启时，审计 actor 来自 session principal，不信任 request body 的 actor。

## 4. 存储语义

必须区分两类实现：

1. `parameter_registry.py`、recommendation/snapshot 等治理模块仍可能采用 DB-first + 文件审计副本/故障降级读取。
2. `aats/bootstrap/active_parameters.py` 的主交易 runtime loader 是 DB-only：只读 `governance.active_parameter_sets`，不会从 `configs/active_parameter_sets/*.json` fallback。

因此：

- 文件副本不能证明 live 正在使用某参数；
- DB active set 不可用时，runtime 退化到 managed/profile 参数并记录 error；
- 禁止用旧 `seed-db` 命令或人工复制 JSON 作为恢复操作；
- 恢复后必须核对 DB、apply history、release、runtime provenance。

## 5. 生产发布约束

前向变更至少要求：

- approved recommendation；
- Step2 integrity 和 pre-apply gate；
- actor、release、notes、observation plan；
- previous target 和 rollback plan；
- 主交易 health/recovery/reconciliation 可接受；
- 发布后 runtime provenance 验证。

`/rdp/parameters/apply`、`/rdp/releases/create` 与 `/rdp/recommendations/{id}/approve-and-release` 在实际执行 apply 时都需要 `action=apply` 的短时 HMAC token；直接 rollback 需要 `action=rollback` token。组合端点的 token 校验在 approve/release 写入之前执行，`skip_apply=true` 除外。

自动 `release_cycle` 当前 disabled 且禁止入队。

## 6. 证据与可审计性

任何 parameter set/recommendation 至少能追溯：

- 输入数据集版本与窗口；
- replay 配置、成本模型和结果；
- live attribution 和 execution realism；
- quality/readiness/gate；
- reviewer/actor 和状态转换；
- release/apply/rollback history；
- observation 与最终结论。

缺失上述证据时只能继续观察或拒绝，不得通过“最近 frozen”推断其可上线。

## 7. 操作入口

- 查询：`GET /rdp/parameters/active`、`GET /rdp/recommendations/latest`、`GET /rdp/readiness`、`GET /rdp/releases/latest`、`GET /rdp/releases/history`。
- 审批：recommendation write API。
- 发布/回滚：[Parameter Apply 与 Rollback](parameter_apply_and_rollback.md)。
- 完整生产流程：[Production Parameter Change Runbook](production_parameter_change_runbook.md)。
