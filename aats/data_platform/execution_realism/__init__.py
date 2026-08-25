"""Execution Realism 模块 — Phase 4.

基于市场微观结构数据评估 replay/live 候选订单的实际可执行性。

V1 (Execution Proxy Realism) 使用 Gold OHLCV bars（volume、high-low range）
作为市场快照代理。这不是最终微观结构层，而是以 bar 数据为代理的
execution 近似层。后续版本可接入 orderbook depth 和 trades 数据提升精度。

子模块:
  market_alignment     — 候选订单与市场快照对齐
  fill_feasibility     — 可成交性评估
  slippage_estimator   — 滑点估计
  execution_cost_model — 执行成本汇总
  aggregation          — 跨 family/timeframe 比较聚合
  report_builder       — Markdown 报告生成
"""
from aats.data_platform.execution_realism.l2_event_replay import (
    L2_EVENT_REPLAY_MODEL_VERSION,
    L2ExecutionEvidence,
    L2OrderBookSnapshot,
    L2OrderRequest,
    L2ReplayPolicy,
    L2TradeEvent,
    OrderBookLevel,
    replay_l2_orders,
)
from aats.data_platform.execution_realism.simulation_calibration import (
    CALIBRATION_MODEL_VERSION,
    ExecutionCalibrationPolicy,
    ExecutionCalibrationReport,
    ObservedCommand,
    ObservedFill,
    ObservedPaperOrder,
    ObservedStateTransition,
    PredictedExecution,
    calibrate_l2_against_paper_lifecycle,
)

__all__ = [
    "L2_EVENT_REPLAY_MODEL_VERSION",
    "L2ExecutionEvidence",
    "L2OrderBookSnapshot",
    "L2OrderRequest",
    "L2ReplayPolicy",
    "L2TradeEvent",
    "OrderBookLevel",
    "CALIBRATION_MODEL_VERSION",
    "ExecutionCalibrationPolicy",
    "ExecutionCalibrationReport",
    "ObservedCommand",
    "ObservedFill",
    "ObservedPaperOrder",
    "ObservedStateTransition",
    "PredictedExecution",
    "calibrate_l2_against_paper_lifecycle",
    "replay_l2_orders",
]
