# 23 — AATS 前端/后端数据契约审计

> 文档状态：现行契约审计结论
> 核对日期：2026-08-25
> 起始 Git：`e4954271427554aa4f56f1114827dc15b62932f1`；整改实现提交：`830e25114c7e8fd0eb4b278061336e440b786f88`
> 范围：Operator UI、`/dashboard/bundle`、页面所用只读 endpoint 与受控 mutation；不覆盖策略算法或实盘正确性证明

## 1. 数据流真相

当前主链路为：

`view-router.js 路由` → `store.js::viewSpecs()` 选择 panel/endpoint → `buildDashboardBundlePath()` → `GET /auth/dashboard/bundle` → 后端 `_protected_dashboard_panel_payload()` 分发 → `dashboard-refresh.js` 按 generation 写入 `state.data/errors/panelMeta` → view renderer → `components.js`/`terms.js`/`formatters.js` → DOM。

前端没有独立 TypeScript model 层，也没有 WebSocket 客户端。契约由 panel key、JSON 字段访问、统一 formatter/词典及单元/集成测试共同约束。公共 endpoint 仍可直接调用，但正常页面刷新走 bundle，以减少重复认证和请求瀑布。

## 2. 公共 panel 契约

| 前端 panel | 后端 endpoint | 关键后端字段 | 前端使用 | 审计结论 |
|---|---|---|---|---|
| `session` | `/auth/session` | authenticated、username、role、write/admin capability | 会话、角色、动作权限 | 一致；不在 UI 中显示凭证 |
| `authProviders` | `/auth/providers` | session/API key/database user capability | 登录/权限能力说明 | 一致 |
| `health` | `/system/health` | overall/operational state、halted、checks、freshness | 顶栏/主页运行健康 | 一致；状态不只靠颜色 |
| `mode` | `/system/mode` | trading mode/profile | 主页/非 risk 页模式说明 | risk 有意排除，避免重复大 payload |
| `runtime` | `/system/runtime` | environment、execution path、profile | 模拟/实盘文字、执行线路 | 一致；本轮只验证 derivatives 模拟 |
| `systemRecovery` | `/system/recovery` | safe_to_trade、reasons、only_reduce、manual_review | 恢复资格、风险页 | 一致；失败关闭语义保留 |
| `blockerControl` | `/system/blocker-control` | blockers、priority、actions、permissions | 首要问题、建议动作、禁用原因 | 一致；动作仍受后端权限和门禁约束 |
| `aiRuntime` | `/ai/runtime` | configured/effective mode、provider/outcome state | 全局运行模式 badge、AI 页 | 一致；不从 `/system/runtime` 推测 AI 模式 |

## 3. Page → Endpoint → Field → Render 映射

“未展示字段”只记录对操作员有潜在意义或需要解释的类别，不要求把全部原始 payload 暴露到 UI。

| 页面 | 后端 endpoint/panel | 关键后端字段 | 前端字段/组件 | 未展示但有意保留在后端 | 映射问题与结果 |
|---|---|---|---|---|---|
| 主页 | blockers、metrics、portfolio、decision/latest、execution/latest、reconciliation/latest、account/state | trading status、equity、exposure、decision action、order state、reconciliation、freshness | `home-view.js` 状态总览、账户、最新动作 | 完整原始 payload、全部历史行 | 恢复动作原缺确认，已修复；没有字段缺口 |
| 总览 | 主页 panel + positions、strategy/runtime | balance/equity、position qty/notional、strategy runtime、latest activity | `overview-view.js` 状态条、资产/仓位/关注事项 | 低层执行诊断、完整策略配置 | 风控原因码原分词显示，已修复 |
| 策略 | strategy/runtime、strategy attribution、lifecycle attribution、decision recent、trial review | candidate/action/reason、allocation、PnL/fee/funding、lifecycle、trial evidence | `strategy-view.js` 四段工作区、表格、详情入口 | 原始事件/配置 JSON 仅在排障详情 | 四类风控码已补；family/decision ID 在审计位置有意保留 |
| 执行 | decision、metrics、execution、orders、fills、lifecycle、errors | order id/state/qty/price/fill、fee、timestamps、execution errors | `execution-view.js` 表格/移动卡/详情 | 订单原始 exchange payload | 契约一致；当前无非空样本，不能声称逐字段目视覆盖 |
| 风险 | metrics、portfolio、positions、account、reconciliation、trial guard、preflight、run packet、replay、exit history | halted/safe_to_trade、gross/net/long/short exposure、limits、breaches、snapshot freshness、reconciliation | `risk-view.js` 任务/恢复/敞口/对账分区 | 低层调试和完整 run packet JSON 折叠 | long/short 文案和恢复确认已修复；数值字段未改 |
| 退出任务 | exitExecutionActionHistoryPage | action、parent、actor、status、created/result、paging | `exit-execution-view.js` 筛选、分页、记录卡 | 完整 payload | 契约一致；当前样本为空 |
| 回放 | replay/status、recent-validations、reconciliation/latest | lifecycle、parent/leg mismatch、validation result、timestamps | `replay-view.js` 摘要、筛选、联读 | 原始 replay 诊断 | 英文腿级空态已修复；当前父腿样本为空 |
| AI 分析 | ai/overview、runtime、latest、shadow、recent、profile-control summary | provider/model、decision source、edge/cost、shadow result、profile candidate/control | `ai-view.js`、`ai-analysis-view.js` | 模型原始响应、完整 prompt、未注册自由文本 | provider/档位 ID 泄漏已修复；只转换六个已注册档位 |
| AI 配置 | ai-config/summary、ai/runtime | mode capability、profile registry、active/control state、permissions | `ai-config-view.js` 模式和档位工作区 | 原始配置树 | 一致；没有提交模式变更 |
| RDP | control-summary、workbench overview/items/alerts、tuning | combo、recommendation、integrity、blocking flags、actions、release/tuning state | `rdp-view.js`、`rdp-control-panel.js` | 完整 research artifacts | blocking flag 原绕过 humanizer，现后端+前端兼容修复 |
| 账户权限 | auth/users | username、role、enabled、capability、self/last-admin constraints | `admin-view.js` + `admin-actions.js` | password hash/secret 永不返回 | 字段一致；三类高影响 PATCH 新增确认 |

## 4. 关键业务字段链路

| 业务事实 | 后端真相 | 前端变换 | 渲染位置 | 验证 |
|---|---|---|---|---|
| 是否可交易/暂停 | health、systemRecovery、blockerControl | `tradingStatusLabel()`、recovery narrative | 主页、风险 | 真实模拟 bundle + 单测 |
| Kill Switch/恢复资格 | halted、safe_to_trade、reasons、actions | 统一状态词典和 blocker controls | 主页、风险 | 恢复确认与状态分离；未触发实际恢复 |
| 模拟/实盘环境 | runtime environment/profile | 运行环境中文标签 | 全局/主页 | 当前明确显示演示合约/演示环境 |
| 账户权益/余额 | portfolio/account snapshots | decimal formatter，不填默认业务值 | 主页、总览、风险 | 真实模拟数据目视；不在文档复制数值 |
| 多头/空头/毛/净敞口 | positions/portfolio/risk payload | number formatter + 中文口径 | 风险、总览 | 文案修复；数值和单位未改 |
| 委托状态/数量/价格 | orders/execution | `toneForOrderStatus()`、trade display helpers | 执行、详情抽屉 | 代码/测试覆盖；当前无活跃样本 |
| 成交手续费与盈亏 | fills/attribution | 费用成本负号/返佣正号、decimal formatter | 执行、策略归因 | 既有金融符号测试保持通过 |
| 策略状态/来源 | strategy runtime/decision | family/state/action humanizer | 策略、总览 | family ID 在审计字段保留，面向人摘要中文 |
| AI provider/决策来源 | ai latest/runtime | `readableState()` | AI 分析、全局 badge | `baseline_fallback` 已本地化测试 |
| RDP 完整性/审批资格 | workbench items/alerts | humanized blocking flags、disabled reason | RDP | 后端 payload 和旧进程兼容测试 |
| 时间与 freshness | created/updated/snapshot timestamps | absolute + relative formatter | 全页面 | 30 秒轮询；陈旧与无数据不混同 |

## 5. Mutation 契约与安全边界

| UI 动作 | Endpoint | 权限/后端门禁 | 前端确认/输入 | 本轮处理 |
|---|---|---|---|---|
| 恢复自动运行 | `POST /system/resume` | operator write + recovery/kill switch/risk validation | 明确二次确认；reason=`ui_manual_resume` | P0 修复 |
| 暂停自动运行 | `POST /system/halt` | operator write | 既有危险确认 | 保持 |
| 重建基线 | `POST /system/rebaseline` | profile/support/recovery constraints | 既有覆盖旧基线确认 | 保持 |
| 对账校验 | `POST /reconciliation/validate` | operator write | 明确进行中/成功/错误反馈 | 保持 |
| 卡单恢复 | `/orders/{id}/resolve-stuck-submission` | 后端 claimed-submit 二次门禁 | 两阶段危险确认 | 保持 |
| AI 模式/档位 | AI config endpoints | admin/governance/profile registry | 专用编辑态和确认 | 保持；未执行 |
| RDP 审批/发布/回滚 | RDP action endpoints | token、integrity、release、admin gate | disabled reason + 危险确认 | 保持；未执行 |
| 账号启停 | `PATCH /auth/users/{username}` | admin、自停用/最后管理员约束 | 新增后果确认 | P1 修复 |
| 改角色 | 同上 | admin/role validation | 表单 + 新增角色变更确认 | P1 修复 |
| 重置密码 | 同上 | admin/password policy | password input + 不含密码的确认 | P1 修复 |
| 删除账号 | `DELETE /auth/users/{username}` | admin、自删除/最后管理员约束 | 既有删除确认 | 保持 |

前端确认不能替代后端门禁；本轮没有删除、放宽或复制任何后端授权逻辑。

## 6. 已确认并修复的契约缺陷

### 6.1 四类合约敞口原因码

后端治理引擎会输出：

- `risk_max_long_notional_exceeded`；
- `risk_max_short_notional_exceeded`；
- `risk_max_gross_notional_exceeded`；
- `risk_max_net_notional_exceeded`。

整改前操作员词典和 `_risk_reason_message()` 未覆盖，导致 UI 进入通用 machine-token 分词。整改后前端 `ERROR_MAP` 与后端 operator message 均有明确中文语义，原始 code 仍保留在结构化字段中供审计。

### 6.2 AI provider 与策略档位

`provider_name` 和 `operator_summary` 可能带 `baseline_fallback`、`trend_aggressive` 等枚举。整改后 provider 经统一状态词典；历史摘要只替换六个注册档位 ID，未知内容保持原文，避免把任意审计 ID 误改成业务文案。

### 6.3 RDP blocking flags

`_build_workbench_items_payload()` 原直接复制 `combo_state.inconsistencies`。整改后同文件已有 `_humanize_reason_entry()` 被应用到输出；前端 `localizeError()` 同时兼容尚未重启的旧后端进程。字段名、数组形状和门禁逻辑均未改变。

## 7. 信息完整性分类

### 7.1 后端已有且已展示

交易资格、环境、账户权益、敞口、仓位、活动委托数、策略结论、风险/恢复、exchange/freshness、AI/RDP 状态、权限和操作失败原因均有明确 UI 位置。

### 7.2 后端已有但有意不直接展示

原始事件 JSON、模型原始响应/prompt、exchange 原始 payload、所有历史行、内部数据库字段和低层诊断只在详情或折叠排障区保留。原因是它们会降低首屏可扫描性，且不应替代后端审计存储。

### 7.3 部分展示

长列表只展示分页窗口；历史/归因用摘要 + 详情；RDP 只展示本轮相关证据；这些是明确的信息层级，不是字段遗漏。

### 7.4 原显示错误

四类敞口 code、AI provider/档位 ID、RDP mixed-language flag、风险/回放英文腿级术语已修复。

### 7.5 后端当前没有或运行样本缺失

当前模拟盘没有成交、活跃委托、退出任务和 replay 父腿样本。UI 展示诚实空态，不生成假数据。本轮没有发现必须新增后端字段或数据库 schema 才能正确展示的 P0/P1 缺口。

## 8. Backend Changes

| 文件 | 变更 | 理由 | 兼容性 |
|---|---|---|---|
| `aats/services/operator/query_service.py` | `_risk_reason_message()` 增加四个敞口原因码 | operator 摘要不再回退原始 code | 仅增加映射，不改字段/schema |
| `aats/api/rdp_control_summary.py` | workbench `blocking_flags` 输出前调用 `_humanize_reason_entry()` | 复用现有业务词典，避免 mixed-language | 字段名、类型、门禁不变 |

没有迁移、表、索引、缓存协议、交易逻辑或公共 endpoint 变化。

## 9. Validation Evidence

- panel/request plan 与 render wiring 既有测试通过；
- 新增前端原因码、AI 文案、账号确认、导航/landmark 契约测试；
- 新增后端 operator reason 与 RDP blocking flag 测试；
- 真实模拟 bundle 在 66 个页面/视口组合全部进入已同步，目标内部泄漏检查为 0；
- 临时预览源拒绝所有写方法，所以浏览器视觉复核不会提交交易、治理或账号变更。

## 10. Remaining Contract Unknowns

- 当前无数据分支尚未用真实模拟记录逐字段目视核对；
- 浏览器能力未提供最终 HAR/console 导出，不声称逐请求证明零重复/零失败；
- 工作区尚未提交和标准部署，8001 当前静态资源不能作为整改后证据；
- 本文不证明 live profile、真实交易所或生产负载状态。
