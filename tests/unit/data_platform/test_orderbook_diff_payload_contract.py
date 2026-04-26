from __future__ import annotations

from datetime import datetime, timezone

from aats.data_platform.orderbook_diff_payload_contract import (
    CAPTURE_STATUS_DIFF_PERSISTED,
    CAPTURE_STATUS_SNAPSHOT_ONLY,
    COLLECTOR_SEQUENCE_SCOPE,
    ORDERBOOK_DIFF_PAYLOAD_SCHEMA_VERSION,
    ORDERBOOK_DIFF_PAYLOAD_TABLE,
    ORDERBOOK_ROW_CHECKSUM_VERSION,
    orderbook_diff_payload_contract_spec,
    required_write_fields_for_status,
    validate_orderbook_diff_payload_record,
)


_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_TS = datetime(2026, 4, 26, 1, 40, tzinfo=timezone.utc)


def _base_record(**overrides):
    record = {
        "storage_table": ORDERBOOK_DIFF_PAYLOAD_TABLE,
        "snapshot_table": "bronze.market_orderbook_books5",
        "symbol": "BTC-USDT-SWAP",
        "ts": _TS,
        "source_ts": _TS,
        "collector_sequence": 42,
        "collector_sequence_scope": COLLECTOR_SEQUENCE_SCOPE,
        "row_checksum": _SHA_A,
        "checksum_version": ORDERBOOK_ROW_CHECKSUM_VERSION,
        "capture_status": CAPTURE_STATUS_SNAPSHOT_ONLY,
        "ingest_run_id": "11111111-1111-1111-1111-111111111111",
        "received_at": _TS,
    }
    record.update(overrides)
    return record


def test_contract_spec_distinguishes_snapshot_and_diff_payload_truth():
    spec = orderbook_diff_payload_contract_spec()

    assert spec["storage_table"] == "bronze.market_orderbook_payloads"
    assert "bronze.market_orderbook_bbo" in spec["supported_snapshot_tables"]
    assert "bronze.market_orderbook_books5" in spec["supported_snapshot_tables"]
    assert "row_checksum" in spec["primary_identity"]
    assert "raw_payload" not in spec["required_write_fields"][CAPTURE_STATUS_SNAPSHOT_ONLY]
    assert "raw_payload" in spec["required_write_fields"][CAPTURE_STATUS_DIFF_PERSISTED]
    assert "no execution schema/table may store payload truth" in spec["required_constraints"]


def test_snapshot_only_record_valid_with_collector_sequence_but_no_payload():
    result = validate_orderbook_diff_payload_record(_base_record())

    assert result.ok is True
    assert result.missing_fields == ()
    assert result.errors == ()


def test_diff_payload_record_requires_payload_hash_schema_kind_and_raw_payload():
    result = validate_orderbook_diff_payload_record(
        _base_record(capture_status=CAPTURE_STATUS_DIFF_PERSISTED)
    )

    assert result.ok is False
    assert result.missing_fields == (
        "payload_hash",
        "payload_schema_version",
        "payload_kind",
        "raw_payload",
    )


def test_diff_payload_record_accepts_public_orderbook_payload():
    result = validate_orderbook_diff_payload_record(
        _base_record(
            capture_status=CAPTURE_STATUS_DIFF_PERSISTED,
            payload_hash=_SHA_B,
            payload_schema_version=ORDERBOOK_DIFF_PAYLOAD_SCHEMA_VERSION,
            payload_kind="okx_books5_snapshot",
            raw_payload={
                "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                "data": [{"bids": [["64000", "1"]], "asks": [["64001", "2"]]}],
            },
        )
    )

    assert result.ok is True
    assert result.errors == ()


def test_contract_rejects_execution_db_payload_storage():
    result = validate_orderbook_diff_payload_record(
        _base_record(storage_table="execution.orderbook_payloads")
    )

    assert result.ok is False
    assert "storage_table_must_be_bronze_orderbook_payloads" in result.errors
    assert "execution_db_payload_storage_forbidden" in result.errors


def test_contract_rejects_sensitive_payload_keys():
    result = validate_orderbook_diff_payload_record(
        _base_record(
            capture_status=CAPTURE_STATUS_DIFF_PERSISTED,
            payload_hash=_SHA_B,
            payload_schema_version=ORDERBOOK_DIFF_PAYLOAD_SCHEMA_VERSION,
            payload_kind="okx_books5_snapshot",
            raw_payload={"data": [{"token": "redacted"}]},
        )
    )

    assert result.ok is False
    assert "sensitive_payload_key:raw_payload.data[0].token" in result.errors


def test_contract_rejects_bad_sequence_checksum_and_scope():
    result = validate_orderbook_diff_payload_record(
        _base_record(
            collector_sequence=0,
            collector_sequence_scope="per_process_wall_clock",
            row_checksum="sha256:not-a-digest",
        )
    )

    assert result.ok is False
    assert "collector_sequence_must_be_positive_int" in result.errors
    assert "invalid_collector_sequence_scope" in result.errors
    assert "invalid_row_checksum" in result.errors


def test_required_write_fields_are_status_specific():
    snapshot_fields = required_write_fields_for_status(CAPTURE_STATUS_SNAPSHOT_ONLY)
    diff_fields = required_write_fields_for_status(CAPTURE_STATUS_DIFF_PERSISTED)

    assert "collector_sequence" in snapshot_fields
    assert "row_checksum" in snapshot_fields
    assert "raw_payload" not in snapshot_fields
    assert "payload_hash" in diff_fields
    assert "raw_payload" in diff_fields
