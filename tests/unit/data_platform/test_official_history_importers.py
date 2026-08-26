from __future__ import annotations

import json
import io
import tarfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aats.data_platform.collectors.backfill.official_history_importers import (
    L2Event,
    _get_okx_page,
    _missing_bar_ranges,
    archive_raw_response_page,
    causal_resample_l2,
    import_l2_file,
    import_mark_price_rest,
    import_trade_file,
    import_trade_rest,
    iter_l2_history,
)


UTC = timezone.utc


class _Result:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _Session:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict | list[dict]]] = []

    def execute(self, statement, params=None):
        self.executions.append((str(statement), params or {}))
        return _Result(len(params) if isinstance(params, list) else 1)


def _payloads(session: _Session) -> list[dict]:
    return [
        payload
        for _, params in session.executions
        for payload in (params if isinstance(params, list) else [params])
    ]


def test_trade_file_is_immutable_archived_half_open_and_source_aware(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(seconds=2)
    source = tmp_path / "trades.jsonl"
    records = [
        {"instId": "BTC-USDT-SWAP", "ts": str(int(start.timestamp() * 1000)), "tradeId": "1", "px": "100", "sz": "2", "side": "buy"},
        {"instId": "BTC-USDT-SWAP", "ts": str(int((end - timedelta(milliseconds=1)).timestamp() * 1000)), "tradeId": "2", "px": "101", "sz": "3", "side": "sell"},
        {"instId": "BTC-USDT-SWAP", "ts": str(int(end.timestamp() * 1000)), "tradeId": "3", "px": "102", "sz": "4", "side": "buy"},
    ]
    source.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    archive = tmp_path / "archive"
    session = _Session()

    stats = import_trade_file(
        session,
        path=source,
        symbol="BTC-USDT-SWAP",
        start=start,
        end=end,
        source_id="00000000-0000-0000-0000-000000000001",
        ingest_run_id="00000000-0000-0000-0000-000000000002",
        raw_archive_dir=archive,
    )

    assert stats.rows_read == stats.rows_written == 2
    archived = list(archive.glob("official_trade_*.jsonl"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == source.read_bytes()
    assert len(stats.raw_sha256[0]) == 64
    sql = "\n".join(statement for statement, _ in session.executions)
    assert "ON CONFLICT (source_id, symbol, ts, trade_id) DO NOTHING" in sql
    assert {params["side"] for params in _payloads(session)} == {"buy", "sell"}


def test_trade_file_fails_eligibility_when_utc_window_edge_is_missing(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    source = tmp_path / "trades.jsonl"
    records = [
        {
            "instId": "BTC-USDT-SWAP",
            "ts": str(int((start + timedelta(milliseconds=1)).timestamp() * 1000)),
            "tradeId": "1",
            "px": "100",
            "sz": "2",
            "side": "buy",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "ts": str(int((start + timedelta(hours=16)).timestamp() * 1000)),
            "tradeId": "2",
            "px": "101",
            "sz": "3",
            "side": "sell",
        },
    ]
    source.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    stats = import_trade_file(
        _Session(),
        path=source,
        symbol="BTC-USDT-SWAP",
        start=start,
        end=end,
        source_id="00000000-0000-0000-0000-000000000001",
        ingest_run_id="00000000-0000-0000-0000-000000000002",
        raw_archive_dir=tmp_path / "archive",
    )

    assert stats.rows_read == stats.rows_written == 2
    assert stats.gaps == (
        {
            "reason": "official_trade_history_coverage_unproven",
            "gap_start": (start + timedelta(hours=16)).isoformat(),
            "gap_end": end.isoformat(),
        },
    )


def test_l2_import_hashes_datetime_payload_and_reports_sequence_gap(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    source = tmp_path / "l2.jsonl"
    rows = [
        {"instId": "BTC-USDT-SWAP", "ts": str(int(start.timestamp() * 1000)), "action": "snapshot", "bids": [["100", "2"]], "asks": [["101", "3"]], "seqId": "10"},
        {"instId": "BTC-USDT-SWAP", "ts": str(int((start + timedelta(seconds=1)).timestamp() * 1000)), "action": "update", "bids": [["100", "1"]], "asks": [], "prevSeqId": "8", "seqId": "11"},
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    session = _Session()

    stats = import_l2_file(
        session,
        path=source,
        symbol="BTC-USDT-SWAP",
        start=start,
        end=start + timedelta(seconds=2),
        source_id="00000000-0000-0000-0000-000000000001",
        ingest_run_id="00000000-0000-0000-0000-000000000002",
        raw_archive_dir=tmp_path / "archive",
    )

    assert stats.rows_read == stats.rows_written == 2
    assert stats.gaps[0]["reason"] == "sequence_discontinuity"
    hashes = [params["source_row_hash"] for params in _payloads(session)]
    assert all(len(value) == 64 for value in hashes)


def test_l2_import_streams_okx_tar_gzip_data_member(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows = [
        {
            "instId": "BTC-USDT-SWAP",
            "ts": str(int(start.timestamp() * 1000)),
            "action": "snapshot",
            "bids": [["100", "2", "1"]],
            "asks": [["101", "3", "1"]],
        },
        {
            "instId": "BTC-USDT-SWAP",
            "ts": str(int((start + timedelta(milliseconds=10)).timestamp() * 1000)),
            "action": "update",
            "bids": [["100", "1", "1"]],
            "asks": [],
        },
    ]
    payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    source = tmp_path / "BTC-USDT-SWAP-L2orderbook-400lv-2026-08-01.tar.gz"
    with tarfile.open(source, mode="w:gz") as archive:
        member = tarfile.TarInfo("BTC-USDT-SWAP-L2orderbook-400lv-2026-08-01.data")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    session = _Session()
    stats = import_l2_file(
        session,
        path=source,
        symbol="BTC-USDT-SWAP",
        start=start,
        end=start + timedelta(seconds=1),
        source_id="00000000-0000-0000-0000-000000000001",
        ingest_run_id="00000000-0000-0000-0000-000000000002",
        raw_archive_dir=tmp_path / "archive",
    )

    assert stats.rows_read == stats.rows_written == 2
    assert stats.gaps == ()
    archived = list((tmp_path / "archive").glob("official_l2_*.tar.gz"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == source.read_bytes()


def test_l2_database_reader_uses_bounded_server_side_streaming() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)

    class _Mappings:
        def fetchmany(self, _size: int):
            return []

    class _StreamingResult:
        closed = False

        def mappings(self):
            return _Mappings()

        def close(self):
            self.closed = True

    class _StreamingSession:
        options: dict[str, object] | None = None

        def execute(self, statement, _params):
            self.options = dict(statement.get_execution_options())
            return _StreamingResult()

    session = _StreamingSession()
    assert list(
        iter_l2_history(
            session,
            source_id="00000000-0000-0000-0000-000000000001",
            symbol="BTC-USDT-SWAP",
            start=start,
            end=start + timedelta(days=1),
            fetch_size=321,
        )
    ) == []
    assert session.options == {"stream_results": True, "yield_per": 321}


def test_causal_resample_never_uses_future_or_crosses_sequence_gap() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    events = [
        L2Event("BTC-USDT-SWAP", start + timedelta(milliseconds=500), "snapshot", ((Decimal("100"), Decimal("2")),), ((Decimal("101"), Decimal("3")),), 10, None),
        L2Event("BTC-USDT-SWAP", start + timedelta(milliseconds=1500), "update", ((Decimal("100"), Decimal("1")),), (), 12, 8),
    ]

    samples, gaps = causal_resample_l2(
        events,
        start=start,
        end=start + timedelta(seconds=3),
        interval_ms=1_000,
        max_staleness_ms=2_000,
    )

    assert [sample.ts for sample in samples] == [start + timedelta(seconds=1)]
    assert samples[0].source_state_ts == start + timedelta(milliseconds=500)
    assert all(sample.source_state_ts <= sample.ts for sample in samples)
    assert [gap["sample_ts"] for gap in gaps] == [
        start.isoformat(),
        (start + timedelta(seconds=2)).isoformat(),
    ]


def test_update_before_first_snapshot_cannot_create_state() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    events = [
        L2Event("BTC-USDT-SWAP", start, "update", ((Decimal("100"), Decimal("2")),), ((Decimal("101"), Decimal("3")),), 1, None)
    ]

    samples, gaps = causal_resample_l2(
        events,
        start=start,
        end=start + timedelta(seconds=1),
        interval_ms=1_000,
        max_staleness_ms=2_000,
    )

    assert samples == []
    assert gaps[0]["reason"] == "state_unavailable"


def test_crossed_orderbook_is_recorded_as_gap_not_research_sample() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    events = [
        L2Event(
            "BTC-USDT-SWAP",
            start,
            "snapshot",
            ((Decimal("101"), Decimal("2")),),
            ((Decimal("100"), Decimal("3")),),
            1,
            None,
        )
    ]

    samples, gaps = causal_resample_l2(
        events,
        start=start,
        end=start + timedelta(seconds=1),
        interval_ms=1_000,
        max_staleness_ms=2_000,
    )

    assert samples == []
    assert gaps[0]["reason"] == "state_crossed_book"


def test_contiguous_resampling_gaps_are_range_compressed() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)

    samples, gaps = causal_resample_l2(
        [],
        start=start,
        end=start + timedelta(seconds=5),
        interval_ms=500,
        max_staleness_ms=2_000,
    )

    assert samples == []
    assert gaps == [
        {
            "sample_ts": start.isoformat(),
            "gap_start": start.isoformat(),
            "gap_end": (start + timedelta(seconds=5)).isoformat(),
            "reason": "state_unavailable",
            "missing_samples": 10,
        }
    ]


def test_okx_request_retries_429_without_logging_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            httpx.Response(429, json={"code": "50011"}),
            httpx.Response(200, json={"code": "0", "data": []}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        body, raw = _get_okx_page(
            client,
            "https://www.okx.com/api/v5/market/history-trades",
            {"instId": "BTC-USDT-SWAP"},
        )

    assert body == {"code": "0", "data": []}
    assert b"BTC-USDT-SWAP" not in raw


def test_trade_history_uses_timestamp_pagination_contract(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.url.params)
        return httpx.Response(200, json={"code": "0", "data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = import_trade_rest(
            _Session(),
            client=client,
            base_url="https://www.okx.com",
            symbol="BTC-USDT-SWAP",
            start=start,
            end=end,
            source_id="00000000-0000-0000-0000-000000000001",
            ingest_run_id="00000000-0000-0000-0000-000000000002",
            raw_archive_dir=tmp_path.resolve(),
            request_interval_seconds=0,
        )

    assert stats.pages_or_files == 1
    assert observed["type"] == "2"
    assert observed["after"] == str(int(end.timestamp() * 1000))
    assert stats.gaps == (
        {
            "reason": "official_trade_history_coverage_unproven",
            "gap_start": start.isoformat(),
            "gap_end": end.isoformat(),
        },
    )


def test_trade_history_short_page_marks_uncovered_prefix_as_gap(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    oldest = start + timedelta(minutes=30)
    end = start + timedelta(hours=1)
    payload = {
        "code": "0",
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "ts": str(int(oldest.timestamp() * 1000)),
                "tradeId": "1",
                "px": "100",
                "sz": "2",
                "side": "buy",
            }
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = import_trade_rest(
            _Session(),
            client=client,
            base_url="https://www.okx.com",
            symbol="BTC-USDT-SWAP",
            start=start,
            end=end,
            source_id="00000000-0000-0000-0000-000000000001",
            ingest_run_id="00000000-0000-0000-0000-000000000002",
            raw_archive_dir=tmp_path.resolve(),
            request_interval_seconds=0,
        )

    assert stats.rows_read == 1
    assert stats.gaps == (
        {
            "reason": "official_trade_history_coverage_unproven",
            "gap_start": start.isoformat(),
            "gap_end": oldest.isoformat(),
        },
    )


def test_trade_history_rejects_symbol_mismatch(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    payload = {
        "code": "0",
        "data": [
            {
                "instId": "ETH-USDT-SWAP",
                "ts": str(int(start.timestamp() * 1000)),
                "tradeId": "1",
                "px": "100",
                "sz": "2",
                "side": "buy",
            }
        ],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="trade_history_symbol_mismatch"):
            import_trade_rest(
                _Session(),
                client=client,
                base_url="https://www.okx.com",
                symbol="BTC-USDT-SWAP",
                start=start,
                end=start + timedelta(minutes=1),
                source_id="00000000-0000-0000-0000-000000000001",
                ingest_run_id="00000000-0000-0000-0000-000000000002",
                raw_archive_dir=tmp_path.resolve(),
                request_interval_seconds=0,
            )


def test_trade_history_fails_closed_when_page_budget_is_exhausted(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    data = [
        {
            "instId": "BTC-USDT-SWAP",
            "ts": str(
                int((start + timedelta(minutes=10, seconds=index)).timestamp() * 1000)
            ),
            "tradeId": str(index),
            "px": "100",
            "sz": "2",
            "side": "buy",
        }
        for index in range(100)
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "data": data})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="trade_history_max_pages_exceeded"):
            import_trade_rest(
                _Session(),
                client=client,
                base_url="https://www.okx.com",
                symbol="BTC-USDT-SWAP",
                start=start,
                end=start + timedelta(hours=1),
                source_id="00000000-0000-0000-0000-000000000001",
                ingest_run_id="00000000-0000-0000-0000-000000000002",
                raw_archive_dir=tmp_path.resolve(),
                max_pages=1,
                request_interval_seconds=0,
            )


def test_mark_history_unconfirmed_rows_advance_pagination_and_remain_gaps(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(minutes=15)
    page = [
        [str(int(start.timestamp() * 1000)), "100", "101", "99", "100", "0"]
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "data": page})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = import_mark_price_rest(
            _Session(),
            client=client,
            base_url="https://www.okx.com",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            start=start,
            end=end,
            source_id="00000000-0000-0000-0000-000000000001",
            ingest_run_id="00000000-0000-0000-0000-000000000002",
            raw_archive_dir=tmp_path.resolve(),
            max_pages=1,
            request_interval_seconds=0,
        )

    assert stats.rows_read == stats.rows_written == 0
    assert stats.gaps == (
        {
            "reason": "official_mark_bar_missing",
            "gap_start": start.isoformat(),
            "gap_end": end.isoformat(),
        },
    )


def test_mark_proxy_missing_bars_are_compressed_into_half_open_ranges() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    step = timedelta(minutes=15)

    gaps = _missing_bar_ranges(
        start=start,
        end=start + 5 * step,
        interval_seconds=900,
        observed={start, start + 3 * step, start + 4 * step},
    )

    assert gaps == [
        {
            "reason": "official_mark_bar_missing",
            "gap_start": (start + step).isoformat(),
            "gap_end": (start + 3 * step).isoformat(),
        }
    ]


def test_raw_archive_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="raw_archive_filename_invalid"):
        archive_raw_response_page(tmp_path.resolve(), "../escaped.json", b"{}\n")

    assert not (tmp_path.parent / "escaped.json").exists()


def test_trade_file_fails_closed_on_non_object_or_non_finite_row(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    common = {
        "symbol": "BTC-USDT-SWAP",
        "start": start,
        "end": start + timedelta(seconds=1),
        "source_id": "00000000-0000-0000-0000-000000000001",
        "ingest_run_id": "00000000-0000-0000-0000-000000000002",
    }
    non_object = tmp_path / "non_object.jsonl"
    non_object.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="official_jsonl_row_not_object"):
        import_trade_file(
            _Session(),
            path=non_object,
            raw_archive_dir=tmp_path / "archive-a",
            **common,
        )

    non_finite = tmp_path / "non_finite.jsonl"
    non_finite.write_text(
        json.dumps(
            {
                "instId": "BTC-USDT-SWAP",
                "ts": str(int(start.timestamp() * 1000)),
                "tradeId": "1",
                "px": "NaN",
                "sz": "1",
                "side": "buy",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="trade_history_row_invalid"):
        import_trade_file(
            _Session(),
            path=non_finite,
            raw_archive_dir=tmp_path / "archive-b",
            **common,
        )


def test_l2_file_fails_closed_on_malformed_level(tmp_path: Path) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    source = tmp_path / "bad_l2.jsonl"
    source.write_text(
        json.dumps(
            {
                "instId": "BTC-USDT-SWAP",
                "ts": str(int(start.timestamp() * 1000)),
                "action": "snapshot",
                "bids": [["100"]],
                "asks": [["101", "1"]],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="l2_history_row_invalid"):
        import_l2_file(
            _Session(),
            path=source,
            symbol="BTC-USDT-SWAP",
            start=start,
            end=start + timedelta(seconds=1),
            source_id="00000000-0000-0000-0000-000000000001",
            ingest_run_id="00000000-0000-0000-0000-000000000002",
            raw_archive_dir=tmp_path / "archive",
        )
