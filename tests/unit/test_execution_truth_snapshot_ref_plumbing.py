"""Execution truth snapshot-ref plumbing 锁定测试。

覆盖 SoW docs/task/golden_path_execution_truth_snapshot_ref_plumbing_sow.md：
- OrderIntent / LegOrderIntent / OrderState / FillEvent 新增 4 个可选 snapshot refs
  (market / feature / portfolio / health) 默认 None（backward-compatible）。
- leg_intent_from_order_intent / order_intent_from_leg_order_intent 互转保留 refs。
- order_service._ensure_order_row 在 raw_payload 顶层落库 refs。
- outbox._ensure_execution_order_row / _ensure_execution_fill_row raw_payload 顶层
  落库 refs；上游没有 refs 时仍工作（None）。
- order_service._intent_from_order_state / shadow.intent_from_fill 保留 refs。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.schemas.execution import (
    ExecutionPlan,
    FillEvent,
    LegExecutionPlan,
    LegOrderIntent,
    OrderIntent,
    OrderState,
    leg_intent_from_order_intent,
    order_intent_from_leg_order_intent,
)


_REFS = {
    "market_snapshot_ref": "mkt_snap_abc",
    "feature_snapshot_ref": "feat_snap_def",
    "portfolio_snapshot_ref": "port_snap_ghi",
    "health_snapshot_ref": "health_snap_jkl",
}

_ACK_REFS = {
    "market_snapshot_ref": "mkt_snap_ack",
    "feature_snapshot_ref": "feat_snap_ack",
    "portfolio_snapshot_ref": "port_snap_ack",
    "health_snapshot_ref": "health_snap_ack",
}


def _make_intent(**overrides: Any) -> OrderIntent:
    base = dict(
        intent_id="intent_snapref",
        decision_id="decision_snapref",
        symbol="BTC-USDT-SWAP",
        side="buy",
        quantity=Decimal("0.01"),
        execution_style="bounded_limit_ioc",
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        idempotency_key="idem_snapref",
    )
    base.update(overrides)
    return OrderIntent(**base)


def _make_leg_intent(**overrides: Any) -> LegOrderIntent:
    base = dict(
        leg_intent_id="leg_intent_snapref",
        decision_id="decision_snapref",
        symbol="BTC-USDT-SWAP",
        side="buy",
        pos_side="long",
        action="open",
        quantity=Decimal("0.01"),
        execution_style="taker",
        order_type="market",
        urgency="medium",
        time_in_force="IOC",
        idempotency_key="idem_leg_snapref",
        product_type="derivatives",
        margin_mode="cross",
        position_mode="long_short_mode",
    )
    base.update(overrides)
    return LegOrderIntent(**base)


def _make_order_state(**overrides: Any) -> OrderState:
    base = dict(
        decision_id="decision_snapref",
        intent_id="intent_snapref",
        symbol="BTC-USDT-SWAP",
        client_order_id="cl_snapref",
        status="SUBMITTED",
        requested_qty=Decimal("0.01"),
        remaining_qty=Decimal("0.01"),
    )
    base.update(overrides)
    return OrderState(**base)


def _make_fill_event(**overrides: Any) -> FillEvent:
    base = dict(
        fill_id="fill_snapref",
        decision_id="decision_snapref",
        intent_id="intent_snapref",
        client_order_id="cl_snapref",
        exchange_order_id="ord_snapref",
        symbol="BTC-USDT-SWAP",
        side="buy",
        fill_qty=Decimal("0.01"),
        fill_price=Decimal("75000"),
        fee_amount=Decimal("-0.0375"),
        liquidity_role="taker",
        exchange_timestamp=datetime.now(timezone.utc),
        ingestion_timestamp=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return FillEvent(**base)


class TestSchemaDefaults(unittest.TestCase):
    """SOW §11: 新字段默认 None，保持 backward-compatible。"""

    def test_order_intent_defaults(self) -> None:
        intent = _make_intent()
        self.assertIsNone(intent.market_snapshot_ref)
        self.assertIsNone(intent.feature_snapshot_ref)
        self.assertIsNone(intent.portfolio_snapshot_ref)
        self.assertIsNone(intent.health_snapshot_ref)

    def test_leg_order_intent_defaults(self) -> None:
        leg = _make_leg_intent()
        self.assertIsNone(leg.market_snapshot_ref)
        self.assertIsNone(leg.feature_snapshot_ref)
        self.assertIsNone(leg.portfolio_snapshot_ref)
        self.assertIsNone(leg.health_snapshot_ref)

    def test_order_state_defaults(self) -> None:
        state = _make_order_state()
        self.assertIsNone(state.market_snapshot_ref)
        self.assertIsNone(state.feature_snapshot_ref)
        self.assertIsNone(state.portfolio_snapshot_ref)
        self.assertIsNone(state.health_snapshot_ref)

    def test_fill_event_defaults(self) -> None:
        fill = _make_fill_event()
        self.assertIsNone(fill.market_snapshot_ref)
        self.assertIsNone(fill.feature_snapshot_ref)
        self.assertIsNone(fill.portfolio_snapshot_ref)
        self.assertIsNone(fill.health_snapshot_ref)

    def test_accepts_refs_when_supplied(self) -> None:
        intent = _make_intent(**_REFS)
        self.assertEqual(intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(intent.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(intent.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(intent.health_snapshot_ref, "health_snap_jkl")


class TestLifecycleSnapshotRefPayload(unittest.TestCase):
    """P1 lifecycle linkage：submit/ack/fill refs 使用固定 machine-readable 结构。"""

    def test_merges_existing_submit_stage_when_adding_ack(self) -> None:
        from aats.services.execution_engine.lifecycle_snapshot_refs import lifecycle_snapshot_ref_payload

        existing_payload = {
            "lifecycle_snapshot_refs": {
                "submit": {
                    **_REFS,
                    "source": "execution_outbox_submit",
                }
            }
        }

        payload = lifecycle_snapshot_ref_payload(
            existing_raw_payload=existing_payload,
            stage="ack",
            refs=_ACK_REFS,
            source="converged_execution_repo",
        )

        lifecycle = payload["lifecycle_snapshot_refs"]
        self.assertEqual(lifecycle["submit"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(lifecycle["ack"]["market_snapshot_ref"], "mkt_snap_ack")
        self.assertEqual(lifecycle["ack"]["source"], "converged_execution_repo")


class TestRoundTripHelpersPreserveRefs(unittest.TestCase):
    """leg_intent_from_order_intent / order_intent_from_leg_order_intent 双向保留 refs。"""

    def test_leg_intent_from_order_intent_preserves_refs(self) -> None:
        intent = _make_intent(
            position_mode="long_short_mode",
            pos_side="long",
            leg_action="open",
            **_REFS,
        )
        leg = leg_intent_from_order_intent(intent)
        self.assertIsNotNone(leg)
        assert leg is not None  # for type checker
        self.assertEqual(leg.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(leg.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(leg.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(leg.health_snapshot_ref, "health_snap_jkl")

    def test_order_intent_from_leg_order_intent_preserves_refs(self) -> None:
        leg = _make_leg_intent(**_REFS)
        intent = order_intent_from_leg_order_intent(leg)
        self.assertEqual(intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(intent.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(intent.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(intent.health_snapshot_ref, "health_snap_jkl")

    def test_legacy_fixture_without_refs_still_roundtrips(self) -> None:
        """旧夹具不传 refs 时 None → None，不抛错。"""
        intent = _make_intent(
            position_mode="long_short_mode", pos_side="long", leg_action="open",
        )
        leg = leg_intent_from_order_intent(intent)
        assert leg is not None
        self.assertIsNone(leg.market_snapshot_ref)
        rebuilt = order_intent_from_leg_order_intent(leg)
        self.assertIsNone(rebuilt.market_snapshot_ref)


class _FakeOrderStateRepo:
    """极简 stub — 捕获 create_order 的 raw_payload 做断言。"""

    def __init__(self) -> None:
        self.last_raw_payload: dict[str, Any] | None = None
        self.last_intent: OrderIntent | None = None

    def get_order_by_client_order_id(self, client_order_id: str):
        return None

    def create_order(
        self,
        *,
        order_id: str,
        intent: OrderIntent,
        initial_state: str,
        created_at: datetime,
        raw_payload: dict[str, Any],
    ) -> None:
        self.last_raw_payload = raw_payload
        self.last_intent = intent


class TestOrderServiceEnsureOrderRow(unittest.TestCase):
    """order_service._ensure_order_row 顶层落库 refs。"""

    def _make_service(self):
        from aats.services.execution_control.order_service import ExecutionOrderService

        fake_repo = _FakeOrderStateRepo()
        service = object.__new__(ExecutionOrderService)
        service.execution_order_repo = fake_repo  # type: ignore[attr-defined]
        service.execution_order_history_repo = None  # type: ignore[attr-defined]
        return service, fake_repo

    def test_raw_payload_contains_refs_at_top_level(self) -> None:
        service, fake_repo = self._make_service()
        intent = _make_intent(**_REFS)
        service._ensure_order_row(
            intent=intent,
            client_order_id="cl_snapref",
            initial_state="SUBMITTED",
            order_state=None,
        )
        self.assertIsNotNone(fake_repo.last_raw_payload)
        payload = fake_repo.last_raw_payload
        assert payload is not None
        self.assertEqual(payload["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(payload["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(payload["portfolio_snapshot_ref"], "port_snap_ghi")
        self.assertEqual(payload["health_snapshot_ref"], "health_snap_jkl")
        lifecycle = payload["lifecycle_snapshot_refs"]
        self.assertEqual(lifecycle["submit"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(lifecycle["submit"]["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(lifecycle["submit"]["source"], "execution_order_service")
        # 既有顶层锚点字段继续存在（避免回归 Path C 观测性修复）。
        self.assertEqual(payload["execution_style"], "bounded_limit_ioc")

    def test_raw_payload_refs_none_when_intent_has_no_refs(self) -> None:
        """旧夹具不传 refs → 顶层字段值为 None，不抛错。"""
        service, fake_repo = self._make_service()
        intent = _make_intent()
        service._ensure_order_row(
            intent=intent,
            client_order_id="cl_snapref_legacy",
            initial_state="SUBMITTED",
            order_state=None,
        )
        payload = fake_repo.last_raw_payload
        assert payload is not None
        self.assertIsNone(payload["market_snapshot_ref"])
        self.assertIsNone(payload["feature_snapshot_ref"])
        self.assertIsNone(payload["portfolio_snapshot_ref"])
        self.assertIsNone(payload["health_snapshot_ref"])
        lifecycle = payload["lifecycle_snapshot_refs"]
        self.assertIsNone(lifecycle["submit"]["market_snapshot_ref"])
        self.assertEqual(lifecycle["submit"]["source"], "execution_order_service")


class TestIntentFromOrderStatePreservesRefs(unittest.TestCase):
    """_intent_from_order_state（取消路径 shadow intent 重建）保留 refs。"""

    def test_preserves_refs(self) -> None:
        from aats.services.execution_control.order_service import ExecutionOrderService

        state = _make_order_state(**_REFS)
        intent = ExecutionOrderService._intent_from_order_state(state)
        self.assertEqual(intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(intent.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(intent.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(intent.health_snapshot_ref, "health_snap_jkl")

    def test_legacy_state_without_refs(self) -> None:
        from aats.services.execution_control.order_service import ExecutionOrderService

        state = _make_order_state()
        intent = ExecutionOrderService._intent_from_order_state(state)
        self.assertIsNone(intent.market_snapshot_ref)


class TestShadowIntentFromFillPreservesRefs(unittest.TestCase):
    """shadow.intent_from_fill（fill backfill 路径）保留 refs。"""

    def test_preserves_refs(self) -> None:
        from aats.services.execution_control.shadow import Phase1ExecutionShadowService

        fill = _make_fill_event(**_REFS)
        intent = Phase1ExecutionShadowService.intent_from_fill(fill)
        self.assertEqual(intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(intent.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(intent.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(intent.health_snapshot_ref, "health_snap_jkl")

    def test_legacy_fill_without_refs(self) -> None:
        from aats.services.execution_control.shadow import Phase1ExecutionShadowService

        fill = _make_fill_event()
        intent = Phase1ExecutionShadowService.intent_from_fill(fill)
        self.assertIsNone(intent.market_snapshot_ref)


class _CapturingOrderRepo:
    """捕获 create_order_in_session 的 raw_payload。"""

    def __init__(self) -> None:
        self.created_raw_payload: dict[str, Any] | None = None

    def get_order_by_client_order_id_in_session(
        self, session: Any, client_order_id: str, for_update: bool = False
    ):
        return None

    def create_order_in_session(
        self,
        session: Any,
        *,
        order_id: str,
        intent: OrderIntent,
        initial_state: str,
        created_at: datetime,
        raw_payload: dict[str, Any],
    ) -> None:
        self.created_raw_payload = raw_payload


class _CapturingFillRepo:
    def __init__(self) -> None:
        self.saved_raw_payload: dict[str, Any] | None = None

    def save_fill_in_session(
        self,
        session: Any,
        *,
        fill: FillEvent,
        order_id: str,
        source: str,
        raw_payload: dict[str, Any],
    ) -> bool:
        self.saved_raw_payload = raw_payload
        return True


class TestOutboxEnsureExecutionOrderRow(unittest.TestCase):
    """outbox._ensure_execution_order_row 顶层落库 refs。"""

    def _make_publisher(self, order_repo: _CapturingOrderRepo):
        from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher

        publisher = object.__new__(PostgresExecutionOutboxPublisher)
        publisher.execution_order_repo = order_repo  # type: ignore[attr-defined]
        publisher.execution_order_history_repo = None  # type: ignore[attr-defined]
        return publisher

    def test_raw_payload_contains_refs_from_intent(self) -> None:
        order_repo = _CapturingOrderRepo()
        publisher = self._make_publisher(order_repo)
        intent = _make_intent(**_REFS)
        order_state = _make_order_state()  # OrderState 无 refs
        command_payload = {"intent": intent.model_dump(mode="python")}
        publisher._ensure_execution_order_row(
            session=object(),
            order_state=order_state,
            command_type="submit",
            command_payload=command_payload,
        )
        payload = order_repo.created_raw_payload
        assert payload is not None
        self.assertEqual(payload["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(payload["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(payload["portfolio_snapshot_ref"], "port_snap_ghi")
        self.assertEqual(payload["health_snapshot_ref"], "health_snap_jkl")
        lifecycle = payload["lifecycle_snapshot_refs"]
        self.assertEqual(lifecycle["submit"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(lifecycle["submit"]["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(lifecycle["submit"]["source"], "execution_outbox_submit")

    def test_raw_payload_falls_back_to_order_state_refs(self) -> None:
        """command_payload 无 refs（或不存在），但 order_state 带 refs 时仍填充。"""
        order_repo = _CapturingOrderRepo()
        publisher = self._make_publisher(order_repo)
        order_state = _make_order_state(**_REFS)
        # 无 command_payload → _seed_intent_from_command_payload 返回 None，
        # 走 _intent_from_order_state 路径（intent 亦会带 refs）。
        publisher._ensure_execution_order_row(
            session=object(),
            order_state=order_state,
            command_type=None,
            command_payload=None,
        )
        payload = order_repo.created_raw_payload
        assert payload is not None
        self.assertEqual(payload["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(payload["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(payload["portfolio_snapshot_ref"], "port_snap_ghi")
        self.assertEqual(payload["health_snapshot_ref"], "health_snap_jkl")

    def test_raw_payload_refs_none_when_neither_has_refs(self) -> None:
        order_repo = _CapturingOrderRepo()
        publisher = self._make_publisher(order_repo)
        intent = _make_intent()
        order_state = _make_order_state()
        command_payload = {"intent": intent.model_dump(mode="python")}
        publisher._ensure_execution_order_row(
            session=object(),
            order_state=order_state,
            command_type="submit",
            command_payload=command_payload,
        )
        payload = order_repo.created_raw_payload
        assert payload is not None
        self.assertIsNone(payload["market_snapshot_ref"])
        self.assertIsNone(payload["feature_snapshot_ref"])
        self.assertIsNone(payload["portfolio_snapshot_ref"])
        self.assertIsNone(payload["health_snapshot_ref"])
        lifecycle = payload["lifecycle_snapshot_refs"]
        self.assertIsNone(lifecycle["submit"]["market_snapshot_ref"])
        self.assertEqual(lifecycle["submit"]["source"], "execution_outbox_submit")


class TestOutboxEnsureExecutionFillRow(unittest.TestCase):
    """outbox._ensure_execution_fill_row fill raw_payload 顶层落库 refs。"""

    def _make_publisher(
        self,
        order_repo: _CapturingOrderRepo | None,
        fill_repo: _CapturingFillRepo,
    ):
        from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher

        publisher = object.__new__(PostgresExecutionOutboxPublisher)
        publisher.execution_order_repo = order_repo  # type: ignore[attr-defined]
        publisher.execution_order_history_repo = None  # type: ignore[attr-defined]
        publisher.execution_fill_repo = fill_repo  # type: ignore[attr-defined]
        return publisher

    def test_fill_raw_payload_contains_refs(self) -> None:
        order_repo = _CapturingOrderRepo()
        fill_repo = _CapturingFillRepo()
        publisher = self._make_publisher(order_repo, fill_repo)
        fill = _make_fill_event(**_REFS)
        publisher._ensure_execution_fill_row(session=object(), fill=fill)
        fill_payload = fill_repo.saved_raw_payload
        assert fill_payload is not None
        self.assertEqual(fill_payload["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(fill_payload["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(fill_payload["portfolio_snapshot_ref"], "port_snap_ghi")
        self.assertEqual(fill_payload["health_snapshot_ref"], "health_snap_jkl")
        fill_lifecycle = fill_payload["lifecycle_snapshot_refs"]
        self.assertEqual(fill_lifecycle["fill"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(fill_lifecycle["fill"]["source"], "execution_outbox_fill")
        # backfill 路径的 execution_order 行也带 refs（与 submit-time 对称）。
        order_payload = order_repo.created_raw_payload
        assert order_payload is not None
        self.assertEqual(order_payload["market_snapshot_ref"], "mkt_snap_abc")
        order_lifecycle = order_payload["lifecycle_snapshot_refs"]
        self.assertEqual(order_lifecycle["fill"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(order_lifecycle["fill"]["source"], "execution_outbox_fill_backfill")

    def test_fill_raw_payload_refs_none_when_fill_has_no_refs(self) -> None:
        order_repo = _CapturingOrderRepo()
        fill_repo = _CapturingFillRepo()
        publisher = self._make_publisher(order_repo, fill_repo)
        fill = _make_fill_event()
        publisher._ensure_execution_fill_row(session=object(), fill=fill)
        fill_payload = fill_repo.saved_raw_payload
        assert fill_payload is not None
        self.assertIsNone(fill_payload["market_snapshot_ref"])
        self.assertIsNone(fill_payload["feature_snapshot_ref"])
        self.assertIsNone(fill_payload["portfolio_snapshot_ref"])
        self.assertIsNone(fill_payload["health_snapshot_ref"])
        lifecycle = fill_payload["lifecycle_snapshot_refs"]
        self.assertIsNone(lifecycle["fill"]["market_snapshot_ref"])
        self.assertEqual(lifecycle["fill"]["source"], "execution_outbox_fill")


class TestPlanAndTargetSchemaDefaults(unittest.TestCase):
    """SOW §11: PositionTarget / ExecutionPlan / LegExecutionPlan 新字段默认 None。"""

    def _make_execution_plan(self, **overrides: Any) -> ExecutionPlan:
        from decimal import Decimal as _D
        base = dict(
            plan_id="plan_snapref",
            decision_id="decision_snapref",
            symbol="BTC-USDT-SWAP",
            current_position_qty=_D("0"),
            target_position_qty=_D("0.01"),
            approved_target_position_qty=_D("0.01"),
            delta_qty=_D("0.01"),
            side="buy",
            execution_style="taker",
            order_type="market",
            urgency="medium",
            max_slippage_tolerance_bps=30,
        )
        base.update(overrides)
        return ExecutionPlan(**base)

    def _make_leg_plan(self, **overrides: Any) -> LegExecutionPlan:
        from decimal import Decimal as _D
        base = dict(
            plan_id="leg_plan_snapref",
            leg_intent_id="leg_intent_snapref",
            decision_id="decision_snapref",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=_D("0.01"),
            execution_style="taker",
            order_type="market",
            urgency="medium",
            max_slippage_tolerance_bps=30,
            position_intent="open_long",
        )
        base.update(overrides)
        return LegExecutionPlan(**base)

    def test_execution_plan_defaults(self) -> None:
        plan = self._make_execution_plan()
        self.assertIsNone(plan.market_snapshot_ref)
        self.assertIsNone(plan.feature_snapshot_ref)
        self.assertIsNone(plan.portfolio_snapshot_ref)
        self.assertIsNone(plan.health_snapshot_ref)

    def test_execution_plan_accepts_refs(self) -> None:
        plan = self._make_execution_plan(**_REFS)
        self.assertEqual(plan.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(plan.health_snapshot_ref, "health_snap_jkl")

    def test_leg_execution_plan_defaults(self) -> None:
        plan = self._make_leg_plan()
        self.assertIsNone(plan.market_snapshot_ref)
        self.assertIsNone(plan.feature_snapshot_ref)
        self.assertIsNone(plan.portfolio_snapshot_ref)
        self.assertIsNone(plan.health_snapshot_ref)

    def test_leg_execution_plan_accepts_refs(self) -> None:
        plan = self._make_leg_plan(**_REFS)
        self.assertEqual(plan.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(plan.health_snapshot_ref, "health_snap_jkl")

    def test_position_target_defaults(self) -> None:
        from datetime import datetime, timezone
        from decimal import Decimal as _D
        from aats.schemas.decision import PositionTarget
        target = PositionTarget(
            decision_id="decision_snapref",
            symbol="BTC-USDT-SWAP",
            current_position_qty=_D("0"),
            target_position_qty=_D("0.01"),
            delta_position_qty=_D("0.01"),
            current_notional=_D("0"),
            target_notional=_D("750"),
            rebalance_reason="test",
            urgency="medium",
            max_slippage_tolerance_bps=30,
            source_mix={"baseline": 1.0},
            decision_expiry_ts=datetime.now(timezone.utc),
        )
        self.assertIsNone(target.market_snapshot_ref)
        self.assertIsNone(target.feature_snapshot_ref)
        self.assertIsNone(target.portfolio_snapshot_ref)
        self.assertIsNone(target.health_snapshot_ref)


class TestPlannerPlumbsRefs(unittest.TestCase):
    """SOW §11: planner build_plan / build_leg_plan / build_intent / build_leg_intent 保留 refs。"""

    def setUp(self) -> None:
        from aats.bootstrap.settings import AATSSettings
        from aats.services.execution_engine.planner import ExecutionPlanner
        self.planner = ExecutionPlanner(settings=AATSSettings.model_validate({}))

    def test_build_plan_then_build_intent_preserves_refs(self) -> None:
        plan = self.planner.build_plan(
            decision_id="decision_snapref",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            approved_target_position_qty=Decimal("0.01"),
            delta_qty=Decimal("0.01"),
            urgency="medium",
            max_slippage_tolerance_bps=30,
            reference_price=Decimal("75000"),
            product_type="spot",
            **_REFS,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(plan.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(plan.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(plan.health_snapshot_ref, "health_snap_jkl")

        intent = self.planner.build_intent(plan=plan)
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual(intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(intent.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(intent.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(intent.health_snapshot_ref, "health_snap_jkl")

    def test_build_plan_without_refs_leaves_none(self) -> None:
        plan = self.planner.build_plan(
            decision_id="decision_snapref",
            symbol="BTC-USDT",
            current_position_qty=Decimal("0"),
            target_position_qty=Decimal("0.01"),
            approved_target_position_qty=Decimal("0.01"),
            delta_qty=Decimal("0.01"),
            urgency="medium",
            max_slippage_tolerance_bps=30,
            reference_price=Decimal("75000"),
            product_type="spot",
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIsNone(plan.market_snapshot_ref)
        intent = self.planner.build_intent(plan=plan)
        assert intent is not None
        self.assertIsNone(intent.market_snapshot_ref)

    def test_build_leg_plan_then_build_leg_intent_preserves_refs(self) -> None:
        plan = self.planner.build_leg_plan(
            decision_id="decision_snapref",
            symbol="BTC-USDT-SWAP",
            side="buy",
            pos_side="long",
            action="open",
            quantity=Decimal("0.01"),
            urgency="medium",
            max_slippage_tolerance_bps=30,
            reference_price=Decimal("75000"),
            product_type="derivatives",
            position_mode="long_short_mode",
            **_REFS,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(plan.health_snapshot_ref, "health_snap_jkl")

        leg_intent = self.planner.build_leg_intent(plan=plan)
        self.assertIsNotNone(leg_intent)
        assert leg_intent is not None
        self.assertEqual(leg_intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(leg_intent.portfolio_snapshot_ref, "port_snap_ghi")


class TestTargetPositionEnginePlumbsRefs(unittest.TestCase):
    """SOW §8: PositionTarget 从 DecisionContext 继承 refs。"""

    def test_position_target_inherits_decision_context_refs(self) -> None:
        from datetime import datetime, timezone
        from decimal import Decimal as _D
        from aats.bootstrap.settings import AATSSettings
        from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext
        from aats.services.decision_engine.target_position import TargetPositionEngine

        context = DecisionContext(
            decision_id="decision_snapref",
            symbol="BTC-USDT",
            timeframe="15m",
            as_of_ts=datetime.now(timezone.utc),
            market_snapshot_ref="mkt_snap_abc",
            feature_snapshot_ref="feat_snap_def",
            portfolio_snapshot_ref="port_snap_ghi",
            health_snapshot_ref="health_snap_jkl",
            mode="paper_live",
            current_position_qty=_D("0"),
            market_last_price=_D("75000"),
        )
        baseline = BaselineAssessment(
            decision_id="decision_snapref",
            symbol="BTC-USDT",
            regime="range",
            direction_bias="long",
            trend_strength=0.2,
            volatility_state="medium",
            confidence=0.7,
            composite_alpha_score=0.2,
            suggested_position_scale=0.5,
            volatility_target_scale=1.0,
            factor_scores={},
            holding_horizon="15m",
            invalidation_conditions=[],
            reason_codes=["regime_range"],
            engine_version="test",
        )
        ai_assessment: AIMarketAssessment | None = None
        engine = TargetPositionEngine(
            settings=AATSSettings.model_validate({"default_order_qty": 0.001}),
        )
        target = engine.build(context, baseline, ai_assessment)
        self.assertEqual(target.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(target.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(target.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(target.health_snapshot_ref, "health_snap_jkl")


class TestOrderManagerBlockedStatePreservesRefs(unittest.TestCase):
    """SOW §8: OrderManager._blocked_order_state_from_intent 保留 refs。"""

    def test_blocked_order_state_from_intent_carries_refs(self) -> None:
        from aats.services.execution_engine.order_manager import OrderManager

        class _StubAdapter:
            def readiness(self) -> dict[str, str]:
                return {"backend": "paper"}

        manager = object.__new__(OrderManager)
        manager.adapter = _StubAdapter()  # type: ignore[attr-defined]
        intent = _make_intent(**_REFS)
        state = manager._blocked_order_state_from_intent(
            intent=intent,
            client_order_id="cl_snapref",
            submission_mode="test",
            execution_error="test_reason",
        )
        self.assertEqual(state.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(state.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(state.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(state.health_snapshot_ref, "health_snap_jkl")

    def test_blocked_order_state_defaults_to_none_without_refs(self) -> None:
        from aats.services.execution_engine.order_manager import OrderManager

        class _StubAdapter:
            def readiness(self) -> dict[str, str]:
                return {"backend": "paper"}

        manager = object.__new__(OrderManager)
        manager.adapter = _StubAdapter()  # type: ignore[attr-defined]
        intent = _make_intent()
        state = manager._blocked_order_state_from_intent(
            intent=intent,
            client_order_id="cl_snapref_legacy",
            submission_mode="test",
            execution_error="test_reason",
        )
        self.assertIsNone(state.market_snapshot_ref)


class TestOrderManagerHydrateRowPreservesRefs(unittest.TestCase):
    """SOW §8: OrderManager._hydrate_order_state_from_execution_row 从 raw_payload 恢复 refs。"""

    def _make_manager(self):
        from aats.services.execution_engine.order_manager import OrderManager
        from aats.bootstrap.settings import AATSSettings

        class _StubAdapter:
            def readiness(self) -> dict[str, str]:
                return {"backend": "paper"}

        manager = object.__new__(OrderManager)
        manager.adapter = _StubAdapter()  # type: ignore[attr-defined]
        manager.settings = AATSSettings.model_validate({})  # type: ignore[attr-defined]
        return manager

    def test_hydrate_from_order_state_payload(self) -> None:
        manager = self._make_manager()
        row = {
            "decision_id": "decision_snapref",
            "intent_id": "intent_snapref",
            "symbol": "BTC-USDT-SWAP",
            "client_order_id": "cl_snapref",
            "state": "SUBMITTED",
            "requested_qty": Decimal("0.01"),
            "product_type": "spot",
            "margin_mode": "cash",
            "raw_payload": {
                "market_snapshot_ref": "mkt_snap_abc",
                "feature_snapshot_ref": "feat_snap_def",
                "portfolio_snapshot_ref": "port_snap_ghi",
                "health_snapshot_ref": "health_snap_jkl",
                "order_state": {
                    "decision_id": "decision_snapref",
                    "intent_id": "intent_snapref",
                    "symbol": "BTC-USDT-SWAP",
                    "client_order_id": "cl_snapref",
                    "status": "SUBMITTED",
                    "requested_qty": "0.01",
                    "remaining_qty": "0.01",
                    "product_type": "spot",
                    "margin_mode": "cash",
                    "td_mode": "cash",
                },
            },
        }
        state = manager._hydrate_order_state_from_execution_row(row)
        self.assertEqual(state.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(state.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(state.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(state.health_snapshot_ref, "health_snap_jkl")

    def test_hydrate_without_refs_yields_none(self) -> None:
        manager = self._make_manager()
        row = {
            "decision_id": "decision_snapref",
            "intent_id": "intent_snapref",
            "symbol": "BTC-USDT-SWAP",
            "client_order_id": "cl_snapref",
            "state": "SUBMITTED",
            "requested_qty": Decimal("0.01"),
            "product_type": "spot",
            "margin_mode": "cash",
            "raw_payload": {
                "order_state": {
                    "decision_id": "decision_snapref",
                    "intent_id": "intent_snapref",
                    "symbol": "BTC-USDT-SWAP",
                    "client_order_id": "cl_snapref",
                    "status": "SUBMITTED",
                    "requested_qty": "0.01",
                    "remaining_qty": "0.01",
                    "product_type": "spot",
                    "margin_mode": "cash",
                    "td_mode": "cash",
                },
            },
        }
        state = manager._hydrate_order_state_from_execution_row(row)
        self.assertIsNone(state.market_snapshot_ref)


class TestConvergedRepoHydrateOrderStateFallbackPreservesRefs(unittest.TestCase):
    """SOW §8: execution_repo_converged_postgres._hydrate_order_state fallback 路径保留 refs。"""

    def test_fallback_hydrate_picks_up_top_level_refs(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        row = {
            "decision_id": "decision_snapref",
            "intent_id": "intent_snapref",
            "symbol": "BTC-USDT-SWAP",
            "client_order_id": "cl_snapref",
            "state": "SUBMITTED",
            "requested_qty": Decimal("0.01"),
            "product_type": "spot",
            "margin_mode": "cash",
            # raw_payload 顶层带 refs，但无 order_state 子树 → 走 fallback 分支。
            "raw_payload": dict(_REFS),
        }
        state = ConvergedPostgresExecutionRepository._hydrate_order_state(row)
        self.assertEqual(state.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(state.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(state.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(state.health_snapshot_ref, "health_snap_jkl")

    def test_fallback_hydrate_without_refs_yields_none(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        row = {
            "decision_id": "decision_snapref",
            "intent_id": "intent_snapref",
            "symbol": "BTC-USDT-SWAP",
            "client_order_id": "cl_snapref",
            "state": "SUBMITTED",
            "requested_qty": Decimal("0.01"),
            "product_type": "spot",
            "margin_mode": "cash",
            "raw_payload": {},
        }
        state = ConvergedPostgresExecutionRepository._hydrate_order_state(row)
        self.assertIsNone(state.market_snapshot_ref)
        self.assertIsNone(state.feature_snapshot_ref)
        self.assertIsNone(state.portfolio_snapshot_ref)
        self.assertIsNone(state.health_snapshot_ref)

    def test_order_state_dict_path_preserves_refs_via_model_validate(self) -> None:
        """order_state 子树本身带 refs → model_validate 路径自然保留。"""
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        row = {
            "raw_payload": {
                "order_state": {
                    "decision_id": "decision_snapref",
                    "intent_id": "intent_snapref",
                    "symbol": "BTC-USDT-SWAP",
                    "client_order_id": "cl_snapref",
                    "status": "SUBMITTED",
                    "requested_qty": "0.01",
                    "remaining_qty": "0.01",
                    **_REFS,
                },
            },
        }
        state = ConvergedPostgresExecutionRepository._hydrate_order_state(row)
        self.assertEqual(state.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(state.health_snapshot_ref, "health_snap_jkl")

    def test_order_state_dict_path_falls_back_to_top_level_refs(self) -> None:
        """order_state 子树缺 refs 时，仍从 raw_payload 顶层恢复，避免早返回断链。"""
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        row = {
            "raw_payload": {
                **_REFS,
                "order_state": {
                    "decision_id": "decision_snapref",
                    "intent_id": "intent_snapref",
                    "symbol": "BTC-USDT-SWAP",
                    "client_order_id": "cl_snapref",
                    "status": "SUBMITTED",
                    "requested_qty": "0.01",
                    "remaining_qty": "0.01",
                },
            },
        }
        state = ConvergedPostgresExecutionRepository._hydrate_order_state(row)
        self.assertEqual(state.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(state.feature_snapshot_ref, "feat_snap_def")


class TestConvergedRepoHydrateFillFallbackPreservesRefs(unittest.TestCase):
    """SOW §8: execution_repo_converged_postgres._hydrate_fill fallback 路径保留 refs。"""

    def _base_row(self, **overrides: Any) -> dict[str, Any]:
        row = {
            "fill_id": "fill_snapref",
            "decision_id": "decision_snapref",
            "intent_id": "intent_snapref",
            "client_order_id": "cl_snapref",
            "venue_order_id": "ord_snapref",
            "order_id": "ord_snapref",
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "fill_qty": Decimal("0.01"),
            "fill_price": Decimal("75000"),
            "fee_amount": Decimal("-0.0375"),
            "liquidity_role": "taker",
            "exchange_ts": datetime.now(timezone.utc),
            "ingestion_ts": datetime.now(timezone.utc),
            "source_system": "okx",
            "raw_payload": {},
        }
        row.update(overrides)
        return row

    def test_fallback_hydrate_picks_up_top_level_refs(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        row = self._base_row(raw_payload=dict(_REFS))
        fill = ConvergedPostgresExecutionRepository._hydrate_fill(row)
        self.assertEqual(fill.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(fill.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(fill.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(fill.health_snapshot_ref, "health_snap_jkl")

    def test_fallback_hydrate_without_refs_yields_none(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        row = self._base_row()
        fill = ConvergedPostgresExecutionRepository._hydrate_fill(row)
        self.assertIsNone(fill.market_snapshot_ref)
        self.assertIsNone(fill.feature_snapshot_ref)
        self.assertIsNone(fill.portfolio_snapshot_ref)
        self.assertIsNone(fill.health_snapshot_ref)

    def test_fill_event_dict_path_falls_back_to_top_level_refs(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        fill_payload = _make_fill_event().model_dump(mode="python")
        row = self._base_row(raw_payload={**_REFS, "fill_event": fill_payload})
        fill = ConvergedPostgresExecutionRepository._hydrate_fill(row)
        self.assertEqual(fill.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(fill.feature_snapshot_ref, "feat_snap_def")


class TestConvergedRepoIntentFromOrderStatePreservesRefs(unittest.TestCase):
    """SOW §8: execution_repo_converged_postgres._intent_from_order_state 保留 refs。"""

    def test_preserves_refs(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        state = _make_order_state(**_REFS)
        intent = ConvergedPostgresExecutionRepository._intent_from_order_state(state)
        self.assertEqual(intent.market_snapshot_ref, "mkt_snap_abc")
        self.assertEqual(intent.feature_snapshot_ref, "feat_snap_def")
        self.assertEqual(intent.portfolio_snapshot_ref, "port_snap_ghi")
        self.assertEqual(intent.health_snapshot_ref, "health_snap_jkl")

    def test_legacy_state_without_refs(self) -> None:
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        state = _make_order_state()
        intent = ConvergedPostgresExecutionRepository._intent_from_order_state(state)
        self.assertIsNone(intent.market_snapshot_ref)
        self.assertIsNone(intent.feature_snapshot_ref)
        self.assertIsNone(intent.portfolio_snapshot_ref)
        self.assertIsNone(intent.health_snapshot_ref)


class TestConvergedRepoLifecycleSnapshotRefs(unittest.TestCase):
    """P1 lifecycle linkage：converged repo ack 更新必须保留 submit refs 并补 ack refs。"""

    class _OrderRepo:
        def __init__(self, existing: dict[str, Any]) -> None:
            self.existing = existing
            self.updated_raw_payload: dict[str, Any] | None = None

        def get_order_by_client_order_id_in_session(
            self, session: Any, client_order_id: str, for_update: bool = False
        ) -> dict[str, Any]:
            return self.existing

        def update_order_state_in_session(
            self,
            session: Any,
            *,
            order_id: str,
            expected_state_version: int,
            next_state: str,
            venue_order_id: str | None,
            last_exchange_ts: datetime | None,
            updated_at: datetime,
            raw_payload: dict[str, Any],
        ) -> None:
            self.updated_raw_payload = raw_payload

    def test_ack_update_merges_submit_and_ack_lifecycle_refs(self) -> None:
        from aats.services.execution_engine.state_machine import OrderStateMachine
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        client_order_id = "cl_snapref_lifecycle"
        submit_state = _make_order_state(
            client_order_id=client_order_id,
            status="SUBMITTING",
            exchange_order_id=None,
            **_REFS,
        )
        existing = {
            "order_id": client_order_id,
            "state_version": 3,
            "raw_payload": {
                **_REFS,
                "lifecycle_snapshot_refs": {
                    "submit": {
                        **_REFS,
                        "source": "execution_outbox_submit",
                    }
                },
                "order_state": submit_state.model_dump(mode="python"),
            },
        }
        order_repo = self._OrderRepo(existing)
        repo = object.__new__(ConvergedPostgresExecutionRepository)
        repo.execution_order_repo = order_repo  # type: ignore[attr-defined]
        repo.execution_order_history_repo = None  # type: ignore[attr-defined]
        repo.state_machine = OrderStateMachine()  # type: ignore[attr-defined]

        ack_state = _make_order_state(
            client_order_id=client_order_id,
            status="SUBMITTED",
            exchange_order_id="ord_snapref_ack",
            last_update_ts=datetime.now(timezone.utc),
            last_exchange_update_ts=datetime.now(timezone.utc),
            **_ACK_REFS,
        )
        repo.save_order_state_in_session(session=object(), state=ack_state)

        payload = order_repo.updated_raw_payload
        assert payload is not None
        lifecycle = payload["lifecycle_snapshot_refs"]
        self.assertEqual(lifecycle["submit"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(lifecycle["ack"]["market_snapshot_ref"], "mkt_snap_ack")
        self.assertEqual(lifecycle["ack"]["source"], "converged_execution_repo")
        self.assertEqual(payload["market_snapshot_ref"], "mkt_snap_ack")
        self.assertEqual(payload["order_state"]["market_snapshot_ref"], "mkt_snap_ack")


class TestConvergedRepoSyntheticRefreshPreservesRefs(unittest.TestCase):
    """SOW §8: _refresh_synthetic_order_state_from_fills 保留 refs。

    通过伪造 session 与 ExecutionOrderModel-like row 对象，直接调用私有方法，
    验证四种 refs 来源优先级（raw_payload 顶层 > existing order_state > last fill）。
    """

    def _make_repo(self):
        from aats.storage.execution_repo_converged_postgres import (
            ConvergedPostgresExecutionRepository,
        )

        repo = object.__new__(ConvergedPostgresExecutionRepository)
        return repo

    def _stub_row(self, raw_payload: dict[str, Any]) -> Any:
        class _Row:
            def __init__(self) -> None:
                self.raw_payload = raw_payload
                self.decision_id = "decision_snapref"
                self.execution_attempt_id = None
                self.intent_id = "intent_snapref"
                self.symbol = "BTC-USDT-SWAP"
                self.order_id = "ord_snapref"
                self.client_order_id = "cl_snapref"
                self.venue_order_id = "ord_snapref"
                self.requested_qty = Decimal("0.01")
                self.reduce_only = False
                self.close_only = False
                self.td_mode = "cash"
                self.position_mode = None
                self.pos_side = None
                self.reduce_only_reason = None
                self.close_only_reason = None
                self.instrument_family = None
                self.settle_currency = None
                self.strategy_family = None
                self.strategy_sleeve_id = None
                self.allocation_id = None
                self.strategy_bundle_id = None
                self.strategy_leg_role = None
                self.product_type = "spot"
                self.margin_mode = "cash"
                self.execution_action = None
                self.position_intent = "open_long"
                self.created_at = datetime.now(timezone.utc)
                self.updated_at = self.created_at
                self.last_exchange_ts = self.created_at
                self.state = "CREATED"

        return _Row()

    def _stub_session(self, row: Any, fill_models: list[Any]) -> Any:
        class _Result:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def scalars(self):
                class _S:
                    def __init__(self, rows: list[Any]) -> None:
                        self._rows = rows

                    def all(self):
                        return self._rows

                return _S(self._rows)

        class _Session:
            def __init__(self, row: Any, fill_models: list[Any]) -> None:
                self._row = row
                self._fill_models = fill_models

            def get(self, model: Any, key: Any) -> Any:
                return self._row

            def execute(self, _query: Any) -> _Result:
                return _Result(self._fill_models)

        return _Session(row, fill_models)

    def _stub_fill_model(self, **refs: Any) -> Any:
        class _FillRow:
            def __init__(self) -> None:
                now = datetime.now(timezone.utc)
                self.fill_id = "fill_snapref"
                self.venue_fill_id = "venue_fill"
                self.order_id = "ord_snapref"
                self.execution_attempt_id = None
                self.venue_order_id = "ord_snapref"
                self.client_order_id = "cl_snapref"
                self.decision_id = "decision_snapref"
                self.intent_id = "intent_snapref"
                self.symbol = "BTC-USDT-SWAP"
                self.side = "buy"
                self.fill_qty = Decimal("0.01")
                self.fill_price = Decimal("75000")
                self.fee_amount = Decimal("-0.0375")
                self.fee_currency = "USDT"
                self.reduce_only = False
                self.close_only = False
                self.td_mode = "cash"
                self.position_mode = None
                self.pos_side = None
                self.reduce_only_reason = None
                self.close_only_reason = None
                self.instrument_family = None
                self.settle_currency = None
                self.strategy_family = None
                self.strategy_sleeve_id = None
                self.allocation_id = None
                self.strategy_bundle_id = None
                self.strategy_leg_role = None
                self.liquidity_role = "taker"
                self.exchange_ts = now
                self.ingestion_ts = now
                self.source_system = "okx"
                self.raw_payload = dict(refs)
                self.created_at = now

        return _FillRow()

    def test_refresh_prefers_top_level_refs(self) -> None:
        repo = self._make_repo()
        raw_payload = {"source_system": "converged_fill_backfill", **_REFS}
        row = self._stub_row(raw_payload)
        fill_row = self._stub_fill_model()  # fill 本身无 refs
        session = self._stub_session(row, [fill_row])
        repo._refresh_synthetic_order_state_from_fills(session, order_id="ord_snapref")
        new_order_state = row.raw_payload["order_state"]
        self.assertEqual(new_order_state["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(new_order_state["feature_snapshot_ref"], "feat_snap_def")
        self.assertEqual(new_order_state["portfolio_snapshot_ref"], "port_snap_ghi")
        self.assertEqual(new_order_state["health_snapshot_ref"], "health_snap_jkl")
        lifecycle = row.raw_payload["lifecycle_snapshot_refs"]
        self.assertEqual(lifecycle["fill"]["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(lifecycle["fill"]["source"], "converged_fill_refresh")

    def test_refresh_falls_back_to_last_fill_refs(self) -> None:
        repo = self._make_repo()
        raw_payload = {"source_system": "converged_fill_backfill"}
        row = self._stub_row(raw_payload)
        fill_row = self._stub_fill_model(**_REFS)
        session = self._stub_session(row, [fill_row])
        repo._refresh_synthetic_order_state_from_fills(session, order_id="ord_snapref")
        new_order_state = row.raw_payload["order_state"]
        self.assertEqual(new_order_state["market_snapshot_ref"], "mkt_snap_abc")
        self.assertEqual(new_order_state["health_snapshot_ref"], "health_snap_jkl")
        lifecycle = row.raw_payload["lifecycle_snapshot_refs"]
        self.assertEqual(lifecycle["fill"]["market_snapshot_ref"], "mkt_snap_abc")

    def test_refresh_without_any_refs_yields_none(self) -> None:
        repo = self._make_repo()
        raw_payload = {"source_system": "converged_fill_backfill"}
        row = self._stub_row(raw_payload)
        fill_row = self._stub_fill_model()
        session = self._stub_session(row, [fill_row])
        repo._refresh_synthetic_order_state_from_fills(session, order_id="ord_snapref")
        new_order_state = row.raw_payload["order_state"]
        self.assertIsNone(new_order_state["market_snapshot_ref"])
        self.assertIsNone(new_order_state["feature_snapshot_ref"])
        self.assertIsNone(new_order_state["portfolio_snapshot_ref"])
        self.assertIsNone(new_order_state["health_snapshot_ref"])


if __name__ == "__main__":
    unittest.main()
