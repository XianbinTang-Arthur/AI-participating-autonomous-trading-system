# Task53 - Phase 3 切资金真相

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 目标
本任务将运行时的资金真相从 `PortfolioState` 可变内存状态切到 `ledger` 驱动的持久化投影主路径。

本次交付的原则是：

- 账本成为余额真相源
- 持仓与均价仍通过持久化 fill 回放生成投影
- `portfolio_snapshot` 降级为 projection，不再承担资金真相职责
- 通过显式配置开关切换，默认仍保持关闭

## 新增配置
- `AATS_PORTFOLIO_LEDGER_TRUTH_ENABLED`
  - 默认 `false`
  - 仅允许在持久化存储模式下启用

## 新增模块
- `aats/services/ledger/settlement_posting.py`
  - `LedgerSettlementPostingService`
  - 负责：
    - 初始余额入账
    - spot 买入 quote 支出 / asset 到账
    - spot 卖出 asset 交割 / quote 到账
    - fill fee journal
    - derivatives realized pnl cashflow
    - `cash_available` 余额投影

- `aats/services/projections/ledger_portfolio.py`
  - `LedgerBackedPortfolioService`
  - 负责：
    - fill 事件驱动的账本落账
    - 基于 `execution_repo.fills()` 回放持仓与均价
    - 基于 `ledger cash_available` 生成余额真相
    - 生成 `PortfolioSnapshot` / `PortfolioBalanceDelta` / `FillOutcomeRecord`

## Runtime 切换
当以下条件同时满足时，runtime 会使用新的 ledger truth portfolio service：

- `portfolio_ledger_truth_enabled = true`
- `storage_mode = postgres`
- `ledger_account_repo / ledger_journal_repo / ledger_entry_repo` 可用

否则继续使用旧的 `PortfolioService`。

## 本次实现效果
- paper/postgres 运行态可以在没有 reservation 的情况下，把真实买入支出/卖出交割直接入账到 ledger
- 有 reservation 的路径会继续复用 Phase 1 reservation mirror 作为预留/释放腿
- snapshot 中的 `balances` 来自 ledger `cash_available`
- `portfolio_service.state` 只保留为兼容投影缓存，不再是主资金真相

## 与 Phase 4 的边界
Phase 3 并没有完整重写 recovery / reconciliation。

为避免旧的 fills-only 重建逻辑覆盖新的账本真相，本次在 reconciliation comparison 中加入了最小兼容逻辑：
- 当 `portfolio_ledger_truth_enabled = true` 且存在已存快照时
- comparison 直接以已存快照作为 comparison baseline

这保证 Phase 3 主路径不会立刻被旧 `local_repair` 逻辑回滚。

## 验收测试
- `tests/unit/test_task53_ledger_truth_guards.py`
- `tests/integration/test_phase3_ledger_truth_runtime.py`

回归同时覆盖：
- `tests/unit/test_task52_execution_command_flow.py`
- `tests/integration/test_phase2_command_flow_runtime.py`
- `tests/unit/test_task39_shadow_writes.py`
- `tests/unit/test_task40_ledger_mirror.py`
- `tests/unit/test_order_manager_errors.py`
- `tests/integration/test_operator_api.py`

## 当前已知边界
- `positions / avg_entry / realized_pnl` 仍然由 fill 回放生成，而不是完整 lot-based ledger projection
- reconciliation / recovery 只是做了 Phase 3 兼容，不等于已经完成 Phase 4
- operator 面还没有单独展示 Phase 3 ledger truth 状态页

## 下一步
下一阶段应进入 Phase 4：
- recovery 改成 execution + ledger 主导
- reconciliation 改成基于 authoritative ledger / exchange state 的分类式恢复
- operator control plane 基于新资金真相输出恢复结论
