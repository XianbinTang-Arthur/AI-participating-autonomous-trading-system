## 目标

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


收紧 independent 子域的状态持久化、replay/recovery 一致性与可测试性，避免继续依赖事后推断。

## 边界

- 仅修改 independent 子域、相关 runtime schema 与 `aats/services/strategy_engines/__init__.py`
- 不改 allocator、coordinator 选族语义
- 不改对外 API 路由

## 模块职责与领域模型

- `independent/models.py`
  - `IndependentBookDecision` 只表示一次独立书决策，内部集合字段必须真正不可变
- `independent/state_machine.py`
  - 负责状态快照、状态转移约束与显式 prior-state 校验
- `independent/engine.py`
  - 负责优先消费已持久化 runtime state，而不是重新推断 scale-in / de-risk 计数
- `independent/replay.py`
  - 负责保留 prior/next state 与转移有效性信息
- `strategy_runtime.py`
  - 负责为 runtime snapshot 增加加性字段，承载 prior state 与转移校验结果

## 输入输出接口

- 输入：
  - `recent_targets_by_family["independent"]` 中最近一次持久化的 `book_runtime_states`
- 输出：
  - `StrategyBookRuntimeState` 中的 prior-state / transition 校验字段
  - replay/recovery 摘要中的更可信状态来源

## 数据一致性与并发

- 本轮不引入新表
- 继续依赖现有 allocation decision / runtime snapshot payload 持久化

## 错误处理

- 非法 prior -> next 状态转移不抛运行时异常
- 通过显式 `transition_valid` / `transition_violation_reason` 暴露给 replay/recovery/operator

## 生命周期

- prior runtime state 存在时，优先继承：
  - `book_state`
  - `current_scale_in_count`
  - `current_de_risk_count`
  - `state_version`
  - `last_transition_reason`
- 当前决策完成后再生成 next snapshot

## 测试策略

- 单测覆盖：
  - prior runtime state 继承
  - transition 校验
  - `IndependentBookDecision` 真正不可变
  - strategy_engines 包 lazy import
- 集成测试覆盖：
  - independent replay / recovery 读取新字段

## 兼容性

- 所有 schema 变更保持 additive
- 取消 `aats.services.strategy_engines` 对 `StrategyCoordinatorService` 的包级 re-export
- 需要 coordinator 的调用方改为显式从 `aats.services.strategy_engines.coordinator` 导入
