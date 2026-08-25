# AATS 全前端 UI/UX 与前后端集成审计整改 SOW

> 文档状态：已完成实施；部署后复验仍是独立门禁
> 日期：2026-08-25
> 起始提交：`e4954271427554aa4f56f1114827dc15b62932f1`
> 整改实现提交：`830e25114c7e8fd0eb4b278061336e440b786f88`
> 分支：`codex/fs-002-kill-switch-p0`
> 环境边界：仅使用 `derivatives` 本地模拟栈；禁止 live profile、真实资金动作和凭证输出

## 1. 业务目标与边界

本任务对 AATS Operator 控制台执行完整的页面清单、视觉层级、信息密度、响应式、无障碍及前后端数据契约审计，并修复已确认的 P0–P3 前端缺陷。目标是让交易、风险、恢复、执行、研究和权限状态更易扫描、更少歧义，并确保“无数据”“不可用”“加载中”“失败”“陈旧”不会被错误混同。

范围包括 `/login`、11 个 `/ui` History 路由、共享 shell/组件/弹层、bundle 拉取、页面动作及其后端响应映射。范围不包括交易算法、量化策略、资金参数、订单状态机、数据库模型重构、live profile 放行或真实交易操作。只有当现有后端缺少正确展示所必需且风险有限的字段时，才允许做最小后端补充，并必须记录理由和兼容性。

## 2. 模块职责与领域模型

- `dashboard-shell.html`：全局导航、运行模式标识、banner、登录会话、人工控制、11 个视图挂载点及详情抽屉。
- `app.js`：状态编排、路由切换、刷新、动作分发、视图渲染与弹层生命周期。
- `modules/store.js`：视图到 panel/endpoint 的请求计划、分页、刷新状态和缓存失效范围。
- `modules/dashboard-refresh.js`、`api-client.js`：两阶段 bundle、超时、中止、重试、错误与 stale-while-revalidate 行为。
- `modules/components.js`、`shell-renderer.js`、`detail-drawers.js`：共享卡片、状态、表格、banner、抽屉和 shell 组件。
- `modules/views/*.js`：主页、交易总览、策略、执行、风险、退出任务、回放、AI 分析、AI 配置、RDP、账户权限的领域展示。
- `app.css`：全局 token、12 栏布局、组件、响应式和 reduced-motion 规则。
- `auth_routes.py::/dashboard/bundle` 与 `routes.py`/RDP/auth routes：前端只读数据和受控动作的后端真源。

核心 UI 状态包括运行环境、trading mode、Kill Switch/recovery、账户与组合、策略判断、委托成交、风险/对账、AI/RDP 治理、认证与授权。任何派生文案必须保留后端状态的失败关闭语义。

## 3. 输入与输出接口

输入：当前 Git 代码、11 个视图路由、登录页、`/dashboard/bundle` panel 响应、受控 mutation endpoint、模拟栈运行数据、浏览器 DOM/网络/控制台/截图。

输出：

- `audit/frontend_ui_2026_08_25/frontend-page-inventory.md`；
- `audit/frontend_ui_2026_08_25/frontend-ui-audit.md`；
- `audit/frontend_ui_2026_08_25/22-frontend-ui-audit.md`；
- `audit/frontend_ui_2026_08_25/23-frontend-backend-contract-audit.md`；
- `audit/frontend_ui_2026_08_25/24-ui-remediation-plan.md`；
- `audit/frontend_ui_2026_08_25/25-ui-remediation-report.md`；
- 对应最小代码、样式和测试变更。

公共路由、endpoint 路径和已有 JSON 字段默认保持兼容；不得静默改变操作语义。

## 4. 数据库、表、索引与约束

预期不修改数据库 schema、表、索引或约束。若审计发现前端正确性必须依赖新字段，应优先从现有查询结果派生；确需持久化变更时停止实施并单独设计、评审和迁移，不在本 SOW 内隐式完成。

## 5. 事务、一致性与并发

前端继续使用 refresh generation、AbortController、panel ownership 和 action-in-flight 门禁防止旧响应覆盖新状态。视觉整改不得破坏：

- mutation 完成后再刷新对应 panel；
- 过期 bundle 不写入当前视图；
- 危险操作在状态刷新期间不可基于旧数据重复提交；
- 分页、筛选和视图缓存失效范围与请求 URL 一致。

## 6. 授权、认证与数据安全

所有受保护视图继续依赖 Operator session；viewer/operator/admin 权限不得因 UI 调整扩大。密码仅通过浏览器密码控件和既有 `/auth/login` 流转，不写入截图、日志、文档或测试 fixture。不得读取或展示 `.env.*`、Cookie、API key、数据库 URL 或交易所凭证。

危险操作必须保留权限校验、明确后果、确认语义和不可用原因。`LIVE` 与模拟环境必须在颜色之外以文字明确区分。

## 7. 错误处理与幂等

页面必须区分 loading、empty、unavailable、error、stale、authorization blocked 和 unsupported。重复刷新、快速切换路由和重复打开/关闭 dialog 不得抛异常或重复 mutation。已有错误码本地化保持兼容；不能把请求失败渲染成“无数据”。

## 8. 状态迁移与生命周期

页面生命周期为 route resolve → shell/loading → primary bundle → ready view → deferred panels → auto refresh。动作生命周期为 idle → confirmation/input → in-flight → success/error → targeted refresh。整改不得把 HALTED、HALTING、degraded、unknown 或 stale 状态提升为健康/可交易。

## 9. 缓存与性能

保留 30 秒视图 freshness、primary/deferred bundle、bundle 缓存与并发预算。重点检查重复请求、无效渲染、巨型 DOM/table、固定高度和不必要的全页重绘；只处理可观测或操作上有意义的问题，不做无依据微优化。

## 10. 日志、监控与审计

检查浏览器 console error/warning、失败请求、超时和未处理 Promise。mutation 的后端审计契约保持不变。审计文档只记录 route、字段、状态、视口、HTTP 状态和非敏感证据，不记录账户秘密或交易凭证。

## 11. 测试策略

- 静态：ES module/Node 语法、HTML 契约、Ruff、YAML/路由与 bundle mapping 测试；
- 单元/集成：现有 dashboard UI、operator API、mapping、action、auth 与响应式契约；
- 浏览器：全部可达路由至少在 1920、1440、1280、1024、768 宽度抽查，重要改动页全覆盖；
- 无障碍：语义 landmark、键盘操作、焦点、dialog、label、非颜色状态、对比度和 200% zoom；
- 运行态：控制台、网络请求、加载/空/错/陈旧态与关键 trading-safety 信息。

## 12. 迁移、回滚与兼容

前端资源由现有 Docker 镜像部署，不引入新构建链。回滚以本任务文件级 Git diff 为单位；公共路由、API 和数据结构保持向后兼容。任何后端字段补充必须是 additive。不得以清空数据库、Grafana volume 或浏览器状态作为整改手段。

## 13. 配置与环境隔离

视觉和功能验证只连接 `127.0.0.1:8001` 的 derivatives 模拟栈。不得切换到 `spot-live`、`derivatives-live` 或 monolith live。响应式视口、reduced-motion、网络失败模拟必须局限于浏览器会话，不改运行配置真源。

## 14. 代码组织与依赖

优先修复共享 token、layout primitive、组件和 formatter，避免在多个视图重复打补丁。维持原生 HTML/CSS/ES modules 架构；除非现有架构无法正确支持目标，否则不引入 React/Vue、CSS framework、chart/table framework 或新运行依赖。

## 15. 文档与操作手册

页面清单记录路由、组件、用途、数据源和复核状态；契约审计记录后端字段、前端使用、缺失和语义问题；整改计划按 P0–P3 分批；最终报告记录截图、视口、测试、剩余问题和真实性边界。若改变现行操作入口或安全提示，同步更新 `docs/code_review/README.md` 或相关操作文档。

## 16. 部署与验收标准

提交后使用项目标准 derivatives 部署入口更新本地模拟栈。提交前验收已通过只读前端预览源连接现有 derivatives 后端完成，且预览源拒绝所有写方法。最终验收要求：全部主路由可达；关键数据链路映射正确；无新增 console error/失败请求；危险操作语义未削弱；主要视口无页面级横向溢出、遮挡或不可达控件；自动化测试通过；报告明确未验证的屏幕阅读器、真实 live 数据和生产负载边界。
