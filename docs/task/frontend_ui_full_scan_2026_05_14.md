# Frontend UI 全量元素扫描报告（2026-05-14）

> 修复状态：本报告问题清单已由 `docs/frontend_ui_contract_fix_sow_2026_05_14.md` 对应修复落地。报告正文保留扫描当时的发现，便于追踪问题来源。

## 范围与方法

本报告只读扫描 `aats/api/static` 前端工作区、`aats/api/ui.py` UI 路由、dashboard bundle 数据源和与 UI 动作直接关联的 API 路由。扫描目标是逐页确认：

- 页面是否真实可路由。
- 每个页面的主要展示元素来自静态壳层、渲染模块、dashboard bundle panel 还是后端动态 action descriptor。
- 按钮、表单、输入框、链接是否有明确动作来源。
- `data-action` 是否有前端 handler。
- 前端展示语义是否与后端权限、接口或 fail-closed 语义对齐。
- 是否存在无效展示、语义不清、按钮功能异常、错误兜底或旧文案。

使用的证据源：

- 静态壳层：`aats/api/static/dashboard-shell.html`
- 登录页：`aats/api/static/login.html`、`aats/api/static/login.js`
- 页面注册：`aats/api/static/modules/view-router.js`
- bundle 数据源：`aats/api/static/modules/store.js`
- 页面渲染：`aats/api/static/modules/views/*.js`
- 动作分发：`aats/api/static/app.js`、`aats/api/static/modules/actions/*.js`
- 共享组件：`components.js`、`reconciliation-controls.js`、`exit-execution-helpers.js`、`shadow-drawer.js`
- 后端路由：`aats/api/ui.py`、`aats/api/auth_routes.py`、`aats/api/routes/*.py`、`aats/api/rdp_routes.py`

渲染烟测使用高信息量 mock 数据对 shell、login 和 11 个 dashboard 页面执行 render 抽取，检查 button/link/form/input/select/heading、`data-action`、空 action、乱码与 `undefined` / `NaN` / `[object Object]` / `Invalid Date`。

## 全局页面入口

| 页面 | 前端 view | 路由 | 页面标题来源 | 内容渲染入口 |
| --- | --- | --- | --- | --- |
| 主页 | `home` | `/ui` | `VIEW_META.home` | `renderHomeView` |
| 交易总览 | `overview` | `/ui/overview` | `VIEW_META.overview` | `renderOverviewView` |
| 策略判断 | `strategy` | `/ui/strategy` | `VIEW_META.strategy` | `renderStrategyView` / `renderStrategySections` |
| 委托与成交 | `execution` | `/ui/execution` | `VIEW_META.execution` | `renderExecutionView` / `renderExecutionSections` |
| 风险与恢复 | `risk` | `/ui/risk` | `VIEW_META.risk` | `renderRiskView` / `renderRiskSections` |
| 退出任务工作台 | `exitExecution` | `/ui/exit-execution` | `VIEW_META.exitExecution` | `renderExitExecutionView` |
| 回放与复盘 | `replay` | `/ui/replay` | `VIEW_META.replay` | `renderReplayView` / `renderReplaySections` |
| AI 分析 | `aiAnalysis` | `/ui/ai-analysis` | `VIEW_META.aiAnalysis` | `renderAIAnalysisView` |
| AI 配置 | `aiConfig` | `/ui/ai-config` | `VIEW_META.aiConfig` | `renderAIConfigView` |
| RDP 治理 | `rdp` | `/ui/rdp` | `VIEW_META.rdp` | `renderRdpView` |
| 账户与权限 | `admin` | `/ui/settings` | `VIEW_META.admin` | `renderAdminView` |

补充入口：

- `/ui/home` 由 `ui.py` 返回 shell，但 `VIEW_ROUTES` 主路由是 `/ui`。
- `/ui/ai` 是旧别名，`VIEW_ROUTE_ALIASES` 指向 `aiAnalysis`。
- `/login` 独立返回 `login.html`。

## 全局壳层元素

来源：`dashboard-shell.html` + `app.js` + `shell-renderer.js`。

| 元素 | 类型 | 展示/动作来源 | 结论 |
| --- | --- | --- | --- |
| 左侧导航 11 个链接 | 静态 `<a data-view>` | `dashboard-shell.html` + `setActiveView` 拦截点击 | 路由与 `VIEW_ROUTES` 对齐 |
| 顶栏运行模式 badge | `button data-action=show-runtime-mode-info` | `app.js` local dispatch | 已注册 |
| 运行模式弹窗关闭 | `button data-action=close-runtime-mode-info` | `app.js` local dispatch | 已注册 |
| 退出登录 | `#logoutButton` | DOM id 直绑 `logoutOperator()` | 有 handler，不是空 action 问题 |
| 立即刷新 | `#refreshButton` | DOM id 直绑 `refreshDashboard({manual:true})` | 有 handler |
| 恢复运行 | `#resumeButton` | DOM id 直绑 `dispatchAction("trigger-resume")` | 功能存在，文案建议统一为“恢复自动运行” |
| 暂停运行 | `#haltButton` | DOM id 直绑 `dispatchAction("trigger-halt")` | 功能存在，文案建议统一为“暂停自动运行” |
| 自动刷新开关 | `#autoRefreshToggle` | DOM id change 事件 | 有 handler |
| 明细抽屉关闭 | `#closeDrawerButton` | DOM id 直绑 `closeDrawer()` | 有 handler |

## 登录页元素

来源：`login.html` + `login.js`。

| 元素 | 类型 | 展示/动作来源 | 结论 |
| --- | --- | --- | --- |
| 登录标题、说明文案 | 静态 HTML | `login.html` | 中文 UTF-8 正常 |
| 用户名输入框 | `#loginUsername` | `login.js` submit 读取 | `required` 已设置 |
| 密码输入框 | `#loginPassword` | `login.js` submit 读取 | `type=password`、`required` 已设置 |
| 登录按钮 | submit button | `loginForm.submit` | 有 handler，不是空 action 问题 |
| 错误/提示区域 | `#loginMessage` | `login.js` | 会展示本地化错误 |

## 主页 `/ui`

数据源：

- core panels: `session`、`authProviders`、`health`、`mode`、`runtime`、`systemRecovery`、`blockerControl`、`aiRuntime`
- view panels: `/system/blockers`、`/system/metrics`、`/portfolio/latest`、`/decision/latest`、`/execution/latest`、`/reconciliation/latest`、`/account/state`
- deferred: `latestDecision`、`executionLatest`、`reconciliationLatest`

主要展示：

- 首要问题、操作概览、账户概览、最新动作、次级提醒。
- 页面内无独立按钮；控制按钮来自全局壳层。

结论：

- 渲染 smoke 未发现乱码、`undefined`、`NaN`、`Invalid Date`。
- deferred panel 与 `store.js` 一致。

## 交易总览 `/ui/overview`

数据源：

- core panels
- `/system/blockers`、`/system/metrics`、`/portfolio/latest`、`/positions`、`/strategy/runtime`、`/decision/latest`、`/execution/latest`、`/reconciliation/latest`、`/account/state`
- deferred: `latestDecision`、`executionLatest`、`reconciliationLatest`

按钮：

| 文案 | action | 来源 | 目标 |
| --- | --- | --- | --- |
| 策略 | `navigate-view` | `overview-view.js` | `strategy` |
| 执行 | `navigate-view` | `overview-view.js` | `execution` |
| 风控 | `navigate-view` | `overview-view.js` | `risk` |
| AI分析 | `navigate-view` | `overview-view.js` | `aiAnalysis` |
| 查看决策链 | `inspect-decision` | `overview-view.js` | `/decision/{id}` drawer |

问题：

- `AI分析` 与导航/页面标题 `AI 分析` 不一致，建议统一。

## 策略判断 `/ui/strategy`

数据源：

- core panels
- `/strategy/runtime`
- `/reports/strategy-attribution?limit=100`
- `/reports/position-lifecycle-attribution?limit=6`
- `/decision/latest`
- `/decision/recent?limit={recentDecisions}&offset=0`
- `/execution/latest`
- `/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4`
- `/reports/trial-review-history?limit=5&offset=0`
- deferred: `strategyAttribution`、`positionLifecycleAttribution`、`trialReviewSummary`

页面链接：

- `#strategy-overview` 本轮结论
- `#strategy-opportunities` 当前机会
- `#strategy-health` 运行质量
- `#strategy-history` 历史归因

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 查看完整决策链 | `inspect-decision` | latest decision | `/decision/{id}` drawer |
| 查看完整历史 | `inspect-decision-history` | 决策历史卡 | `/decision/recent?limit=50&offset=0` drawer |
| 查看风险与恢复 | `navigate-view` | 试盘复盘兜底动作 | `risk` |
| 查看委托与成交 | `navigate-view` | hard-stop 分支 | `execution` |
| 记录本次复盘 | `record-trial-review` | 试盘复盘动作 | `/system/trial-review/record` |
| 查看复盘明细 | `inspect-trial-review-details` | 试盘复盘动作 | `/reports/trial-review-details?...` drawer |
| 查看完整归因 | `inspect-strategy-attribution` | 策略归因卡 | `/reports/strategy-attribution?limit=200` drawer |
| 查看诊断 | `inspect-lifecycle-attribution` | 生命周期归因 | `/reports/position-lifecycle-attribution/{id}` drawer |
| 提交放量评审 | `record-scaling-review` | 放量建议分支 | `/system/scaling-review` |
| 记为继续小资金试盘 | `record-scaling-review` | 放量建议分支 | `/system/scaling-review` |
| 记为缩小试盘规模 | `record-scaling-review` | 放量建议分支 | `/system/scaling-review` |
| 记为暂停试盘并复盘 | `record-scaling-review` | 放量建议分支 | `/system/scaling-review` |
| 后端 workbench action label | dynamic `client_action` | `renderWorkbenchActionButton` | 当前缺失时回退 `refresh-dashboard` |

问题：

- `renderWorkbenchActionButton` 对缺失/未知 `client_action` 回退到 `refresh-dashboard`，可能导致按钮文案与实际动作错位。

## 委托与成交 `/ui/execution`

数据源：

- core panels
- `/decision/latest`
- `/system/metrics`
- `/execution/latest`
- `/orders/recent?limit={recentOrders}&offset=0`
- `/fills/recent?limit={recentFills}&offset=0`
- `/reports/position-lifecycle-attribution?limit=8`
- `/execution/errors`
- deferred: `positionLifecycleAttribution`

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 查看最新委托 / 查看历史委托 | `inspect-order` | latest order | `/orders/{client_order_id}` drawer |
| 查看详情 | `inspect-order` | 委托列表 | `/orders/{client_order_id}` drawer |
| 处理卡单 | `resolve-stuck-order` | stuck order 分支 | `/orders/{id}/resolve-stuck-submission` |
| 查看详情 | `inspect-fill` | 成交列表 | `/fills/{fill_id}` drawer |
| 查看诊断 | `inspect-lifecycle-attribution` | 生命周期归因 | `/reports/position-lifecycle-attribution/{id}` drawer |
| 加载更多/收起委托 | `load-more-orders` / `collapse-orders` | pagination footer | 调整 bundle limit |
| 加载更多/收起成交 | `load-more-fills` / `collapse-fills` | pagination footer | 调整 bundle limit |

结论：

- 已注册 action 与 handler 对齐。
- 卡单处理有二次确认和 claimed submit 二阶段确认。

## 风险与恢复 `/ui/risk`

数据源：

- core panels，但排除 `mode`、`runtime`
- `/system/metrics`
- `/portfolio/latest`
- `/positions`
- `/account/state`
- `/reconciliation/latest`
- `/system/trial-guard`
- `/system/guarded-live-preflight`
- `/reports/guarded-live-run-packet`
- `/replay/status`
- `/system/exit-execution/action-history?...`
- deferred: `trialGuard`、`guardedLivePreflight`、`guardedLiveRunPacket`、`replayStatus`、`exitExecutionActionHistoryPage`

页面链接：

- `#risk-overview` 当前任务
- `#risk-recovery` 恢复条件
- `#risk-review` 阻断与复盘
- `#risk-exit-workspace` 退出任务工作区
- `#risk-diagnostics` 辅助诊断

按钮/输入：

| 文案 | action/输入 | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 恢复自动运行 | `trigger-resume` | recovery controls | `/system/resume` |
| 查看最新对账 / 查看对账 | `inspect-reconciliation` | reconciliation controls | `/reconciliation/{id}` drawer |
| 重新对账（刷新交易所状态） | `trigger-reconciliation-validate` | reconciliation controls | `/reconciliation/validate` |
| 接受当前状态为新基线 | `trigger-rebaseline` | reconciliation controls | `/system/rebaseline`，120s timeout |
| 查看影子详情 | `inspect-shadow` | shadow blocker | `/system/shadow` + `/system/shadow/history` |
| 进入独立工作台 | `navigate-view` | exit history summary | `exitExecution` |
| 查看回放工作区 | `navigate-view` | replay summary | `replay` |
| action/parent/actor/windowHours 筛选 | `data-exit-history-filter` | exit history helper | 改写 bundle URL 参数 |
| 应用筛选 | `apply-exit-execution-history-workspace` | exit history helper | refresh `exitExecutionActionHistoryPage` |
| 重置筛选 | `reset-exit-execution-history-workspace` | exit history helper | 清空筛选 |
| 上一页 / 下一页 | `paginate-exit-execution-history` | exit history helper | 改 offset |
| 后端 blocker client action label | dynamic `client_action` | `renderBlockerActions` | 当前缺失时回退 `refresh-dashboard` |
| 后端 blocker API action label | `trigger-blocker-action` | `renderBlockerActions` | `/system/blocker-actions/{action_id}` |

问题：

- `renderBlockerActions` 对缺失/未知 `client_action` 回退到 `refresh-dashboard`，会掩盖后端 action 拼写错误。

## 退出任务工作台 `/ui/exit-execution`

数据源：

- core panels
- `/system/exit-execution/action-history?...`
- deferred: `exitExecutionActionHistoryPage`

按钮/输入：

| 文案 | action/输入 | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 返回风险页 | `navigate-view` | `exit-execution-view.js` | `risk` |
| action/parent/actor/windowHours 筛选 | `data-exit-history-filter` | shared helper | 改写 bundle URL 参数 |
| 应用筛选 | `apply-exit-execution-history-workspace` | shared helper | refresh action history |
| 重置筛选 | `reset-exit-execution-history-workspace` | shared helper | 清空筛选 |
| 上一页 / 下一页 | `paginate-exit-execution-history` | shared helper | 改 offset |

结论：

- 与 risk 使用同一个 backing panel key，`navigation-state.js` 有同步逻辑。

## 回放与复盘 `/ui/replay`

数据源：

- core panels
- `/replay/status`
- `/replay/recent-validations?limit={recentReplayValidations}&offset=0`
- `/reconciliation/latest`
- deferred: `replayStatus`、`replayRecentValidations`

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 查看风险页 | `navigate-view` | replay reconciliation bridge | `risk` |
| parent filter option label | `set-replay-parent-filter` | replay parent filter | 本地筛选 |
| 查看更多 | `load-more-replay-validations` | validation history | 调整 bundle limit |
| 收起历史 | `collapse-replay-validations` | validation history | 重置 bundle limit |

结论：

- action 已注册。

## AI 分析 `/ui/ai-analysis`

数据源：

- core panels
- `/ai/overview`
- `/ai/runtime`
- `/ai/latest`
- `/ai/shadow/latest`
- `/reports/profile-control-summary`
- `/ai/recent?limit={recentAIAssessments}&offset=0`
- `/ai/shadow/recent?limit={recentAIShadowDecisions}&offset=0`
- `/ai/shadow/evaluations?limit={recentAIShadowEvaluations}&offset=0`
- deferred: `aiRecent`、`aiShadowRecent`、`aiShadowEvaluations`、`profileControlSummary`

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 加载更多/收起 AI 评估 | `load-more-ai-assessments` / `collapse-ai-assessments` | pagination footer | 调整 bundle limit |
| 加载更多/收起 shadow 决策 | `load-more-ai-shadow-decisions` / `collapse-ai-shadow-decisions` | pagination footer | 调整 bundle limit |
| 加载更多/收起 shadow 评估 | `load-more-ai-shadow-evaluations` / `collapse-ai-shadow-evaluations` | pagination footer | 调整 bundle limit |
| AI 复核 client action label | dynamic `client_action` | `renderReviewActions` | 当前缺失时回退 `refresh-dashboard` |
| AI 复核 API action label | `trigger-blocker-action` | `renderReviewActions` | `/system/blocker-actions/{action_id}` |

问题：

- `renderReviewActions` 对缺失/未知 `client_action` 回退到 `refresh-dashboard`。

## AI 配置 `/ui/ai-config`

数据源：

- core panels
- `/ai-config/summary`
- `/ai/runtime`

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| AI mode option label | `select-ai-operating-mode` | `ai-config-view.js` | `/ai/operating-mode/select` |
| 手动切档 | `set-profile-control-mode` value=`manual` | profile control | `/strategy-profiles/pause-auto` |
| 自动切档 | `set-profile-control-mode` value=`auto` | profile control | `/strategy-profiles/restore-auto` |
| strategy profile label | `manual-activate-strategy-profile` | profile list | `/strategy-profiles/profiles/{profile_id}/activate` |

结论：

- action 已注册。
- 页面已和 RDP 治理拆分，当前不再承载 RDP 发布/审批按钮。

## RDP 治理 `/ui/rdp`

数据源：

- core panels
- `/rdp/control-summary`
- `/rdp/workbench/overview`
- `/rdp/workbench/items`
- `/rdp/workbench/alerts`
- `/rdp/tuning/overview`
- `/rdp/tuning/proposals`
- deferred: `rdpWorkbenchItems`、`rdpWorkbenchAlerts`、`rdpTuningProposals`

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 运行完整 RDP / 其他 workflow label | `rdp-trigger-workflow` | `rdpControl.overview.primary_action` | `/rdp/tasks/trigger` |
| 审批建议 | `rdp-approve-only` | recommendation actions | `/rdp/recommendations/{id}/approve` |
| 拒绝建议 | `rdp-reject-recommendation` | recommendation actions | `/rdp/recommendations/{id}/reject` |
| 运行 Gate | `rdp-run-gate` | candidate actions | `/rdp/gates/run` |
| 创建发布 | `rdp-create-release` | candidate actions | `/rdp/releases/create` |
| 运行观察 | `rdp-run-observation` | observation card | `/rdp/observations/run` |
| 执行回滚 | `rdp-rollback-parameters` | observation card | `/rdp/operator-tokens` + `/rdp/parameters/rollback` |
| 批准调参 | `rdp-approve-tuning-proposal` | tuning proposal | `/rdp/tuning/proposals/{id}/approve` |
| 拒绝调参 | `rdp-reject-tuning-proposal` | tuning proposal | `/rdp/tuning/proposals/{id}/reject` |
| dynamic action label | `action.ui_action` | `renderActionDescriptor` | 当前未知 action 仍可渲染 |

确认：

- 当前后端 `rdp_control_summary.py` 已知 `ui_action` 均在 `rdp-actions.js` 注册。

问题：

- `renderActionDescriptor` 对缺失/未知 `ui_action` 没有前端 allowlist，可能显示可点击但无 handler 的按钮。
- `release_history_status.stale=true` 时 UI 显示副本警告，但观察/回滚动作仍启用；回滚属于高风险治理动作，建议 stale 时禁用或要求二次确认。

## 账户与权限 `/ui/settings`

数据源：

- core panels
- `/auth/users`

表单：

| 元素 | 来源 | 后端/行为 |
| --- | --- | --- |
| 创建账号表单 | `form data-action=submit-create-operator` | `/auth/users` POST |
| 用户名 | `#operatorCreateUsername` | JS 校验非空 |
| 初始密码 | `#operatorCreatePassword type=password` | JS 校验非空 |
| 角色 | `#operatorCreateRole` | viewer/operator/admin |
| 启用状态 | `#operatorCreateEnabled` | true/false |
| 修改角色表单 | `#changeRoleForm` | `/auth/users/{username}` PATCH role |
| 重置密码表单 | `#resetPasswordForm` | `/auth/users/{username}` PATCH password |

按钮：

| 文案 | action | 来源 | 后端/行为 |
| --- | --- | --- | --- |
| 创建账号 | form submit | create form | `/auth/users` POST |
| 确认修改角色 | `confirm-change-user-role` | role form | PATCH role |
| 确认重置密码 | `confirm-reset-user-password` | password form | PATCH password |
| 启用/停用 | `toggle-user` | user row | PATCH enabled |
| 改角色 | `change-user-role` | user row | 预填 role form |
| 重置密码 | `reset-user-password` | user row | 预填 password form |
| 删除 | `delete-user` | user row | DELETE user |

问题：

- UI 已显示 `protected_last_admin`，但仍对该用户展示可点击的 `停用`、`改角色`、`删除`。后端会拒绝 `operator_last_admin_required`。
- 当前登录用户仍可看到自删/自停用按钮，后端会拒绝 `operator_self_delete_forbidden` / `operator_self_disable_forbidden`。
- 表单没有 HTML `required`，但 JS 会做非空校验；不是功能错误，但浏览器原生提示无法参与。

## 受保护页面认证错误视图

来源：`protected-auth-view.js`。

元素：

- 标题按 `operator_admin_access_required`、`operator_write_access_required`、`transport_blocked` 等错误分支生成。
- 说明文案会提示“切换到管理员账号”或“切换到具有写入权限的账号”。
- `/login` 链接在 `operator_admin_access_required` 与 `operator_write_access_required` 时被隐藏。

问题：

- 文案要求切换账号，但不给登录/切换入口，建议显示“切换账号”按钮。

## 动作注册与后端契约

确认已注册：

- local dispatch: `refresh-dashboard`、`navigate-view`、decision/report drawers、AI mode、profile mode、pagination、runtime modal。
- risk actions: resume/halt/rebaseline/reconcile/blocker/shadow/exit-execution/trial/scaling。
- execution actions: order/fill/lifecycle drawer、stuck order、orders/fills pagination。
- admin actions: user CRUD、role/password form。
- rdp actions: workflow、approve/reject、gate、release、observation、rollback、tuning approve/reject。

需加防线：

- 动态后端 action descriptor 应做 allowlist 校验。
- 缺失/未知 client action 不应回退为 `refresh-dashboard`。

## 问题清单

| 优先级 | 问题 | 影响页面 | 证据 |
| --- | --- | --- | --- |
| P1 | 最后管理员/当前用户仍显示后端会拒绝的危险按钮 | 账户与权限 | `admin-view.js`、`accounts.py` |
| P2 | 后端 client action 缺失时回退刷新，按钮语义可能错位 | 策略判断、风险与恢复、AI 分析、影子抽屉 | `strategy-view.js`、`risk-view.js`、`ai-view.js`、`shadow-drawer.js` |
| P2 | RDP dynamic `ui_action` 无前端 allowlist | RDP 治理 | `rdp-control-panel.js` |
| P2 | RDP release history stale 时仍允许观察/回滚 | RDP 治理 | `rdp-control-panel.js` |
| P2 | 权限不足视图提示切换账号但隐藏登录入口 | protected auth view | `protected-auth-view.js` |
| P3 | 壳层恢复/暂停文案缺少“自动” | 全局壳层 | `dashboard-shell.html` |
| P3 | `AI分析` 缺少空格 | 交易总览 | `overview-view.js` |
| P3 | 管理表单缺 HTML required | 账户与权限 | `admin-view.js` |

## 已验证

- dashboard 前端单元测试：`18 passed`
- WSL2 dashboard UI 集成测试：`94 passed`
- 11 个 dashboard 页面 + shell + login 渲染 smoke：未发现 `undefined`、`[object Object]`、`Invalid Date`、`NaN`、乱码替换符。

## Caveats

本次没有登录真实生产网关逐按钮点击，也没有读取任何凭证文件。真实权限态、实时数据态和浏览器视觉布局仍建议在修复后做一次登录态浏览器 smoke。
