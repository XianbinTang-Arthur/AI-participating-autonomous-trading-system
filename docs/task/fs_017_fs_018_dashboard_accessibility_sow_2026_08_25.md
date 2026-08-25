# FS-017 / FS-018 Dashboard 无障碍收敛设计与实施范围

> 文档状态：Phase 3O 已实施 / 目标浏览器与辅助技术验证开放  
> 最后核对：2026-08-25（起始代码基线 `00b6df0f8a8d2665d6cae3e88996843767cd1f56`）  
> 当前工作区：`codex/fs-002-kill-switch-p0`，包含尚未提交的 Phase 3A–3O 整改  
> 核对范围：当前 Dashboard shell、详情抽屉控制、CSS 动画/滚动与相关静态测试  
> 运行时边界：未读取 `.env.*`，未启动 Dashboard、数据库、交易所、账户、Docker 或 WSL2 运行态  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 业务目标与边界

本阶段收敛 `FS-017` 与 `FS-018`：让订单、成交、决策、对账等详情抽屉遵循可验证的模态对话框契约，并在用户启用 `prefers-reduced-motion: reduce` 时停止非必要持续动画、位移过渡和平滑滚动。

本阶段只修改浏览器展示与交互壳层，不改变交易决策、风控、执行、API、认证、数据库、部署或运行参数。代码和静态测试通过不能替代目标浏览器、键盘、NVDA/VoiceOver 与 axe 实测，因此两项风险只能更新为代码级收敛、人工验证仍开放。

## 2. 整改前行为与根因

本阶段开始时的代码事实如下：

1. 详情抽屉使用 `<aside aria-hidden>` 和独立 backdrop；
2. 打开/关闭只切换 class、`aria-hidden` 与 backdrop hidden；
3. 没有原生 modal、accessible name/description、初始焦点、Tab 限制、Escape 关闭或关闭后返回触发元素；
4. 背景页面仍可被键盘访问；
5. refresh pulse、pending spinner、section shimmer、skeleton shimmer 都是无限动画；
6. CSS 平滑滚动、hover 位移、抽屉过渡和 JavaScript 显式 smooth scroll 没有 reduced-motion 分支。

根因是视觉状态被当作交互语义，且动效实现没有统一消费操作系统偏好。

## 3. 模块职责与领域模型

| 模块 | 本阶段职责 |
|---|---|
| `dashboard-shell.html` | 使用原生 `<dialog>` 声明详情模态、标题/说明关联和明确关闭按钮 |
| `app.js` | 以 `showModal()` 打开；处理 Escape/backdrop/按钮关闭、初始焦点和返回焦点；按 reduced-motion 选择滚动行为 |
| `app.css` | 适配 dialog top layer/`::backdrop`；在 reduced-motion 下移除无限动画、非必要 transition/hover 位移和平滑滚动 |
| 单元测试 | 静态锁定 HTML/JS/CSS 无障碍契约，防止退回视觉-only drawer |

`drawerReturnFocusElement` 只保存本次打开抽屉的触发元素引用，不持久化、不跨页面恢复；元素已离开 DOM 或不可用时安全跳过。

## 4. 输入/输出接口

`openDrawer(drawerPayload, triggerElement)` 在现有 drawer payload 后增加可选触发元素参数。现有单参数调用保持兼容；所有 `data-action` 详情入口应显式转交事件 target。

DOM 契约：

- `detailDrawer` 为 `<dialog role="dialog" aria-modal="true">`；
- `aria-labelledby="drawerTitle"`；
- `aria-describedby="drawerSummary"`；
- `closeDrawerButton` 是 `type="button"` 且有清晰可访问名称；
- 不再维护第二套 `drawerBackdrop` 或 `aria-hidden` 状态。

无 API、JSON 或后端接口变化。

## 5. 数据库 schema、表、索引与约束

无数据库 schema、migration、table、index 或 constraint 变更；不读取或写入任何数据库。

## 6. 事务、一致性与并发

无数据库事务。异步详情请求完成后才打开 dialog；触发元素随请求参数传递，避免页面当前焦点在等待期间变化导致返回目标错误。

重复打开已打开 dialog 不重复调用会抛异常的 `showModal()`；关闭路径清理 class 和返回焦点引用，失效 DOM 引用不得抛错。

## 7. 授权、认证与数据安全

无授权或认证变更。详情请求继续沿用现有 session/API 权限。本阶段不读取、记录或显示 `.env.*`、密码、token、账户、余额、仓位或订单运行态。

## 8. 错误处理与幂等

- dialog 节点缺失：函数安全返回；
- `showModal()` 不可用：保留 `open` 属性兼容显示，但不得把该 fallback 记为目标浏览器无障碍验证通过；
- dialog 已打开：更新内容并将焦点重新置于关闭按钮，不重复 show；
- Escape：阻止浏览器绕过统一清理路径，再调用 `closeDrawer()`；
- backdrop 点击：仅点击 dialog 面板边界外时关闭；面板内部点击不关闭；
- 触发元素已断开、disabled 或 hidden：不尝试返回焦点。

## 9. 状态转换与生命周期

```text
closed --openDrawer(payload, trigger)--> modal open + close button focused
modal open --close button/Escape/backdrop--> closed + trigger focus restored
modal open --openDrawer(new payload)--> modal remains open + content/focus refreshed
```

原生 modal 负责背景 inert 与 Tab 焦点约束；应用只负责统一打开/关闭和返回焦点。

## 10. 缓存与性能

无缓存变化。仅增加常数次 DOM 状态检查、focus 和 media-query 检查，对 Dashboard 请求量与渲染复杂度没有实质影响。

## 11. 日志、监控与审计

不新增包含业务数据的日志。审计记录必须区分：

- 静态 DOM/JS/CSS 契约与单元测试已通过；
- 目标 Chromium、缩放、键盘-only、NVDA/VoiceOver、axe 与真实 operator 流程仍未执行；
- reduced-motion CSS/JS 分支存在不等于已在目标浏览器观察验证。

## 12. 测试策略

新增静态契约测试覆盖：

1. drawer 使用具名、具说明的原生 modal dialog；
2. 旧 `aria-hidden`/自制 backdrop 被移除；
3. 所有详情 action 将 target 传给 drawer；
4. `showModal()`、初始焦点、Escape、backdrop 与返回焦点路径存在；
5. CSS 使用 `::backdrop` 且不覆盖 closed dialog 的 UA 隐藏语义；
6. `prefers-reduced-motion: reduce` 禁用四类无限动画、transition/hover 位移与 smooth scroll；
7. JavaScript 显式滚动读取同一 media query。

运行新增测试、Dashboard 相关 unit、全量 unit 和 Ruff；浏览器/辅助技术测试保持开放并明确列为下一门禁。

## 13. 迁移、回滚与兼容

不需要数据迁移。现代目标浏览器使用原生 dialog；缺少 `showModal()` 的环境仅作非模态显示兼容，不能据此放行可访问性验收。

回滚只涉及 HTML/JS/CSS 与测试，但回退到 `<aside aria-hidden>` 会重新引入已确认风险，不应作为生产回滚方案。

## 14. 配置与环境隔离

不新增环境变量。reduced-motion 直接读取浏览器 `matchMedia("(prefers-reduced-motion: reduce)")` 和 CSS media query，不建立应用内第二套偏好。

本阶段验证不启动服务，不连接 WSL2/Docker/Redis/NATS/Postgres/交易所，不访问 `.env.*`。

## 15. 代码组织与依赖

预计修改：

- `aats/api/static/dashboard-shell.html`；
- `aats/api/static/app.js`；
- `aats/api/static/app.css`；
- `aats/api/static/modules/actions/execution-actions.js`；
- `aats/api/static/modules/actions/risk-actions.js`；
- 新增 `tests/unit/test_fs017_fs018_dashboard_accessibility.py`；
- 更新现行静态资源说明和全系统审计台账。

不新增第三方依赖，不引入自制 focus-trap 库。

## 16. 文档、运维手册与验收标准

本阶段代码级验收：

- 详情抽屉是 accessible-name/description 完整的原生 modal dialog；
- 打开后焦点进入关闭按钮，Tab 由 modal 限制在 dialog 内；
- 关闭按钮、Escape、backdrop 均走同一关闭路径；
- 关闭后尽可能返回原始触发元素；
- reduced-motion 下无限动画、抽屉/控件位移、CSS/JS smooth scroll 停止，加载状态仍以静态形状和文案存在；
- focused、Dashboard related、full unit、Ruff、文档链接和 diff check 通过，或准确披露环境阻塞；
- `FS-017/018` 不标记为最终 CLOSED，真实资金生产继续 NO-GO。

最终关闭仍需在目标浏览器完成：键盘-only（正向/反向 Tab、Escape、返回焦点）、200%/400% 缩放、NVDA 或 VoiceOver、axe/core、reduced-motion 开/关、订单/成交/决策/对账等代表性详情流程和移动 viewport 人工验证，并保存可复核证据。

实施与验证结果见
[`35-fs-017-fs-018-dashboard-accessibility.md`](../../audit/full_system_2026_08_24/35-fs-017-fs-018-dashboard-accessibility.md)。
