# AATS Research OS Legacy Freeze 分类登记

> 文档状态：截至核对日的治理分类快照；技术强制仍为部分实现
> 生效决议：2026-08-26 G0 决策记录
> 代码核对基线：`main@40dc6817861a1ddfd92cc8a01d2b9ce87af523aa`
> 运行状态边界：仅静态仓库分类；未据此核验当前 WSL2、容器、数据库或交易所状态

## 1. 目的

在 Research OS 独立建设期间冻结旧 RDP 的研究扩张，避免同一数据/研究问题在两个平台重复
建设。Freeze 不是停止 AATS：真实资金安全、金融正确性、数据连续性、恢复和审计缺陷仍须
在旧系统修复。所有新需求先归类，再决定进入旧系统、Research OS backlog 或停止。

## 2. 分类规则

### LF-A：允许且优先在 Legacy 修复

只允许最小、可验证、向后兼容的变更：

1. P0 风控：Kill Switch、短时交易许可租约、NATS/Redis 分区、外部 watchdog、live fail-closed；
2. 金融正确性：合约乘数、quantity/notional、fee sign、PnL、精度和并发；
3. 数据连续性：现有采集器跨日连续、gap 证据、archive、retention、恢复，不扩大数据产品；
4. 恢复与迁移：schema/app rollback、恢复演练、审计日志和不可抵赖证据；
5. 安全：数据库层只读、凭证/权限最小化、连接预算、身份和速率限制；
6. 仅为上述变更服务的测试、事实型文档和运维手册。

### LF-B：例外审批后才允许

- 会触及真实运行事实的只读 RDP 查询或验证，例如既有 `RDP-DATA-025`；
- 为关闭 P0/P1 而不可避免的窄 schema 扩展；
- 有明确期限、Owner、rollback 和证据边界的兼容修复。

审批至少需要 PO、相关领域 Owner、SEC（涉及边界时）和 Risk Reviewer；不得把“只读”自动
解释为无风险，也不得把一次批准扩展为持续授权。

### LF-C：停止在 Legacy 新建，转入 Research OS

- 新交易所、新数据供应商、新数据产品和新的历史数据大规模导入；
- Instrument Master、Canonical Event、长期不可变 Raw、跨来源 capability/license registry；
- 新研究工作流、因子、候选策略、模型训练、实验调度和研究 UI；
- 新 L2 模拟器、微观结构特征、candidate campaign 和 90 日 Raw L2 平台化存储；
- 分布式 Research scheduler 或为研究扩张而进行的 UI 重构。

这些事项不能在 G0/G1 未满足时偷偷在旧仓库实施；应进入 Research OS 对应 Gate backlog。

### LF-D：明确禁止

- 伪造/推算交易所未提供的 liquidation、event/publish time、sequence、checksum 或 attribution；
- 用 K 线、receive time、本机自增号填补来源原生字段；
- 绕过当前返回 501 的参数 apply/rollback/ACK 门禁；
- 在旧 Postgres 中直接建设 90 日 Raw L2 长期主存储；
- 引入私有交易 endpoint、live secret 或绕过地域/许可的数据源；
- 用文档、模板、mock、CI 绿色或历史快照声称现场数据、许可或运行门已通过。

## 3. 已识别 Legacy backlog 分类

| 优先级 | 事项 | 当前代码/证据入口 | 分类 | 完成定义 |
| --- | --- | --- | --- | --- |
| P0 | 衍生品 quantity/notional 统一，移除 replay/microstructure 隐式 `0.01` 等默认 | `aats/data_platform/merge/microstructure_silver_merger.py`、`aats/data_platform/replay/`、`aats/services/execution_engine/quantity_rules.py` | LF-A | 全链使用已验证 InstrumentMetadata；linear/inverse golden tests |
| P0 | RDP 查询在 DB 层真正只读 | `aats/data_platform/live_query_adapter.py` | LF-A | read-only transaction/role 证据；写入负向测试失败关闭 |
| P0 | 既有采集跨日连续性和 archive 恢复证据 | `scripts/rdp_archive_microstructure.py`、`scripts/rdp_verify_archive_restore.py` | LF-A | 跨日窗口、随机 restore、gap 台账；不扩来源/产品 |
| P0 | Stage 19 schema/Gold 验证 | `scripts/rdp_build_source_aware_gold.py`、`scripts/rdp_build_gold_all.py` | LF-A | 只修 schema/lineage/金融正确性，不扩研究范围 |
| P0 | schema/app/rollback 灾备演练 | `aats/data_platform/migrations/`、现行部署/恢复手册 | LF-A | 新空环境恢复、RPO/RTO 和 rollback 实测证据 |
| P0 | Kill Switch 分区安全 | `aats/services/governance_engine/kill_switch.py`、`tests/unit/test_fs002_kill_switch_p0.py` | LF-A | Redis/NATS/进程分区仍 fail closed；独立运行验证 |
| P0 | 关键任务外部监督/watchdog | `aats/data_platform/operations/rdp_daemon_health.py`、FS-006 SOW | LF-A | 进程卡死/无进度由进程外监督发现并处置 |
| P0 | NATS peer readiness 告警闭环 | `aats/bus/nats_bus.py`、FS-016 SOW | LF-A | readiness 失败可观测且部署代次不混淆 |
| P1 | 数据库连接总预算实测 | FS-008 SOW、各 engine/连接池配置 | LF-A | 目标负载/瞬时连接/内存联合测量，不凭声明通过 |
| P1 | 登录负载与分布式限速 | FS-019 SOW | LF-A | 多进程/高并发实测；避免单进程局部结论 |
| P1 | 真实 RDP 数据事实查询 | `scripts/rdp_check_live_facts_connection.py` 等 | LF-B | 单次书面授权、只读账号、范围/输出/销毁记录 |
| P1 | 30 日数据活动或新来源 | 历史恢复任务书 | LF-C | 迁入 Research OS 且相应 Gate 通过后再执行 |
| STOP | 旧库 90 日 Raw L2 | 旧 RDP 存储/retention 方案 | LF-C/LF-D | 不在 Legacy 实施；Research OS 架构另审 |
| STOP | 参数 apply/rollback/ACK | 当前 501 fail-closed 路径 | LF-D | 保持禁止，除非后续独立发布 Gate 正式授权 |
| STOP | 新策略/因子/候选/UI/scheduler | 旧 Research Factory/RDP UI | LF-C | 旧仓库不扩张，进入 Research OS 后续 Gate |

## 4. 变更准入模板

每个 Legacy PR 必须回答：

1. `freeze_class`：LF-A/B/C/D；
2. `safety_or_correctness_defect`：具体缺陷和资金/证据风险；
3. `scope_boundary`：为什么不构成新研究能力；
4. `runtime_evidence_required`：静态、隔离、WSL2、数据库或现场验证；
5. `rollback`：代码、schema 和运行回退；
6. `research_os_overlap`：是否与新平台重复；
7. `approvals`：LF-B 例外的角色和同一 digest 签字。

LF-C/D 直接关闭或迁移；LF-B 未签字保持阻断；LF-A 仍需正常 code review、测试和部署门。

## 5. 技术强制现状与待办

当前已完成分类登记，但尚不能把以下现场治理声明为已完成：

- 远端分支规则是否已强制要求 freeze label/check；
- 真实 CODEOWNERS 团队与 reviewer 权限；
- 自动识别新 connector/strategy/schema/UI 范围的 CI check；
- 所有现有 backlog 是否已逐项绑定 Owner 和期限。

因此当前状态是 `CLASSIFIED / PROCEDURALLY_ACTIVE / TECHNICAL_ENFORCEMENT_PARTIAL`，不是
`FULLY_ENFORCED`。G0 Closure Packet 必须引用本文件所在 commit/tree digest，并补齐远端现场证据。
