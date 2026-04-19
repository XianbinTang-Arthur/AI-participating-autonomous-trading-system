# Live Schema Contract for RDP

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


RDP 读取主交易系统 production DB 的表结构契约文档。

## 1. 总则

- RDP 对 production DB **只读访问**，不写入、不修改
- 连接通过 `RDP_LIVE_DATABASE_URL` 配置
- 所有查询通过 `aats.data_platform.live_query_adapter` 统一收口
- 禁止在脚本中直接写 SQL

## 2. 数据库信息

| 项目 | 值 |
|------|-----|
| 引擎 | PostgreSQL 15+ |
| 访问方式 | SQLAlchemy + raw SQL text |
| 连接池 | pool_size=3, max_overflow=5 |
| 只读强制 | 逻辑层 rollback + 建议使用 DB readonly 用户 |

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
| payload | JSON | 完整载荷 |
| created_at | TIMESTAMP TZ | 创建时间 |

**索引**: (family, created_at), (symbol, created_at), (decision_id, created_at)
**时间字段**: `created_at`
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

## 9. 配置示例

在 `.env.research` 中添加:

```env
RDP_LIVE_DATABASE_URL=postgresql+psycopg://rdp_readonly:password@localhost:5432/aats_production
RDP_LIVE_DB_READONLY=true
```

建议在 PostgreSQL 创建只读用户:

```sql
CREATE ROLE rdp_readonly WITH LOGIN PASSWORD 'secure_password';
GRANT CONNECT ON DATABASE aats_production TO rdp_readonly;
GRANT USAGE ON SCHEMA public TO rdp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO rdp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO rdp_readonly;
```

## 10. Contract 维护注意事项

> **已知偏差（2026-04-12 审查发现）**: attribution 模块中的 live 查询
> 目前直接使用 `attribution/alignment.py` 内的 raw SQL，绕过了
> `live_query_adapter` 统一收口。建议后续将 attribution live 查询
> 迁移到 adapter 层，确保所有 production DB 访问统一管理。
>
> 维护本文档时，应同步检查 `aats/storage/sqlalchemy_models.py` ORM
> 定义，确保 contract 表格与实际字段一致。
