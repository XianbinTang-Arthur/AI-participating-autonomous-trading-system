# Task66: PostgreSQL 历史生产库无缝升级方案

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 目标
- 保留最新 PostgreSQL 基线 schema，作为新库初始化入口。
- 为历史生产库提供可重复执行的原地升级路径。
- 保证同一套 `migrations/*.sql` 同时适用于：
  - 新库从零建库
  - 历史库停机升级
  - 测试环境临时 schema 回放

## 方案概览
采用双轨 migration 结构：

1. `0001_postgres_latest_schema.sql`
- 负责最新基线 schema。
- 新库只需要执行这一条基线和后续补丁即可完成初始化。

2. `0002_postgres_legacy_upgrade.sql`
- 负责历史生产库归一化升级。
- 只做幂等变更：
  - `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  - `ALTER TABLE ... ALTER COLUMN TYPE`
  - `CREATE INDEX IF NOT EXISTS`
  - 旧数据回填
- 允许在新库上重复执行而无副作用。

## 设计原则
- 不重建业务表，不破坏历史数据。
- 不依赖已经删除的旧增量 migration 链。
- 所有旧 payload 兼容问题由两层共同处理：
  - SQL 层补齐结构和关键字段
  - Repo 层兼容读取稀疏旧 payload
- 升级目标是把历史库直接归一化到当前运行时可接受的最新 schema。

## 升级覆盖范围
### 历史核心表
- `event_store`
- `portfolio_snapshots`
- `order_states`
- `fill_events`
- `reconciliation_reports`
- `decision_audit_records`
- `order_obligations`

### 中后期新增表或演化表
- `fill_outcomes`
- `reservations`
- `settlements`
- `position_lots`
- `lot_events`
- `execution_orders`
- `execution_fills`
- `execution_commands`
- `ledger_accounts`
- `ledger_journals`
- `ledger_entries`

## 实施细则
### 步骤 1：建立最新基线
- 用 `0001_postgres_latest_schema.sql` 定义当前 PostgreSQL 权威 schema。
- 基线只服务于新库和测试临时库初始化。

### 步骤 2：建立历史库升级补丁
- 用 `0002_postgres_legacy_upgrade.sql` 对已存在历史库做结构归一化。
- 具体包括：
  - 补齐旧表缺失列
  - 从旧 payload 回填必要字段
  - 修正状态列宽
  - 修正 lot 相关外键语义
  - 补齐关键索引

### 步骤 3：补齐运行时兼容读取
- 对历史稀疏 payload，不要求 SQL 一次性把所有 JSON 都改写成最新格式。
- 在 PostgreSQL repo 层做兼容合成，确保运行时仍可读取：
  - `execution_repo_postgres`
  - `portfolio_repo_postgres`
  - `reconciliation_repo_postgres`

### 步骤 4：增加真实 PG 升级验证
- 构造代表性 legacy schema。
- 在真实 PostgreSQL 临时 schema 中执行：
  - 建立旧 schema
  - 执行当前 `migrations/*.sql`
  - 执行 `validate_runtime_schema()`
  - 启动 runtime
- 验证升级后系统能正常构建和读取。

## 本次已实现内容
### Migration
- 新基线：
  - `migrations/0001_postgres_latest_schema.sql`
- 历史升级补丁：
  - `migrations/0002_postgres_legacy_upgrade.sql`

### 运行时兼容
- `aats/storage/execution_repo_postgres.py`
- `aats/storage/portfolio_repo_postgres.py`
- `aats/storage/reconciliation_repo_postgres.py`

### 真实 PostgreSQL 集成验证
- `tests/integration/test_legacy_postgres_upgrade_path.py`

## 验收标准
- 新库执行当前 `migrations/*.sql` 后可通过关键 PostgreSQL 集成测试。
- 历史库代表性 schema 升级后可通过：
  - `validate_runtime_schema()`
  - `build_runtime()`
  - execution / ledger / recovery / control plane 的关键 PG 回归

## 运维边界
- 当前方案适用于停机升级或维护窗口升级。
- 这不是零停机滚动升级方案。
- 如果未来要支持零停机，还需要单独设计：
  - 在线索引构建
  - 分批回填
  - 新旧读写兼容窗口
  - 版本化应用切换策略

## 本次结论
- 历史生产库“无缝升级”目标已具备可执行方案和实现。
- 对新库，使用最新基线。
- 对历史库，使用历史升级补丁。
- 对稀疏旧 payload，运行时 repo 兼容读取兜底。
- 关键真实 PostgreSQL 升级路径已通过集成验证。
