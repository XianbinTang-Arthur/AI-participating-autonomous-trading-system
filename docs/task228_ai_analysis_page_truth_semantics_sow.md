# Task 228: AI 分析页 truth semantics bounded 修复

## Business Objectives And Boundaries

目标是修复 AI 分析页逐元素语义失真：页面必须区分 AI 服务状态、AI 评估结果、AI 是否被最终采纳、基础策略最终路径、策略层 shadow 对照和策略档位控制证据。

本任务只改 dashboard 展示层和前端测试，不修改交易决策、AI provider 调用、风险门、执行门、数据库 schema 或部署流程。

## Module Responsibilities And Domain Model

- `aats/api/static/modules/views/ai-analysis-view.js`：AI 分析页组合入口，负责档位控制证据卡片。
- `aats/api/static/modules/views/ai-view.js`：AI 分析页共享渲染，负责运行概览、决策链、经济门槛、执行建议、表现报告、AI 历史。
- `aats/api/static/modules/terms.js`：统一枚举中文展示。
- `tests/integration/test_dashboard_ui.py`：Node 渲染级 UI 语义断言。

核心语义：

- `effective_operating_mode=ai_decision_maker` 只表示 AI 可以参与最终决策链路。
- `decision_source=baseline` 表示本轮最终仍沿用基础策略路径，不等于 AI 服务关闭。
- `fallback_used` 是 AI assessment provider/output fallback，不等于最终交易决策 fallback。
- `economically_actionable=false` 是 AI 未进入最终采纳链的重要原因。
- `strategy_profile.latest_selection_decision` 是最终档位门控事实；`latest_optimization_report` 是优化建议，不应覆盖门控结果。

## Per-Element Diagnosis

### AI 状态概览

- 当前运行模式：应显示有效模式与配置是否一致；当前文案在 effective 等于 configured 时仍强调“手动切到”，会夸大人工覆盖。
- 最近一轮真实决策结果：当前显示 raw `baseline`，应显示“基础策略路径”。
- 模型服务状态：当前可能显示 `not_loaded`，但同一 payload 里 `provider_ready=true`，属于 gateway stub 与 authoritative runtime 混用导致的展示矛盾；UI 应优先用 authoritative runtime。
- 人工复核状态：同上，不能在无人工复核时显示 `not_loaded`。
- 运行模式说明 callout：当前把 `manual_selection` 直接写成管理员手动切换；当 effective 与 configured 一致时应说明“与配置一致”。
- 连续失败 / 成功：可保留，但应继续作为统计，不解释为本轮结论。
- 近期回退到基础策略比率：应改为“AI 评估回退比率”，避免被误解为最终决策回退。
- 最近一次状态变化：只可作为历史事件，不应暗示当前仍阻断。
- 策略层 shadow 优于基础策略：应带样本口径；无样本不得给 0 当结论。
- 失败预算 / 结果预算：当前把缺失值渲染为 0，会制造假告警；缺失应显示待同步。

### 决策链概览

- 基础策略参考：可保留，但应明确是 baseline reference，不是最终交易理由全貌。
- AI 决策意图：当前无 intent 时显示“待确认 / 或 AI 已回退”，这不是事实；应显示“本轮未生成 AI 交易意图”，并说明 AI assessment 存在但经济门槛不足或方向为空。
- 最终决策结果：当前 meta 混用 raw `baseline`，应显示“基础策略路径 / final_decision 只是权限口径”。
- 策略档位控制：应显示最终门控结果，不把优化建议当实际候选。
- 判断时间：保留。
- 最终决策来源：raw `baseline` 应本地化。
- AI 未被采用的主要原因：当前只看 `decision_blocked_reasons` 和 `rejection_flags`，漏掉 `validation_flags=low_edge`；应纳入经济门槛原因。
- 最新策略层 shadow 动作：保留，但必须避免暗示直接实盘改动。
- 经济性概览：应补充模型、prompt、输出有效性和 provider latency；这直接影响 DeepSeek 决策时效。

### 档位控制证据

- 当前策略档位：保留。
- 冷启动观察期：保留。
- 安全档触发：保留，但只说明 safety event 允许安全档，不等于已经切档。
- 候选策略档位：当前优先显示 `latest_optimization_report.recommended_profile_id`，会把优化建议误当实际 gate 候选；应优先 `latest_selection_decision.candidate_profile_id`。
- 切换分类：保留。
- 紧急安全切档：应改成“快速通道”，明确 applied 与 eligible。
- 自动切档阻断原因：当前无限铺开，信息噪声过高；应摘要并保留数量。
- 自动切档闸门：应显式包含 reconciliation 不干净、replay 不足、冷却等事实。
- 快速通道依据：保留。
- 风险预算 / 执行侵略性自适应：保留。
- 最近一次切档时间：保留。
- 候选档位摘要：应跟门控候选一致；优化建议只作为补充说明。

### 执行层建议

- 仅在有真实建议、翻译结果、裁剪或拒绝原因时显示。
- 若 `enabled_live` 但本轮无建议，不应展示空卡误导为执行链缺失。
- 若出现预演，应明确“预览/受限翻译”不等于裸下单。

### 表现报告

- 已持久化报告：保留。
- 平均短窗 / 中窗净收益差：保留，但必须来自真实 windows。
- 回放健康率：当前 `validation_count=0` 时显示 `0`，会误读为不健康；应显示“暂无回放验证”。
- 文案中的“AI 影子”应统一为“策略层 shadow”。

### AI 记录

- 窗口净收益差：当前读取 `performance_view.windows`，但后端真实字段在 `aiOverview.performance_windows` 或 latest report；导致有 44 条 evaluation 仍显示“暂未形成结论”。应修正数据源。
- history callout：当前“最近 10 个窗口里，短窗净收益差”口径混乱；应改成“最近可用评估窗口”。
- AI 判断记录表：`fallback_used` 是 provider/output fallback，不是“最终回退到基础策略”；应改为“使用回退评估 / 模型输出有效”。
- 策略层 shadow 动作表：不得写“会不会真的改动”，shadow 只观察；应写“是否形成改写建议”。
- shadow 收益表：统一“策略层 shadow 结果”，避免“AI 影子”。
- 空表：保留空状态，但不把缺失数据当 0 结论。

## Input/Output Interfaces

输入：dashboard bundle panels `aiOverview`, `aiRuntime`, `aiLatest`, `profileControlSummary`, `aiRecent`, `aiShadowRecent`, `aiShadowEvaluations`。

输出：AI 分析页 HTML。无 API 合约变更。

## Database Schema / Tables / Indexes / Constraints

不改数据库。

## Transactions, Consistency, Concurrency

不改事务或并发路径。前端渲染只消费当前 bundle 快照。

## Authorization, Authentication, Data Security

不改鉴权。调试时可在进程内读取 `.env.derivatives.live` 连接或登录信息，但不得打印凭证。

## Error Handling And Idempotency

缺失字段必须显示“待同步 / 暂无样本 / 未生成”，不得把缺失当 0 或健康/故障结论。

## State Transition And Lifecycle

本修复不改变 AI lifecycle，只修正以下展示边界：

- AI enabled != AI 已被最终采纳。
- provider fallback != 最终决策 fallback。
- optimization recommendation != profile gate selection。
- shadow comparison != live action。

## Caching And Performance

不改 bundle 缓存。新增判断均为本地纯函数，复杂度为常数或线性于已渲染列表。

## Logging, Monitoring, Auditing

不新增日志。页面补充 provider latency、model/prompt/output 状态，提升人工审计能力。

## Testing Strategy

新增/更新 Node render integration tests，覆盖：

- AI 分析页不再显示 raw `baseline` / `not_loaded` 矛盾。
- no intent 时显示事实原因而非“待确认 / 已回退”。
- low_edge 出现在 AI 未进入最终路径原因。
- profile candidate 使用 selection candidate，不被 optimization recommendation 覆盖。
- replay validation 为 0 时不显示健康率 0。
- 历史表不再把 assessment fallback 写成最终决策 fallback。

## Migration, Rollback, Compatibility

无迁移。回滚方式：恢复本任务修改的 JS 与测试文件。

## Configuration And Environment Isolation

不改 `.env`、YAML 或 AI provider 配置。

## Code Organization And Dependencies

不新增依赖。保留现有模块结构和组件 API。

## Documentation And Operations Manual

本文档作为 bounded task 记录。无需更新操作手册。

## Deployment And Acceptance Criteria

验收标准：

- AI 分析页逐元素语义与当前 bundle 事实一致。
- 页面不再把缺失值渲染为 0 结论。
- 页面显示 DeepSeek 模型与 latency。
- 前端集成测试通过。
- `ruff`, unit tests, narrow integration test, `node --check`, `git diff --check` 完成并报告。
