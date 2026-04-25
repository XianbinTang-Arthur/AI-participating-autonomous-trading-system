# Task 226: AI 配置页 UX 与自动换档默认语义修正

## Business objectives and boundaries

目标是修正 AI 配置页中三个会误导 operator 的表现：顶部运行模式徽标不应把 `ai_decision_maker` 渲染成高危红色；运行模式说明弹窗必须匹配当前“AI 服务全面开启”的运行边界；自动换档面板不能把 `execution_degraded_safe` 表现成正常默认档位。范围限定在 Operator 前端展示与必要的只读状态归因，不扩大交易标的、策略族、发布或调参路径。

## Module responsibilities and domain model

`dashboard-shell.html` 负责运行模式说明弹窗的静态语义。`shell-renderer.js` 与 `app.css` 负责顶部运行模式徽标。`ai-config-view.js` 负责 AI 配置页运行模式与策略档位渲染。后端 `OperatorQueryService` / strategy profile 查询只提供当前状态，不在本任务中改变交易决策逻辑。

## Input/output interfaces

输入来自 `/ai/runtime`、`/ai-config/summary`、`/dashboard/bundle` 中的 `aiRuntime` 与 `aiConfigModel`。输出是浏览器中的中文 UI 文案、颜色与当前档位解释。接口字段保持兼容，不新增强制字段。

## Database schema / tables / indexes / constraints

本任务不改数据库 schema、索引或约束。如需要判断 active profile 来源，仅读取现有 activation 状态和 latest profile control decision。

## Transactions, Consistency, Concurrency

本任务不改变写事务。UI 展示必须在 bundle 缓存命中时仍然根据同一份 payload 得出一致结果。

## Authorization, Authentication, Data Security

不新增权限。页面仍遵守现有 session/API key 访问控制。验证时不得打印 `.env.*` 中的密码、token、API key。

## Error Handling and Idempotency

如果 profile 状态缺失，UI 显示“待确认”或解释为“暂无新切档动作”，不得臆造自动降级。按钮状态仍按当前权限与 auto-control 状态禁用。

## State Transition and Lifecycle

不改变运行模式切换、自动换档、手动切档的状态机。只修正“AI 决策者”视觉语义和自动换档当前档位的展示语义。

## Caching and Performance

变更仅影响静态渲染函数和 CSS，不新增网络请求。Bundle 缓存策略不变。

## Logging, Monitoring, Auditing

不新增日志或审计表。若后续需要追踪为什么进入 `execution_degraded_safe`，应另开后端 state-audit。

## Testing Strategy

更新 Node 渲染 smoke test，覆盖：`ai_decision_maker` 不使用危险红色语义；弹窗不再提示必须启用旧 override；自动换档开启时无真实 applied safety decision 不应把安全档位表述成普通推荐默认。

## Migration, Rollback, Compatibility

无迁移。回滚方式是还原本任务修改的静态资源和测试文件。接口兼容。

## Configuration and Environment Isolation

不修改 `.env`。当前 AI enablement 配置继续由运行环境控制：`ai_operating_mode=ai_decision_maker`、`strategy_profile_auto_control_enabled=true`。

## Code Organization and Dependencies

不新增依赖。继续使用现有前端模块、CSS class 与 integration Node smoke test。

## Documentation and Operations Manual

本 SOW 作为任务记录。若部署后 UI 仍显示 `execution_degraded_safe`，运维侧应复查真实 activation history，确认是否存在安全快速通道或管理员手动切档历史。

## Deployment and Acceptance Criteria

验收标准：顶部 badge 对 `ai_decision_maker` 使用稳态 AI 语义而非刺眼深红；运行模式弹窗说明当前 AI-enabled 边界且不再要求旧 override 才放行；AI 配置页自动换档开启时，除非 latest profile control 明确 applied 到 `execution_degraded_safe` 或安全快速通道触发，否则不把该档位当作默认合理状态展示。
