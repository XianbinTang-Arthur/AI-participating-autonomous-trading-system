# AATS 前端页面与数据流清单

> 文档状态：现行页面清单与整改后复核结果
> 核对日期：2026-08-25
> 起始代码基线：`e4954271427554aa4f56f1114827dc15b62932f1`；整改实现提交：`830e25114c7e8fd0eb4b278061336e440b786f88`
> 运行基线：本机 `derivatives` 模拟盘后端 `http://127.0.0.1:8001`；整改后前端通过临时只读预览源连接该后端复核；本文不证明实盘、交易所或资金状态
> 证据优先级：当前代码与真实浏览器渲染 > 测试 > 既有文档

## 1. 前端实现形态

AATS 当前前端不是 React/Vue 工程，而是由 FastAPI 直接提供的原生 HTML、CSS 和 ES Module 单页应用：

- 页面壳：`aats/api/static/dashboard-shell.html`；登录页：`login.html`。
- 全局编排：`app.js`，负责路由、事件委派、详情抽屉、刷新和操作反馈。
- 路由真相源：`modules/view-router.js`。
- 数据请求真相源：`modules/store.js`；所有主页面通过 `/dashboard/bundle` 分主批次与延迟批次加载。
- 请求层：`modules/api-client.js`；GET 网络错误只重试一次，主 bundle 默认 30 秒超时，延迟 bundle 45 秒超时。
- 状态层：内存态 `createState()`；页面缓存新鲜度与 30 秒自动刷新周期一致。
- 视图层：`modules/views/*.js` 返回 HTML 字符串，`shell-renderer.js` 将当前视图增量写入稳定容器。
- 实时性：当前没有浏览器 WebSocket 客户端；运行态通过 30 秒轮询和人工刷新更新。

## 2. 公共数据与刷新协议

除风险页有意排除 `mode`、`runtime` 外，每个受保护页面的 bundle 都以以下公共面板为基础：

| 前端键 | HTTP 真相源 | 全局用途 |
|---|---|---|
| `session` | `/auth/session` | 当前身份、角色、认证来源 |
| `authProviders` | `/auth/providers` | 会话/API Key/数据库用户能力 |
| `health` | `/system/health` | 系统健康、暂停与 shadow 摘要 |
| `mode` | `/system/mode` | 运行模式 |
| `runtime` | `/system/runtime` | 运行环境与执行线路 |
| `systemRecovery` | `/system/recovery` | 恢复资格、只减仓、人工复核 |
| `blockerControl` | `/system/blocker-control` | 阻断项、建议动作与操作权限 |
| `aiRuntime` | `/ai/runtime` | 顶栏 AI 运行模式和 AI 服务状态 |

刷新状态机为 `idle -> primary -> deferred -> idle`。主批次决定首屏是否可用；延迟批次不能锁住全页操作。切换页面、人工刷新和后续刷新均通过 generation/AbortController 防止旧响应覆盖新页面。

## 3. 路由清单

| 路由 | 页面组件 | 视图键 | 主要任务 | 主要数据源 | 复核状态 |
|---|---|---|---|---|---|
| `/login` | `login.html` + `login.js` | 独立页 | 运维账号登录 | `/auth/providers`、`POST /auth/login` | 已做源码、浏览器登录链复核；密码由用户手工输入 |
| `/ui` | `home-view.js` | `home` | 值班首屏、交易资格、人工控制 | blockers、metrics、portfolio、latestDecision、executionLatest、reconciliationLatest、accountState | 已完成 |
| `/ui/overview` | `overview-view.js` | `overview` | 交易链、资产、持仓和近期事件总览 | home 面板 + positions、strategyRuntime | 已完成 |
| `/ui/strategy` | `strategy-view.js` | `strategy` | 策略结论、候选机会、质量与历史归因 | strategyRuntime、strategyAttribution、positionLifecycleAttribution、decision、trial review | 已完成 |
| `/ui/execution` | `execution-view.js` | `execution` | 委托、成交、异常、卡单与生命周期 | decision、metrics、orders、fills、lifecycle attribution、execution errors | 已完成；当前样本没有活跃委托/成交 |
| `/ui/risk` | `risk-view.js` | `risk` | 阻断、恢复、账户、合约风险、对账、退出任务 | metrics、portfolio、positions、accountState、reconciliation、trial guard、replay、exit history | 已完成 |
| `/ui/exit-execution` | `exit-execution-view.js` | `exitExecution` | parent-exit 独立历史工作台 | exitExecutionActionHistoryPage | 已完成；当前样本为空 |
| `/ui/replay` | `replay-view.js` | `replay` | 回放父腿、历史验证与腿级对账 | replayStatus、replayRecentValidations、reconciliationLatest | 已完成；当前父腿样本为空 |
| `/ui/ai-analysis` | `ai-analysis-view.js` + `ai-view.js` | `aiAnalysis` | AI 状态、决策解释、shadow、长期表现和档位证据 | aiOverview、aiRuntime、aiLatest、shadow、profileControlSummary、aiRecent | 已完成 |
| `/ui/ai-config` | `ai-config-view.js` | `aiConfig` | AI 运行模式与六档策略控制 | aiConfigModel、aiRuntime | 已完成 |
| `/ui/rdp` | `rdp-view.js` + `rdp-control-panel.js` | `rdp` | RDP 完整性、recommendation、发布和调优治理 | rdpControl、workbench、alerts、tuning | 已完成 |
| `/ui/settings` | `admin-view.js` | `admin` | 运维账号、角色、启停和密码管理 | operatorUsers | 已完成；写操作仅做取消/测试验证 |
| `/ui/ai` | `view-router.js` 别名 | `aiAnalysis` | 兼容旧 AI 路径 | 同 `/ui/ai-analysis` | 已确认映射 |

## 4. 页面尺寸：整改前与整改后

下表是 bundle 明确到达“已同步”、无骨架、无 `aria-busy` 后的文档高度；数值受模拟盘数据量影响，只用于比较布局密度。

### 4.1 整改前基线

| 页面 | 1920px | 1440px | 1280px | 1024px | 768px | 390px |
|---|---:|---:|---:|---:|---:|---:|
| 主页 | 1,287 | 1,269 | 2,173 | 2,193 | 3,186 | 3,283 |
| 交易总览 | 2,909 | 2,926 | 3,716 | 3,727 | 5,857 | 5,904 |
| 策略判断 | 8,841 | 8,938 | 9,466 | 10,075 | 12,695 | 22,976 |
| 委托与成交 | 1,613 | 1,613 | 1,685 | 1,713 | 2,533 | 2,915 |
| 风险与恢复 | 3,867 | 3,950 | 5,218 | 5,356 | 9,673 | 9,891 |
| 退出任务 | 1,080 | 934 | 1,029 | 1,120 | 1,423 | 1,867 |
| 回放与复盘 | 1,080 | 961 | 1,013 | 1,027 | 1,609 | 1,853 |
| AI 分析 | 5,904 | 5,939 | 7,353 | 7,468 | 7,413 | 12,461 |
| AI 配置 | 1,873 | 1,921 | 2,466 | 2,544 | 3,839 | 4,002 |
| RDP 治理 | 2,883* | 900* | 4,496 | 4,483 | 5,566 | 6,519 |
| 账户与权限 | 1,435 | 1,477 | 2,541 | 2,605 | 3,045 | 3,322 |

\* RDP 运行依赖在采集中由“尚无 workbench 数据”转为“已返回 workbench 数据”，两次高度不能直接横向比较；这也证明文档不能把一次截图当作 RDP 永久状态。

### 4.2 整改后复核

整改后高度来自同一模拟后端的临时只读前端预览。运行数据会持续变化，因此高度只用于验证布局趋势，不是像素级快照合同。

| 页面 | 1920px | 1280px | 1024px | 768px | 390px |
|---|---:|---:|---:|---:|---:|
| 主页 | 1,287 | 1,365 | 2,000 | 3,134 | 3,033 |
| 交易总览 | 2,926 | 3,065 | 3,692 | 5,771 | 5,567 |
| 策略判断 | 8,859 | 9,233 | 9,987 | 12,941 | 22,198 |
| 委托与成交 | 1,613 | 1,633 | 1,661 | 2,481 | 2,665 |
| 风险与恢复 | 3,850 | 4,234 | 5,304 | 9,621 | 9,465 |
| 退出任务 | 1,080 | 898 | 1,068 | 1,161 | 1,198 |
| 回放与复盘 | 1,080 | 961 | 975 | 1,557 | 1,603 |
| AI 分析 | 4,211 | 6,370 | 7,375 | 10,046 | 12,213 |
| AI 配置 | 1,873 | 2,067 | 2,492 | 3,787 | 3,752 |
| RDP 治理 | 2,480 | 3,101 | 4,431 | 5,514 | 6,186 |
| 账户与权限 | 1,435 | 1,477 | 2,553 | 2,993 | 3,072 |

1280px 主页由整改前约 2,173px 降为约 1,365px，证明通用栅格不再过早单列化。390px 主导航从约 292px/六行降为 42px/单行，策略和风险的分区导航降为 36px 横向导航。

## 5. 响应式与可访问性终检

- 1920、1440、1280、1024、768、390 六组尺寸的 11 个受保护路由共 66 个组合全部到达“已同步”。
- 66/66 没有文档级横向溢出、控件文字裁切、未清理骨架或可见错误面板。
- 主导航 66/66 保持单行，活动路由 66/66 位于可视区域；1280px 及以上不需要横向滚动，1024px 及以下允许横向滚动但隐藏装饰性滚动条。
- 表格在窄屏切换为记录卡，不要求用户横向拖动数据表。
- 主按钮默认 42px；自动化裁切检查为 0。原生 checkbox 的视觉控件小于 36px，但其关联 label 是完整点击目标，不作为小触点缺陷。
- 原生 `<dialog>` 已提供 Escape、backdrop、焦点回收和 reduced-motion 契约；真实读屏器仍属未验证边界。
- 新增跳过导航链接、单一全局 H1、命名主内容区、`aria-current=page` 与深层直达活动标签可视性。

## 6. 截图证据与敏感信息边界

浏览器审计对整改前页面执行了全路由、多宽度渲染，并在整改后对 1280px 与 390px 的 11 个路由逐页触发截图检查；其中主页、策略、风险、AI 分析、RDP、账户权限等关键首屏又做了人工目视复核。PNG 不提交也不持久化到仓库，`screenshots/` 仅保留目录占位和忽略规则，因为图像可能包含模拟账户金额、决策编号与运维用户名。审计文档只记录布局和字段形状，不复制密码、cookie、API Key 或环境文件内容。
