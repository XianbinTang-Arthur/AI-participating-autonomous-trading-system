from __future__ import annotations

from aats.schemas.features import LiquidityFeatureSet
from aats.schemas.market import MarketSnapshot


class LiquidityAnalyzer:
    def calculate(self, snapshot: MarketSnapshot) -> LiquidityFeatureSet:
        best_bid = float(snapshot.best_bid)
        best_ask = float(snapshot.best_ask)
        last_price = float(snapshot.last_price)
        bid_size = float(snapshot.bid_size)
        ask_size = float(snapshot.ask_size)
        mid_price = (best_bid + best_ask) / 2.0 if (best_bid > 0.0 and best_ask > 0.0) else last_price
        spread = max(best_ask - best_bid, 0.0)
        spread_bps = (spread / mid_price) * 10_000.0 if mid_price else 0.0

        top_depth = max(bid_size + ask_size, 0.0)
        top_of_book_imbalance = (
            (bid_size - ask_size) / top_depth if top_depth else 0.0
        )

        bid_depth = self._depth_total(snapshot.orderbook_depth.get("bids"))
        ask_depth = self._depth_total(snapshot.orderbook_depth.get("asks"))
        total_depth = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / total_depth if total_depth else top_of_book_imbalance
        trade_flow_imbalance = self._trade_flow_imbalance(snapshot.recent_trades)

        quoted_depth = total_depth if total_depth else top_depth
        spread_score = max(0.0, 1.0 - min(spread_bps / 10.0, 1.0))
        depth_score = min(quoted_depth / 10.0, 1.0)
        balance_score = 1.0 - min(abs(depth_imbalance), 1.0)
        liquidity_score = max(0.0, min((spread_score * 0.5) + (depth_score * 0.3) + (balance_score * 0.2), 1.0))
        spread_penalty = min(spread_bps / 12.0, 1.0)
        execution_quality_scale = max(
            0.05,
            min(
                (spread_score * 0.45)
                + (depth_score * 0.2)
                + ((1.0 - min(abs(top_of_book_imbalance - trade_flow_imbalance), 1.0)) * 0.15)
                + ((1.0 - min(abs(trade_flow_imbalance), 1.0)) * 0.2),
                1.0,
            ),
        )

        return LiquidityFeatureSet(
            created_at=snapshot.snapshot_ts,
            spread_bps=spread_bps,
            top_of_book_imbalance=top_of_book_imbalance,
            depth_imbalance=depth_imbalance,
            trade_flow_imbalance=trade_flow_imbalance,
            quoted_depth=quoted_depth,
            spread_penalty=spread_penalty,
            execution_quality_scale=execution_quality_scale,
            liquidity_score=liquidity_score,
        )

    @staticmethod
    def _depth_total(levels: object) -> float:
        if not isinstance(levels, list):
            return 0.0
        total = 0.0
        for level in levels:
            if not isinstance(level, dict):
                continue
            size = level.get("size") or level.get("qty") or level.get("quantity")
            try:
                total += float(size)
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def _trade_flow_imbalance(trades: object) -> float:
        if not isinstance(trades, list):
            return 0.0
        buy_volume = 0.0
        sell_volume = 0.0
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            side = str(trade.get("side") or "").lower()
            size = trade.get("size") or trade.get("qty") or trade.get("quantity") or trade.get("fillSz")
            try:
                quantity = abs(float(size))
            except (TypeError, ValueError):
                continue
            if side == "buy":
                buy_volume += quantity
            elif side == "sell":
                sell_volume += quantity
        total = buy_volume + sell_volume
        if total <= 0.0:
            return 0.0
        return (buy_volume - sell_volume) / total
