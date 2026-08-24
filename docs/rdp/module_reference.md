# RDP 代码模块参考

> 最后核对：2026-08-22（代码基线 `be9179e`）。本页按当前目录和运行入口组织，不再沿用早期“少量 Phase 文件清单”。RDP 总览见 [`aats/data_platform/README.md`](../../aats/data_platform/README.md)。

## 1. 根模块

| 文件 | 当前职责 |
| --- | --- |
| `config.py` | `ResearchPlatformSettings`；读取 `RDP_DATABASE_URL`、live 只读库、采集和 artifact 配置；容器中可复用 `AATS_ACTIVE_PARAMETER_DB_URL` |
| `db.py` | Engine/Session、迁移入口和数据库生命周期 |
| `rdp_models.py` | `RdpBase` 的 78 张 ORM 表，覆盖 staging/bronze/silver/gold/meta/research/governance |
| `models.py` | 采集、replay 等轻量领域数据结构和表名解析 |
| `live_query_adapter.py` | 主交易数据库只读查询适配层 |
| `orderbook_diff_payload_contract.py` | orderbook diff payload 契约 |

## 2. 目录总览

当前 `aats/data_platform/` 有 185 个 Python 文件。目录职责如下；数量用于发现明显漏扫，不是公共 API 保证。

| 目录 | Python 文件数 | 职责 |
| --- | ---: | --- |
| `collectors/` | 12 | 历史 ZIP、OKX REST rolling、funding/candles/history 采集 |
| `normalize/` | 3 | 时间与输入标准化 |
| `validate/` | 4 | Candle/funding 质量检查和报告 |
| `merge/` | 5 | staging→bronze→silver、microstructure Silver 合并 |
| `gold/` | 3 | funding 对齐、replay bar 构建 |
| `jobs/` | 4 | checkpoint、run registry、gap repair |
| `replay/` | 26 | replay core、strategy adapters、diagnostics、scan、reports |
| `attribution/` | 6 | live/replay 对齐、瀑布归因、聚合、报告 |
| `execution_realism/` | 7 | fill feasibility、slippage、cost、market alignment |
| `decision_system/` | 8 | evidence、candidate、decision、readiness、recommendation registry |
| `governance/` | 27 | 参数/推荐/active set、任务队列、调度状态、snapshot、tuning 与 apply saga |
| `production_workflow/` | 9 | gate、release、observation、rollback policy |
| `operations/` | 11 | dispatcher、scheduler、failure/retry、reliability、daemon health、tuning review |
| `metrics/` | 9 | 指标、baseline、release effectiveness、periodic review、backlog |
| `live_facts/` | 4 | live 事实只读访问和模型 |
| `research/` | 3 | profile research job 与结果 |
| `research_factory/` | 42 | 证据契约、实验、verdict、治理 review、dry-run/manual apply design |
| `migrations/` | 3 | RDP schema 版本化迁移 |
| `gates/` | 2 | gate 相关共享能力 |
| `runtime/` | 2 | RDP runtime 辅助能力 |

## 3. 数据采集与数仓

### `collectors/`

- `backfill/`：发现和解析 OKX 文件、去重注册、candles/funding/history 回填。
- `rolling/`：通过 OKX REST 增量采集 candles、funding 及历史市场指标。
- 历史文件成功/失败路径和 checkpoint 由 settings、run registry 管理。

### `merge/` 与 `gold/`

- Merge pipeline 执行 validate→bronze→silver，并在质量失败时阻断不合格数据继续传播。
- `microstructure_silver_merger.py` 构建 15m 微观结构事实。
- Gold builder 生成 replay 消费数据和 funding 对齐结果。

### `jobs/`

- `checkpoint_manager.py`：采集水位线。
- `run_registry.py`：ingest run/item 生命周期。
- `gap_repair.py`：gap 识别与 repair run。

## 4. 研究与证据

### `replay/`

包含逐 bar replay、directional/independent adapter、参数网格、批量扫描、诊断和 Markdown/JSON/CSV 产物。Replay 结论必须结合 execution realism 和 live attribution，不能直接视为可发布参数。

### `attribution/`

通过 `live_query_adapter` 只读主交易事实，对 replay/live 事件做时间对齐、分类和瀑布归因。主交易数据库访问必须保持只读事务边界。

### `execution_realism/`

根据 Gold 市场数据评估 fill feasibility、bar-proxy slippage 和执行成本。该层是研究证据，不是交易所成交模拟的绝对真值。

### `research_factory/`

Research Factory 是当前最大的 RDP 子域，覆盖：

- evidence manifest/bundle 和数据集契约；
- experiment plan、执行、结果和 registry；
- verdict board，标准结论为 `reject`、`keep_observing`、`positive_executable_edge`；
- governance review、candidate lineage、quality status；
- dry-run planning 和 manual apply design。

硬边界：只生成证据与人工应用设计，不写 active parameters、runtime settings、managed profile、交易数据库或 OKX。

## 5. Governance 与 Production Workflow

### `governance/`

重要模块：

| 文件 | 职责 |
| --- | --- |
| `parameter_registry.py` | 参数候选 registry；DB-first，保留文件降级/审计语义 |
| `recommendations_db.py` / `parameter_sets_db.py` | recommendation、active decision 和参数版本表访问 |
| `active_params_db.py` | `governance.active_parameter_sets` DB 操作 |
| `profile_apply_saga.py` | 跨 research/live 边界的 profile apply saga 与补偿 |
| `rdp_task_db.py` | 10 个 workflow allowlist、原子入队、SKIP LOCKED claim、状态与孤儿恢复 |
| `operational_state_db.py` | scheduler 等运行状态真源 |
| `snapshot_db.py` | 治理和 research round DB-first snapshot |
| `quality_monitor.py` | 治理质量巡检 |
| `strategy_tuning_db.py` / `system_config_db.py` | 策略 tuning 与系统配置治理数据 |

注意：部分 governance registry 仍是 DB-first + 文件副本/降级；主交易 bootstrap 的 active parameter loader 则是严格 DB-only。两者不能写成同一个“统一文件 fallback”规则。

### `production_workflow/`

- `pre_apply_gate.py` / `gate_rules.py`：发布前硬门和规则。
- `release_cycle.py` / `release_registry.py`：release 生命周期。
- `observation_cycle.py` / `observation_window.py`：发布后观察。
- `rollback_policy.py`：回滚建议和保护。

当前调度层禁用 `release_cycle`，任务队列还在 `ENQUEUE_BLOCKED_WORKFLOWS` 中冻结它。保留代码不代表允许自动执行。

## 6. Operations

| 文件 | 职责 |
| --- | --- |
| `workflow_dispatcher.py` | 加载 10 份 JSON 定义、校验任务并执行 |
| `workflow_scheduler.py` | UTC slot 计算、bootstrap、数据库调度状态、到期入队 |
| `rdp_daemon_health.py` | daemon heartbeat/healthcheck |
| `failure_registry.py` / `retry_manager.py` | workflow failure 与补跑 |
| `reliability_checks.py` / `alerting.py` | 可靠性检查和告警摘要 |
| `strategy_tuning_registry.py` / `strategy_tuning_review.py` | tuning proposal/review 生命周期 |

RDP daemon 的标准容器入口是 `scripts/rdp_task_daemon.py --poll-interval 10 --enable-scheduler`。

## 7. 主交易整合层

| 文件 | 边界 |
| --- | --- |
| `aats/bootstrap/active_parameters.py` | runtime active parameter DB-only loader；数据库失败返回空 registry，不读 JSON fallback |
| `aats/api/rdp_routes.py` | RDP 核心查询、审批、token、apply/rollback、workflow、workbench 和 tuning API |
| `aats/api/rdp_profile_routes.py` | profile recommendation、profile type review、sleeve advice API |
| `aats/api/rdp_apply_token.py` | v2 HMAC apply/rollback token 签发与验证 |
| `aats/services/operator/rdp_queries.py` | Operator RDP 查询聚合 |

FastAPI 当前共有 50 个 `/rdp/*` 路由。完整清单以运行时 `/openapi.json` 和 [项目代码审查与系统说明](../code_review/README.md) 为准。

## 8. 维护规则

1. 新增 ORM 表时同步迁移、模型计数和 RDP 总览。
2. 新增 workflow JSON 时同步 `VALID_WORKFLOWS`、daemon timeout、文档和覆盖测试。
3. 修改 active parameter 路径时必须明确“治理 registry 存储语义”和“runtime loader 真源语义”。
4. 新增写 API 时必须有认证依赖、token/gate（如适用）、审计和负向测试。
5. 带 Phase/Stage/日期的旧设计只作历史证据，不能覆盖本页当前边界。
