# Task 113: Independent 实盘前阻断问题修复

## 1. 目标与边界

本次只修两类会直接影响 `independent` 实盘与验证的问题：

- PostgreSQL 测试入口默认没有读取本地 `derivatives_live` 数据库配置，导致每次都要手工导出 `AATS_DATABASE_URL`
- 当前 `independent` 本地 live 风控上限与双书规模不匹配，容易在真实 BTC 价格下被 `gross/pending/total_open` 上限误拦

不在本次范围内：

- 不改 `independent` 策略逻辑
- 不改交易所凭证本身
- 不改 overlay 并行架构

## 2. 当前行为摘要

- `tests/support/postgres.py` 只有在显式环境变量、`.env.test.postgres` 或显式 `AATS_TEST_ENV_TEMPLATE_PROFILE` 存在时才会导入数据库连接
- 本地已经有 `/.env.derivatives.live`，但测试默认不会主动使用
- `.env.derivatives.live` 当前只设置了：
  - `AATS_MAX_ABS_POSITION_QTY=0.02`
  - `AATS_MAX_NOTIONAL_PER_SYMBOL=10000`
- 而 `independent` 双书风控还会同时受：
  - `max_gross_notional_per_symbol`
  - `max_pending_notional_per_symbol`
  - `max_total_open_notional`
  影响。若这些键不显式配置，就会回落到代码默认值，导致双腿在当前 BTC 价格下被提前阻断

## 3. 修改范围

### 3.1 `tests/support/postgres.py`

- 调整 `bootstrap_postgres_test_env()` 默认回退顺序
- 在无显式测试库、无 `.env.test.postgres` 时，自动回退到本地 `derivatives_live` profile

### 3.2 `.env.derivatives.live`

- 显式写入 `AATS_DERIVATIVES_POSITION_MODE=hedge`
- 显式写入与当前 `independent` 双书规模一致的：
  - `AATS_MAX_GROSS_NOTIONAL_PER_SYMBOL`
  - `AATS_MAX_PENDING_NOTIONAL_PER_SYMBOL`
  - `AATS_MAX_TOTAL_OPEN_NOTIONAL`

### 3.3 测试

- 新增/更新 unit test，验证 PostgreSQL 测试入口会默认回退到本地 `derivatives_live` profile
- 集成测试直接验证无需手工导出 `AATS_DATABASE_URL` 时，Postgres 测试可自动起临时 schema

## 4. 一致性与风险控制

- 不修改数据库 schema
- 不修改业务事件模型
- PostgreSQL 集成测试仍使用临时 schema，不会直接污染基础库对象
- 名义上限只做“补齐当前双书规模所需的显式键”，不改变 `independent` 核心风控算法

## 5. 验收标准

- 不手工导出 `AATS_DATABASE_URL` 时，Postgres 测试可以自动从本地 `/.env.derivatives.live` 取库连接
- `independent` 当前双书运行不再被默认 `gross/pending/total_open` 上限意外拦住
- lint、最窄 unit、最窄 integration 通过
