# 收益可信度与交易就绪全链路整改 SOW

> 文档状态：实施中任务书 / 历史证据
> 最后核对：2026-08-25（变更前基线 `c5124eb8dc91246dbf0319d1b6b0fc9e662a3a5b`）
> 核对范围：静态代码、测试契约与后续受控 derivatives 模拟运行
> 运行时边界：本文不证明真实账户、真实订单、真实仓位、真实资金安全或生产就绪；所有真实资金 profile 继续失败关闭且没有 override。

## 1. 业务目标与边界

本工作把当前“容器健康但微观结构为空、旧研究产物不可作为资金证据、成交模型仅为
OHLCV 代理、参数发布无 runtime 读回、部署证据固定为非交易就绪”的状态，整改为一条
可审计、可复现、默认失败关闭的收益证据与模拟交易就绪链路。

目标：

1. derivatives 模拟栈持续采集不需要交易凭证的 OKX 公共微观结构与强平数据；
2. 数据是否可用于研究由独立、可配置、带 fingerprint 的 eligibility gate 决定；
3. 所有旧 `benchmark_segment=test` 候选显式失去资金资格，并可生成 v2 重跑计划；
4. Research Factory 具备 purged walk-forward、block bootstrap、成本压力、多重检验和一次性
   holdout 访问账本；
5. 新增 L2/event 成交回放和模拟成交生命周期校准，OHLCV 代理不得冒充盘口证据；
6. 参数变更使用 execution-owned generation、prepare/commit/readback/rollback 状态机；
7. 真实 Redis/NATS 故障矩阵和交易就绪 evidence schema 可执行且失败关闭；
8. future canary 仅作为受门禁的配置契约存在，本工作不解除 live NO-GO、不触发真实订单。

非目标：

- 不承诺策略盈利；
- 不把模拟盘、回测或静态测试写成生产证明；
- 不读取或展示 `.env.*`、API key、密码或 token；
- 不直接执行 `docker compose`，不使用 rsync；
- 不让 AI 或研究 recommendation 获得真实资金最终交易权；
- 不自动解封普通 `spot-live`、`derivatives-live` 或 `derivatives-live-monolith`。

## 2. 模块职责与领域模型

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| Compose/deploy | 启动模拟主进程和公共采集器，验证容器及证据 | 证明生产就绪 |
| microstructure quality | 连续/稀疏频道分类、freshness、sample count、fingerprint | 产生交易信号 |
| capital eligibility | 单一资金资格判定、旧产物失效、原因码 | 修改历史 artifact |
| Research Factory | v2 development、统计证据、一次性 holdout | 自动发布运行参数 |
| L2 replay | 按时间因果回放盘口、队列、partial/no-fill、成本 | 声称等同真实撮合 |
| lifecycle calibration | 对齐模拟订单、command、state history、fill | 读取/保存凭证 |
| parameter activation | prepare/commit/readback/rollback generation | 绕过双人签和 Kill Switch |
| fault matrix | 受控模拟依赖分区、重启、TTL 与恢复验证 | 在 live 栈做破坏性演练 |
| readiness evidence | 聚合无秘密证据并保持 unknown=NO-GO | 接受手工 ready override |
| canary contract | future 最小权限、最小风险边界 | 本工作实际启动 live |

核心领域对象：

- `MicrostructureEligibilityReport`
- `CapitalEligibilityDecision`
- `CandidateReplayPlan`
- `WalkForwardEvidence`
- `StatisticalEvidence`
- `HoldoutAccessRecord`
- `L2ExecutionEvidence`
- `ExecutionCalibrationReport`
- `ParameterActivationOperation`
- `ParameterRuntimeAck`
- `FaultMatrixEvidence`
- `TradingReadinessEvidence`

## 3. 输入与输出接口

输入仅允许：受版本控制的配置、公共市场数据、RDP/模拟交易数据库只读视图、显式 actor/
reason、当前 Git/image/schema/runtime generation。所有 CLI 均不得自行读取 `.env` 文件，连接
由调用进程环境注入。

主要输出放在 `artifacts/`，默认不可覆盖：

- `artifacts/research/microstructure_eligibility/`
- `artifacts/research/research_factory/artifact_eligibility_registry.jsonl`
- `artifacts/research/research_factory/replay_plans/`
- `artifacts/research/research_factory/experiments/<id>/`
- `artifacts/research/holdout_access/`
- `artifacts/research/l2_execution/`
- `artifacts/research/execution_calibration/`
- `deploy/wsl2-dev/runtime/fault-matrix-evidence/`
- `deploy/wsl2-dev/runtime/trading-readiness-evidence/`

任何状态变更 API 初始只能返回 `202 pending`；只有持久层、execution authority 和所有预期
worker readback 一致时才能出现终态 `succeeded`。

## 4. 数据库 Schema、索引与约束

新增 schema 由现有显式 schema job 管理，并提供 rollback：

1. `governance.research_holdout_access_ledger`
   - 唯一约束：`(candidate_id, holdout_content_fingerprint)`；
   - 保存 actor、reason、commit、accessed_at、status、artifact hash、error；
   - 第一次读取尝试即占用唯一键，失败也不得静默重试。
2. `governance.parameter_activation_operations`
   - 唯一 `operation_id`、唯一非终态 `(scope, scope_ref)`；
   - generation、from/to set、payload hash、state、actor、错误和时界。
3. `governance.parameter_runtime_acks`
   - 唯一 `(operation_id, process_role, phase)`；
   - generation、payload hash、ack status、ack_at、error。

不修改 OrderState 生命周期，不触碰其 Postgres 列、JSON payload 和 Redis 三层一致性。

## 5. 事务、一致性与并发

- eligibility、统计、L2 和 calibration artifact 采用临时目录写入、fsync、原子 rename；目标已
  存在时失败，不提供隐式覆盖。
- holdout ledger 在读取 test 数据之前提交 `access_started`，防止崩溃后再次偷看。
- 参数 activation 使用 DB row lock + 幂等 operation ID；prepare、commit、rollback 分阶段
  持久化，API 进程不直接改变 worker 内存。
- active parameter truth 只在 execution 确认所有 prepare ack 后原子切换；commit readback
  不一致触发 halt 和 `ROLLBACK_REQUIRED`，不得返回成功。
- fault matrix 只针对隔离模拟拓扑，所有 drill 必须有 cleanup/final-state evidence。

## 6. 授权、认证与数据安全

- 保留现有 session、apply token v2 和 approver != applier 双人签要求；
- holdout actor/reason 必填，命令行不得记录认证 token；
- canary 未来要求无提现权限、symbol 白名单、低杠杆、硬 notional/loss cap 和手动 resume；
- evidence 仅允许无秘密字段，URL、headers、raw exchange payload 必须脱敏或不写入；
- live profile 拒绝逻辑保持在任何 WSL/Docker/DB 副作用之前。

## 7. 错误处理与幂等

- 未知协议、缺失证据、损坏 fingerprint、陈旧数据、部分 worker ack、依赖异常统一失败关闭；
- 所有批处理输出逐项状态，不允许静默跳过；
- replay/holdout/activation/fault matrix 使用稳定幂等键；
- 第二次 holdout 访问必须拒绝；
- readiness 中 `UNKNOWN`、`DEGRADED`、`FAILED` 一律使 `trading_ready=false`；
- canary 不接受 `--force`、`--yes` 或环境变量绕过证据。

## 8. 状态转换与生命周期

参数 activation：

```text
PENDING -> PREPARING -> PREPARED -> COMMITTING -> SUCCEEDED
                     \-> FAILED
COMMITTING -> ROLLBACK_REQUIRED -> ROLLING_BACK -> ROLLED_BACK
```

Holdout：

```text
SEALED -> ACCESS_STARTED -> EVALUATED_PASS | EVALUATED_FAIL | ACCESS_FAILED
```

任何终态不可回退；任何失败访问都保留审计记录。

## 9. 缓存与性能

- 公共 collector 保持现有 batch buffer、独立容器和 512 MiB 限额；
- eligibility 按窗口批量查询，不为每条 tick 单独发 SQL；
- L2 replay 以时间窗口流式加载，禁止把全历史订单簿无界载入内存；
- parameter ack 热通知可用 NATS，但 Postgres 是最终审计真源；
- readiness 只读查询设置有界 timeout，不允许阻塞 Gateway 主 event loop。

## 10. 日志、监控与审计

- collector 输出频道连接、最后消息、最后 flush、样本数和失败原因；
- Prometheus 分别监控 process health、channel freshness 和 research eligibility；
- eligibility、holdout、activation、fault matrix、readiness 均写结构化原因码；
- 不记录密钥、认证 header、数据库 URL 或未脱敏 raw payload；
- evidence 记录 Git commit、schema revision、dataset fingerprint 和 model version。

## 11. 测试策略

1. 单元：连续/稀疏频道、fingerprint、旧协议失效、统计确定性、L2 深度/排队/partial、
   holdout 一次性、activation 状态机和 readiness unknown 传播。
2. 集成（WSL2）：Postgres schema/唯一约束、collector→bronze→silver→eligibility、真实 Redis+
   NATS ack、模拟订单 lifecycle calibration。
3. 故障注入：Redis/NATS 双向和单向断连、execution kill/restart、旧 generation、TTL。
4. 回归：Ruff、完整 unit、最窄 integration、Compose config、部署 shell syntax。
5. 受控运行：标准 derivatives 部署，验证全部 required containers 和新 evidence；绝不运行 live。

## 12. Migration、Rollback 与兼容

- 新表 migration 与 rollback 成对；rollback 不删除已有审计行，必要时只停止新 writer；
- 历史 artifact 原地保留，新 registry 叠加资格状态；
- OHLCV fill model 保留为 `bar_proxy`，不破坏历史解析，但不能进入 capital eligibility；
- profile apply/rollback 在 generation 完整交付前继续 `501`；切换后保留异步状态查询，不静默
  改成同步成功；
- 变更前恢复基线为 `origin/main@c5124eb8dc91246dbf0319d1b6b0fc9e662a3a5b`。

## 13. 配置与环境隔离

- `derivatives` 模拟 profile 增加两个公共 collector；不加载 live env；
- collector 只写 `aats_research`，不连交易执行 command topic；
- fault matrix 使用隔离命名、隔离端口和临时 volume；
- readiness schema 区分 simulation 和 future production；
- canary 配置不加入当前可部署 profile 列表，直到独立 GO 决策。

## 14. 代码组织与依赖

优先复用现有 collector、silver merger、Research Factory、execution repos、Kill Switch 和
deployment evidence；不引入新的网络服务。新增依赖必须进入 lock、供应链与许可证检查，若
Toxiproxy 能由测试容器隔离使用则不进入应用 runtime 依赖。

## 15. 文档与运维手册

实施完成后同步：

- `CLAUDE.md`、`DEPLOYMENT.md`、`ARCHITECTURE.md`；
- `docs/README.md`、`docs/testing/README.md`；
- `docs/operations/README.md` 和对应 collector/readiness/parameter runbook；
- `deploy/wsl2-dev/README.md`；
- 审计 gate 与本任务交付记录。

历史 `deploy/wsl2-dev/RUNBOOK.md` 不升级为现行入口。

## 16. 部署与验收标准

- Windows Ruff 与完整 unit 通过；
- WSL2 最窄 integration 通过；
- Compose config 与 shell syntax 通过；
- 标准 `derivatives` 部署成功，所需应用容器全部 running healthy；
- 连续频道产生新鲜数据，eligibility 与 sparse liquidation 语义正确；
- 当前旧候选全部 `capital_eligible=false`；
- v2 runner、统计、holdout、L2、calibration、activation、fault matrix 和 readiness 均有不可覆盖
  evidence 与失败关闭测试；
- simulation evidence 保持 `production_ready=false`；
- live profile 仍在副作用前非零退出且无 override；
- 不声称盈利，不声称真实账户或生产已验证。
