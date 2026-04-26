from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from aats.data_platform.orderbook_diff_payload_contract import compute_orderbook_row_checksum
from aats.services.execution_engine.orderbook_snapshot_refs import OrderbookSnapshotReadSource
from aats.services.operator.query_service import OperatorQueryService


class _FakeStateScope:
    product_type = "derivatives"
    margin_mode = "cross"

    @staticmethod
    def symbol_allowed(symbol: str | None) -> bool:
        return symbol == "BTC-USDT-SWAP"


class _FakeExecutionRepo:
    def __init__(
        self,
        *,
        order_states: list[dict] | None = None,
        fills: list[dict] | None = None,
        orderbook_snapshot_read_source: OrderbookSnapshotReadSource | None = None,
    ) -> None:
        self._order_states = order_states or []
        self._fills = fills or []
        self.orderbook_snapshot_read_source = orderbook_snapshot_read_source

    def order_states_for_scope(self, **_kwargs):
        return self._order_states

    def order_states_for_decision(self, decision_id: str):
        return [order for order in self._order_states if order.get("decision_id") == decision_id]

    def fills_for_decisions(self, decision_ids: list[str]):
        allowed = set(decision_ids)
        return [fill for fill in self._fills if fill.get("decision_id") in allowed]


class _FakeMappingResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def first(self) -> dict[str, Any] | None:
        return self._row


class _FakeExecuteResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappingResult:
        return _FakeMappingResult(self._row)


class _FakeOrderbookSession:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        payload_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._rows_by_key = {
            (row["symbol"], row["ts"]): row
            for row in (rows or [])
        }
        self._payload_rows_by_key = {
            (
                row.get("snapshot_table"),
                row.get("symbol"),
                row.get("ts"),
                row.get("row_checksum"),
            ): row
            for row in (payload_rows or [])
        }
        self.execute_calls: list[dict[str, Any]] = []
        self.close_count = 0

    def execute(self, statement: Any, params: dict[str, Any]) -> _FakeExecuteResult:
        self.execute_calls.append(dict(params))
        if "market_orderbook_payloads" in str(statement):
            key = (
                params.get("snapshot_table"),
                params.get("symbol"),
                params.get("ts"),
                params.get("row_checksum"),
            )
            return _FakeExecuteResult(self._payload_rows_by_key.get(key))
        return _FakeExecuteResult(self._rows_by_key.get((params.get("symbol"), params.get("ts"))))

    def close(self) -> None:
        self.close_count += 1


class _FailingOrderbookSession:
    def execute(self, _statement: Any, _params: dict[str, Any]) -> None:
        raise RuntimeError("orderbook_source_unavailable")


def _service(
    *,
    order_states: list[dict] | None = None,
    fills: list[dict] | None = None,
    orderbook_rows: list[dict[str, Any]] | None = None,
    orderbook_payload_rows: list[dict[str, Any]] | None = None,
    failing_orderbook_source: bool = False,
) -> OperatorQueryService:
    if failing_orderbook_source:
        orderbook_source = OrderbookSnapshotReadSource(
            session_factory=lambda: _FailingOrderbookSession(),  # type: ignore[arg-type]
            source_name="aats_research",
        )
    else:
        orderbook_session = _FakeOrderbookSession(orderbook_rows, payload_rows=orderbook_payload_rows)
        orderbook_source = OrderbookSnapshotReadSource(
            session_factory=lambda: orderbook_session,  # type: ignore[arg-type]
            source_name="aats_research",
        ) if orderbook_rows is not None else None
    query = OperatorQueryService.__new__(OperatorQueryService)
    query.state_scope = _FakeStateScope()
    query.runtime = SimpleNamespace(
        execution_repo=_FakeExecutionRepo(
            order_states=order_states,
            fills=fills,
            orderbook_snapshot_read_source=orderbook_source,
        ),
        orderbook_snapshot_read_source=orderbook_source,
    )
    return query


def _audit(**overrides):
    defaults = {
        "decision_context_ref": "ctx-1",
        "position_target_ref": "target-1",
        "policy_decision_ref": "policy-1",
        "risk_decision_ref": "risk-1",
        "decision_outcome_ref": "outcome-1",
        "execution_plan_ref": "plan-1",
        "order_intent_refs": [],
        "order_state_refs": [],
        "fill_event_refs": [],
        "reconciliation_refs": [],
        "strategy_sleeve_intent_refs": [],
        "portfolio_delta_refs": [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _books5_row(
    *,
    ts: datetime,
    source_ts: datetime | None = None,
) -> dict[str, Any]:
    return {
        "symbol": "BTC-USDT-SWAP",
        "ts": ts,
        "source_ts": source_ts or ts,
        "bid_px_1": Decimal("77000.1"),
        "bid_sz_1": Decimal("0.10"),
        "bid_px_2": Decimal("76999.9"),
        "bid_sz_2": Decimal("0.11"),
        "bid_px_3": Decimal("76999.8"),
        "bid_sz_3": Decimal("0.12"),
        "bid_px_4": Decimal("76999.7"),
        "bid_sz_4": Decimal("0.13"),
        "bid_px_5": Decimal("76999.6"),
        "bid_sz_5": Decimal("0.14"),
        "ask_px_1": Decimal("77000.2"),
        "ask_sz_1": Decimal("0.20"),
        "ask_px_2": Decimal("77000.3"),
        "ask_sz_2": Decimal("0.21"),
        "ask_px_3": Decimal("77000.4"),
        "ask_sz_3": Decimal("0.22"),
        "ask_px_4": Decimal("77000.5"),
        "ask_sz_4": Decimal("0.23"),
        "ask_px_5": Decimal("77000.6"),
        "ask_sz_5": Decimal("0.24"),
        "ingest_run_id": "11111111-1111-1111-1111-111111111111",
        "received_at": ts,
    }


def _payload_sidecar_row(
    *,
    row: dict[str, Any],
    collector_sequence: int,
) -> dict[str, Any]:
    return {
        "storage_table": "bronze.market_orderbook_payloads",
        "snapshot_table": "bronze.market_orderbook_books5",
        "symbol": row["symbol"],
        "ts": row["ts"],
        "source_ts": row["source_ts"],
        "collector_sequence": collector_sequence,
        "collector_sequence_scope": "per_ingest_run_symbol_channel",
        "row_checksum": compute_orderbook_row_checksum("bronze.market_orderbook_books5", row),
        "checksum_version": "orderbook_row_v1",
        "capture_status": "diff_payload_persisted",
        "payload_hash": f"sha256:{collector_sequence:064x}",
        "payload_schema_version": "orderbook_diff_payload_v1",
        "payload_kind": "okx_books5_snapshot",
        "exchange_sequence_id": str(1000 + collector_sequence),
        "previous_payload_hash": None,
        "channel": "books5",
        "ingest_run_id": row["ingest_run_id"],
        "received_at": row["received_at"],
    }


def test_decision_truth_chain_links_repo_order_fill_and_lifecycle_refs() -> None:
    query = _service(
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "FILLED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                    }
                },
            }
        ],
        fills=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "fill_id": "fill-1",
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is True
    assert payload["overall_status"] == "complete"
    assert payload["order"]["status"] == "linked"
    assert payload["order"]["client_order_ids"] == ["order-1"]
    assert payload["fill"]["status"] == "linked"
    assert payload["fill"]["fill_ids"] == ["fill-1"]
    assert payload["lifecycle"]["status"] == "linked"
    assert payload["lifecycle"]["stages"] == ["submit"]
    assert payload["provenance"]["status"] == "linked"
    assert payload["execution_science"]["orderbook_context"]["status"] == "missing_after_lifecycle_record"
    assert payload["execution_science"]["orderbook_context"]["missing_market_context_refs_by_stage"] == {
        "submit": [
            "post_event_orderbook_snapshot_ref",
            "pre_event_orderbook_snapshot_ref",
        ]
    }
    assert payload["execution_science"]["sequence_validation"]["status"] == "missing_orderbook_refs"
    assert payload["execution_science"]["sequence_validation"]["missing_snapshot_ref_sequence_stage_count"] == 1


def test_decision_truth_chain_reports_linked_orderbook_context_by_stage() -> None:
    query = _service(
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "SUBMITTED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    orderbook_context = payload["execution_science"]["orderbook_context"]
    assert orderbook_context["status"] == "linked"
    assert orderbook_context["complete"] is True
    assert orderbook_context["complete_stage_count"] == 1
    assert orderbook_context["incomplete_stage_count"] == 0
    assert orderbook_context["stage_evidence"] == [
        {
            "record_kind": "order",
            "record_id": "order-1",
            "client_order_id": "order-1",
            "stage": "submit",
            "status": "linked",
            "missing_market_context_refs": [],
            "orderbook_ref_sequence_status": "snapshot_ref_sequence_validated_row_truth_missing",
        }
    ]
    sequence_validation = payload["execution_science"]["sequence_validation"]
    assert sequence_validation["status"] == "snapshot_ref_sequence_validated_row_truth_missing"
    assert sequence_validation["valid_snapshot_ref_sequence_stage_count"] == 1
    assert sequence_validation["row_truth_missing_stage_count"] == 1
    assert sequence_validation["stage_evidence"][0]["delta_ms"] == 1000
    assert "orderbook_row_truth_missing" in sequence_validation["missing_evidence"]
    assert "pre_orderbook_row_truth_source_unavailable" in sequence_validation["stage_evidence"][0]["missing_evidence"]


def test_decision_truth_chain_resolves_orderbook_rows_and_checksums() -> None:
    pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
    post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
    query = _service(
        orderbook_rows=[
            _books5_row(ts=pre_ts),
            _books5_row(ts=post_ts),
        ],
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "SUBMITTED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    sequence_validation = payload["execution_science"]["sequence_validation"]
    assert sequence_validation["status"] == "local_snapshot_row_sequence_validated_diff_payload_missing"
    assert sequence_validation["valid_snapshot_ref_sequence_stage_count"] == 1
    assert sequence_validation["local_orderbook_row_sequence_stage_count"] == 1
    stage = sequence_validation["stage_evidence"][0]
    assert stage["status"] == "local_snapshot_row_sequence_validated_diff_payload_missing"
    assert stage["ordered_by"] == {
        "snapshot_ref_ts": True,
        "source_ts": True,
        "client_ts": True,
    }
    assert stage["source_delta_ms"] == 1000
    assert stage["client_delta_ms"] == 1000
    assert stage["pre"]["row_exists"] is True
    assert stage["post"]["row_exists"] is True
    assert stage["pre"]["content_checksum"].startswith("sha256:")
    assert stage["post"]["content_checksum"].startswith("sha256:")
    assert stage["pre"]["content_checksum"] != stage["post"]["content_checksum"]
    assert sequence_validation["missing_evidence"] == [
        "local_orderbook_diff_payload_not_exposed",
        "local_orderbook_diff_checksum_not_exposed",
    ]
    assert stage["pre"]["top_of_book"]["source"] == "level1_projection_from_persisted_row"
    assert stage["pre"]["top_of_book"]["bid_px"] == "77000.1"
    assert stage["pre"]["top_of_book"]["bid_sz"] == "0.1"
    assert stage["pre"]["top_of_book"]["ask_px"] == "77000.2"
    assert stage["pre"]["top_of_book"]["ask_sz"] == "0.2"
    assert stage["pre"]["top_of_book"]["mid_px"] == "77000.15"
    assert stage["pre"]["top_of_book"]["spread_px"] == "0.1"
    assert stage["pre"]["top_of_book"]["spread_bps"] is not None


def test_decision_truth_chain_projects_orderbook_payload_sidecar_without_raw_payload() -> None:
    pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
    post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
    pre_row = _books5_row(ts=pre_ts)
    post_row = _books5_row(ts=post_ts)
    query = _service(
        orderbook_rows=[
            pre_row,
            post_row,
        ],
        orderbook_payload_rows=[
            _payload_sidecar_row(row=pre_row, collector_sequence=11),
            _payload_sidecar_row(row=post_row, collector_sequence=12),
        ],
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "SUBMITTED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    sequence_validation = payload["execution_science"]["sequence_validation"]
    assert sequence_validation["status"] == "local_snapshot_row_sequence_validated_diff_payload_persisted"
    assert sequence_validation["complete"] is True
    assert sequence_validation["missing_evidence"] == []
    assert sequence_validation["local_orderbook_diff_payload_stage_count"] == 1
    stage = sequence_validation["stage_evidence"][0]
    assert stage["status"] == "local_snapshot_row_sequence_validated_diff_payload_persisted"
    assert stage["missing_evidence"] == []
    assert stage["pre"]["payload_evidence"]["collector_sequence"] == 11
    assert stage["post"]["payload_evidence"]["collector_sequence"] == 12
    assert stage["pre"]["payload_evidence"]["payload_hash"] == "sha256:" + f"{11:064x}"
    assert stage["post"]["payload_evidence"]["payload_hash"] == "sha256:" + f"{12:064x}"
    assert stage["pre"]["payload_evidence"]["exchange_sequence_id"] == "1011"
    assert stage["post"]["payload_evidence"]["exchange_sequence_id"] == "1012"
    assert stage["pre"]["payload_evidence"]["channel"] == "books5"
    assert stage["pre"]["payload_evidence"]["raw_payload_exposed"] is False
    assert "raw_payload" not in stage["pre"]["payload_evidence"]


def test_decision_truth_chain_reports_fill_feasibility_from_resolved_book_rows() -> None:
    pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
    post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
    query = _service(
        orderbook_rows=[
            _books5_row(ts=pre_ts),
            _books5_row(ts=post_ts),
        ],
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "FILLED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
        fills=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "fill_id": "fill-1",
                "side": "buy",
                "fill_price": Decimal("77000.3"),
                "fill_qty": Decimal("0.05"),
                "fill_notional": Decimal("3850.015"),
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    feasibility = payload["execution_science"]["fill_feasibility"]
    assert feasibility["status"] == "local_book_fill_feasibility_observed"
    assert feasibility["feasible_stage_count"] == 1
    stage = feasibility["stage_evidence"][0]
    assert stage["status"] == "fill_feasibility_observed"
    fill = stage["fill_evidence"][0]
    assert fill["status"] == "top_of_book_size_covers_fill"
    assert fill["side"] == "buy"
    assert fill["expected_price_source"] == "pre_event_top_of_book"
    assert fill["expected_price"] == "77000.2"
    assert fill["top_level_size_covers_fill"] is True
    assert fill["adverse_slippage_bps"] is not None
    assert fill["estimated_adverse_cost_source"] == "fill_notional_x_pre_event_adverse_slippage_bps"


def test_decision_truth_chain_fill_feasibility_reports_no_fill_for_resolved_book_rows() -> None:
    pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
    post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
    query = _service(
        orderbook_rows=[
            _books5_row(ts=pre_ts),
            _books5_row(ts=post_ts),
        ],
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "SUBMITTED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    feasibility = payload["execution_science"]["fill_feasibility"]
    assert feasibility["status"] == "no_fills_for_resolved_book_rows"
    assert feasibility["no_fill_stage_count"] == 1
    assert feasibility["stage_evidence"][0]["status"] == "no_fill_observed_for_client_order"
    assert "fill_absent_for_orderbook_stage" in feasibility["stage_evidence"][0]["missing_evidence"]


def test_decision_truth_chain_fill_feasibility_reports_depth_limited_fill() -> None:
    pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
    post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
    query = _service(
        orderbook_rows=[
            _books5_row(ts=pre_ts),
            _books5_row(ts=post_ts),
        ],
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "FILLED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
        fills=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "fill_id": "fill-1",
                "side": "buy",
                "fill_price": Decimal("77000.3"),
                "fill_qty": Decimal("0.30"),
                "fill_notional": Decimal("23100.09"),
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    feasibility = payload["execution_science"]["fill_feasibility"]
    assert feasibility["status"] == "fill_requires_depth_or_maker_context"
    fill = feasibility["stage_evidence"][0]["fill_evidence"][0]
    assert fill["status"] == "top_of_book_size_insufficient_or_unknown_depth"
    assert fill["top_level_size_covers_fill"] is False


def test_decision_truth_chain_fill_feasibility_keeps_missing_row_evidence_explicit() -> None:
    query = _service(
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "FILLED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
        fills=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "fill_id": "fill-1",
                "side": "buy",
                "fill_price": Decimal("77000.3"),
                "fill_qty": Decimal("0.05"),
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    feasibility = payload["execution_science"]["fill_feasibility"]
    assert feasibility["status"] == "missing_fill_feasibility_evidence"
    assert "orderbook_row_truth_missing" in feasibility["missing_evidence"]
    assert feasibility["stage_evidence"][0]["fill_evidence"] == []


def test_decision_truth_chain_marks_source_timestamp_order_invalid() -> None:
    pre_ts = datetime(2026, 4, 25, 3, 48, 30, tzinfo=timezone.utc)
    post_ts = datetime(2026, 4, 25, 3, 48, 31, tzinfo=timezone.utc)
    query = _service(
        orderbook_rows=[
            _books5_row(ts=pre_ts, source_ts=post_ts),
            _books5_row(ts=post_ts, source_ts=pre_ts),
        ],
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "SUBMITTED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    sequence_validation = payload["execution_science"]["sequence_validation"]
    assert sequence_validation["status"] == "invalid_local_orderbook_sequence"
    assert sequence_validation["invalid_local_orderbook_sequence_stage_count"] == 1
    stage = sequence_validation["stage_evidence"][0]
    assert stage["ordered_by"] == {
        "snapshot_ref_ts": True,
        "source_ts": False,
        "client_ts": True,
    }
    assert "source_ts_order_invalid" in stage["missing_evidence"]


def test_decision_truth_chain_marks_invalid_orderbook_ref_sequence() -> None:
    query = _service(
        order_states=[
            {
                "decision_id": "decision-1",
                "symbol": "BTC-USDT-SWAP",
                "product_type": "derivatives",
                "margin_mode": "cross",
                "client_order_id": "order-1",
                "status": "SUBMITTED",
                "lifecycle_snapshot_refs": {
                    "submit": {
                        "market_snapshot_ref": "mkt-1",
                        "feature_snapshot_ref": "feat-1",
                        "portfolio_snapshot_ref": "port-1",
                        "health_snapshot_ref": "health-1",
                        "market_context_snapshot_refs": {
                            "pre_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:31.000000Z"
                            ),
                            "post_event_orderbook_snapshot_ref": (
                                "bronze.market_orderbook_books5:BTC-USDT-SWAP:2026-04-25T03:48:30.000000Z"
                            ),
                        },
                    }
                },
            }
        ],
    )

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    sequence_validation = payload["execution_science"]["sequence_validation"]
    assert sequence_validation["status"] == "invalid_snapshot_ref_sequence"
    assert sequence_validation["invalid_snapshot_ref_sequence_stage_count"] == 1
    assert sequence_validation["stage_evidence"][0]["status"] == "invalid_ref_order"
    assert "pre_ref_after_post_ref" in sequence_validation["stage_evidence"][0]["missing_evidence"]


def test_decision_truth_chain_distinguishes_clean_no_execution_from_missing_links() -> None:
    query = _service()

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is True
    assert payload["order"]["status"] == "absent_no_order_intent"
    assert payload["fill"]["status"] == "absent_no_order"
    assert payload["lifecycle"]["status"] == "absent_no_execution_record"
    assert payload["execution_science"]["orderbook_context"]["status"] == "absent_no_execution_record"
    assert payload["execution_science"]["sequence_validation"]["status"] == "absent_no_execution_record"
    assert payload["missing_evidence"] == []


def test_decision_truth_chain_marks_order_intent_without_order_state_as_missing() -> None:
    query = _service()

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(order_intent_refs=["intent-1"]),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is False
    assert payload["overall_status"] == "incomplete"
    assert payload["order"]["status"] == "missing_after_order_intent"
    assert "missing_after_order_intent" in payload["missing_evidence"]


def test_decision_truth_chain_marks_provenance_gaps_as_partial() -> None:
    query = _service()

    payload = query._decision_truth_chain_payload(
        decision_id="decision-1",
        audit=_audit(risk_decision_ref=None),
        order_updates=[],
        fills=[],
    )

    assert payload["complete"] is False
    assert payload["provenance"]["status"] == "partial"
    assert payload["provenance"]["missing_required_refs"] == ["risk_decision_ref"]
    assert "provenance_partial" in payload["missing_evidence"]
