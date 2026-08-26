from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aats.data_platform.data_governance.continuity import (
    classify_continuity_window,
)
from aats.data_platform.data_governance.gaps import (
    DataGap,
    prospective_drop_gap,
    record_data_gaps,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement, _params):
        return _Rows(self.rows)


START = datetime(2026, 8, 26, tzinfo=timezone.utc)
END = START + timedelta(minutes=15)
RUN_ID = "00000000-0000-0000-0000-000000000001"


def _classify(rows, *, require_flush: bool = True):
    enriched = []
    for row in rows:
        item = dict(row)
        if item["event_type"] == "MESSAGE":
            item.setdefault("earliest_event_ts", START + timedelta(seconds=1))
            item.setdefault("latest_event_ts", END - timedelta(seconds=1))
        else:
            item.setdefault("earliest_event_ts", START + timedelta(seconds=1))
            item.setdefault("latest_event_ts", START + timedelta(seconds=1))
        enriched.append(item)
    return classify_continuity_window(
        _Session(enriched),
        collector="collector",
        channel="channel",
        symbol="BTC-USDT-SWAP",
        window_start=START,
        window_end=END,
        require_flush=require_flush,
    )


def test_complete_continuous_channel_requires_message_and_flush() -> None:
    report = _classify(
        [
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "MESSAGE", "count": 10},
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "FLUSH", "count": 2},
        ]
    )

    assert report.status == "complete"
    assert report.reason_codes == ()
    assert report.generations == (1,)
    assert report.ingest_run_ids == (RUN_ID,)


def test_sparse_liquidation_zero_is_valid_only_with_connection_frames() -> None:
    healthy = _classify(
        [{"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "MESSAGE", "count": 3}],
        require_flush=False,
    )
    unknown = _classify([], require_flush=False)

    assert healthy.status == "complete"
    assert unknown.status == "unknown"
    assert "continuity_unknown" in unknown.reason_codes


def test_drop_disconnect_or_generation_change_is_not_complete() -> None:
    report = _classify(
        [
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "MESSAGE", "count": 4},
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "FLUSH", "count": 1},
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "DISCONNECT", "count": 1},
            {"connection_generation": 2, "ingest_run_id": RUN_ID, "event_type": "DROP", "count": 2},
        ]
    )

    assert report.status == "known_gap"
    assert "collector_disconnect_in_window" in report.reason_codes
    assert "collector_drop_observed" in report.reason_codes
    assert "multiple_connection_generations" in report.reason_codes


def test_process_restart_with_reset_generation_is_a_known_gap() -> None:
    report = _classify(
        [
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "MESSAGE", "count": 4},
            {"connection_generation": 1, "ingest_run_id": RUN_ID, "event_type": "FLUSH", "count": 1},
            {"connection_generation": 1, "ingest_run_id": "00000000-0000-0000-0000-000000000002", "event_type": "MESSAGE", "count": 4},
        ]
    )

    assert report.status == "known_gap"
    assert "multiple_ingest_runs" in report.reason_codes


def test_message_evidence_must_cover_both_window_boundaries() -> None:
    report = _classify(
        [
            {
                "connection_generation": 1,
                "ingest_run_id": RUN_ID,
                "event_type": "MESSAGE",
                "count": 10,
                "earliest_event_ts": START + timedelta(minutes=5),
                "latest_event_ts": END - timedelta(minutes=5),
            },
            {
                "connection_generation": 1,
                "ingest_run_id": RUN_ID,
                "event_type": "FLUSH",
                "count": 2,
            },
        ]
    )

    assert report.status == "known_gap"
    assert "collector_window_start_unproven" in report.reason_codes
    assert "collector_window_end_unproven" in report.reason_codes


def test_drop_gap_is_prospective_only_and_preserves_exact_row_bounds() -> None:
    gap = prospective_drop_gap(
        dataset_name="bronze.market_trades",
        symbol="BTC-USDT-SWAP",
        channel="trades",
        event_ts=START + timedelta(minutes=1),
        reason_code="flush_failed",
        details={
            "gap_start": START.isoformat(),
            "gap_end": (START + timedelta(seconds=3)).isoformat(),
            "dropped_rows": 4,
        },
    )

    assert gap.classification == "prospective_only"
    assert gap.status == "AWAITING_LIVE_COLLECTION"
    assert gap.gap_start == START
    assert gap.gap_end == START + timedelta(seconds=3)
    assert gap.evidence["range_precision"] == "source_row_bounds"


def test_gap_persistence_is_idempotent_by_schema_constraint() -> None:
    class _Result:
        def scalar_one_or_none(self):
            return "00000000-0000-0000-0000-000000000001"

    class _WriteSession:
        sql = ""
        parameters = None

        def execute(self, statement, parameters):
            self.sql = str(statement)
            self.parameters = parameters
            return _Result()

    session = _WriteSession()
    gap = prospective_drop_gap(
        dataset_name="staging.raw_liquidations",
        symbol="BTC-USDT-SWAP",
        channel="liquidation-orders",
        event_ts=START,
        reason_code="collector_drop",
    )

    assert record_data_gaps(session, [gap]) == 1
    assert "ON CONFLICT ON CONSTRAINT uq_data_gap_scope_reason" in session.sql
    assert session.parameters["classification"] == "prospective_only"


def test_gap_persistence_rejects_conflicting_evidence() -> None:
    class _Result:
        def scalar_one_or_none(self):
            return None

    class _WriteSession:
        def execute(self, _statement, _parameters):
            return _Result()

    gap = prospective_drop_gap(
        dataset_name="staging.raw_liquidations",
        symbol="BTC-USDT-SWAP",
        channel="liquidation-orders",
        event_ts=START,
        reason_code="collector_drop",
    )
    with pytest.raises(RuntimeError, match="data_gap_immutable_evidence_conflict"):
        record_data_gaps(_WriteSession(), [gap])


def test_gap_contract_rejects_direct_resolved_or_incompatible_status() -> None:
    common = {
        "dataset_name": "staging.official_trade_history",
        "symbol": "BTC-USDT-SWAP",
        "channel": "history-trades",
        "gap_start": START,
        "gap_end": START + timedelta(seconds=1),
        "classification": "official_backfill",
        "reason_code": "source_gap",
    }
    with pytest.raises(ValueError, match="resolved_status_requires_transition"):
        DataGap(status="BACKFILLED", **common)
    with pytest.raises(ValueError, match="status_incompatible_with_classification"):
        DataGap(status="AWAITING_LIVE_COLLECTION", **common)


def test_continuity_window_rejects_naive_timestamps() -> None:
    with pytest.raises(
        ValueError,
        match="continuity_window_requires_timezone_aware_timestamps",
    ):
        classify_continuity_window(
            _Session([]),
            collector="collector",
            channel="channel",
            symbol="BTC-USDT-SWAP",
            window_start=datetime(2026, 8, 26),
            window_end=datetime(2026, 8, 27),
        )
