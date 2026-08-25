# 35 FS-017 / FS-018 Dashboard 无障碍收敛记录

> 文档状态：现行整改证据  
> 阶段：Phase 3O  
> 核对日期：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作分支：`codex/fs-002-kill-switch-p0`，变更尚未提交  
> 验证边界：HTML/JavaScript/CSS 静态契约、Node 语法检查和 Windows 单元测试；未启动 Dashboard，未执行 WSL2 integration、目标浏览器、键盘-only、NVDA/VoiceOver 或 axe  
> 安全边界：未读取 `.env.*`，未连接数据库、Redis、NATS、交易所或账户，未启动容器，未部署  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 结论

Phase 3O 消除了 `FS-017/018` 中可由当前前端代码确定性修复的路径：

1. 详情抽屉由 `<aside aria-hidden>` + 自制遮罩改为有标题/说明关联的原生 modal `<dialog>`；
2. 所有异步详情 action 显式保留原触发按钮，打开后焦点进入关闭按钮；
3. 关闭按钮、Escape 和 dialog backdrop 都走同一清理路径，关闭后尽可能返回原按钮；
4. 原生 `showModal()` 提供背景 inert 和 Tab 焦点范围，不再由视觉 class 冒充 modal；
5. `prefers-reduced-motion: reduce` 下停止 CSS animation、transition、smooth scroll 与已知 hover 位移；
6. JavaScript 的三处显式 smooth scroll 同样读取 `matchMedia`，偏好 reduce 时改为 `auto`。

当前裁定：

- **FS-017：CODE REMEDIATED / TARGET BROWSER & ASSISTIVE-TECH VERIFICATION OPEN**；
- **FS-018：CODE REMEDIATED / TARGET REDUCED-MOTION VERIFICATION OPEN**。

这不是最终 CLOSED。静态测试不能证明浏览器 top-layer 实际行为、屏幕阅读器输出、缩放、色彩/布局或真实 Operator 工作流。

## 2. 原始缺陷与用户影响

整改前 `dashboard-shell.html` 使用 `<aside id="detailDrawer" aria-hidden="true">` 和独立
`drawerBackdrop`。`app.js` 打开/关闭只切换 class、`aria-hidden` 与 backdrop hidden，
没有 accessible dialog name、`aria-modal`、初始焦点、焦点限制、Escape 统一关闭或返回
触发元素。视觉打开时背景仍可键盘访问，视觉关闭后也没有恢复上下文。

同时，refresh pulse、button spinner、section shimmer 与 skeleton shimmer 都是无限动画；
CSS 全局 smooth scroll、控件 hover 位移、drawer transition 以及 JavaScript 三处显式
smooth scroll 不读取用户的减少动态效果偏好。

可信影响是：键盘/读屏用户无法可靠知道详情已打开或回到原记录；前庭敏感用户即使
已在操作系统选择减少动态效果，仍会看到持续脉冲、旋转、闪烁和位移。

## 3. 实施内容

### 3.1 原生模态语义

`dashboard-shell.html` 当前声明：

- `<dialog id="detailDrawer" role="dialog" aria-modal="true">`；
- `aria-labelledby="drawerTitle"`；
- `aria-describedby="drawerSummary"`；
- 关闭按钮固定 `type="button"`、`aria-label="关闭明细面板"`；
- 移除 `aria-hidden` 状态机和独立 `drawerBackdrop` DOM。

`app.css` 不覆写 closed dialog 的 UA `display` 语义；打开样式只匹配
`.detail-drawer[open].is-open`，遮罩由 `.detail-drawer::backdrop` 提供。dialog 的高度、
默认 margin/padding/border/max-size 被显式归一，保留右侧 drawer 布局。

### 3.2 焦点与关闭生命周期

`openDrawer(payload, triggerElement)` 在所有决策、历史、归因、订单、成交、生命周期、
对账和 shadow action 中接收真实点击 target。请求返回后：

1. 若 dialog 尚未打开，保存原 target；
2. 更新 drawer 内容；
3. 调用 `showModal()`；
4. 加入视觉 open class；
5. 将焦点放到关闭按钮。

Escape 的 `cancel` 事件先 `preventDefault()`，再统一调用 `closeDrawer()`；backdrop
点击以 dialog bounding rect 区分面板内外；关闭按钮复用同一路径。关闭后只有原 target
仍连接 DOM、可见且未 disabled/`aria-disabled` 时才返回焦点，失效引用安全跳过。

缺少 `showModal()` 或调用异常时保留 `open` 属性兼容显示；该 fallback 不具备完整
modal 证明，因此明确排除在目标浏览器放行证据之外。

### 3.3 Reduced motion

`@media (prefers-reduced-motion: reduce)` 对当前及未来 descendant/pseudo-element 统一：

- `animation: none !important`；
- `transition: none !important`；
- `scroll-behavior: auto !important`；
- 已知 badge、tab、link、button 与 toggle hover 不再 `translateY(-1px)`。

加载中的圆点、spinner border、shimmer surface 与 skeleton 结构仍以静态视觉形状和既有
状态文案存在；只去除非必要运动，不隐藏加载/刷新语义。JavaScript 的页面顶部和
exit-execution workspace 三处滚动通过 `preferredScrollBehavior()` 返回 `auto/smooth`。

## 4. 防御性验证

新增 `tests/unit/test_fs017_fs018_dashboard_accessibility.py`，锁定：

1. 原生 dialog、accessible name/description、modal 与关闭按钮属性；
2. 旧 `drawerBackdrop`/`aria-hidden` 不再出现；
3. `showModal()`、初始焦点、Escape、backdrop、return-focus 路径；
4. 九类详情 action 都转交 trigger target；
5. `::backdrop` 与 `[open].is-open` CSS，不覆盖 closed display；
6. reduced-motion 同时禁用 animation/transition/smooth scroll/hover 位移；
7. 四类现有无限动画被通用 pseudo-element 规则覆盖；
8. JavaScript 三处显式滚动不再硬编码 smooth。

Node `--check` 对 `app.js`、execution actions 和 risk actions 均返回 0。现有 Dashboard
render wiring、runtime mode、refresh interactivity、panel error、terms、snapshot 与 login
静态资源回归保持通过。

## 5. 测试记录

### 5.1 新增与相关回归

```text
8 passed, 1 warning in 0.08s
67 passed, 1 warning in 4.88s
```

警告是既存 `.pytest_cache` Windows 创建告警，不是断言失败。

### 5.2 仓库规定的原样全量命令

```text
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q
87 passed, 2 warnings, 1 error in 3.38s
```

唯一 error 发生在 `tmp_path` fixture setup：Windows 系统临时目录
`C:\Users\...\AppData\Local\Temp\pytest-of-...` 返回 `PermissionError [WinError 5]`。
在错误发生前没有 test assertion failure。

### 5.3 仓库内全新 basetemp 复跑

```text
4337 passed, 30 skipped, 1666 warnings, 85 subtests passed in 104.06s
```

30 项 skip 未计作覆盖。1666 条 warning 仍主要是既存 SQLite datetime adapter
deprecation、LongShort poller AsyncMock 未 await 和 `.pytest_cache` 创建告警；继续由
FS-021 测试治理风险承接。

### 5.4 Lint 与语法

```text
.venv\Scripts\python.exe -m ruff check aats/ --fix
All checks passed!

.venv\Scripts\python.exe -m ruff check tests/unit/test_fs017_fs018_dashboard_accessibility.py
All checks passed!

node --check aats/api/static/app.js
node --check aats/api/static/modules/actions/execution-actions.js
node --check aats/api/static/modules/actions/risk-actions.js
exit 0
```

### 5.5 文档与差异检查

```text
变更/新增 Markdown：82 files，367 local links，broken=0
git diff --check：exit 0（仅输出既存 CRLF 转换提示）
```

## 6. 未执行验证

没有运行 WSL2 integration：当前变更尚未提交，标准 Windows→WSL2 同步流程只拉取已提交
代码；手工 rsync/Compose 不属于项目允许路径。没有为了测试而部署或改变运行态。

以下仍为 UNKNOWN：

- 目标 Chromium/Edge 的 dialog top layer、backdrop click 与 focus return；
- 正向/反向 Tab、Escape 和不同详情内容中的 focus order；
- NVDA/VoiceOver 对标题、说明、模态边界和动态内容的实际播报；
- axe/core、200%/400% 缩放、移动 viewport 与高对比模式；
- 操作系统 reduced-motion 开/关时四类动画、drawer、hover 与滚动的视觉结果；
- 自动刷新重绘使原触发按钮离开 DOM 后的人工可用性。

因此不能把 unit/full pass 写成可访问性验收或目标浏览器通过。

## 7. 剩余关闭条件

最终关闭 FS-017/018 至少需要：

1. 在目标浏览器用订单、成交、决策、对账和 shadow 代表流程做 keyboard-only；
2. 验证 forward/reverse Tab 不离开 dialog，Escape/backdrop/按钮都返回合理上下文；
3. 用 NVDA 或 VoiceOver 核对 role、name、description、内容更新和关闭返回；
4. 运行 axe/core，并人工复核其无法覆盖的语义/工作流；
5. 在 100%/200%/400% 缩放、窄屏和高对比模式验证 drawer 内容可见、可滚动、无截断；
6. 切换系统 reduced-motion，观察 pulse/spinner/shimmer/skeleton、hover、drawer 和滚动；
7. 保存浏览器/版本、操作系统、辅助技术版本、用例、结果和问题截图/日志；
8. 对失败项修复后做独立人工复核。

## 8. 当前裁定

已收敛：视觉-only drawer、缺 modal/name/description、缺初始/返回焦点、Escape 绕过统一
清理、自制 backdrop、四类无限动画和显式 smooth scroll 不尊重 reduce 偏好的代码路径。

未收敛：目标浏览器、keyboard-only、辅助技术、axe、缩放/窄屏/高对比和 reduced-motion
实际观察证据。

**FS-017：CODE REMEDIATED / TARGET BROWSER & ASSISTIVE-TECH VERIFICATION OPEN。**  
**FS-018：CODE REMEDIATED / TARGET REDUCED-MOTION VERIFICATION OPEN。**  
**REAL-MONEY PRODUCTION：NO-GO。**
