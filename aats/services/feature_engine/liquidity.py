from __future__ import annotations

from aats.schemas.features import LiquidityFeatureSet
from aats.schemas.market import MarketSnapshot


class LiquidityAnalyzer:
    def calculate(self, snapshot: MarketSnapshot) -> LiquidityFeatureSet:
        best_bid = snapshot.best_bid
        best_ask = snapshot.best_ask
        mid_price = (best_bid + best_ask) / 2.0 if (best_bid > 0.0 and best_ask > 0.0) else snapshot.last_price
        spread = max(best_ask - best_bid, 0.0)
        spread_bps = (spread / mid_price) * 10_000.0 if mid_price else 0.0

        top_depth = max(snapshot.bid_size + snapshot.ask_size, 0.0)
        top_of_book_imbalance = (
            (snapshot.bid_size - snapshot.ask_size) / top_depth if top_depth else 0.0
        )

        bid_depth = self._depth_total(snapshot.orderbook_depth.get("bids"))
        ask_depth = self._depth_total(snapshot.orderbook_depth.get("asks"))
        total_depth = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / total_depth if total_depth else top_of_book_imbalance

        quoted_depth = total_depth if total_depth else top_depth
        spread_score = max(0.0, 1.0 - min(spread_bps / 10.0, 1.0))
        depth_score = min(quoted_depth / 10.0, 1.0)
        balance_score = 1.0 - min(abs(depth_imbalance), 1.0)
        liquidity_score = max(0.0, min((spread_score * 0.5) + (depth_score * 0.3) + (balance_score * 0.2), 1.0))

        return LiquidityFeatureSet(
            created_at=snapshot.snapshot_ts,
            spread_bps=spread_bps,
            top_of_book_imbalance=top_of_book_imbalance,
            depth_imbalance=depth_imbalance,
            quoted_depth=quoted_depth,
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
