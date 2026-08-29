# AATS Task 与 SOW 历史索引

> 文档状态：任务与交付历史证据索引（截至 2026-08-28）
> 最后核对：2026-08-28（Research OS G0 bootstrap、RDP 衍生品合同 P0-A、
> P0-B LF-A、参数内容身份、typed JSON、正式制品读取与衍生品 Phase 2 资格闭环任务已登记；其余历史状态边界不变）
> 核对范围：任务文件存在性、索引与各任务自报状态；不把任务状态升级为运行时或生产事实

本目录保存任务书、SOW、阶段设计、实施记录和交付报告。它是工程可追溯性材料，不是当前系统说明；“完成”“通过”“上线”等措辞只对文件记录的基线和验证范围成立。

## 使用规则

- 查当前行为：使用 [`../code_review/README.md`](../code_review/README.md) 与代码真源；
- 查当前操作：使用 [`../operations/README.md`](../operations/README.md) 与 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)；
- 查当前测试：使用 [`../testing/README.md`](../testing/README.md)；
- 引用历史任务时保留文件名、日期、commit 和验证范围；
- 新任务材料继续写入本目录，不得写入 `docs/` 根层兼容区；
- 同一任务有子目录时，以其 `README.md` 作为该任务入口。

## AATS Research OS 独立建设计划

- [`aats_research_os_program_plan_2026_08_26.md`](aats_research_os_program_plan_2026_08_26.md)：
  从零建设独立研究证据平台的立项规划基线，覆盖目标架构、领域合同、WBS、G0--G8 阶段门、
  30/60/90 日纵向切片、团队资源、测试、风险、Legacy 迁移和资本授权边界。项目启动已条件性
  批准，但 G0 Gate 尚未通过；不代表新平台已实施，也不授权 live、真实资金或 Runtime 参数写入。
- [`aats_research_os_g0_decision_record_2026_08_26.md`](aats_research_os_g0_decision_record_2026_08_26.md)：
  G0 正式决议；确认独立仓库、BTC/ETH 永续楔子、Owner/Reviewer 规则、精简专业资源模式、
  Binance 条件性第二来源、G0--G4 USD 120,000 预算硬上限和 Legacy Freeze。项目启动状态为
  `APPROVED_WITH_CONDITIONS`，G0 证据门为 `OPEN / NOT PASSED`。
- [`aats_research_os_g0_closure_and_g1_kickoff_plan_2026_08_26.md`](aats_research_os_g0_closure_and_g1_kickoff_plan_2026_08_26.md)：
  十个工作日 G0 收口清单和 G0 通过后的首个 30 日 G1 Canonical Truth Kernel tranche；明确
  Owner、依赖、证据、预算、停止线，以及条件关闭前允许和禁止的工作。
- [`aats_research_os_g0_bootstrap_implementation_sow_2026_08_26.md`](aats_research_os_g0_bootstrap_implementation_sow_2026_08_26.md)：
  G0 bootstrap 的历史实施基线，覆盖独立仓库骨架、合成领域合同、不可变 Raw、治理证据、测试、
  回滚和验收；实际实现差异和现行状态以交付记录为准。
- [`aats_research_os_g0_bootstrap_delivery_2026_08_27.md`](aats_research_os_g0_bootstrap_delivery_2026_08_27.md)：
  截至核对日的交付快照；记录独立本地仓库、领域/治理/CI 合同、审查修复、任务书差异和真实阻断。
  独立工程基线为 `22a465b06cd731a27cf92154190b1b480fa84d2b`，当前仍为
  `G0_OPEN / G1_LOCKED`，无 remote/采集/168h/部署事实；动态 Gate 以独立仓库 current/readiness 为准。
- [`aats_research_os_legacy_freeze_register_2026_08_26.md`](aats_research_os_legacy_freeze_register_2026_08_26.md)：
  截至核对日的 Legacy Freeze 分类快照；区分仍须在旧系统完成的 P0/P1 安全/正确性修复、需例外审批的
  现场事实、必须迁入 Research OS 的研究扩张和明确禁止事项。当前为程序性生效、技术强制部分实现。

## 2026-08-26 RDP 数据治理实施记录

### 2026-08-27 控制面真值收口

- [`rdp_legacy_recommendation_forward_isolation_p0_sow_2026_08_27.md`](rdp_legacy_recommendation_forward_isolation_p0_sow_2026_08_27.md)：
  LF-A 代码级失败关闭任务；apply-capable recommendation 必须绑定精确、成功、未过期且使用
  当前 qualification policy 的 Phase 6 round，approval/gate/release/apply 各层独立阻断旧证据。
  当前仍需随本轮完整验证和运行证据一起验收，不能把历史 recommendation 升级为合格。
- [`rdp_control_plane_truth_finalization_sow_2026_08_27.md`](rdp_control_plane_truth_finalization_sow_2026_08_27.md)：
  promotion readiness、canonical UTC、DB/mirror 状态边界、post-apply provenance、自动风险
  收敛与 action proof 的收口任务。当前状态以文件头为准；完整回归、独立复审和模拟运行前
  不得标记完成。
- [`rdp_promotion_parameter_identity_p1_sow_2026_08_27.md`](rdp_promotion_parameter_identity_p1_sow_2026_08_27.md)：
  参数晋级内容身份 LF-A；把 Phase 6 评估的精确参数值指纹贯穿 qualification、短时授权、
  dry-run 与锁内 apply，并将官方 parameter/recommendation/decision-round writer 收口为同 ID
  内容只写一次。Step 3 自动导入改为确定性 ID、部分导入可恢复、先补齐后 CAS 废弃，避免
  并发 apply 将 `released` 覆盖成 `deprecated`。Stage 20 数据库原生 trigger/DELETE 防护仍待
  真人审批，当前不得据此宣告生产就绪。
- [`rdp_typed_json_identity_p1_sow_2026_08_28.md`](rdp_typed_json_identity_p1_sow_2026_08_28.md)：
  修复 PostgreSQL JSONB 将 `1` 与 `1.0` 判等造成的不可变身份绕过；统一严格 JSON canonical
  SHA-256，并为 parameter set、research round snapshot、decision round snapshot 增加持久摘要。
  历史行不伪造类型来源，只在 exact retry 时安全补写；迁移与真实 PostgreSQL 测试完成前不代表现场生效。
- [`rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md`](rdp_derivatives_phase2_promotion_evidence_producer_p1_sow_2026_08_28.md)：
  记录“完整 RDP”缺少正式 Phase 2 晋级证据 producer 的结构性 P1。现有 v2 harness 仅支持 SPOT，
  legacy Step 2 只可审计；LF-A 先关闭 SPOT/SWAP scope 污染并暴露真实阻断，LF-B 再建设可重算、
  manifest-last、managed snapshot 锚定的 linear perpetual 回测与 metrics 闭环。LF-B1.1 纯合同/记账
  基础已在 `bf7a24dfe0a3` 静态验收；event/reducer/publisher/qualification 仍开放。LF-B 整体验收前保持
  REAL-MONEY NO-GO，不能把基础代码或阶段执行完成解释为可晋级研究完成。
- [`rdp_derivatives_backtest_lfb1_2_engine_evidence_sow_2026_08_28.md`](rdp_derivatives_backtest_lfb1_2_engine_evidence_sow_2026_08_28.md)：
  LF-B1.2 事件与证据内核现行实施任务书；冻结 immutable snapshot loader、封闭事件联合类型、固定 phase、
  freshness/funding continuity、single-position reducer、manifest-last publisher、exact checkpoint/recovery、
  fault/determinism 与资源验收边界。LF-B1.2-A1 与 A2 event-set 严格契约基础已完成本地静态验收；
  formal 双遍 reader/feature/decision input、reducer、publisher、checkpoint/recovery、正式运行接入和 UI/UX
  全面重构仍未完成，不代表
  producer、qualification、UI 或真实资金路径已解锁。
- [`rdp_formal_artifact_reader_hardening_sow_2026_08_28.md`](rdp_formal_artifact_reader_hardening_sow_2026_08_28.md)：
  正式 research artifact 的有界、单描述符稳定读取加固记录；统一拒绝路径替换、读取中变化、符号链接、
  超限、非法 UTF-8、重复 JSON key 和非有限数值，并覆盖候选导入、parameter lineage、Phase 6
  qualification 与 evidence bundle 高价值消费者。该切片不改变研究阈值、数据库发布或 live 行为。

- [`rdp_continuity_monitoring_correctness_p1_sow_2026_08_27.md`](rdp_continuity_monitoring_correctness_p1_sow_2026_08_27.md)：
  RDP 连续性监控与研究资格 LF-B 设计；拟引入 typed subscription catalog、default/registered/ACK
  三方对账、活动模型、current lifecycle、桶/序列完整性及 DROP/gap 一致性。当前状态为
  `BLOCKED_PENDING_HUMAN_LF_B_APPROVAL`；旧投影的 95 条告警是不可静默的监控伪影，不代表
  95 个真实当前故障，也不能作为采集健康或研究准入证据。
- [`rdp_derivatives_contract_arithmetic_p0_sow_2026_08_27.md`](rdp_derivatives_contract_arithmetic_p0_sow_2026_08_27.md)：
  现行 LF-A/P0 实施任务书；统一 spot/linear/inverse 的张数、base quantity、quote notional、
  fee 与 PnL 语义，绑定版本化 instrument contract，并使旧错误产物失格后重建。P0-A 代码切片
  已通过单元验证和独立复审；P0-B LF-A 已完成本地静态验收，增加 snapshot/source/bundle 绑定并对不可验证
  Gold、资本资格与完整 campaign 执行失败关闭，但 Stage 20、不可变 Silver、DB-backed verifier、
  WSL2 集成/运行验证及 P0-C 至 P0-F 仍未验收，不能据此声明历史收益证据已修复。
- [`rdp_strict_instrument_scope_p0_sow_2026_08_27.md`](rdp_strict_instrument_scope_p0_sow_2026_08_27.md)：
  记录 BTC/ETH spot/swap 显式支持集合及统一失败关闭规则；未知币对、伪 `-SWAP` 与 FUTURES
  不再因字符串形状进入 lineage、Gold、replay、capital eligibility、治理 API 或历史导入。
- [`rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md`](rdp_historical_data_recovery_and_collection_hardening_sow_2026_08_26.md)：历史数据恢复、持续采集、归档、双准入与完整模拟 RDP 的实施台账。旧 v1 30 日 campaign 已在 2026-08-27 写入 `SUCCEEDED`，但不满足当前 v2 持久 fencing、不可变 Silver/source-aware Gold 与逐行终态 verifier 契约；账户只读事实、跨日连续性、签名 UI、精确归因、OOS/holdout 和资本资格仍未完成，90 日保持 NO-GO。

现行操作入口是 [`../operations/rdp_historical_data_recovery_runbook.md`](../operations/rdp_historical_data_recovery_runbook.md)，当前收益/上线门结论是 [`../testing/profit_readiness_acceptance.md`](../testing/profit_readiness_acceptance.md)。任务书不替代下一次运行前的新覆盖审计和健康检查。

## 2026-08-24 全系统审计整改 SOW

以下文件记录 2026-08-24 至 2026-08-25 整改工作的实施边界与验证契约；其“完成”状态只对应各文件记录的当时基线，不是当前生产已生效声明：

- [`phase3_post_audit_full_change_review_sow_2026_08_25.md`](phase3_post_audit_full_change_review_sow_2026_08_25.md)：Phase 3 全量变更复审、问题修复、完整提交与 derivatives 模拟栈运行观测；
- [`fs_001_profile_apply_fail_closed_sow_2026_08_24.md`](fs_001_profile_apply_fail_closed_sow_2026_08_24.md)：profile 参数应用错误成功失败关闭；
- [`fs_001_profile_rollback_fail_closed_sow_2026_08_24.md`](fs_001_profile_rollback_fail_closed_sow_2026_08_24.md)：profile 参数回滚错误成功失败关闭；
- [`fs_002_kill_switch_p0_remediation_sow_2026_08_24.md`](fs_002_kill_switch_p0_remediation_sow_2026_08_24.md)：Kill Switch P0；
- [`fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md`](fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md)：Kill Switch 全分区短时交易许可租约；
- [`fs_003_backtest_causal_timing_sow_2026_08_24.md`](fs_003_backtest_causal_timing_sow_2026_08_24.md)：回测因果时序；
- [`fs_014_ohlcv_fill_realism_containment_sow_2026_08_24.md`](fs_014_ohlcv_fill_realism_containment_sow_2026_08_24.md)：OHLCV 成交量参与上限、partial fill 与成本证据边界；
- [`fs_005_gateway_loopback_containment_sow_2026_08_24.md`](fs_005_gateway_loopback_containment_sow_2026_08_24.md)：Gateway 本机绑定与本地入口收口；
- [`fs_006_critical_task_supervision_sow_2026_08_24.md`](fs_006_critical_task_supervision_sow_2026_08_24.md)：关键任务监督；
- [`fs_006_critical_task_progress_watchdog_sow_2026_08_24.md`](fs_006_critical_task_progress_watchdog_sow_2026_08_24.md)：固定周期关键任务成功进度看门狗；
- [`fs_007_deployment_fail_closed_sow_2026_08_24.md`](fs_007_deployment_fail_closed_sow_2026_08_24.md)：部署失败关闭与 live 禁用；
- [`fs_009_schema_single_truth_sow_2026_08_24.md`](fs_009_schema_single_truth_sow_2026_08_24.md)：schema 单一迁移真源；
- [`fs_010_managed_profile_unknown_key_fail_closed_sow_2026_08_25.md`](fs_010_managed_profile_unknown_key_fail_closed_sow_2026_08_25.md)：managed 伪配置删除、unknown-key 失败关闭与生成文档防回退；
- [`fs_011_legacy_run_local_fail_closed_sow_2026_08_25.md`](fs_011_legacy_run_local_fail_closed_sow_2026_08_25.md)：失效本地 paper-loop 入口的无配置副作用迁移失败关闭；
- [`fs_015_replay_short_bias_parity_sow_2026_08_25.md`](fs_015_replay_short_bias_parity_sow_2026_08_25.md)：independent replay 与生产 short-bias gate 一致性；
- [`fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md`](fs_016_nats_peer_readiness_fail_closed_sow_2026_08_24.md)：NATS/hybrid peer readiness 失败关闭与部署代次隔离；
- [`fs_016_runtime_readiness_lease_restart_safety_p0_sow_2026_08_28.md`](fs_016_runtime_readiness_lease_restart_safety_p0_sow_2026_08_28.md)：
  记录模拟栈运行超过 300 秒后单角色无法重新加入的现场 P0，以 Redis-only protocol v2、
  全局 `aats:runtime:owner:<role>`、`PROVISIONING -> READY` CAS、claim 即续租、独立 subprocess
  watchdog、55 秒 takeover quarantine、`NatsDeliveryGate`、strict gated `max_ack_pending=1`、首次
  cutover drain gate、critical durable runtime supervision、Redis `noeviction`、4,096 条/64 MiB build
  publish 缓冲、60/10/30/10 秒 lease 时间界、每次成功 PROVISIONING 写/续租后的 50 秒滑动 hard
  fence 与 claim→READY 180 秒绝对上界修订原 FS-016；non-strict/in-memory/monolith 不注入 gate、
  不强制窗口。v1 -> v2 首发/回滚必须走标准 full-down。入口在任何 mutation 前取得固定
  `/tmp/aats-standard-deploy.lock` 的长寿命 WSL `flock`；只有 `AATS_DEPLOY_TEST_MODE=true` 的隔离
  测试可用 `AATS_DEPLOY_LOCK_FILE` 覆盖。Windows 3 秒 heartbeat / WSL 12 秒失联释放协议持有到
  最终 evidence/report；新 holder 必须隔离等待 fresh predecessor lease 清除或过期，并等待同一作用域
  内所有无 TTL 的 durable active marker 消失。外部步骤 spawn 前复核锁、spawn 后登记唯一 active
  child；最终 launch 前可取消，launch 后丢锁或收到 `TERM/HUP/INT/EXIT` 时保留 lease/flock 并等待
  guard 的实际完成证明，不能以杀死 `wsl.exe` 推断远端 mutation 已停止。WSL 命令以七字段 ACK 绑定
  marker、语义退出码、I/O 模式及 stdout/stderr 字节数和 SHA-256；默认 `capture` 先写 `0600` 暂存、
  验证后回放。缺失/畸形 ACK、transport 非零、输出或清理歧义均失败关闭；待成功释放改为状态 16，
  既有非零状态不覆盖。标准代码同步是单个 ACK 支持的 WSL Git 事务，已证明的 dirty/branch 语义拒绝
  会清理自身 marker，而 transport 歧义保留 poison marker。stop 七个
  应用后，以 ID/status/StartedAt/FinishedAt/RestartCount 和精确 Docker events 区间证明 quiescence。
  基础设施-only up 后、full-down 前执行第一次 loopback 全量分页只读 preflight；full-down 后重建
  基线，并在 infra/schema 后、app-up 前执行第二次。阻断或 quiescence 变化时保留 NATS，最终部署
  证据校验最后一次 v2 PASS 的 lock id/generation/deployed commit/quiescence 及路径/hash；标准 stop
  仍不能替代 drain 证明。LAST/NEW 的运行时重建受 strict delivery gate、非 ALL policy，以及
  “outstanding 超过目标”或“收缩窗口且 outstanding 非零”条件约束，自动重启也可能触发；full-down
  是额外发布门禁，不是运行时信号。真实 NATS
  consumer-delete 集成已通过，锁竞争/释放/重取、WSL completion ACK、输出完整性、幂等 proof 重试与
  marker cleanup 有窄 smoke；最后一次 NATS 健康窗与部署 completion protocol 修复后，当前候选通过
  `213` 项 FS-016 聚焦、`173` 项根级独立聚焦
  复跑、六项隔离 smoke 和 `6434 passed / 31 skipped / 259 subtests passed` 全量单测，但真
  Redis/NATS/Docker、标准
  部署、网络/push/heartbeat/backlog stall、真实逐角色重启、双故障和下游 fencing 均为 `OPEN`；
  不能据本地候选宣称模拟栈已恢复或 live 已放行。
- [`fs_019_operator_login_async_isolation_sow_2026_08_24.md`](fs_019_operator_login_async_isolation_sow_2026_08_24.md)：Operator 登录异步隔离、有界 worker、每进程限流与 dummy KDF；
- [`fs_020_browser_security_headers_sow_2026_08_24.md`](fs_020_browser_security_headers_sow_2026_08_24.md)：Gateway Host 失败关闭与浏览器安全响应头；
- [`fs_017_fs_018_dashboard_accessibility_sow_2026_08_25.md`](fs_017_fs_018_dashboard_accessibility_sow_2026_08_25.md)：Dashboard 原生 modal/focus contract 与 reduced-motion 收敛；
- [`fs_021_ci_quality_gate_sow_2026_08_25.md`](fs_021_ci_quality_gate_sow_2026_08_25.md)：仓库级最小权限 CI、strict marker 与 warning 预算；远端 required check 和 integration/security gate 仍开放。
- [`fs_022_reproducible_dependencies_sow_2026_08_25.md`](fs_022_reproducible_dependencies_sow_2026_08_25.md)：Python 3.12/Linux hashed lock、基础/外部镜像 digest 与防回退契约；APT、SBOM/扫描、clean build 和远端治理仍开放。
- [`fs_008_database_connection_budget_sow_2026_08_25.md`](fs_008_database_connection_budget_sow_2026_08_25.md)：PostgreSQL 角色化连接池、声明拓扑预算和 engine inventory；目标负载、瞬时路径与联合内存预算仍开放。
- [`fs_004_research_selection_holdout_sow_2026_08_25.md`](fs_004_research_selection_holdout_sow_2026_08_25.md)：Research Factory train/valid 双门与 test 内容封存；最终 OOS、walk-forward、历史产物审计和独立复核仍开放。

现行结论、残余风险和运行时未知项以 [`../../audit/full_system_2026_08_24/README.md`](../../audit/full_system_2026_08_24/README.md) 为入口；这些 SOW 只记录获准范围、设计和静态/隔离验证。

截至 2026-08-23，本目录含数百份历史材料，另有 184 份早期 SOW/任务文件保留在 `docs/` 根层以维持路径兼容。批量迁移必须先做引用和外部审计影响评估。

## Legacy Word 文档

下列 `.docx` 是 2026-03 的初始蓝图，保留原文件名和路径以维持历史追溯；它们不是当前说明：

- [`Ai Autonomous Trading System Reference Implementation Skeleton.docx`](<Ai Autonomous Trading System Reference Implementation Skeleton.docx>)：早期 reference skeleton，包含已经漂移的目录、Kafka/Redpanda 候选、旧事件名、旧服务边界和 `run_local.py` 假设；
- [`Ai Autonomous Trading System Whitepaper V1.docx`](<Ai Autonomous Trading System Whitepaper V1.docx>)：概念白皮书 v1，包含 Kubernetes、Kafka/Redpanda/NATS 候选和早期状态机，不代表当前 WSL2 Compose、NATS、Postgres、Redis 或交易实现。

两份文件只可用于了解项目起点。当前替代入口是 [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)、[`../code_review/README.md`](../code_review/README.md) 和 [`../../DEPLOYMENT.md`](../../DEPLOYMENT.md)。本轮只读结构检查确认其历史性质；由于文档 OOXML 缺少明确页尺寸且当前环境没有 LibreOffice，未完成视觉渲染检查，因此不对原件版式作“已优化”声明。
