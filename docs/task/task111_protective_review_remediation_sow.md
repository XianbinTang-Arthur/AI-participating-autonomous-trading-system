# Task111 Protective Review Remediation SOW

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 1. 业务目标与边界
- 目标：针对合约保护性对冲主线（`protective`）做一次定向 review，修复已确认的前端展示回归，并补齐对应验证。
- 本次覆盖：
  - `strategy-view` 中 protective overlay 的状态展示与说明文案回退
  - `protective` 主链集成测试夹具的风险边界对齐
- 非目标：
  - 不重写 protective 压力评分
  - 不改 execution / risk 的核心风控规则
  - 不改数据库 schema 或 public API

## 2. 当前行为摘要
- protective overlay 在 `derivatives + hedge` 下以 directional 主腿为基准，按压力分数生成保护腿。
- Operator/策略页会显示 overlay 配置、状态、阻断原因与当前腿语义。
- 主链集成测试通过 mock `PositionTarget.strategy_execution_legs` 验证 protective bundle -> order intent -> order state 的链路。

## 3. 模块职责与领域模型
- `aats/api/static/modules/views/strategy-view.js`
  - 决定 protective / opportunistic / independent 的状态文案、详情文案与提示等级。
- `tests/integration/test_dashboard_ui.py`
  - 校验 protective overlay 在策略页上的展示回归。
- `tests/integration/test_mainline_chain.py`
  - 校验 protective overlay 主链 bundle 与订单事实链是否完整。

## 4. 输入 / 输出接口
- 输入：
  - `strategyRuntime.configured_parameters.directional.hedge_overlay_*`
  - `latestDecision.position_target.hedge_overlay_decision`
- 输出：
  - 策略页 protective overlay 的状态、详情与说明
  - 主链集成测试中的 `order_intent_refs` / `order_state_refs`

## 5. 数据库 schema / 表 / 索引 / 约束
- 本次不改 schema。

## 6. 事务、一致性与并发
- 前端文案回退为纯只读逻辑调整，不引入新的事务边界。
- 测试夹具仅用于验证链路，不影响运行时并发模型。

## 7. 授权、认证与数据安全
- 不改鉴权、会话或密钥逻辑。
- 不新增外部网络依赖。

## 8. 错误处理与幂等性
- UI 在缺少 `hedge_overlay_enabled_in_mode` / `hedge_overlay_mode_ready` 字段时，应对 `protective` 做兼容回退，而不是误判为“当前模式未单独打开”。
- 集成测试夹具应显式给出与 mock 目标一致的风控上限，避免无关风控阈值掩盖主链断言。

## 9. 状态迁移与生命周期
- 修复前：
  - protective UI 在缺少新字段时会误显示未启用，导致“对冲比例”等详情被隐藏
  - protective 主链集成测试会被与用例目标无关的 leverage/pending 上限拦住
- 修复后：
  - protective UI 对旧/简化 payload 兼容
  - protective 主链测试只验证 bundle/订单事实链，不被默认风控阈值误伤

## 10. 缓存与性能
- 不新增缓存。
- 仅增加前端辅助判断函数，性能影响可忽略。

## 11. 日志、监控与审计
- 不改日志字段。
- protective 审计链保持现状。

## 12. 测试策略
- 集成测试：
  - `tests/integration/test_dashboard_ui.py`
  - `tests/integration/test_mainline_chain.py`
- 单元测试：
  - 继续保留现有 `tests/unit/test_target_position_engine.py -k protective_overlay`

## 13. 迁移、回滚与兼容性
- 无 migration。
- 如需回滚，只需回退本次 JS 与测试改动。

## 14. 配置与环境隔离
- 运行时配置不变。
- 仅测试夹具额外声明与 mock 目标一致的风控参数。

## 15. 代码组织与依赖
- 变更限定在：
  - `aats/api/static/modules/views/strategy-view.js`
  - `tests/integration/test_dashboard_ui.py`
  - `tests/integration/test_mainline_chain.py`

## 16. 文档与运维手册
- 本文档记录本轮 protective review 的修复边界与验收口径。

## 17. 部署与验收标准
- protective 策略页在简化 payload 下仍能展示“对冲比例”等详情。
- protective 策略页文案校验使用中文期望，不再依赖过时英文短语。
- protective 主链集成测试能稳定产出 `order_intent_refs` 与 bundle 事实链。
