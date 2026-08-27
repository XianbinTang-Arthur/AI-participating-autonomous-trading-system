# RDP 控制面真值收口与资本证据血缘加固任务书

> 文档状态：`IMPLEMENTED / VALIDATION_PASSED`
>
> 制定日期：2026-08-27
>
> 代码基线：起始 `main@9c4112c6`；本文描述其上的 RDP 控制面收口候选，以当前代码、测试及下述验证记录为真值。
>
> 安全边界：仅允许代码、测试、文档和只读核验；不得启动 live profile、触发真实订单、应用参数到 live、读取凭证、push 或把外部审批标记为完成。

## 1. 业务目标与边界

本任务关闭 RDP 发布、观察、晋级与自动回滚控制面中仍可导致错误放行、错误展示、假失败或风险证据丢失的 P1。目标不是提高策略收益，也不改变研究算法，而是保证任何参数推进或风险收敛动作都由同一份精确、时序正确、可追溯的数据库真值授权。

范围内缺口：

1. promotion readiness 对失败、不完整、陈旧、未来时间或轮次身份不符的快照错误显示可用；
2. Gate 使用 recommendation 创建时间代替精确 evidence round 完成时间；
3. recommendation 数据库状态提交后，JSON 审计镜像失败被误报为业务失败；
4. observation 各阶段共用异常边界，已生成的有效风险可能无法物化；
5. observation/rollback risk evidence 缺少版本化 source provenance，旧 payload 可被错误用于资本回滚；
6. effectiveness 通用 JSONB writer 可伪造终态，且 legacy 单布尔值会错误解除 apply veto；
7. rollback 资本事务与 effectiveness 终态事务之间缺少独立数据库证明锚点，并发 apply 可能穿过两事务缝隙；
8. Operator 已完成回滚会被 enforcer 误标成 active-change cancellation，终态核验失败后永久卡住；
9. 单 release CLI 的 `--enforce` 会意外处理其他 pending release，动作范围超过显式选择。

非目标：策略参数优化、历史数据伪回填、Research OS G0 人员/许可/预算审批、live 部署与真实资金验证。

## 2. 模块职责与领域模型

- `promotion_qualification.py`：验证 recommendation 与精确 Phase 6 round 的身份、状态、完成时间及资格策略。
- `gate_rules.py` / `pre_apply_gate.py`：只消费精确 evidence round 的 canonical 完成时间，不以 recommendation 时间替代。
- `rdp_queries.py`：构建只读 readiness 投影；不产生授权，不把 audit-only 快照显示为可推进。
- `recommendation_registry.py` / `rdp_routes.py`：PostgreSQL 为状态真值；JSON 仅为可降级审计镜像。
- `observation_window.py` / `rollback_policy.py`：从 post-apply canonical 来源生成 versioned evidence。
- `release_effectiveness.py` / `release_registry.py`：验证 evidence contract、风险优先级和资本动作前置条件。
- `observation_cycle.py`：逐 release、逐阶段隔离故障，并保证有效风险仍进入 effectiveness materialization。

核心领域对象：`Recommendation`、`DecisionRoundSnapshot`、`ParameterRelease`、`ObservationResult`、`RollbackRecommendation`、`ReleaseEffectiveness`、`RollbackActionAttempt`、`ReleaseEffectivenessActionProof`。

## 3. 输入、输出与公共接口

- 保持现有 HTTP 路径和主要响应字段兼容；新增状态只能作为扩展字段。
- readiness 输入必须包含精确 `round_id`、Phase 6 manifest、`status=succeeded`、canonical `finished_at` 和现行 promotion policy。
- `finished_at` 接受任意带显式时区偏移的 ISO 8601 值并统一归一到 UTC；naive、未来或 snapshot/manifest 非同一瞬间的时间继续失败关闭。
- post-apply evidence 新增 `evidence_contract_version` 与 `source_provenance`；每个可触发风险的检查项必须有 `source_kind`、`source_id`、`source_timestamp` 和必要的 source contract/version。
- 对旧版或不完整 evidence，输出 `insufficient_evidence` / `reconciliation_required`，不得输出可执行 rollback authorization。

## 4. 数据库、表、索引与约束

本轮复用以下现有表及 JSONB payload：

- `governance.decision_round_snapshots`
- `governance.recommendations`
- `governance.parameter_releases`
- `governance.observation_results`
- `governance.rollback_recommendations`
- `governance.release_effectiveness`

审查证明仅靠 mutable JSONB 无法同时关闭“伪造终态”和“后续 active 变化复活旧义务”两类缺口，因此批准新增一张应用层 insert-once 证明表：

- `governance.release_effectiveness_action_proofs`：每个 release/attempt 仅一条；保存 outcome、proof kind、精确起止时间、rollback operation/target 或 active-change/soft-pause 事实；通过 FK、两个唯一约束和 proof-shape CHECK 限制。

该表由 `RdpBase.metadata.create_all()` 幂等创建，不修改既有表和历史行。终态 writer 在持有 combo lock 的同一事务中先复核 canonical release、apply history、active set 或 active decision，再 insert-once 写 proof，最后写 effectiveness 投影；caller 提供的 `proof_verified` 会被丢弃。标量身份列继续作为查询真值，版本化 provenance 存入 JSONB。当前数据库约束包含 FK、UNIQUE 与 proof-shape CHECK，但没有禁止 UPDATE/DELETE 的 trigger，因此本任务不声称数据库级不可变；运维写权限仍必须受控。

## 5. 事务、一致性与并发

- recommendation 状态 CAS 在数据库事务提交后即为 canonical；镜像失败不得回滚或伪装成 CAS 失败。
- 参数、release、observation、rollback evidence 和 effectiveness 继续使用既有 combo advisory lock 与行锁。
- rollback action 必须遵循 `pending -> in_progress -> enforced|cancelled|reconciliation_required`，同一 attempt ID 才可收口。
- rollback 资本事务提交后、application insert-once action proof 写入前，raw risk 必须持续阻断 apply；`parameter_releases.observation_status=rolled_back` 不能单独解除义务。
- terminal resolver 只接受 JSONB 投影与 application insert-once proof ledger、release 和 exact apply history 一致的记录；不能依赖随后会变化的 current active/decision。
- raw risk 首次触发时间保持稳定，重复 producer 不得刷新后使已证明的 terminal action 重新变成 pending。
- Operator 已完成的精确回滚收口为 `enforced + proof_kind=rollback`，而不是 cancellation；仅有 rolled_back 状态不足以证明完成。
- observation cycle 保留全量 pending 收敛；单 release CLI 必须传入精确 release filter。

## 6. 授权、认证与数据安全

- 不改变现有 session、write access、operator token 和 apply token 规则。
- readiness、镜像与 provenance 修复不能绕过 approve/release/apply 资格检查。
- approve-and-release 的进程内晋级授权是一次短期 capability：最长有效 5 分钟，且绝不超过精确证据 168 小时寿命；过期、未来签发、naive 或身份漂移全部在任何参数读取/写入前失败关闭。
- 日志、测试输出和文档不得包含数据库 URL、密码、token 或私有账户数据。

## 7. 错误处理与幂等性

- malformed/unknown/legacy 数据失败关闭，但不允许一个无效 sibling evidence 掩盖另一份有效高风险证据。
- 已提交 DB 状态后的文件镜像错误记录结构化 degraded 状态并返回 canonical 成功，不抛出假 500。
- observation 每一阶段独立捕获异常；已有 canonical risk 时仍必须尝试 effectiveness materialization。
- 同一 evidence/release/action 的重复执行依赖 fingerprint、CAS、锁和 attempt ID 保持幂等。

## 8. 状态迁移与生命周期

- Phase 6 readiness：`unavailable/audit_only -> available` 仅在精确轮次全部契约通过时成立。
- Evidence：`legacy|invalid -> reconciliation_required`；`v1 + post-apply provenance -> valid`。
- Recommendation：DB CAS 成功后状态不可因镜像失败退回。
- Release effectiveness：禁止首写终态和 `pending -> terminal` 跳转。
- Action proof：`不存在 -> insert once`；无 update/delete 业务接口，但当前没有数据库级禁止 UPDATE/DELETE trigger。legacy `status=NULL + terminal boolean` 永远按 unresolved/reconciliation 处理。

## 9. 缓存与性能

- 精确 round 查询按主键/唯一 round ID 完成；不得扫描所有历史目录或快照。
- 同一 gate/readiness 请求复用已经加载的 snapshot，避免重复数据库连接。
- provenance 只保存必要身份、时间和版本，不复制大型研究 payload。

## 10. 日志、监控与审计

- 统一输出不含秘密的 reason code：`promotion_round_*`、`evidence_source_*`、`recommendation_mirror_degraded`、`release_observation_stage_failed`。
- API 响应明确区分 canonical DB transition 与 mirror refresh 状态。
- 自动回滚的 source provenance、action attempt 和 terminal audit 必须可从 DB 重建。

## 11. 测试策略

至少覆盖：

- failed/incomplete/stale/future/wrong-round/wrong-phase readiness；
- 新 recommendation 包装旧 evidence round；
- DB CAS 成功后 OSError 与 JSON version CAS 冲突；
- observation 已写 rollback risk、后续 evaluator 抛错，effectiveness 仍被调用；
- legacy risk payload 无 provenance 被拒绝执行；v1 post-apply provenance 通过；
- 非 UTC aware 时间归一、naive 时间拒绝；
- 晋级授权在 5 分钟短 TTL 或精确证据 168 小时寿命任一先到时失效，且失效后 release/apply 零写入；
- apply/rollback capital lineage proof 的成功与任一证明缺失失败。
- rollback 已提交但 action proof 尚未写入时并发 apply 仍阻断；proof 写入后连续多次合法 active 变化不复活历史义务；
- malformed/future/reversed action timestamp 不抛查询异常且保持 unresolved；history/decision 事实时间必须落在 attempt 窗口内；
- proof 表存在、列、FK、唯一约束和 proof-shape CHECK 与 ORM 一致。
- Operator rollback 先完成、与 enforcer 竞态完成及证据不完整三类分支；单 release CLI 不得触碰 sibling pending release。

验证顺序：Ruff -> 最窄单元 -> RDP 相关单元集合 -> Windows 全量 unit -> WSL2 最窄 integration/真实 PostgreSQL -> 规定入口的 derivatives 模拟验证。

## 12. 迁移、回滚与兼容性

- 不原地补写或伪造旧 evidence provenance；旧记录保持审计可见并进入 reconciliation。
- 新 contract 由 producer 自然生成；历史 release 如需继续观察，必须基于新的 post-apply canonical 来源重新评估。
- 部署前由现行 schema 初始化入口幂等创建 `release_effectiveness_action_proofs`，并核验表、FK、唯一约束和 CHECK；不为 legacy terminal 伪造 proof，缺 proof 的记录进入 reconciliation。
- 回滚策略为停止新 proof 写入并保留既有账本审计数据；禁止 DROP/TRUNCATE 证明表。旧版本代码回退前必须保持 apply veto 或由 operator 完成逐条 reconciliation，不能恢复 release-only/boolean-only 放行。
- 回滚代码变更时不得恢复 file fallback、created_at freshness 或无 provenance 的资本执行。

## 13. 配置与环境隔离

- managed 环境只接受 PostgreSQL 真值；纯离线测试可使用明确 mock/file fixture。
- 不增加无消费者配置开关，不读取 `.env.*` 内容。
- live profile 保持硬禁用；模拟部署只能在代码提交且所有门禁通过后使用规定入口。

## 14. 代码组织与依赖

- 时间解析复用 governance canonical UTC 工具。
- 数值 evidence 共用严格 finite/range parser。
- provenance validation 收敛为共享 helper，避免 observation、effectiveness、enforcer 三套漂移规则。
- 不引入新第三方依赖。

## 15. 文档与运维手册

完成后同步 `docs/rdp/`、`docs/operations/rdp_operator_workflow.md`、参数 apply/rollback 运行手册和任务索引；明确旧 evidence 只能审计、DB/mirror 状态边界以及人工 reconciliation 步骤。

## 16. 部署与验收标准

只有同时满足以下条件才可标记完成：

1. 独立 code review 无未关闭 P0/P1；
2. 所有上述负向矩阵与资本 happy path 通过；
3. 当前 Windows 全量 unit 和 WSL2 相关 integration 通过；
4. 现有 legacy DB 状态被如实隔离或完成非伪造对账；
5. derivatives 模拟端到端、健康、日志和回滚证据通过；
6. 文档、API、数据库行为和 UI 状态一致；
7. 无 live、真实订单、真实资金或凭证副作用。

## 17. 2026-08-27 验证记录

- 独立最终复审：`ACCEPT`，无未关闭 P0/P1/P2；复核覆盖单 release enforcement scope、人工回滚证明收敛、短期晋级授权和非零 UTC offset。
- Windows 全量单元：`5595 passed, 30 skipped, 259 subtests passed`；仅保留既有 Python 3.12 SQLite datetime adapter deprecation warnings。
- 晋级资格/授权定向回归：`84 passed`；授权最长 5 分钟且不越过证据 168 小时寿命，过期 release 在参数读取前零写入。
- WSL2 RDP API/no-DB integration：`38 passed`；真实 PostgreSQL 回滚与证明 integration：`17 passed`。
- Ruff（`aats/` 与全部改动 Python 文件）、`git diff --check`、30 份改动 Markdown 本地链接检查均通过。
- 历史恢复 campaign `60e46f5e-e3e0-4090-b141-b53c92f1aa71` 的旧版 v1 30 日流程已 `SUCCEEDED`，但它不满足当前 v2 fencing、immutable Silver、source-aware Gold 与 row verifier 合同，因此不得据此解除当前 NO-GO 或宣称历史恢复已经按现行标准完成。
- 本状态只表示代码与静态/单元/集成验证完成；derivatives 模拟发布仍必须由仓库规定入口执行并单独保留运行证据，且不授权 live、真实订单或参数应用。
