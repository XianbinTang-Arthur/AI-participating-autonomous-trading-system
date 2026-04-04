# AATS 系统架构与完整链路文档

> 本文档基于对整个代码仓库的逐文件阅读整理而成，覆盖系统从启动到运行的全部链路。

---

## 目录

- [1. 系统总览](#1-系统总览)
- [2. 仓库目录结构](#2-仓库目录结构)
- [3. 系统启动链路](#3-系统启动链路)
- [4. 链路一：行情数据采集与标准化](#4-链路一行情数据采集与标准化)
- [5. 链路二：特征计算](#5-链路二特征计算)
- [6. 链路三：决策引擎](#6-链路三决策引擎)
- [7. 链路四：AI 推理服务](#7-链路四ai-推理服务)
- [8. 链路五：治理引擎（风控/策略/模式）](#8-链路五治理引擎风控策略模式)
- [9. 链路六：策略引擎与 Sleeve 自动执行控制](#9-链路六策略引擎与-sleeve-自动执行控制)
- [10. 链路七：执行引擎与订单生命周期](#10-链路七执行引擎与订单生命周期)
- [11. 链路八：持仓、对账与恢复](#11-链路八持仓对账与恢复)
- [12. 链路九：账本与 Lot 追踪](#12-链路九账本与-lot-追踪)
- [13. 链路十：研究数据平台 (RDP)](#13-链路十研究数据平台-rdp)
- [14. API 与操作员控制面](#14-api-与操作员控制面)
- [15. 事件总线与事件溯源](#15-事件总线与事件溯源)
- [16. 存储层架构](#16-存储层架构)
- [17. 完整事件流全景图](#17-完整事件流全景图)
- [18. 关键设计不变量](#18-关键设计不变量)

---

## 1. 系统总览

AATS（AIParticipatingAutonomousTradingSystem）是一个面向加密资产的**事件驱动自动交易系统**，核心设计目标不是高频盈利，而是：

- 决策链与执行链清晰可审计
- 订单生命周期可恢复、可回放、可对账
- 自动执行具备受保护的门禁
- 运行时具备 operator 控制面
- 环境异常时 fail-closed

系统由两大子系统组成：

| 子系统 | 职责 | 数据库 |
|--------|------|--------|
| **主交易系统** | 行情 → 特征 → 决策 → 策略 → 执行 → 持仓 → 对账 | `aats` (PostgreSQL) |
| **研究数据平台 (RDP)** | 历史数据 → 回放 → 参数研究 → 归因 → 决策 → 参数推送 | `aats_research` (PostgreSQL) |

支持的运行模式（Profile）：

| Profile | 行情来源 | 执行方式 | 适用场景 |
|---------|----------|----------|----------|
| `spot` | OKX 真实行情 | 模拟盘 | 现货联调 |
| `spot_live` | OKX 真实行情 | 实盘（受保护） | 现货 live |
| `derivatives` | OKX 真实行情 | 模拟盘 | 合约联调 |
| `derivatives_live` | OKX 真实行情 | 实盘（受保护） | 合约 live |

---

## 2. 仓库目录结构

```
AIParticipatingAutonomousTradingSystem/
├── aats/                          # 核心 Python 包 (376 个 .py 文件)
│   ├── bootstrap/                 # 启动引导：配置、设置、运行时构建
│   │   ├── config.py              # ★ 核心：build_runtime() 构建全部服务
│   │   ├── settings.py            # AATSSettings Pydantic 模型 (300+ 字段)
│   │   ├── env_profiles.py        # Profile .env 文件加载
│   │   ├── managed_profiles.py    # 托管 Profile 默认值
│   │   ├── active_parameters.py   # RDP 活跃参数注入
│   │   ├── logging.py             # 结构化日志配置
│   │   └── metrics.py             # 运行时指标收集
│   │
│   ├── api/                       # REST API 层
│   │   ├── routes.py              # 主操作端点 (991 行, 50+ 端点)
│   │   ├── auth_routes.py         # 认证与用户管理 (645 行)
│   │   ├── rdp_routes.py          # RDP 治理端点 (584 行)
│   │   ├── auth.py                # 认证逻辑 (session/API key/角色)
│   │   ├── session_auth.py        # HMAC-SHA256 会话令牌
│   │   └── ui.py                  # 前端静态资源服务
│   │
│   ├── bus/                       # 事件总线
│   │   ├── base.py                # EventBus 抽象接口
│   │   ├── memory_bus.py          # 内存总线实现（当前使用）
│   │   └── kafka_bus.py           # Kafka 占位（未实现）
│   │
│   ├── events/                    # 事件基础设施
│   │   ├── envelopes.py           # EventEnvelope 构建/解析
│   │   └── topics.py              # 45 个事件主题常量
│   │
│   ├── schemas/                   # Pydantic 领域模型 (23 文件)
│   │   ├── common.py              # EventEnvelope, SchemaBase
│   │   ├── market.py              # MarketSnapshot
│   │   ├── features.py            # FeatureSnapshot, AnalysisContext
│   │   ├── decision.py            # DecisionContext, BaselineAssessment, PositionTarget
│   │   ├── execution.py           # OrderIntent, OrderState, FillEvent
│   │   ├── exit_execution.py      # ExitExecutionIntent, ChildExitOrderRef
│   │   ├── portfolio.py           # PortfolioSnapshot, Position
│   │   ├── reconciliation.py      # ReconciliationReport, ReconciliationFinding
│   │   ├── governance.py          # PolicyDecision, RiskDecision
│   │   ├── strategy_runtime.py    # StrategySleeveIntent, AllocationDecision
│   │   ├── strategy_profiles.py   # StrategyProfileRevision
│   │   ├── ai_brief.py            # AIDecisionBrief
│   │   ├── operator.py            # OperatorActionRecord, BlockerSnapshot
│   │   └── system.py              # HealthSnapshot, RecoveryStatus
│   │
│   ├── services/                  # ★ 核心服务层 (15 个子模块)
│   │   ├── market_gateway/        # 行情接入（6 文件）
│   │   ├── feature_engine/        # 特征计算（6 文件）
│   │   ├── decision_engine/       # 决策引擎（8 文件）
│   │   ├── ai_service/            # AI 推理（7 文件）
│   │   ├── governance_engine/     # 治理引擎（11 文件）
│   │   ├── strategy_engines/      # 策略引擎（27+ 文件）
│   │   │   ├── families/          # 策略族注册表
│   │   │   ├── independent/       # 独立对冲策略（16 文件）
│   │   │   └── smart_arbitrage/   # 智能套利策略（10 文件）
│   │   ├── execution_engine/      # 执行引擎（21 文件）
│   │   ├── execution_control/     # 执行控制（7 文件）
│   │   ├── portfolio_service/     # 持仓服务（8 文件）
│   │   ├── reconciliation_service/# 对账服务（4 文件）
│   │   ├── recovery_control/      # 恢复控制（2 文件）
│   │   ├── ledger/                # 账本（5 文件）
│   │   ├── blocker_control/       # 阻断控制（3 文件）
│   │   ├── operator/              # 操作员服务（23 文件）
│   │   └── projections/           # 投影层（1 文件）
│   │
│   ├── storage/                   # 持久化层 (51 文件)
│   │   ├── base.py                # 仓库接口 (Protocol)
│   │   ├── *_repo.py              # 接口定义
│   │   ├── *_repo_postgres.py     # PostgreSQL 实现
│   │   └── sqlalchemy_models.py   # ORM 模型 (84KB)
│   │
│   └── data_platform/             # 研究数据平台 (100+ 文件)
│       ├── collectors/            # 数据采集（backfill + rolling）
│       ├── merge/                 # Bronze/Silver 合并
│       ├── gold/                  # Gold 层构建
│       ├── validate/              # 数据质量检查
│       ├── replay/                # 回放引擎
│       ├── metrics/               # 研究指标
│       ├── attribution/           # 归因分析
│       ├── execution_realism/     # 执行可行性
│       ├── decision_system/       # 决策引擎
│       ├── governance/            # 参数治理
│       └── production_workflow/   # 发布门禁
│
├── apps/                          # 应用入口
│   ├── api_gateway/main.py        # ★ FastAPI 应用 (lifespan 管理)
│   └── decision_engine/main.py    # 本地循环入口
│
├── scripts/                       # 启动和运维脚本
│   ├── start_api.py               # ★ API 服务启动脚本
│   ├── run_local.py               # 本地 paper 循环
│   ├── rdp_start.py               # RDP 数据守护进程
│   ├── rdp_run_full_pipeline.py   # RDP 全流程编排
│   └── ...                        # 其他 RDP 脚本
│
├── configs/                       # 配置文件
│   ├── strategy_profiles/         # 策略 YAML 调参
│   └── active_parameter_sets/     # RDP 活跃参数
│
├── migrations/                    # 数据库迁移
│   ├── 0001~0007_*.sql            # 主系统迁移
│   └── research/                  # RDP 迁移
│
├── .env.spot                      # 现货模拟盘环境变量
├── .env.spot.live                 # 现货实盘环境变量
├── .env.derivatives               # 合约模拟盘环境变量
├── .env.derivatives.live          # 合约实盘环境变量
└── .env.research                  # RDP 环境变量
```

---

## 3. 系统启动链路

### 3.1 启动入口

系统有两种主要启动方式：

**API 服务模式**（生产）：
```
scripts/start_api.py --profile derivatives
```

**本地循环模式**（开发）：
```
scripts/run_local.py --profile spot --iterations 100
```

### 3.2 完整启动序列

```
start_api.py / run_local.py
    │
    ▼
(1) load_profiled_dotenv_into_process(profile)
    │  加载 .env.{profile} → os.environ
    ▼
(2) load_settings() → AATSSettings
    │  合并: 硬编码默认值 → YAML → managed_profile → 环境变量
    ▼
(3) configure_logging_for_settings()
    │  设置多级别轮转日志: runtime/debug/info/warning/error
    ▼
(4) build_runtime(settings) → ApplicationRuntime
    │
    ├── (4a) build_storage_backends()
    │   │  内存模式: InMemory* 仓库
    │   │  PostgreSQL: 20+ Postgres* 仓库 + 迁移 + 运行锁
    │   ▼
    ├── (4b) apply_active_parameters_to_settings()
    │   │  注入 RDP 研究参数 (fail-soft)
    │   ▼
    ├── (4c) 按顺序创建服务:
    │   │
    │   ├── MarketDataGateway (OKX REST + WebSocket)
    │   ├── OKXAccountService (账户快照)
    │   ├── EffectiveFeeResolver (费率解析)
    │   ├── KillSwitch + RuntimeModeController
    │   ├── SystemHealthService + PolicyEngine + RiskEngine
    │   ├── ExecutionAdapter (Paper 或 OKX)
    │   ├── ExecutionPlanner + OrderManager
    │   ├── PortfolioService + PortfolioSnapshotBuilder
    │   ├── ReconciliationService
    │   ├── FeatureEngine + FeatureCalculator
    │   ├── AIInferenceService (如果启用)
    │   ├── DecisionOrchestrator + StrategyCoordinator
    │   ├── DecisionCycleTrigger + DecisionTriggerPolicy
    │   ├── Phase1LedgerMirrorService + LotProjection
    │   └── ExecutionLedgerRecoveryService
    │   ▼
    ├── (4d) 注册事件总线订阅
    │   │  market.snapshots → DecisionCycleTrigger
    │   │  features.snapshots → DecisionOrchestrator
    │   │  execution.fill_events → PortfolioService
    │   │  等等...
    │   ▼
    └── (4e) 启动后台任务
        │  WebSocket 连接、账户同步、健康检查
        ▼
    ApplicationRuntime 就绪，开始处理请求
```

### 3.3 配置层级

优先级从低到高：

```
① AATSSettings 字段默认值
② YAML 配置 (configs/strategy_profiles/*.yaml)
   或 managed_profile runtime_defaults
③ RDP 活跃参数 (configs/active_parameter_sets/*)
④ 环境变量 (AATS_*)
⑤ CLI 参数 (--profile, --host, --port)
```

---

## 4. 链路一：行情数据采集与标准化

### 4.1 数据流概览

```
OKX 交易所
    │
    ├─→ WebSocket Public (tickers: 实时报价)
    │   └─→ OKXPublicWebSocketClient
    │
    └─→ WebSocket Business (candle15m, candle1H: K 线)
        └─→ OKXPublicWebSocketClient
                │
                ▼
        MarketDataGateway._handle_okx_message()
                │
                ▼
        OKXMarketSnapshotNormalizer.apply_message()
            ├─→ _parse_ticker() → OKXTickerState
            └─→ _parse_candle() → OKXCandleState
                │
                ▼  (ticker + candle_15m + candle_1h 三者齐备)
        _build_snapshot() → MarketSnapshot
                │
                ▼
        MarketSnapshotPublisher.publish()
                │
                ▼
        EventBus: topic="market.snapshots", key=symbol
```

### 4.2 关键模块

| 文件 | 职责 |
|------|------|
| `services/market_gateway/okx_websocket.py` | 双连接管理（public + business），自动重连 |
| `services/market_gateway/okx_normalizer.py` | OKX 报文 → 内部状态聚合，三组件齐备才出快照 |
| `services/market_gateway/normalizer.py` | 通用标准化（支持非 OKX 数据源） |
| `services/market_gateway/publisher.py` | 发布到事件总线 |
| `services/market_gateway/gateway.py` | 总协调：WebSocket 流 + REST 回退 + 本地 Demo 数据 |

### 4.3 MarketSnapshot 核心字段

```python
symbol, exchange, snapshot_ts,
best_bid, best_ask, bid_size, ask_size,
last_price, volume_24h,
kline_15m: {open, high, low, close, volume},
kline_1h:  {open, high, low, close, volume},
recent_trades: [{price, qty, side}],
orderbook_depth: {bids: [[price, size]], asks: [[price, size]]}
```

### 4.4 REST 回退机制

当 WebSocket 数据超过 `market_data_stale_after_seconds` 未更新时，Gateway 自动切换到 REST 轮询模式，从 OKX REST API 获取最新 ticker 和 K 线数据。

---

## 5. 链路二：特征计算

### 5.1 数据流

```
EventBus: "market.snapshots"
    │
    ▼
FeatureEngine.handle_market_snapshot()
    │
    ▼
FeatureCalculator.calculate(market_snapshot)
    │
    ├─→ TrendCalculator (15m + 1h)
    │     momentum = (close - open) / open
    │     trend_strength = clamp(momentum × 120 + body_ratio × 0.25)
    │
    ├─→ VolatilityAnalyzer (15m + 1h)
    │     volatility = range_ratio × 0.7 + close_to_open × 0.3
    │     状态: <0.003 low, <0.01 medium, ≥0.01 high
    │
    ├─→ LiquidityAnalyzer
    │     spread_score, depth_score, balance_score
    │     liquidity = spread×0.5 + depth×0.3 + balance×0.2
    │
    ├─→ RegimeClassifier
    │     决策树: breakout / trend / range / uncertain
    │
    ├─→ MultiTimeframeContext
    │     方向对齐、动量对齐、regime 对齐
    │
    ├─→ AlphaFactors (复合因子)
    │     composite = momentum×0.34 + trend×0.22 + regime×0.17
    │               + mtf×0.12 + micro×0.15  (× liquidity_scale)
    │
    └─→ PositionSizingContext
          volatility_target_scale, suggested_position_scale
    │
    ▼
FeatureSnapshot → EventBus: "features.snapshots"
```

### 5.2 关键输出

| 字段 | 范围 | 含义 |
|------|------|------|
| `composite_alpha_score` | [-1.0, 1.0] | 综合交易信号强度 |
| `regime_indicator` | breakout/trend/range/uncertain | 市场状态分类 |
| `regime_confidence` | [0.0, 1.0] | 分类置信度 |
| `suggested_position_scale` | [0.0, 1.0] | 建议仓位比例 |
| `trend_strength` | [-1.0, 1.0] | 趋势方向强度 |
| `volatility_state` | low/medium/high | 波动率级别 |
| `liquidity_score` | [0.0, 1.0] | 流动性评分 |

---

## 6. 链路三：决策引擎

### 6.1 触发机制

```
EventBus: "features.snapshots"
    │
    ▼
DecisionCycleTrigger.handle_feature_snapshot()
    │
    ▼
DecisionTriggerPolicy.should_trigger()
    ├── 防重复（相同快照不重复触发）
    ├── 每分钟最大决策次数
    ├── 最小触发间隔（15m: 60s, 1h: 0s）
    ├── 检测 regime 或 momentum 实质变化
    └── 检测最小价格变动 (bps)
    │
    ▼  (通过触发策略)
DecisionOrchestrator.run_cycle(symbol, timeframe)
```

### 6.2 决策周期完整流程

```
run_cycle(symbol, timeframe)
│
├── ① 构建 HealthSnapshot (SystemHealthService)
│     发布 → "system.health_snapshots"
│
├── ② 构建 DecisionContext (DecisionContextBuilder)
│     ├── 最新 MarketSnapshot
│     ├── 最新 FeatureSnapshot
│     ├── 最新 PortfolioSnapshot
│     ├── 当前持仓状态（方向、数量、生命周期）
│     └── 策略执行健康指标
│     发布 → "strategy.decision_context"
│
├── ③ 基线评估 (BaselineStrategy.evaluate)
│     ├── 根据 regime + alpha 确定方向偏好
│     │   breakout: |alpha| ≥ 0.10
│     │   trend:    |alpha| ≥ 0.16
│     │   range:    |alpha| ≥ 0.24
│     │   uncertain:|alpha| ≥ 0.30
│     ├── 计算置信度 (0.4-0.96)
│     └── 输出 BaselineAssessment
│     发布 → "strategy.baseline_assessment"
│
├── ④ AI 评估 (AIInferenceService.assess) [如果启用]
│     ├── 构建 AIDecisionBrief (PromptBuilder)
│     ├── 调用 OpenAI → 结构化 JSON 响应
│     ├── 验证输出 (AssessmentValidator)
│     └── AIMarketAssessment (directional_edge, confidence)
│     发布 → "strategy.ai_assessment" / "strategy.ai_decision_brief"
│
├── ⑤ 构建目标仓位 (TargetPositionEngine.build)
│     ├── 信号边际 = alpha × scale
│     ├── 交易成本 = fee + slippage + funding
│     ├── 净边际 = 信号边际 - 成本 - buffer
│     ├── 应用护栏（现货禁止做空等）
│     ├── 风险限制
│     └── PositionTarget (目标仓位量、delta、杠杆)
│     发布 → "strategy.position_target"
│
├── ⑥ 治理评估
│     ├── PolicyEngine.evaluate() → PolicyDecision
│     │   发布 → "policy.decisions"
│     └── RiskEngine.evaluate() → RiskDecision
│         发布 → "risk.decisions"
│
├── ⑦ 策略协调 (StrategyCoordinator)
│     └── [见链路六]
│
└── ⑧ 审计记录
      发布 → "system.audit_records"
```

### 6.3 影子决策 (Shadow Decision)

当系统运行在 `baseline_only` 模式时，AI 仍会生成"如果我决策会怎样"的影子评估，用于离线对比和 AI 性能追踪。

---

## 7. 链路四：AI 推理服务

### 7.1 推理流程

```
AIInferenceService.assess(context, baseline)
    │
    ├── 检查是否应该尝试 (should_attempt_assessment)
    │   ├── 降级状态检查
    │   └── 恢复探测逻辑
    │
    ├── 构建提示 (PromptBuilder.build)
    │   ├── 市场状态 (价格、价差、流动性)
    │   ├── 技术信号 (regime、alpha、momentum)
    │   ├── 持仓状态 (当前仓位、敞口方向)
    │   ├── 基线评估 (方向、置信度)
    │   ├── 成本因子 (费率、资金费、滑点)
    │   └── 系统状态 (safe_to_trade, halted)
    │
    ├── 调用 OpenAI (OpenAIProvider.generate_assessment)
    │   ├── 严格 JSON Schema 响应
    │   └── 超时/错误处理
    │
    ├── 验证 (AssessmentValidator)
    │   ├── Schema 验证
    │   ├── 时间框架匹配
    │   ├── 置信度/不确定性一致性
    │   ├── 方向边际范围 [-1, 1]
    │   └── 经济可行性: 净边际 ≥ min_net_edge_bps
    │
    └── 降级管理
        ├── 连续失败 ≥ 5 → 自动降级到 baseline_only
        ├── 恢复探测: 每 5 分钟尝试一次
        └── 连续成功 → 自动恢复
```

### 7.2 AI 输出

```python
AIMarketAssessment:
    directional_edge: float [-1, 1]     # 方向性判断
    confidence: float [0, 1]             # AI 置信度
    uncertainty: float [0, 1]            # 不确定性
    economically_actionable: bool        # 经济上是否可行
    estimated_edge_bps: float            # 估算边际 (bps)
    estimated_cost_bps: float            # 估算成本 (bps)
    risk_tags: list[str]                 # 风险标签
    rationale_summary: str               # 推理摘要
```

---

## 8. 链路五：治理引擎（风控/策略/模式）

### 8.1 多层治理架构

```
┌─────────────────────────────────────────────────┐
│ RuntimeModeController (运行模式)                 │
│   backtest / paper_live / guarded_live          │
│   → 决定执行路由和权限边界                       │
├─────────────────────────────────────────────────┤
│ KillSwitch (紧急停机)                           │
│   halt(reason) / resume()                       │
│   → 立即阻断所有新增风险操作                     │
├─────────────────────────────────────────────────┤
│ PolicyEngine (策略门禁)                          │
│   symbol 白名单、做空许可、杠杆许可              │
│   dry_run 模式、人工审批要求                     │
├─────────────────────────────────────────────────┤
│ RiskEngine (风险评估)                            │
│   仓位上限、名义价值上限、杠杆限制               │
│   保证金检查、强平缓冲、挂单限制                 │
│   自适应风险预算收缩                             │
├─────────────────────────────────────────────────┤
│ AdaptiveControls (自适应控制)                    │
│   risk_budget_multiplier: 0.1-1.0               │
│   execution_aggressiveness_multiplier: 0.1-1.0  │
│   触发条件: 高保证金使用率、窄强平间距、           │
│   trial guard 告警、数据过期                     │
├─────────────────────────────────────────────────┤
│ DerivativesLiveGuard (合约专属保护)              │
│   healthy → grace → only_reduce → critical      │
│   监控保证金使用率和风险快照可用性               │
├─────────────────────────────────────────────────┤
│ ForwardTrialGuard (表现监控)                     │
│   日亏损限额、连续亏损次数、费率拖累比           │
│   高滑点频率、慢成交频率                         │
│   healthy → warning → breached                  │
├─────────────────────────────────────────────────┤
│ RecoveryPostureEvaluator (恢复态评估)            │
│   healthy / review_required / only_reduce        │
│   resume_blocked / rebaseline_pending            │
│   → 决定系统是否允许继续交易                     │
├─────────────────────────────────────────────────┤
│ RuntimeLayering (运行时配置组合)                  │
│   RuntimeProfile → EnvironmentCapabilities       │
│   → PolicyProfile → RecoveryPolicy               │
└─────────────────────────────────────────────────┘
```

### 8.2 风险预算自适应收缩表

| 触发条件 | risk_budget 乘数 |
|----------|------------------|
| 正常 | 1.0 |
| 风险快照缺失 (grace) | 0.70 |
| 保证金使用率 75-90% | 0.45-0.65 |
| 强平间距过窄 | 0.50-0.70 |
| only_reduce 状态 | 0.45 |
| trial guard breached | 0.50 |
| 运行安全降级 | 0.50 |

---

## 9. 链路六：策略引擎与 Sleeve 自动执行控制

### 9.1 策略族注册表

系统支持 7 种策略族，按优先级排序：

| 优先级 | 策略族 | 描述 | 执行模型 |
|--------|--------|------|----------|
| 1 | smart_arbitrage | 现货/合约基差套利 | 配对执行 |
| 2 | spot_grid | 区间网格交易 | 库存再平衡 |
| 3 | dca | 定投积累 | 定时/回调买入 |
| 4 | directional | 方向性持仓 | 基线跟随 |
| 5 | protective | 保护性对冲 | 风险缩减 |
| 6 | opportunistic | 机会性交易 | 战术操作 |
| 7 | independent | 独立对冲 | 多空账本 |

### 9.2 策略协调完整流程

```
DecisionOrchestrator → StrategyCoordinator
    │
    ├── ① 构建 StrategyEngineInput
    │     最新快照 + 持仓 + 市场历史
    │
    ├── ② 构建 StrategyEvaluationContext
    │     每族独立的运行时控制和数据视图
    │
    ├── ③ 评估所有策略族 (StrategyFamilyRegistry)
    │     每族返回 list[StrategyCandidate]
    │     候选项包含: state, score, confidence,
    │     target_position_qty, delta, route_action, legs
    │
    ├── ④ 候选项选择
    │     取每族的主候选项
    │
    ├── ⑤ 组合分配 (Allocator)
    │     选择要执行的候选项子集
    │     考虑冲突、风险限制、权重
    │
    ├── ⑥ 生成 Sleeve Intent
    │     每个选中的候选项 → StrategySleeveIntent
    │
    └── ⑦ Sleeve 自动执行控制 (三层)
          └── [见 9.3]
```

### 9.3 Sleeve 三层自动执行控制 (核心)

```
原始 Candidate + Intent
         │
         ▼
┌─────────────────────────────────────────┐
│ 第一层: SleeveExecutionPermissionPolicy │
│                                         │
│ 输入: 候选项状态、运行时支持、保护意图    │
│ 输出: ExecutionPermissionDecision        │
│                                         │
│ 决策分支:                                │
│   不支持运行时     → unsupported         │
│   保护性但被禁用   → protective_override │
│   候选项被禁用     → hold / advisory     │
│   auto_execution 关 → hold / advisory    │
│   通过             → approved            │
└─────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│ 第二层: SleeveBudgetController          │
│                                         │
│ 三个乘数（非保护性才生效）:              │
│   ① 波动率乘数: volatility_target_scale │
│   ② 对账乘数: 异常时缩至 0.5 或 0      │
│   ③ PnL 乘数: 软亏损→渐缩, 硬亏损→归零 │
│                                         │
│ effective_scale = min(①, ②, ③)          │
│ scaled_delta = delta × effective_scale   │
└─────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│ 第三层: SleeveRoutingComposer           │
│                                         │
│ 合并权限 + 预算决策:                     │
│                                         │
│ 保护性例外 → protective_execute         │
│ 权限拒绝   → hold_current / advisory    │
│ 预算归零   → suppressed_after_approval  │
│ 正常批准   → execute_target             │
│                                         │
│ 输出: 最终 route_action, delta, legs    │
└─────────────────────────────────────────┘
         │
         ▼
    最终 StrategySleeveIntent (可执行)
```

### 9.4 独立对冲策略 (Independent) 详解

```
IndependentFamilyEngine.evaluate()
    │
    ├── 分别评估 long book 和 short book
    │   ├── 打分: confidence score (因子加权)
    │   ├── 判断动作: open / scale_in / de_risk / close_failed_thesis / hold
    │   ├── 检查门禁: edge 强度、成本阈值
    │   └── 计算 sizing: 基础 + scale_in 乘数
    │
    ├── 综合: 净目标仓位 = long + short
    │
    └── 输出: 候选项 + 两条 StrategyLegIntent (long/short)
```

### 9.5 DCA 策略详解

```
DcaStrategyEngine.evaluate()
    │
    ├── 检查: dca_enabled, 现货运行时, 有价格
    ├── 验证仓位上限未达: sleeve_qty < max_position_qty
    ├── 验证间隔已过: now - last_dca > interval_seconds
    ├── 可选回调守卫: price < anchor × (1 - pullback_bps)
    ├── 计算档位: tranche_qty = budget / price
    └── 返回 "ready" 候选项 with override_target
```

---

## 10. 链路七：执行引擎与订单生命周期

### 10.1 执行流程概览

```
StrategySleeveIntent (可执行)
         │
         ▼
ExecutionPlanner.build_plan()
    ├── 验证 delta 超过最小阈值
    ├── 解析执行风格 (market/limit, IOC/FOK)
    ├── 数量规整 (合约换算、lot_size 对齐)
    └── 生成 ExecutionPlan
         │
         ▼
ExecutionPlanner.build_intent()
    ├── 设置幂等键
    ├── 保留策略元数据 (bundle_id, sleeve_id)
    └── 生成 OrderIntent
         │
         ▼
OrderManager.handle_order_intent()
    │
    ├── 守卫检查:
    │   ├── Kill switch 状态
    │   ├── 重复 intent 检查
    │   ├── 瞬态重试冷却
    │   ├── Unknown write 阻断 (风险增加操作)
    │   └── 余额预留 (ExecutionObligationService)
    │
    ├── 提交到适配器:
    │   │
    │   ├── PaperAdapter (模拟)
    │   │   ├── 按当前价格立即成交
    │   │   ├── 滑点检查
    │   │   └── IOC/Limit 交叉验证
    │   │
    │   └── OKXAdapter (交易所)
    │       ├── 账户刷新
    │       ├── 负载构建 (OKXOrderPayloadBuilder)
    │       │   ├── 内部数量 → 合约数量换算
    │       │   ├── lot_size 规整
    │       │   └── client_order_id 生成
    │       ├── 提交守卫 (模式/语义/最大量/滑点)
    │       ├── HTTP 下单
    │       ├── 立即查询订单状态
    │       └── 回填成交记录
    │
    └── 持久化 + 事件发布
        ├── OrderState 持久化
        ├── FillEvent 持久化
        ├── 余额预留消费/释放
        └── 发布 "execution.order_updates" / "execution.fill_events"
```

### 10.2 订单状态机

```
CREATED → SUBMITTING → SUBMITTED → PARTIALLY_FILLED ↘
                         ↓                            FILLED
                     FILLED ↙
CANCEL_PENDING → CANCELED

终态: FILLED, CANCELED, REJECTED, FAILED, BLOCKED, DRY_RUN, EXPIRED
```

状态转换规则：
- 不允许倒退（FILLED → SUBMITTED 被拒绝）
- 成交量取已知最大值
- filled_qty ≥ requested_qty - epsilon → 自动转为 FILLED

### 10.3 Unknown Write 处理

当写请求（submit/cancel）因网络超时或服务器错误失败时：

```
网络异常
    │
    ▼
标记为 submission_unknown_check_exchange 或
       cancel_unknown_check_exchange
    │
    ├── 阻断新的风险增加操作
    ├── 允许风险缩减操作（reduce_only, close_only）
    │
    ├── 自动清除条件:
    │   ├── submit unknown: 交易所 order_id 出现
    │   └── cancel unknown: 订单到达终态
    │
    └── 超过阈值 (30s submit / 300s cancel):
        └── 提升为 operator review
```

### 10.4 退出执行聚合

```
ExitExecutionIntent (父退出意图)
    │
    ├── ChildExitOrderRef #1 (子订单)
    │   状态: PENDING_DISPATCH / WORKING / TERMINAL
    │
    ├── ChildExitOrderRef #2
    │
    └── ChildExitOrderRef #N
    │
    聚合: filled + canceled + rejected = total
    当全部子订单终结 → 父意图 COMPLETED
```

### 10.5 Paper vs OKX 适配器对比

| 维度 | Paper | OKX |
|------|-------|-----|
| 提交 | 同步立即成交 | HTTP 异步，网络依赖 |
| 成交发现 | 提交时合成 | 查询订单+成交端点 |
| 滑点检查 | 本地计算 | 门禁预检+提交时验证 |
| Unknown write | 永不发生 | 可能（断连/服务器错误） |
| 数量转换 | 直接传递 | 合约感知（internal ↔ exchange） |

---

## 11. 链路八：持仓、对账与恢复

### 11.1 持仓服务

```
EventBus: "execution.fill_events"
    │
    ▼
PortfolioService.handle_fill_event()
    │
    ├── 应用成交到本地仓位状态 (PortfolioState.apply_fill)
    │   ├── 更新持仓数量、均价
    │   ├── 计算已实现 PnL
    │   └── 累计费用
    │
    ├── 持久化 lot book (PersistentLotBookService)
    │   ├── FIFO 匹配
    │   └── lot 开/关事件
    │
    ├── 持久化 sleeve PnL (SleevePnLProjectionService)
    │
    ├── 构建快照 (PortfolioSnapshotBuilder)
    │   ├── 总权益 = 余额 + 未实现 PnL
    │   ├── 总/净敞口
    │   └── 保证金使用率
    │
    └── 发布
        ├── → "portfolio.snapshots"
        └── → "portfolio.balance_deltas"
```

### 11.2 对账服务

```
对账触发 (定时 / 手动 / 快照事件)
    │
    ▼
ReconciliationService._build_report()
    │
    ├── ExchangeStateFetcher.fetch_snapshot()
    │   └── 获取交易所真实状态
    │
    ├── StateComparator.compare()
    │   ├── 订单差异 (本地 vs 交易所)
    │   ├── 成交差异
    │   ├── 余额差异
    │   ├── 持仓差异
    │   └── 严重性分级: CLEAN / SOFT / HARD / REVIEW
    │
    ├── ReconciliationRepairService.repair() [如果可自动修复]
    │   └── 从成交历史重建快照
    │
    └── ReconciliationClassification [由 RecoveryReconciliationClassifier 注入]
        ├── halt_required: 硬不匹配
        ├── projection_rebuild_required: 本地可修复
        ├── manual_review_required: 需人工审查
        ├── derivatives_only_reduce: 合约只允许减仓
        ├── observational_drift: 软偏差，持续观察
        └── clean: 无问题
```

### 11.3 启动恢复

```
系统启动
    │
    ▼
ExecutionLedgerRecoveryService.recover()
    │
    ├── 收集:
    │   ├── 范围内订单状态
    │   ├── 最近成交
    │   ├── 挂单
    │   ├── 策略 bundle
    │   └── 对账报告
    │
    ├── 检查:
    │   ├── 冷启动 (无成交) → 安全开始
    │   ├── 快照偏差 (重建 ≠ 存储) → 阻断，需 operator
    │   ├── 有挂单 → 评估可恢复性
    │   ├── Bundle 恢复 → partial_fill / review_required
    │   ├── 待处理执行命令 → 过期命令清理
    │   └── 对账阻断
    │
    └── 输出 RecoveryStatus:
        ├── safe_startup: bool
        ├── safe_to_trade: bool
        └── resume_eligible: bool
```

### 11.4 Blocker 控制

操作员可以执行的阻断消解动作：

| 动作 | 效果 |
|------|------|
| `reconcile-now` | 触发即时对账 |
| `accept-rebaseline` | 从交易所重建基线 |
| `resume-system` | 恢复交易 |
| `halt-system` | 紧急停机 |
| `refresh-exchange-state` | 刷新交易所状态 |
| `ai-review-restore` | 恢复 AI 推理 |
| `ai-review-degrade-to-baseline` | AI 降级到基线 |

---

## 12. 链路九：账本与 Lot 追踪

### 12.1 复式账本

```
成交事件
    │
    ▼
Phase1LedgerMirrorService.sync_obligation()
    ├── 预留: available → reserved (下单时)
    ├── 消费: reserved → external (成交时)
    └── 释放: reserved → available (取消时)

LedgerSettlementPostingService.post_fill_effects()
    ├── 现货买入: quote 支出 + asset 收入
    ├── 现货卖出: asset 支出 + quote 收入
    ├── 已实现 PnL 分录
    └── 费用分录
```

### 12.2 Lot FIFO 追踪

```
LotBasedProjectionBuilder.rebuild_lot_book(fills)
    │
    ├── 按时间顺序处理每笔成交
    │
    ├── 开仓成交 → 创建新 PositionLot
    │   lot_id, entry_price, quantity, opened_at
    │
    ├── 平仓成交 → FIFO 匹配最早的 lot
    │   ├── lot 全部平仓: 计算 realized_pnl_delta
    │   └── lot 部分平仓: 拆分 lot
    │
    └── 输出 LotBookSnapshot:
        ├── open_lots: list[PositionLot]
        ├── closed_lots: list[ClosedLot]
        ├── realized_pnl: Decimal
        └── total_fees: Decimal
```

### 12.3 资金费同步

```
LedgerFundingFeeSyncService.sync_recent_bills()
    │
    ├── 从 OKX 账单中识别资金费
    ├── 过账为收入/支出分录
    └── 按 symbol 库存比例分配到 sleeve
```

---

## 13. 链路十：研究数据平台 (RDP)

### 13.1 RDP 总览

RDP 是独立于主交易系统的研究子系统，负责：

- 历史数据仓库管理
- 策略参数回测与优化
- 实盘归因分析
- 执行可行性评估
- 参数升级决策
- 参数发布治理

### 13.2 数据分层架构

```
数据源
├── Backfill: OKX 历史 ZIP → file_parser → Staging
└── Rolling: OKX API → candles/funding_api_collector → Staging
        │
        ▼
┌─── 质量检查 (candle/funding quality checker) ────┐
│    fail → 阻断合并    warn → 告警继续    pass     │
└──────────────────────────────────────────────────┘
        │
        ▼
    Bronze (UPSERT on symbol,ts)  ← Staging
        │
        ▼
    Silver (去重、验证后的规范层)  ← Bronze
        │
        ▼
    Gold (回放就绪，含资金费对齐)  ← Silver
```

### 13.3 六阶段研究流程

```
Phase 2: 参数研究
    │  Gold bars → replay_runner → 27 参数组合扫描
    │  输出: parameter_candidates.json
    ▼
Phase 3: 归因分析
    │  回放 vs 实盘对比
    │  瀑布分类: 策略→权限→分配→预算→风控→执行→订单→成交
    │  输出: top_failure_modes, alignment_status
    ▼
Phase 4: 执行可行性
    │  滑点估算、成交可行性、成本调整边际
    │  输出: execution_cost_summary.json
    ▼
Phase 5: 治理刷新
    │  冻结候选参数集
    │  输出: parameter_registry.json
    ▼
Phase 6: 决策引擎
    │  综合 Phase 2-5 证据
    │  决策: keep_active / lower_priority / pause / require_review
    │  输出: recommendation_registry.json (带审批流)
    ▼
发布到主系统
    参数写入 configs/active_parameter_sets/
    主系统 active_parameters.py 加载（fail-soft）
```

### 13.4 回放引擎

```
replay_runner.run_replay(family, symbol, timeframe, params)
    │
    ├── 加载 Gold bars (时间排序)
    │
    ├── 对每根 bar 调用 adapter.evaluate_bar()
    │   ├── independent_adapter: 因子打分 → 稳定性确认 → 边际计算
    │   └── directional_adapter: SMA 交叉 → 趋势强度 → 边际计算
    │
    └── 输出: ReplayDecision 列表
        每个决策包含:
        ├── dominant_leg (long/short)
        ├── signal_edge_proxy_bps
        ├── funding_adjustment_bps
        ├── cost_bps
        ├── expected_net_edge_bps = signal - funding - cost
        └── blocking_reasons
```

### 13.5 参数治理

参数集生命周期：`draft → candidate → frozen → deprecated`

发布门禁检查：
- 治理健康检查
- 证据时效性（>7天阻断, >3天告警）
- 证据完整性
- 产物完整性

---

## 14. API 与操作员控制面

### 14.1 API 端点分类

| 类别 | 端点示例 | 数量 |
|------|----------|------|
| 系统健康与控制 | `/system/health`, `/halt`, `/resume` | ~15 |
| AI 管理 | `/ai/runtime`, `/ai/latest`, `/ai/shadow/*` | ~10 |
| 决策与策略 | `/decision/latest`, `/strategy/runtime` | ~8 |
| 订单与成交 | `/orders/open`, `/fills/recent`, `/orders/{id}/cancel` | ~12 |
| 持仓与账户 | `/portfolio`, `/balances`, `/account/state` | ~10 |
| 报告 | `/reports/execution-quality`, `/reports/profitability-overview` | ~12 |
| 对账与审计 | `/reconciliation/latest`, `/audit/latest` | ~8 |
| 阻断控制 | `/system/blockers`, `/system/blocker-actions/{id}` | ~5 |
| RDP 治理 | `/rdp/parameters/active`, `/rdp/recommendations/*` | ~15 |
| 认证 | `/auth/login`, `/auth/session`, `/auth/users` | ~8 |

### 14.2 认证模型

```
认证链:
  session_principal() → 验证 cookie 中的 HMAC-SHA256 令牌
       ↓
  require_read_access → session + read API key + anonymous(无用户时)
       ↓
  require_write_access → operator / admin 角色
       ↓
  require_admin_access → admin 角色

角色:
  viewer   → 只读
  operator → 读写 (交易操作)
  admin    → 全部 (用户管理、策略控制)
```

### 14.3 Dashboard Bundle

`/dashboard/bundle` 端点一次性聚合返回：
- 系统健康状态
- 最新决策
- 最新 AI 评估
- 当前持仓
- 阻断面板
- 运行时指标

---

## 15. 事件总线与事件溯源

### 15.1 事件总线架构

```python
EventBus (抽象接口):
    publish(topic, key, payload) → None
    subscribe(topic, handler) → None

InMemoryEventBus (当前实现):
    订阅关系: dict[topic, list[handler]]
    持久化: EventStore.append() (如果配置)
    错误处理: 第一个错误向上传播
```

### 15.2 事件主题清单 (45 个)

| 分类 | 主题 |
|------|------|
| **市场** | market.snapshots, features.snapshots |
| **健康** | system.health_snapshots, system.blocker_snapshots, system.operator_actions |
| **策略** | strategy.decision_context, strategy.baseline_assessment, strategy.ai_assessment, strategy.ai_decision_brief, strategy.ai_shadow_decision, strategy.coordinator_snapshots, strategy.sleeve_intents, strategy.portfolio_allocation_decisions, strategy.execution_bundles, strategy.position_target, strategy.decision_outcome |
| **治理** | policy.decisions, risk.decisions |
| **执行** | execution.plans, execution.order_intents, execution.order_updates, execution.fill_events, execution.error_summaries |
| **持仓** | portfolio.balance_deltas, portfolio.snapshots, account.baselines |
| **对账** | reconciliation.reports, reconciliation.validations |
| **审计** | system.audit_records |
| **策略配置** | strategy.profile_recommendations, strategy.profile_activations, ... (9 个) |

### 15.3 核心事件流

```
market.snapshots → features.snapshots → 触发决策周期
    → strategy.decision_context → strategy.baseline_assessment
    → strategy.ai_assessment → strategy.position_target
    → policy.decisions → risk.decisions
    → strategy.sleeve_intents → execution.order_intents
    → execution.order_updates → execution.fill_events
    → portfolio.snapshots → reconciliation.reports
```

---

## 16. 存储层架构

### 16.1 仓库模式

所有仓库定义为 **Protocol 接口**，支持依赖注入：

```python
# 接口 (base.py)
class ExecutionRepository(Protocol):
    async def save_order_state(state) → None
    async def save_fill(fill) → None
    async def open_order_states() → list[OrderState]
    ...

# 内存实现 (用于测试)
class InMemoryExecutionRepository:
    ...

# PostgreSQL 实现 (用于生产)
class PostgresExecutionRepository:
    ...
```

### 16.2 仓库清单 (20+ 仓库)

| 仓库 | 职责 |
|------|------|
| EventStore | 事件溯源（热 + 归档） |
| ExecutionRepository | 订单状态、成交记录 |
| ExecutionObligationRepository | 余额预留 |
| ExitExecutionRepository | 父-子退出订单关系 |
| PortfolioRepository | 持仓快照 |
| FillOutcomeRepository | 成交分析 |
| FundingFeeRepository | 资金费记录 |
| SleevePnLRepository | 策略 sleeve PnL |
| ReconciliationRepository | 对账报告、发现、基线 |
| AuditRepository | 决策审计链 |
| OperatorUserRepository | 操作员账户 |
| StrategyProfileRepository | 策略配置版本 |
| StrategySleeveRepository | 策略 sleeve 管理 |
| StrategyRuntimeRepository | 运行时分配与 bundle |
| LedgerRepository | 复式账本 |
| LotRepository | Lot 追踪 |
| CommandOutboxRepository | 命令发件箱 |
| ExecutionCommandRepository | 执行命令队列 |

### 16.3 数据库迁移

主系统 (`aats` 数据库): 7 个迁移文件
```
0001_postgres_storage.sql          # 基础存储
0002_execution_and_audit.sql       # 执行与审计
0003_audit_execution_plan_refs.sql # 审计关联
0004_operator_users.sql            # 操作员表
0005_storage_scope_columns.sql     # 多 scope 支持
0006_order_obligations.sql         # 余额预留
0007_execution_outbox.sql          # 执行发件箱
```

RDP (`aats_research` 数据库): 独立迁移在 `migrations/research/`

---

## 17. 完整事件流全景图

```
╔══════════════════════════════════════════════════════════════════════╗
║                        AATS 完整事件流                              ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  OKX Exchange                                                        ║
║      │                                                               ║
║      ▼                                                               ║
║  [MarketDataGateway]                                                 ║
║      │ WebSocket + REST fallback                                     ║
║      │ OKX 报文 → OKXMarketSnapshotNormalizer                       ║
║      ▼                                                               ║
║  market.snapshots ─────────────────────────────────────────┐         ║
║      │                                                     │         ║
║      ▼                                                     │         ║
║  [FeatureEngine]                                           │         ║
║      │ Trend + Volatility + Liquidity + Regime             │         ║
║      │ → Alpha Factors → Position Sizing                   │         ║
║      ▼                                                     │         ║
║  features.snapshots                                        │         ║
║      │                                                     │         ║
║      ▼                                                     │         ║
║  [DecisionCycleTrigger]                                    │         ║
║      │ 触发策略检查                                        │         ║
║      ▼                                                     │         ║
║  [DecisionOrchestrator]                                    │         ║
║      │                                                     │         ║
║      ├─ [DecisionContextBuilder] ←─── market.snapshots ───┘         ║
║      │      构建决策上下文                                           ║
║      │                                                               ║
║      ├─ [BaselineStrategy]                                           ║
║      │      基线评估 (regime + alpha → 方向偏好)                     ║
║      │                                                               ║
║      ├─ [AIInferenceService] (可选)                                  ║
║      │      OpenAI → 结构化 JSON → 验证 → 降级管理                   ║
║      │                                                               ║
║      ├─ [TargetPositionEngine]                                       ║
║      │      信号边际 - 成本 = 净边际 → 目标仓位                     ║
║      │                                                               ║
║      ├─ [PolicyEngine]                                               ║
║      │      symbol 白名单、做空/杠杆许可、kill switch                ║
║      │                                                               ║
║      └─ [RiskEngine]                                                 ║
║             仓位/名义上限、保证金、自适应收缩                        ║
║      │                                                               ║
║      ▼                                                               ║
║  [StrategyCoordinator]                                               ║
║      │                                                               ║
║      ├─ 评估所有策略族                                              ║
║      │   smart_arbitrage / spot_grid / dca / directional /           ║
║      │   protective / opportunistic / independent                    ║
║      │                                                               ║
║      ├─ Allocator 选择                                              ║
║      │                                                               ║
║      └─ Sleeve 三层控制                                             ║
║          ① Permission → ② Budget → ③ Routing                        ║
║      │                                                               ║
║      ▼                                                               ║
║  [ExecutionPlanner]                                                  ║
║      │ Intent → Plan → OrderIntent                                   ║
║      ▼                                                               ║
║  [OrderManager]                                                      ║
║      │ 守卫 → 适配器提交 → 状态跟踪                                 ║
║      │                                                               ║
║      ├─ PaperAdapter (模拟) ──── 立即合成成交                        ║
║      └─ OKXAdapter (交易所) ──── HTTP 提交 → 状态查询               ║
║      │                                                               ║
║      ▼                                                               ║
║  execution.fill_events                                               ║
║      │                                                               ║
║      ▼                                                               ║
║  [PortfolioService]                                                  ║
║      │ 应用成交 → 更新持仓 → Lot 追踪 → 快照构建                   ║
║      ▼                                                               ║
║  portfolio.snapshots                                                 ║
║      │                                                               ║
║      ▼                                                               ║
║  [ReconciliationService]                                             ║
║      │ 本地 vs 交易所对比 → 分级 → 自动修复 / operator review       ║
║      ▼                                                               ║
║  [RecoveryControl]                                                   ║
║      │ 启动恢复 → 安全评估 → resume / halt                         ║
║      ▼                                                               ║
║  [BlockerControl] → [Operator UI]                                    ║
║      阻断面板 → 操作员执行消解动作                                   ║
║                                                                      ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  [研究数据平台 (RDP)]                                                ║
║      │                                                               ║
║      ├─ 数据采集: Backfill ZIP + Rolling API → Staging              ║
║      ├─ 质量检查 → Bronze → Silver → Gold                          ║
║      ├─ Phase 2: 参数回放扫描 (27 组合)                             ║
║      ├─ Phase 3: 实盘归因 (瀑布分类)                                ║
║      ├─ Phase 4: 执行可行性 (滑点/成交)                             ║
║      ├─ Phase 5: 参数治理 (冻结候选集)                              ║
║      ├─ Phase 6: 综合决策 → 推荐                                    ║
║      └─ 发布 → active_parameter_sets → 主系统注入                   ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
```

---

## 18. 关键设计不变量

1. **幂等性**: 相同 intent_id 或 idempotency_key 的操作只处理一次
2. **状态不可倒退**: 订单状态机不允许从高优先级状态回退到低优先级
3. **Unknown Write 保护**: 新风险增加操作在 unknown write 未解决前被阻断，但风险缩减操作豁免
4. **余额预留**: ExecutionObligationService 预留余额防止超额承诺
5. **终态不可变**: FILLED/CANCELED 订单不再转换
6. **对账门禁**: 对账要求 review 时阻断恢复
7. **保护性优先**: 风控门禁阻断风险增加操作，但不阻断保护性退出
8. **Fail-closed**: 依赖不可确认时倾向阻断新增风险、保留保护性动作
9. **执行真相优先**: 交易所上的真实状态优先于本地意图
10. **RDP 参数注入 fail-soft**: 加载失败时继续使用原参数，不中断系统
