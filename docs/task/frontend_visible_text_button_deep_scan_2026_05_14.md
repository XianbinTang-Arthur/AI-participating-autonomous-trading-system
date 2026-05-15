# 前端可见文字 / 按钮 / 展示深度扫描报告（登录态浏览器）

扫描时间：2026-05-14  
环境：`https://127.0.0.1:8011`，`derivatives-live`，Codex in-app browser 登录态  
当前身份：`admin` / 管理员  
方式：逐页打开主导航 11 个页面，采集浏览器 DOM snapshot、按钮/链接/输入状态、控制台日志和关键接口/网关日志。未点击任何会改变实盘状态的按钮。

证据目录：`artifacts/ui-deep-text-scan-2026-05-14/`

## 总体结论

大部分页面的前端文字、按钮和展示状态可读，且上轮修复项仍然生效：

- 未再发现按钮级旧文案：`恢复运行`、`暂停运行`、`AI分析`。
- 未发现 `unsupported-client-action` / `unsupported-rdp-action`。
- 11 个主导航页面均保持登录态，没有跳回登录页。
- 账户与权限页识别到当前管理员、当前唯一启用管理员，并正确禁用 `停用` / `改角色` / `删除`。

但本次深扫发现 3 个需要处理的问题：

1. **P1：AI 配置页稳定不可用，直接后端接口 500。**
2. **P2：RDP 治理页存在英文 key-value 诊断串直接展示，中文前端语义不干净。**
3. **P2/P3：RDP 已观察完成的卡片仍展示 `运行观察`，语义容易误导；`执行回滚` 是否应直接可点需要结合发布状态再确认。**

## 页面统计

| 页面 | DOM 行数 | 按钮数 | 禁用按钮 | 链接数 | 标题数 | 主要结果 |
|---|---:|---:|---:|---:|---:|---|
| 主页 | 126 | 6 | 0 | 11 | 9 | 正常 |
| 交易总览 | 217 | 7 | 0 | 11 | 9 | 正常 |
| 策略判断 | 701 | 17 | 0 | 15 | 16 | 正常 |
| 委托与成交 | 355 | 21 | 0 | 11 | 6 | 正常 |
| 风险与恢复 | 390 | 9 | 2 | 16 | 23 | 正常 |
| 退出任务工作台 | 87 | 7 | 2 | 11 | 5 | 正常 |
| 回放与复盘 | 81 | 7 | 0 | 11 | 4 | 正常 |
| AI 分析 | 601 | 5 | 0 | 11 | 12 | 首次快速采集出现骨架，单独重载后正常 |
| AI 配置 | 42 | 2 | 0 | 11 | 4 | 异常：稳定读取失败 |
| RDP 治理 | 259 | 20 | 0 | 11 | 19 | 有中英混排和操作语义问题 |
| 账户与权限 | 135 | 9 | 3 | 11 | 9 | 权限禁用正确 |

## 发现 1：AI 配置页稳定不可用

可见表现：

- 页面显示 `读取失败`。
- 页面主卡显示 `AI 配置暂时不可用`。
- 说明文案为 `现在还拿不到配置摘要。`
- 详情为 `dashboard / snapshot / refresh / failed`。

浏览器复核：

- `AI 分析` 页面单独重载后正常渲染完整数据。
- `AI 配置` 页面单独重载后仍稳定显示失败。
- 登录态仍是管理员，不是权限不足。
- `aiRuntime` panel 正常，说明不是 AI 总体状态完全不可用。

接口证据：

- 直接打开 `/ai-config/summary` 返回 `Internal Server Error`。
- `/dashboard/bundle?...view=aiConfig...panel=aiConfigModel` 返回 200，但 `aiConfigModel.error = dashboard_snapshot_refresh_failed`，`data` 只剩默认空壳 `{ "ai": {} }`。

后端日志定位到的异常链路：

- `aats/api/routes/ai.py:29`
- `aats/services/operator/query_service.py:6913`
- `aats/services/operator/query_service.py:6894`
- `aats/services/operator/query_service.py:10627`
- `aats/services/operator/query_service.py:4820`
- `aats/services/operator/query_service.py:3807`

根因信号：

`_decision_fill_payloads_from_repo(decision_id)` 内部得到 `rows = None`，随后执行 `for row in rows` 抛出 `TypeError: 'NoneType' object is not iterable`。

影响：

- 管理员无法在 AI 配置页查看运行模式切换、自动换档控制、当前运行参数。
- 页面把错误描述为“检查登录状态和后端接口”，但真实登录/权限是正常的，属于前后端错误归因不准确。
- Dashboard snapshot plane 对 `aiConfigModel` 持续刷新失败，页面只能使用默认空壳。

建议修复：

- 在 `_decision_fill_payloads_from_repo` 对 repository 返回 `None` 做空列表归一化。
- 或在 `ai_config_summary_with_runtime` 避免为配置摘要拉取完整 `decision_view` truth chain，只取 `profile_control_decision` 所需的轻量字段。
- 给 `/ai-config/summary` 增加回归测试：当 fill repo 返回 `None` 或无成交时，接口仍返回可渲染摘要而不是 500。

## 发现 2：RDP 治理页仍有英文 key-value 诊断串

可见表现：

RDP 治理页 `观察与回滚` 多张卡片中出现：

`conclusion=rollback_triggered; behavior=positive; execution=positive; operations=negative; governance=mixed`

问题：

- 这是面向实现/数据结构的英文 key-value 串，不是面向操作员的中文解释。
- 与项目约束“前端展示文字使用干净 UTF-8 中文”不一致。
- 操作员需要自己理解 `behavior/execution/operations/governance` 的含义，语义成本偏高。

建议修复：

- 在 RDP view 或后端 summary 层把这些字段转为中文句子或中文标签。
- 示例：`回滚触发：行为指标正向，执行指标正向，运维指标负向，治理结论混合。`
- 保留原始 codes 作为详情抽屉或 debug 字段，不要作为主卡直接展示。

## 发现 3：RDP 已观察完成卡片的按钮语义不清

可见表现：

多张卡片标题为 `观察完成 rel_...`，状态文案为 `观察完成`，但仍展示：

- `运行观察`
- `执行回滚`

问题判断：

- `执行回滚` 在 `rollback_triggered` 场景下可能是合法下一步，不能直接判定为错。
- 但 `运行观察` 对“观察完成”的卡片语义不清，用户会理解为重新跑同一个观察窗口，还是继续观察，还是补跑缺失评估。

建议修复：

- 如果允许再次运行，应改文案为 `重新运行观察`，并在 title/说明里写清楚会覆盖还是追加观察结果。
- 如果不允许再次运行，应禁用按钮并显示 `观察已完成`。
- `执行回滚` 建议绑定发布状态、回滚资格、是否已回滚等后端字段后再决定是否禁用。

## 权限页复核

账户与权限页当前显示：

- 当前身份：`admin`
- 当前角色：`管理员`
- 启用管理员数：`1`
- 当前账号记录标记：`当前最后一个启用中的管理员`

按钮状态：

- `停用`：disabled
- `改角色`：disabled
- `删除`：disabled
- `重置密码`：enabled

结论：上轮修复的“最后一个启用管理员 / 当前账号危险动作禁用”在真实登录态生效。

## 控制台与运行态补充

浏览器控制台中曾出现多次 `dashboard-refresh` 主 bundle 超时/中止日志。网关日志也出现过 `dashboard_bundle_slow`，其中一次 `session` / `authProviders` panel 约 7 秒。后续页面仍能恢复渲染，主要确定性故障集中在 `aiConfigModel`。

建议把 AI 配置页修复后，再单独观察 5 分钟自动刷新日志，确认 `dashboard-refresh` 超时是否随 `aiConfigModel` 修复一起消失，或是否还需要进一步优化 `session/authProviders` panel。
