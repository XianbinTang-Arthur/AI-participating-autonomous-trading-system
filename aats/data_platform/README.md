# Research Data Platform (RDP)

> 模块级 README。项目级概览见 [主 README §21](../../README.md#21-研究数据平台-research-data-platform)。

RDP 是 AATS 的**离线参数研究子系统**,与实时交易主链路完全解耦。它从市场数据采集开始,经过参数研究、归因分析、执行可行性验证,最终通过受控审批流程把研究结论回灌到主交易系统。

- **数据库**: 独立的 `aats_research` PostgreSQL 库,6 schema 分层
- **配置**: `.env.research`(`RDP_` 前缀),不与交易系统的 `.env.*` 混用
- **不侵入主链**: 主交易引擎从 OKX websocket 直连市场数据,**从不读取 RDP 的 Bronze/Silver/Gold**
- **回灌方式**: 仅通过人工审批的 `configs/active_parameter_sets/*.yaml`

---

## 1. 目录结构

```
aats/data_platform/
├── __init__.py
├── config.py                  # Pydantic 配置 (env_prefix=RDP_)
├── db.py                      # 连接池 + migration runner
├── models.py                  # 数据类 + 表名解析器 (含白名单防 SQL 注入)
├── live_query_adapter.py      # Live DB 只读查询 (供 Phase 3 归因用)
│
├── collectors/                # ── Phase 1: 数据采集 ──
│   ├── backfill/              #   历史 ZIP/CSV 回填 (file_discovery + parser)
│   └── rolling/               #   OKX REST API 增量采集
├── normalize/                 #   时间标准化 (ms epoch → UTC)
├── validate/                  #   质量门控 (candle/funding 检查 + 报告)
├── merge/                     #   staging → bronze → silver upsert pipeline
├── gold/                      #   funding aligner + replay bar builder
├── jobs/                      #   checkpoint / run_registry / gap_repair
│
├── replay/                    # ── Phase 2: 参数研究 ──
│   ├── core/                  #   replay engine + result writer
│   ├── adapters/              #   independent / directional 策略适配器
│   ├── registry/              #   experiment metadata
│   ├── diagnostics/           #   edge breakdown 计算
│   ├── scan/                  #   parameter grid scan
│   └── reports/               #   markdown report builder
│
├── attribution/               # ── Phase 3: Live Attribution ──
├── execution_realism/         # ── Phase 4: Execution Realism ──
├── governance/                # ── Phase 5: 治理 (artifact / 参数 / 质量) ──
├── decision_system/           # ── Phase 6: 闭环决策 ──
│
├── production_workflow/       # workflow_dispatcher + pre_apply_gate
├── operations/                # failure_registry / retry / reliability / alerting
├── metrics/                   # 24 个指标定义 + 历史 + 基线比较
└── live_facts/                # 主系统事实数据查询适配器
```

每个文件的具体职责见 [`docs/rdp/module_reference.md`](../../docs/rdp/module_reference.md)。

---

## 2. 数据架构

### 6 schema 分层

```
meta      元数据    运行记录、checkpoint、质量报告、文件注册
staging   原始入库  保留 raw_symbol / raw_ts / source_file_id 全链路溯源
bronze    去重     PK=(symbol, ts) upsert,保留原始字段
silver    标准化   质量验证后的规范数据
gold      回放     candle + funding rate as-of join 对齐后的 replay bars
research  研究     实验元数据、诊断摘要、扫描批次
```

共 44 张表,通过 `migrations/research/0001-0012` SQL 文件管理。

### 数据流(2026-04-07 起)

```
                    ┌─────────────────────────────────┐
                    │      OKX (REST API + ZIP)       │
                    └─────────────┬───────────────────┘
                                  │
        ┌─────────────────────────┼──────────────────────────┐
        │                         │                          │
        ▼                         ▼                          ▼
┌──────────────┐         ┌────────────────┐         ┌────────────────┐
│ historical   │         │  daily_ingest  │         │  deep_backfill │
│ daemon       │         │ (cron 04:00 UTC│         │  (一次性灾后    │
│ (--once 模式 │         │  每天 1 次)    │         │   恢复)         │
│  消费 ZIP)   │         │                │         │                │
└──────┬───────┘         └────────┬───────┘         └───────┬────────┘
       │                          │                         │
       └──────────────────────────┼─────────────────────────┘
                                  │
                                  ▼
                  ┌─────────────────────────────┐
                  │  staging  →  bronze         │
                  │            ↓                │
                  │      silver (质量门控)      │
                  │            ↓                │
                  │      gold (replay bars)     │
                  └─────────────┬───────────────┘
                                │
                                ▼
                  ┌─────────────────────────────┐
                  │  Phase 2-6 研究管线          │
                  │  (replay / attribution /    │
                  │   execution / governance /  │
                  │   decision)                 │
                  └─────────────────────────────┘
```

> **历史变更**: 2026-04-07 之前数据采集走 60s tick 常驻 daemon。该模式 99% 调用浪费(没有任何 RDP 消费方需要 intra-minute 数据),已退役为日批。详见 [`docs/operations/rdp_scheduling_strategy.md` § "数据采集迁移到日批"](../../docs/operations/rdp_scheduling_strategy.md#数据采集迁移到日批)。

---

## 3. 关键入口脚本

> 完整脚本清单见 [主 README § scripts](../../README.md)。本节只列日常会用到的。

### Phase 1 — 数据采集

| 脚本 | 用途 | 调用频率 |
|---|---|---|
| **`scripts/rdp_run_daily_ingest.py`** ★ | 日批增量采集 (candles + funding + Gold + Gap) | cron 每天 1 次 |
| `scripts/rdp_historical_daemon.py --once` | 消费 `incoming/` 目录的 ZIP | 拖完 ZIP 后手动 1 次 |
| `scripts/rdp_init_db.py` | 初始化 schema (运行 migration 0001-0012) | 首次部署 1 次 |
| `scripts/rdp_build_gold_all.py` | 全量重建 Gold replay bars | 灾后恢复或 schema 变更后 |
| `scripts/rdp_detect_gaps.py` | Silver 层 gap 巡检 | 排查数据缺口时 |
| `scripts/rdp_deep_backfill_api.py` | 深度回拉(REST,跨多月) | 灾后恢复 |

**已退役为薄壳**(仍可调用,会打印 deprecation 警告并转发到 daily_ingest):

- `scripts/rdp_realtime_daemon.py` → 转发 `daily_ingest`
- `scripts/rdp_start.py` → 顺序调用 `daily_ingest` + `historical_daemon --once`

### Phase 2-6 — 研究管线

| 脚本 | 用途 |
|---|---|
| **`scripts/rdp_run_full_pipeline.py`** ★ | 一键编排 Phase 2 → 3 → 4 → 5 → Decision |
| `scripts/rdp_run_replay.py` | 单次 replay 实验 |
| `scripts/rdp_run_parameter_scan.py` | 参数网格批量扫描 |
| `scripts/rdp_run_step2_research.py` | Step 2 完整研究闭环 |
| `scripts/rdp_run_phase3_round.py` | Phase 3 归因批量轮次 |
| `scripts/rdp_run_phase4_round.py` | Phase 4 执行可行性批量轮次 |
| `scripts/rdp_run_decision_round.py` | Phase 6 闭环决策完整轮次 |

### 调度入口

| 脚本 | 用途 |
|---|---|
| **`scripts/rdp_run_scheduled_workflow.py`** ★ | 统一 workflow 入口 (`--workflow data_maintenance` 等) |

---

## 4. 快速上手

### 4.1 配置

```bash
cp configs/templates/.env.research.example .env.research
# 编辑 .env.research, 至少填入 RDP_DATABASE_URL
```

### 4.2 初始化数据库

```bash
# 自动建库 + 迁移 0001-0012 (44 张表)
python scripts/rdp_init_db.py
```

### 4.3 拉取数据

**方式 A — 历史数据(从 ZIP)**:

```bash
# 1. 把 OKX ZIP 放到约定目录, 子目录名决定 timeframe:
#    data/historical/incoming/candles_swap/15m/BTC-USDT-SWAP-candles-2026-03-15.zip

# 2. 消费一次 (扫描 → staging → bronze → silver → 自动 Gold)
python scripts/rdp_historical_daemon.py --once
```

**方式 B — 增量日批(推荐生产)**:

```bash
# 直接调用 (单次)
python scripts/rdp_run_daily_ingest.py

# 或纳入 workflow (含后续 artifact 索引重建)
python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance

# Dry run 预览
python scripts/rdp_run_daily_ingest.py --dry-run
```

**配置 cron**:

```bash
# Linux crontab — 每天 04:00 UTC 自动拉取昨日数据
0 4 * * * cd /path/to/aats && python scripts/rdp_run_scheduled_workflow.py \
    --workflow data_maintenance >> /var/log/rdp/data_maintenance.log 2>&1
```

```powershell
# Windows Task Scheduler
schtasks /create /tn "RDP_DataMaintenance" `
    /tr "python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance" `
    /sc daily /st 04:00
```

### 4.4 运行研究管线

```bash
# 完整 Phase 2 → 3 → 4 → 5 → Decision 一键串联
python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02

# 只跑某些阶段
python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 \
    --start-from phase3                  # 从 Phase 3 开始
python scripts/rdp_run_full_pipeline.py --start 2026-03-31 --end 2026-04-02 \
    --stop-after phase4                  # 只跑到 Phase 4
```

---

## 5. 配置一览

完整模板: `configs/templates/.env.research.example`

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `RDP_DATABASE_URL` | `postgresql+psycopg://localhost:5432/aats_research` | Research DB 连接串 |
| `RDP_LIVE_DATABASE_URL` | — | Production DB 只读(Phase 3+ 必需) |
| `RDP_HISTORICAL_INCOMING_DIR` | `./data/historical/incoming` | ZIP 输入目录 |
| `RDP_ROLLING_CANDLES_SYMBOLS` | `BTC-USDT,ETH-USDT,BTC-USDT-SWAP,ETH-USDT-SWAP` | 采集 symbol 列表 |
| `RDP_ROLLING_CANDLES_TIMEFRAMES` | `15m,1H` | 采集 timeframe(1m/5m 已默认禁用) |
| `RDP_ROLLING_FUNDING_SYMBOLS` | `BTC-USDT-SWAP,ETH-USDT-SWAP` | funding 采集 symbol |
| `RDP_GAP_AUTO_DETECT_WINDOW_HOURS` | `24` | gap 检测回看窗口 |
| `RDP_ENV` | `dev` | 环境标识 (dev/staging/prod) |

> 1m / 5m timeframe 在 schema 中保留(`*_1m`/`*_5m` 表仍存在),只是默认不再增量采集。如需启用,设置 `RDP_ROLLING_CANDLES_TIMEFRAMES=1m,5m,15m,1H`。详见 `config.py` 的注释块。

---

## 6. Workflow 调度

`configs/rdp_workflows/` 下有 4 个 workflow JSON:

| Workflow | 调度建议 | 任务数 | 说明 |
|---|---|---|---|
| `data_maintenance` | 每日 04:00 UTC | 2 | 日批采集 + artifact 索引重建 |
| `governance_cycle` | 每日 07:00 UTC | 4 | 质量监控、产物验证、轮次刷新 |
| `research_cycle` | 每周日 08:00 UTC | 3 | Step 2 研究、Phase 3 归因、Phase 4 执行 |
| `decision_cycle` | 每周(研究后) | 3 | Decision round、可靠性检查、观察检查 |

```bash
# 列出所有 workflow
python scripts/rdp_run_scheduled_workflow.py --list

# Dry run 预览
python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle --dry-run

# 执行
python scripts/rdp_run_scheduled_workflow.py --workflow governance_cycle
```

完整调度策略和依赖关系: [`docs/operations/rdp_scheduling_strategy.md`](../../docs/operations/rdp_scheduling_strategy.md)。

---

## 7. 与主交易系统的整合

| 整合点 | 文件 | 作用 |
|---|---|---|
| Live DB 只读访问 | `live_query_adapter.py` | RDP 读 production DB 7 张事实表 |
| 参数加载器 | `aats/bootstrap/active_parameters.py` | 启动时注入 family/tf 参数 |
| 只读 API 路由 | `aats/api/rdp_routes.py` | `/rdp/` 前缀 8 个 GET 端点 |
| 受控应用流程 | `scripts/approve_recommendation_and_apply.py` | Recommendation → Gate → Approval → Apply |

整合原则: **不侵入实时主链 · 旁路分析 + 受控回灌 · 研究与生产分库 · 建议与应用分离 · 第一版不做自动 apply**

---

## 8. 测试

```bash
# 单元测试 (本目录相关)
python -m pytest tests/unit/ -k "data_platform or rdp" -q

# 端到端集成 (需要 docker testcontainers)
python -m pytest tests/integration/data_platform/ -q
```

测试用例位置:
- `tests/unit/data_platform/` — 各模块单测
- `tests/integration/data_platform/` — testcontainers 端到端
- `tests/replay/` — Phase 2 replay engine 测试

---

## 9. 进一步阅读

| 文档 | 内容 |
|---|---|
| **架构概览** | |
| [主 README §21](../../README.md#21-研究数据平台-research-data-platform) | RDP 全景 + 七阶段管线 + 整合架构 |
| [`docs/rdp/module_reference.md`](../../docs/rdp/module_reference.md) | 全部代码模块职责清单 |
| **Phase 详解** | |
| [`docs/rdp/phase2_parameter_research_details.md`](../../docs/rdp/phase2_parameter_research_details.md) | Phase 2: Edge Contract、CLI、产物 |
| [`docs/rdp/phase3_4_attribution_execution_details.md`](../../docs/rdp/phase3_4_attribution_execution_details.md) | Phase 3-4: 归因瀑布、滑点模型 |
| **运营** | |
| [`docs/operations/rdp_scheduling_strategy.md`](../../docs/operations/rdp_scheduling_strategy.md) | Workflow 调度 + cron + 日批迁移背景 |
| [`docs/operations/platform_runbook.md`](../../docs/operations/platform_runbook.md) | 平台全景 + 日常操作 + 故障排查 |
| [`docs/operations/operator_checklist.md`](../../docs/operations/operator_checklist.md) | 日常巡检 + 运行前后检查 |
| [`docs/operations/rdp_reliability_runbook.md`](../../docs/operations/rdp_reliability_runbook.md) | 可靠性 Runbook + 异常 SOP |
| [`docs/operations/rdp_environment_matrix.md`](../../docs/operations/rdp_environment_matrix.md) | 环境隔离权限矩阵 |
| **参数治理** | |
| [`docs/operations/parameter_governance.md`](../../docs/operations/parameter_governance.md) | 参数生命周期 (draft → frozen) |
| [`docs/operations/parameter_apply_and_rollback.md`](../../docs/operations/parameter_apply_and_rollback.md) | 应用与回滚操作指南 |
| [`docs/operations/parameter_mapping_reference.md`](../../docs/operations/parameter_mapping_reference.md) | RDP↔主系统参数映射 |

---

## 10. 已知限制

| 项目 | 说明 |
|---|---|
| Symbol 白名单 | 4 个 instrument 硬编码,后续需改为数据库驱动 |
| Gold volume 语义 | spot vol = 基础币量,swap vol = 合约张数,未做跨类型统一 |
| Replay 评分 | Phase 2 使用简化模型(不含 AI assessment),与生产存在偏差 |
| Replay 撮合 | Phase 2 不含撮合仿真和 PnL accounting |
| Signal 校准 | `signal_edge_scale_bps` 当前为经验默认值(10.0),尚未历史数据校准 |
| Execution realism V1 | Phase 4 无 orderbook depth/trades,spread 和 impact 基于 OHLCV proxy |
| 仓位极小 | BTC-USDT-SWAP 1 合约 = 0.01 BTC,小仓位下 feasibility 区分度有限 |
