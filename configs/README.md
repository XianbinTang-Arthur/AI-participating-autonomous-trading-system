# configs 目录职责

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../docs/project_positioning.md)。


最后核对：2026-08-25（起始 HEAD `00b6df0f8a8d2665d6cae3e88996843767cd1f56`；未提交 Phase 3A–3V 工作区）

本文档说明配置文件应该放在哪里、如何生效，以及哪些配置在 live 环境属于安全关键项。

配置治理服务于项目的最高目标：通过长期稳定盈利为 AI 持续积累资本。凡是影响仓位规模、执行行为、风控阈值、回滚策略、恢复策略和参数治理的配置，都应优先围绕真实净收益、回撤控制和资本安全来定义，而不是围绕“多出单”或“更激进”来定义。完整定位见 [docs/project_positioning.md](../docs/project_positioning.md)。

## 1. 配置生效顺序

从低到高：

1. `AATSSettings` 代码默认值。
2. managed profile 代码基线。
3. `configs/strategy_profiles/<profile>.yaml` 策略调参（由 managed profile loader 合并；非 mapping 或未知 `AATSSettings` key 会失败关闭）。
4. 根目录 `.env.*` 中允许覆盖的环境字段；managed profile 派生身份字段会被忽略并记录日志。
5. 启动器的显式 bind 参数（当前仅 `start_api.py --host/--port`，通过环境层生效）。
6. `build_runtime()` 从 Postgres 注入 RDP active parameters，覆盖其映射到的策略字段。

`runtime_profile_resolution()` 当前是 `env_only`，旧的运行时 profile 管理控制面不会再插入一层配置。

原则：运行身份、凭证、数据库、端口和 live 安全开关放 `.env.*`；策略细节和可研究参数放 `strategy_profiles/*.yaml` 或 RDP active parameters。OKX 账户可用余额不通过配置填写，必须来自交易所账户快照。

## 2. 托管 profile

| Profile | 运行语义 | 策略文件 | 环境文件 |
| --- | --- | --- | --- |
| `spot` | 现货/cash/模拟盘 | `configs/strategy_profiles/spot.yaml` | `.env.spot` |
| `spot_live` | 现货/cash/实盘 | `configs/strategy_profiles/spot_live.yaml` | `.env.spot.live` |
| `derivatives` | 合约/cross/net/模拟盘 | `configs/strategy_profiles/derivatives.yaml` | `.env.derivatives` |
| `derivatives_live` | 合约/cross/hedge/实盘 | `configs/strategy_profiles/derivatives_live.yaml` | `.env.derivatives.live` |

根目录 `.env.*` 被 gitignore 管理，不能提交真实凭证。

## 3. 目录说明

| 路径 | 用途 |
| --- | --- |
| `strategy_profiles/` | 托管 profile 使用的策略调参 YAML |
| `active_parameter_sets/` | 历史兼容/审计副本；主交易 runtime 不从这里 fallback |
| `rdp_workflows/` | RDP workflow 调度定义 |
| `research_batches/` | RDP 参数扫描批次定义 |
| `research_rounds/` | RDP 研究轮次矩阵 |
| `templates/` | `.env.*.example` 示例模板 |
| `base.yaml`、`dev.yaml`、`prod.yaml`、`guarded_*.yaml` | legacy/manual config_profile 或测试兼容路径，不是托管 profile 的主要配置来源 |

## 4. 应该放在 `.env.*` 的字段

| 类型 | 示例字段 | 原因 |
| --- | --- | --- |
| 数据库 | `AATS_DATABASE_URL`、`AATS_DB_NAME`、`AATS_DATABASE_RUNTIME_LOCK_KEY` | 环境隔离和凭证 |
| API/日志 | `AATS_API_PORT`、`AATS_LOG_DIR` | 实例隔离 |
| 交易所凭证 | `AATS_OKX_API_KEY`、`AATS_OKX_API_SECRET`、`AATS_OKX_API_PASSPHRASE` | secret |
| Operator 会话 | `AATS_OPERATOR_SESSION_SECRET`、`AATS_OPERATOR_SESSION_COOKIE_NAME` | secret / 浏览器隔离 |
| 本地演练规模 | `AATS_DEFAULT_ORDER_QTY`；local paper/demo 的本地账本种子 | 避免 exchange-coupled 余额由配置值驱动 |
| live 安全 | `AATS_OPERATOR_UNSAFE_WRITE_WITHOUT_AUTH` 等非派生安全开关 | 生产安全；`AUTH/LIVE_SUBMIT` 等身份字段由 managed profile 派生 |
| 合约风控 | `AATS_MAX_TARGET_LEVERAGE`、`AATS_MAX_MARGIN_USAGE_FRACTION`、`AATS_LIQUIDATION_BUFFER_FRACTION` | 账户级限制 |
| recovery | `AATS_EXECUTION_UNKNOWN_SUBMIT_REVIEW_AFTER_SECONDS`、`AATS_EXECUTION_UNKNOWN_CANCEL_REVIEW_AFTER_SECONDS` | 运行恢复策略 |

## 5. 应该放在 `strategy_profiles/*.yaml` 的字段

| 类型 | 示例 |
| --- | --- |
| AI 行为 | `ai_operating_mode`、`ai_model_name`、`ai_decision_min_confidence` |
| 策略族启用 | `strategy_family_active`、`smart_arbitrage_enabled`、`spot_grid_enabled`、`dca_enabled` |
| directional 参数 | `strategy_entry_*`、`strategy_scale_in_*`、`strategy_reversal_*` |
| independent 参数 | independent entry/exit/expectancy/guard 参数 |
| sleeve 预算 | `strategy_sleeve_auto_*` |
| 自动换档 | `strategy_profile_auto_control_enabled`；自动回滚没有统一 runtime Settings 开关 |

四个托管 profile 的自动换档默认值均为 `false`（手动切档）。该字段是硬门禁：配置为
`false` 时，页面和 API 都不能临时恢复自动切档；确需自动控制时，必须先显式改为 `true`，
再通过标准提交、同步和重启流程使配置生效。配置允许自动控制后，操作员仍可在页面中临时
暂停或恢复自动切档，运行态以 API 返回的 effective 状态为准。

`strategy_profile_auto_rollback_enabled` 曾出现在四个 profile 中，但没有 Settings 字段或
行为消费者，实际始终被静默忽略。Phase 3P 已删除该伪配置；不要重新加入，除非先完成
真实自动回滚的状态机、权限、审计、失败恢复和端到端测试设计。

## 6. RDP active parameter set 边界

RDP active parameters 会在 `build_runtime()` 时从 `governance.active_parameter_sets` 注入策略参数。数据库是 runtime 唯一真源；未配置数据库或加载失败时使用 profile 参数，不读取 JSON fallback。它是生产行为变更，应按 release 管理：

1. recommendation 必须 approved。
2. pre-apply gate 必须运行。
3. apply 必须记录 actor、gate status、release id。
4. rollback 必须可执行。
5. 生产环境不得跳过 gate。

## 7. live 配置硬约束

live exchange-coupled runtime 必须满足：

| 有效设置 | 要求 |
| --- | --- |
| `AATS_STORAGE_MODE` | `postgres` |
| `AATS_DATABASE_URL` | 已配置，且指向对应 live DB |
| `AATS_DATABASE_SINGLE_RUNTIME_GUARD_ENABLED` | `true` |
| `AATS_EXECUTION_BACKEND` | `okx` |
| `AATS_ACCOUNT_BACKEND` | `okx` |
| `AATS_ACCOUNT_READ_ENABLED` | `true` |
| `AATS_OPERATOR_AUTH_ENABLED` | `true` |
| `AATS_OPERATOR_UNSAFE_WRITE_WITHOUT_AUTH` | `false` |
| `AATS_OPERATOR_SESSION_COOKIE_SECURE` | live 环境为 `true` |

表中多数字段属于 managed profile 派生身份，表示启动后的有效值，不表示应该把它们重新写进 `.env.*`。允许人工维护的环境字段以 `docs/configuration/managed-config-reference.md` 为准。

## 8. 新增配置字段维护规则

新增字段时按以下顺序处理：

1. 在 `aats/bootstrap/settings.py` 增加类型、默认值和说明。
2. 判断归属：环境隔离/secret/资金安全放 `.env.*`，策略可调参数放 YAML/RDP。
3. 更新 `docs/configuration/managed-config-reference.md`。
4. 更新对应 `.env.*.example` 模板。
5. 增加测试：settings parse、managed profile、live startup guard 或策略行为。

`scripts/generate_managed_config_artifacts.py` 只生成 `.env.*.example` 与
`managed-config-reference.md`；本 README 是人工治理入口，生成器不会覆盖它。

## 9. 当前配置相关风险

| 风险 | 状态 | 文档/修复方向 |
| --- | --- | --- |
| legacy YAML 与 managed profile 同时存在 | 可维护性风险 | 托管 profile 以 managed baseline + strategy YAML + 允许的 `.env.*` override + DB active parameters 为准 |
| live `.env.*` secret 本地存在 | 正常但敏感 | 保持 gitignored，不在日志/文档中扩散 |
| derivatives auto halt 与 reduce-only close 语义不清 | 未定 | 在风险策略和 runbook 中明确 |
