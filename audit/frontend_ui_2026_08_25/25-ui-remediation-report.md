# 25 — AATS 前端 UI/UX 与集成整改报告

> 文档状态：现行实施报告
> 完成日期：2026-08-25
> 起始 Git：`e4954271427554aa4f56f1114827dc15b62932f1`
> 后续状态：整改实现已提交为 `830e25114c7e8fd0eb4b278061336e440b786f88`；部署结论必须以每次模拟栈证据包和现场复验为准

## 1. Overall Assessment

本轮完成了 AATS 全前端架构、页面、布局、信息层级、响应式、无障碍、危险动作和前后端 panel/字段契约审计，并修复所有本轮确认的 P0–P3 缺陷。变更集中在共享 shell、导航、词典、动作处理和两个最小后端展示边界，没有改交易策略、订单状态机、风险限额、数据库 schema 或公共 API 形状。

整改后 11 个受保护路由在 1920、1440、1280、1024、768、390px 共 66 个组合全部同步成功；文档横向溢出、控件文字裁切、可见错误面板、目标内部枚举泄漏均为 0；活动导航项 66/66 可见。前端状态为“实质整改完成”，但尚不是生产发布完成：工作区未提交，标准 derivatives 部署和 WSL 集成环境仍有待恢复。

## 2. Pages Changed / Audited

审计覆盖：

- 独立登录页 `/login`；
- 主页 `/ui`；
- 交易总览 `/ui/overview`；
- 策略判断 `/ui/strategy`；
- 委托与成交 `/ui/execution`；
- 风险与恢复 `/ui/risk`；
- 退出任务 `/ui/exit-execution`；
- 回放与复盘 `/ui/replay`；
- AI 分析 `/ui/ai-analysis`；
- AI 配置 `/ui/ai-config`；
- RDP 治理 `/ui/rdp`；
- 账户与权限 `/ui/settings`；
- 兼容别名 `/ui/ai`。

直接发生展示/交互变更的页面是主页、总览/策略/AI 的风控文案、风险、回放、AI 分析、RDP、账户与权限，以及所有受保护页共享的导航/landmark/断点。

## 3. Major Layout Fixes

1. 主导航从多行换行改为 42px 单行横向滚动；隐藏装饰性滚动条，保留链接键盘访问和部分标签提示。
2. 活动路由在 SPA 切换、深层直达、数据渲染引入垂直滚动条后都重新保证可见。
3. 通用 span 堆叠断点从 1280px 下调到 1100px；1280px 主页高度从约 2,173px 降至约 1,365px。
4. 策略/风险分区导航在移动端改成横向 chip；390px 最大分区导航高度为 36px。
5. 新增跳过导航链接、单一全局 H1、命名 main、body view 同步和 `aria-current=page`。
6. 风险/回放的 long/short/gross/net 面向人文案统一为多头、空头、毛敞口、净敞口。

## 4. Major Mapping Fixes

1. 前端和后端 operator 摘要同时补齐四个合约敞口原因码：long、short、gross、net。
2. AI provider 元数据通过统一状态词典展示；历史摘要只本地化六个已注册策略档位 ID。
3. RDP workbench `blocking_flags` 在后端复用已有 humanizer；前端词典兼容尚未重启的旧进程返回值。
4. 更新过时的 dashboard 集成断言，使生命周期详情动作继续传递触发元素，保证关闭 drawer 后恢复焦点。

## 5. Major UX / Safety Fixes

1. “恢复自动运行”改用危险动作确认链；文案明确系统仍会重新检查暂停、对账、恢复资格和全部风控门禁。
2. 账号启停、角色修改、密码重置均新增最终后果确认；密码值不进入确认消息。
3. 保留后端权限、最后管理员、自操作限制、RDP integrity gate、卡单 claimed-submit gate 和所有审计 reason。
4. 浏览器预览代理只允许 GET/HEAD，拒绝 POST/PUT/PATCH/DELETE；视觉复核期间没有提交交易、治理或账号写操作。

## 6. Files Changed

### 6.1 前端与后端

- `aats/api/static/dashboard-shell.html`
- `aats/api/static/app.css`
- `aats/api/static/app.js`
- `aats/api/static/modules/shell-renderer.js`
- `aats/api/static/modules/terms.js`
- `aats/api/static/modules/actions/risk-actions.js`
- `aats/api/static/modules/actions/admin-actions.js`
- `aats/api/static/modules/views/ai-view.js`
- `aats/api/static/modules/views/ai-analysis-view.js`
- `aats/api/static/modules/views/risk-view.js`
- `aats/api/static/modules/views/replay-view.js`
- `aats/api/rdp_control_summary.py`
- `aats/services/operator/query_service.py`

### 6.2 测试

- `tests/unit/test_dashboard_refresh_interactivity.py`
- `tests/unit/test_dashboard_terms_localization.py`
- `tests/unit/test_fs017_fs018_dashboard_accessibility.py`
- `tests/unit/test_operator_dashboard_read_paths.py`
- `tests/unit/test_rdp_control_summary.py`
- `tests/unit/test_dashboard_admin_action_confirmation.py`
- `tests/unit/test_dashboard_ai_localization.py`
- `tests/integration/test_dashboard_ui.py`

### 6.3 审计与任务文档

- `docs/task/frontend_ui_ux_integration_audit_remediation_sow_2026_08_25.md`
- `audit/frontend_ui_2026_08_25/frontend-page-inventory.md`
- `audit/frontend_ui_2026_08_25/frontend-ui-audit.md`
- `audit/frontend_ui_2026_08_25/22-frontend-ui-audit.md`
- `audit/frontend_ui_2026_08_25/23-frontend-backend-contract-audit.md`
- `audit/frontend_ui_2026_08_25/24-ui-remediation-plan.md`
- `audit/frontend_ui_2026_08_25/25-ui-remediation-report.md`
- `audit/frontend_ui_2026_08_25/screenshots/.gitignore`
- 两个截图目录的 `.gitkeep`
- `audit/full_system_2026_08_24/15-consolidated-risk-register.md`（仅追加本轮对 FS-002/017/018/021 的影响说明，不改变开放状态）

## 7. Backend Changes

仅两项展示边界补充：

- `OperatorQueryService._risk_reason_message()` 增加四个风险原因码；
- RDP workbench item 在返回 `blocking_flags` 前调用现有 `_humanize_reason_entry()`。

它们不改变字段名、JSON 类型、审批条件、数据库、交易算法或风险计算。前端对旧 RDP 字符串的兼容映射允许前后端滚动更新。

## 8. Tests

| 命令/验证 | 结果 |
|---|---|
| `.venv\Scripts\python.exe -m ruff check aats/ --fix` | 通过，`All checks passed!` |
| 7 个本轮目标单测文件 | 81 passed；后续增量回归均通过 |
| `.venv\Scripts\python.exe -m pytest tests/unit/ -x -q -p no:cacheprovider --basetemp <独立临时目录>` | 4,442 passed，30 skipped，94 subtests passed；1,659 条现有 SQLite/Python 3.12 弃用警告 |
| `.venv\Scripts\python.exe -m pytest tests/integration/test_dashboard_ui.py -q -p no:cacheprovider --basetemp <独立临时目录>` | 99 passed |
| `git diff --check` | 通过；仅 Git 提示未来可能做 LF→CRLF 工作区转换 |
| WSL2 同一集成测试 | 未运行：规定的 `~/aats-venv/bin/python` 不存在，WSL 系统 Python 没有 pytest |

第一次完整 unit 命令因用户临时目录权限错误在 87 passed 后中断；改用全新随机 `--basetemp` 后完整通过。第一次 dashboard integration 暴露过时的 lifecycle handler 断言；更新为当前 focus-return 合同后 99 项全绿。

## 9. Visual Verification

| 视口 | 页面数 | 已同步 | 横向页面溢出 | 主导航高度 | 活动标签可见 | 目标泄漏 |
|---:|---:|---:|---:|---:|---:|---:|
| 1920×1080 | 11 | 11 | 0 | 42px | 11/11 | 0 |
| 1440×900 | 11 | 11 | 0 | 42px | 11/11 | 0 |
| 1280×800 | 11 | 11 | 0 | 42px | 11/11 | 0 |
| 1024×768 | 11 | 11 | 0 | 42px | 11/11 | 0 |
| 768×1024 | 11 | 11 | 0 | 42px | 11/11 | 0 |
| 390×844 | 11 | 11 | 0 | 42px | 11/11 | 0 |

浏览器对 1280px 与 390px 全部页面逐页触发截图检查，并对主页、策略、风险、AI 分析、RDP、账户权限重点人工目视。没有把 PNG 持久化进仓库，因为其中可能包含模拟账户金额、决策 ID 和运维用户名。

最终渲染使用临时 `127.0.0.1:8765` 只读前端预览源读取当前工作区并代理 GET 到 8001 derivatives 后端。它证明“当前前端工作区 + 当前模拟后端”的组合，不证明 8001 已部署新前端。

## 10. Remaining Issues

1. **部署缺口**：未创建 commit，因而没有按标准部署入口更新模拟栈；8001 仍是整改前静态资源。
2. **WSL 测试环境**：`~/aats-venv` 缺失；需要先恢复规定 venv，再在 WSL 重跑 dashboard integration。
3. **非空数据分支**：当前没有成交、活跃委托、退出任务和 replay 父腿样本，详情/危险动作需要可审计模拟数据复验。
4. **辅助技术**：NVDA/JAWS/VoiceOver、高对比度、200%/400% zoom 未完成。
5. **最终 console/HAR**：基线 console 为 0；最终浏览器控制接口没有可用导出能力，部署后仍应人工检查 DevTools。
6. **生产边界**：未验证 live profile、真实交易所、真实资金或生产负载。

## 11. Final Frontend Status

**Substantially remediated（实质整改完成），但尚未完成部署级上线验收。**

进入上线测试前仍需：创建/审核提交 → 恢复 WSL venv → 标准 derivatives 部署 → 8001 六视口抽查与 console/network 检查 → 用非空模拟样本复核执行/退出/replay 分支。不得从当前报告跳过这些门禁直接推导生产就绪。
