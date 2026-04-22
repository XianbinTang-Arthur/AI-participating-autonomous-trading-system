# Research Data Platform — Backtest MVP
#
# 本子包提供 backtest 的纯函数式工具（fill 模拟、position tracking、PnL）。
# 与 live execution path 完全隔离：只消费 ReplayBar，不写 DB、不发消息、无 I/O。
from __future__ import annotations

from aats.data_platform.replay.backtest.fill_simulator import (
    FillRequest,
    FillResult,
    FillSimulator,
    OrderSide,
    OrderType,
)
from aats.data_platform.replay.backtest.position_tracker import (
    Fill,
    PositionSnapshot,
    PositionTracker,
)
from aats.data_platform.replay.backtest.equity_builder import (
    BacktestSummary,
    EquityBuilder,
    EquityPoint,
)
from aats.data_platform.replay.backtest.cost_validator import (
    CostDiagnostic,
    CostValidationSummary,
    CostValidator,
)

__all__ = [
    "BacktestSummary",
    "CostDiagnostic",
    "CostValidationSummary",
    "CostValidator",
    "EquityBuilder",
    "EquityPoint",
    "Fill",
    "FillRequest",
    "FillResult",
    "FillSimulator",
    "OrderSide",
    "OrderType",
    "PositionSnapshot",
    "PositionTracker",
]
