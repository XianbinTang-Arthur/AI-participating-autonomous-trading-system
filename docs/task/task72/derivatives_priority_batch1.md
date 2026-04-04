# Task72-A1 合约优先版第一批具体开发任务

## 1. 目标

把“合约优先版独立实施清单”进一步收敛成第一批可以直接进入开发排期的任务。

这批任务只解决合约真钱运行的第一道硬门槛：

- 启动约束必须正确
- 合约账户模式与产品规则必须被系统理解
- 订单意图和持久化真相必须带上合约关键语义
- 下单前风险 gate 必须能阻断明显危险行为
- 交易所提交与恢复必须围绕合约语义收敛

这批任务完成后，系统仍然不能宣称“合约已可生产运行”，但应该进入“可以继续推进第二批账务闭环与对账停机”的状态。

## 2. 本批边界

### 本批纳入

- 合约配置与启动门槛
- 合约账户模式 / 产品规则快照
- 合约订单语义扩展
- 合约 pre-trade 风控 v1
- 合约提交与恢复 gate v1
- 合约 operator 可见性最小闭环

### 本批不纳入

- 资金费正式记账与报表闭环
- 合约已实现 / 未实现盈亏完整财务投影
- 合约小资金 `guarded_live` 前向验证
- 放量评审
- 多交易所支持

## 3. 当前代码判断

当前仓库里已经存在一些合约基础，但还不够作为实盘准入能力：

- `AATSSettings` 已有 `trading_product_type`、`margin_mode`、`max_target_leverage`、`max_margin_usage_fraction`、`liquidation_buffer_fraction` 等字段
- `ExchangeAccountSnapshot` 已有 `account_mode`、`position_mode`、`account_risk`、`instruments`
- `OKXAccountService` 已拉取账户配置、风险视图、交易费率、system status、recent bills
- `OrderIntent` / `OrderState` / `FillEvent` 只有 `margin_mode`、`target_leverage`、`exposure_side`，还没有显式的 `tdMode` / `posSide` / reduce-only 语义快照
- `RiskDecision` 仍然过于简化，无法承载合约 pre-trade 解释链
- `OKXOrderPayloadBuilder` 目前用 `margin_mode` 推出 `tdMode`，用 `exposure_side` 推出 `posSide`，这对真钱合约还不够严谨

因此，第一批任务必须先把这些基础问题做硬。

## 4. 任务拆分

### Task72-A1：合约启动约束与配置分层固化

目标：

- 让 `.env.derivatives`、runtime profile、YAML 配置和代码默认值的生效顺序可验证
- 在启动阶段就阻断错误账户、错误环境、错误产品线

主要改动：

- 梳理并固化 `aats/bootstrap/env_profiles.py`、`aats/bootstrap/settings.py`、`aats/bootstrap/config.py`、`configs/*.yaml` 的配置来源优先级
- 增加“合约模式启动前自检”：
  - 账户后端必须是 `okx`
  - `trading_product_type` 必须是 `derivatives`
  - `margin_mode` 不允许是 `cash`
  - `account_read_enabled` 必须开启
  - 未配置凭证或数据库时禁止进入真钱运行态
- 把合约专用 profile 与 spot profile 明确分开，禁止混用
- 为 operator 高风险写操作补齐鉴权和审计前置要求

建议落点：

- `aats/bootstrap/env_profiles.py`
- `aats/bootstrap/settings.py`
- `aats/bootstrap/config.py`
- `aats/api/routes.py`
- `configs/base.yaml`
- `configs/staging.yaml`
- `configs/prod.yaml`
- `tests/integration/test_operator_api.py`

产出：

- 合约启动自检规则
- 合约专用配置模板
- 合约 profile 启动测试

验收：

- 错误 profile、错误 `margin_mode`、缺失凭证、错误产品类型时启动直接失败
- operator 不能在未授权状态下修改合约高风险运行参数

代码级开发子项：

- A1.1 为启动脚本注入 `startup_profile` 标记，并在运行时暴露该标记
- A1.2 拆出现货 / 合约专用 `guarded_*` 配置档，禁止 profile 与产品线混用
- A1.3 在 `build_runtime` 中加入合约 exchange runtime 的硬门槛校验
- A1.4 为 `.env` 模板、配置加载和启动 guard 补齐回归测试

### Task72-A2：合约账户模式与产品规则快照建模

目标：

- 把交易所账户模式和合约产品规则变成系统内可复用、可持久化、可比较的结构化数据

主要改动：

- 扩展 `aats/schemas/exchange.py`，把 raw 账户配置、手续费、风险信息和合约产品规则抽成结构化 schema
- 梳理 `aats/services/execution_engine/okx_account.py` 的解析逻辑，把 `account_config`、`trade_fee`、`account_position_risk`、`system_status`、`instruments` 解析为 typed schema，而不是仅留在 `raw`
- 为 operator / query service 提供“当前账户配置、风险快照、主合约规则、受跟踪合约规则”读取接口
- 为后续 A3/A4 的订单语义、pre-trade 风控和恢复阻断提供统一的合约规则读取入口

建议落点：

- `aats/schemas/exchange.py`
- `aats/services/execution_engine/okx_account.py`
- `aats/services/operator/query_service.py`
- `aats/storage/*snapshot*`
- `tests/unit`
- `tests/integration`

产出：

- 合约账户模式 schema
- 合约产品规则 schema
- 账户模式 / 产品规则快照接口

验收：

- 系统能明确回答当前账户是 `cross` 还是 `isolated`、是 `net` 还是 `long_short`
- 系统能明确回答某个合约品种的 `lot size`、`tick size`、`contract value`、`settle currency` 和风险相关参数

代码级开发子项：

- A2.1 在 `aats/schemas/exchange.py` 中新增账户配置、手续费计划、风险快照、系统状态和合约规则扩展字段
- A2.2 在 `aats/services/execution_engine/okx_account.py` 中把 OKX raw payload 解析为 typed schema，并暴露结构化读取方法
- A2.3 在 `aats/services/operator/query_service.py` 中把结构化账户快照和产品规则暴露到 `/account/state` 与 `/system/runtime`
- A2.4 为 `tests/unit/test_okx_account.py` 和 `tests/integration/test_operator_api.py` 补齐结构化 schema 回归测试

### Task72-A3：合约订单语义与执行真相扩展

目标：

- 让合约订单从意图到持久化真相都带上关键账户语义，而不是只靠 `margin_mode` 和 `exposure_side` 猜

主要改动：

- 扩展 `aats/schemas/execution.py`：
  - `OrderIntent`
  - `ExecutionPlan`
  - `OrderState`
  - `FillEvent`
- 新增或显式固化字段：
  - `td_mode`
  - `position_mode`
  - `pos_side`
  - `reduce_only_reason`
  - `close_only_reason`
  - `instrument_family`
  - `settle_currency`
- 扩展 `aats/storage/sqlalchemy_models.py` 与对应 repo / migration，让上述字段进入 `execution_orders`、`execution_fills`、`order_states`、`fill_events`
- 确保 outbox / replay / recovery / operator 查询链路不会丢这些字段

建议落点：

- `aats/schemas/execution.py`
- `aats/storage/sqlalchemy_models.py`
- `aats/storage/execution_order_repo_postgres.py`
- `aats/storage/execution_repo_converged_postgres.py`
- `migrations/*`
- `aats/services/execution_engine/outbox.py`
- `aats/services/reconciliation_service/replay.py`
- `tests/unit/test_task58_converged_execution_truth.py`

产出：

- 合约订单语义扩展后的 schema
- 持久化迁移脚本
- execution truth 回归测试

验收：

- 任一合约订单都能从 DB 中直接读出 `td_mode`、`pos_side`、`reduce_only`
- 重放和恢复不会丢失这些字段

代码级开发子项：

- A3.1 在 `aats/schemas/execution.py` 中扩展 `OrderIntent`、`ExecutionPlan`、`OrderState`、`FillEvent`，补齐 `td_mode`、`position_mode`、`pos_side`、`reduce_only_reason`、`close_only_reason`、`instrument_family`、`settle_currency`
- A3.2 在 `aats/services/execution_engine/planner.py` 与 `aats/bootstrap/config.py` 中把账户配置和 instrument rule 注入执行计划，避免合约下单阶段再猜 `pos_side`
- A3.3 在 `aats/services/execution_engine/order_manager.py`、`aats/services/execution_engine/okx_adapter.py`、`aats/services/execution_engine/paper_adapter.py` 中把合约语义贯穿到下单、同步、失败态和 fill 生成路径
- A3.4 在 `aats/storage/sqlalchemy_models.py`、`aats/storage/execution_repo_postgres.py`、`aats/storage/execution_order_repo_postgres.py`、`aats/storage/execution_fill_repo_v2_postgres.py`、`aats/storage/execution_repo_converged_postgres.py` 中把这些字段落到显式列，不再只依赖 JSON payload
- A3.5 新增 `migrations/0003_task72_execution_semantics.sql`，把历史 `payload` 和 `submission_payload` 中已有的合约语义回填到结构化列
- A3.6 在 `aats/services/reconciliation_service/replay.py` 中增加 execution semantic mismatch 校验，让 replay 能直接发现 `td_mode`、`position_mode`、`pos_side`、`reduce_only` 等字段漂移
- A3.7 为 `tests/unit/test_execution_planner.py`、`tests/unit/test_guarded_simulated.py`、`tests/unit/test_task58_converged_execution_truth.py`、`tests/integration/test_execution_outbox_postgres.py`、`tests/integration/test_legacy_postgres_upgrade_path.py`、`tests/integration/test_persistence_and_replay.py` 补齐 A3 回归测试

### Task72-A4：合约 pre-trade 风控与限仓 v1

目标：

- 在本地下单前完成第一版合约风控，而不是依赖交易所 reject

主要改动：

- 扩展 `aats/schemas/governance.py` 中的 `RiskDecision`，增加合约可解释字段：
  - `required_initial_margin`
  - `projected_margin_usage`
  - `projected_notional`
  - `only_reduce_required`
  - `risk_limit_breached`
  - `liquidation_buffer_remaining`
- 扩展 `aats/bootstrap/settings.py` 增加合约专用限制：
  - `max_gross_notional_per_symbol`
  - `max_pending_notional_per_symbol`
  - `max_total_open_notional`
  - `max_daily_realized_loss_usdt`
  - `derivatives_only_reduce_trigger_margin_fraction`
- 重写 `aats/services/governance_engine/risk.py` 的合约分支：
  - 基于持仓、挂单、可用保证金计算可开仓额度
  - 区分开仓、减仓、平仓、反手
  - 高风险时把新单降级为 `only_reduce`
  - 超限时直接拒绝
- 在 operator 查询结果里输出清晰的拒单 reason code 和 UTF-8 中文说明

建议落点：

- `aats/schemas/governance.py`
- `aats/bootstrap/settings.py`
- `aats/services/governance_engine/risk.py`
- `aats/services/operator/query_service.py`
- `aats/services/operator/*`
- `tests/unit`
- `tests/integration/test_operator_api.py`

产出：

- 合约风控 v1
- 风控拒绝码字典
- 合约风控回归测试

验收：

- 新开仓、加仓、减仓、平仓、反手都能给出不同风险判断
- 风险超限时会拒绝或强制 `only_reduce`，不会继续放开新暴露

代码级开发子项：

- A4.1 在 `aats/schemas/governance.py` 中扩展 `RiskDecision`，补齐 `required_initial_margin`、`projected_margin_usage`、`projected_notional`、`only_reduce_required`、`risk_limit_breached`、`liquidation_buffer_remaining`
- A4.2 在 `aats/bootstrap/settings.py` 和衍生 profile 配置中增加 `max_gross_notional_per_symbol`、`max_pending_notional_per_symbol`、`max_total_open_notional`、`max_daily_realized_loss_usdt`、`derivatives_only_reduce_trigger_margin_fraction`
- A4.3 在 `aats/services/governance_engine/risk.py` 中把合约风控改成显式区分开仓、加仓、减仓、平仓、反手，并基于账户风险快照、挂单名义金额和当日已实现亏损做 only-reduce / 硬拒绝判断
- A4.4 在 `aats/services/operator/query_service.py` 中为 `risk_decision` 增加结构化中文解释，输出 `rejection_reason_details`、`constraint_details` 和 `operator_summary`
- A4.5 在 `aats/api/static/modules/terms.js` 中补齐新增合约风控 reason code 的 UTF-8 中文映射，避免控制面直接暴露英文内部码
- A4.6 为 `tests/unit/test_guarded_live.py`、`tests/unit/test_settings.py`、`tests/integration/test_operator_api.py` 补齐 A4 回归测试，覆盖 only-reduce、当日亏损限制、待成交名义金额上限和 operator 风控解释

### Task72-A5：OKX 合约提交语义与交易所一致性校验

目标：

- 让系统提交给 OKX 的合约 payload 与真实账户模式、持仓模式、reduce-only 语义一致

主要改动：

- 收紧 `aats/services/execution_engine/okx_adapter.py`：
  - 提交前校验 intent 中的 `td_mode` / `pos_side` 是否与账户快照一致
  - `reduceOnly` 不再仅靠布尔值透传，而要结合订单动作和账户状态
  - 对 net / long_short 两类模式分别构造 payload
  - 对 `closeOnly`、`reduceOnly`、杠杆、最小下单量、最大下单量做提交前校验
- 必要时拆分 `OKXOrderPayloadBuilder` 的 spot / derivatives 构造路径
- 把交易所预校验失败分类成稳定的本地错误码

建议落点：

- `aats/services/execution_engine/okx_adapter.py`
- `aats/services/execution_engine/okx_rest.py`
- `aats/services/execution_engine/order_manager.py`
- `tests/unit`
- `tests/integration/test_execution_outbox_postgres.py`

产出：

- 合约 payload builder v2
- OKX 提交前一致性 gate
- 提交前拒单测试

验收：

- 账户模式不匹配、持仓方向不匹配、reduce-only 语义错误时，本地直接阻断
- 生成的 OKX payload 可以清晰解释每个关键字段来自哪里

代码级开发子项：

- A5.1 在 `aats/schemas/execution.py`、`aats/services/execution_engine/planner.py`、`aats/bootstrap/config.py` 中把 `required_initial_margin`、`projected_margin_usage`、`projected_notional`、`only_reduce_required`、`risk_limit_breached`、`liquidation_buffer_remaining` 从 `RiskDecision` 传到 `ExecutionPlan` / `OrderIntent`
- A5.2 在 `aats/services/execution_engine/okx_adapter.py` 中增加 derivatives submit semantic gate，提交前校验 `td_mode`、`position_mode`、`pos_side`、`reduce_only`、`close_only`、杠杆和 instrument size limit
- A5.3 在 `aats/services/execution_engine/okx_adapter.py` 中把 `only_reduce_required` 等风险上下文写入 `submission_payload`，保证 blocked / submitted / replay 路径都能回溯本地下单依据
- A5.4 在 `aats/api/static/modules/terms.js` 中补齐 A5 新增本地错误码的 UTF-8 中文映射，避免控制面直接暴露英文内部码
- A5.5 为 `tests/unit/test_execution_planner.py` 和 `tests/unit/test_guarded_simulated.py` 补齐 A5 回归测试，覆盖 `td_mode` 不一致、`pos_side` 非法、risk only-reduce 绕过尝试、instrument 杠杆上限和 close/reduce 语义校验

### Task72-A6：合约恢复阻断与未知中间态分类 v1

目标：

- 在系统重启、私有 WS 抖动或 REST 补拉不一致时，优先阻断而不是冒险继续交易

主要改动：

- 扩展 `aats/services/execution_engine/recovery.py` 与 `aats/services/reconciliation_service/*`
- 增加合约专用未知状态分类：
  - 订单本地存在但交易所状态未知
  - 交易所有仓位但本地订单链路不完整
  - `posSide` / `position_mode` 与本地记录不一致
  - 已观察到 fill，但本地未完成记账
- 为上述情况定义：
  - `halt`
  - `review_required`
  - `resume_blocked`
  - `only_reduce`
- operator 页面需要能展示“为什么当前不允许继续开仓”

建议落点：

- `aats/services/execution_engine/recovery.py`
- `aats/services/reconciliation_service/comparator.py`
- `aats/services/reconciliation_service/repair.py`
- `aats/services/operator/query_service.py`
- `tests/integration/test_recovery.py`
- `tests/integration/test_phase4_recovery_reconciliation_runtime.py`

产出：

- 合约恢复阻断分类 v1
- 合约异常状态说明文案
- 恢复阻断回归测试

验收：

- 启动恢复发现未知中间态时，系统不会继续开新仓
- operator 可以看到具体阻断原因和建议动作

代码级开发子项：

- A6.1 在 `aats/schemas/reconciliation.py`、`aats/schemas/system.py`、`aats/storage/reconciliation_repo_postgres.py` 中补齐 `only_reduce_required`、`only_reduce_reasons`、`unknown_state_details`，保证恢复与对账状态可以持久化并跨重启读取
- A6.2 在 `aats/services/reconciliation_service/comparator.py` 中增加合约未知中间态分类，覆盖本地 open order 在交易所不可确认、交易所 fill 已出现但本地未入账、`position_mode` / `posSide` 与账户模式冲突、交易所有仓位但本地执行链缺失
- A6.3 在 `aats/services/recovery_control/reconciliation_classifier.py`、`aats/services/execution_engine/recovery.py`、`aats/services/recovery_control/startup_recovery.py`、`aats/services/governance_engine/recovery_posture.py` 中把合约恢复姿态细化为 `halt` 与 `only_reduce`，让启动恢复和控制面都能反映真实运行约束
- A6.4 在 `aats/services/governance_engine/risk.py` 与 `aats/bootstrap/config.py` 中把 `only_reduce` 型恢复约束接入 `pre-trade` 风控，确保这类异常状态下不会继续新增暴露
- A6.5 在 `aats/services/operator/query_service.py`、`aats/api/static/app.js`、`aats/api/static/modules/terms.js` 中把 only-reduce 原因、恢复分类和中文文案透出到控制面，避免页面只显示模糊的“可交易”或“恢复受限”
- A6.6 为 `tests/unit/test_reconciliation.py`、`tests/unit/test_task54_recovery_reconciliation.py`、`tests/unit/test_execution_recovery.py`、`tests/unit/test_recovery_posture.py`、`tests/unit/test_guarded_live.py` 补齐 A6 回归，分别覆盖分类、恢复姿态、风控继承与本地阻断

## 5. 建议实施顺序

第一批建议严格按下面顺序推进：

1. `Task72-A1` 合约启动约束与配置分层固化
2. `Task72-A2` 合约账户模式与产品规则快照建模
3. `Task72-A3` 合约订单语义与执行真相扩展
4. `Task72-A4` 合约 pre-trade 风控与限仓 v1
5. `Task72-A5` OKX 合约提交语义与交易所一致性校验
6. `Task72-A6` 合约恢复阻断与未知中间态分类 v1

原因：

- 没有 `A1`，后续任务的运行边界会混乱
- 没有 `A2`，系统无法知道自己正在什么账户模式下交易
- 没有 `A3`，风控与恢复缺少正确的持久化真相
- 没有 `A4`，合约 submit 仍然主要依赖交易所拦错
- 没有 `A5`，payload 语义和账户状态可能错位
- 没有 `A6`，一旦遇到重启或链路不确定，系统就没有安全停机门槛

## 6. 第一批完成标准

第一批完成后，至少要达到以下状态：

- 合约 profile 启动时能验证账户类型、环境和关键配置
- 系统内能读到结构化的合约账户模式和产品规则快照
- 合约订单、状态、成交真相里带有明确的账户和方向语义
- 下单前风控能对开仓、加仓、减仓、平仓做差异化阻断
- OKX 提交前能校验账户模式与 payload 一致性
- 启动恢复发现未知中间态时，系统默认阻断新开仓

## 7. 第一批之后再进入的第二批方向

第一批做完后，下一批再进入以下方向：

- 合约持仓、保证金、PnL、资金费账务闭环
- 合约对账、自动停机与人工恢复细化
- 合约观测、报警和控制面收口
- 合约小资金 `guarded_live` 前向验证

## 8. 结论

如果现在要真正开工，“第一批”不应该从报表或试运行开始，而应该先把合约的配置约束、账户模式、订单语义、风控 gate、交易所提交一致性和恢复阻断做成硬门槛。

这些任务完成前，系统即使能把合约订单发到交易所，也还不具备安全的真钱运行资格。
