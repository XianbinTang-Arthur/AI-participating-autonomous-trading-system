# AATS RDP 研究工厂重构计划书（Qlib / RD-Agent 吸收改造）

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](project_positioning.md)。

## Current behavior summary

- AATS 当前 live trading runtime 由 gateway / market / decision / execution / rdp-daemon 组成，真实交易所为 OKX 衍生品。
- RDP 已经具备 Phase 1 数据仓库、Phase 2 replay/参数研究、Phase 3 live attribution、Phase 4 execution realism、Phase 5 governance、Phase 6 decision system。
- `configs/rdp_workflows/research_cycle.json` 当前承担完整研究闭环调度：先刷新近期数据，再执行 `rdp_run_full_pipeline.py --lookback-days 90 --replay-only --no-stop-on-failure`。
- RDP 产物通过 artifacts、governance registry、recommendation、pre-apply gate 和 active parameter apply 进入治理链；未经过 approve/apply 的研究结论不应影响 live runtime。
- 当前缺口不是 live execution，而是研究层的标准化能力不足：数据集切片、特征/因子表达、实验记录、基线对比、研究资源分配和 AI 自动研发沙箱还没有形成一个统一的 Research Factory。

## Business objectives and boundaries

- 目标：把 Qlib 的 research substrate 能力吸收到 AATS RDP，包括 dataset contract、handler/processor 分层、表达式型因子、实验记录、信号/组合/成本报告。
- 目标：把 RD-Agent 的研发循环思想吸收到 AATS RDP，包括 hypothesis -> experiment -> coding/proposal -> running -> feedback -> record，但先作为隔离研究沙箱，不进入 live 下单闭环。
- 目标：提高 RDP 研究吞吐、复现性、成本归因可信度和候选参数/模型/因子质量。
- 目标：让未来自动化代码开发任务有明确边界：每次只实现一个可验证的 RDP vertical slice，并且必须通过测试与治理文档要求。
- 边界：不替换 AATS live `OrderManager`、execution command processor、OKX adapter、ledger、reconciliation、risk guard 或 Operator write control plane。
- 边界：不把 Qlib/RD-Agent 直接加入 live runtime 依赖；如需引入，只允许作为可选 research extra 或 sandbox 镜像依赖，并单独评审。
- 边界：不允许 RD-Agent 或任何 LLM 自动生成 live order、自动 apply active parameter、读取生产密钥、调用 OKX 写接口或绕过 Operator 权限。

## Design principle: 取其精华，去其糟粕

| 来源 | 吸收部分 | 不吸收部分 |
|------|----------|------------|
| Qlib | DatasetH / DataHandler / Processor 分层、表达式因子、qrun 式 workflow、SignalRecord / SigAnaRecord / PortAnaRecord 思路、NestedExecutor 的研究模拟思想 | A 股默认市场假设、研究级 Exchange 作为 live execution、默认数据源、默认股票 benchmark / TopKDropout 直接套用 |
| RD-Agent | 研发闭环、factor/model 联合优化、bandit 研究资源分配、Co-STEER 的 evaluator + fallback 思路、实验反馈驱动迭代 | LLM 直接生产审批、LLM 直接触碰 live runtime、无隔离代码执行、默认 Qlib 股票模板、未经 deterministic gate 的 SOTA 替换 |

## Integration decision record

本重构不采用“整包搬入 Qlib + 整包搬入 RD-Agent”的路线。这里的“整合”定义为能力整合、数据/实验/治理接口整合和研究自动化闭环整合，而不是把两个外部项目变成 AATS live runtime 的核心依赖。

| 方案 | 说明 | 结论 | 原因 |
|------|------|------|------|
| Full vendor | 把 Qlib 和 RD-Agent 源码整体 vendor 到本仓库，并让 AATS 直接改造其内部实现 | 拒绝 | 维护面过大，升级困难，默认市场假设与 OKX 衍生品不匹配，容易把研究级执行模型误带入 live 边界 |
| Hard dependency | 在 AATS 主环境中直接依赖 qlib / rdagent，把 RDP pipeline 改成外部框架驱动 | 暂不采用 | 依赖重、环境复杂、版本漂移风险高；Qlib/RD-Agent 的默认 workflow 不符合 AATS 现有 Phase 2-6 治理链 |
| Qlib adapter only | 将 AATS Gold 数据导出为 Qlib-compatible dataset，用 Qlib 跑离线 benchmark | 可作为后续可选实验 | 适合校准研究指标和模型 baseline，但不能成为 live 参数治理的唯一真源 |
| RD-Agent direct loop | 让 RD-Agent 直接生成代码、跑实验并替换最佳参数 | 拒绝 | LLM feedback 不能替代 deterministic gate；直接替换 active parameter 会破坏 AATS 的审批、pre-apply gate、rollback 和审计链 |
| AATS-native Research Factory | 在 AATS RDP 内实现 dataset/feature/experiment/metrics/governance contract，吸收 Qlib/RD-Agent 的模式 | 采用 | 与现有 bounded context、artifact convention、research_cycle、parameter governance 兼容，能保留实盘安全边界 |
| Isolated sandbox agent | 把 RD-Agent 思路变成隔离 proposal worker，只输出 candidate/recommendation draft | 采用，放在后期 | 可以提高研究吞吐，同时不接触 secret、OKX 写接口、Operator write API 或 active apply |

因此，当前计划是对 AATS 目标函数的最优路径：先把研究底座 AATS-native 化，再接入可选 Qlib benchmark adapter，最后接入 RD-Agent-style sandbox。只有当某个外部组件经过独立 SOW、测试和安全评审后，才允许成为 research-only optional dependency。

## Target architecture

```text
AATS historical / replay / readonly live-derived data
  -> AATSDatasetSpec
  -> AATSDataHandler + Processor chain
  -> Feature / Factor DSL
  -> Label + Cost + Funding contract
  -> ExperimentSpec
  -> ExperimentRunner
  -> ExperimentRecorder
  -> MetricsSnapshot + ArtifactManifest
  -> Benchmark / OOS / Execution Realism / Governance Gate
  -> Candidate factor / model / parameter / execution policy
  -> recommendation
  -> approved pre-apply gate
  -> active parameter apply
  -> live runtime reads approved artifact only
```

RD-Agent 风格自动研发只能挂在 Research Factory 之前或之内：

```text
Sandbox Research Agent
  -> propose hypothesis / factor / model / parameter
  -> generate candidate code or JSON proposal in isolated workspace
  -> static scan + tests + replay
  -> Research Factory experiment
  -> feedback report
  -> candidate recommendation
```

## Module responsibilities and domain model

建议新增或重组的 research-only bounded context：

```text
aats/data_platform/research_factory/
  specs.py
  datasets/
  features/
  experiments/
  metrics/
  benchmarks/
  allocation/
  sandbox/
```

核心领域对象：

| 对象 | 职责 |
|------|------|
| `ResearchWorkflowSpec` | 描述一个研究工作流的输入、阶段、产物、成功条件和治理边界 |
| `DatasetSpec` | 定义 symbol/timeframe/window/dataset_version/segment/train-valid-test 切片 |
| `AATSDataHandler` | 从 Gold replay bars、funding、orderbook/trades 或 future feature store 生成训练/回测输入 |
| `ProcessorSpec` | 标准化缺失值、winsorize、normalize、leakage guard、OOS split 等处理链 |
| `FactorExpression` | 可序列化、可审计的 crypto factor DSL 表达式 |
| `LabelSpec` | 定义预测目标，必须包含 fee/slippage/funding/holding horizon 语义 |
| `ExperimentSpec` | 绑定 dataset、features、label、model、execution realism、metrics、artifact root |
| `ExperimentRecord` | 记录 code version、input refs、output refs、metrics、status、failure reason |
| `MetricsSnapshot` | 统一 IC、Rank IC、ICIR、net ARR、IR、MDD、turnover、fee/slippage/funding attribution 等指标 |
| `ResearchAllocationPolicy` | 动态决定下一轮研究资源投向 factor/model/execution/risk/regime/validation |
| `SandboxProposal` | RD-Agent 风格研究建议，只能进入 proposal queue，不能直接进入 production registry |

## Phase plan

### P0: Contract and scaffolding

- 新增 Research Factory 设计文档、模块骨架和 contract tests。
- 定义 `DatasetSpec`、`ExperimentSpec`、`ExperimentRecord`、`MetricsSnapshot` 的 Pydantic 或 dataclass 合约。
- 定义 artifact manifest 与现有 `round_manifest.json` 的兼容关系。
- 验收标准：不改 live runtime；单元测试覆盖 schema validation、序列化、非法字段拒绝、artifact path 规范。

### P1: Qlib-style dataset / handler layer

- 实现 AATS-native dataset handler，不直接依赖 Qlib。
- 从 Gold replay bars 构造 train/valid/test/replay segments。
- 支持 funding 对齐、未来 orderbook feature 扩展位、dataset leakage guard。
- 验收标准：同一 `DatasetSpec` 可重复生成相同输入；窗口边界、时区、缺失数据处理有测试；不读取 live secret。

### P2: Factor DSL and baseline benchmark harness

- 参考 Alpha158/Alpha360 思路，实现 crypto derivatives 因子 DSL。
- 第一批因子聚焦 price/volume/volatility/funding/basis/orderbook placeholder/execution cost proxy。
- 建立 baseline harness：当前参数、候选参数、简单模型、factor-only benchmark 统一输出 metrics。
- 验收标准：DSL 表达式可序列化、可 replay、可拒绝危险表达式；benchmark 输出包含成本后指标与稳定性指标。

### P3: Experiment recorder and governance bridge

- 建立统一 experiment recorder，写入 artifacts 并可选同步 research/governance DB。
- 每次实验必须记录 code version、dataset version、input refs、output refs、metrics、failure reason。
- 与现有 Phase 5/6 registry 对接，但只生成 candidate/recommendation，不自动 apply。
- 验收标准：失败实验也可审计；artifact validator 能识别缺失 manifest；candidate 到 recommendation 的转换有 deterministic gate。

### P4: Execution realism and live-aware metrics upgrade

- 把 Qlib 的信号/组合报告思想改造成 AATS 指标体系。
- 扩展指标：net ARR after fee/slippage/funding、tail loss、turnover、maker/taker ratio、fill failure proxy、cancel/replace proxy、margin usage、liquidation buffer、parameter stability。
- 验收标准：研究报告不能只展示收益；必须展示成本、执行可行性、回撤和稳定性归因。

### P5: Research allocation policy

- 参考 RD-Agent(Q) 的 bandit 调度思路，建立 AATS research allocation policy。
- 初始 action arms：factor proposal、model proposal、execution policy proposal、risk budget proposal、regime classifier、validation/stability。
- reward 不直接用 LLM 判断，使用 deterministic metrics 加权。
- 验收标准：policy 输入输出可审计；低质量/高风险方向会被降权；每次调度有 reason trace。

### P6: RD-Agent-style sandbox proposal loop

- 引入隔离 sandbox worker，只能访问只读研究数据、只读 benchmark config、临时 workspace 和限定预算的 LLM API。
- 输出 `SandboxProposal`、candidate code patch、实验报告和 recommendation draft。
- 自动生成代码必须经过 static scan、unit test、schema validation、deterministic replay、OOS test、cost-adjusted backtest、governance approval。
- 验收标准：sandbox 不可访问 `.env.*`、OKX key、Operator write API、production DB write URL；任何失败默认 fail closed。

### P7: Operator and automation integration

- Operator 只展示研究状态、候选质量、gate 阻断原因、recommendation lineage。
- 后续若设置定时自动开发任务，该任务只能按本计划每次选择一个 bounded slice 实现，不能部署、不能 apply、不能触碰 live execution。
- 验收标准：自动化任务报告必须列出 changed files、tests run、failed tests、residual risk 和 next slice。

## Input/output interfaces

输入：

- Gold replay bars、funding、未来 orderbook/trades 研究数据。
- `configs/rdp_workflows/*.json` workflow 配置。
- active/frozen parameter set 只读快照。
- sandbox proposal 或人工编写的 `ExperimentSpec`。

输出：

- `artifacts/research/...` 下的 experiment artifacts。
- `round_manifest.json` / `experiment_manifest.json`。
- `metrics_snapshot.json`。
- `candidate_factors.json` / `candidate_models.json` / `candidate_parameters.json` / `candidate_execution_policies.json`。
- governance recommendation draft。

禁止输出：

- live order。
- active parameter direct mutation。
- OKX write command。
- 未经过 gate 的 production config mutation。

## Database schema / tables / indexes / constraints

- P0-P2 优先使用文件 artifact 与现有 registry，不强制新增 migration。
- 若 P3 需要 DB-first，则新增 research schema 表前必须单独写 migration SOW。
- 候选表必须包含 source experiment、dataset version、code version、metrics snapshot、status、created_at、superseded_by。
- 任何 active parameter 相关写入仍走现有 governance / pre-apply gate / apply history，不在 Research Factory 中绕开。

## Transactions, consistency, concurrency

- Artifact 写入必须使用临时文件 + atomic replace。
- Experiment status 必须支持 pending/running/succeeded/partial_success/failed/cancelled。
- 同一个 experiment id 重跑必须幂等：要么拒绝覆盖，要么创建新的 attempt id。
- Scheduler/automation 并发必须依赖 task queue 或 lock file，避免两个研究任务写同一个 artifact root。

## Authorization, authentication, data security

- 不读取、不打印、不复制 `.env.wsl2`、`.env.*.live`、OKX key、DB 密码或 token。
- Sandbox 只能使用只读数据和临时目录。
- 不允许 sandbox 访问 production DB write URL。
- 不允许 research agent 调用 Operator write endpoints。
- 所有自动化开发任务必须保留用户可审查的 diff、测试输出和风险说明。

## Error handling and idempotency

- 数据缺失、feature 空值、label 无效、metrics 不完整时默认 failed，不生成 recommendation。
- RD-Agent 风格 proposal 失败时只记录 failure feedback，不自动降级为 live 变更。
- 指标提升不足、OOS 退化、成本后收益为负、执行可行性不足、回撤超限时拒绝 promotion。
- 失败产物必须保留 enough context 用于复现，但不能包含 secret。

## State transition and lifecycle

```text
draft proposal
  -> validated experiment spec
  -> running experiment
  -> metrics recorded
  -> candidate
  -> recommendation draft
  -> approved recommendation
  -> pre-apply gate pass
  -> active parameter apply
```

Research Factory 只负责到 recommendation draft / candidate；后续仍由现有治理链负责。

## Caching and performance

- Dataset 生成可以缓存，但 cache key 必须包含 dataset spec、code version、processor version 和 source data watermark。
- Feature 计算应支持 incremental rebuild，但第一版不牺牲 correctness。
- 大规模扫描应限制并发和预算，避免影响 live stack 所在机器资源。

## Logging, monitoring, auditing

- 每个 experiment 必须记录 started_at、finished_at、status、failure reason、input refs、output refs。
- 每个 automated research slice 必须记录选择原因、跳过项、测试结果和残余风险。
- 指标快照必须能追溯到具体 dataset window 和 code version。
- Operator 只展示摘要和 lineage，不展示 secret 或原始敏感配置。

## Testing strategy

基础验证：

- `.venv\Scripts\python.exe -m ruff check aats/ --fix`
- `.venv\Scripts\python.exe -m pytest tests/unit/ -x -q`
- 受影响的 WSL2 集成测试。

分阶段测试：

- P0：schema validation、serialization、artifact path、status lifecycle 单元测试。
- P1：dataset split、time zone、funding alignment、missing data、leakage guard 测试。
- P2：factor DSL parser/evaluator、dangerous expression rejection、benchmark metrics 测试。
- P3：recorder atomic write、manifest validation、candidate/recommendation bridge 测试。
- P4：cost attribution、execution realism metrics、OOS degradation gate 测试。
- P5：allocation policy deterministic reward、reason trace、bad arm downweight 测试。
- P6：sandbox permission denial、static scan、failed proposal recording、no-secret-access 测试。

## Migration, rollback, compatibility

- 第一阶段只新增 docs、research-only 模块和 tests，不改变 live runtime 行为。
- 任一阶段回滚应只影响 Research Factory，不影响 existing `research_cycle`。
- 如果引入可选依赖，必须放入 research-only extra 或 sandbox 镜像，不进入生产 runtime 默认路径。
- 老的 RDP artifacts 和 governance registry 必须继续可读。

## Configuration and environment isolation

- `configs/rdp_workflows/*.json` 仍是 RDP workflow 配置真源。
- Research Factory 新配置建议放入 `configs/research_factory/`。
- Sandbox 配置必须显式声明 allowed roots、denied env patterns、network policy、timeout、token budget。
- Windows 开发与 WSL2 集成测试路径必须分别验证。

## Code organization and dependencies

- 优先实现 AATS-native contract，不直接复制 Qlib/RD-Agent 代码。
- 不新增全局生产依赖；如需 pandas/lightgbm/torch 等 research deps，应评审是否已经存在以及是否会污染 runtime image。
- 公共指标和 artifact validator 应复用现有 `aats/data_platform/governance`、`metrics`、`operations` 能力。
- 不做无关 refactor，不改 OKX adapter、execution、ledger、reconciliation。

## Documentation and operations manual

- 本计划书作为后续重构总 SOW。
- 每个 phase 开始前应补充更小的 implementation SOW 或 task doc。
- 自动化开发任务的 prompt 必须引用本计划书和 `CLAUDE.md` / `AGENTS.md` 的 live trading 约束。
- Operator runbook 更新只在实际新增 UI/API 行为后进行。

## Deployment and acceptance criteria

全局验收标准：

- Research Factory 能复现同一输入下的同一指标。
- 每个候选因子/模型/参数都有 dataset、code、metrics、artifact lineage。
- 研究指标必须包含成本后收益、回撤、执行可行性和稳定性。
- RD-Agent 风格自动研发只能输出 candidate/recommendation，不能越过 governance gate。
- Live runtime 只读取 approved active parameter artifact，不读取 Research Factory 中间产物。
- 所有实现阶段均通过 lint、unit tests、受影响 WSL2 集成测试，并报告精确结果。

## Initial backlog

1. 新增 `research_factory` contract skeleton 与单元测试。
2. 实现 `DatasetSpec` / `ExperimentSpec` / `MetricsSnapshot` schema validation。
3. 实现 artifact manifest writer + validator，与现有 `round_manifest.json` 对齐。
4. 实现 Gold replay bars dataset handler 和 train/valid/test split。
5. 实现第一版 crypto factor DSL，只支持安全白名单函数。
6. 实现 baseline benchmark harness，输出 cost-adjusted metrics。
7. 实现 experiment recorder 与 candidate artifact 生成。
8. 实现 deterministic promotion gate，不依赖 LLM 主观判断。
9. 实现 research allocation policy。
10. 实现隔离 sandbox proposal loop，并证明无 secret / no live write access。
