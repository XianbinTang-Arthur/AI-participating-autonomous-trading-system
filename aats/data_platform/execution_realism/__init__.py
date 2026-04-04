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
