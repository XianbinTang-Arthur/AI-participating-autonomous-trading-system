# AATS 自主交易控制台 — 前端 README

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


> 本 README 覆盖 `aats/api/static/` 下的全部前端代码（顶层 HTML/JS/CSS + `modules/` + `modules/views/` + `modules/actions/`），按"顶层资源 → 基础模块 → View 层 → Actions 层 → 关键机制 → 问题清单"的顺序组织，**逐文件、逐函数**记录。
>
> 生成时间：2026-04-06
> 覆盖文件数：35（5 个顶层 + 18 个 `modules/` + 11 个 `modules/views/` + 3 个 `modules/actions/`）
> 总代码量：约 17 700 行（以 2026-04-06 的 `wc -l` 为准）

---

## 目录

1. [项目总览](#1-项目总览)
2. [目录结构](#2-目录结构)
3. [顶层静态资源](#3-顶层静态资源)
4. [基础模块 `modules/`](#4-基础模块-modules)
5. [View 层 `modules/views/`](#5-view-层-modulesviews)
6. [Actions 层 `modules/actions/`](#6-actions-层-modulesactions)
7. [关键机制](#7-关键机制)
8. [架构问题 / 代码 bug / 语义混乱 / 逻辑混乱汇总](#8-架构问题--代码-bug--语义混乱--逻辑混乱汇总)

---

## 1. 项目总览

AATS 前端是一个**零构建、零框架、纯 ES module** 的单页控制台，运行在浏览器里，与后端 FastAPI 服务通过 HTTP/JSON 通讯。

- **入口**：`dashboard-shell.html`（主控台）+ `login.html`（登录页）
- **加载方式**：`<script type="module" src="/ui/app.js">`；所有模块直接走浏览器原生 ESM。
- **视图数**：10 个 workspace view（home / overview / strategy / execution / risk / exitExecution / replay / aiAnalysis / aiConfig / admin）
- **语言**：全中文 UI，术语表集中在 `modules/terms.js`（>700 条目）。
- **交互范式**：数据展示为主；所有写操作走后端 REST 接口，刷新后回读。

---

## 2. 目录结构

```
aats/api/static/
├── dashboard-shell.html          主控台 HTML 壳
├── login.html                    登录页 HTML 壳
├── login.js                      登录页脚本
├── app.js                        主控台入口（路由、事件、动作分发）
├── app.css                       全部样式
├── README.md                     本文件
└── modules/
    ├── api-client.js             HTTP 客户端 + 超时/abort 链
    ├── store.js                  全局状态 + 配置常量 + viewSpecs + bundle 规划
    ├── flash.js                  Sticky-flash 协议
    ├── copy.js                   文案/列表本地化助手
    ├── formatters.js             数字/时间/HTML 转义助手
    ├── terms.js                  中文术语表 + readable/tone 助手
    ├── components.js             通用组件（pill、card、table、kvList 等）
    ├── shell-renderer.js         DOM-diff patch 引擎 + workspace 渲染
    ├── dashboard-refresh.js      数据刷新引擎（refreshPhase 状态机）
    ├── navigation-state.js       视图路由状态 + URL 同步
    ├── refresh-interactivity.js  按钮刷新锁
    ├── view-router.js            VIEW_ROUTES / VIEW_META / VIEW_LABELS
    ├── trade-display.js          交易场景推断 + 委托/成交表头
    ├── detail-drawers.js         决策/订单/成交/对账四类详情抽屉
    ├── shadow-drawer.js          Phase1 shadow 抽屉
    ├── overlay-parent-renderers.js  overlay parent 子渲染块
    ├── reconciliation-controls.js 对账/恢复按钮渲染逻辑
    ├── exit-execution-helpers.js  exit-execution 工作台纯计算 helper
    ├── dev-self-check.js         运行时自检（验证 kludge 契约）
    ├── views/
    │   ├── home-view.js
    │   ├── overview-view.js
    │   ├── strategy-view.js      （最大文件，~172 KB）
    │   ├── execution-view.js
    │   ├── risk-view.js          （~83 KB，含 exit-execution 工作台）
    │   ├── exit-execution-view.js
    │   ├── replay-view.js
    │   ├── ai-view.js            （~63 KB，含 aiHero/aiLatest/aiReview 等分区）
    │   ├── ai-analysis-view.js   （renderAIAnalysisView 组合入口）
    │   ├── ai-config-view.js
    │   └── admin-view.js
    └── actions/
        ├── admin-actions.js      账号 CRUD
        ├── execution-actions.js  订单/成交明细 + 卡单恢复
        ├── risk-actions.js       阻断处理 + 退出任务动作 + 对账/恢复
        └── rdp-actions.js        RDP workflow 动作处理器
```

---

## 3. 顶层静态资源

### 3.1 `dashboard-shell.html`（145 行）

主控台 HTML 壳。**职责**：

- `<body data-view="home">`：顶层 `data-view` 属性由 `app.js` 根据当前路由同步。
- 顶部固定导航 `<nav class="workspace-nav workspace-nav--top">`：10 个 `<a class="workspace-link" data-view="...">`，`app.js` 通过 `data-view` 做拦截跳转。
- `<section id="bannerContainer">`：flash 与 banner 挂载点。
- `<main class="workspace">`：
  - `#pageHead` 内的 `#pageEyebrow` / `#pageHeading` / `#pageCopy` 由 `app.js` 根据 `VIEW_META` 同步。
  - 每个 view 对应一个 `<section class="workspace-view" data-view="...">`，`is-active` 类由 `app.js` 切换。
  - 每个 view 内部放一个挂载 DIV（如 `#homeContent`、`#strategyContent`），shell-renderer 把 view 返回的 HTML 写进去。
- 首页状态卡片区（`#statusRibbon`）、会话面板（`#sessionIdentityValue` / `#authStateChip` / `#refreshStateChip` / `#lastRefreshLabel`）、命令面板（`#refreshButton` / `#resumeButton` / `#haltButton` / `#autoRefreshToggle` / `#actionPermissionHint`）全部写死在 home view 内。
- `<aside id="detailDrawer">` + `<div id="drawerBackdrop">`：右侧抽屉 + 遮罩，`closeDrawerButton` 带 `data-refresh-ignore`，避免刷新锁。
- 末尾 `<script type="module" charset="utf-8" src="/ui/app.js">`。

### 3.2 `login.html`（36 行）

登录页 HTML 壳。唯一交互表单：`#loginForm` 含 `#loginUsername` / `#loginPassword` / `#loginButton`；提示区 `#loginMessage`；末尾加载 `login.js`。

### 3.3 `login.js`（80 行）

登录页脚本，**不依赖 app.js 主链**，只依赖 `modules/api-client.js`。

- 顶部常量：`nodes = { form, username, password, button, message }`（通过 `document.getElementById`）。
- `init()`：调用 `renderProviders()`，然后挂 `submit` 监听器（阻止默认、调用 `login()`）。
- `renderProviders()`：`GET /auth/providers`，把结果交给 `updateLoginAvailability`；失败则强制走 disabled 分支 + 展示本地化错误。
- `login()`：禁按钮→设 "登录中…" → `POST /auth/login`（body = trim 后的用户名/原样密码）→ 成功跳 `/ui`；失败调 `localizeLoginError` + 重新启用按钮。
- `updateLoginAvailability(payload)`：根据 `auth_enabled` 和 `session_enabled` 切 `is-disabled` 类与输入 disabled；针对 `auth_enabled=false` / `session_enabled=false` 给不同的文案。
- `setMessage(message, tone)`：切 `notice-card tone-*` 类并写入文本。
- `localizeLoginError(message)`：硬编码 3 条错误→中文映射（`operator_auth_required`、`operator_login_failed`、`operator_session_auth_not_configured`），其它原样返回。

### 3.4 `app.js`（~42 KB，主控台入口）

**职责**：路由选择、事件绑定、动作分发、视图切换、状态挂载。由于体量较大，这里按"顶部常量 / 初始化 / 核心循环 / 动作分发 / 调试后门"分块描述。

#### 顶部常量与依赖
- 从 `modules/api-client.js` 中分两次 import（第 1 行 `requestJson`，第 3 行 `fetchDashboardBundle` 等）— 见 **问题 #1**，这是可以合并的冗余 import split。
- 从 `modules/store.js` 导入 `createState` / `REFRESH_PHASE_*` / `viewSpecs` 等。
- 从 `modules/shell-renderer.js` 导入 `renderView` / `patchHtml` / `patchText` / `patchClassName` 等 DOM-diff 原语。
- 从 `modules/dashboard-refresh.js` 导入 `createRefreshController`（返回 `refreshDashboard` / `setupAutoRefresh` / `handleVisibilityChange`）。

#### 初始化
- `init()`：
  1. 构建全局 `state = createState()`。
  2. 挂载 `VIEW_META` 到 `pageHead`。
  3. 创建 `navigationStateController`，并从 URL 的 `?action=…&parent_intent_id=…` 做 `hydrateViewStateFromLocation`。
  4. 挂 `popstate` 监听器 → 切换 `state.activeView` 并重渲染。
  5. 创建 `refreshController = createRefreshController({...})`，把 `refreshDashboard` 暴露给全局（**debug 后门**，见 **问题 #2**）。
  6. 挂全局点击监听器 → 进入 `dispatchAction` 分发器。
  7. 调用首次 `refreshDashboard({ manual: false, initial: true })`。
- `bindWorkspaceLinks()`：为每个 `.workspace-link` 挂 click 监听，拦截默认跳转，改为 `state.activeView = link.dataset.view` + `syncActiveViewLocationState({ pushHistory: true })` + `refreshDashboard`。

#### 核心循环
- `refreshDashboard()` 来自 `dashboard-refresh.js`，但 **app.js 里 wrap 了一层空 catch**，把错误吞掉仅 `console.error` — 见 **问题 #26**。
- `renderAllViews()`：遍历 10 个 view，每个 view 走各自的 `renderXView(data, uiState)` 并通过 `shell-renderer.renderView()` 写入挂载 DIV。
- `syncSessionPanel()`：把 `#sessionIdentityValue` / `#authStateChip` / `#refreshStateChip` / `#lastRefreshLabel` 根据 `state.data.session` 和 `state.refreshPhase` 同步。

#### 动作分发 `dispatchAction(event)`
- 通过 `event.target.closest("[data-action]")` 找到触发元素。
- 以 `data-action` 名做 switch：
  - `refresh-dashboard` → `refreshDashboard({ manual: true })`
  - `toggle-auto-refresh` → 切 `state.autoRefresh` 布尔
  - `select-workspace` → 切 `state.activeView` + 推送 history
  - `begin-logout` → 走 `logoutInFlight` 本地闩（独立于 `actionInFlight`，见 **问题 #4**）
  - `trigger-resume/halt/rebaseline/reconciliation-validate/...` → 分派到 `risk-actions.js`
  - `create-operator / toggle-user / change-user-role / reset-user-password / delete-user` → 分派到 `admin-actions.js`
  - `inspect-order / inspect-fill / resolve-stuck-order / load-more-orders / ...` → 分派到 `execution-actions.js`
  - 未命中则**静默 fallthrough**（没有警告） — 见 **问题 #3**。
- 权限门禁：动作前先走 `canOperate(session)`，提示走 `setFlash(state, "warning", …)` + `renderBanners()`。

#### `runAction` / `runDangerousAction` 辅助
- 两者都是"设置 `actionInFlight` → 发请求 → 成功时 setFlash(info) → 失败时 setFlash(danger) → finally 清 `actionInFlight` → 触发一次刷新"的壳。
- `runDangerousAction` 先做 `confirm(confirmMessage)`，然后走相同的 pipeline。**顺序是 `confirm → ensureNotBusy → beginAction`**（见 **关键机制 §7.5**）。
- 两者都支持可选 `target` DOM 元素 → 加 `is-pending` 类。

#### 调试后门（问题 #2）
`window.refreshDashboard = refreshDashboard;` — 把主刷新函数暴露在 window 上，方便 DevTools 测试，但生产环境也会泄漏，应加 `if (DEBUG) {}` 包裹。

### 3.5 `app.css`（2281 行）

全部样式。分块描述：

1. **`:root` CSS 变量**（行 1-32）：定义色板（`--bg`/`--positive`/`--warning`/`--danger`/`--info`/`--neutral-soft`）、阴影（`--shadow-xl`/`--shadow-lg`/`--shadow-sm`）、圆角（`--radius-xl/lg/md/sm`）、字体族（`--font-display`/`--font-sans`/`--font-mono`）。
2. **全局基础**（行 34-99）：`* { box-sizing: border-box }`、body 径向渐变背景、`body::before` 的 48px 网格纹理、标题/按钮/输入的 reset。
3. **容器与面板**（行 100-240）：`.console-shell`（最大 1680px）、`.masthead` / `.utility-bar` / `.session-panel` / `.command-panel` 共享的 `var(--line) / var(--surface) / var(--shadow-lg) + backdrop-filter: blur(10px)`。
4. **状态胶囊**（行 375-463）：`.status-pill` / `.signal-pill` / `.actor-tag`；`.actor-tag--{system,ai,admin,unknown}` 对应 ACTOR_LABELS 的四种 badge 颜色。
5. **Tone 变体**（行 434-463）：`.tone-positive` / `.tone-warning` / `.tone-danger` / `.tone-info` / `.tone-neutral` / `.tone-outline`。
6. **按钮族**（行 465-614）：`.primary-button` / `.secondary-button` / `.warning-button` / `.danger-button` / `.ghost-button` / `.table-button` / `.inline-button` / `.workspace-tab` / `.workspace-link` 共享 radius/padding/font；`.is-pending` 带 `::before` loading spinner 动画；`.is-refresh-locked` 表示刷新期间禁用。
7. **开关按钮**（行 615-870）：`.toggle-pill` / `.toggle-pill--grid` 的复杂 grid-layout、checked 状态高亮、`:has(input:checked)`。
8. **刷新 shimmer**（行 884-910）：`.view-layout.is-refreshing` / `.surface-card.is-refreshing` 的 `::before` 动画（`section-refresh-shimmer`）。
9. **12 列网格**（行 1008-1014）：`.span-12` / `.span-8` / `.span-7` / `.span-6` / `.span-5` / `.span-4` / `.span-3`。
10. **Summary Strip/Stat Grid**（行 1023-1153）：`summary-strip` 自适应 auto-fit、`.stat-grid` 两列、`.stat-item` 卡片。
11. **KV List / Callout**（行 1155-1220）：`.kv-list` / `.kv-row` / `.callout`。
12. **表格**（行 1222-1276）：`.data-table` 固定 18% + 20% 前两列宽度；`table-layout: fixed`。
13. **Mobile record card**（行 1278-1406）：移动端替代表格的卡片形态，带 `::before` 左侧 4px 色条表示 tone。
14. **Detail Drawer**（行 1646-1695）：`.detail-drawer` 固定右侧，`transform: translateX(100%)`，`.is-open` 时归位；`.drawer-backdrop` 遮罩 z-index=35、抽屉 z-index=40。
15. **Skeleton 骨架**（行 1797-1969）：`.skeleton-panel` / `.skeleton-card` / `.skeleton-tile` / `.skeleton-row` 加 `@keyframes skeleton-shimmer` 的闪动动画。
16. **登录页**（行 1971-1993）。
17. **`@keyframes`**（行 1999-2034）：`refresh-pulse` / `section-refresh-shimmer` / `skeleton-shimmer` / `button-spin`。
18. **响应式**（行 2036-2280）：
    - `@media (max-width: 1280px)`：status-ribbon 3 列、span-* 降级为 span-12。
    - `@media (max-width: 980px)`：masthead 堆叠、command-grid 单列、panel-head flex-direction column。
    - `@media (max-width: 720px)`：console-shell padding 收窄、`.table-wrap { display: none }` + `.mobile-record-list { display: grid }` 表格/卡片切换。
    - `@media (max-width: 1100px)`：policy-summary-grid 单列（仅用于 AI 配置）。

---

## 4. 基础模块 `modules/`

### 4.1 `api-client.js`（116 行）

HTTP 客户端，底座。

**常量**
- `DEFAULT_TIMEOUT_MS = 60_000` — 主 bundle 刷新的 60s 超时。
- `DEFERRED_BUNDLE_TIMEOUT_MS = 120_000` — 延迟 bundle 的 120s 超时（注释解释：延迟 bundle 聚合慢报告）。

**导出函数**
- `requestJson(path, options = {})` — 核心 `fetch` 封装。
  - 自建 `AbortController`；若传入 `options.signal`，挂 `abort` 监听器把外部 abort 转发到自己的 controller（**问题 #18**：外部 signal 的初始 aborted 分支没有走清理路径，但 try/finally 能兜住）。
  - 设置 `Content-Type: application/json`（当且仅当有 body）；`credentials: "same-origin"`。
  - 失败时：`response.text() → 尝试 JSON.parse → 取 payload.detail / 原文`，然后走 `localizeError` 把后端的 `snake_case` 错误码转成中文 → `throw new Error(...)`。
  - `finally` 清理 `timeoutId` + `externalAbortForwarder` 监听器。
- `fetchPanels(specs, options = {})` — 并发取多个 panel，每个 panel 单独捕获错误（除了 AbortError 会往外抛）。返回 `{ [key]: { data, error } }`，错误走 `localizePanelError` 本地化。
- `fetchDashboardBundle(path, options = {})` — 取打包的 bundle（`payload.panels`），同样走 `localizePanelResults`。

**内部辅助**
- `safeJsonParse(text)` — try JSON.parse 失败则返回原始 text。
- `localizePanelResults(results)` — 遍历每个 panel，调 `localizePanelError`。
- `localizePanelError(error)` — null/空字符串 → null；否则走 `localizeError`。
- `isAbortError(error)` — 检查 `error.name === "AbortError"`。

### 4.2 `store.js`（~342 行 / 13 KB）

**全局状态 + 配置常量 + viewSpecs + bundle 规划**。

**导出常量**
- `AUTO_REFRESH_MS = 30_000` — 30s 自动刷新。
- `VIEW_FRESHNESS_MS` — view 缓存过期阈值（与 `AUTO_REFRESH_MS` 一致）。
- `DEFAULT_PAGE_LIMITS` — 每个分页 key 的默认条数（如 `recentOrders: 12`）。
- `PAGE_LOAD_STEP = 12` — `adjustPageLimit(key, +12)` 的步长。
- `DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS` — `{ action: "all", parent: "", actor: "", windowHours: "all" }`。
- `DEFAULT_EXIT_EXECUTION_HISTORY_PAGING` — `{ risk: { offset: 0, limit: 20 }, exitExecution: { offset: 0, limit: 20 } }`。
- `REFRESH_PHASE_IDLE / _PRIMARY / _DEFERRED` — 三个状态机常量。
- `CORE_SPECS` — Map<viewKey, Array<[panelKey, path]>>。每个 view 的主 bundle 所需 panels。
- `EXCLUDED_CORE_PANELS` — 从 core spec 中排除的 panels。重点：`EXCLUDED_CORE_PANELS.risk = new Set([ "mode", "runtime" ])` —— risk view 的 mode/runtime 面板由 home bundle 填。
- `DEFERRED_VIEW_PANELS` — Map<viewKey, Set<panelKey>>，哪些 panel 属于延迟 bundle。
- `viewSpecs` — 每个 view 的入口 URL/数据 key/布局权重。
- `PAGE_LIMIT_AFFECTED_VIEWS` — 调整分页会影响哪些 view 的缓存。
- `EXIT_EXECUTION_FILTER_AFFECTED_VIEWS = new Set(["risk", "exitExecution"])` — 调整退出任务过滤器需要同步失效哪些 view。

**导出函数**
- `createState()` — 返回一个空的全局状态对象：
  ```js
  {
    data: {},                    // 所有后端数据
    ui: { ... },                 // 每个 view 的 UI 状态（分页、过滤器等）
    session: { identity, role },
    activeView: "home",
    refreshPhase: REFRESH_PHASE_IDLE,
    readyViews: new Set(),       // SWR 缓存"已填充"的 view 集合
    viewRefreshedAt: {},         // 每个 view 最后刷新时间戳
    pendingPanels: {},            // panelKey -> { generation: number, reason: string }
    pendingGeneration: 0,
    viewIsLoading: false,         // 主 bundle 加载中
    isPrimaryRefreshing: false,
    autoRefresh: true,
    actionInFlight: false,
    logoutInFlight: false,
    flash: null,
    banners: [],
  }
  ```
- `buildExitExecutionActionHistoryPath(filters)` — 根据过滤器拼 `/system/exit-execution/action-history?...` 路径。
- `dashboardBundlePanelKeys(view)` — 返回某个 view 的 bundle 中应该出现的 panelKey 数组（排除掉 `EXCLUDED_CORE_PANELS[view]` + `DEFERRED_VIEW_PANELS[view]`）。
- `buildDashboardBundlePath(panelKeys)` — 拼 `/dashboard/bundle?panels=key1&panels=key2...` 请求路径。
- `buildDashboardBundleRequestPlan(view, opts)` — 根据 view 给出"primary panels / deferred panels / path"三元组。
- `invalidateCachedViews(state, viewKeys)` — 批量清 `readyViews` + `viewRefreshedAt`。
- `isRefreshInFlight(state)` — `viewIsLoading || isPrimaryRefreshing`。
- `isPrimaryRefreshInFlight(state)` — `state.refreshPhase === REFRESH_PHASE_PRIMARY`。

### 4.3 `flash.js`（70 行）

**Sticky-flash 协议**。Flash 是顶部的提示 banner。

**常量**
- `FLASH_DEFAULT_TTL_MS = 8000` — 8s 默认过期时间。

**导出函数**
- `setFlash(state, tone, message, ttlMs = FLASH_DEFAULT_TTL_MS)` — 写入 `state.flash = { tone, message, _expiresAt: null, ttlMs }`。**懒戳**：`_expiresAt` 留空，由下次 render 时第一次看到 flash 才戳（保证"从渲染开始计时"而非"从发起计时"）。
- `clearFlash(state)` — `state.flash = null`。
- `isFlashLive(state, now = Date.now())` — 当前 flash 是否仍在 TTL 内（若 `_expiresAt` 为 null 则视为存活）。
- `ensureNotBusy(state, renderBanners)` — 在用户动作前调用；若 `state.actionInFlight` 为 true，则 `setFlash("warning", "请等上一个动作完成后再点击。")` + `renderBanners()` + 返回 `false`；否则返回 `true`。所有 `*-actions.js` 都会走这个闸门。

### 4.4 `copy.js`（62 行）

**文案/列表本地化助手**。

**导出函数**
- `hasMeaningfulValue(value)` — 非 null/undefined、非空字符串、数组里至少有一个有意义元素。
- `textOrFallback(value, fallback = "待确认")` — 若 meaningful 则 `String(value).trim()`，否则 fallback。
- `splitCodeList(value)` — 把 `"a、b, c  d"` 切成 `["a","b","c","d"]`（支持中文顿号/英文逗号/空白）。
- `localizeList(value, fallback = "当前暂无说明")` — 走 `splitCodeList` + `localizeError` 逐项本地化，用 `；` 拼接；全空则 fallback。
- `summarizeLocalizedList(value, { fallback, limit = 3, suffix = "等" })` — 同 `localizeList` 但限制最多 N 项，超出加"等"。
- `stateOrFallback(value, fallback)` — `readableState` 的 hasMeaningfulValue 版。
- `meaningfulEntries(value)` — 把 object 过滤成非空 key-value 数组。

### 4.5 `formatters.js`（95 行）

**数字/时间/HTML 转义助手**。

**导出函数**
- `escapeHtml(value)` — 转义 `&`/`<`/`>`/`"`/`'`。
- `formatNumber(value, digits = 4, fallback = "待确认")` — 非有限数→fallback；**`Math.abs(number) >= 1000` 时强制降到 2 位小数**（**问题 #22**：这是隐藏的精度切换，调用者不知道 4 位不会生效）。末尾走 `trimTrailingZeros` 去尾零。
- `formatSigned(value, digits, fallback)` — 走 `formatNumber` 然后在正数前加 `+`。
- `formatMaybeTimestamp(value, fallback = "时间待同步")` — 走 `parseDate` → `toLocaleString("zh-CN", { hour12: false })`（**问题 #21**：`parseDate` 用 `replace("Z", "+00:00")` 的 kludge 处理 UTC）；若 `parseDate` 失败则 `escapeHtml(原始值)`。
- `formatRelativeAge(value, fallback)` — 秒/分钟/小时/天级相对时间。
- `formatDuration(seconds, fallback)` — "N 天 N 小时" / "N 小时 N 分钟" / "N 分钟" / "N 秒"。
- `listOrDash(value, fallback)` — 数组用 `、` 拼接，空则 fallback。
- `booleanWord(value, fallback)` — `true → "是"`、`false → "否"`，其它 fallback。
- `middleEllipsis(value, start = 10, end = 6, fallback)` — 字符串长于 `start+end+3` 时首尾保留、中间 `...`。
- `emptyState(message)` — `<div class="empty-state">...</div>`。
- `rawJson(value)` — `<pre class="raw-json">JSON.stringify(...)</pre>`。
- `parseDate(value)` — `new Date(String(value).replace("Z", "+00:00"))`，NaN 则 null。

**内部**
- `trimTrailingZeros(value)` — 去掉"小数点后无意义的零"（保留第一个有意义小数）。

### 4.6 `terms.js`（1595 行 / ~94 KB）

**中文术语表 + readable/tone 助手**。按块组织：

#### TERM_MAP（行 1-477，约 500 条）
从后端的 snake_case 枚举到中文短词的扁平字典。按主题分区：交易状态、决策来源、盘面状态、风控阶段、对账状态、shadow 状态、AI 状态、档位、overlay 驱动、恢复状态、权限、执行建议、leg 角色、profile 名称 等。

**已知重复 key**（**问题 #13**）：
- `blocked`（行 14 和行 106 重复定义）
- `regime_range`（行 140 和行 176 重复定义）

#### ERROR_MAP（行 479-708，约 230 条）
从后端报错码到中文短句的扁平字典。

#### 导出助手（行 709-1595）

**readable 系列**
- `readableState(value, fallback = "待确认")` — 核心：先 trim → 查 TERM_MAP → 失败则走"snake → 空格 → capitalize"兜底。
- `readableBookLabel(book)` — target/inventory/target_and_inventory → 中文。
- `readableExpectancyBps(value)` — 格式化 bps 形式的 expectancy。
- `readablePercentRatio(value)` — 0-1 → 百分比。
- `readableSignedQuantity(value)` — 带正负号的数量。
- `readableCloseReasonDistribution(dict)` — close reason 的比例分布。
- `readableExpectedVsRealizedBookBreakdown(summary)` — target vs inventory 的 expected/realized 比较。

**normalized 系列**（接收后端原始 dict → 返回一个前端好用的 normalized shape）
- `normalizedFamilyExecutionSummary(summary)`
- `normalizedSummaryList(list)`
- `normalizedBookExpectancySummary(summary)`
- `normalizedExpectedVsRealizedSummary(summary)`
- `normalizedIndependentAdaptiveSummary(summary)`
- `normalizedIndependentTransitionExceptionSummary(summary)`
- `normalizedOverlayDecision(decision)`
- `normalizedBookRuntimeStates(states)`

**family execution summary**
- `hasFamilyExecutionSummary(summary)` — 是否存在。
- `readableFamilyExecutionSummary(summary)`
- `readableFamilyExecutionDirection(summary)` — long/short/neutral → 中文。
- `readableFamilyExecutionMeta(summary)` — 副标题行。

**book expectancy summary**
- `readableBookExpectancySummary(summary)`
- `readableExpectedVsRealizedSummary(summary)`
- `readableExpectedVsRealizedMeta(summary)`

**independent adaptive summary**
- `readableIndependentAdaptiveLegSummary(leg)`
- `readableIndependentAdaptiveSummary(summary)`
- `readableIndependentAdaptiveMeta(summary)`
- `readableTransitionViolationReason(code)`
- `readableIndependentTransitionExceptionSummary(summary)`
- `readableIndependentTransitionExceptionMeta(summary)`

**overlay parent**
- `overlayDriverLabel(code)` / `overlayLifecycleLabel(code)` — 按驱动类型 / 生命周期状态转中文。
- `readableOverlayParentSignalSummary(signal)` — 把 signal dict 拼中文。
- `readableOverlayParentPostmortemMeta(postmortem)` — 复盘的 meta 行。
- `readableOverlayParentLegQuantitySummary(summary)` — leg 数量分布。

**book runtime states**
- `readableBookRuntimeStateSummary(states)`

**错误/状态本地化**
- `localizeError(code, fallback)` — **核心**：`ERROR_MAP[trimmed] || TERM_MAP[trimmed] || fallback || original`。这是整套前端本地化的主入口。
- `toneForRuntimeState(state)` — healthy/warning/danger/info/outline。
- `toneForReconciliationSeverity(severity)` — warn/critical → warning/danger。
- `toneForOrderStatus(status)` — submitted/partial/filled/canceled/expired → 对应 tone。

**状态 headline/copy**
- `tradingStatusLabel(state)` / `recoveryStatusLabel(state)` / `reviewStatusLabel(state)` / `reconciliationStatusLabel(state)` / `permissionStatusLabel(state)` — 每个 family 返回一个短标签。
- `statusHeadline(state)` — 首页大号字。
- `operationalStatusLabel(state)` / `operationalStatusHeadline(state)` / `operationalStatusCopy(state)` — operational status 的三段文案（**问题 #45**：`operationalStatusLabel` 是一串 10+ `if-return` 链，可以表驱动化）。

### 4.7 `components.js`（355 行 / 12 KB）

**通用组件**（所有组件都返回 HTML 字符串；没有任何 DOM 操作）。

**常量**
- `ACTOR_LABELS` — 9 个身份 key 的中文短词（`system`/`ai`/`admin`/`risk_control`/`config`/`operator`/`viewer`/`unknown`/`api_key_write`）。

**导出函数**
- `pill(label, tone = "neutral")` — 返回 `<span class="status-pill tone-${tone}">...</span>`。
- `actorFallbackLabel(key)` — 没命中 ACTOR_LABELS 时返回 "未知"。
- `actorTag(key)` — 返回单个 `<span class="actor-tag actor-tag--${kind}">...</span>`。
- `actorTags(...keys)` — 把多个 actor 合并为一串（过滤掉 null/重复）。
- `surfaceCard({ title, kicker, copy, classes = "", content, panelKey, actions, badge })` — 通用卡片壳；`panelKey` 走 `panelKeyAttribute`。
- `primaryStatusPanel({ tone, eyebrow, headline, summary })` — 首页顶部大板。
- `panelKeyAttribute(keys)` — 把单 key / key 数组转成 `data-panel-key="key1 key2"`（用空格分隔多 key，供 `syncRefreshDisabledButtons` 匹配）。
- `summaryStrip(tiles)` — 自适应 grid 的"概要磁贴"行，每个 tile 支持 `{ label, value, meta, tone, badge }`。
- `alertQueue(items)` — 警报队列。
- `kvList(rows)` — 二/三列的 key-value 列表。`rows` 是 `Array<[label, value, meta?]>`。
- `statGrid(items)` — 2 列的统计网格。
- `callout({ title, copy, pills })` — 高亮框。
- `table({ headers, rows, empty })` — 桌面端表格 + 移动端 fallback 卡片。
- `responsiveTable(...)` — table 的自适应包装。
- `timeline(items)` — 事件时间线。
- `notice({ tone, title, copy })` — notice-card。
- `actionButton(label, action, value = "", tone = "secondary", { disabled, title } = {})` — 带 `data-action` / `data-value` 的按钮，且 `data-refresh-ignore` 可以通过额外 prop 传入。
- `mobileRecordCard(...)` — 移动端用的卡片替代品。

**内部**
- `buttonClass(tone)` — 根据 tone 返回按钮 class。
- `buildFallbackMobileCards(headers, rows)` — 把 `table(...)` 的 rows 转成 mobileRecordCard 列表（行内部还要先 parseTableCell）。
- `parseTableCell(html)` — **正则解析**`<strong>` + `table-meta` 从原始 HTML（**问题 #35 相关**：这里的做法是反向的，理想应该让调用方直接传结构化数据，而不是把 HTML 再 parse）。
- `htmlCellToText(html)` — 去掉所有标签。
- `decodeHtmlEntities(value)` — `&amp;` / `&lt;` / `&gt;` / `&quot;` / `&#39;` 反解。

### 4.8 `shell-renderer.js`（~26 KB）

**DOM-diff patch 引擎 + workspace 渲染**。核心思想：view 层返回**纯 HTML 字符串**，渲染器通过 DOM diff 最小化更新。

**常量/缓存**
- 每个 `[data-panel-key]` / `[data-view]` 元素都有一个 `WeakMap` 记录上次写入的 `cacheKey`，避免对完全相同的内容重复 innerHTML 赋值（**问题 #20**：WeakMap 只按 node 缓存，不按 panelKey，意味着 node 被重建后缓存就失效；整体权衡合理）。

**导出函数**
- `renderView(mountNode, html, { cacheKey, isRefreshing })` — 往 `mountNode` 写 HTML；若 cacheKey 相同则跳过；根据 isRefreshing 切 `is-refreshing` 类。
- `patchHtml(node, html)` — 仅在内容真变化时 `innerHTML = html`。
- `patchText(node, text)` — 同上但针对 `textContent`。
- `patchClassName(node, className)` — 同上但针对 `className`。
- `patchAttribute(node, name, value)` — 对特定属性。
- `togglePanelRefreshing(node, flag)` — 快速切 `is-refreshing` 类。
- `syncSectionNavLinks(nav, sections)` — 把 section-nav 的高亮同步到当前 scroll 位置。
- `renderSkeletonPanel()` / `renderSkeletonCard(...)` — Skeleton 骨架渲染器（这里才是"看到 `.skeleton-panel` 动画"的来源）。

### 4.9 `dashboard-refresh.js`（~22 KB）

**数据刷新引擎**。核心是 `refreshPhase` 状态机。

**`createRefreshController({ state, ... })`** 返回 4 个函数：`refreshDashboard`, `setupAutoRefresh`, `handleVisibilityChange`, `invalidateCachedViewsNow`。

**核心流程 `refreshDashboard(opts = {})`**
1. 若当前 `refreshPhase !== IDLE` 且不是 `manual`，直接返回（去重）。
2. 计算当前 view 的 `primary panels` / `deferred panels`（来自 `buildDashboardBundleRequestPlan`）。
3. 切 `refreshPhase = PRIMARY` + `isPrimaryRefreshing = true`。
4. 设 `viewIsLoading = true`（如果当前 view 的 `readyViews` 没命中或 `viewRefreshedAt` 超过 `VIEW_FRESHNESS_MS`，则 SWR 失效）。
5. `fetchDashboardBundle(primaryPath)` → 把数据灌到 `state.data`。
6. 清 `viewIsLoading`、加 `readyViews`、戳 `viewRefreshedAt`、切 `refreshPhase = DEFERRED`。
7. 并发启动 `deferred panels` 请求（不阻塞 UI）。
8. 每个 deferred 请求到达后写 `state.data[key] = ...` 并重渲染那一片；生成号（`state.pendingGeneration`）用来避免"旧请求覆盖新状态"。
9. 全部 deferred 完成 → 切 `refreshPhase = IDLE`。

**`setupAutoRefresh()`**
- 每 `AUTO_REFRESH_MS` 触发一次 `refreshDashboard({ manual: false })`。
- 一旦 `state.autoRefresh = false` 则暂停。

**`handleVisibilityChange()`**
- `document.visibilityState === "visible"` 时立即触发一次 refresh。
- **问题 #25**：里面有一段 `if (!state.autoRefresh) return; else refreshDashboard(...)` 的冗余分支（先 return 后又执行），代码不对称但不影响逻辑。

**`invalidateCachedViewsNow(views)`** — 清指定 view 的 readyViews/viewRefreshedAt，下次渲染立即走真正的 refetch。

**每次 refreshDashboard 失败的处理**
- `catch (error) { /* 空 */ }` — **问题 #26**：把错误吞掉，诊断信息丢失；应该至少 `console.error`。

### 4.10 `navigation-state.js`（196 行）

**视图路由状态 + URL 同步**。

**导出常量**
- `EXIT_EXECUTION_HISTORY_ACTION_FILTERS = new Set(["all", "refresh_exchange_state", "retry_limit_lookup", "safe_cancel"])`
- `EXIT_EXECUTION_HISTORY_WINDOW_FILTERS = new Set(["all", "1", "6", "24", "168", "720"])` —— **问题 #19**：硬编码 window-hour 集合（1/6/24/168/720 小时），如果要加一个新窗口就得三处同步改（这里 + risk-view 渲染 + normalize 函数）。
- `VIEW_REPLAY_FILTERS = new Set(["all", "inventory_only", "target_only", "target_and_inventory"])` —— 同样硬编码。

**导出函数**
- `coerceReplayParentFilter(value)` — 非法值兜底为 `"all"`。
- `normalizeExitExecutionHistoryFilterValue(value)` — trim + lowercase。
- `exitExecutionHistoryWindowThresholdMs(value, now = Date.now())` — 根据小时数返回"截止时间戳"，`"all"` 返回 null。
- `createNavigationStateController({ state, viewLinks = [] })` — 返回一个控制器对象，内含：
  - `ensureExitExecutionHistoryState(view = "risk")` — 懒初始化 risk/exitExecution 两个 view 的过滤器状态（从 `DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS` + `DEFAULT_EXIT_EXECUTION_HISTORY_PAGING[view]` 起步）。
  - `copyExitExecutionHistoryFilters(source, target)` — 复制 action/parent/actor/windowHours 四个字段。
  - `syncExitExecutionHistoryFiltersAcrossViews(sourceView)` — 保持 risk 和 exitExecution 两个 view 的过滤器同步；**问题 #24**：当 sourceView 不是 risk 时重置 risk.offset，反之则重置 exitExecution.offset —— 这种不对称是为了"用户在一侧翻页不影响另一侧"，但注释没说明。
  - `activeExitExecutionHistoryView()` — 根据 `state.activeView` 返回 "risk" 或 "exitExecution"。
  - `activeExitExecutionHistoryState()` — 返回当前活跃 view 对应的 history state。
  - `readExitExecutionHistoryStateFromLocation(search)` — 从 URL query 读过滤器并校验。
  - `hydrateViewStateFromLocation(view)` — 仅对 exitExecution view 生效，把 URL → state.ui.exitExecution.exitExecutionHistory。
  - `buildExitExecutionViewPath()` — 根据当前 state 拼 `/ui/exit-execution?offset=…&limit=…&action=…&window_hours=…` URL。
  - `buildViewPath(view)` — exitExecution 走 `buildExitExecutionViewPath`，其余查 `VIEW_ROUTES`。
  - `syncActiveViewLocationState({ pushHistory })` — 把 URL 同步到当前 view；推 history 还是 replace history 可选。
  - `syncExitExecutionNavigationLinks()` — 更新顶部导航里 `exitExecution` 链接的 href（因为 URL 带 query，需要根据当前过滤器重建）。

### 4.11 `refresh-interactivity.js`（101 行）

**按钮刷新锁**。所有"还在刷新就禁止点击"的逻辑都在这里。

**常量**
- `REFRESH_LOCKED_FLAG = "refreshLocked"`
- `REFRESH_TITLE_FLAG = "refreshLockedTitle"`
- `MISSING_TITLE_SENTINEL = "__refresh_title_missing__"`
- `REFRESH_LOCKED_CLASS = "is-refresh-locked"`
- `PANEL_REFRESHING_CLASS = "is-refreshing"`

**导出函数**
- `syncRefreshDisabledButtons({ roots, refreshing, pendingPanels, reason, panelReason })` — 主入口。遍历每个 root：
  1. 先同步 `[data-panel-key]` 容器的 `is-refreshing` 类（走 `syncPendingPanelIndicators`）。
  2. 遍历所有 `<button>`：带 `data-refresh-ignore` 的跳过；其它根据"全局刷新中"或"所在 panel 在 pending"决定是否上锁。

**内部**
- `shouldIgnoreButton(button)` — 检查 `data-refresh-ignore`。
- `syncPendingPanelIndicators(root, pendingPanels)` — 每个 panel 根据 `panelHasPendingKey` 切 class。
- `panelHasPendingKey(panel, pendingPanels)` — 读 `data-panel-key` 后 **split 空格**，任何一个 key 命中就算 pending（这就是 `panelKeyAttribute` 能接收多个 key 的前提）。
- `isInsidePendingPanel(element, pendingPanels)` — 用 `element.closest("[data-panel-key]")` 找祖先。
- `syncRefreshDisabledButton(button, { shouldLock, reason })` — dispatcher。
- `lockButtonForRefresh(button, reason)` — 保存原 `disabled` / `title`，设 `is-refresh-locked` + 新 title + disabled = true；**注意**：已 disabled 的按钮不会被锁（避免覆盖权限禁用）。
- `unlockButtonAfterRefresh(button)` — 解锁并还原 title；原本没有 title 的用 `MISSING_TITLE_SENTINEL` 记住，解锁时 `removeAttribute("title")`。

### 4.12 `view-router.js`（110 行）

**纯数据模块**，不含业务逻辑。

**导出**
- `VIEW_ROUTES` — 10 个 view → URL 的映射。
- `VIEW_META` — 每个 view 的 `docTitle` / `eyebrow` / `heading` / `copy` / `hidePageHead`。home view 是 `hidePageHead: true`（它自己的 status ribbon 兼具 page head）。
- `VIEW_LABELS` — 10 个 view 的中文短名，用于菜单按钮。
- `VIEW_ROUTE_ALIASES = { "/ui/ai": "aiAnalysis" }` — 老链接的兼容。
- `resolveKnownView(view, fallback = "home")` — 查 VIEW_ROUTES，失败返回 fallback。
- `resolveViewFromLocation(location = window.location)` — 先看 ALIASES，再用 Object.entries 找匹配 pathname。

### 4.13 `trade-display.js`（~12 KB）

**交易场景推断 + 委托/成交表头**。

**常量**
- `TRADE_SCENES` — "spot" / "derivatives" / "unknown" 三种场景。
- `SCENE_LABELS` — 场景中文标签。

**导出函数**
- `inferTradeScene(runtime)` — 根据 `runtime.trading_product_type` 或 `runtime.margin_mode` 或 `runtime.symbol` 推断场景。
- `tradeSceneLabel(scene)` — 中文名。
- `orderTableHeaders(scene)` — 返回表头数组；**问题 #23**：不管 scene 是 spot 还是 derivatives，返回的表头都一样，场景参数目前是 dead parameter。
- `orderTableRow(order, scene)` — 订单行构造。
- `fillTableHeaders(scene)` — 成交表头。
- `fillTableRow(fill, scene)` — 成交行构造。
- `formatOrderLifecycle(order)` — 订单生命周期摘要。
- `formatFillLifecycle(fill)` — 成交生命周期摘要。

### 4.14 `detail-drawers.js`（644 行）

**详情抽屉构建器**。四类抽屉：decision / order / fill / reconciliation。

**特殊依赖**：**问题 #5** — `import { ... } from "./views/risk-view.js"`（第 38 行）—— 基础层反向依赖 view 层，破坏了单向分层。

**常量**
- `EXECUTION_SUGGESTION_LABELS` — 6 个 execution suggestion key 的中文（`conservative` / `balanced` / `aggressive` / `defer` / `veto` / `reference_only`）。**问题 #32**：这个常量在 `detail-drawers.js` / `ai-view.js` / `strategy-view.js` 三处各有一份，triple source of truth。

**导出函数**
- `buildDecisionDrawer(detail, { recovery, uiHints })` — 决策详情抽屉（最大）。内含 economic rows / audit rows / execution rows / hedge mode rows / overlay audit rows / overlay parent postmortem rows / leg order rows / leg trial guard rows / leg reconciliation rows。
- `buildOrderDrawer(detail)` — 订单详情抽屉（生命周期 + 事件 + 链路 ID）。
- `buildFillDrawer(detail)` — 成交详情抽屉。
- `buildReconciliationDrawer(detail, { recovery, latestReconciliationId, uiHints })` — 对账详情抽屉。内含 bills categories / bill explanations / bill cases / bill actions 四块。

**私有 decision 助手**（行 39-644）
- `decisionEconomicRows(decision)` — expectancy / net edge / cost / gate 诊断的 kv 行。
- `targetExpectancyDisciplineSummary(decision)` — target 侧的 expectancy 自律性摘要。
- `decisionAuditRows(decision)` — 决策审计（谁做的、为什么、什么时候）。
- `decisionExecutionRows(decision)` — 执行侧的 kv 行。
- `shouldRenderHedgeModeAudit(decision)` — 是否显示 hedge mode 审计（只在衍生品场景显示）。
- `decisionHedgeModeRows(decision)`
- `decisionOverlayAuditRows(decision)` — **问题 #39**：一行用 `items.slice(0, 3)`，下一行用 `items.slice(0, 2)`，两个 preview size 不一致且没解释为什么。
- `decisionOverlayParentPostmortemRows(decision)`
- `decisionLegOrderRows(decision)`
- `decisionLegTrialGuardRows(decision)`
- `decisionLegReconciliationRows(decision)`

**label 辅助**
- `decisionSourceLabel(code)` — baseline / ai / baseline_fallback / admin_override → 中文。
- `decisionAuthorityLabel(code)` — reference_only / advisory / authoritative → 中文。
- `hedgeModeLabel(code)` — one_way / hedge / unknown → 中文。
- `exchangePositionModeLabel(code)` — long_only / short_only / net / hedge → 中文。
- `decisionSourceNarrative(decision)` — 把决策来源讲成一句自然语言。

**reconciliation 子渲染**
- `renderReconciliationBillsCategories(bills)` — bills 按 category 分组列出。
- `renderReconciliationBillExplanations(bills)`
- `renderReconciliationBillCases(bills)`
- `renderReconciliationBillActions(bill, { recovery, uiHints })`

**suggestion / translation**
- `suggestionSummaryText(suggestion)` — suggestion → 中文句子。
- `executionSuggestionLabel(code)`
- `translationPreviewSummaryText(translation)` — translation preview → 中文句子。
- `liveExecutionSummaryText(live)` — **问题 #40**：里面的优先级表达式 `a || b || c !== null && c !== undefined` 在 JS 里依靠 `!==` 优先级高于 `&&` 和 `||` 才能工作，读起来很容易看错，应该加括号。

**drawer 工具**
- `drawerText(value, fallback)` — 空安全文本。
- `drawerListText(items)` — drawer 内用的列表拼接。
- `strategySummary(strategy)`
- `describeDecisionIntent(intent)`

### 4.15 `shadow-drawer.js`（253 行）

**Phase1 shadow 抽屉构建器**。

**常量**
- 5 个本地 status：`not_configured` / `idle` / `healthy` / `lagging` / `degraded` → 标签 + tone。**这些 key 在 terms.js 里也有对应条目**，但这里单独维护了一份本地 map，理由是"shadow 的状态码和通用 state 不完全重合"。

**导出函数**
- `buildPhase1ShadowDrawer(detail, { shadowBlocker, uiHints, history })` — 主构建器。

**内部助手**
- `renderShadowActions(shadowBlocker, uiHints)` — 根据 blocker 渲染可点击动作。
- `readableShadowStatus(status)` — 本地 label map。
- `toneForShadowStatus(status)` — 本地 tone map。
- `toneForBacklog(backlogCount)`
- `backlogValue(dict)`
- `syncMeta(state)`
- `kvRow(label, value, meta)` — **问题 #35**：本地自己拼 kv-row 的 HTML，duplicate 了 `components.kvList` 里已有的 row markup。两边如果 class 名改了会不同步。
- `renderHistory(history)` — history list 渲染。
- `historyTitle(item)` / `historySubtitle(item)` / `historyTone(item)` / `historyPill(item)` — 每条 history 的一小撮 label 助手。

### 4.16 `overlay-parent-renderers.js`（95 行）

**overlay parent 的渲染子块**。

**导出函数**
- `overlayParentPostmortemMeta(postmortem)` — postmortem meta 行。
- `overlayParentPostmortemRows(postmortem)` — kv-list 行数组。
- `renderOverlayParentHistoryTable(history)` — history 表格。

**内部**
- `overlayParentSourceOfTruth(postmortem)` — 判断此条 postmortem 依据哪一侧（protective / opportunistic / independent）。
- `overlayParentQuantityMeta(postmortem)` — 数量一致性 meta。
- `replayHealthSummary(postmortem)` — replay 侧健康度摘要。

---

## 5. View 层 `modules/views/`

### 5.1 `home-view.js`

**主页 view**。职责：展示首页 `status-ribbon`、会话面板、命令面板下方的一整屏布局。

- `renderHomeView(data, uiState)` — 主入口；返回字符串 HTML 塞进 `#homeContent`。内部构造：
  - 首页概览（trading status / AI state / recovery state）。
  - 最近决策 callout。
  - 最近订单/成交/对账的小摘要卡。
  - 最近阻断摘要。
- 局部还维护一份 `deferredLoading` 集合，列出"延迟 bundle 里该 view 关心的 panelKey"。**问题 #27**：同一份 key 列表在 home-view / `DEFERRED_VIEW_PANELS.home` (store.js) / dashboard-refresh 的 `buildDashboardBundleRequestPlan` 里各写一次，三处散落。

### 5.2 `overview-view.js`

**交易总览 view**。多卡片组合：
- `renderOverviewView(data, uiState)` — 主入口。
- `overviewStatusStrip(data)`
- `overviewAccountCard(data)`
- `overviewPositionCard(data)`
- `overviewExposureCard(data)`
- `overviewRecentTradesCard(data, uiState)`
- `overviewLatestDecisionCard(data)`
- 每个卡片都走 `surfaceCard` 并在适当位置用 `kvList` / `summaryStrip` / `statGrid`。

### 5.3 `strategy-view.js`（172 KB — 最大文件）

**策略判断 view**。解释"为什么要/不要做这笔交易"。里面包含大量的条件分支、内嵌卡片、数据 normalize。

**主入口**：
- `renderStrategyView(data, uiState)` — 组合策略层和执行层的所有判断。

**section 级渲染**：
- `strategyHeroStrip(data)` — 顶部状态条。
- `strategyRegimeCard(data)` — 当前盘面状态 / 方向偏好。
- `strategyExpectancyCard(data)` — expectancy + 经济门槛。
- `strategyBlockersCard(data)` — 当前阻断原因（`blockerControl.blockers`）。
- `strategyLatestDecisionCard(data)` — 最新决策 callout。
- `strategyExecutionCard(data)` — 执行层指标（滑点、cost edge、preflight 等）。
- `strategyAttributionCard(data)` — 策略归因，按 book 拆分。
- `strategyShadowCard(data)` — 策略层 shadow 摘要。
- `strategyFamilyExecutionCard(data)` — family execution 汇总。
- `strategyTrialGuardCard(data)` — 试盘守护。
- `strategyScalingReviewCard(data)` — 放量评审。
- `strategyIndependentBookCard(data)` — 独立 thesis 书的状态。
- `strategyOverlayParentCard(data)` — overlay parent 状态。

**助手**：
- `listText(items, fallback)` — **问题 #6**：本地自建的 list → 中文拼接助手，duplicate `localizeList` 里已有的函数。同一份代码在 strategy-view / risk-view / ai-config-view 三处。
- `tradeCostConfigRow(data)` — **问题 #14**：几乎纯 delegator，直接调下层。
- `escapeFallbackReadableState(state)` — **问题 #15**：低使用率的本地化包装。
- `plainListText(items)` — **问题 #16**：定义了但在本文件里找不到调用者（dead code）。
- `chooseTrialVerdict(verdicts)` — **问题 #17**：trivial 的一步 if-return。
- `renderPaginationFooter(payload, { onLoadMore, onCollapse })` — **问题 #11**：分页 footer 的渲染逻辑，execution-view / ai-view 各有一份，三处有轻微差异。

### 5.4 `execution-view.js`

**委托与成交 view**。

- `renderExecutionView(data, uiState)` — 主入口。
- `executionSummaryStrip(data)` — 顶部概要。
- `renderOrderTable(data, uiState)` — 最近订单表；走 `trade-display.orderTableHeaders(scene)` + `orderTableRow`。
- `renderFillTable(data, uiState)` — 最近成交表。
- `renderStuckOrders(data)` — 卡单列表，附 `resolve-stuck-order` 动作按钮。
- `renderPaginationFooter(...)` — **问题 #11**：本地版分页 footer。
- `renderExecutionBlockers(data)` — execution 相关阻断。

### 5.5 `risk-view.js`（1696 行 / 83 KB）

**风险与恢复 view + exit-execution 工作台**。**第二大文件**。

**顶部**：
- 第 2 行有一个**单独的 `kvList` import**（**问题 #10**），与下面大块的导入分开。不影响运行但不整洁。
- 此外 `localizeError` / `readableState` 等 helper 被**重复导入多次**（**问题 #44**），至少 5 处来自 `../terms.js`/`../copy.js`。

**主导出**
- `renderRiskSections(data, uiState)` — 返回 22 个命名 section：overview / trading / halting / blockers / recovery / reconciliation / accountSnapshot / legAssertions / phase1Shadow / exitExecutionOverview / exitExecutionWorkspace / ... 。调用方（`renderRiskView` 或 `exit-execution-view`）可以挑需要的 section。
- `renderRiskView(data, uiState)` — 主 view。用 5 段式 workspace nav：
  1. `risk-overview` — 综合总览。
  2. `risk-recovery` — 恢复流程状态。
  3. `risk-review` — 待人工复核项。
  4. `risk-exit-workspace` — 退出任务工作台（包含全量历史 + 过滤器）。
  5. `risk-diagnostics` — 对账/leg 异常/phase1 shadow 诊断。
- `mergedExitExecutionReviewItems(data)` — 把系统侧和 shadow 侧的 review item 合并。
- `renderExitExecutionActionHistoryList(items)` — 退出任务动作历史列表。
- `renderExitExecutionWorkspace(data, uiState)` — 完整的 exit-execution 工作台（过滤器 + 列表 + 分页 + review 列表），**在 `exit-execution-view.js` 里被反向 import**（**问题 #36**）。
- `normalizedExitExecutionHistoryFilters(filters)` — 过滤器字段归一化。
- `renderReconciliationControls(data, uiHints)` — 对账控制按钮（validate/rebaseline）。
- `reconciliationActionCopy(action)` — 按动作返回按钮文案。

**大量私有助手**

Risk 本体：
- `renderPrimaryTaskPanel(data)` — 顶部主任务卡。
- `renderBlockerControlList(blockers)` — 阻断列表。
- `renderBlockerActions(blocker)` — 每条阻断的 action 按钮。
- `noPrimaryBlockerSummary(data)` — **问题 #9**：行 1510-1516 的缩进明显错乱（4 空格 vs 2 空格），不影响运行但会扰乱 diff。
- `riskHeadline(data)` / `riskTone(data)` — 顶部大号字与颜色。
- `shouldShowHalting(data)` / `shouldShowBlockerList(data)` / ... — 一串 `shouldShow*` 谓词。
- `legMismatchTone(severity)` / `legMismatchSummaryMeta(mismatch)` — leg 对账异常的渲染。
- `derivativesPositionModeLabel(code)` / `exchangePositionModeLabel(code)` — 衍生品持仓模式标签。
- `requiredExchangeModeMeta(data)` / `localInstrumentLegMeta(leg)` — 所需交易所模式 meta。
- `actionSuggestsRebaseline(action)` — 判断某个 action 是否暗示需要 rebaseline。
- `reconciliationNeedsAttention(data)` — 是否需要人工关注对账。
- `resumeActionHint(data)` — 恢复按钮提示。
- `isPausedAwaitingResume(data)` — 系统是否暂停中等待恢复。
- `listText(items, fallback)` — **问题 #6**：再一次重复。

Phase1 shadow 助手：
- `phase1ShadowLabel(state)` / `phase1ShadowTone(state)` / `backlogText(state)` / `phase1ShadowHasBacklog(state)` / `phase1ShadowReviewMeta(state)` / `phase1ShadowLastError(state)`。

Exit-execution 工作台助手：
- `renderExitExecutionReviewList(items)` / `renderExitExecutionReviewItem(item)` / `renderExitExecutionReviewActions(item)`.
- `renderExitExecutionActionHistoryItem(item)`.
- `renderExitExecutionActionHistoryFilters(uiState)` — 过滤器渲染。
- `renderExitExecutionActionFilterOptions()` — **问题 #37**：硬编码 `["all", "refresh_exchange_state", "retry_limit_lookup", "safe_cancel"]`，和 `navigation-state.EXIT_EXECUTION_HISTORY_ACTION_FILTERS` 重复。
- `renderExitExecutionActionWindowOptions()` — **问题 #38**：硬编码 `[1, 6, 24, 168, 720]` 小时，和 `EXIT_EXECUTION_HISTORY_WINDOW_FILTERS` 重复。
- `filterExitExecutionActionHistory(items, filters)` — 过滤 pipeline。
- `exitExecutionActionActorSearch(item, keyword)` — 按 actor 搜索。
- `exitExecutionActionCreatedAtMs(item)` — 提取时间戳。
- `exitExecutionHistoryWindowThresholdMs(windowValue)` — 本地版窗口阈值（与 `navigation-state` 同名但独立）。
- `exitExecutionActionDisabledReason(session)` / `exitExecutionAdminPermissionReason(session)` / `hasExitExecutionAdminAccess(session)`.
- `normalizedExitExecutionOperatorActions(items)` / `exitExecutionOperatorActionDescriptor(action)`.
- `exitExecutionReviewSummary(review)` / `exitExecutionReviewReasonLabel(code)` / `exitExecutionReviewMeta(review)`.
- `exitExecutionLatestAction(items)` — 找到最近一条动作。
- `renderExitExecutionRecentActions(items)` / `renderExitExecutionRecentAction(item)`.
- `exitExecutionOperatorActionSignature(item)` — 生成 action 签名（用于 dedupe）。
- `exitExecutionCurrentBlocker(data)` / `exitExecutionLatestActionLabel(item)` / `exitExecutionLatestActionStatus(item)`.

Trial guard 助手：
- `trialGuardStatusLabel(status)` / `trialGuardTone(status)` / `marginBufferTone(value)` / `preflightTone(value)` / `packetTone(value)` / `trialRatioText(ratio)`。

### 5.6 `exit-execution-view.js`（102 行）

**退出任务工作台 view**。

**问题 #36 的源头**：从 `./risk-view.js` 反向 import `mergedExitExecutionReviewItems`, `normalizedExitExecutionHistoryFilters`, `renderExitExecutionWorkspace`。

**导出函数**
- `renderExitExecutionView(data, uiState)` — 单一主入口。内部构造 3 个 surfaceCard：
  1. **概览** — 当前 exit-execution 状态摘要。
  2. **历史工作台** — 调用 `renderExitExecutionWorkspace(data, uiState)`。
  3. **复核提醒列表** — 调用 `mergedExitExecutionReviewItems(data)`。

### 5.7 `replay-view.js`（288 行）

**回放与复盘 view**。

**常量**
- `DEFAULT_REPLAY_PARENT_FILTER = "all"`
- `REPLAY_PARENT_FILTERS` — 4 个值：`all` / `inventory_only` / `target_only` / `target_and_inventory`。

**导出函数**
- `renderReplaySections(data, uiState, paging)` — 返回 7 个命名 section：`replayHero` / `replayLatestPostmortem` / `replayAdaptivePostmortem` / `replayTransitionPostmortem` / `replayIndependentVersions` / `replayLinkedRead` / `replayHistory`。
- `renderReplayView(data, uiState)` — **问题 #42**：根据三个 postmortem 是否存在做 4 分支级联 ternary 计算 span，可读性差。

**内部助手**
- `replayHeroPill(data)` — 顶部胶囊。
- `renderReplayHistoryActions(item)` — 每条 history 的动作按钮。
- `filterReplayValidations(validations, filter)` — 按 lifecycle_state 过滤。**问题 #43**：过滤 key 是 `lifecycle_state`，但过滤选项包含 `target_and_inventory`，这要求 `normalizedOverlayDecision` 能产生对应的 lifecycle 值，两者之间有未文档化的耦合。
- `renderReplayReconciliationLinkedRead(linked)` — 对账联读区。
- `replayReconciliationNarrative(item)` — 把对账描述讲成一句话。
- `replayLegMismatchMeta(mismatch)`.
- `replayLifecycleTone(state)`.

### 5.8 `ai-view.js`（1103 行）

**AI 分析 view（含 aiHero/aiLatest/aiReview/aiHistory/aiPerformance/aiExecutionSuggestion 六块）**。被 `ai-analysis-view.js` 组合为顶层 view。

**常量**（**问题 #30 / #31 / #32 集中地**）
- `AI_STATE_MAP`（~30 个 key）— 本地再维护一份 state 名→中文，大部分 key 与 TERM_MAP 重合。`humanState` 会在 miss 时 fallback 到 `readableState`，说明 AI_STATE_MAP 几乎是 dead map。
- `AI_ERROR_MAP`（~20 个 key）— 本地再维护一份错误码→中文，与 terms.js ERROR_MAP 大量重合。
- `EXECUTION_SUGGESTION_LABELS`（6 个 key）— 与 detail-drawers.js 和 strategy-view.js 各一份的同名常量 triple source of truth。

**导出函数**
- `renderAIAnalysisSectionCards(data)` — 返回 3 个子卡 `{ aiExecutionSuggestion, aiHistory, aiPerformanceReports }`。
- `hasExecutionSuggestionContent(suggestion)` — 是否值得显示 execution suggestion 卡片。
- `renderAISections(data)` — 返回 6 个命名 section `{ aiHero, aiLatest, aiExecutionSuggestion, aiReview, aiHistory, aiPerformanceReports }`。

**内部助手**
- `humanState(code)` — AI_STATE_MAP → readableState。
- `humanError(code)` — AI_ERROR_MAP → localizeError。
- `executionSuggestionLabel(code)` — EXECUTION_SUGGESTION_LABELS 查表。
- `activeDegradationReasons(downgradeState)` — 当前仍生效的降级原因。
- `reviewResolutionSummary(runtime, latestDegradation)` — review 决策的一行摘要。
- `signedOrFallback(value)` — `formatSigned` 的薄包装。
- `basisPoints(value)` — → "X.X bps"。
- `configuredMode(runtime)` / `effectiveMode(runtime)` — 运行模式提取。
- `toneForRuntime(runtime)` / `toneForShadowSummary(summary)` — tone 辅助。
- `readableShadowMeta(summary)` — shadow 摘要一行。
- `decisionSourceSummary(outcome)` — 决策来源摘要。
- `profileControlSummary(outcome, profileControl)` — 档位控制摘要 `{ value, meta }`。
- `latestDecisionCallout(outcome, assessment, profileControl)` — 最新决策 callout。
- `reviewCallout(blocker, latestDegradation)` — 人工复核 callout。
- `executionSuggestionCallout(suggestion)` — execution suggestion callout。
- `historyCallout(recent)` — 最近决策 history callout。
- `performanceCallout(reports)` — 长期表现 callout。
- `aiRuntimeNarrative(runtime, latestDegradation)` — runtime 讲故事。
- `economicGateRows(assessment, outcome, profileControl)` — 经济门槛 kv rows。
- `executionSuggestionRows(suggestion)` — execution suggestion kv rows。
- `renderPaginationFooter(payload, { key, loadAction, collapseAction })` — **问题 #11 / #33**：与 strategy-view / execution-view 的 `renderPaginationFooter` 同名但签名不同，并硬编码 `limit > 8` 作为 collapse 阈值，不读 `DEFAULT_PAGE_LIMITS`。
- `readableList(items, fallback)` — **问题 #34**：纯 delegator，直接调 `localizeList`。
- `blockerSummary(outcome, assessment)` — 被阻断原因摘要。
- `assessmentCards(recentAssessments)` — 最近评估卡片列表。
- `shadowDecisionCards(shadowRecent)` — 策略层 shadow 决策卡片。
- `shadowEvaluationCards(evaluations)` — 策略层 shadow 评估卡片。
- `performanceReportCards(reports, replayContext)` — 长期复盘卡片；短/中/长窗收益差 + 是否需要复核 + 最近 replay 健康度。
- `renderReviewActions(blocker)` — 复核按钮组；按 `action.kind === "client"` 和 "blocker" 两分支构造 `actionButton`。

### 5.9 `ai-analysis-view.js`（307 行）

**AI 分析 view 的顶层组合**。把 `ai-view.js` 的 section 和本文件的 profile evidence 卡片拼装成整页。

**导出函数**
- `renderAIAnalysisView(data)` — 主入口：`{ aiHero (span-4), aiLatest (span-8), aiReview? (span-12), profileEvidenceCard (span-12), aiExecutionSuggestion? (span-12), aiPerformanceReports (span-12), aiHistory (span-12) }`。

**内部助手**
- `readableProfile(value, fallback)` — profile id → 中文。
- `activeProfileSummary(activeRevision, activation)` — 当前生效档位摘要 `{ value, meta }`。
- `candidateSummary(selection, optimization)` — 候选档位摘要。4 分支：无候选 / 有阻断 / 有 notes / 有 score_delta / fallback。
- `controlStateSummary(state, fallback)` — 乘数 + reasons 摘要。
- `gatingSummary(selection)` — 自动切档闸门摘要（confidence floor / next switch / remaining trades / remaining wins）。
- `fastTrackSummary(selection)` — 紧急安全通道摘要 `{ value, meta, tone }`；三分支：applied / eligible / 未触发。
- `profileEvidenceCallout(controlSummary, evidence, latestCandidate)` — 顶部 callout：3 场景文案（fast-track / safety / cold start / 比较候选档位）。
- `profileEvidenceCard(data)` — 大卡片：顶部 callout + 7 个 summary tile + 8 行 kv-list。

### 5.10 `ai-config-view.js`（461 行）

**AI 配置 view**。左右两列布局：左边"运行模式切换"、右边"自动换档控制"，下面一行"当前生效配置"。

**常量**
- `PROFILE_OPTIONS` — 6 个策略档位（`trend_aggressive` / `trend_normal` / `trend_strict` / `range_defensive` / `high_volatility_defensive` / `execution_degraded_safe`）。
- `MANUAL_MODE_OPTIONS` — 3 个手动模式（`baseline_only` / `ai_assisted` / `ai_decision_maker`）。

**导出函数**
- `renderAIConfigView(data)` — 主入口。错误分支返回单张 `surfaceCard` 放 error callout；正常路径返回 `panel-grid` 布局。

**内部助手**
- `renderManualOperatingModePanel({ runtime, canAdmin })` — 左边卡。
- `renderProfileControlPanel({ runtime, activeRevision, activation, latestProfileControl, latestSelectionDecision, latestOptimizationReport, canAdmin })` — 右边卡。
- `renderProfileControlModeActions({ canAdmin, autoEnabled })` — 右卡顶部的"手动切档 / 自动切档"开关。
- `renderCurrentConfigurationCard({ runtimeProfiles, runtime, aiState, activeRevision, activation })` — 底部"当前生效"卡。
- `runtimeModeSummary(runtime)` — 左卡的 callout 文案 `{ title, copy, actors }`。
- `autoControlSummary(runtime, latestProfileControl, latestSelectionDecision, latestOptimizationReport)` — 右卡的 callout 文案；6 分支：配置手动 / 配置自动但临时手动 / 配置手动但临时自动 / 本轮已切档 / 正在观察候选 / 自动但无动作。
- `executionShadowState(mode)` — 4 分支：`enabled_live` / `shadow_translation` / `diagnostic_only` / 其它 → `{ value, meta, tone }`。
- `currentOperatingMode(runtime)` — 归一化当前运行模式（manual override > effective > configured）。
- `currentStrategyProfile(activeRevision, activation)` — 当前档位 id。
- `readableProfile(value, fallback)` — profile 中文化。
- `readableMode(value, fallback)` — mode 中文化。
- `textOrFallback(value, fallback)` — **问题 #7**：本地重复 `copy.js` 里的 textOrFallback。
- `listText(items, fallback)` — **问题 #6**：第三次重复。
- `summarizeList(items, fallback)` — **问题 #8**：本地重复 `copy.js` 里的 summarizeLocalizedList，但 limit 硬编码为 2。

### 5.11 `admin-view.js`（183 行）

**账户与权限 view**。

**导出函数**
- `renderAdminView(data)` — 主入口。构造：
  - 会话身份卡。
  - 当前用户列表表格。
  - 如果 `canAdmin` 则额外显示 `renderCreateForm(true)`。

**内部助手**
- `renderCreateForm(canAdmin)` — **问题 #41**：创建用户的 `<form id="operatorCreateForm">` 通过 DOM id 绑定事件（在 app.js 的 init 里 `document.getElementById("operatorCreateForm").addEventListener("submit", ...)`），而不是统一的 `data-action` 分发机制。整个前端里其它按钮都走 `data-action`，只有这张表单例外，导致 action 分发不统一。
- `renderUserActions(user, canAdmin)` — 按 viewer/operator/admin 生成"启用/停用 + 改角色 + 改密码 + 删除"四类按钮；权限不足时禁用。

---

## 6. Actions 层 `modules/actions/`

### 6.1 `admin-actions.js`（139 行）

账号 CRUD 的动作工厂。

**导出**
- `createAdminActions({ beginAction, documentRef, renderBanners, refreshDashboard, requestJson, state, windowRef })` — 返回 `{ actionHandlers, createOperatorUser }`。

**内部**
- `findOperatorUser(username)` — 从 `state.data.operatorUsers.users` 里查一个。
- `ensureNotBusy()` — thunk 套 `flash.js` 的 `ensureNotBusy`。
- `createOperatorUser()` — 从 `operatorCreateUsername` / `operatorCreatePassword` / `operatorCreateRole` / `operatorCreateEnabled` 四个 input 读值，校验→`POST /auth/users`→`setFlash("info")`→刷新。
- `toggleOperatorUser(username)` — `PATCH /auth/users/:username` with `{ enabled: !user.enabled }`。
- `updateOperatorUserRole(username)` — **`windowRef.prompt("请输入新的角色：viewer / operator / admin", ...)`** → PATCH。
- `resetOperatorPassword(username)` — **问题 #29**：用 `windowRef.prompt` 让管理员输入新密码（明文、在浏览器的 prompt 对话框里）。对安全标准较高的部署不合适。
- `deleteOperatorUser(username)` — `confirm → ensureNotBusy → beginAction → DELETE → setFlash → refresh`（注释里特别引用了 `app.js` 的 `activateStrategyProfile` 作为这个顺序的"规范范例"）。

**actionHandlers 映射**
- `toggle-user / change-user-role / reset-user-password / delete-user` → 上面四个函数。

### 6.2 `execution-actions.js`（56 行）

订单/成交明细 + 卡单恢复的动作工厂。

**导出**
- `createExecutionActionHandlers({ pageLoadStep, requestJson, renderBanners, openDrawer, runDangerousAction, state, adjustPageLimit, resetPageLimit })` — 返回 6 个 action handler。

**内部**
- `inspectOrder(orderId)` — `GET /orders/:id` → `buildOrderDrawer` → `openDrawer`；失败 setFlash + renderBanners。
- `inspectFill(fillId)` — `GET /fills/:id` → `buildFillDrawer` → `openDrawer`。
- `resolveStuckOrder(orderId)` — `runDangerousAction({ path: "/orders/:id/resolve-stuck-submission", body: { reason: "ui_resolve_stuck_submission", operator_confirmation: "resolve_claimed_submit_as_failed:<orderId>" }, successMessage, confirmMessage })`。

**actionHandlers 映射**
- `inspect-order / inspect-fill / resolve-stuck-order / load-more-orders / collapse-orders / load-more-fills / collapse-fills`。

### 6.3 `risk-actions.js`（454 行）

最大的 action 工厂。含阻断处理、对账、恢复、暂停、试盘评审、退出任务动作、phase1 shadow 抽屉。

**导出**
- `createRiskActionHandlers({ ... })` — 参数很多，见代码头。返回一个 action handler 映射（13 个 action）。

**内部 — 阻断动作映射表**
- `defaultBlockerActionReason(actionId)` — 8 个 action → reason 字符串。fallback 是 `operator_${actionId}`。
- `blockerActionPendingLabel(actionId)` — 8 个 action → pending label（"正在重新对账…"等）。
- `blockerActionSuccessMessage(actionId)` — 8 个 action → 成功提示。
- `blockerActionConfirmMessage(actionId)` — 5 个 action 需要额外 confirm（accept-rebaseline / halt-system / acknowledge-phase1-shadow / ai-review-restore / ai-review-degrade-to-baseline）；其余返回空串表示不需要 confirm。

**内部 — 系统动作**
- `triggerReconciliationValidate(target)` — `POST /reconciliation/validate`。
- `triggerRebaseline(target)` — `runDangerousAction` 走 `/system/rebaseline`。
- `triggerResume(target)` — `POST /system/resume`。
- `triggerHalt(target)` — `runDangerousAction` 走 `/system/halt`。

**内部 — 试盘评审**
- `recordScalingReview(verdict, target)` — 4 个 verdict 映射表 `{ reason, successMessage, pendingLabel, confirmMessage }`。
- `recordTrialReview(target)` — 单个 `"review_snapshot"` 动作的便捷版。
- `recordTrialReviewAction(actionType, target)` — 6 个 actionType 映射表（review_snapshot / reset_trial_guard / continue_small_capital / shrink_trial / pause_trial / approve_scale_up）。

**内部 — 退出任务动作**
- `normalizeExitExecutionParentIntentId(value)` — trim 后空串 → null。
- `exitExecutionActionFlashMessage(result, fallback)` — 把后端返回的 `details.current_blocker_after_action` 拼到成功提示里。
- `runExitExecutionAction({ path, body, successMessage, target, pendingLabel, confirmMessage })` — 共用 runner；**严格遵循"confirm → ensureNotBusy → beginAction"顺序**（**问题 #28**：代码注释引用 `app.js` 里 `activateStrategyProfile` 作为规范范例）。
- `triggerExitExecutionRefresh(value, target)` — `POST /system/exit-execution/refresh`。
- `triggerExitExecutionRetryLimitLookup(value, target)` — `POST /system/exit-execution/retry-limit-lookup`。
- `triggerExitExecutionSafeCancel(value, target)` — `POST /system/exit-execution/safe-cancel`（有 confirm）。

**内部 — 过滤器 / 分页**
- `applyExitExecutionHistoryWorkspaceFilters(target)` — 重置 offset = 0，`syncExitExecutionHistoryFiltersAcrossViews`，同步 URL，刷新，滚动定位。
- `resetExitExecutionHistoryWorkspaceFilters(target)` — 同时重置 risk 和 exitExecution 两侧过滤器。
- `paginateExitExecutionHistory(direction, target)` — 根据 direction 计算 nextOffset（`next` / `prev` / 其它重置为 0），同步 URL，刷新，滚动定位。

**内部 — 抽屉**
- `inspectReconciliation(reconciliationId)` — `GET /reconciliation/:id` → `buildReconciliationDrawer(...)` → `openDrawer`。
- `inspectPhase1Shadow()` — 并发 `GET /system/shadow` + `GET /system/shadow/history?limit=12` → `buildPhase1ShadowDrawer(...)` → `openDrawer`。

**内部 — 通用 blocker action**
- `triggerBlockerAction(value, target)` — 从 `value = "actionId::blocker"` 拆出 actionId 和 blocker → 根据 actionId 查 confirm → 调 runAction 到 `/system/blocker-actions/:actionId`。

**action handler 映射**（返回对象的 key）
- `apply-exit-execution-history-workspace`, `inspect-reconciliation`, `inspect-shadow`, `paginate-exit-execution-history`, `record-scaling-review`, `record-trial-review`, `record-trial-review-action`, `reset-exit-execution-history-workspace`, `trigger-blocker-action`, `trigger-exit-execution-refresh`, `trigger-exit-execution-retry-limit-lookup`, `trigger-exit-execution-safe-cancel`, `trigger-halt`, `trigger-rebaseline`, `trigger-reconciliation-validate`, `trigger-resume`。

---

## 7. 关键机制

### 7.1 refreshPhase 状态机
三种状态：
- `REFRESH_PHASE_IDLE` — 空闲，等待用户或定时器触发。
- `REFRESH_PHASE_PRIMARY` — 主 bundle 请求中。
- `REFRESH_PHASE_DEFERRED` — 主 bundle 已完成，正在补延迟 bundle。

转移：
```
IDLE --(refreshDashboard)--> PRIMARY --(primary done)--> DEFERRED --(all deferred done)--> IDLE
          ^                                                                                    |
          |                                                                                    |
          +------(error / supersede)-------------------------------------------------------+---+
```

状态机本体在 `dashboard-refresh.js` 里；`store.js` 只提供常量和查询。

### 7.2 View 级 Stale-While-Revalidate 缓存
- `state.readyViews: Set<viewKey>` — 哪些 view 已经有第一次数据，可以立即渲染。
- `state.viewRefreshedAt: { [viewKey]: number }` — 每个 view 最后刷新时间戳。
- `VIEW_FRESHNESS_MS = 30_000` — 过期阈值。
- 切 view 时：如果 `readyViews` 命中且 `Date.now() - viewRefreshedAt[view] < VIEW_FRESHNESS_MS`，就**立即渲染缓存数据**，同时在后台启动一次 refresh（SWR）；否则走 `viewIsLoading` 先显示 skeleton。

### 7.3 三层按钮锁
1. **全局锁** — `state.viewIsLoading === true`：整个 view 的所有按钮上 `is-refresh-locked`。
2. **主刷新锁** — `state.isPrimaryRefreshing === true`：同上，但由 `refreshPhase === PRIMARY` 触发。
3. **面板锁** — `state.pendingPanels[panelKey]`：只有带 `data-panel-key` 的 panel 下的按钮上锁，其它 panel 不受影响。
   - `panelKeyAttribute` 用空格拼接多个 key，`panelHasPendingKey` split 空格后逐个检查。
   - `state.pendingGeneration` 用来追踪生成号，旧请求到达时通过比对 generation 决定是否放弃写入。

解锁时不覆盖已经 `disabled=true` 的按钮（保留权限禁用）；`refresh-interactivity.lockButtonForRefresh` 里有专门注释。

### 7.4 Sticky-Flash TTL 协议
- `setFlash` 留空 `_expiresAt`。
- 第一次 render banner 时看到 `_expiresAt == null`，戳 `Date.now() + ttlMs`。
- `isFlashLive` 在 `_expiresAt == null` 时默认视为存活，保证"从渲染开始计时"。
- 这避免了"发 setFlash 后用户刚好在主 bundle 请求中等 8 秒，flash 还没看到就过期"。

### 7.5 用户动作的 confirm → ensureNotBusy → beginAction 顺序
所有 dangerous action 都遵守这个顺序：
1. **先 confirm** — 用户确认之前不动任何状态；用户取消后不必清理。
2. **再 ensureNotBusy** — 在 confirm 对话框弹出期间，可能有另一个动作落地（比如 auto-refresh 刚好触发），所以要在 confirm 之后重新校验一次。
3. **最后 beginAction** — 设 `actionInFlight`、亮 pending 按钮、启动动作。

规范范例在 `app.js::activateStrategyProfile`，`admin-actions.js::deleteOperatorUser` 和 `risk-actions.js::runExitExecutionAction` 都有注释引用。

### 7.6 生成号守护
`state.pendingGeneration` 在每次 `refreshDashboard` 开始时 +1，延迟请求到达时比较 generation，只有等号成立才写 `state.data`，避免"上一轮的迟到数据覆盖当前轮"。

### 7.7 logoutInFlight 独立闩
退出登录有专门的 `state.logoutInFlight` 布尔（不是复用 `actionInFlight`），原因是退出成功后会重定向到 `/login`，此时永远也不会被清除（**问题 #4**：成功路径的泄漏是"故意的"，但代码里没解释）。

### 7.8 cache invalidation 定向失效
`EXIT_EXECUTION_FILTER_AFFECTED_VIEWS = Set(["risk", "exitExecution"])` + `PAGE_LIMIT_AFFECTED_VIEWS` 两个定向集合告诉 `invalidateCachedViews` 应该清哪些 view 的缓存。修改分页或退出任务过滤器只会让这两类 view 重取数据，其它 view 仍然可以复用缓存。

---

## 8. 架构问题 / 代码 bug / 语义混乱 / 逻辑混乱汇总

以下共 **45 条**，按"架构分层 → 重复代码 → 硬编码/魔数 → 局部 bug / 风格"分类。

### A. 分层与导入

| # | 位置 | 描述 |
|---|---|---|
| 1 | `app.js` 行 1 和 3 | `api-client.js` 的 `requestJson` 和 `fetchDashboardBundle` 分两次 import，中间夹着别的 import。虽然 ESM 允许，但同一个模块的符号应合并。 |
| 2 | `app.js` ~行 990 | `window.refreshDashboard = refreshDashboard;` 是调试后门。生产环境应用 `if (process.env.NODE_ENV !== "production")` 或等价守卫包裹；当前实现会让任意脚本/第三方在 DevTools 里随意触发刷新。 |
| 3 | `app.js::dispatchAction` | 对未命中的 `data-action` **静默 fallthrough**，不打 `console.warn`。新增 action 忘记加 case 时没有反馈。 |
| 4 | `app.js::beginLogout` 及状态机 | `logoutInFlight` 在成功路径上"故意"不清除，代码里没有注释解释这是刻意行为；下个维护者看了容易以为是泄漏 bug。 |
| 5 | `detail-drawers.js` 行 38 | `import { ... } from "./views/risk-view.js"` — 基础层反向依赖 view 层，破坏单向分层。 |
| 36 | `exit-execution-view.js` 顶部 import | 从 `./risk-view.js` 反向 import `mergedExitExecutionReviewItems` / `normalizedExitExecutionHistoryFilters` / `renderExitExecutionWorkspace` — view-to-view 水平依赖（类似 #5 的问题）。理想做法是把这三个 helper 上提到 `modules/` 基础层。 |
| 44 | `risk-view.js` 顶部 | `localizeError` / `readableState` 等 helper 被重复 import 多次（至少 5 处 `from "../terms.js"`），应合并。 |

### B. 重复源 / Triple Source of Truth

| # | 位置 | 描述 |
|---|---|---|
| 6 | `strategy-view.js` / `risk-view.js` / `ai-config-view.js` | 三处各有一份 `listText(items, fallback)`，内容几乎相同；应改为统一 import `copy.localizeList`。 |
| 7 | `ai-config-view.js::textOrFallback` | 本地重定义 `textOrFallback`，而 `copy.js` 已经有一份。 |
| 8 | `ai-config-view.js::summarizeList` | 本地重定义 list summarizer，limit 硬编码为 2；`copy.summarizeLocalizedList` 已有参数化版本。 |
| 11 | `strategy-view.js` / `execution-view.js` / `ai-view.js` | 三处各有一份 `renderPaginationFooter`，签名还不一致（ai-view 额外吃 `{ key, loadAction, collapseAction }`）。 |
| 30 | `ai-view.js::AI_STATE_MAP` | 约 30 个 key，与 `terms.js::TERM_MAP` 大量重合。`humanState` 在 miss 时 fallback 到 `readableState`，说明这份本地 map 可删。 |
| 31 | `ai-view.js::AI_ERROR_MAP` | 约 20 个 key，与 `terms.js::ERROR_MAP` 大量重合。 |
| 32 | `detail-drawers.js` / `ai-view.js` / `strategy-view.js` | `EXECUTION_SUGGESTION_LABELS` 在三处各有一份。一改要三处改。 |
| 34 | `ai-view.js::readableList` | 纯 delegator，直接调 `copy.localizeList`，删就行。 |
| 35 | `shadow-drawer.js::kvRow` | 本地自拼 `<div class="kv-row">`，重复 `components.kvList` 的 row markup；class 名改动不会同步。 |
| 45 | `terms.js::operationalStatusLabel` | 10+ 个 `if-return` 串联，典型可表驱动化（改成 map lookup）。 |

### C. 硬编码 / 魔数 / 耦合未文档化

| # | 位置 | 描述 |
|---|---|---|
| 13 | `terms.js::TERM_MAP` | 重复 key：`blocked`（行 14 & 106）、`regime_range`（行 140 & 176）。第二次赋值会覆盖第一次，可能是意外。 |
| 19 | `navigation-state.js::EXIT_EXECUTION_HISTORY_WINDOW_FILTERS` | 硬编码 `[all, 1, 6, 24, 168, 720]` 小时集合。 |
| 33 | `ai-view.js::renderPaginationFooter` | 硬编码 `limit > 8` 作为 collapse 阈值，不读 `DEFAULT_PAGE_LIMITS`。 |
| 37 | `risk-view.js::renderExitExecutionActionFilterOptions` | 硬编码 `["all", "refresh_exchange_state", "retry_limit_lookup", "safe_cancel"]`，与 `navigation-state.EXIT_EXECUTION_HISTORY_ACTION_FILTERS` 重复。 |
| 38 | `risk-view.js::renderExitExecutionActionWindowOptions` | 硬编码 `[1, 6, 24, 168, 720]` 小时值。和 #19 呼应。 |
| 43 | `replay-view.js::filterReplayValidations` | 过滤 key 是 `lifecycle_state`，但过滤选项包含 `target_and_inventory`。这要求 `normalizedOverlayDecision` 产出特定 lifecycle 值。这个耦合没有文档。 |
| 27 | `home-view.js::deferredLoading` vs `store.js::DEFERRED_VIEW_PANELS.home` vs `dashboard-refresh.buildDashboardBundleRequestPlan` | 同一份 panel key 列表在三处各写一次。 |

### D. 局部 bug / 风格 / 难读

| # | 位置 | 描述 |
|---|---|---|
| 9 | `risk-view.js` 行 1510-1516 | `noPrimaryBlockerSummary` 函数体的缩进明显错位（4 空格 vs 2 空格混用），不影响运行但扰乱 diff 阅读。 |
| 10 | `risk-view.js` 行 2 | 只有一个 `kvList` 的独立 import，与下面成块的 import 分开；建议合并。 |
| 12 | `ai-config-view.js::currentOperatingMode` | 已彻底删除：`ai_decision_maker_with_profile_control` 枚举值、UI 翻译、canonical map 条目均已从代码库移除（profile 自动换档独立由 `strategy_profile_auto_control_enabled` 控制）。 |
| 14 | `strategy-view.js::tradeCostConfigRow` | 纯 delegator，没有自己的逻辑。 |
| 15 | `strategy-view.js::escapeFallbackReadableState` | 使用率极低（看起来只剩 1 处调用），可以 inline。 |
| 16 | `strategy-view.js::plainListText` | 定义了但在本文件里找不到调用者（dead code）。 |
| 17 | `strategy-view.js::chooseTrialVerdict` | trivial if-return，可 inline 到调用处。 |
| 18 | `api-client.js::requestJson` | 外部 signal 的初始 `aborted` 分支只调 `controller.abort()` 后继续走 fetch；虽然 `fetch(signal=aborted)` 会立即拒绝，但这条路径没在本地 try 之前先 return，风格上不对称。 |
| 20 | `shell-renderer.js` 的 WeakMap 缓存 | 按 DOM node 缓存 cacheKey，node 一旦被销毁缓存就失效。对典型使用场景 OK，但"切回之前访问过的 view"时，缓存不复用。 |
| 21 | `formatters.js::parseDate` | 用 `String(value).replace("Z", "+00:00")` 处理后端返回的 ISO 时间。这是因为 `new Date("…Z")` 在某些旧浏览器里丢失时区；kludge 但有效。 |
| 22 | `formatters.js::formatNumber` | `Math.abs(number) >= 1000` 时强制降到 2 位小数，调用方写的 `digits` 被覆盖。调用者无法知道"1 000 以上的数字只有 2 位精度"。 |
| 23 | `trade-display.js::orderTableHeaders(scene)` | 不管 scene 参数传什么，返回的表头都一样。要么是 dead parameter，要么忘了分场景实现。 |
| 24 | `navigation-state.js::syncExitExecutionHistoryFiltersAcrossViews` | offset 重置非对称：sourceView !== "risk" 时重置 risk.offset，反之亦然。设计意图是"用户在一侧翻页不影响另一侧"，但没注释。 |
| 25 | `dashboard-refresh.js::handleVisibilityChange` | 条件分支冗余：有一处 `if (!state.autoRefresh) return; else refreshDashboard(...)`，可简化成 `if (state.autoRefresh) refreshDashboard(...)`。 |
| 26 | `dashboard-refresh.js::refreshDashboard` | 捕获后的 `catch` 块是空的（不 `console.error`），诊断信息被吞。 |
| 28 | `risk-actions.js::runExitExecutionAction` 注释 | 引用 `app.js::activateStrategyProfile` 作为"规范范例"，增加阅读跳转成本；应该把规范文档放到独立注释块。 |
| 29 | `admin-actions.js::resetOperatorPassword` | 用 `windowRef.prompt` 让管理员在浏览器 prompt 对话框里输入明文密码，并以明文走 PATCH。对需要满足审计/敏感数据要求的部署不合适，应改为专用表单 + 服务端受控流程。 |
| 39 | `detail-drawers.js::decisionOverlayAuditRows` | 连续两行：一行 `items.slice(0, 3)`，下一行 `items.slice(0, 2)`，预览尺寸不一致且没解释。 |
| 40 | `detail-drawers.js::liveExecutionSummaryText` | `a || b || c !== null && c !== undefined` 依靠 `!==` 优先级高于 `&&` 和 `||` 才能正确工作。语法合法但阅读起来很容易看错，应加括号或拆行。 |
| 41 | `admin-view.js::renderCreateForm` | `<form id="operatorCreateForm">` 通过 DOM id 绑定事件，而非统一 `data-action`。整个前端的其它按钮都走 data-action，这里是唯一例外。 |
| 42 | `replay-view.js::renderReplayView` | span 计算用了 4 分支级联 ternary，跨三个 postmortem 指标；提一个 helper 会更清楚。 |

### E. 补充说明

- 问题 #1-#29 中的相当一部分**不影响运行**，属于"代码整洁"类，可以延后。
- 问题 #2（`window.refreshDashboard`）、#26（`catch {}` 吞错误）、#29（明文密码 prompt）属于**需要尽快处理**的类别（debug 后门、隐藏诊断信息、安全敏感操作）。
- 问题 #5、#36、#44 是**分层纪律**问题，若要长期维护应统一上提 helper 到基础层。
- 问题 #32、#37、#38、#19、#11、#6 是**同一份常量/逻辑在多处硬编码**，修改时容易漏一处。

---

**文档结束** — 若需要进一步拆成多个子文件（例如把"关键机制"挪到独立文档、把"问题清单"挪到 issue tracker），可以告诉我继续。
