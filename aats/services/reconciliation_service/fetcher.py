from __future__ import annotations


class ExchangeStateFetcher:
    def fetch_positions(self, *, local_positions: dict[str, float]) -> dict[str, float]:
        return dict(local_positions)

