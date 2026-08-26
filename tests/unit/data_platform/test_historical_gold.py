from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aats.data_platform.data_governance.historical_gold import plan_historical_gold


START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _bundle(bundle_id: str, *, purpose: str, role: str, rows: int) -> dict:
    return {
        "bundle_id": bundle_id,
        "purpose": purpose,
        "eligibility_mode": "historical_research",
        "status": "ELIGIBLE",
        "fingerprint": bundle_id.replace("-", "")[:1] * 64,
        "dataset_version": f"dataset-{role}",
        "coverage_start": START,
        "coverage_end": START + timedelta(days=1),
        "component_sources": [
            {
                "source_id": "00000000-0000-0000-0000-000000000001",
                "symbol": "BTC-USDT-SWAP",
                "role": role,
                "provenance": {
                    "source_key": f"source-{role}",
                    "row_count": rows,
                    "gap_manifest": {"raw_partition_sha256": ["a" * 64]},
                },
            }
        ],
    }


class _Mapped:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _Session:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows

    def execute(self, _statement, params):
        return _Mapped(self.rows[params["bundle_id"]])


def test_source_aware_gold_plan_is_deterministic_and_requires_funding_for_swap() -> None:
    candle_id = "10000000-0000-0000-0000-000000000001"
    funding_id = "20000000-0000-0000-0000-000000000002"
    session = _Session(
        {
            candle_id: _bundle(candle_id, purpose="ohlcv_research", role="candles", rows=96),
            funding_id: _bundle(funding_id, purpose="funding_research", role="funding", rows=3),
        }
    )

    first = plan_historical_gold(
        session,
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=funding_id,
        git_commit="a" * 40,
    )
    second = plan_historical_gold(
        session,
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        candle_bundle_id=candle_id,
        funding_bundle_id=funding_id,
        git_commit="a" * 40,
    )

    assert first == second
    assert first.operation_key.startswith("hist-gold-")
    assert len(first.input_fingerprint) == 64
    assert [item.role for item in first.inputs] == ["candles", "funding"]

    with pytest.raises(ValueError, match="swap_requires_funding_bundle"):
        plan_historical_gold(
            session,
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            candle_bundle_id=candle_id,
            funding_bundle_id=None,
            git_commit="a" * 40,
        )
