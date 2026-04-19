# Task110：independent 命令流腿级复核补强

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../docs/project_positioning.md)。


## 业务目标与边界

- 目标：修复 `independent` 在 `execution_command_flow_enabled=true` 时，异步提交阶段没有重新执行腿级 `risk/rollout` 复核的问题。
- 边界：本次不改交易策略、不改 adapter payload 结构、不改命令表 schema，只补命令流提交前的腿级 guard，并补回归测试。

## 当前行为

- 直连总线路径里，`OrderManager.handle_order_intent()` 会从普通 `OrderIntent` 还原 `LegOrderIntent`，所以腿级 `risk/rollout` 能生效。
- 但命令流路径里，`ExecutionCommandProcessor -> process_submit_command()` 默认只拿普通 `OrderIntent`，导致 queued submit 在真正发单前不会再次执行腿级复核。

## 修复方案

1. 在 `OrderManager` 内抽出统一的腿级提交 guard。
2. `handle_order_intent()` 和 `process_submit_command()` 共用这段 guard。
3. `process_submit_command()` 在命令流提交前，先从 `OrderIntent` 恢复 `LegOrderIntent`，再重新检查：
   - `leg_risk_evaluator`
   - overlay rollout blockers
4. 若命令流二次复核失败：
   - 不调用 adapter submit
   - 将订单状态持久化为 `BLOCKED`
   - 命令正常 ack，避免无限重试旧 submit

## 一致性与幂等

- 同一个 `intent_id` 仍然沿用现有 `submit:{intent_id}` 幂等键。
- 阻断发生在 submit command 真正出箱前，不会生成重复交易所请求。
- 已进入终态的订单仍沿用现有 `process_submit_command()` 早返回逻辑。

## 测试策略

- 单测补两条命令流回归：
  - 排队后腿级风控收紧，命令处理时必须重新拦住
  - 排队后 rollout 阶段回收，命令处理时必须重新拦住
- 保留现有直达总线路径测试，确保行为不回退。

## 验收标准

- `independent` 订单经命令流异步提交时，也会再次经过腿级 `risk/rollout` 复核。
- 若排队期间状态变化导致不再允许提交，最终订单状态为 `BLOCKED`，adapter 不会收到 submit。
