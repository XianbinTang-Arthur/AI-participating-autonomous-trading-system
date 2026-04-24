"""operator 层 _execution_record_payload 真实度暴露锁定测试。

验证 dict 行经 operator/control-plane 转换后，无需调用方手动拆 raw_payload
即可获得：
- execution_style（来源优先：payload > raw_payload 顶层 > nested intent > fill_event > order_state）
- 四个 snapshot refs (market / feature / portfolio / health)
- fill 行的 raw_exchange（来源优先：payload > raw_payload 顶层 > nested fill_event）

对应 acceptance：/orders/recent、/orders/{id}、/fills/recent 等 phase5 控制面
路径在 repo dict 源头下也能平面化这些字段，符合 execution truth density 目标。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from aats.services.operator.query_service import OperatorQueryService


_REFS = {
    "market_snapshot_ref": "mkt_snap_abc",
    "feature_snapshot_ref": "feat_snap_def",
    "portfolio_snapshot_ref": "port_snap_ghi",
    "health_snapshot_ref": "health_snap_jkl",
}

_RAW_EXCHANGE = {
    "feeRate": "-0.0005",
    "execType": "T",
    "liquidity": "taker",
    "ordType": "market",
    "posSide": "long",
    "tradeId": "v_trade_001",
}


def _make_service() -> OperatorQueryService:
    svc = object.__new__(OperatorQueryService)
    # _execution_record_payload 只依赖 _fill_outcome_map / _action_from_execution_fields
    # / _signed_fee_delta_in_quote；空 outcome map 即可让 has_fill_outcome 分支不执行。
    svc._fill_outcome_map = lambda: {}  # type: ignore[attr-defined]
    return svc


class TestOrderDictRowTruthExposure(unittest.TestCase):
    """phase5 控制面 order 行：execution_style + 4 refs 在 dict 源头下也要平面化。"""

    def _order_row(self, *, raw_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": "cl_snapref",
            "intent_id": "intent_snapref",
            "decision_id": "decision_snapref",
            "client_order_id": "cl_snapref",
            "symbol": "BTC-USDT-SWAP",
            "state": "SUBMITTED",
            "requested_qty": Decimal("0.01"),
            "product_type": "spot",
            "margin_mode": "cash",
            "raw_payload": raw_payload,
        }

    def test_top_level_raw_payload_refs_and_style_are_surfaced(self) -> None:
        svc = _make_service()
        row = self._order_row(
            raw_payload={
                "execution_style": "bounded_limit_ioc",
                **_REFS,
                # 旧 order_service 路径无 nested intent：仅有 top-level 锚点字段
            },
        )
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["execution_style"], "bounded_limit_ioc")
        for key, value in _REFS.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(payload["truth_source"], "execution_order_repo")

    def test_nested_intent_supplies_execution_style_when_top_level_absent(self) -> None:
        """outbox 路径：raw_payload 顶层未必有 execution_style，nested intent 里一定有。"""
        svc = _make_service()
        row = self._order_row(
            raw_payload={
                **_REFS,
                "intent": {
                    "intent_id": "intent_snapref",
                    "decision_id": "decision_snapref",
                    "symbol": "BTC-USDT-SWAP",
                    "side": "buy",
                    "quantity": "0.01",
                    "execution_style": "taker",
                    "order_type": "market",
                    "urgency": "medium",
                    "time_in_force": "IOC",
                    "idempotency_key": "idem_snapref",
                },
            },
        )
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["execution_style"], "taker")
        for key, value in _REFS.items():
            self.assertEqual(payload[key], value)

    def test_hard_column_execution_style_precedes_json_fallback(self) -> None:
        svc = _make_service()
        row = self._order_row(
            raw_payload={
                "execution_style": "bounded_limit_ioc",
            },
        )
        row["execution_style"] = "post_only"
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["execution_style"], "post_only")

    def test_nested_order_state_supplies_refs_when_top_level_absent(self) -> None:
        """raw_payload 顶层无 refs，但 nested order_state 带 refs 时仍暴露。"""
        svc = _make_service()
        row = self._order_row(
            raw_payload={
                "execution_style": "taker",
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
        )
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["execution_style"], "taker")
        for key, value in _REFS.items():
            self.assertEqual(payload[key], value)

    def test_missing_fields_yield_none_no_crash(self) -> None:
        """旧数据无 refs / execution_style → 字段存在且值为 None，保持向后兼容。"""
        svc = _make_service()
        row = self._order_row(raw_payload={})
        payload = svc._execution_record_payload(row)
        self.assertIsNone(payload["execution_style"])
        for key in _REFS:
            self.assertIsNone(payload[key])
        # 既有 truth_source 行为不回归
        self.assertEqual(payload["truth_source"], "execution_order_repo")


class TestFillDictRowTruthExposure(unittest.TestCase):
    """phase5 控制面 fill 行：raw_exchange + 4 refs 在 dict 源头下也要平面化。"""

    def _fill_row(self, *, raw_payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "fill_id": "fill_snapref",
            "order_id": "ord_snapref",
            "client_order_id": "cl_snapref",
            "venue_order_id": "ord_snapref",
            "decision_id": "decision_snapref",
            "intent_id": "intent_snapref",
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "fill_qty": Decimal("0.01"),
            "fill_price": Decimal("75000"),
            "fee_amount": Decimal("-0.0375"),
            "fee_currency": "USDT",
            "liquidity_role": "taker",
            "exchange_ts": now,
            "ingestion_ts": now,
            "source_system": "okx",
            "raw_payload": raw_payload,
        }

    def test_nested_fill_event_supplies_raw_exchange_and_refs(self) -> None:
        svc = _make_service()
        row = self._fill_row(
            raw_payload={
                # 与 outbox._ensure_execution_fill_row 对称：顶层 refs + nested fill_event
                **_REFS,
                "fill_event": {
                    "fill_id": "fill_snapref",
                    "decision_id": "decision_snapref",
                    "intent_id": "intent_snapref",
                    "client_order_id": "cl_snapref",
                    "exchange_order_id": "ord_snapref",
                    "symbol": "BTC-USDT-SWAP",
                    "side": "buy",
                    "fill_qty": "0.01",
                    "fill_price": "75000",
                    "fee_amount": "-0.0375",
                    "liquidity_role": "taker",
                    "exchange_timestamp": datetime.now(timezone.utc).isoformat(),
                    "ingestion_timestamp": datetime.now(timezone.utc).isoformat(),
                    "raw_exchange": dict(_RAW_EXCHANGE),
                    **_REFS,
                },
            },
        )
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["raw_exchange"], _RAW_EXCHANGE)
        self.assertEqual(payload["fee_rate"], "-0.0005")
        self.assertEqual(payload["exec_type"], "T")
        for key, value in _REFS.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(payload["truth_source"], "execution_fill_repo_v2")

    def test_raw_exchange_from_top_level_raw_payload(self) -> None:
        """极端场景：顶层 raw_payload 直接存 raw_exchange（无 nested fill_event）。"""
        svc = _make_service()
        row = self._fill_row(
            raw_payload={
                "raw_exchange": dict(_RAW_EXCHANGE),
                **_REFS,
            },
        )
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["raw_exchange"], _RAW_EXCHANGE)
        self.assertEqual(payload["fee_rate"], "-0.0005")
        self.assertEqual(payload["exec_type"], "T")

    def test_hard_columns_precede_raw_exchange_fallbacks(self) -> None:
        svc = _make_service()
        row = self._fill_row(
            raw_payload={
                "raw_exchange": dict(_RAW_EXCHANGE),
            },
        )
        row["fee_rate"] = "-0.0002"
        row["exec_type"] = "M"
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["fee_rate"], "-0.0002")
        self.assertEqual(payload["exec_type"], "M")

    def test_missing_raw_exchange_yields_none(self) -> None:
        svc = _make_service()
        row = self._fill_row(raw_payload={})
        payload = svc._execution_record_payload(row)
        self.assertIsNone(payload["raw_exchange"])
        for key in _REFS:
            self.assertIsNone(payload[key])

    def test_non_dict_raw_exchange_coerced_to_none(self) -> None:
        """防御：raw_exchange 若是非 dict 类型（历史脏数据）→ 暴露为 None，避免向下游泄露奇怪类型。"""
        svc = _make_service()
        row = self._fill_row(
            raw_payload={
                "raw_exchange": "not_a_dict_but_a_string",
            },
        )
        payload = svc._execution_record_payload(row)
        self.assertIsNone(payload["raw_exchange"])


class TestExecutionRecordPayloadBackwardCompat(unittest.TestCase):
    """保留 truth_source / execution_chain_id / product_type 等既有字段语义。"""

    def test_existing_fields_not_regressed_for_order(self) -> None:
        svc = _make_service()
        row = {
            "order_id": "cl_snapref",
            "intent_id": "intent_snapref",
            "decision_id": "decision_snapref",
            "client_order_id": "cl_snapref",
            "symbol": "BTC-USDT-SWAP",
            "state": "SUBMITTED",
            "requested_qty": Decimal("0.01"),
            "product_type": "derivatives",
            "margin_mode": "cross",
            "execution_action": None,
            "position_intent": "open_long",
            "raw_payload": {
                "execution_chain_id": "chain_abc",
                "execution_attempt_id": "attempt_1",
                "execution_style": "taker",
            },
        }
        payload = svc._execution_record_payload(row)
        self.assertEqual(payload["execution_chain_id"], "chain_abc")
        self.assertEqual(payload["execution_attempt_id"], "attempt_1")
        self.assertEqual(payload["product_type"], "derivatives")
        self.assertEqual(payload["margin_mode"], "cross")
        self.assertEqual(payload["status"], "SUBMITTED")
        self.assertEqual(payload["quantity"], Decimal("0.01"))
        # execution_action 由 position_intent 派生
        self.assertEqual(payload["execution_action"], "open_long")


if __name__ == "__main__":
    unittest.main()
