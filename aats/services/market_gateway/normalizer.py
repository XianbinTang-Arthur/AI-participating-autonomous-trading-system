from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from aats.schemas.market import MarketSnapshot


class MarketSnapshotNormalizer:
    def __init__(self, exchange_name: str) -> None:
        self.exchange_name = exchange_name

    def normalize(self, raw_snapshot: Mapping[str, Any]) -> MarketSnapshot:
        payload = dict(raw_snapshot)
        payload.setdefault("exchange", self.exchange_name)
        snapshot_ts = payload.get("snapshot_ts")
        if isinstance(snapshot_ts, str):
            payload["snapshot_ts"] = datetime.fromisoformat(snapshot_ts)
        return MarketSnapshot.model_validate(payload)

