# 微观结构研究数据桥接与预注册候选验证 SOW

> 文档状态：实施中任务书  
> 最后核对：2026-08-25（起始 HEAD `64a6c13e2146d5b996111dc0f2760e8021112a81`）  
> 核对范围：Research Factory Gold 数据源、Factor DSL、微观结构 Silver、Gold replay 构建、预注册 development campaign  
> 安全边界：只读研究数据与 research artifact；允许重建 `gold.market_swap_replay_bars_15m`；不读取 holdout 收益、不写运行参数、不提交订单、不启动 live profile。

## 1. 业务目标与边界

当前盈利候选只实际使用 OHLCV 与 funding。2026-08-25 的代码和数据库交叉核对表明，
`silver.market_orderbook_metrics_15m`、`silver.market_trade_flow_15m` 和
`silver.market_oi_funding_metrics_15m` 已保存订单簿、主动成交、持仓量和 basis 信息，但
Research Factory 的 Gold 数据源与 Factor DSL 未暴露这些字段。与此同时，2026-04-20 至
2026-05-29 的 BTC swap Silver 15m K 线有 3,744 行，对应 Gold replay 仅有 246 行，阻断了
同窗研究。

本任务把“已采集但不可研究”的信息接入严格预注册链路，并对至少三个不同经济机制运行
development-only campaign。验收条件不是正收益，而是数据来源、缺失处理、预注册、成本、失败项
和 holdout 边界均可证明；若候选全部失败，必须如实淘汰。

## 2. 模块职责与领域模型

- `GoldReplayDataSource`：仅当表达式引用微观结构字段且 timeframe=`15m` 时，按 `(symbol, ts)`
  左连接对应 Silver 表；普通 OHLC/funding 实验保持原查询路径。
- `GoldBarRecord`：通过受白名单约束的 `feature_values` 携带额外研究字段，不允许任意动态列。
- Factor DSL：首批只开放五个有明确数据语义的字段：`top5_weighted_imbalance`、
  `trade_flow_imbalance`、`oi_delta`、`funding_z_score_7d`、`basis_bps`。
- feature-input quality gate：按实际表达式引用字段统计全窗及 train/valid/test 的非空率，超过
  预注册阈值时失败关闭。
- Gold builder：重建选定历史窗口，使 Gold replay 与同窗口 closed Silver K 线一一对应。

## 3. 输入与输出接口

输入：

- 受版本控制的 `configs/research_campaigns/*.json`；
- 只读 `gold.market_swap_replay_bars_15m` 与三个 microstructure Silver 表；
- campaign 中显式固定的 `max_factor_input_missing_ratio`、时间窗口、成本和假设。

输出：

- 每个 experiment 的 `factor_input_quality_report.json`；
- source watermark 中的所需字段、非空计数、Silver dataset version 与 ingest run lineage；
- 既有 development evidence、campaign evidence 与完整失败清单。

Artifact 只能写入 `artifacts/research/research_factory`，禁止覆盖内容不同的既有证据。

## 4. 数据库 Schema、表、索引与约束

不新增 schema、表、索引或 migration。Gold 重建复用现有
`build_gold_replay_bars()` 的 `(symbol, ts)` upsert 约束。研究查询只连接现有主键：

- `gold.market_swap_replay_bars_15m(symbol, ts)`；
- `silver.market_orderbook_metrics_15m(symbol, ts)`；
- `silver.market_trade_flow_15m(symbol, ts)`；
- `silver.market_oi_funding_metrics_15m(symbol, ts)`。

## 5. 事务、一致性与并发

Gold 重建由一个 ingest run 在事务内完成；失败不得留下伪成功 run。研究读取在单个 SQL 查询中按
时间排序取出 Gold 与 Silver 快照。source watermark 纳入 Gold build run 和微观结构 lineage，任何
源行或版本变化必须改变 dataset fingerprint。并发运行不得覆盖同名 artifact。

## 6. 授权、认证与数据安全

CLI 只从进程环境接收 `RDP_DATABASE_URL`，不加载或打印 dotenv。查询不接触交易账户、API key、
实时资金或 live 参数。development runner 不生成 capital eligibility，不调用参数 apply API，也不
访问交易执行接口。

## 7. 错误处理与幂等

以下情况失败关闭：

- 微观结构字段用于非 15m timeframe；
- 非白名单字段、非有限值、未来引用或不安全 Factor DSL；
- 实际字段缺失率超过预注册阈值；
- Gold 时间缺口、混合 candle version、不可追溯 build run；
- campaign/plan/proposal/card SHA 漂移；
- 数据库或 artifact 写入失败。

内容相同的预注册允许幂等返回；内容不同的同名 campaign/experiment 禁止静默覆盖。

## 8. 状态转换与生命周期

```text
SILVER_AVAILABLE
  -> GOLD_REBUILT
  -> CAMPAIGN_PREREGISTERED
  -> FACTOR_INPUT_QUALITY_PASS | FACTOR_INPUT_QUALITY_FAIL
  -> DEVELOPMENT_RUN
  -> DEVELOPMENT_FAIL | CAMPAIGN_STATISTICS_PASS
  -> P2_L2_REQUEST_ELIGIBLE_ONLY
```

所有状态均保持 `holdout=sealed_not_evaluated`、`capital_eligible=false`。

## 9. 缓存与性能

连接只在表达式引用微观结构字段时启用，避免改变历史 OHLC 查询性能。连接列受固定映射限制，查询
继续使用各表主键。1,152 级别的 15m development 窗口可内存评估；不新增常驻缓存。

## 10. 日志、监控与审计

日志只输出行数、窗口、字段名、artifact 路径、run ID 与摘要哈希，不输出数据库 URL 或凭证。
质量报告必须列出每个引用字段在全窗和各 segment 的 missing count/ratio。Campaign evidence 必须
保留全部注册计划，包括加载失败、质量失败和统计失败。

## 11. 测试策略

- Factor parser/evaluator：新字段白名单、非白名单拒绝、缺失值传播；
- Gold record/dataset：`feature_values` 验证、序列化、fingerprint 变化；
- Gold data source：15m 条件连接、普通 1h 查询不连接、lineage/non-null watermark；
- quality gate：阈值边界、segment 统计、失败原因；
- preregistration/batch：缺失率阈值绑定到 manifest/plan，历史配置兼容；
- 运行 Ruff、相关 unit、完整 unit；数据库链路在 WSL2 derivatives 模拟栈验证。

## 12. Migration、Rollback 与兼容

无数据库 migration。历史 OHLC/funding 表达式、旧 campaign 配置和 plan 保持可读；未提供新阈值时
使用代码默认值，但本轮配置必须显式固定。回滚代码不会删除 Gold 或研究证据，重建行仍是同源
Silver 的派生事实，可由后续 ingest run 再次幂等生成。

## 13. 配置与环境隔离

本轮只使用 `BTC-USDT-SWAP`、`15m`、已完成的历史窗口。Windows 负责代码与测试；WSL2
`derivatives` 模拟环境负责数据库重建和 development campaign。不得启动 `spot-live`、
`derivatives-live` 或 `derivatives-live-monolith`。

## 14. 代码组织与依赖

字段白名单放在 Research Factory feature 模块，数据适配放在现有 Gold dataset/real-data runner，
质量模型放在 `research_factory/features`，CLI 与预注册复用现有脚本。不增加第三方依赖，不复制
回测收益或统计公式。

## 15. 文档与运维手册

完成后更新盈利差距评估、收益验收矩阵和研究运行手册，记录：实际窗口、Gold 修复前后行数、字段
覆盖率、campaign ID、全部候选结果、artifact SHA、holdout 状态及下一门。历史 P1-D 文档只作为
设计背景，不作为本次运行事实。

## 16. 部署与验收标准

- 普通 1h campaign 的 SQL、字段与结果兼容；
- 15m 微观结构表达式可从 Silver 因果读取并进入 dataset fingerprint；
- 所需字段缺失超过显式阈值时，在计算收益前失败关闭；
- 选定窗口 Gold closed rows 与 Silver closed rows 数量一致；
- 至少三个不同机制先预注册、后运行，成本至少包含 fee=5bps、slippage=2bps、funding=0.5bps；
- development 只评估 train/valid，test/holdout 不暴露收益；
- 所有失败候选进入完整 trial count，禁止因结果不好而改阈值重跑；
- Ruff、相关单测、完整 unit 和 WSL2 数据链路验证通过；
- 只有 campaign 统计门通过者才可申请 L2 execution evidence，本任务绝不自动进入实盘。
