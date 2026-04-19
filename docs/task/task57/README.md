# Task57：去掉 legacy 写真相与金融级收敛推进

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 本轮目标

这一轮没有宣称“彻底完成最终金融级收敛”，而是专门推进两件最关键的基础能力：

- 把“最终收敛”做成显式、强约束的 runtime 模式，而不是靠人为约定组合多个 flag
- 把 ledger-backed 组合投影从旧的 `PortfolioState.apply_fill()` 加权均价逻辑，推进到独立的 lot-based 计算器

## 本轮完成的能力

### 1. 新增严格收敛模式

新增开关：

- `financial_convergence_mode_enabled`

开启后必须满足：

- `storage_mode=postgres`
- `execution_command_flow_enabled=True`
- `portfolio_ledger_truth_enabled=True`
- `recovery_reconciliation_execution_ledger_enabled=True`
- `operator_control_plane_execution_ledger_enabled=True`
- `event_persistence_mode="strict"`
- `database_single_runtime_guard_enabled=True`

这意味着：

- 最终收敛不再只是“把几个 Phase 开关手工一起打开”
- 而是有一个明确的、可校验的、面向生产的严格模式入口

### 2. Ledger-backed 组合投影改为独立 lot-based 计算

新增模块：

- `aats/services/ledger/lot_projection.py`

新计算器职责：

- 按 fill 顺序维护 open lots
- 使用 FIFO 方式计算平仓 realized PnL
- 在穿仓反向时保留新方向 lot
- 重新生成：
  - position quantity
  - avg entry price
  - realized pnl
  - total fees paid

这个 lot 计算器已经接到：

- `aats/services/projections/ledger_portfolio.py`

新的 Phase 3 路径现在是：

1. 从 execution fills 重建 lot state
2. 计算 fill 前持仓状态
3. 计算 fill 后持仓状态
4. 根据 lot delta 和 fee delta 生成 ledger settlement projection
5. 再从 ledger available balances + lot projection 生成 portfolio snapshot

因此当前组合快照的资金/仓位语义比之前更接近金融账本，而不是单纯复用旧 `PortfolioState.apply_fill()` 的平均成本逻辑。

## 本轮没有宣称完成的部分

以下能力仍未完成，必须明确：

- 仍未完全去掉 legacy 写真相
  - 当前 legacy `execution_repo` 仍在写链中存在
  - 新执行表和 ledger 已经是 recovery/control plane 的主读真相，但不是唯一写真相

- lot-based 计算器还只是投影层，不是持久化 lot ledger
  - 现在 open lots 是从 fills 重建出来的
  - 还没有独立的 `position_lots` / `lot_journals` 持久化真相表

- 双实例验证仍未完成
  - 当前只把“单 runtime guard 必须开启”纳入了严格收敛模式
  - 还没有完成真实的双实例冲突演练与恢复证明

- 外部副作用仍未达到完全 once-only 财务证明
  - 当前 command flow、recovery gate、control plane 已更严格
  - 但“提交已发出、确认丢失、重启后恢复”的金融级证明仍需更强 failure injection

## 当前更准确的结论

可以这样描述当前状态：

- 系统已经从“Phase 2-5 的分阶段可选功能”进一步推进到了“存在一个严格收敛模式入口”
- 组合投影已经开始脱离 legacy 平均成本路径，转向独立 lot-based 计算
- 但还没有完成“持久化 lot ledger + 去掉 legacy 写真相 + 双实例安全证明”这最后三块

因此当前最准确的工程结论是：

- 已进入最终金融级收敛路线
- 已完成其中一段关键主干
- 但还没有到可以诚实地宣称“最终金融级收敛已全部完成”的程度

## 下一步建议

如果继续推进，优先级应是：

1. 引入持久化 `position_lots` / `lot_events` 表，结束“从 fills 临时重建 open lots”的状态
2. 让 execution primary write 切到 `execution_orders / execution_fills`，把 legacy `execution_repo` 降级成兼容投影
3. 增加 failure injection：
   - submit sent but ack lost
   - cancel races with fill
   - restart between fill persist and portfolio projection
4. 在真实 PostgreSQL 环境下做双实例锁冲突和恢复演练
