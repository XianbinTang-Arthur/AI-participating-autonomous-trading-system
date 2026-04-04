# Task107：测试入口 profile dotenv 与 independent 主模式调整

## 目标

- 让 PostgreSQL 相关测试在直接执行 `pytest` 时，不再要求人工先导出 `AATS_DATABASE_URL`。
- 保持现有多模式能力不变，只把合约 live 的主 overlay 模式切到 `independent`。

## 边界

- 不改交易逻辑。
- 不关闭 `opportunistic` 或 `protective` 的能力开关。
- 不改 `derivatives.yaml`，只调整 `derivatives_live.yaml` 的主模式。

## 方案

### 测试入口

- 在 `tests/conftest.py` 启动时统一执行 PostgreSQL 测试环境引导。
- 优先使用外部已经注入的 `AATS_DATABASE_URL`。
- 如果缺失，则按下面顺序寻找测试专用来源：
  - `AATS_TEST_DATABASE_URL`
  - 本地 `/.env.test.postgres`
  - 显式指定的 `AATS_TEST_ENV_TEMPLATE_PROFILE`
- 不再默认读取 `.env.derivatives.live`，避免把 live 密钥文件作为测试默认入口。

### 配置

- `configs/strategy_profiles/derivatives_live.yaml`
  - 将 `strategy_hedge_overlay_mode` 从 `protective` 调整为 `independent`
  - 保留 `strategy_hedge_opportunistic_enabled=true`
  - 保留 `strategy_hedge_independent_enabled=true`

## 一致性与兼容性

- 外部显式注入的 `AATS_DATABASE_URL` 优先级最高，不会被测试入口覆盖。
- 只在缺少数据库连接时做 profile fallback，避免影响已有 CI/本地显式环境。
- live 配置只切主模式，不改变其余 rollout 开关与阈值。

## 验证

- `ruff check`：针对改动文件通过。
- `pytest tests/unit/test_env_profiles.py -q`
- `pytest tests/integration/test_persistence_and_replay.py -q -k "independent_overlay_bundle_consistent"`
- `pytest tests/integration/test_recovery.py -q -k "overlay_bundle_review_required_without_open_orders"`

## 风险

- 如果没有显式提供 `AATS_TEST_DATABASE_URL`、也没有本地 `/.env.test.postgres`、也没有显式指定 `AATS_TEST_ENV_TEMPLATE_PROFILE`，PostgreSQL 测试仍会被跳过。
- 测试入口只自动补数据库连接，不会把整套 live 环境变量注入测试进程。
