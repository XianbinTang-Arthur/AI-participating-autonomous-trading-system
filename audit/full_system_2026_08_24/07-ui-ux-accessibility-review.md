# 07 UI、UX 与可访问性审查

## FS-017 — 详情抽屉缺少模态焦点管理

- 严重度：P2；置信度：高；类别：WCAG / keyboard / screen reader
- 状态：VERIFIED（静态）；未做辅助技术实测
- 位置：`dashboard-shell.html:246-257`；`app.js:1294-1310`
- 证据：抽屉是 `<aside aria-hidden>`；打开/关闭只切 class、aria-hidden 与 backdrop。没有 `role="dialog"`/`aria-modal`、焦点移入、焦点陷阱、Escape 关闭或返回触发元素。页面背景仍可键盘访问。
- 触发：键盘或屏幕阅读器用户打开订单、决策、风险等详情。
- 后果：焦点位置与视觉状态脱节，用户可进入隐藏/背景内容，关闭后失去上下文。
- 建议：优先改为原生 `<dialog>` 或实现完整 WAI-ARIA dialog pattern；添加 accessible name、初始焦点、Tab 循环、Escape、return focus 和隐藏背景。

### Phase 3O 当前状态补充

上列证据冻结 Phase 1/2 修复前事实。Phase 3O 已把详情抽屉改为具名、具说明的
原生 modal `<dialog>`；九类异步详情入口显式传递原触发按钮，打开聚焦关闭按钮，
Escape/backdrop/按钮统一关闭并尽可能返回焦点。8 项新增、67 项 Dashboard 相关和
4,337 项全量 unit 通过。尚未执行目标浏览器、keyboard-only、NVDA/VoiceOver、axe、
缩放与移动 viewport 实测，所以当前为 **CODE REMEDIATED / TARGET BROWSER &
ASSISTIVE-TECH VERIFICATION OPEN**，不能标为最终 CLOSED。权威证据见
[35-fs-017-fs-018-dashboard-accessibility.md](35-fs-017-fs-018-dashboard-accessibility.md)。

## FS-018 — 动画没有 reduced-motion 降级

- 严重度：P3；置信度：高；类别：accessibility / comfort
- 状态：VERIFIED
- 位置：`app.css:280,674,1059,2134` 及相关 keyframes
- 证据：refresh pulse、spinner、shimmer、skeleton 均可无限动画；未找到 `prefers-reduced-motion`。
- 后果：对前庭敏感或主动选择减少动态效果的用户不友好。
- 建议：在 reduced-motion 下禁用非必要位移/闪烁，仅保留静态状态变化；不要隐藏加载状态语义。

### Phase 3O 当前状态补充

上列证据冻结修复前事实。Phase 3O 新增 `prefers-reduced-motion: reduce`：停止 CSS
animation、transition、smooth scroll 与已知 hover 位移；JavaScript 三处显式 smooth
scroll 同样读取 `matchMedia` 并降级为 `auto`。静态 loading 形状和文案仍保留。
目标浏览器中实际开关/视觉观察尚未执行，所以当前为 **CODE REMEDIATED / TARGET
REDUCED-MOTION VERIFICATION OPEN**。权威证据同上。

## 已确认的良好实践

- HTML 设置 `lang="zh-CN"`；主导航有 aria-label；状态区有 live region；登录表单与管理表单的输入大多使用显式 label/for。
- CSS 有 `:focus-visible`；按钮使用真实 button，登录密码输入采用 `type=password`。
- runtime 关键确认使用原生 dialog，比自制 div modal 更可靠。
- UI 文案大量使用中文并区分风险、权限、未知、等待和失败，而不是统一显示“加载中”。

## UX 风险

- Dashboard 信息密度极高，operator 需要在 summary、drawer、RDP、risk、replay 多区跳转；应以“能否开仓/为什么不能/当前事实时间/下一安全动作”为首屏层级。
- profile rollback 的 `ok:true` 是最高优先 UX 真实性缺陷；视觉提示不能修复错误后端语义。
- 长查询依赖 deferred panel，必须持续显示 `as_of` 与 stale 标记，避免刷新按钮给出“新鲜”错觉。
- 当前只做静态可访问性审计；色彩对比、缩放 200/400%、屏幕阅读器、触控目标和移动横屏仍为 UNKNOWN。

## 建议验证

对登录、总览、kill/resume、order drawer、recovery review、RDP apply/rollback 建立键盘-only 和 NVDA/VoiceOver 用例；加入 axe/core 静态检查，但人工验证仍不可省略。
