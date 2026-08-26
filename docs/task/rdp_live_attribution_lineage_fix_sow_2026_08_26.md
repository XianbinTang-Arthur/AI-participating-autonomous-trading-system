# RDP 实盘归因链路失真修复任务书

> 文档状态：实施任务书 / 历史证据
> 编写日期：2026-08-26
> 起始代码基线：`51448768bb3ff08fa44066d286f7383800d8d744`
> 核对范围：RDP 编排、Phase 3 实盘归因、交易意图持久化、readiness 判定、数据库迁移与测试
> 运行时边界：本文不证明当前容器、数据库、交易所账户或数据新鲜度；本轮不补采、不回填历史市场数据，也不触发真实资金操作。

## 1. 业务目标与边界

修复完整 RDP 在已注入 `RDP_LIVE_DATABASE_URL` 时仍被编排为 `replay_only`，以及 Phase 3 仅凭宽松时间窗口把 replay 与 live intent 关联、readiness 在零对齐时仍可能通过的问题。

本轮只修代码、schema、门禁和文档。历史强平、盘口、成交、OI/funding、volume baseline 等数据覆盖度不在本轮变更范围；旧 intent 也不做猜测性回填。

## 2. 模块职责与领域模型

- `research_cycle` / `rdp_run_full_pipeline.py`：决定完整研究是否显式进入 replay-only；缺少 live DB 时必须失败关闭。
- `StrategySleeveIntent`：持久化产生该意图时的 family、symbol、timeframe、信号 K 线、市场数据时点、参数来源、部署代次、代码版本及快照引用。
- Phase 3 alignment：只使用显式 lineage 精确对齐；旧记录或缺字段记录标为不可归因，不按 `created_at` 猜测。
- Phase 3 waterfall：使用当前真实枚举（`override_target` / `hold_current`、大写订单状态），并按每条 intent 当时可见的 reconciliation snapshot 归因，禁止用窗口末尾状态覆盖整个历史窗口。
- readiness evaluator：要求 live 查询成功、至少存在一个精确对齐样本、且没有不可归因 live lineage。

## 3. 输入/输出接口

- 完整 RDP 默认读取进程环境中的 `RDP_LIVE_DATABASE_URL`；只有操作员显式传入 `--replay-only` 才允许纯回放。
- Phase 3 每个 combo 输出扩展后的 alignment 统计：`aligned`、`replay_only`、`live_only`、`unattributable`。
- Phase 3 round manifest 和 DB snapshot 保存 `live_query_succeeded` 与每个 combo 的 alignment 统计，供 Phase 6 使用。

## 4. 数据库 schema、表、索引与约束

在 `strategy_sleeve_intents` 增加向后兼容的可空字段：

- `timeframe`
- `signal_bar_start` / `signal_bar_end`
- `market_data_asof`
- `parameter_set_id`
- `runtime_generation`
- `code_version`
- `market_snapshot_ref` / `feature_snapshot_ref`

增加 `(family, symbol, timeframe, signal_bar_start)` 查询索引。字段保持可空以允许旧库无损迁移；新 runtime 负责尽可能写齐，readiness 对缺失记录失败关闭。

## 5. 事务、一致性与并发

intent 专用列和 JSON payload 在同一 ORM session/事务写入。迁移由现有 root migration ledger 与 advisory lock 串行执行；不在应用启动期间执行 DDL。对齐是只读离线过程，不修改主交易库。

## 6. 权限、认证与数据安全

RDP 只通过既有只读 live DB 边界查询。命令日志不得打印 live DSN；本轮不读取或输出 `.env.*` 内容、密码、token 或账户标识。

## 7. 错误处理与幂等

- 未显式 `--replay-only` 且无 live DB 配置：在进入 Phase 3 前返回参数/配置错误。
- live 查询或 schema 不满足：combo/round 失败，不能伪装为 replay 成功。
- migration 使用 `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`，由 checksum ledger 保证一次性应用。

## 8. 状态转换与生命周期

`replay_only` 只能来自显式用户意图。live round 必须经历“DB 已配置 → 查询成功 → lineage 精确对齐 → readiness 检查”；任一步缺证据都保持 `not_ready_attribution_issue`。

## 9. 缓存与性能

新增复合索引支持 family/symbol/timeframe/bar 查询。Phase 3 继续按研究窗口批量只读，不引入主交易热路径额外数据库查询；参数 provenance 随启动时已加载 registry 注入 settings。

## 10. 日志、监控与审计

记录模式、live 查询成功状态、精确对齐数量和不可归因数量；DSN 在命令展示中脱敏。round manifest、DB snapshot、CSV 与结论文档保留同一统计口径。

## 11. 测试策略

- 编排：环境存在 live DB 时不自动附加 `--replay-only`；缺失时 fail closed；显式 replay 保留。
- 模型/持久化：lineage 同步写入专用列和 payload。
- 对齐：跨 timeframe 不匹配；精确 bar 可匹配；旧 lineage 标为不可归因。
- readiness：零 aligned、live query 失败、存在不可归因记录均阻塞；有效精确样本可继续评估其他门。
- migration/schema：模型列、SQL migration 与 live facts contract 一致。

## 12. 迁移、回滚与兼容

迁移只新增可空列和索引，不重写旧行。回滚如必须执行，仅在停服且确认没有新代码依赖字段后删除索引/列；常规回滚优先回退应用镜像并保留新增列。旧 JSON payload 仍可由 Pydantic 默认值读取。

## 13. 配置与环境隔离

模拟部署继续由标准 `derivatives` profile 注入 live simulation DB URL；所有 live profile 现有硬门不变。`runtime_generation` 使用标准部署代次，`code_version` 从代次中提取提交前缀；非标准本地 runtime 允许为空，但其记录不能满足生产归因门。

## 14. 代码组织与依赖

复用现有 schema、repository、root migration、Phase 3 artifact 和 decision evidence 模块；不新增外部依赖，不改公共 HTTP API。

## 15. 文档与运维手册

同步更新 RDP 当前入口、live schema contract 与 Operator 流程，明确默认 live attribution、显式 replay-only 和旧 lineage 的停止边界。

## 16. 部署与验收标准

本轮代码验收：Ruff、受影响目标测试、完整 Windows unit 和最窄 WSL2 integration。只有用户后续明确要求部署时才通过唯一标准入口部署；本轮不运行完整 RDP、不应用建议、不触发真实资金操作。

完成标准：

1. 完整 RDP 不再被配置或父进程隐式降级为 replay-only；
2. 新 intent 持久化可验证的 lineage；
3. Phase 3 不再用 `created_at` 宽窗猜测 timeframe/bar 对齐；
4. 零精确对齐或 lineage 缺失不能通过 readiness；
5. 旧记录保留且明确不可归因，没有历史数据伪造。

## 17. 实施结果（2026-08-26）

- `research_cycle` 与完整 pipeline 已改为默认 live attribution；缺少只读 live DB 配置时返回失败，CLI 连接串通过子进程环境传递且不进入命令日志/进程参数。
- 新 intent 已在模型、PostgreSQL 专用列与 JSON payload 同事务写入 attribution lineage；迁移 `006_strategy_sleeve_intent_attribution_lineage.sql` 增加字段和复合索引。
- 对齐已改为 `family + symbol + timeframe + signal_bar_start` 精确键；旧行或缺字段行输出 `unattributable`。
- waterfall 已对齐现行 route/order/bundle 枚举，数值判断使用 `Decimal`，预算或 reconciliation 证据缺失时失败关闭；reconciliation 按每条 intent 的发生时刻做 as-of 查询，不再用窗口末尾状态覆盖历史。
- 首次 PostgreSQL 初始化时，纯注释 baseline migration 不再作为空 SQL 发送；迁移后 runtime schema guard 会核验 lineage 列确实存在。
- 验证结果：Ruff 通过；完整 Windows unit `4699 passed, 30 skipped, 94 subtests passed`；WSL2/Testcontainers PostgreSQL migration + as-of query `2 passed`。验证仅使用隔离测试数据库。

未执行：历史数据补采/回填、完整 RDP、参数应用、服务部署或真实资金操作。
