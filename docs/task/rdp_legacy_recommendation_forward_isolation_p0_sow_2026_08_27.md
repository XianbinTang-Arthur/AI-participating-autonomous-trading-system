# RDP 旧 Recommendation 前向隔离 P0 任务书

> 文档状态：现行实施任务书（LF-A，代码级失败关闭）
>
> 首次核对：2026-08-27
>
> 实施基线：`main@9c4112c6d769735f171971c8fa4f2cae5a03a824`
>
> 真值优先级：当前代码、治理数据库快照、现行 artifact 合约、测试与运行证据；历史文档仅作线索
>
> 关联总任务书：`docs/task/rdp_derivatives_contract_arithmetic_p0_sow_2026_08_27.md`
> 风险边界：本任务不启动 live profile、不应用参数、不下单、不访问凭证、不 push

## 1. 决策摘要

本任务关闭一个独立的控制面 P0 缺口：历史 `parameter_upgrade` recommendation 即使没有
`phase2-promotion-metrics/v1` 资格策略，或其 `evidence_bundle_ref` 不能精确还原产生该建议的
decision round，仍可能被审批、创建 release 或应用。现行 pre-apply gate 还会检查“最新 round”，
而不是 recommendation 自己引用的 round；因此后来的健康 round 可能替旧建议背书。

本切片引入单一、共享、失败关闭的资格判定，并将其接入审批、gate、release、apply 和运营只读
展示。旧记录保持原始状态用于审计，不原地升级、不补写虚假证据；它们只能被拒绝或 supersede，
不得继续向资本参数状态推进。

## 2. 当前行为与风险

### 2.1 已核实行为

1. recommendation 仅保存 `source_round_id` 与字符串 `evidence_bundle_ref`；历史记录可能缺失其中
   任一字段。
2. `check_evidence_freshness()` 在 evidence 引用缺失时返回通过。
3. `check_evidence_completeness()` 和 `check_latest_round_health()`读取全局“最新 decision round”，
   不验证 recommendation 的精确引用。
4. approval API 只检查 Step 2 当前完整性；底层 `approve_recommendation()` 不检查 promotion 资格。
5. release 与 apply 依赖 status、审批元数据和通用 gate，无法独立证明被推进的是原始合格候选。
6. 2026-04 历史 decision round 的 evidence bundle 不含
   `phase2-promotion-metrics/v1`，因此只能作为审计材料。

### 2.2 失效模式

- 缺少 evidence 引用的旧建议被人工批准；
- recommendation 引用 round A，但 gate 使用较新的 round B；
- round 中没有该参数集、family/timeframe/symbol 对应的 `promote_candidate`，仍被发布；
- recommendation 的 `source_round_id` 与候选参数集来源不一致；
- Phase 2 evidence 使用旧聚合格式，却被历史 `available=true` 冒充为新资格证据；
- UI 将已 approved 但失格的旧建议展示为可发布，诱导 operator 反复操作；
- API 门闸存在但底层函数或脚本直接调用时可以绕过。

## 3. 目标与非目标

### 3.1 目标

1. 为 `parameter_upgrade` 建立一个可复用的 `PromotionQualificationVerdict`。
2. 资格判定必须绑定 recommendation 的精确 `evidence_bundle_ref`，禁止 latest-round 替代。
3. 精确 round 必须能证明：
   - round identity、phase、完成状态与输出结构有效；
   - evidence 使用现行 `phase2-promotion-metrics/v1`；
   - 目标 combo 的 Phase 2 stats 可用；
   - round 的候选清单存在与 recommendation 精确匹配的 `promote_candidate`；
   - parameter set、family、timeframe、symbol 与 `source_round_id` 均不矛盾。
4. approval、gate、release、apply 各自独立失败关闭，不能依赖 UI 或上一步已检查。
5. 运营查询和前端 payload 显示结构化资格、稳定 reason code 与禁止操作原因。
6. 非 apply 类型（例如 `pause`、`require_review`）保持原有风险收敛能力，不被本门闸误伤。

### 3.2 非目标

- 不修改现有 recommendation 或 decision-round 数据库 schema；
- 不执行 Stage 20 migration、rollback 或 trigger；
- 不把旧 evidence bundle 重写成新版本；
- 不把“当前规格”追溯写成历史事实；
- 不生成缺失的 `phase2_promotion_metrics.json`；
- 不宣称 P0-F 全部完成；新 metrics producer、不可变 round seal、统一历史 inventory 和确定性重建
  仍是后续任务；
- 不解锁 derivative/MARGIN public replay，不改变 G0/G1 状态；
- 不部署、不 push、不触发真实资金或参数应用。

## 4. 资格合约

### 4.1 适用范围

- `recommendation_type == "parameter_upgrade"`：必须通过本合约；
- 其他 recommendation type：返回 `not_required`，本合约不阻断其原有治理语义；
- 类型缺失但携带 `target_parameter_set_id` 的历史记录：按 apply-capable 处理并失败关闭，避免旧 schema
  借缺字段绕过。

### 4.2 精确引用解析

`evidence_bundle_ref` 必须是 canonical decision round ID，且只能解析到
`artifacts/decision_rounds/<round_id>` 或治理 DB 中同 ID 的 snapshot。禁止绝对路径、父目录跳转、
软推断和“找最新”。DB 可用时以 DB snapshot 为真源；DB 中没有精确记录时不得用另一个 round
代替。离线测试/开发的文件 fallback 也必须验证同一 round ID 和同一输出集。

### 4.3 必须同时满足的条件

1. recommendation ID、类型和目标 parameter set 字段合法；
2. `evidence_bundle_ref` 存在且 round ID 语法合法；
3. snapshot/目录中的 `round_manifest.round_id` 与引用完全相同；
4. manifest `phase == "phase6"` 且 `status == "succeeded"`；
5. evidence summary 的 `phase2_evidence.promotion_qualification_policy` 等于
   `phase2-promotion-metrics/v1`；
6. `get_phase2_combo_stats()` 对 recommendation 的 family/timeframe 返回 `available=true`，且至少一
   个合格实验具有 opening；
7. 精确 round 的 `parameter_upgrade_candidates` 中恰有一条匹配记录：
   `decision=promote_candidate`、parameter set、family、规范化 timeframe、symbol 均相同；
8. recommendation 与候选二者的 `source_round_id` 都必须存在且相等；
9. manifest 声明的候选数量与实际候选列表数量一致；关键输出引用不得指向 round 外部。

任何条件不满足时返回 `eligible=false`、稳定 reason code 和非敏感 detail。异常、不可读 JSON、
DB 错误和 schema 不支持均按不合格处理，不抛出可绕过的“warn-only”结果。

### 4.4 不可变性边界

本 LF-A 会验证现存 snapshot/文件的一致性，但现有 Phase 6 round 尚未具备独立 trust root、完整
artifact hash seal 和 append-only DB trigger。因此本任务只声称“精确引用与前向隔离”，不声称
历史 round 已经密码学不可变。后续 LF-B 必须为 round、candidate、evidence 和 recommendation
建立同一 seal，并进行 mutation-negative 验证。

## 5. 接入点

| 接入点 | 必须行为 |
| --- | --- |
| 底层审批 helper | `draft -> approved` 前检查；不合格时不产生 DB/JSON 状态变化 |
| Approval API | 返回结构化 `promotion_qualification`；拒绝不是 500，也不伪称成功 |
| approve-and-release | 审批前同样阻断，避免 orphan approved 记录 |
| pre-apply context/rule | 加载精确 round verdict；默认规则以 block severity 执行 |
| release helper | 即使 `run_gate=false`，也必须在创建 release record 前阻断 |
| apply helper | 即使由内部脚本直接调用，也必须在读取/写入 active parameter 前阻断 |
| control summary/workspace | 只展示合格 approved candidate 为“可发布”；旧 approved 标记 audit-only |
| 操作计数 | release-candidate 数量只统计合格项，不把历史 approved 等同 ready-to-apply |

## 6. 接口与兼容策略

1. 新 verdict 使用普通只读结构，至少包含：`required`、`eligible`、`reason_code`、
   `evidence_bundle_ref`、`source_round_id`、`qualified_round_id`。
2. 现有 recommendation JSON/DB 字段不删除；只读 API 可增量增加 qualification 字段。
3. 失败消息采用稳定 reason code，中文 detail 供 operator 解释；测试不得只依赖易变的整段文案。
4. 非参数升级 recommendation 保持兼容；reject/supersede 保持可用，便于清理旧记录。
5. 不自动改写历史 `approved` 为 `rejected/superseded`。这类记录在只读视图标记为
   `audit_only=true`，等待人工处置。

## 7. 数据、事务、并发与安全

- 本切片无 schema 变更、无数据回填、无 destructive migration。
- 资格验证必须发生在首次治理写入前；CAS 状态迁移仍由现有机制保证。
- approve-and-release 在资格失败时必须零写入；release/apply 重检用于覆盖 TOCTOU 和直接调用。
- 路径必须 resolve 后保持在精确 round 目录内；拒绝路径跳转与绝对路径注入。
- detail 不输出环境变量、凭证、数据库 URL 或原始异常中的敏感内容。
- 文件/DB 不可达视为 unknown/ineligible，而不是 `eligible=true`。

## 8. 可观测性与审计

- gate check 名称固定为 `promotion_qualification`；
- 阻断结果携带稳定 reason code；
- UI 明确区分“已审批”和“具备发布资格”；
- 本切片不制造自动审计写入或历史状态 churn；操作员真正发起的 gate 仍沿用现有持久化审计；
- 后续 inventory 应统计 eligible、audit-only、missing-ref、legacy-policy 和 mismatch 数量，但不得把扫描
  本身写成资格证据。

## 9. 测试矩阵

### 9.1 纯资格单元测试

- 合格的 exact round/candidate/evidence 通过；
- 缺 evidence ref、非法 round ID、path escape、round 不存在；
- manifest round ID/phase/status 不匹配；
- 旧 policy、缺 combo、combo unavailable；
- candidate 缺失、重复、decision 非 promote；
- parameter set/family/timeframe/symbol/source round 任一 mismatch；
- malformed/non-object/non-finite JSON；
- 非 apply recommendation 返回 not-required。

### 9.2 接入回归

- approve 和 approve-and-release 在首次写入前阻断；
- reject/supersede 仍可清理旧建议；
- gate 不再读取 latest round 冒充精确 round；
- `run_gate=false` 的 release 仍阻断失格建议；
- direct/dry-run apply 同样阻断；
- UI/action enabled、operations summary 与 qualification 一致；
- DB 精确 snapshot 与文件 fallback 产生相同 verdict；DB 可用但精确 round 缺失时失败关闭。

### 9.3 验证顺序

1. 新 verifier 与接入点聚焦单元测试；
2. RDP route/control-summary/workspace 契约测试；
3. WSL2 中最窄治理 DB 集成测试（如本地状态允许）；
4. `ruff check aats/ --fix`；
5. Windows 全量 unit，使用仓库内唯一 `--basetemp`；
6. 独立 code review，修复所有 P0/P1 后重跑；
7. `git diff --check` 与范围核对。

## 10. 回滚

代码回滚只需撤销本切片的 shared verifier 与各接入点；没有 schema/data rollback。回滚会重新暴露
历史 recommendation 前向推进风险，因此只有在替代门闸已经验证后才可执行。旧 artifact 和状态
从始至终不变，无需恢复。

## 11. 验收条件

- [ ] 所有 apply-capable recommendation 都必须通过同一个 exact-reference verifier；
- [ ] latest round 不能替另一 recommendation 背书；
- [ ] 旧 policy/missing ref recommendation 在 approval/release/apply 三层均失败关闭；
- [ ] 资格失败无 DB、JSON、release 或 active parameter 写入；
- [ ] 非 apply 类型及 reject/supersede 不被误伤；
- [ ] read-only API/UI 不再把失格 approved 记录显示为可发布；
- [ ] focused、full unit、Ruff、diff-check 与独立 review 通过；
- [ ] 文档明确保留 LF-B、producer、inventory、真实观察和 G0 外部阻断；
- [ ] 无 live、订单、资金、凭证、参数应用、push 或越权副作用。

## 12. 当前外部与后续阻断

1. Stage 20 schema/trigger 仍等待同一 digest 的人类审批；
2. Phase 6 round 的不可变 seal 与独立 trust root 未建立；
3. 现行 replay/calibration/scan writer 尚无自动 metrics producer；
4. 衍生品历史 instrument snapshot 与 Gold row binding 未建立；
5. P0-C/P0-E 的金额、深度、fee、market alignment 与 calibration 仍有公开审查发现；
6. Research OS G0 仍为 `G0_OPEN`，G1 采集不得解锁；
7. 任何自然时间观察、实名 Owner/Reviewer、许可、预算和 IAM 条件不能由代码或 AI 代签。
