# Live Schema Contract for RDP

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。

> 文档状态：现行专题参考
> 最后核对：2026-08-26（起始 HEAD `51448768bb3ff08fa44066d286f7383800d8d744`；含本轮未提交 RDP attribution lineage 修复）
> 核对范围：RDP 读取主交易库的静态契约；不证明当前 live 库、账户或表内数据健康


RDP 读取主交易系统 production DB 的表结构契约文档。这里的 `RDP_LIVE_DATABASE_URL` 是**只读主交易库**，与 RDP 自身可写的 `RDP_DATABASE_URL`/`aats_research` 不是同一边界。RDP 自身 schema 的迁移由部署期 `scripts/apply_schema_migrations.py` 所有；运行进程只读校验。

## 1. 总则

- RDP 对 production DB **只读访问**，不写入、不修改
- 连接通过 `RDP_LIVE_DATABASE_URL` 配置
- 通用 live facts 查询通过 `aats.data_platform.live_query_adapter` / `live_facts.query_adapter` 收口；Phase 3 的关联查询位于 `aats.data_platform.attribution.alignment`，连接会话强制 PostgreSQL `default_transaction_read_only=on`
- 业务脚本不得临时拼接或散落新增 production SQL；新增查询必须进入上述受控模块并同步本契约

## 2. 数据库信息

| 项目 | 值 |
|------|-----|
| 引擎 | PostgreSQL 15+ |
| 访问方式 | SQLAlchemy + raw SQL text |
| 连接池 | pool_size=3, max_overflow=5 |
| 只读强制 | Phase 3 session 设置 `default_transaction_read_only=on`；同时要求 DB readonly 身份 |

## 3. RDP 需要读取的表

### 3.1 strategy_sleeve_intents

**用途**: Phase 3 attribution — 策略意图分析

| 字段 | 类型 | 说明 |
|------|------|------|
| sleeve_intent_id | VARCHAR(64) PK | 意图 ID |
| decision_id | VARCHAR(64) | 决策 ID |
| family | VARCHAR(32) | 策略 family |
| strategy_sleeve_id | VARCHAR(64) | 策略 sleeve ID |
| allocation_id | VARCHAR(64) | 分配 ID |
| state | VARCHAR(32) | 状态 |
| route_action | VARCHAR(32) | 路由动作 |
| inventory_policy | VARCHAR(32) | 库存策略 |
| product_type | VARCHAR(32) | 产品类型 |
| margin_mode | VARCHAR(32) | 保证金模式 |
| symbol | VARCHAR(64) | 交易对 |
| budget_multiplier | NUMERIC(36,18) | 预算乘数 |
| automatic_enabled | BOOLEAN | 是否自动执行 |
| timeframe | VARCHAR(8), nullable | 决策时间框架；旧记录为空时不可归因 |
| signal_bar_start | TIMESTAMP TZ, nullable | 产生信号的 K 线起点 |
| signal_bar_end | TIMESTAMP TZ, nullable | 产生信号的 K 线终点 |
| market_data_asof | TIMESTAMP TZ, nullable | 决策实际可见的市场数据时点 |
| parameter_set_id | VARCHAR(128), nullable | active parameter set 或 profile default 来源标识 |
| runtime_generation | VARCHAR(128), nullable | 标准部署运行代次 |
| code_version | VARCHAR(64), nullable | 从标准部署代次取得的代码提交前缀 |
| market_snapshot_ref | VARCHAR(128), nullable | 市场快照事件引用 |
| feature_snapshot_ref | VARCHAR(128), nullable | 特征快照事件引用 |
| payload | JSON | 完整载荷 |
| created_at | TIMESTAMP TZ | 创建时间 |

**索引**: (family, created_at), (symbol, created_at), (decision_id, created_at), (family, symbol, timeframe, signal_bar_start)
**时间字段**: 新记录按 `signal_bar_start` 选择研究窗口并精确归因；仅缺少该字段的旧记录按 `created_at` 纳入不可归因审计，绝不据此匹配
**Symbol 字段**: `symbol`
**Family 字段**: `family`

### 3.2 portfolio_allocation_decisions

**用途**: Phase 3 attribution — 组合分配决策分析

| 字段 | 类型 | 说明 |
|------|------|------|
| allocation_id | VARCHAR(64) PK | 分配 ID |
| decision_id | VARCHAR(64) | 决策 ID |
| symbol | VARCHAR(64) | 交易对 |
| allocator_version | VARCHAR(32) | 分配器版本 |
| route_action | VARCHAR(32) | 路由动作 |
| primary_family | VARCHAR(32) | 主策略 family |
| portfolio_requested_notional | NUMERIC(36,18) | 请求金额 |
| portfolio_approved_notional | NUMERIC(36,18) | 批准金额 |
| portfolio_budget_cut_notional | NUMERIC(36,18) | 预算削减 |
| expected_edge_bps | NUMERIC(36,18) | 预期边际 bps |
| expected_cost_bps | NUMERIC(36,18) | 预期成本 bps |
| product_type | VARCHAR(32) | 产品类型 |
| payload | JSON | 完整载荷 |
| created_at | TIMESTAMP TZ | 创建时间 |

**索引**: (symbol, created_at), (primary_family, created_at)
**时间字段**: `created_at`
**Symbol 字段**: `symbol`

### 3.3 allocator_budget_snapshots

**用途**: Phase 3 attribution — 预算快照分析

| 字段 | 类型 | 说明 |
|------|------|------|
| budget_snapshot_id | VARCHAR(64) PK | 快照 ID |
| allocation_id | VARCHAR(64) | 分配 ID |
| family | VARCHAR(32) | 策略 family |
| symbol | VARCHAR(64) | 交易对 |
| requested_notional | NUMERIC(36,18) | 请求金额 |
| approved_notional | NUMERIC(36,18) | 批准金额 |
| budget_multiplier | NUMERIC(36,18) | 预算乘数 |
| priority_rank | INTEGER | 优先级排名 |
| clamped | BOOLEAN | 是否被限制 |
| product_type | VARCHAR(32) | 产品类型 |
| payload | JSON | 完整载荷 |
| created_at | TIMESTAMP TZ | 创建时间 |

**时间字段**: `created_at`
**Symbol 字段**: `symbol`

### 3.4 reconciliation_state_snapshots

**用途**: Phase 3 attribution — 对账状态分析

| 字段 | 类型 | 说明 |
|------|------|------|
| snapshot_id | VARCHAR(64) PK | 快照 ID |
| reconciliation_id | VARCHAR(64) FK | 对账报告 ID |
| recovery_state | VARCHAR(32) | 恢复状态 |
| resume_eligible | BOOLEAN | 可恢复 |
| safe_to_trade | BOOLEAN | 安全交易 |
| review_required | BOOLEAN | 需要审查 |
| halt_required | BOOLEAN | 需要停止 |
| product_type | VARCHAR(32) | 产品类型 |
| primary_symbol | VARCHAR(64) | 主交易对 |
| details_json | JSON | 详细信息 |
| created_at | TIMESTAMP TZ | 创建时间 |

**时间字段**: `created_at`
**Symbol 字段**: `primary_symbol`

### 3.5 strategy_execution_bundles

**用途**: Phase 3/4 — 执行包分析

| 字段 | 类型 | 说明 |
|------|------|------|
| bundle_id | VARCHAR(64) PK | Bundle ID |
| decision_id | VARCHAR(64) | 决策 ID |
| family | VARCHAR(32) | 策略 family |
| route_action | VARCHAR(32) | 路由动作 |
| status | VARCHAR(32) | 状态 |
| selected_symbol | VARCHAR(64) | 选定交易对 |
| gross_requested_exposure | NUMERIC(36,18) | 总请求敞口 |
| net_approved_exposure | NUMERIC(36,18) | 净批准敞口 |
| expected_cost_bps | NUMERIC(36,18) | 预期成本 |
| expected_edge_bps | NUMERIC(36,18) | 预期边际 |
| product_type | VARCHAR(32) | 产品类型 |
| payload | JSON | 完整载荷 |
| created_at | TIMESTAMP TZ | 创建时间 |

**时间字段**: `created_at`
**Symbol 字段**: `selected_symbol`

### 3.6 execution_orders

**用途**: Phase 3/4 — 订单分析

| 字段 | 类型 | 说明 |
|------|------|------|
| order_id | VARCHAR(64) PK | 订单 ID |
| intent_id | VARCHAR(64) UNIQUE | 意图 ID |
| decision_id | VARCHAR(64) | 决策 ID |
| symbol | VARCHAR(64) | 交易对 |
| side | VARCHAR(8) | 方向 (buy/sell) |
| order_type | VARCHAR(16) | 订单类型 |
| requested_qty | NUMERIC(36,18) | 请求数量 |
| limit_price | NUMERIC(36,18) | 限价 |
| state | VARCHAR(32) | 订单状态 |
| strategy_family | VARCHAR(32) | 策略 family |
| product_type | VARCHAR(32) | 产品类型 |
| raw_payload | JSON | 原始载荷 |
| created_at | TIMESTAMP TZ | 创建时间 |

**时间字段**: `created_at`
**Symbol 字段**: `symbol`

### 3.7 execution_fills

**用途**: Phase 4 execution realism — 成交分析

| 字段 | 类型 | 说明 |
|------|------|------|
| fill_id | VARCHAR(64) PK | 成交 ID |
| order_id | VARCHAR(64) FK | 订单 ID |
| symbol | VARCHAR(64) | 交易对 |
| side | VARCHAR(8) | 方向 |
| fill_qty | NUMERIC(36,18) | 成交数量 |
| fill_price | NUMERIC(36,18) | 成交价格 |
| fee_amount | NUMERIC(36,18) | 手续费 |
| fee_currency | VARCHAR(16) | 手续费币种 |
| liquidity_role | VARCHAR(16) | 流动性角色 |
| strategy_family | VARCHAR(32) | 策略 family |
| exchange_ts | TIMESTAMP TZ | 交易所时间 |
| ingestion_ts | TIMESTAMP TZ | 入库时间 |
| raw_payload | JSON | 原始载荷 |

**时间字段**: `ingestion_ts`
**Symbol 字段**: `symbol`

## 4. 允许的 NULL 值

以下字段允许为 NULL:

- `expected_edge_bps`, `expected_cost_bps` — 不是所有决策都有预期
- `limit_price` — 市价单无限价
- `fee_currency` — 部分 fill 可能无费用信息
- `strategy_family` — 旧数据可能缺失
- `primary_strategy_sleeve_id` — 部分分配无关联 sleeve

## 5. 状态值定义

### 订单状态 (execution_orders.state)
- `pending_submit` / `submitted` / `partially_filled` / `filled` / `cancelled` / `rejected` / `expired`

### Bundle 状态 (strategy_execution_bundles.status)
- `created` / `executing` / `completed` / `failed` / `cancelled`

### 对账恢复状态 (reconciliation_state_snapshots.recovery_state)
- `clean` / `divergence_detected` / `repair_in_progress` / `repaired`

## 6. 数据保留周期

| 表 | 建议保留 | 说明 |
|----|---------|------|
| execution_fills | 90+ 天 | Phase 4 需要足够的成交数据 |
| execution_orders | 90+ 天 | 与 fills 对应 |
| strategy_execution_bundles | 30+ 天 | attribution 窗口 |
| 其他表 | 30+ 天 | attribution 窗口 |

## 7. 查询性能建议

- 所有查询都应带时间窗口（使用 `created_at` / `ingestion_ts` 索引）
- 默认 limit=1000，避免全表扫描
- symbol 过滤可显著降低查询量
- 建议在 production DB 为 RDP 创建专用 readonly 用户

## 8. 安全要求

- RDP 使用独立的 DB 用户，只授予 SELECT 权限
- 禁止 INSERT / UPDATE / DELETE / TRUNCATE
- 连接字符串存储在 `.env.research`（已被 .gitignore 覆盖）
- live_query_adapter 逻辑层强制 session.rollback()

## 9. 配置与权限边界

`RDP_LIVE_DATABASE_URL` 与 `RDP_LIVE_DB_READONLY=true` 由受控环境配置注入。本文不复制 DSN、用户名或密码示例；也不建议把创建角色/GRANT 语句当作日常操作命令。权限应由数据库管理流程在明确目标库上建立，并独立验证该身份只能 `CONNECT`/`USAGE`/`SELECT`，不能 `INSERT`/`UPDATE`/`DELETE`/DDL。

## 10. Contract 维护注意事项

> **现行边界（2026-08-26）**: attribution 的多表关联查询仍由
> `attribution/alignment.py` 集中维护，没有复用通用 adapter；但 Phase 3 创建的
> PostgreSQL session 已强制 transaction readonly，字段契约由
> `live_facts/contracts.py` 校验。后续若统一 adapter，必须保持精确 lineage 和
> fail-closed 语义。
>
> 维护本文档时，应同步检查 `aats/storage/sqlalchemy_models.py` ORM
> 定义，确保 contract 表格与实际字段一致。
