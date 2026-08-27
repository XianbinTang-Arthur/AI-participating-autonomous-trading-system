# 参数治理（Parameter Governance）

> 文档状态：现行运维专题参考
> 最后核对：2026-08-27（静态起始 HEAD `main@9c4112c6d769735f171971c8fa4f2cae5a03a824`，含尚未部署的控制面收口候选）
> 核对范围：recommendation、release、active parameter、效果评估、风险回滚收敛与 Operator API；运行值仍以目标数据库、runtime provenance 和部署证据为准。

本页区分候选参数 registry、发布控制面与 runtime active parameter 真源，避免把 frozen、JSON 镜像或单个 API 返回值误认为已经生效。

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

1. recommendation 在数据库可用时以数据库 registry 为 canonical；JSON 只是在数据库提交成功后，从 canonical registry 重读生成的审计镜像。镜像损坏、并发 CAS 耗尽或文件系统失败会明确降级，但不能撤销已提交的数据库状态，也不能阻断后续 release 创建。
2. `parameter_registry.py`、snapshot 等治理模块仍可能采用 DB-first + 文件审计副本/故障降级读取；各对象必须按自身代码确认，不能由 recommendation 的策略类推。
3. `aats/bootstrap/active_parameters.py` 的主交易 runtime loader 是 DB-only：只读 `governance.active_parameter_sets`，不会从 `configs/active_parameter_sets/*.json` fallback。

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

直接 `POST /rdp/parameters/apply` 已在所有环境退役：它固定以 `release_required` 无写入失败，不能再作为前向变更或故障恢复入口。当前两条人工前向入口是：

- `POST /rdp/releases/create`；
- `POST /rdp/recommendations/{id}/approve-and-release`。

二者在实际执行 apply 时都需要 `action=apply` 的短时 HMAC token；组合端点的 token 校验在 approve/release 首次写入之前完成，`skip_apply=true` 除外。Operator 直接 rollback 需要 `action=rollback` token。

效果评估判定必须持久化到数据库 canonical registry；JSON 是可降级镜像。若评估要求风险回滚，Operator rollback 与 observation cycle 内部风险收敛是两条不同路径：前者受 session、角色与 action token 保护；后者只能依据 canonical release、apply history、active set/decision 和 application insert-once action proof 在数据库中精确收敛，不能伪造 Operator token，也不能把调用方传入的 boolean 当成证明。该 proof 表当前没有禁止 UPDATE/DELETE 的数据库 trigger，因此不能称为数据库不可变账本。

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

- 查询：`GET /rdp/parameters/active`、`GET /rdp/recommendations/latest`、`GET /rdp/readiness`、`GET /rdp/releases/latest`、`GET /rdp/releases/history`、`GET /rdp/parameters/apply-history`。
- 审批：recommendation write API。
- 发布/回滚：[Parameter Apply 与 Rollback](parameter_apply_and_rollback.md)。
- 完整生产流程：[Production Parameter Change Runbook](production_parameter_change_runbook.md)。
