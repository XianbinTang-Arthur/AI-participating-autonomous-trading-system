from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from aats.bootstrap.active_parameters import build_settings_overrides
from aats.data_platform.attribution.alignment import align_replay_with_live
from aats.data_platform.attribution.layer_classifier import classify_all
from aats.data_platform.decision_system.readiness_evaluator import (
    evaluate_promotion_readiness,
)
from aats.data_platform.decision_system.evidence_bundle import (
    PHASE2_PROMOTION_QUALIFICATION_POLICY,
)
from aats.schemas.decision import DecisionContext
from aats.schemas.market import KlineBar, MarketSnapshot
from aats.schemas.strategy_runtime import StrategySleeveIntent
from aats.services.strategy_engines.coordinator import StrategyCoordinatorService
from aats.storage.sqlalchemy_models import Base, StrategySleeveIntentModel
from aats.storage.session import _has_executable_sql
from aats.storage.strategy_runtime_repo_postgres import PostgresStrategyRuntimeRepository
from scripts import (
    rdp_run_full_pipeline,
    rdp_run_live_attribution,
    rdp_run_phase3_round,
)


_BAR_START = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _replay(*, ts: datetime = _BAR_START) -> dict[str, object]:
    return {
        "ts": ts.isoformat(),
        "selectable": True,
        "action": "open",
        "state": "ready",
        "execution_compatible": True,
        "blocking_reasons": "",
        "expected_net_edge_bps": 2.5,
        "bar_index": 1,
    }


def _live_intent(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "sleeve_intent_id": "sintent_1",
        "decision_id": "decision_1",
        "allocation_id": "allocation_1",
        "created_at": _BAR_START + timedelta(minutes=1),
        "timeframe": "15m",
        "signal_bar_start": _BAR_START,
        "signal_bar_end": _BAR_START + timedelta(minutes=15),
        "market_data_asof": _BAR_START + timedelta(seconds=5),
        "parameter_set_id": "ps_1",
        "runtime_generation": "abc123def456-20260826T120000Z-1-2",
        "code_version": "abc123def456",
        "market_snapshot_ref": "evt_market_1",
        "feature_snapshot_ref": "evt_feature_1",
        "route_action": "override_target",
        "state": "ready",
        "automatic_enabled": True,
    }
    row.update(overrides)
    return row


def test_exact_lineage_aligns_even_when_created_at_is_not_the_bar_key() -> None:
    rows = align_replay_with_live(
        [_replay()],
        [_live_intent(created_at=_BAR_START + timedelta(minutes=14))],
        timeframe="15m",
    )

    assert [row["alignment_status"] for row in rows] == ["aligned"]
    assert rows[0]["live_parameter_set_id"] == "ps_1"
    assert rows[0]["live_runtime_generation"].startswith("abc123def456-")
    assert rows[0]["live_opening"] is True


def test_created_at_proximity_cannot_override_a_different_signal_bar() -> None:
    rows = align_replay_with_live(
        [_replay()],
        [
            _live_intent(
                signal_bar_start=_BAR_START + timedelta(minutes=15),
                signal_bar_end=_BAR_START + timedelta(minutes=30),
                market_data_asof=_BAR_START + timedelta(minutes=15, seconds=5),
            )
        ],
        timeframe="15m",
    )

    assert {row["alignment_status"] for row in rows} == {"replay_only", "live_only"}


def test_legacy_live_intent_is_unattributable_instead_of_guessed() -> None:
    rows = align_replay_with_live(
        [_replay()],
        [_live_intent(timeframe=None, signal_bar_start=None)],
        timeframe="15m",
    )

    assert [row["alignment_status"] for row in rows] == [
        "replay_only",
        "unattributable",
    ]
    assert "timeframe" in rows[1]["lineage_error"]
    assert "signal_bar_start" in rows[1]["lineage_error"]


def _readiness_evidence(*, aligned: int, unattributable: int, live_ok: bool) -> dict:
    return {
        "phase2_evidence": {
            "promotion_qualification_policy": (
                PHASE2_PROMOTION_QUALIFICATION_POLICY
            ),
            "combo_stats": {
                "independent_15m": {
                    "available": True,
                    "experiments_with_openings": 1,
                    "mean_positive_edge_ratio": 0.3,
                }
            }
        },
        "phase3_evidence": {
            "round_count": 1,
            "latest_round": {
                "replay_only": False,
                "live_query_succeeded": live_ok,
                "combos": {
                    "independent_15m": {
                        "status": "succeeded",
                        "alignment_stats": {
                            "aligned": aligned,
                            "unattributable": unattributable,
                        },
                    }
                },
            },
        },
        "phase4_evidence": {
            "round_count": 1,
            "latest_round": {
                "combos": {
                    "independent_15m": {
                        "cost_summary": {"total_candidates": 1},
                    }
                }
            },
        },
        "phase5_governance_evidence": {
            "quality_health": "healthy",
            "frozen_parameter_sets": [{"parameter_set_id": "ps_1"}],
            "candidate_parameter_sets": [],
        },
    }


def _evaluate_phase3_gate(*, aligned: int, unattributable: int, live_ok: bool) -> dict:
    return evaluate_promotion_readiness(
        _readiness_evidence(
            aligned=aligned,
            unattributable=unattributable,
            live_ok=live_ok,
        ),
        [{"decision": "promote_candidate", "parameter_set_id": "ps_1", "score_ratio": 1.1}],
        [{"decision": "keep_active", "combo_key": "independent_15m", "confidence": "high"}],
    )


def test_readiness_blocks_unproven_live_query() -> None:
    result = _evaluate_phase3_gate(aligned=1, unattributable=0, live_ok=False)
    check = next(item for item in result["checks"] if item["check"] == "attribution_no_severe_issue")
    assert check["passed"] is False
    assert check["detail"] == "live_query_failed_or_unproven"


def test_readiness_blocks_zero_exact_alignment() -> None:
    result = _evaluate_phase3_gate(aligned=0, unattributable=0, live_ok=True)
    check = next(item for item in result["checks"] if item["check"] == "attribution_no_severe_issue")
    assert check["passed"] is False
    assert "zero_exact_alignment" in check["detail"]


def test_readiness_blocks_incomplete_live_lineage() -> None:
    result = _evaluate_phase3_gate(aligned=1, unattributable=2, live_ok=True)
    check = next(item for item in result["checks"] if item["check"] == "attribution_no_severe_issue")
    assert check["passed"] is False
    assert "unattributable_live_lineage" in check["detail"]


def test_readiness_accepts_exact_attribution_evidence() -> None:
    result = _evaluate_phase3_gate(aligned=1, unattributable=0, live_ok=True)
    assert result["readiness"] == "ready_for_next_live_test"


def test_full_pipeline_phase3_command_never_adds_implicit_replay_only() -> None:
    args = SimpleNamespace(
        start="2026-08-25",
        end="2026-08-26",
        dataset_version="v1.0",
        live_db_url=None,
        replay_only=False,
    )
    command = rdp_run_full_pipeline._build_phase3_cmd(
        args,
        ensure=False,
        params_json=None,
    )
    assert "--replay-only" not in command


def test_full_pipeline_phase3_command_does_not_expose_live_db_url() -> None:
    live_db_url = "postgresql://readonly:secret@example.invalid/aats"
    args = SimpleNamespace(
        start="2026-08-25",
        end="2026-08-26",
        dataset_version="v1.0",
        live_db_url=live_db_url,
        replay_only=False,
    )

    command = rdp_run_full_pipeline._build_phase3_cmd(
        args,
        ensure=False,
        params_json=None,
    )

    assert live_db_url not in command
    assert "--live-db-url" not in command


def _aligned_classification_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "alignment_status": "aligned",
        "replay_ts": _BAR_START.isoformat(),
        "live_ts": (_BAR_START + timedelta(minutes=1)).isoformat(),
        "replay_selectable": True,
        "replay_opening": True,
        "live_route_action": "override_target",
        "live_automatic_enabled": True,
        "live_allocation_id": "allocation_1",
        "live_decision_id": "decision_1",
    }
    row.update(overrides)
    return row


def _classification_dependencies() -> dict[str, object]:
    event_ts = _BAR_START + timedelta(minutes=1)
    return {
        "allocations": {
            "allocation_1": {
                "portfolio_approved_notional": 100,
                "route_action": "override_target",
            }
        },
        "budgets": {
            "allocation_1": {
                "budget_multiplier": 1,
                "approved_notional": 100,
                "portfolio_budget_cut_notional": 0,
                "clamped": False,
            }
        },
        "bundles": {
            "decision_1": [{"status": "submitted", "net_approved_exposure": 100}]
        },
        "orders": {
            "decision_1": [{
                "order_id": "order_1",
                "state": "SUBMITTED",
                "requested_qty": 1,
            }]
        },
        "fills": {"order_1": [{"fill_qty": 1}]},
        "recon_snapshots": [{
            "attribution_event_ts": event_ts,
            "created_at": event_ts - timedelta(seconds=1),
            "halt_required": False,
            "only_reduce_required": False,
            "review_required": False,
            "bundle_recovery_required": False,
            "safe_to_trade": True,
            "resume_eligible": True,
        }],
    }


def test_classifier_uses_real_route_action_and_uppercase_order_states() -> None:
    deps = _classification_dependencies()

    held = classify_all(
        [_aligned_classification_row(live_route_action="hold_current")],
        **deps,
    )[0]
    rejected = classify_all(
        [_aligned_classification_row()],
        **{
            **deps,
            "orders": {
                "decision_1": [{
                    "order_id": "order_1",
                    "state": "REJECTED",
                    "requested_qty": 1,
                }]
            },
        },
    )[0]

    assert held["final_attribution_reason"] == "intent_route_action_hold_current"
    assert rejected["final_attribution_reason"] == "order_state_rejected"


def test_classifier_does_not_treat_missing_reconciliation_as_safe() -> None:
    deps = _classification_dependencies()
    classified = classify_all(
        [_aligned_classification_row()],
        **{**deps, "recon_snapshots": []},
    )[0]

    assert classified["final_attribution_category"] == "risk_rejected"
    assert classified["final_attribution_reason"] == "reconciliation_snapshot_missing"


def test_classifier_does_not_treat_missing_budget_snapshot_as_approved() -> None:
    deps = _classification_dependencies()
    classified = classify_all(
        [_aligned_classification_row()],
        **{**deps, "budgets": {}},
    )[0]

    assert classified["final_attribution_category"] == "budget_rejected"
    assert classified["final_attribution_reason"] == "budget_snapshot_missing"


def test_classifier_uses_reconciliation_state_as_of_each_intent() -> None:
    deps = _classification_dependencies()
    first_event = _BAR_START + timedelta(minutes=1)
    second_event = _BAR_START + timedelta(minutes=2)
    rows = [
        _aligned_classification_row(live_ts=first_event.isoformat()),
        _aligned_classification_row(live_ts=second_event.isoformat()),
    ]
    recon = [
        {
            "attribution_event_ts": first_event,
            "created_at": first_event - timedelta(seconds=1),
            "halt_required": False,
            "only_reduce_required": False,
            "review_required": False,
            "safe_to_trade": True,
            "resume_eligible": True,
        },
        {
            "attribution_event_ts": second_event,
            "created_at": second_event - timedelta(seconds=1),
            "halt_required": True,
            "only_reduce_required": False,
            "review_required": False,
            "safe_to_trade": False,
            "resume_eligible": False,
        },
    ]

    classified = classify_all(rows, **{**deps, "recon_snapshots": recon})

    assert classified[0]["final_attribution_category"] == "live_traded"
    assert classified[1]["final_attribution_reason"] == "halt_required"


def test_active_parameter_id_is_carried_with_consumed_runtime_values() -> None:
    registry = {
        "active_sets": {
            "independent_15m": {
                "parameter_set_id": "ps_runtime_1",
                "family": "independent",
                "timeframe": "15m",
                "values": {"signal_edge_scale_bps": 20.0},
            }
        }
    }
    with patch(
        "aats.bootstrap.active_parameters._try_load_from_db",
        return_value=registry,
    ):
        overrides = build_settings_overrides(db_url="postgresql://example.invalid/db")

    assert overrides["strategy_signal_edge_scale_bps"] == 20.0
    assert overrides["active_parameter_set_ids"] == {
        "independent_15m": "ps_runtime_1"
    }


def test_strategy_intent_lineage_uses_market_bar_and_deployment_provenance() -> None:
    service = object.__new__(StrategyCoordinatorService)
    service.settings = SimpleNamespace(
        active_parameter_set_ids={"independent_15m": "ps_runtime_1"},
        config_profile="derivatives",
        runtime_readiness_generation="abc123def456-20260826T120000Z-1-2",
    )
    snapshot = MarketSnapshot(
        symbol="BTC-USDT-SWAP",
        exchange="okx",
        snapshot_ts=_BAR_START + timedelta(seconds=10),
        best_bid=Decimal("99999"),
        best_ask=Decimal("100001"),
        last_price=Decimal("100000"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        volume_24h=Decimal("100"),
        kline_15m=KlineBar(
            open=Decimal("100000"),
            high=Decimal("100010"),
            low=Decimal("99990"),
            close=Decimal("100005"),
            ts=_BAR_START,
        ),
        kline_1h=KlineBar(
            open=Decimal("100000"),
            high=Decimal("100010"),
            low=Decimal("99990"),
            close=Decimal("100005"),
            ts=_BAR_START,
        ),
    )
    context = DecisionContext(
        decision_id="decision_1",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        as_of_ts=snapshot.snapshot_ts,
        market_snapshot_ref="evt_market_1",
        feature_snapshot_ref="evt_feature_1",
        portfolio_snapshot_ref="evt_portfolio_1",
        health_snapshot_ref="evt_health_1",
        mode="manual",
        current_position_qty=Decimal("0"),
        market_snapshot=snapshot,
    )

    lineage = service._intent_attribution_lineage(
        context=context,
        family="independent",
    )

    assert lineage["signal_bar_start"] == _BAR_START
    assert lineage["signal_bar_end"] == _BAR_START + timedelta(minutes=15)
    assert lineage["parameter_set_id"] == "ps_runtime_1"
    assert lineage["runtime_generation"] == "abc123def456-20260826T120000Z-1-2"
    assert lineage["code_version"] == "abc123def456"


def test_full_pipeline_fails_closed_when_live_db_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("RDP_LIVE_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        rdp_run_full_pipeline.sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start", "2026-08-25",
            "--end", "2026-08-26",
            "--start-from", "phase3",
            "--stop-after", "phase3",
            "--dry-run",
        ],
    )
    assert rdp_run_full_pipeline.main() == 2


def test_one_shot_and_round_fail_closed_when_live_db_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("RDP_LIVE_DATABASE_URL", raising=False)
    common = [
        "--family", "independent",
        "--symbol", "BTC-USDT-SWAP",
        "--timeframe", "15m",
        "--start", "2026-08-25",
        "--end", "2026-08-26",
    ]
    monkeypatch.setattr(
        rdp_run_live_attribution.sys,
        "argv",
        ["rdp_run_live_attribution.py", *common],
    )
    assert rdp_run_live_attribution.main() == 2

    monkeypatch.setattr(
        rdp_run_phase3_round.sys,
        "argv",
        [
            "rdp_run_phase3_round.py",
            "--start", "2026-08-25",
            "--end", "2026-08-26",
        ],
    )
    assert rdp_run_phase3_round.main() == 2


def test_phase3_child_receives_live_db_via_environment_not_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(rdp_run_phase3_round.subprocess, "run", _fake_run)
    monkeypatch.setattr(rdp_run_phase3_round, "_list_subdirs", lambda _path: set())
    live_db_url = "postgresql://readonly:secret@example.invalid/aats"

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=Path("unused-test-artifacts"),
        live_db_url=live_db_url,
        replay_only=False,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert live_db_url not in captured["command"]
    assert captured["env"]["RDP_LIVE_DATABASE_URL"] == live_db_url


def test_live_attribution_postgres_session_forces_read_only_pool() -> None:
    fake_engine = object()
    fake_session = object()
    with (
        patch("sqlalchemy.create_engine", return_value=fake_engine) as create_engine_mock,
        patch("sqlalchemy.orm.Session", return_value=fake_session) as session_mock,
    ):
        result = rdp_run_live_attribution._create_live_session(
            "postgresql://readonly@example.invalid/aats?options=-cstatement_timeout%3D30000"
        )

    assert result is fake_session
    session_mock.assert_called_once_with(fake_engine)
    engine_options = create_engine_mock.call_args.kwargs
    assert engine_options["pool_pre_ping"] is True
    assert engine_options["pool_size"] == 3
    assert engine_options["max_overflow"] == 5
    connection_options = engine_options["connect_args"]["options"]
    assert "statement_timeout=30000" in connection_options
    assert "default_transaction_read_only=on" in connection_options


def test_lineage_columns_are_in_model_and_migration() -> None:
    expected = {
        "timeframe",
        "signal_bar_start",
        "signal_bar_end",
        "market_data_asof",
        "parameter_set_id",
        "runtime_generation",
        "code_version",
        "market_snapshot_ref",
        "feature_snapshot_ref",
    }
    assert expected <= set(StrategySleeveIntentModel.__table__.columns.keys())
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations/006_strategy_sleeve_intent_attribution_lineage.sql"
    ).read_text(encoding="utf-8")
    for column in expected:
        assert column in migration


def test_comment_only_root_migration_is_not_sent_as_empty_postgres_query() -> None:
    assert _has_executable_sql("-- baseline marker\n/* no DDL */\n") is False
    assert _has_executable_sql("-- migration\nSELECT 1;\n") is True


def test_sleeve_intent_lineage_persists_in_columns_and_payload() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    repo = PostgresStrategyRuntimeRepository(factory)
    intent = StrategySleeveIntent(
        sleeve_intent_id="sintent_persist_1",
        decision_id="decision_1",
        family="independent",
        strategy_sleeve_id="sleeve_1",
        state="ready",
        symbol="BTC-USDT-SWAP",
        product_type="derivatives",
        margin_mode="cross",
        inventory_policy="inventory_accumulation",
        route_action="override_target",
        timeframe="15m",
        signal_bar_start=_BAR_START,
        signal_bar_end=_BAR_START + timedelta(minutes=15),
        market_data_asof=_BAR_START + timedelta(seconds=5),
        parameter_set_id="ps_runtime_1",
        runtime_generation="abc123def456-20260826T120000Z-1-2",
        code_version="abc123def456",
        market_snapshot_ref="evt_market_1",
        feature_snapshot_ref="evt_feature_1",
    )

    repo.save_sleeve_intent(intent)

    with factory() as session:
        row = session.scalar(
            select(StrategySleeveIntentModel).where(
                StrategySleeveIntentModel.sleeve_intent_id == intent.sleeve_intent_id
            )
        )
        assert row is not None
        assert row.timeframe == "15m"
        assert row.signal_bar_start == _BAR_START.replace(tzinfo=None)
        assert row.parameter_set_id == "ps_runtime_1"
        assert row.runtime_generation.startswith("abc123def456-")
        assert row.payload["timeframe"] == "15m"
        assert row.payload["signal_bar_start"] == "2026-08-26T12:00:00Z"

    engine.dispose()
