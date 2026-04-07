# RDP 代码模块参考

> 本文档从 README 抽出，汇总所有 RDP 代码模块的职责清单。
> 概览请参阅 [README § 21](../../README.md)。

## Phase 1 数据仓库（`aats/data_platform/`）

| 文件 | 职责 |
|------|------|
| `config.py` | Pydantic 配置，从 `.env.research` 加载 `RDP_` 前缀环境变量 |
| `db.py` | 连接池管理 (pool_size=5, max_overflow=10) + migration runner |
| `models.py` | CandleRow / FundingRow / ReplayBarRow 数据类 + 表名解析器 |
| `collectors/backfill/file_discovery.py` | ZIP 文件扫描、SHA256 去重、meta 注册、目录 timeframe 推断 |
| `collectors/backfill/file_parser.py` | OKX CSV/ZIP 解析、header 标准化 (BOM/引号/空格容错) |
| `collectors/backfill/candles_backfill_collector.py` | candle 历史回填编排 + timeframe 路由决策 |
| `collectors/backfill/funding_backfill_collector.py` | funding 历史回填编排 |
| `collectors/rolling/candles_api_collector.py` | OKX REST API candle 增量采集 + 去重 + checkpoint |
| `collectors/rolling/funding_api_collector.py` | OKX REST API funding 增量采集 + 去重 + checkpoint |
| `normalize/time_normalizer.py` | ms epoch -> UTC datetime 转换 |
| `validate/candle_quality_checker.py` | candle 质量检查（重复/缺失/乱序/OHLC/volume） |
| `validate/funding_quality_checker.py` | funding 质量检查（重复/乱序/null rate） |
| `validate/report_writer.py` | 质量报告写入 `meta.quality_reports` |
| `merge/bronze_merger.py` | staging -> bronze upsert |
| `merge/silver_merger.py` | bronze -> silver upsert |
| `merge/merge_pipeline.py` | 端到端编排：validate -> bronze -> silver + 质量门控 |
| `gold/funding_aligner.py` | as-of join：funding rate 对齐到 candle bar |
| `gold/replay_bar_builder.py` | Gold replay bar 构建 + upsert |
| `jobs/checkpoint_manager.py` | checkpoint 水位线管理 (get/upsert/advance) |
| `jobs/run_registry.py` | ingest_run / run_item 生命周期管理 |
| `jobs/gap_repair.py` | Silver 层 gap 检测 + repair run 创建 |

## Phase 2 参数研究（`aats/data_platform/replay/`）

| 文件 | 职责 |
|------|------|
| `core/replay_context.py` | 数据模型：ReplayBar, ReplayCostConfig, ReplayParameterOverrides, ReplayDecision, ReplayState（含统一 Edge Contract 定义） |
| `core/replay_runner.py` | 逐 bar 重放引擎（读取 Gold bars -> 调用 adapter -> 输出决策列表） |
| `core/replay_result_writer.py` | 产物写入器（CSV / JSON） |
| `adapters/base_adapter.py` | 策略适配器抽象基类（统一 evaluate_bar 接口） |
| `adapters/independent_adapter.py` | Independent 策略 replay 适配器（signal 来自 OHLCV 因子，edge 4 层分解） |
| `adapters/directional_adapter.py` | Directional 策略 replay 适配器（signal 来自 SMA + return 混合，edge 4 层分解） |
| `registry/experiment_registry.py` | 实验元数据 CRUD + summary upsert（写入 research schema） |
| `diagnostics/replay_diagnostics.py` | 诊断计算 + 多组对比（含 edge 分解统计：signal / funding / cost） |
| `scan/parameter_grid.py` | 参数网格定义与展开（DEFAULT_PARAMETER_GRID, build_grid） |
| `scan/scan_runner.py` | 批量扫描引擎（支持 partial_success 状态 + failed_combos.json 产物） |
| `reports/markdown_report_builder.py` | Markdown 报告生成（含 Edge Breakdown 表格 + edge 来源分析） |

## Phase 3 Live Attribution（`aats/data_platform/attribution/`）

| 文件 | 职责 |
|------|------|
| `taxonomy.py` | 统一归因分类（10 个 category, 30+ reason code, 严格瀑布顺序） |
| `alignment.py` | Replay/live 事件按 bar 时间窗口对齐 + live DB SQL 查询（7 张表） |
| `layer_classifier.py` | 瀑布式分层归因引擎（8 层 waterfall，停在第一层失败处） |
| `aggregation.py` | category × reason 聚合 + top failure modes + layer analysis |
| `report_builder.py` | Markdown 报告（单次 + 批量结论，含交叉 family/tf 比较） |

## Phase 4 Execution Realism（`aats/data_platform/execution_realism/`）

| 文件 | 职责 |
|------|------|
| `market_alignment.py` | Gold bar 查询 + replay decision → bar 对齐（OHLCV + volume 匹配） |
| `fill_feasibility.py` | Volume-based 可成交性评估（4 类：fully/partially/not fillable, no data） |
| `slippage_estimator.py` | V1 Bar-proxy 滑点模型（half-spread + sqrt impact, 成本调整后 edge） |
| `execution_cost_model.py` | 执行成本汇总（分布统计 + Phase 2 比较 + edge 正负分析） |
| `aggregation.py` | 跨 family/timeframe 比较表 + 交叉发现生成 |
| `report_builder.py` | Markdown 报告（单次 realism report + Phase 4 conclusion） |

## Phase 5 Governance（`aats/data_platform/governance/`）

| 文件 | 职责 |
|------|------|
| `manifest_validation.py` | Round manifest 规范校验 + 旧版 manifest 自动补全（normalize_legacy_manifest） |
| `artifact_index.py` | 全局 artifact 索引构建（experiments + rounds，含 diagnostics 摘要提取） |
| `parameter_registry.py` | 参数版本治理 CRUD（draft/candidate/frozen/deprecated + 从 candidates/recommendations 导入） |
| `round_status.py` | Active round 索引构建（按 phase 分组 + latest round 提取） |
| `retry_logic.py` | 失败 round 重跑计划生成（自动构建 per-combo / 整轮重跑命令） |
| `quality_monitor.py` | 四维质量巡检（artifact/结果/参数/治理层 × critical/warning/info） |
| `_atomic_io.py` | 原子 JSON 写入（tmpfile → fsync → replace，防并发损坏） |

## Phase 6 Decision System（`aats/data_platform/decision_system/`）

| 文件 | 职责 |
|------|------|
| `evidence_bundle.py` | 跨 Phase 2/3/4/5 证据统一收集与完整度评估 |
| `candidate_selector.py` | 规则化参数评分：4 维度（研究/归因/执行/治理）→ promote/hold/reject |
| `decision_engine.py` | Family/Timeframe 状态决策：keep_active/lower_priority/pause/require_review |
| `readiness_evaluator.py` | 7 项 check 评估上线就绪度 |
| `recommendation_registry.py` | Recommendation + Active Decision + Evidence Bundle 三个 registry 管理 |
| `report_builder.py` | 7 节结论文档生成 |

## Production Workflow（`aats/data_platform/production_workflow/`）

| 文件 | 职责 |
|------|------|
| `workflow_dispatcher.py` | JSON 配置驱动的工作流调度器（4 种 workflow type） |
| `pre_apply_gate.py` | 参数应用前置门控（block/warn/pass） |

## Operations（`aats/data_platform/operations/`）

| 文件 | 职责 |
|------|------|
| `failure_registry.py` | 失败记录注册（record/find/retry/status lifecycle） |
| `retry_manager.py` | 重试管理（单任务/整工作流重试 + 自动故障录入） |
| `reliability_checks.py` | 7 项可靠性检查（质量监控/活跃决策/工作流/产物/故障/发布/参数） |
| `alerting.py` | 告警摘要构建 + 历史管理 + 确认 |
| `environment_guard.py` | 环境隔离策略（dev/staging/prod 权限矩阵） |

## Metrics（`aats/data_platform/metrics/`）

| 文件 | 职责 |
|------|------|
| `definitions.py` | 24 个指标定义（研究/归因/执行/运维/可靠性 5 层） |
| `metric_calculator.py` | 指标计算器（从各 registry/artifact 聚合计算） |
| `metric_registry.py` | 指标快照生成 + 滚动历史 + 快照比较 |
| `baseline_comparison.py` | 基线比较（3 种策略：前版/同组合/冻结参数） |
| `release_effectiveness.py` | 发布有效性评估（行为/执行/运维/治理 4 维度） |
| `periodic_review.py` | 周期性评审（周/月，含 combo ranking + 改进建议） |
| `backlog_builder.py` | 改进积压自动检测（6 个来源） + 合并管理 |

## Integration Layer（主交易系统整合）

| 文件 | 职责 |
|------|------|
| `aats/data_platform/live_query_adapter.py` | Live DB 只读查询适配器（7 张表统一收口、时间窗口查询、健康检查） |
| `aats/bootstrap/active_parameters.py` | Active Parameter Set 加载器（启动时注入 family/tf 参数，参数映射，原子写入） |
| `aats/api/rdp_routes.py` | RDP 只读 API 路由（8 个 GET 端点） |
| `aats/services/operator/rdp_queries.py` | RDP 查询服务（从治理/决策 artifact 读取结构化数据供 API 使用） |
