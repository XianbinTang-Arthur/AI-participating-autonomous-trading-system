# Task 190: Independent Book-State / Guard-State 分离小重构

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界
- 把 `independent` 的生命周期状态与 guard 阻断状态拆开，消除 `book_state` 同时承载两种语义的建模耦合。
- 本轮只改 `independent` 状态机、runtime/replay/recovery schema、operator/UI 读数与相关测试。
- 不改 allocator / coordinator 选族逻辑，不改 execution policy，不在本轮调整 `score_stability`。

## 模块职责与领域模型
- `aats/services/strategy_engines/independent/state_machine.py`
  - `book_state` 只表示生命周期：`flat/probing/building/holding/de_risking/forced_exit`
  - 新增 `guard_state` 表示当前 guard：`cooldown/suspended/None`
  - transition guard 校验从“guard 写进 book_state”改成“生命周期迁移 + guard 限制”两层判定
- `aats/services/strategy_engines/independent/engine.py`
  - 负责从 prior runtime state 拆出 lifecycle prior 与 guard prior
  - 兼容历史 payload：旧数据里若 `book_state in {cooldown,suspended}` 且没有 `guard_state`，读取时自动回填为 `prior_guard_state`
- `aats/services/strategy_engines/independent/diagnostics.py`
  - 负责输出新的 `guard_state / prior_guard_state`
  - 停止用 `cooldown_until` 伪造 `suspended_until`
- `aats/services/strategy_engines/independent/replay.py`
  - replay / recovery / decision snapshot 同步暴露 `guard_state / prior_guard_state`
- `aats/schemas/*`
  - additive 新增 `guard_state / prior_guard_state`
- `aats/services/operator/query_service.py` / `aats/api/static/modules/terms.js`
  - operator 摘要与 UI 文案同步展示 guard 信息

## 输入 / 输出接口
- 输入
  - `StrategyBookRuntimeState.book_state`
  - `StrategyBookRuntimeState.guard_state`
  - `StrategyBookRuntimeState.cooldown_until`
  - `StrategyBookRuntimeState.suspended_until`
  - `IndependentBookDecision.blocked_reasons`
  - `DecisionContext.as_of_ts`
- 输出
  - `StrategyBookRuntimeState.book_state`：纯生命周期
  - `StrategyBookRuntimeState.guard_state`：当前 guard
  - `StrategyBookRuntimeState.prior_book_state`：prior 生命周期
  - `StrategyBookRuntimeState.prior_guard_state`：prior guard
  - `transition_valid / transition_violation_reason`

## 数据库 Schema / 表 / 索引 / 约束
- 无数据库 schema 迁移。
- 变更仅发生在现有 runtime / replay / recovery payload 的 additive 字段。

## 事务、一致性、并发
- 无新事务。
- 需要保证同一轮 decision 内：
  - lifecycle 计算
  - guard 计算
  - prior->next transition 校验
  使用同一份 snapshot 语义，不允许再次把 guard 混回 `book_state`。

## 授权、认证、数据安全
- 无新增认证/授权路径。
- 不引入新的外部输入面。

## 错误处理与幂等
- 非法迁移仍保持 fail-closed。
- 为兼容历史 payload，读取旧 runtime state 时要容忍：
  - `book_state="cooldown"` / `"suspended"`
  - `guard_state` 缺失
- 同一份输入重复评估应得到相同的 lifecycle/guard 拆分结果。

## 状态迁移与生命周期
- `book_state` 只允许生命周期状态之间迁移。
- `guard_state` 不再作为 lifecycle 状态参与 `_ALLOWED_TRANSITIONS`。
- active `guard_state` 仍可阻止：
  - `flat -> probing`
  - `holding -> building`
- 旧兼容规则：
  - prior runtime state 若 `guard_state` 缺失但 `book_state in {cooldown,suspended}`，则:
    - `prior_guard_state = legacy book_state`
    - `prior_book_state = inventory-backed base state`

## 缓存与性能
- 只增加轻量字段归一化和兼容读取。
- 无新增 IO、无额外数据库查询。

## 日志、监控、审计
- 保留现有 `transition_valid / transition_violation_reason`。
- operator / replay / recovery / UI 需要能直接看到 `guard_state / prior_guard_state`，避免再从 `book_state` 误推断。

## 测试策略
- 单元测试
  - blocked + active trial guard 时，`book_state=flat/holding`，`guard_state=suspended`
  - blocked + active cooldown 时，`book_state=flat/holding`，`guard_state=cooldown`
  - stale guard 清除后，lifecycle 能重新进入 `probing/building`
  - active guard 仍会 fail-close
  - legacy runtime state 仅写旧 `book_state` 时仍可被正确读取
  - diagnostics 不再用 `cooldown_until` 回填 `suspended_until`
- 最窄集成
  - `evaluate_independent_book` 与 runtime state/replay state 能同时输出 lifecycle + guard
  - operator/UI transition summary 能展示 guard 信息

## 迁移、回滚、兼容
- 无 migration。
- 回滚方式是恢复本轮 additive 字段与状态机改动。
- 兼容策略：
  - 新 payload additive 增加 `guard_state / prior_guard_state`
  - 旧 payload 读取时保留 fallback

## 配置与环境隔离
- 不新增配置项。
- 不修改 `.env`、profile、live 阈值。

## 代码组织与依赖
- 目标文件：
  - `aats/services/strategy_engines/independent/state_machine.py`
  - `aats/services/strategy_engines/independent/engine.py`
  - `aats/services/strategy_engines/independent/diagnostics.py`
  - `aats/services/strategy_engines/independent/replay.py`
  - `aats/services/strategy_engines/independent/models.py`
  - `aats/services/strategy_engines/families/independent_models.py`
  - `aats/schemas/strategy_runtime.py`
  - `aats/schemas/system.py`
  - `aats/schemas/operator.py`
  - `aats/services/operator/query_service.py`
  - `aats/api/static/modules/terms.js`
  - 相关 unit / integration / scenario tests

## 文档与运维手册
- 本文档作为本轮小重构说明。
- 若上线后仍反馈“强趋势不下单”，下一轮单独处理 `score_stability` 定义与阈值。

## 部署与验收标准
- 新生成的 runtime state 中：
  - `book_state` 不再出现 `cooldown/suspended`
  - `guard_state` 承载 guard 语义
- legacy runtime state 仍能被正确读取并参与 transition 校验
- `suspended_until` 不再借用 `cooldown_until`
- lint / unit tests / 最窄 integration tests 通过
