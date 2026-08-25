from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aats.data_platform.metrics.effectiveness_backfill import (
    backfill_release_effectiveness,
)
from aats.schemas.common import dump_payload_exact
from aats.schemas.execution import OrderState
from aats.schemas.strategy_runtime import StrategyExecutionBundle
from aats.services.execution_engine.bundle_status_backfill import (
    backfill_independent_blocked_bundles,
)
from scripts.rdp_backfill_independent_blocked_bundles import _resolve_runtime_database_url
from aats.storage.sqlalchemy_models import (
    Base,
    ExecutionOrderModel,
    OrderStateModel,
    StrategyExecutionBundleModel,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_backfill_release_effectiveness_only_processes_rolled_back(tmp_path: Path, monkeypatch) -> None:
    _write_json(
        tmp_path / "artifacts/production_workflow/parameter_release_history.json",
        {
            "releases": [
                {"release_id": "rel_1", "observation_status": "rolled_back"},
                {"release_id": "rel_2", "observation_status": "completed"},
                {"release_id": "rel_3", "observation_status": "rolled_back"},
            ],
        },
    )

    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "aats.data_platform.metrics.effectiveness_backfill.find_effectiveness",
        lambda _root, release_id: {"conclusion": "mixed"} if release_id == "rel_1" else None,
    )

    def _fake_evaluate(root: Path, release_id: str, *, save_result: bool) -> dict:
        calls.append((release_id, save_result))
        return {"release_id": release_id, "conclusion": "rollback_triggered"}

    monkeypatch.setattr(
        "aats.data_platform.metrics.effectiveness_backfill.evaluate_release_effectiveness",
        _fake_evaluate,
    )

    result = backfill_release_effectiveness(tmp_path, save_result=False)

    assert [item[0] for item in calls] == ["rel_1", "rel_3"]
    assert result["processed_count"] == 2
    assert result["changed_count"] == 2
    assert result["error_count"] == 0


def test_backfill_independent_blocked_bundles_reclassifies_review_required(
    request: pytest.FixtureRequest,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    request.addfinalizer(engine.dispose)
    Base.metadata.create_all(
        engine,
        tables=[StrategyExecutionBundleModel.__table__, OrderStateModel.__table__],
    )
    now = datetime.now(timezone.utc)
    bundle = StrategyExecutionBundle(
        bundle_id="bundle_backfill_1",
        decision_id="decision_backfill_1",
        family="independent",
        participating_families=["independent"],
        strategy_sleeve_refs=["sleeve_backfill_1"],
        allocation_id="alloc_backfill_1",
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        route_action="override_target",
        bundle_type="single_sleeve",
        status="review_required",
        selected_symbol="BTC-USDT-SWAP",
        reason_codes=["strategy_bundle_review_required"],
        created_at=now,
    )
    order = OrderState(
        decision_id="decision_backfill_1",
        intent_id="intent_backfill_1",
        client_order_id="cl_backfill_1",
        symbol="BTC-USDT-SWAP",
        status="BLOCKED",
        requested_qty=Decimal("0.01"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("0.01"),
        average_fill_price=None,
        fees=Decimal("0"),
        product_type="derivatives",
        margin_mode="cross",
        position_mode="long_short_mode",
        pos_side="long",
        strategy_family="independent",
        strategy_sleeve_id="sleeve_backfill_1",
        allocation_id="alloc_backfill_1",
        strategy_bundle_id="bundle_backfill_1",
        strategy_leg_role="hedge",
        strategy_execution_mode="independent_long_book",
        created_at=now,
        last_update_ts=now,
        submission_payload={},
    )

    with Session(engine) as session, session.begin():
        session.add(
            StrategyExecutionBundleModel(
                bundle_id=bundle.bundle_id,
                decision_id=bundle.decision_id,
                family=bundle.family,
                strategy_sleeve_id=None,
                allocation_id=bundle.allocation_id,
                product_type=bundle.product_type,
                margin_mode=bundle.margin_mode,
                route_action=bundle.route_action,
                bundle_type=bundle.bundle_type,
                bundle_priority=bundle.bundle_priority,
                status=bundle.status,
                selected_symbol=bundle.selected_symbol,
                gross_requested_exposure=bundle.gross_requested_exposure,
                net_approved_exposure=bundle.net_approved_exposure,
                expected_cost_bps=bundle.expected_cost_bps,
                expected_edge_bps=bundle.expected_edge_bps,
                portfolio_risk_budget_state=bundle.portfolio_risk_budget_state,
                created_at=bundle.created_at,
                payload=dump_payload_exact(bundle),
                row_version=1,
            )
        )
        session.add(
            OrderStateModel(
                client_order_id=order.client_order_id,
                decision_id=order.decision_id,
                intent_id=order.intent_id,
                symbol=order.symbol,
                exchange_order_id=order.exchange_order_id,
                created_at=order.created_at,
                status=order.status,
                submitted_ts=order.submitted_ts,
                last_update_ts=order.last_update_ts,
                requested_qty=order.requested_qty,
                filled_qty=order.filled_qty,
                remaining_qty=order.remaining_qty,
                average_fill_price=order.average_fill_price,
                fees=order.fees,
                reduce_only=order.reduce_only,
                close_only=order.close_only,
                td_mode=order.td_mode,
                position_mode=order.position_mode,
                pos_side=order.pos_side,
                reduce_only_reason=order.reduce_only_reason,
                close_only_reason=order.close_only_reason,
                instrument_family=order.instrument_family,
                settle_currency=order.settle_currency,
                strategy_family=order.strategy_family,
                strategy_sleeve_id=order.strategy_sleeve_id,
                allocation_id=order.allocation_id,
                strategy_bundle_id=order.strategy_bundle_id,
                strategy_leg_role=order.strategy_leg_role,
                product_type=order.product_type,
                margin_mode=order.margin_mode,
                position_intent=order.position_intent,
                payload=dump_payload_exact(order),
            )
        )

    with Session(engine) as session, session.begin():
        result = backfill_independent_blocked_bundles(session)

    assert result["updated"] == 1
    with Session(engine) as session:
        refreshed = session.get(StrategyExecutionBundleModel, "bundle_backfill_1")
        assert refreshed is not None
        assert refreshed.status == "blocked"
        assert "strategy_bundle_blocked" in list((refreshed.payload or {}).get("reason_codes", []))

    # 第二次运行必须是 no-op：candidate query 只看 review_required 的 bundle，
    # 第一次已经搬到 blocked，再跑不应扫到任何行、不应更新、也不应改变 row_version。
    with Session(engine) as session:
        row_version_before = session.get(
            StrategyExecutionBundleModel, "bundle_backfill_1",
        ).row_version

    with Session(engine) as session, session.begin():
        second_result = backfill_independent_blocked_bundles(session)

    assert second_result["scanned"] == 0
    assert second_result["updated"] == 0
    with Session(engine) as session:
        refreshed = session.get(StrategyExecutionBundleModel, "bundle_backfill_1")
        assert refreshed.status == "blocked"
        assert refreshed.row_version == row_version_before


def test_backfill_independent_blocked_bundles_supports_converged_execution_orders(
    request: pytest.FixtureRequest,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    request.addfinalizer(engine.dispose)
    Base.metadata.create_all(
        engine,
        tables=[StrategyExecutionBundleModel.__table__, ExecutionOrderModel.__table__],
    )
    now = datetime.now(timezone.utc)
    bundle = StrategyExecutionBundle(
        bundle_id="bundle_backfill_converged_1",
        decision_id="decision_backfill_converged_1",
        family="independent",
        participating_families=["independent"],
        strategy_sleeve_refs=["sleeve_backfill_converged_1"],
        allocation_id="alloc_backfill_converged_1",
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        route_action="override_target",
        bundle_type="single_sleeve",
        status="review_required",
        selected_symbol="BTC-USDT-SWAP",
        reason_codes=["strategy_bundle_review_required"],
        created_at=now,
    )
    order = OrderState(
        decision_id="decision_backfill_converged_1",
        intent_id="intent_backfill_converged_1",
        client_order_id="cl_backfill_converged_1",
        symbol="BTC-USDT-SWAP",
        status="BLOCKED",
        requested_qty=Decimal("0.01"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("0.01"),
        average_fill_price=None,
        fees=Decimal("0"),
        product_type="derivatives",
        margin_mode="cross",
        position_mode="long_short_mode",
        pos_side="short",
        strategy_family="independent",
        strategy_sleeve_id="sleeve_backfill_converged_1",
        allocation_id="alloc_backfill_converged_1",
        strategy_bundle_id="bundle_backfill_converged_1",
        strategy_leg_role="hedge",
        strategy_execution_mode="independent_short_book",
        created_at=now,
        last_update_ts=now,
        submission_payload={},
    )

    with Session(engine) as session, session.begin():
        session.add(
            StrategyExecutionBundleModel(
                bundle_id=bundle.bundle_id,
                decision_id=bundle.decision_id,
                family=bundle.family,
                strategy_sleeve_id=None,
                allocation_id=bundle.allocation_id,
                product_type=bundle.product_type,
                margin_mode=bundle.margin_mode,
                route_action=bundle.route_action,
                bundle_type=bundle.bundle_type,
                bundle_priority=bundle.bundle_priority,
                status=bundle.status,
                selected_symbol=bundle.selected_symbol,
                gross_requested_exposure=bundle.gross_requested_exposure,
                net_approved_exposure=bundle.net_approved_exposure,
                expected_cost_bps=bundle.expected_cost_bps,
                expected_edge_bps=bundle.expected_edge_bps,
                portfolio_risk_budget_state=bundle.portfolio_risk_budget_state,
                created_at=bundle.created_at,
                payload=dump_payload_exact(bundle),
                row_version=1,
            )
        )
        session.add(
            ExecutionOrderModel(
                order_id="cl_backfill_converged_1",
                intent_id=order.intent_id,
                decision_id=order.decision_id,
                execution_attempt_id=order.execution_attempt_id,
                client_order_id=order.client_order_id,
                venue_order_id=order.exchange_order_id,
                symbol=order.symbol,
                side="sell",
                order_type="market",
                time_in_force="IOC",
                requested_qty=order.requested_qty,
                limit_price=None,
                reduce_only=order.reduce_only,
                close_only=order.close_only,
                td_mode=order.td_mode,
                position_mode=order.position_mode,
                pos_side=order.pos_side,
                reduce_only_reason=order.reduce_only_reason,
                close_only_reason=order.close_only_reason,
                instrument_family=order.instrument_family,
                settle_currency=order.settle_currency,
                strategy_family=order.strategy_family,
                strategy_sleeve_id=order.strategy_sleeve_id,
                allocation_id=order.allocation_id,
                strategy_bundle_id=order.strategy_bundle_id,
                strategy_leg_role=order.strategy_leg_role,
                product_type=order.product_type,
                margin_mode=order.margin_mode,
                execution_action=order.execution_action,
                position_intent=order.position_intent,
                state=order.status,
                state_version=1,
                source_system="aats",
                last_exchange_ts=order.last_exchange_update_ts,
                created_at=order.created_at,
                updated_at=order.last_update_ts or order.created_at,
                raw_payload={"order_state": dump_payload_exact(order)},
            )
        )

    with Session(engine) as session, session.begin():
        result = backfill_independent_blocked_bundles(session)

    assert result["storage_source"] == "converged"
    assert result["updated"] == 1
    with Session(engine) as session:
        refreshed = session.get(StrategyExecutionBundleModel, "bundle_backfill_converged_1")
        assert refreshed is not None
        assert refreshed.status == "blocked"
        assert "strategy_bundle_blocked" in list((refreshed.payload or {}).get("reason_codes", []))


def test_resolve_runtime_database_url_uses_runtime_profile_loader(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def _fake_load_profiled_dotenv_into_process(project_root: Path, profile: str | None = None) -> Path:
        calls.append(("load_profile", profile))
        return project_root / ".env.derivatives.live"

    monkeypatch.setattr(
        "aats.bootstrap.env_profiles.load_profiled_dotenv_into_process",
        _fake_load_profiled_dotenv_into_process,
    )
    monkeypatch.setattr(
        "aats.bootstrap.config.load_settings",
        lambda: type(
            "Settings",
            (),
            {"storage_mode": "postgres", "database_url": "postgresql://runtime-db"},
        )(),
    )

    resolved = _resolve_runtime_database_url(profile="derivatives_live")

    assert resolved == "postgresql://runtime-db"
    assert calls == [("load_profile", "derivatives_live")]


def test_resolve_runtime_database_url_shims_aats_profile(monkeypatch) -> None:
    monkeypatch.setenv("AATS_PROFILE", "derivatives_live")
    monkeypatch.delenv("AATS_STARTUP_PROFILE", raising=False)
    monkeypatch.delenv("AATS_ENV_TEMPLATE_PROFILE", raising=False)
    monkeypatch.setattr(
        "aats.bootstrap.config.load_settings",
        lambda: type(
            "Settings",
            (),
            {"storage_mode": "postgres", "database_url": "postgresql://runtime-db"},
        )(),
    )

    resolved = _resolve_runtime_database_url(profile=None)

    assert resolved == "postgresql://runtime-db"
    assert os.environ["AATS_STARTUP_PROFILE"] == "derivatives"
    assert os.environ["AATS_ENV_TEMPLATE_PROFILE"] == "derivatives_live"


def test_resolve_runtime_database_url_falls_back_to_compose_env(monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "aats")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret value")
    monkeypatch.setenv("AATS_DB_NAME", "aats_live_derivatives")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setattr(
        "aats.bootstrap.config.load_settings",
        lambda: type(
            "Settings",
            (),
            {"storage_mode": "postgres", "database_url": None},
        )(),
    )

    resolved = _resolve_runtime_database_url(profile=None)

    assert resolved == "postgresql+psycopg://aats:secret+value@postgres:5432/aats_live_derivatives"
