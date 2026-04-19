# Task56：执行真相与控制面硬化收口

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 本轮目标

这一轮不是继续补零碎边角，而是专门清理以下高风险尾巴：

- Phase 5 已经把 operator/control plane 的读路径切到了 `execution_orders + execution_fills + ledger`，但部分高风险 action 仍然隐含依赖 legacy `execution_repo`。
- Phase 2 下，订单在尚未获得 `exchange_order_id` 时，operator cancel 没有真正的“预提交取消”语义，容易退化成错误的 adapter cancel。
- 团队继续阅读和排障时，仍然容易把系统理解成旧的 `legacy execution -> portfolio snapshot` 主链，而不是现在的分阶段双轨体系。

## 本轮完成的硬化

### 1. Phase 5 现在强制要求 Phase 2 执行命令流

新增约束：

- `operator_control_plane_execution_ledger_enabled=True` 时，必须同时启用 `execution_command_flow_enabled=True`

原因：

- Phase 5 的 control plane 已经把订单真相切到了 `execution_order_repo`
- 如果 Phase 2 没启用，`execution_order_repo` 仍然只是 shadow mirror，不适合作为 operator 主真相
- 这会导致“控制面读的是新表，执行写的仍然是旧链”的结构性背离

当前启用顺序应为：

1. `execution_command_flow_enabled=True`
2. `portfolio_ledger_truth_enabled=True`
3. `recovery_reconciliation_execution_ledger_enabled=True`
4. `operator_control_plane_execution_ledger_enabled=True`

### 2. 预提交取消现在有安全语义

修复前：

- 订单处于 `CREATED` 或“尚未拿到 `exchange_order_id`”的早期状态时
- operator cancel 会继续沿着旧 cancel 路径往下走
- 最终容易退化为“尝试对一个不存在的 exchange order 做 cancel”

修复后：

- 当订单尚未获得 `exchange_order_id`
- 且 submit command 仍处于本地可安全拦截的阶段
- 系统会直接将本地 submit command 标记为 `ABANDONED`
- 然后把订单安全地本地终结为 `CANCELED`
- 不再错误调用 adapter cancel

这条语义适用于：

- Phase 2 队列中尚未真正提交到交易所的订单
- Phase 5 control plane 在只持有新执行表视图时的取消动作

### 3. control plane 不再把 legacy `OrderState` 当作唯一可操作状态

新增能力：

- `OrderManager.resolve_order_state_for_control()` 可以在 legacy `execution_repo` 缺失时，从 `execution_order_repo` 回填出可操作的 `OrderState`
- `OperatorQueryService.order_detail()` 在 Phase 5 下会优先使用 control-plane 可恢复状态，而不是简单退化成“只能看，不能判断”
- `resolve_stuck_submission()` 现在通过 control-plane 状态和 fills 视图工作，并在处理后同步更新 `execution_order_repo`

这意味着：

- 即使旧 `order_states` 行缺失
- 只要 `execution_orders` 仍然存在
- control plane 仍然能完成订单详情判断和部分恢复动作

### 4. 订单状态机补齐了 Phase 2/Phase 5 必需的取消迁移

新增允许迁移：

- `CREATED -> CANCEL_PENDING`
- `CREATED -> CANCELED`
- `SUBMITTING -> CANCEL_PENDING`
- `SUBMITTING -> CANCELED`

原因：

- 在命令流架构下，订单可能在真正获得交易所确认前就被人工取消
- 如果状态机不允许这些迁移，系统会被迫继续依赖旧同步提交流程的假设

## flags 全开后的最新全链路

下面描述的是当前建议的“Phase 2 到 Phase 5 全开”路径。

### A. 订单提交流程

1. `OrderIntent` 到达 `OrderManager`
2. 先在 legacy `execution_repo` 中写入 `CREATED`
3. 同时写入 `execution_orders`
4. `ExecutionOrderService` 写入 `execution_commands.submit`
5. `ExecutionCommandProcessor` 异步拉取 pending command
6. 真正调用 adapter submit
7. 订单状态和 fills 回写到 legacy `execution_repo`
8. shadow/同步层把状态同步回 `execution_orders / execution_fills`

当前真实状态：

- 执行写路径仍然兼容 legacy repo
- 但 Phase 5 读路径已经优先使用 `execution_orders / execution_fills`

### B. 成交流程

1. fill 先进入 legacy `execution_repo`
2. Phase 1/2 shadow 把 fill 镜像到 `execution_fills`
3. Phase 3 `LedgerSettlementPostingService` 把资金影响写入 `ledger_journals + ledger_entries`
4. `LedgerBackedPortfolioService` 基于 ledger available balance 和 fills 重建最新组合投影
5. `portfolio_snapshots` 继续作为投影快照存在

当前真实状态：

- 余额真相已经切到 ledger
- 持仓和部分 PnL 仍然属于投影层，不是完整 lot-based 账本

### C. 恢复与对账流程

1. Phase 4 恢复启动时优先看 `execution_orders / execution_commands / ledger-backed snapshot`
2. 若存在 `pending_execution_commands`，直接阻断 resume
3. reconciliation 结果会被分类为：
   - `clean`
   - `projection_rebuild_required`
   - `manual_review_required`
   - `investigate_state_divergence`
   - `halt_required`
4. `RecoveryPostureEvaluator` 把这些分类并入 resume gate

### D. Operator/control plane 流程

1. Phase 5 打开后：
   - `/orders/*` 读 `execution_order_repo`
   - `/fills/*` 读 `execution_fill_repo_v2`
   - `/balances` 读 `ledger_accounts + ledger_entries`
2. 高风险 write action 仍经过 runtime service，但现在要求：
   - Phase 2 已打开
   - Phase 4 已打开
   - auth 已满足要求
3. stuck submission / detail 评估不再把 legacy `OrderState` 当唯一来源

## 本轮之后仍然保留的边界

以下边界必须明确，不能误写成“已完全完成金融级收敛”：

- 当前仍是双轨系统
  - legacy `execution_repo / portfolio_repo` 仍在写链路中存在
  - 新表是 operator/control/recovery 的主读真相，但不是唯一写真相

- 账本仍不是完整 lot-based 金融账本
  - 余额真相已切到 ledger
  - 但持仓成本、均价、部分 realized PnL 仍依赖 fill replay 投影

- 多实例一致性还没有被证明
  - 当前设计仍默认单 runtime
  - 没有完成“双实例同时指向同一数据库”的生产级收敛

- 外部交易所乱序、重复、超时场景仍需更强失败注入验证
  - 尤其是 `submit sent but ack lost`
  - `cancel racing with fill`
  - `restart between command ack and local projection`

## 当前更准确的结论

可以这样描述当前系统：

- 已经完成“执行命令流 + ledger 余额真相 + recovery/reconciliation gating + control plane 主读切换”的主骨架
- 已经明显脱离“纯 legacy snapshot runtime”
- 但还没有达到“单真相、完整金融账本、可证明多实例安全”的最终形态

因此当前更准确的工程表述是：

- `execution + ledger + recovery + control plane` 已经完成主要收口
- 但“最终的金融级收敛”仍然是接近完成而非彻底完成

下一阶段如果继续推进，重点应转向：

- 去掉 legacy 写真相
- 用 ledger 驱动完整 position / avg entry / realized pnl
- 做失败注入、重启演练、双实例隔离验证
