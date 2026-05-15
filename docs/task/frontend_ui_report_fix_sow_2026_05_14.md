# 前端深度扫描报告修复 SOW

## Status
- 状态：实施中
- 日期：2026-05-14

## Background
前端深度扫描报告指出三类需要修复的问题：

1. `/ai-config/summary` 在真实登录态下稳定返回 `Internal Server Error`。
2. RDP 治理观察卡片把机器诊断串直接显示给操作员。
3. 已完成观察的 release 仍显示“运行观察”，语义不清。

## Current Behavior
- AI Config 汇总接口会调用最新决策详情；当执行仓储的订单或成交查询返回 `None` 时，真源链遍历行集直接抛出 `TypeError`。
- RDP 观察卡片的 `effectiveness.detail` 直接输出原始 `key=value` 诊断串。
- 观察已完成后仍复用首次观察按钮文案。

## Plan
1. 在 operator query service 中把订单、成交仓储 `None` 结果归一为空集合，保留异常路径的原有错误码。
2. 在 RDP 控制面板中把观察建议和效果诊断转成中文操作员文案。
3. 已完成或已给出观察结论的 release 将按钮显示为“重新运行观察”，保留管理员权限与真源过期的禁用逻辑。
4. 增加单元测试覆盖后端空行集，增加前端集成测试覆盖中文化与按钮语义。

## Risks
- 订单或成交行集为 `None` 时会被视为“没有仓储记录”，因此页面可用性优先于暴露接口不规范。真实异常仍通过已有 `except` 分支返回 lookup 失败。
- RDP 诊断串只对结构化 `key=value; ...` 格式做中文化，未知非结构化文本继续走通用本地化兜底。

## Validation
- `.venv\Scripts\python.exe -m ruff check aats/ --fix`
- `.venv\Scripts\python.exe -m pytest tests/unit/test_operator_decision_truth_chain.py -q`
- WSL2 前端集成测试：`pytest tests/integration/test_dashboard_ui.py -q`
- 部署后登录态浏览器 smoke：`/ai-config/summary` 与 RDP 治理页面。
