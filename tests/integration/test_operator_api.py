from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aats.api.auth_routes import auth_router
from aats.api.routes import router
from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.events.envelopes import build_envelope
from aats.schemas.audit import DecisionAuditRecord
from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.ai_shadow import AIDegradationEvent
from aats.schemas.common import utc_now
from aats.schemas.decision import (
    AIMarketAssessment,
    BaselineAssessment,
    DecisionContext,
    DecisionOutcome,
    PositionTarget,
    ProfileControlDecision,
)
from aats.schemas.execution import FillEvent, OrderIntent, OrderState
from aats.schemas.exchange import (
    AccountBaselineSnapshot,
    ExchangeAccountConfiguration,
    ExchangeAccountRiskSnapshot,
    ExchangeAccountSnapshot,
    ExchangeBalance,
    ExchangeFeeSchedule,
    ExchangeOpenOrder,
    ExchangePosition,
    ExchangeSystemStatusItem,
    InstrumentMetadata,
)
from aats.schemas.governance import PolicyDecision, RiskDecision
from aats.events import topics
from aats.schemas.operator import ExecutionErrorSummary, ReplayValidationSummary
from aats.schemas.operator import OperatorUserRecord
from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord
from aats.schemas.strategy_profiles import StrategyProfileMarketRegimeAssessment, StrategyProfileRecommendation
from aats.services.ai_service.provider import AIProviderResponse
from aats.services.operator.strategy_profiles import StrategyProfileControlService
from aats.services.operator.strategy_profiles import _seed_revisions, seed_strategy_profiles
from aats.services.operator.passwords import hash_password
from aats.schemas.strategy_profiles import strategy_profile_payload_from_settings
from tests.support.postgres import temporary_postgres_url


class FakeOperatorAccountService:
    SNAPSHOT: ExchangeAccountSnapshot | None = None
    PRIVATE_ORDER_ROW: dict | None = None
    PRIVATE_ORDER_FILLS: list | None = None

    def __init__(self, *, settings, client, private_ws_client=None) -> None:
        self.settings = settings
        self.client = client
        self.private_ws_client = private_ws_client
        self._snapshot = self.SNAPSHOT

    async def refresh(self, *, force: bool = False):
        return self._snapshot

    def latest_snapshot(self):
        return self._snapshot

    def instrument_metadata(self, symbol: str):
        if self._snapshot is None:
            return None
        for instrument in self._snapshot.instruments:
            if instrument.symbol == symbol:
                return instrument
        return None

    def open_order_count(self, symbol: str | None = None) -> int:
        return len(self._snapshot.open_orders) if self._snapshot is not None else 0

    def recent_fills(self, symbol: str | None = None):
        return list(self._snapshot.fills) if self._snapshot is not None else []

    async def recent_bills(self, *, symbol: str | None = None, limit: int | None = None):
        _ = symbol
        _ = limit
        return [
            {"billId": "bill_1", "type": "1", "subType": "173", "ccy": "USDT", "bal": "1000"},
            {"billId": "bill_2", "type": "2", "subType": "174", "ccy": "USDT", "bal": "998"},
        ]

    def latest_recent_bills(self):
        return [
            {"billId": "bill_1", "type": "1", "subType": "173", "ccy": "USDT", "bal": "1000"},
            {"billId": "bill_2", "type": "2", "subType": "174", "ccy": "USDT", "bal": "998"},
        ]

    def recent_bills_summary(self):
        return {
            "available": True,
            "count": 2,
            "latest_bill_id": "bill_2",
            "latest_bill_ts": utc_now(),
            "currencies": ["USDT"],
            "top_categories": [
                {
                    "type": "1",
                    "sub_type": "173",
                    "currency": "USDT",
                    "count": 1,
                    "type_label": "transfer",
                    "sub_type_label": "funding_fee_expense",
                    "semantic_group": "funding_fee",
                    "human_label": "transfer:funding_fee_expense:USDT",
                }
            ],
            "last_error": None,
        }

    def recent_funding_fee_summary(self, *, symbol: str | None = None):
        _ = symbol
        return {
            "available": True,
            "count": 2,
            "latest_bill_ts": utc_now(),
            "currencies": ["USDT"],
            "net_total_by_currency": {"USDT": "-2"},
            "absolute_total_by_currency": {"USDT": "2"},
            "current_position_notional_usd": Decimal("1000"),
            "funding_fee_bps_proxy": Decimal("20"),
        }

    def latest_private_order_row(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        _ = symbol
        _ = order_id
        _ = client_order_id
        return self.PRIVATE_ORDER_ROW

    def latest_private_order_fills(self, *, symbol: str, order_id: str | None = None, client_order_id: str | None = None):
        _ = symbol
        _ = order_id
        _ = client_order_id
        return list(self.PRIVATE_ORDER_FILLS or [])

    def status(self):
        account_configuration = (
            self._snapshot.account_configuration.model_dump(mode="json")
            if self._snapshot is not None and self._snapshot.account_configuration is not None
            else None
        )
        fee_schedule = (
            self._snapshot.fee_schedule.model_dump(mode="json")
            if self._snapshot is not None and self._snapshot.fee_schedule is not None
            else None
        )
        risk_snapshot = (
            self._snapshot.risk_snapshot.model_dump(mode="json")
            if self._snapshot is not None and self._snapshot.risk_snapshot is not None
            else None
        )
        return {
            "backend": "okx",
            "enabled": True,
            "credentials_configured": True,
            "connected": self._snapshot is not None,
            "fresh": self._snapshot is not None,
            "last_update_ts": self._snapshot.fetched_at if self._snapshot is not None else None,
            "last_error": None,
            "ready": self._snapshot is not None,
            "detail": "fake_operator_account",
            "blockers": [] if self._snapshot is not None else ["account_snapshot_missing"],
            "account_configuration": account_configuration,
            "fee_schedule": fee_schedule,
            "risk_snapshot": risk_snapshot,
            "system_status_items": (
                [item.model_dump(mode="json") for item in self._snapshot.system_status_items]
                if self._snapshot is not None
                else []
            ),
            "maker_fee_rate": None if fee_schedule is None else fee_schedule.get("maker"),
            "taker_fee_rate": None if fee_schedule is None else fee_schedule.get("taker"),
            "fee_rates_source": None if fee_schedule is None else fee_schedule.get("source"),
        }


class FakeShadowProvider:
    async def generate_assessment(self, *, prompt: str, response_schema: dict[str, object]) -> AIProviderResponse:
        _ = prompt
        _ = response_schema
        return AIProviderResponse(
            provider_name="fake_shadow_provider",
            request_id="shadow_req",
            latency_ms=8.0,
            payload={
                "regime": "trend",
                "directional_edge": -0.42,
                "expected_volatility": 0.07,
                "confidence": 0.86,
                "uncertainty": 0.18,
                "expected_holding_horizon": "15m",
                "invalidation_conditions": ["trend_break", "book_flip"],
                "risk_tags": ["shadow_ok"],
                "rationale_summary": "shadow_primary_signal",
                "baseline_override_recommended": True,
                "override_reason_codes": ["ai_trend_override"],
                "execution_parameter_suggestion": {
                    "passive_bias": 0.78,
                    "maker_taker_bias": -0.3,
                    "max_cross_spread_bps": 3.5,
                    "slice_count": 2,
                    "max_participation_rate": 0.2,
                },
            },
        )


class FakeLaggingPhase1ShadowMonitor:
    def snapshot(self) -> dict[str, object]:
        return {
            "configured": True,
            "status": "lagging",
            "connected": True,
            "ready": False,
            "fresh": False,
            "detail": "Phase 1 shadow compatibility layer is behind the legacy runtime.",
            "blockers": ["phase1_shadow_lagging"],
            "summary": "Phase 1 shadow compatibility layer is behind the legacy runtime.",
            "lag": {
                "order_backlog": 2,
                "fill_backlog": 1,
                "obligation_backlog": 0,
            },
            "execution_shadow": {
                "configured": True,
                "status": "healthy",
            },
            "ledger_shadow": {
                "configured": True,
                "status": "healthy",
            },
        }


class TestOperatorAPI(unittest.IsolatedAsyncioTestCase):
    async def test_system_status_and_mode_endpoints_are_operator_readable(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            health = client.get("/system/health")
            mode = client.get("/system/mode")
            runtime_response = client.get("/system/runtime")
            blockers = client.get("/system/blockers")
            blocker_history = client.get("/system/blocker-history?limit=5")
            metrics = client.get("/system/metrics")
            shadow = client.get("/system/shadow")
            trial_guard = client.get("/system/trial-guard")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(mode.status_code, 200)
        self.assertEqual(runtime_response.status_code, 200)
        self.assertEqual(blockers.status_code, 200)
        self.assertEqual(metrics.status_code, 200)
        self.assertEqual(shadow.status_code, 200)

        health_payload = health.json()
        mode_payload = mode.json()
        runtime_payload = runtime_response.json()
        blockers_payload = blockers.json()
        blocker_history_payload = blocker_history.json()
        metrics_payload = metrics.json()
        shadow_payload = shadow.json()
        trial_guard_payload = trial_guard.json()

        self.assertIn("overall_status", health_payload)
        self.assertIn("subsystems", health_payload)
        self.assertIn("execution_summary", health_payload)
        self.assertIn("storage", health_payload["subsystems"])
        self.assertIn("phase1_shadow", health_payload["subsystems"])
        self.assertIn("audit_replay", health_payload["subsystems"])
        self.assertIn("portfolio_snapshot_repairs", health_payload["execution_summary"])
        self.assertIn("phase1_shadow_status", health_payload["execution_summary"])
        self.assertEqual(mode_payload["config_profile"], "local_demo")
        self.assertEqual(mode_payload["market_data_backend"], "demo")
        self.assertEqual(mode_payload["execution_backend"], "paper")
        self.assertEqual(mode_payload["ai_operating_mode"], "baseline_only")
        self.assertEqual(mode_payload["runtime_profile"]["name"], "paper_local")
        self.assertEqual(mode_payload["environment_capabilities"]["execution_adapter_kind"], "paper")
        self.assertFalse(mode_payload["policy_profile"]["exchange_submission_allowed_in_principle"])
        self.assertFalse(mode_payload["recovery_policy"]["operator_rebaseline_supported"])
        self.assertFalse(mode_payload["execution_blocked"])
        self.assertTrue(mode_payload["submit_blocked"])
        self.assertIn("local_demo_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIn("paper_execution_has_no_exchange_submission", mode_payload["submit_blocked_reasons"])
        self.assertIsNone(mode_payload["blocked_reason"])
        self.assertEqual(runtime_payload["symbols"], ["BTC-USDT"])
        self.assertEqual(runtime_payload["enabled_timeframes"], ["15m"])
        self.assertGreaterEqual(runtime_payload["uptime_seconds"], 0.0)
        self.assertEqual(runtime_payload["runtime_profile"]["name"], "paper_local")
        self.assertEqual(runtime_payload["environment_capabilities"]["execution_route"], "paper_local")
        self.assertIn("trial_guard", runtime_payload)
        self.assertIn("baseline_takeover", runtime_payload)
        self.assertIn("decision_cycle_count", metrics_payload)
        self.assertIn("recent_execution_errors", metrics_payload)
        self.assertIn("exposure_summary", metrics_payload)
        self.assertIn("portfolio_snapshot_repair_count", metrics_payload)
        self.assertIn("phase1_shadow", metrics_payload)
        self.assertIn("phase1_shadow_alert_count", metrics_payload)
        self.assertIn("phase1_shadow_recovery_count", metrics_payload)
        self.assertIn("strategy_execution_health", metrics_payload)
        self.assertIn("recent_churn_ratio", metrics_payload["strategy_execution_health"])
        self.assertIn("status", shadow_payload)
        self.assertIn("execution_shadow", shadow_payload)
        self.assertIn("ledger_shadow", shadow_payload)
        self.assertIn("lag", shadow_payload)
        self.assertIn("status", trial_guard_payload)
        self.assertIn("summary", trial_guard_payload)
        self.assertIn("thresholds", trial_guard_payload)
        self.assertIn("phase1_shadow_alert_count", health_payload["execution_summary"])
        self.assertIn("phase1_shadow_recovery_count", health_payload["execution_summary"])
        self.assertIsInstance(blockers_payload["blockers"], list)
        self.assertTrue(any(item["submit_only"] for item in blockers_payload["blockers"]))
        self.assertIn("history", blocker_history_payload)
        self.assertIn("total_available", blocker_history_payload)
        self.assertIn("has_more", blocker_history_payload)
        self.assertIn(health_payload["runtime_state"], {"healthy", "degraded", "blocked", "halted"})

    async def test_derivatives_account_state_and_runtime_expose_structured_exchange_snapshots(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "startup_profile": "derivatives",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
                "okx_api_key": "demo_key",
                "okx_api_secret": "demo_secret",
                "okx_api_passphrase": "demo_passphrase",
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("2500"), available=Decimal("2100"), frozen=Decimal("400"))],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.02"),
                    average_entry_price=Decimal("80000"),
                    mark_price=Decimal("80100"),
                    notional_usd=Decimal("1602"),
                    side="net",
                    margin_mode="cross",
                    margin_currency="USDT",
                    leverage=Decimal("12"),
                    margin_allocated=Decimal("320"),
                    maintenance_margin=Decimal("140"),
                    margin_ratio=Decimal("5.2"),
                    liquidation_price=Decimal("62000"),
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                )
            ],
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT-SWAP",
                    exchange_order_id="ord_deriv_1",
                    client_order_id="cl_deriv_1",
                    side="buy",
                    order_type="limit",
                    status="LIVE",
                    quantity=Decimal("0.01"),
                    filled_quantity=Decimal("0"),
                    price=Decimal("80000"),
                )
            ],
            fills=[],
            instruments=[
                InstrumentMetadata(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    base_currency="BTC",
                    quote_currency="USDT",
                    lot_size=Decimal("0.01"),
                    tick_size=Decimal("0.1"),
                    min_size=Decimal("0.01"),
                    contract_value=Decimal("0.01"),
                    instrument_type="SWAP",
                    instrument_family="BTC-USDT",
                    underlying="BTC-USDT",
                    settle_currency="USDT",
                    contract_value_currency="BTC",
                    max_leverage=Decimal("50"),
                    max_market_size=Decimal("2000"),
                    max_limit_size=Decimal("2500"),
                    state="live",
                ),
                InstrumentMetadata(
                    instrument_id="ETH-USDT-SWAP",
                    symbol="ETH-USDT-SWAP",
                    base_currency="ETH",
                    quote_currency="USDT",
                    lot_size=Decimal("0.1"),
                    tick_size=Decimal("0.01"),
                    min_size=Decimal("0.1"),
                    contract_value=Decimal("0.1"),
                    instrument_type="SWAP",
                    instrument_family="ETH-USDT",
                    underlying="ETH-USDT",
                    settle_currency="USDT",
                    contract_value_currency="ETH",
                    max_leverage=Decimal("25"),
                    max_market_size=Decimal("5000"),
                    max_limit_size=Decimal("6000"),
                    state="live",
                ),
            ],
            account_mode="4",
            position_mode="long_short_mode",
            account_configuration=ExchangeAccountConfiguration(
                account_level_code="4",
                account_level_label="portfolio_margin",
                position_mode="long_short_mode",
                position_mode_label="long_short",
                auto_loan_enabled=True,
                greeks_type="PA",
                isolated_margin_mode="automatic",
                raw={"acctLv": "4", "posMode": "long_short_mode"},
            ),
            fee_rates={"maker": "-0.0002", "taker": "0.0005", "source": "okx_trade_fee"},
            fee_schedule=ExchangeFeeSchedule(
                maker=Decimal("-0.0002"),
                taker=Decimal("0.0005"),
                delivery=Decimal("0.0001"),
                exercise=Decimal("0.00015"),
                source="okx_trade_fee",
                raw={"maker": "-0.0002", "taker": "0.0005"},
            ),
            account_risk={"adjEq": "2500", "imr": "320", "mmr": "140", "mgnRatio": "5.2"},
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("2500"),
                total_equity=Decimal("2550"),
                available_equity=Decimal("2100"),
                initial_margin_requirement=Decimal("320"),
                maintenance_margin_requirement=Decimal("140"),
                margin_ratio=Decimal("5.2"),
                notional_usd=Decimal("12500"),
                raw={"adjEq": "2500", "mgnRatio": "5.2"},
            ),
            system_status=[{"state": "completed", "serviceType": "0"}],
            system_status_items=[
                ExchangeSystemStatusItem(
                    state="completed",
                    service_type="0",
                    title="All Systems Operational",
                    description="No active incident",
                    raw={"state": "completed", "serviceType": "0"},
                )
            ],
        )
        try:
            with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
                runtime = await build_runtime(settings)
            runtime.portfolio_service.state.load_exchange_snapshot(FakeOperatorAccountService.SNAPSHOT)
            await runtime.portfolio_service.bootstrap_snapshot(snapshot_origin="operator_rebaseline")
            app = self._app(runtime)

            with TestClient(app) as client:
                account_state = client.get("/account/state")
                positions = client.get("/positions")
                runtime_response = client.get("/system/runtime")
                margin_buffer = client.get("/risk/margin-buffer")

            self.assertEqual(account_state.status_code, 200)
            self.assertEqual(positions.status_code, 200)
            self.assertEqual(runtime_response.status_code, 200)
            self.assertEqual(margin_buffer.status_code, 200)

            account_state_payload = account_state.json()
            positions_payload = positions.json()
            runtime_payload = runtime_response.json()
            margin_buffer_payload = margin_buffer.json()

            self.assertEqual(account_state_payload["backend"], "okx")
            self.assertEqual(account_state_payload["account_configuration"]["account_level_code"], "4")
            self.assertEqual(account_state_payload["account_configuration"]["position_mode_label"], "long_short")
            self.assertEqual(account_state_payload["fee_schedule"]["taker"], "0.0005")
            self.assertEqual(account_state_payload["risk_snapshot"]["margin_ratio"], "5.2")
            self.assertEqual(account_state_payload["system_status_items"][0]["title"], "All Systems Operational")
            self.assertEqual(account_state_payload["exchange_position_margin_summary"]["margin_allocated_total"], "320")
            self.assertEqual(account_state_payload["exchange_position_margin_summary"]["position_count_by_margin_mode"]["cross"], 1)
            self.assertEqual(
                Decimal(account_state_payload["local_position_margin_summary"]["margin_allocated_total"]),
                Decimal("320"),
            )
            self.assertEqual(account_state_payload["margin_reconciliation"]["position_margin_metric_mismatch_count"], 0)
            self.assertEqual(
                {item["symbol"] for item in account_state_payload["tracked_instrument_rules"]},
                {"BTC-USDT-SWAP", "ETH-USDT-SWAP"},
            )
            self.assertEqual(
                next(
                    item["settle_currency"]
                    for item in account_state_payload["tracked_instrument_rules"]
                    if item["symbol"] == "BTC-USDT-SWAP"
                ),
                "USDT",
            )
            self.assertEqual(Decimal(positions_payload["local_margin_summary"]["margin_allocated_total"]), Decimal("320"))
            self.assertEqual(Decimal(positions_payload["exchange_margin_summary"]["maintenance_margin_total"]), Decimal("140"))
            self.assertEqual(positions_payload["local_positions"][0]["margin_source"], "exchange")
            self.assertEqual(Decimal(positions_payload["exchange_positions"][0]["liquidation_price"]), Decimal("62000"))
            self.assertEqual(margin_buffer_payload["status"], "healthy")
            self.assertEqual(Decimal(margin_buffer_payload["current"]["initial_margin_usage_fraction"]), Decimal("0.128"))
            self.assertEqual(
                Decimal(margin_buffer_payload["liquidation"]["nearest_liquidation_gap_ratio"]).quantize(Decimal("0.000001")),
                Decimal("0.225968"),
            )
            self.assertEqual(margin_buffer_payload["liquidation"]["closest_position"]["symbol"], "BTC-USDT-SWAP")
            self.assertEqual(account_state_payload["margin_buffer_overview"]["status"], "healthy")
            self.assertEqual(runtime_payload["startup_profile"], "derivatives")
            self.assertEqual(runtime_payload["account_configuration"]["account_level_label"], "portfolio_margin")
            self.assertEqual(runtime_payload["risk_snapshot"]["initial_margin_requirement"], "320")
            self.assertEqual(runtime_payload["primary_instrument_rule"]["symbol"], "BTC-USDT-SWAP")
            self.assertEqual(runtime_payload["primary_instrument_rule"]["instrument_type"], "SWAP")
            self.assertEqual(runtime_payload["primary_instrument_rule"]["max_leverage"], "50")
            self.assertEqual(runtime_payload["margin_buffer_overview"]["status"], "healthy")
        finally:
            FakeOperatorAccountService.SNAPSHOT = None
            FakeOperatorAccountService.PRIVATE_ORDER_ROW = None
            FakeOperatorAccountService.PRIVATE_ORDER_FILLS = None

    async def test_guarded_live_preflight_and_run_packet_surface_structural_and_margin_failures(self) -> None:
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=Decimal("1000"), available=Decimal("620"), frozen=Decimal("380"))],
            positions=[
                ExchangePosition(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    quantity=Decimal("0.02"),
                    average_entry_price=Decimal("70000"),
                    mark_price=Decimal("70000"),
                    notional_usd=Decimal("1400"),
                    side="long",
                    margin_mode="cross",
                    margin_currency="USDT",
                    leverage=Decimal("8"),
                    margin_allocated=Decimal("860"),
                    maintenance_margin=Decimal("380"),
                    margin_ratio=Decimal("1.8"),
                    liquidation_price=Decimal("65000"),
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                )
            ],
            open_orders=[],
            fills=[],
            instruments=[
                InstrumentMetadata(
                    instrument_id="BTC-USDT-SWAP",
                    symbol="BTC-USDT-SWAP",
                    base_currency="BTC",
                    quote_currency="USDT",
                    lot_size=Decimal("0.01"),
                    tick_size=Decimal("0.1"),
                    min_size=Decimal("0.01"),
                    contract_value=Decimal("0.01"),
                    instrument_type="SWAP",
                    instrument_family="BTC-USDT",
                    underlying="BTC-USDT",
                    settle_currency="USDT",
                    contract_value_currency="BTC",
                    max_leverage=Decimal("50"),
                    max_market_size=Decimal("2000"),
                    max_limit_size=Decimal("2500"),
                    state="live",
                )
            ],
            account_mode="4",
            position_mode="net_mode",
            account_configuration=ExchangeAccountConfiguration(
                account_level_code="4",
                account_level_label="portfolio_margin",
                position_mode="net_mode",
                position_mode_label="net",
                auto_loan_enabled=True,
                raw={"acctLv": "4", "posMode": "net_mode"},
            ),
            risk_snapshot=ExchangeAccountRiskSnapshot(
                adjusted_equity=Decimal("1000"),
                total_equity=Decimal("1000"),
                available_equity=Decimal("620"),
                initial_margin_requirement=Decimal("860"),
                maintenance_margin_requirement=Decimal("380"),
                margin_ratio=Decimal("1.8"),
                notional_usd=Decimal("1400"),
                raw={"adjEq": "1000", "imr": "860"},
            ),
        )
        try:
            with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
                settings = AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "guarded_live",
                        "market_data_backend": "okx",
                        "execution_backend": "okx",
                        "account_backend": "okx",
                        "account_read_enabled": True,
                        "trading_product_type": "derivatives",
                        "margin_mode": "cross",
                        "default_symbol": "BTC-USDT-SWAP",
                        "allowed_symbols": ("BTC-USDT-SWAP",),
                        "storage_mode": "postgres",
                        "database_url": database_url,
                        "database_auto_create_schema": True,
                        "event_persistence_mode": "strict",
                        "live_submit_enabled": True,
                        "guarded_execution_dry_run": False,
                        "okx_simulated_trading": False,
                        "operator_auth_enabled": False,
                        "operator_unsafe_write_without_auth": True,
                    }
                )
                with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
                    runtime = await build_runtime(settings)
                runtime.portfolio_service.state.load_exchange_snapshot(FakeOperatorAccountService.SNAPSHOT)
                await runtime.portfolio_service.bootstrap_snapshot(snapshot_origin="operator_rebaseline")
                app = self._app(runtime)

                with TestClient(app) as client:
                    preflight = client.get("/system/guarded-live-preflight")
                    run_packet = client.get("/reports/guarded-live-run-packet")
                    health = client.get("/system/health")
                    runtime_response = client.get("/system/runtime")

                self.assertEqual(preflight.status_code, 200)
                self.assertEqual(run_packet.status_code, 200)
                self.assertEqual(health.status_code, 200)
                self.assertEqual(runtime_response.status_code, 200)

                preflight_payload = preflight.json()
                run_packet_payload = run_packet.json()
                health_payload = health.json()
                runtime_payload = runtime_response.json()

                self.assertEqual(preflight_payload["status"], "fail")
                self.assertFalse(preflight_payload["launch_ready"])
                self.assertTrue(any(item["check_id"] == "real_money_route_ready" and item["status"] == "fail" for item in preflight_payload["checks"]))
                self.assertTrue(any(item["check_id"] == "margin_buffer_safe" and item["status"] == "fail" for item in preflight_payload["checks"]))
                self.assertEqual(run_packet_payload["status"], "critical")
                self.assertTrue(run_packet_payload["derivatives_live_guard"]["auto_halt_required"])
                self.assertIn("derivatives_liquidation_proximity_auto_halt", run_packet_payload["derivatives_live_guard"]["auto_halt_reasons"])
                self.assertIn("derivatives_liquidation_proximity_auto_halt", [item["blocker"] for item in health_payload["blockers"]])
                self.assertEqual(health_payload["subsystems"]["derivatives_live_guard"]["status"], "critical")
                self.assertEqual(runtime_payload["guarded_live_preflight"]["status"], "fail")
                self.assertEqual(runtime_payload["guarded_live_run_packet_summary"]["status"], "critical")
                if runtime.database_runtime is not None:
                    runtime.database_runtime.dispose()
        finally:
            FakeOperatorAccountService.SNAPSHOT = None
            FakeOperatorAccountService.PRIVATE_ORDER_ROW = None
            FakeOperatorAccountService.PRIVATE_ORDER_FILLS = None

    async def test_persisted_funding_fees_are_operator_readable(self) -> None:
        runtime = await self._runtime(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="bill_fee_1",
                symbol="BTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("-2.5"),
                bill_type="8",
                sub_type="173",
                type_label="funding_fee",
                sub_type_label="funding_fee_expense",
                funding_direction="expense",
                bill_ts=utc_now(),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_fee_1",
                ledger_posted_at=utc_now(),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="bill_fee_2",
                symbol="BTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("1.25"),
                bill_type="8",
                sub_type="174",
                type_label="funding_fee",
                sub_type_label="funding_fee_income",
                funding_direction="income",
                bill_ts=utc_now(),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_fee_2",
                ledger_posted_at=utc_now(),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recent = client.get("/account/recent-funding-fees?limit=10")
            account_state = client.get("/account/state")

        self.assertEqual(recent.status_code, 200)
        recent_payload = recent.json()
        self.assertEqual(recent_payload["total_available"], 2)
        self.assertEqual(len(recent_payload["funding_fees"]), 2)
        self.assertEqual(recent_payload["summary"]["count"], 2)
        self.assertEqual(recent_payload["summary"]["expense_count"], 1)
        self.assertEqual(recent_payload["summary"]["income_count"], 1)
        self.assertEqual(recent_payload["summary"]["net_total_by_currency"]["USDT"], "-1.25")
        self.assertEqual(recent_payload["latest_funding_fee"]["bill_id"], "bill_fee_2")

        account_state_payload = account_state.json()
        self.assertEqual(account_state_payload["persisted_funding_fee_summary"]["count"], 2)
        self.assertIn("exchange_funding_fee_summary", account_state_payload)

    async def test_profitability_overview_merges_funding_fee_into_realized_view(self) -> None:
        runtime = await self._runtime(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="bill_profit_fee_1",
                symbol="BTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("-2.5"),
                bill_type="8",
                sub_type="173",
                type_label="funding_fee",
                sub_type_label="funding_fee_expense",
                funding_direction="expense",
                bill_ts=utc_now() - timedelta(minutes=5),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_profit_fee_1",
                ledger_posted_at=utc_now() - timedelta(minutes=5),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="bill_profit_fee_2",
                symbol="BTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("1.25"),
                bill_type="8",
                sub_type="174",
                type_label="funding_fee",
                sub_type_label="funding_fee_income",
                funding_direction="income",
                bill_ts=utc_now(),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_profit_fee_2",
                ledger_posted_at=utc_now(),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.get("/reports/profitability-overview?limit=20")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        summary = payload["summary"]
        self.assertEqual(summary["funding_fee_count"], 2)
        self.assertEqual(summary["funding_fee_income_count"], 1)
        self.assertEqual(summary["funding_fee_expense_count"], 1)
        self.assertEqual(Decimal(str(summary["funding_fee_net_pnl"])), Decimal("-1.25"))
        self.assertEqual(
            Decimal(str(summary["combined_net_realized_pnl"])),
            Decimal(str(summary["net_realized_pnl"])) + Decimal(str(summary["funding_fee_net_pnl"])),
        )
        self.assertIn("funding_fee_summary", payload)
        self.assertIn("recent_realized_events", payload)
        self.assertEqual(payload["funding_fee_summary"]["net_total_by_currency"]["USDT"], "-1.25")
        self.assertTrue(any(item["event_kind"] == "funding_fee" for item in payload["recent_realized_events"]))
        self.assertEqual(payload["truth_source"], "fill_outcomes_plus_funding_fee_records")

    async def test_strategy_attribution_report_groups_regime_profile_exit_and_risk_protection(self) -> None:
        runtime = await self._runtime(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        now = utc_now()

        def seed_decision(
            *,
            decision_id: str,
            regime: str,
            volatility_state: str,
            profile_id: str,
            strategy_family: str,
            strategy_sleeve_id: str,
            allocation_id: str,
            strategy_bundle_id: str,
            strategy_route_action: str,
            exit_attribution: str,
            risk_constraints: list[str],
            realized_pnl: Decimal,
        ) -> None:
            decision_context = DecisionContext(
                decision_id=decision_id,
                symbol="BTC-USDT-SWAP",
                timeframe="15m",
                as_of_ts=now,
                market_snapshot_ref="evt_market_seed",
                feature_snapshot_ref="evt_feature_seed",
                portfolio_snapshot_ref="evt_portfolio_seed",
                health_snapshot_ref="evt_health_seed",
                mode="guarded_live",
                current_position_qty=Decimal("0.02"),
                product_type="derivatives",
                current_exposure_side="long",
                current_target_leverage=3.0,
            )
            baseline = BaselineAssessment(
                decision_id=decision_id,
                symbol="BTC-USDT-SWAP",
                regime=regime,
                direction_bias="long",
                trend_strength=0.72,
                volatility_state=volatility_state,
                confidence=0.81,
                composite_alpha_score=0.26,
                suggested_position_scale=0.8,
                volatility_target_scale=0.84,
                factor_scores={"momentum_alpha": 0.18, "microstructure_alpha": 0.08},
                holding_horizon="15m",
                invalidation_conditions=[],
                reason_codes=[f"regime_{regime}"],
                engine_version="test",
            )
            decision_outcome = DecisionOutcome(
                decision_id=decision_id,
                symbol="BTC-USDT-SWAP",
                decision_source="baseline",
                decision_authority="reference_only",
                final_direction="long",
                final_action="reduce",
                final_target_qty=Decimal("0.01"),
                baseline_reference={"regime": regime, "volatility_state": volatility_state},
                active_profile_id=profile_id,
                profile_control_source="system",
                position_management_reason_codes=[exit_attribution],
                exit_attribution=exit_attribution,
                selected_strategy_family=strategy_family,
                selected_strategy_route_action=strategy_route_action,
                strategy_selection_reason_codes=[f"active_strategy_family_{strategy_family}"],
            )
            position_target = PositionTarget(
                decision_id=decision_id,
                symbol="BTC-USDT-SWAP",
                current_position_qty=Decimal("0.02"),
                target_position_qty=Decimal("0.01"),
                delta_position_qty=Decimal("-0.01"),
                current_notional=Decimal("1400"),
                target_notional=Decimal("700"),
                rebalance_reason="test",
                urgency="medium",
                max_slippage_tolerance_bps=20,
                source_mix={"baseline": 1.0},
                decision_expiry_ts=now + timedelta(minutes=15),
                product_type="derivatives",
                current_exposure_side="long",
                target_exposure_side="long",
                position_intent="reduce_long",
                target_leverage=3.0,
                margin_mode="cross",
                strategy_family=strategy_family,
                strategy_route_action=strategy_route_action,
                strategy_reason_codes=[f"active_strategy_family_{strategy_family}"],
                guardrail_flags=[exit_attribution],
                decision_outcome=decision_outcome,
            )
            risk_decision = RiskDecision(
                decision_id=decision_id,
                approved=True,
                modified=bool(risk_constraints),
                capped_target_position_qty=Decimal("0.01"),
                capped_target_notional=Decimal("700"),
                current_open_order_count=0,
                risk_budget_multiplier=Decimal("0.72") if risk_constraints else Decimal("1"),
                risk_budget_state={},
                execution_aggressiveness_multiplier=Decimal("0.65") if risk_constraints else Decimal("1"),
                execution_aggressiveness_state={},
                constraints_applied=risk_constraints,
                risk_score=0.42,
                rejection_reasons=[],
            )
            decision_event = build_envelope(
                topic=topics.DECISION_CONTEXTS,
                key="BTC-USDT-SWAP",
                payload_model=decision_context,
                source_component="test",
            )
            baseline_event = build_envelope(
                topic=topics.BASELINE_ASSESSMENTS,
                key="BTC-USDT-SWAP",
                payload_model=baseline,
                source_component="test",
            )
            target_event = build_envelope(
                topic=topics.POSITION_TARGETS,
                key="BTC-USDT-SWAP",
                payload_model=position_target,
                source_component="test",
            )
            risk_event = build_envelope(
                topic=topics.RISK_DECISIONS,
                key="BTC-USDT-SWAP",
                payload_model=risk_decision,
                source_component="test",
            )
            runtime.event_store.append(decision_event)
            runtime.event_store.append(baseline_event)
            runtime.event_store.append(target_event)
            runtime.event_store.append(risk_event)
            runtime.audit_repo.upsert(
                DecisionAuditRecord(
                    decision_id=decision_id,
                    decision_context_ref=decision_event.event_id,
                    baseline_assessment_ref=baseline_event.event_id,
                    position_target_ref=target_event.event_id,
                    risk_decision_ref=risk_event.event_id,
                )
            )
            runtime.fill_outcome_repo.save_outcome(
                FillOutcomeRecord(
                    fill_id=f"fill_{decision_id}",
                    decision_id=decision_id,
                    order_id=f"order_{decision_id}",
                    symbol="BTC-USDT-SWAP",
                    position_key="BTC-USDT-SWAP:long",
                    venue="OKX",
                    side="sell",
                    fill_qty=Decimal("0.01"),
                    fill_price=Decimal("70000"),
                    fill_notional=Decimal("700"),
                    fee_amount=Decimal("0.50"),
                    fee_currency="USDT",
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                    order_status_after_fill="FILLED",
                    strategy_family=strategy_family,
                    strategy_sleeve_id=strategy_sleeve_id,
                    allocation_id=allocation_id,
                    strategy_bundle_id=strategy_bundle_id,
                    strategy_leg_role="primary",
                    target_leverage=3.0,
                    exposure_side="long",
                    execution_action="reduce_long",
                    position_intent="reduce_long",
                    position_mode="net_mode",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    starting_position_qty=Decimal("0.02"),
                    starting_avg_entry_price=Decimal("69000"),
                    ending_position_qty=Decimal("0.01"),
                    ending_avg_entry_price=Decimal("69000"),
                    realized_pnl_delta=realized_pnl,
                    fee_delta=Decimal("-0.50"),
                    product_type="derivatives",
                    margin_mode="cross",
                    created_at=now,
                )
            )
            runtime.execution_repo.save_fill(
                FillEvent(
                    fill_id=f"fill_{decision_id}",
                    decision_id=decision_id,
                    intent_id=f"intent_{decision_id}",
                    client_order_id=f"order_{decision_id}",
                    exchange_order_id=f"venue_{decision_id}",
                    symbol="BTC-USDT-SWAP",
                    venue="OKX",
                    side="sell",
                    fill_qty=Decimal("0.01"),
                    fill_price=Decimal("70000"),
                    fee_amount=Decimal("0.50"),
                    fee_currency="USDT",
                    liquidity_role="taker",
                    exchange_timestamp=now,
                    ingestion_timestamp=now,
                    order_status_after_fill="FILLED",
                    strategy_family=strategy_family,
                    strategy_sleeve_id=strategy_sleeve_id,
                    allocation_id=allocation_id,
                    strategy_bundle_id=strategy_bundle_id,
                    strategy_leg_role="primary",
                    target_leverage=3.0,
                    exposure_side="long",
                    execution_action="reduce",
                    position_intent="reduce_long",
                    position_mode="net_mode",
                    instrument_family="BTC-USDT",
                    settle_currency="USDT",
                    product_type="derivatives",
                    margin_mode="cross",
                )
            )

        seed_decision(
            decision_id="decision_attr_trend",
            regime="trend",
            volatility_state="medium",
            profile_id="trend_normal",
            strategy_family="directional",
            strategy_sleeve_id="directional_btc_core",
            allocation_id="alloc_directional_btc",
            strategy_bundle_id="bundle_directional_btc",
            strategy_route_action="override_target",
            exit_attribution="alpha_decay_reduce",
            risk_constraints=["risk_budget_multiplier_applied", "execution_aggressiveness_contracted"],
            realized_pnl=Decimal("12"),
        )
        seed_decision(
            decision_id="decision_attr_range",
            regime="range",
            volatility_state="high",
            profile_id="execution_degraded_safe",
            strategy_family="smart_arbitrage",
            strategy_sleeve_id="smart_arbitrage_btc_pair",
            allocation_id="alloc_smart_arb_btc",
            strategy_bundle_id="bundle_smart_arb_btc",
            strategy_route_action="protective_fallback",
            exit_attribution="emergency_protective_exit",
            risk_constraints=[],
            realized_pnl=Decimal("-3"),
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            response = client.get("/reports/strategy-attribution?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["summary"]["fill_count"], 2)
        self.assertGreaterEqual(payload["summary"]["sleeve_pnl_record_count"], 2)
        self.assertGreaterEqual(payload["summary"]["protected_fill_count"], 1)
        self.assertGreaterEqual(payload["summary"]["unprotected_fill_count"], 1)
        self.assertEqual(payload["truth_source"], "sleeve_pnl_records_plus_fill_outcomes_plus_decision_audit")
        self.assertTrue(any(item["strategy_sleeve_id"] == "directional_btc_core" for item in payload["profitability_by_strategy_sleeve"]))
        self.assertTrue(any(item["allocation_id"] == "alloc_directional_btc" for item in payload["profitability_by_allocation"]))
        self.assertTrue(any(item["strategy_bundle_id"] == "bundle_directional_btc" for item in payload["profitability_by_strategy_bundle"]))
        self.assertTrue(any(item["attribution_type"] == "direct_fill" for item in payload["profitability_by_attribution_type"]))
        self.assertTrue(any(item["strategy_sleeve_id"] == "directional_btc_core" for item in payload["sleeve_inventory_summary"]))
        self.assertTrue(any(item["market_regime"] == "trend" for item in payload["profitability_by_regime"]))
        self.assertTrue(any(item["active_profile_id"] == "trend_normal" for item in payload["profitability_by_profile"]))
        self.assertTrue(any(item["strategy_family"] == "directional" for item in payload["profitability_by_strategy_family"]))
        self.assertTrue(
            any(item["strategy_family"] == "smart_arbitrage" for item in payload["profitability_by_strategy_family"])
        )
        self.assertTrue(
            any(
                item["strategy_route_action"] == "protective_fallback"
                for item in payload["profitability_by_strategy_route_action"]
            )
        )
        self.assertTrue(
            any(item["exit_attribution"] == "emergency_protective_exit" for item in payload["profitability_by_exit_attribution"])
        )
        self.assertTrue(
            any(
                item["code"] == "execution_aggressiveness_contracted"
                for item in payload["risk_protection_summary"]["top_constraint_codes"]
            )
        )

    async def test_position_lifecycle_profitability_tracks_closed_lifecycle_and_unassigned_funding(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP", "LTC-USDT-SWAP"),
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        runtime = await build_runtime(settings)
        now = utc_now()
        runtime.fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="ltc_lifecycle_open",
                order_id="ltc_order_open",
                symbol="LTC-USDT-SWAP",
                position_key="LTC-USDT-SWAP:long",
                venue="PAPER",
                side="buy",
                fill_qty=Decimal("2"),
                fill_price=Decimal("80"),
                fill_notional=Decimal("160"),
                fee_amount=Decimal("0.20"),
                fee_currency="USDT",
                liquidity_role="maker",
                exchange_timestamp=now - timedelta(minutes=20),
                ingestion_timestamp=now - timedelta(minutes=20),
                order_status_after_fill="FILLED",
                target_leverage=3.0,
                exposure_side="long",
                execution_action="open_long",
                position_intent="open_long",
                position_mode="long_short_mode",
                pos_side="long",
                instrument_family="LTC-USDT",
                settle_currency="USDT",
                starting_position_qty=Decimal("0"),
                starting_avg_entry_price=Decimal("0"),
                ending_position_qty=Decimal("2"),
                ending_avg_entry_price=Decimal("80"),
                realized_pnl_delta=Decimal("0"),
                fee_delta=Decimal("-0.20"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now - timedelta(minutes=20),
            )
        )
        runtime.fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="ltc_lifecycle_close",
                order_id="ltc_order_close",
                symbol="LTC-USDT-SWAP",
                position_key="LTC-USDT-SWAP:long",
                venue="PAPER",
                side="sell",
                fill_qty=Decimal("2"),
                fill_price=Decimal("82.5"),
                fill_notional=Decimal("165"),
                fee_amount=Decimal("0.25"),
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=10),
                ingestion_timestamp=now - timedelta(minutes=10),
                order_status_after_fill="FILLED",
                target_leverage=3.0,
                exposure_side="flat",
                execution_action="close_long",
                position_intent="close_long",
                position_mode="long_short_mode",
                pos_side="long",
                instrument_family="LTC-USDT",
                settle_currency="USDT",
                starting_position_qty=Decimal("2"),
                starting_avg_entry_price=Decimal("80"),
                ending_position_qty=Decimal("0"),
                ending_avg_entry_price=Decimal("0"),
                realized_pnl_delta=Decimal("5"),
                fee_delta=Decimal("-0.25"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now - timedelta(minutes=10),
            )
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="ltc_funding_assigned",
                symbol="LTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("-0.30"),
                bill_type="8",
                sub_type="173",
                type_label="funding_fee",
                sub_type_label="funding_fee_expense",
                funding_direction="expense",
                bill_ts=now - timedelta(minutes=15),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_ltc_funding_1",
                ledger_posted_at=now - timedelta(minutes=15),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="ltc_funding_unassigned",
                symbol="LTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("-0.20"),
                bill_type="8",
                sub_type="173",
                type_label="funding_fee",
                sub_type_label="funding_fee_expense",
                funding_direction="expense",
                bill_ts=now - timedelta(minutes=2),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_ltc_funding_2",
                ledger_posted_at=now - timedelta(minutes=2),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            lifecycle_response = client.get("/reports/position-lifecycle-profitability?limit=20")
            trial_review_details = client.get(
                "/reports/trial-review-details?profitability_limit=20&anomaly_limit=20&segment_limit=20&window_days=7&period_count=2"
            )

        self.assertEqual(lifecycle_response.status_code, 200)
        lifecycle_payload = lifecycle_response.json()
        target = next(
            item
            for item in lifecycle_payload["lifecycles"]
            if item["symbol"] == "LTC-USDT-SWAP" and item["position_key"] == "LTC-USDT-SWAP:long"
        )
        self.assertEqual(target["status"], "closed")
        self.assertEqual(Decimal(str(target["trading_net_realized_pnl"])), Decimal("5"))
        self.assertEqual(Decimal(str(target["fee_total"])), Decimal("0.45"))
        self.assertEqual(Decimal(str(target["funding_fee_total"])), Decimal("-0.30"))
        self.assertEqual(Decimal(str(target["combined_net_realized_pnl"])), Decimal("4.70"))
        self.assertEqual(target["funding_fee_attribution_scope"], "symbol_window")
        self.assertEqual(lifecycle_payload["summary"]["unassigned_funding_fee_count"], 1)
        self.assertEqual(lifecycle_payload["unassigned_funding_fees"][0]["bill_id"], "ltc_funding_unassigned")
        self.assertEqual(lifecycle_payload["unassigned_funding_fees"][0]["attribution_reason"], "no_matching_position_window")
        self.assertEqual(trial_review_details.status_code, 200)
        self.assertIn("position_lifecycle_profitability", trial_review_details.json()["sections"])

    async def test_trial_guard_and_forward_validation_include_funding_fee_drag(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "forward_test_small_capital",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "trading_product_type": "derivatives",
                "margin_mode": "cross",
                "default_symbol": "BTC-USDT-SWAP",
                "allowed_symbols": ("BTC-USDT-SWAP",),
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
                "trial_guard_enabled": True,
                "trial_guard_min_closed_fills": 1,
                "trial_guard_lookback_fills": 20,
                "trial_guard_max_daily_loss_usdt": 20.0,
            }
        )
        runtime = await build_runtime(settings)
        now = utc_now()
        runtime.fill_outcome_repo.save_outcome(
            FillOutcomeRecord(
                fill_id="btc_trial_guard_fill",
                order_id="btc_trial_guard_order",
                symbol="BTC-USDT-SWAP",
                position_key="BTC-USDT-SWAP",
                venue="PAPER",
                side="sell",
                fill_qty=Decimal("1"),
                fill_price=Decimal("101"),
                fill_notional=Decimal("101"),
                fee_amount=Decimal("0.10"),
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=now - timedelta(minutes=30),
                ingestion_timestamp=now - timedelta(minutes=30),
                order_status_after_fill="FILLED",
                target_leverage=2.0,
                exposure_side="flat",
                execution_action="close_long",
                position_intent="close_long",
                position_mode="net_mode",
                pos_side="net",
                instrument_family="BTC-USDT",
                settle_currency="USDT",
                starting_position_qty=Decimal("1"),
                starting_avg_entry_price=Decimal("95"),
                ending_position_qty=Decimal("0"),
                ending_avg_entry_price=Decimal("0"),
                realized_pnl_delta=Decimal("8"),
                fee_delta=Decimal("-0.10"),
                product_type="derivatives",
                margin_mode="cross",
                created_at=now - timedelta(minutes=30),
            )
        )
        runtime.funding_fee_repo.save_record(
            FundingFeeRecord(
                bill_id="btc_trial_guard_funding",
                symbol="BTC-USDT-SWAP",
                currency="USDT",
                amount=Decimal("-40"),
                bill_type="8",
                sub_type="173",
                type_label="funding_fee",
                sub_type_label="funding_fee_expense",
                funding_direction="expense",
                bill_ts=now - timedelta(minutes=5),
                ledger_posting_state="POSTED",
                ledger_journal_id="jrnl_btc_trial_guard_funding",
                ledger_posted_at=now - timedelta(minutes=5),
                product_type="derivatives",
                margin_mode="cross",
            )
        )
        runtime.trial_guard_service.evaluate_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            trial_guard = client.get("/system/trial-guard")
            forward_validation = client.get("/reports/forward-validation?window_days=1&period_count=2")
            trial_review_summary = client.get(
                "/reports/trial-review-summary?segment_limit=20&window_days=1&period_count=2"
            )

        self.assertEqual(trial_guard.status_code, 200)
        trial_guard_payload = trial_guard.json()
        self.assertEqual(trial_guard_payload["status"], "breached")
        self.assertEqual(Decimal(str(trial_guard_payload["daily_trading_net_realized"])), Decimal("8"))
        self.assertEqual(Decimal(str(trial_guard_payload["daily_funding_fee_net"])), Decimal("-40"))
        self.assertEqual(Decimal(str(trial_guard_payload["daily_combined_net_realized"])), Decimal("-32"))
        self.assertTrue(any(item["code"] == "trial_guard_daily_loss_limit" for item in trial_guard_payload["breaches"]))

        self.assertEqual(forward_validation.status_code, 200)
        latest_period = forward_validation.json()["periods"][0]
        self.assertEqual(Decimal(str(latest_period["net_realized_pnl"])), Decimal("8"))
        self.assertEqual(Decimal(str(latest_period["funding_fee_net_pnl"])), Decimal("-40"))
        self.assertEqual(Decimal(str(latest_period["combined_net_realized_pnl"])), Decimal("-32"))
        self.assertEqual(forward_validation.json()["summary"]["verdict"], "pause")

        self.assertEqual(trial_review_summary.status_code, 200)
        summary_payload = trial_review_summary.json()["summary"]
        self.assertEqual(Decimal(str(summary_payload["funding_fee_net_pnl"])), Decimal("-40"))
        self.assertEqual(Decimal(str(summary_payload["combined_net_realized_pnl"])), Decimal("-32"))

    async def test_operator_visibility_endpoints_cover_decision_execution_reconciliation_and_audit(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            latest_decision = client.get("/decision/latest").json()
            decision_id = latest_decision["decision_id"]
            recent_decisions = client.get("/decision/recent").json()
            decision_detail = client.get(f"/decision/{decision_id}").json()
            risk_latest = client.get("/risk/latest").json()
            risk_recent = client.get("/risk/recent?limit=2").json()
            margin_buffer = client.get("/risk/margin-buffer").json()
            policy_latest = client.get("/policy/latest").json()
            policy_recent = client.get("/policy/recent?limit=2").json()
            portfolio_latest = client.get("/portfolio/latest").json()
            portfolio_history = client.get("/portfolio/history?limit=5").json()
            balances = client.get("/balances").json()
            positions = client.get("/positions").json()
            orders_recent = client.get("/orders/recent").json()
            latest_order_id = orders_recent["orders"][0]["client_order_id"]
            order_detail = client.get(f"/orders/{latest_order_id}").json()
            fills_recent = client.get("/fills/recent").json()
            latest_fill_id = fills_recent["fills"][0]["fill_id"]
            fill_detail = client.get(f"/fills/{latest_fill_id}").json()
            execution_latest = client.get("/execution/latest").json()
            execution_quality = client.get("/reports/execution-quality?limit=5").json()
            profitability_overview = client.get("/reports/profitability-overview?limit=5").json()
            lifecycle_profitability = client.get("/reports/position-lifecycle-profitability?limit=5").json()
            strategy_segments = client.get("/reports/strategy-segments?limit=10").json()
            execution_anomalies = client.get("/reports/execution-anomalies?limit=10").json()
            forward_validation = client.get("/reports/forward-validation?window_days=7&period_count=4").json()
            scaling_readiness = client.get("/reports/scaling-readiness?window_days=7&period_count=4").json()
            trial_review_summary = client.get(
                "/reports/trial-review-summary?segment_limit=100&window_days=7&period_count=4"
            ).json()
            trial_review_details = client.get(
                "/reports/trial-review-details?profitability_limit=100&anomaly_limit=100&segment_limit=100&window_days=7&period_count=4"
            ).json()
            trial_review_packet = client.get(
                "/reports/trial-review-packet?profitability_limit=100&anomaly_limit=100&segment_limit=100&window_days=7&period_count=4"
            ).json()
            trial_review_history = client.get("/reports/trial-review-history?limit=5").json()
            reconciliation_latest = client.get("/reconciliation/latest").json()
            reconciliation_recent = client.get("/reconciliation/recent").json()
            reconciliation_mismatches = client.get("/reconciliation/mismatches").json()
            latest_reconciliation_id = reconciliation_latest["reconciliation"]["reconciliation_id"]
            reconciliation_detail = client.get(f"/reconciliation/{latest_reconciliation_id}").json()
            audit_latest = client.get("/audit/latest").json()
            audit_detail = client.get(f"/audit/{decision_id}").json()
            replay_status_before = client.get("/replay/status").json()
            replay_validation = client.post(f"/replay/validate/{decision_id}").json()
            replay_status_after = client.get("/replay/status").json()
            replay_recent = client.get("/replay/recent-validations?limit=5").json()

        self.assertIsNotNone(decision_id)
        self.assertIn("strategy_execution_health", latest_decision)
        self.assertIn("strategy_execution_health", decision_detail)
        self.assertEqual(decision_detail["decision_id"], decision_id)
        self.assertTrue(recent_decisions["decisions"])
        self.assertIn("total_available", recent_decisions)
        self.assertIn("has_more", recent_decisions)
        self.assertEqual(risk_latest["decision_id"], decision_id)
        self.assertIn("total_available", risk_recent)
        self.assertIn("has_more", risk_recent)
        self.assertIn("status", margin_buffer)
        self.assertIn("current", margin_buffer)
        self.assertIn("liquidation", margin_buffer)
        self.assertEqual(policy_latest["decision_id"], decision_id)
        self.assertIn("total_available", policy_recent)
        self.assertIn("has_more", policy_recent)
        self.assertIsNotNone(portfolio_latest["portfolio"])
        self.assertTrue(portfolio_history["snapshots"])
        self.assertIn("local_balances", balances)
        self.assertIn("local_positions", positions)
        self.assertIn("local_net_positions", positions)
        self.assertIn("local_margin_summary", positions)
        self.assertIn("exchange_net_positions", positions)
        self.assertIn("exchange_margin_summary", positions)
        self.assertIn("margin_reconciliation", positions)
        self.assertTrue(orders_recent["orders"])
        self.assertIn("total_available", orders_recent)
        self.assertIn("has_more", orders_recent)
        self.assertEqual(order_detail["order"]["client_order_id"], latest_order_id)
        self.assertIn("execution_action", order_detail["order"])
        self.assertTrue(fills_recent["fills"])
        self.assertIn("total_available", fills_recent)
        self.assertIn("has_more", fills_recent)
        self.assertEqual(fill_detail["fill"]["fill_id"], latest_fill_id)
        self.assertIn("execution_action", fill_detail["fill"])
        self.assertTrue(fill_detail["fill"].get("has_fill_outcome"))
        self.assertIn("realized_pnl", fill_detail["fill"])
        self.assertIsNotNone(fill_detail.get("fill_outcome"))
        self.assertIn("fill_qty", fill_detail["fill_outcome"])
        self.assertIn("fill_price", fill_detail["fill_outcome"])
        self.assertIn("starting_position_qty", fill_detail["fill_outcome"])
        self.assertIn("ending_position_qty", fill_detail["fill_outcome"])
        self.assertIsNotNone(execution_latest["latest_order"])
        self.assertIn("execution_action", execution_latest["latest_order"])
        self.assertIn("rows", execution_quality)
        self.assertIn("summary", execution_quality)
        self.assertTrue(execution_quality["rows"])
        self.assertIn("signal_timestamp", execution_quality["rows"][0])
        self.assertIn("decision_to_submit_latency_ms", execution_quality["rows"][0])
        self.assertIn("adverse_slippage_bps", execution_quality["rows"][0])
        self.assertIn("fee_ratio", execution_quality["rows"][0])
        self.assertIn("closed_fill_count", profitability_overview["summary"])
        self.assertIn("gross_realized_pnl", profitability_overview["summary"])
        self.assertIn("net_realized_pnl", profitability_overview["summary"])
        self.assertIn("funding_fee_count", profitability_overview["summary"])
        self.assertIn("combined_net_realized_pnl", profitability_overview["summary"])
        self.assertIn("execution_quality", profitability_overview)
        self.assertIn("funding_fee_summary", profitability_overview)
        self.assertTrue(profitability_overview["recent_closed_fills"])
        self.assertIn("recent_realized_events", profitability_overview)
        self.assertIn("lifecycles", lifecycle_profitability)
        self.assertIn("summary", lifecycle_profitability)
        self.assertIn("group_by", strategy_segments)
        self.assertIn("segments", strategy_segments)
        self.assertTrue(strategy_segments["segments"])
        self.assertIn("segment", strategy_segments["segments"][0])
        self.assertIn("fill_count", strategy_segments["segments"][0])
        self.assertIn("net_realized_pnl", strategy_segments["segments"][0])
        self.assertIn("avg_adverse_slippage_bps", strategy_segments["segments"][0])
        self.assertIn("summary", execution_anomalies)
        self.assertIn("rows", execution_anomalies)
        self.assertIn("evaluated_fill_count", execution_anomalies)
        self.assertIn("high_slippage_count", execution_anomalies["summary"])
        self.assertIn("slow_submit_to_fill_count", execution_anomalies["summary"])
        self.assertIn("summary", forward_validation)
        self.assertIn("periods", forward_validation)
        self.assertEqual(forward_validation["window_days"], 7)
        self.assertEqual(forward_validation["period_count"], 4)
        self.assertIn("verdict", forward_validation["summary"])
        self.assertIn("readiness", scaling_readiness)
        self.assertIn("summary", scaling_readiness)
        self.assertIn("requirements", scaling_readiness)
        self.assertIn("latest_review", scaling_readiness)
        self.assertEqual(scaling_readiness["window_days"], 7)
        self.assertEqual(scaling_readiness["period_count"], 4)
        self.assertIn("summary", trial_review_summary)
        self.assertIn("recommendation", trial_review_summary)
        self.assertIn("sections", trial_review_summary)
        self.assertIn("forward_validation", trial_review_summary["sections"])
        self.assertIn("scaling_readiness", trial_review_summary["sections"])
        self.assertIn("strategy_segments", trial_review_summary["sections"])
        self.assertEqual(trial_review_summary["truth_source"], "aggregated_operator_reports_summary")
        self.assertIn("sections", trial_review_details)
        self.assertIn("profitability", trial_review_details["sections"])
        self.assertIn("position_lifecycle_profitability", trial_review_details["sections"])
        self.assertIn("execution_anomalies", trial_review_details["sections"])
        self.assertIn("trial_guard", trial_review_details["sections"])
        self.assertIn("margin_buffer_overview", trial_review_details["sections"])
        self.assertIn("recovery", trial_review_details["sections"])
        self.assertEqual(trial_review_details["truth_source"], "aggregated_operator_reports_details")
        self.assertIn("summary", trial_review_packet)
        self.assertIn("recommendation", trial_review_packet)
        self.assertIn("sections", trial_review_packet)
        self.assertIn("action_items", trial_review_packet["recommendation"])
        self.assertIn("scaling_readiness", trial_review_packet["sections"])
        self.assertEqual(
            trial_review_packet["summary"]["readiness"],
            trial_review_summary["summary"]["readiness"],
        )
        self.assertEqual(
            trial_review_packet["recommendation"]["readiness"],
            trial_review_summary["recommendation"]["readiness"],
        )
        self.assertEqual(
            trial_review_packet["sections"]["profitability"],
            trial_review_details["sections"]["profitability"],
        )
        self.assertEqual(
            trial_review_packet["sections"]["execution_anomalies"],
            trial_review_details["sections"]["execution_anomalies"],
        )
        self.assertIn("actions", trial_review_history)
        self.assertIsNotNone(reconciliation_latest["reconciliation"])
        self.assertIn("mismatch_categories", reconciliation_latest["mismatch_summary"])
        self.assertIn("exchange_bills_summary", reconciliation_latest)
        self.assertTrue(reconciliation_recent["reconciliations"])
        self.assertIn("total_available", reconciliation_recent)
        self.assertIn("has_more", reconciliation_recent)
        self.assertIn("exchange_bills_summary", reconciliation_recent)
        self.assertIsInstance(reconciliation_mismatches["mismatches"], list)
        self.assertEqual(
            reconciliation_detail["reconciliation"]["reconciliation_id"],
            latest_reconciliation_id,
        )
        self.assertIn("exchange_bills_summary", reconciliation_detail)
        self.assertIn("exchange_bills_explanations", reconciliation_detail)
        self.assertIsNotNone(audit_latest["audit"])
        self.assertEqual(audit_detail["audit"]["decision_id"], decision_id)
        self.assertIsNone(replay_status_before["last_validation"])
        self.assertEqual(replay_validation["decision_id"], decision_id)
        self.assertEqual(replay_validation["symbol"], runtime.settings.default_symbol)
        self.assertIsNotNone(replay_validation["regime"])
        self.assertIsNotNone(replay_validation["active_profile_id"])
        self.assertIn("chain_health_score", replay_validation)
        self.assertIn("execution_chain_issue_count", replay_validation)
        self.assertEqual(replay_validation["product_type"], runtime.settings.trading_product_type)
        self.assertTrue(replay_status_after["recent_validations"])
        self.assertTrue(replay_recent["validations"])
        self.assertIn("total_available", replay_recent)
        self.assertIn("has_more", replay_recent)

    async def test_operator_can_record_capital_scaling_review(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            response = client.post(
                "/system/scaling-review",
                json={
                    "verdict": "continue_small_capital",
                    "reason": "test_scaling_review",
                },
            )
            report = client.get("/reports/scaling-readiness?window_days=7&period_count=4")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        report_payload = report.json()
        self.assertEqual(payload["action"], "capital_scale_review")
        self.assertEqual(payload["status"], "review_recorded")
        self.assertEqual(payload["details"]["selected_verdict"], "continue_small_capital")
        self.assertIsNotNone(report_payload["latest_review"])
        self.assertEqual(report_payload["latest_review"]["action"], "capital_scale_review")

    async def test_operator_can_record_trial_review_snapshot(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            response = client.post(
                "/system/trial-review/record",
                json={"reason": "test_trial_review_snapshot"},
            )
            packet = client.get(
                "/reports/trial-review-packet?profitability_limit=100&anomaly_limit=100&segment_limit=100&window_days=7&period_count=4"
            ).json()
            history = client.get("/reports/trial-review-history?limit=5").json()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["action"], "trial_review_snapshot")
        self.assertEqual(payload["status"], "snapshot_recorded")
        self.assertIsNotNone(packet["latest_review"])
        self.assertEqual(packet["latest_review"]["action"], "trial_review_snapshot")
        self.assertTrue(history["actions"])

    async def test_metrics_order_intent_count_uses_persisted_events_not_runtime_counter(self) -> None:
        runtime = await self._runtime(
            trading_product_type="derivatives",
            margin_mode="cross",
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.ORDER_INTENTS,
                key="BTC-USDT-SWAP",
                payload_model=OrderIntent(
                    intent_id="intent_test_metrics",
                    decision_id="decision_test_metrics",
                    symbol="BTC-USDT-SWAP",
                    side="buy",
                    quantity=Decimal("0.01"),
                    execution_style="taker",
                    order_type="market",
                    urgency="medium",
                    time_in_force="IOC",
                    reduce_only=False,
                    close_only=False,
                    idempotency_key="idem_test_metrics",
                    product_type="derivatives",
                    target_leverage=3.0,
                    margin_mode="cross",
                    exposure_side="long",
                    execution_action="enter",
                    position_intent="open_long",
                ),
                source_component="test",
            )
        )
        runtime.metrics = type(runtime.metrics)()
        app = self._app(runtime)

        with TestClient(app) as client:
            metrics = client.get("/system/metrics")

        self.assertEqual(metrics.status_code, 200)
        self.assertGreaterEqual(metrics.json()["order_intent_count"], 1)

    async def test_ai_endpoints_expose_latest_assessment_and_shadow_decisions(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_shadow_mode_enabled=True,
            ai_execution_suggestion_mode="shadow_translation",
            ai_provider="openai",
            openai_api_key="test-key",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._consecutive_failures = 0
        runtime.ai_service._consecutive_successes = 0
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            overview = client.get("/ai/overview")
            performance_overview_before = client.get("/ai/performance/overview")
            latest = client.get("/ai/latest")
            recent = client.get("/ai/recent?limit=5")
            removed_takeovers_route = client.get("/ai/takeovers/recent?limit=5")
            shadow_latest = client.get("/ai/shadow/latest")
            shadow_recent = client.get("/ai/shadow/recent?limit=5")
            runtime_status = client.get("/ai/runtime")
            removed_shadow_evaluate_route = client.post("/ai/shadow/evaluate-now")
            evaluations = client.get("/ai/shadow/evaluations?limit=5")
            performance_reports = client.get("/ai/performance/reports?limit=5")
            performance_overview = client.get("/ai/performance/overview")
            decision_id = client.get("/decision/latest").json()["decision_id"]
            decision_detail = client.get(f"/decision/{decision_id}")
            replay_validation = client.post(f"/replay/validate/{decision_id}")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(performance_overview_before.status_code, 200)
        self.assertEqual(performance_overview.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(removed_takeovers_route.status_code, 404)
        self.assertEqual(shadow_latest.status_code, 200)
        self.assertEqual(shadow_recent.status_code, 200)
        self.assertEqual(runtime_status.status_code, 200)
        self.assertEqual(removed_shadow_evaluate_route.status_code, 404)
        self.assertEqual(evaluations.status_code, 200)
        self.assertEqual(performance_reports.status_code, 200)
        self.assertEqual(decision_detail.status_code, 200)
        self.assertEqual(replay_validation.status_code, 200)

        self.assertTrue(overview.json()["runtime"]["provider_ready"])
        self.assertEqual(overview.json()["runtime"]["execution_suggestion_mode"], "shadow_translation")
        self.assertIn("shadow_summary", overview.json())
        self.assertIn("performance_windows", overview.json())
        self.assertIn("downgrade_state", overview.json())
        self.assertIn("performance_view", overview.json())
        self.assertIn("latest_execution_suggestion", overview.json())
        self.assertIn("recent_reports", performance_overview.json())
        self.assertIn("replay_context", performance_overview.json())
        self.assertIsNotNone(performance_overview_before.json()["latest_report"])
        self.assertIsNotNone(latest.json()["assessment"])
        self.assertIsNotNone(latest.json()["baseline_reference"])
        self.assertIsNotNone(latest.json()["ai_decision_intent"])
        self.assertIsNotNone(latest.json()["decision_outcome"])
        self.assertIsNotNone(latest.json()["execution_suggestion"])
        self.assertNotIn("legacy_takeover", latest.json())
        self.assertIsNotNone(shadow_latest.json()["shadow_decision"])
        self.assertTrue(evaluations.json()["evaluations"])
        self.assertTrue(performance_reports.json()["reports"])
        self.assertIsNotNone(decision_detail.json()["ai_decision_brief"])
        self.assertIsNotNone(decision_detail.json()["ai_assessment"])
        self.assertIsNotNone(decision_detail.json()["baseline_reference"])
        self.assertIsNotNone(decision_detail.json()["ai_decision_intent"])
        self.assertIsNotNone(decision_detail.json()["decision_outcome"])
        self.assertNotIn("legacy_ai_takeover_decision", decision_detail.json())
        self.assertEqual(decision_detail.json()["decision_outcome"]["decision_source"], "ai")
        self.assertIsNotNone(decision_detail.json()["ai_decision_audit"])
        self.assertIsNotNone(decision_detail.json()["ai_economic_actionability"])
        self.assertIsNotNone(decision_detail.json()["ai_execution_suggestion"])
        self.assertIsNotNone(overview.json()["latest_baseline_reference"])
        self.assertIsNotNone(overview.json()["latest_ai_decision_intent"])
        self.assertIsNotNone(overview.json()["latest_decision_outcome"])
        self.assertTrue(decision_detail.json()["ai_shadow_decisions"])
        self.assertTrue(decision_detail.json()["ai_shadow_evaluations"])
        self.assertIn(
            shadow_latest.json()["shadow_decision"]["shadow_action_type"],
            {"same_as_baseline", "hold_instead", "entry_override", "exit_override", "reverse_override"},
        )
        self.assertEqual(
            decision_detail.json()["ai_execution_suggestion"]["latest_translation"]["status"],
            "shadow_translation",
        )
        self.assertIn(
            "required_total_edge_bps",
            decision_detail.json()["ai_economic_actionability"],
        )
        self.assertIn(
            "market_snapshot_fresh",
            decision_detail.json()["ai_decision_audit"],
        )
        self.assertFalse(
            decision_detail.json()["ai_execution_suggestion"]["latest_translation"]["applied_to_live_execution"]
        )
        self.assertTrue(recent.json()["assessments"])
        self.assertTrue(shadow_recent.json()["shadow_decisions"])
        self.assertTrue(evaluations.json()["evaluations"])
        self.assertTrue(performance_reports.json()["reports"])
        self.assertEqual(
            performance_reports.json()["reports"][0]["effective_operating_mode"],
            overview.json()["runtime"]["effective_operating_mode"],
        )
        self.assertEqual(
            performance_overview.json()["latest_report"]["report_id"],
            performance_reports.json()["reports"][0]["report_id"],
        )
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_SHADOW_EVALUATIONS)), 1)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_PERFORMANCE_REPORTS)), 1)
        self.assertFalse(
            any("ai_" in issue for issue in replay_validation.json()["decision_chain_issues"]),
            replay_validation.json()["decision_chain_issues"],
        )
        self.assertFalse(
            any("ai_" in issue for issue in replay_validation.json()["audit_issues"]),
            replay_validation.json()["audit_issues"],
        )

    async def test_ai_profile_control_decision_is_exposed_in_mainline_and_operator_views(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_decision_maker_with_profile_control",
            strategy_profile_auto_control_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._consecutive_failures = 0
        runtime.ai_service._consecutive_successes = 0
        self.assertIsNotNone(runtime.decision_engine.strategy_profile_service)
        runtime.decision_engine.strategy_profile_service.evaluate_mainline_profile_control = AsyncMock(
            return_value=ProfileControlDecision(
                decision_id="decision_profile_control",
                requested_by="ai",
                requested_profile_id="trend_strict",
                current_profile_id="trend_normal",
                applied=True,
                blocked_reasons=[],
                decision_reason_codes=["ai_profile_adjustment_accepted"],
            )
        )

        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            overview = client.get("/ai/overview")
            latest = client.get("/ai/latest")
            decision_id = client.get("/decision/latest").json()["decision_id"]
            decision_detail = client.get(f"/decision/{decision_id}")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(decision_detail.status_code, 200)
        self.assertIsNotNone(overview.json()["latest_profile_control_decision"])
        self.assertEqual(
            overview.json()["latest_profile_control_decision"]["requested_profile_id"],
            "trend_strict",
        )
        self.assertIsNotNone(latest.json()["profile_control_decision"])
        self.assertEqual(latest.json()["profile_control_decision"]["requested_by"], "ai")
        self.assertEqual(
            decision_detail.json()["decision_outcome"]["decision_authority"],
            "final_decision_with_profile_control",
        )
        self.assertEqual(
            decision_detail.json()["decision_outcome"]["profile_control_source"],
            "ai",
        )
        self.assertEqual(
            decision_detail.json()["decision_outcome"]["active_profile_id"],
            "trend_strict",
        )
        self.assertTrue(decision_detail.json()["decision_outcome"]["finalized"])

    async def test_shadow_evaluation_failure_does_not_block_position_target_publication(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_decision_maker",
            ai_shadow_mode_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service.evaluate_shadow_window = Mock(side_effect=RuntimeError("shadow_eval_failed"))
        position_targets_before = len(runtime.event_store.by_topic(topics.POSITION_TARGETS))
        shadow_evaluations_before = len(runtime.event_store.by_topic(topics.AI_SHADOW_EVALUATIONS))
        performance_reports_before = len(runtime.event_store.by_topic(topics.AI_PERFORMANCE_REPORTS))

        target = await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)

        self.assertIsNotNone(target)
        self.assertEqual(len(runtime.event_store.by_topic(topics.POSITION_TARGETS)), position_targets_before + 1)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_SHADOW_EVALUATIONS)), shadow_evaluations_before)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_PERFORMANCE_REPORTS)), performance_reports_before)

    async def test_shadow_evaluation_publish_failure_does_not_block_position_target_publication(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_decision_maker",
            ai_shadow_mode_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        from aats.services.decision_engine import orchestrator as orchestrator_module

        original_publish_model = orchestrator_module.publish_model
        position_targets_before = len(runtime.event_store.by_topic(topics.POSITION_TARGETS))
        shadow_evaluations_before = len(runtime.event_store.by_topic(topics.AI_SHADOW_EVALUATIONS))
        performance_reports_before = len(runtime.event_store.by_topic(topics.AI_PERFORMANCE_REPORTS))

        async def failing_publish_model(*args, **kwargs):
            if kwargs.get("topic") == topics.AI_SHADOW_EVALUATIONS:
                raise RuntimeError("shadow_publish_failed")
            return await original_publish_model(*args, **kwargs)

        with patch("aats.services.decision_engine.orchestrator.publish_model", new=failing_publish_model):
            target = await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)

        self.assertIsNotNone(target)
        self.assertEqual(len(runtime.event_store.by_topic(topics.POSITION_TARGETS)), position_targets_before + 1)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_SHADOW_EVALUATIONS)), shadow_evaluations_before)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_PERFORMANCE_REPORTS)), performance_reports_before)

    async def test_profile_control_is_not_evaluated_when_ai_degrades_to_baseline_during_cycle(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_decision_maker_with_profile_control",
            ai_provider="openai",
            openai_api_key="test-key",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        self.assertIsNotNone(runtime.decision_engine.strategy_profile_service)
        runtime.ai_service.should_attempt_assessment = Mock(return_value=True)
        runtime.ai_service.effective_operating_mode = Mock(
            side_effect=["ai_decision_maker_with_profile_control", "baseline_only"]
        )
        runtime.ai_service.canonical_effective_operating_mode = Mock(return_value="baseline_only")
        runtime.ai_service.assess = AsyncMock(
            return_value=AIMarketAssessment(
                decision_id="decision_profile_degraded",
                symbol="BTC-USDT-SWAP",
                regime="trend",
                directional_edge=0.4,
                expected_volatility=0.1,
                confidence=0.8,
                uncertainty=0.2,
                expected_holding_horizon="15m",
                invalidation_conditions=[],
                risk_tags=[],
                rationale_summary="degraded_after_assess",
                operating_mode="ai_decision_maker_with_profile_control",
                provider_name="fake",
                output_valid=True,
                fallback_used=False,
                degraded=True,
                calibrated_confidence=0.82,
                baseline_override_recommended=True,
                override_reason_codes=["ai_trend_override"],
                economically_actionable=True,
                estimated_edge_bps=30.0,
                estimated_cost_bps=6.0,
                estimated_net_edge_bps=24.0,
                source_mode="provider",
                execution_condition="normal",
                model_name="fake",
                model_version="1",
                prompt_version="1",
            )
        )
        runtime.ai_service.latest_brief = Mock(return_value=None)
        runtime.ai_service.latest_shadow_assessment = Mock(return_value=None)
        runtime.decision_engine.strategy_profile_service.evaluate_mainline_profile_control = AsyncMock(
            return_value=ProfileControlDecision(
                decision_id="decision_profile_degraded",
                requested_by="ai",
                requested_profile_id="trend_strict",
                current_profile_id="trend_normal",
                applied=True,
            )
        )

        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)

        runtime.decision_engine.strategy_profile_service.evaluate_mainline_profile_control.assert_not_awaited()

    async def test_profile_control_can_run_independently_of_ai_mode_when_explicitly_enabled(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="baseline_only",
            strategy_profile_auto_control_enabled=True,
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        self.assertIsNotNone(runtime.decision_engine.strategy_profile_service)
        runtime.ai_service.should_attempt_assessment = Mock(return_value=False)
        runtime.ai_service.effective_operating_mode = Mock(return_value="baseline_only")
        runtime.ai_service.canonical_effective_operating_mode = Mock(return_value="baseline_only")
        runtime.ai_service.latest_brief = Mock(return_value=None)
        runtime.ai_service.latest_shadow_assessment = Mock(return_value=None)
        runtime.decision_engine.strategy_profile_service.evaluate_mainline_profile_control = AsyncMock(
            return_value=ProfileControlDecision(
                decision_id="decision_profile_independent",
                requested_by="ai",
                requested_profile_id="trend_strict",
                current_profile_id="trend_normal",
                applied=False,
            )
        )

        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)

        runtime.decision_engine.strategy_profile_service.evaluate_mainline_profile_control.assert_awaited_once()

    async def test_finalized_decision_outcome_collapses_to_hold_when_policy_blocks(self) -> None:
        runtime = await self._runtime()
        original_policy_evaluate = runtime.policy_engine.evaluate

        def blocked_policy(*, target):
            base = original_policy_evaluate(target=target)
            return PolicyDecision(
                decision_id=target.decision_id,
                mode=base.mode,
                allowed=False,
                execution_allowed=False,
                submission_allowed=False,
                dry_run_only=base.dry_run_only,
                requires_human_approval=base.requires_human_approval,
                allowed_symbols=base.allowed_symbols,
                allowed_execution_styles=base.allowed_execution_styles,
                max_notional_override=base.max_notional_override,
                forced_degrade_mode=base.forced_degrade_mode,
                rejection_reasons=["policy_test_block"],
            )

        runtime.policy_engine.evaluate = blocked_policy
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        decision_id = runtime.audit_repo.recent(limit=1)[0].decision_id

        app = self._app(runtime)
        with TestClient(app) as client:
            decision_detail = client.get(f"/decision/{decision_id}")

        payload = decision_detail.json()
        outcome = payload["decision_outcome"]
        target = payload["position_target"]
        self.assertTrue(outcome["finalized"])
        self.assertTrue(outcome["policy_blocked"])
        self.assertIn("policy_test_block", outcome["policy_blocked_reasons"])
        self.assertEqual(outcome["final_action"], "hold")
        self.assertEqual(outcome["final_target_qty"], target["current_position_qty"])
        self.assertIsNone(payload["execution_plan"])

    async def test_finalized_decision_outcome_collapses_to_hold_when_risk_rejects(self) -> None:
        runtime = await self._runtime()

        def rejected_risk(*, target):
            return RiskDecision(
                decision_id=target.decision_id,
                approved=False,
                modified=True,
                capped_target_position_qty=Decimal("0.001"),
                capped_target_notional=Decimal("10"),
                current_open_order_count=0,
                constraints_applied=["risk_test_cap"],
                risk_score=0.9,
                flatten_required=False,
                halt_required=False,
                rejection_reasons=["risk_test_reject"],
            )

        runtime.risk_engine.evaluate = rejected_risk
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        decision_id = runtime.audit_repo.recent(limit=1)[0].decision_id

        app = self._app(runtime)
        with TestClient(app) as client:
            decision_detail = client.get(f"/decision/{decision_id}")

        payload = decision_detail.json()
        outcome = payload["decision_outcome"]
        target = payload["position_target"]
        self.assertTrue(outcome["finalized"])
        self.assertTrue(outcome["risk_capped"])
        self.assertIn("risk_test_reject", outcome["risk_capped_reasons"])
        self.assertEqual(Decimal(str(outcome["risk_capped_target_qty"])), Decimal("0.001"))
        self.assertEqual(outcome["final_action"], "hold")
        self.assertEqual(outcome["final_target_qty"], target["current_position_qty"])
        self.assertIsNone(payload["execution_plan"])

    async def test_risk_decision_payload_includes_operator_friendly_explanations(self) -> None:
        runtime = await self._runtime()

        def explained_risk(*, target):
            return RiskDecision(
                decision_id=target.decision_id,
                approved=False,
                modified=True,
                capped_target_position_qty=Decimal("0"),
                capped_target_notional=Decimal("0"),
                required_initial_margin=Decimal("20"),
                projected_margin_usage=Decimal("0.82"),
                projected_notional=Decimal("67"),
                current_open_order_count=0,
                constraints_applied=["only_reduce_required"],
                risk_score=0.82,
                flatten_required=False,
                halt_required=False,
                only_reduce_required=True,
                risk_limit_breached=True,
                liquidation_buffer_remaining=Decimal("0.03"),
                rejection_reasons=["max_daily_realized_loss_usdt_exceeded"],
            )

        runtime.risk_engine.evaluate = explained_risk
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        decision_id = runtime.audit_repo.recent(limit=1)[0].decision_id

        app = self._app(runtime)
        with TestClient(app) as client:
            decision_detail = client.get(f"/decision/{decision_id}")
            latest_risk = client.get("/risk/latest")

        detail_payload = decision_detail.json()
        risk_payload = detail_payload["risk_decision"]
        latest_payload = latest_risk.json()["risk_decision"]

        self.assertEqual(risk_payload["rejection_reason_details"][0]["code"], "max_daily_realized_loss_usdt_exceeded")
        self.assertIn("当日已实现亏损超过上限", risk_payload["rejection_reason_details"][0]["message"])
        self.assertEqual(risk_payload["constraint_details"][0]["code"], "only_reduce_required")
        self.assertIn("减仓或平仓", risk_payload["constraint_details"][0]["message"])
        self.assertIn("风控当前已阻断", risk_payload["operator_summary"])
        self.assertEqual(latest_payload["rejection_reason_details"][0]["code"], "max_daily_realized_loss_usdt_exceeded")
        self.assertEqual(latest_payload["constraint_details"][0]["code"], "only_reduce_required")

    async def test_finalized_decision_outcome_collapses_to_hold_when_kill_switch_halts_after_risk(self) -> None:
        runtime = await self._runtime()
        original_risk_evaluate = runtime.risk_engine.evaluate

        def halting_risk(*, target):
            decision = original_risk_evaluate(target=target)
            runtime.kill_switch.halt(reason="test_halt_after_risk")
            return decision

        runtime.risk_engine.evaluate = halting_risk
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        decision_id = runtime.audit_repo.recent(limit=1)[0].decision_id

        app = self._app(runtime)
        with TestClient(app) as client:
            decision_detail = client.get(f"/decision/{decision_id}")

        payload = decision_detail.json()
        outcome = payload["decision_outcome"]
        target = payload["position_target"]
        self.assertTrue(outcome["finalized"])
        self.assertTrue(outcome["policy_blocked"])
        self.assertIn("kill_switch_active", outcome["policy_blocked_reasons"])
        self.assertIn("kill_switch_active", outcome["decision_blocked_reasons"])
        self.assertEqual(outcome["final_action"], "hold")
        self.assertEqual(outcome["final_target_qty"], target["current_position_qty"])
        self.assertIsNone(payload["execution_plan"])

    async def test_baseline_only_does_not_emit_ai_chain_events(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="baseline_only",
            ai_shadow_mode_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service.provider = FakeShadowProvider()
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            overview = client.get("/ai/overview")
            latest = client.get("/ai/latest")
            recent = client.get("/ai/recent?limit=5")
            removed_takeovers_route = client.get("/ai/takeovers/recent?limit=5")
            shadow_latest = client.get("/ai/shadow/latest")
            shadow_recent = client.get("/ai/shadow/recent?limit=5")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(removed_takeovers_route.status_code, 404)
        self.assertEqual(overview.json()["shadow_summary"]["window_count"], 0)
        self.assertIsNone(latest.json()["brief"])
        self.assertIsNone(latest.json()["assessment"])
        self.assertNotIn("legacy_takeover", latest.json())
        self.assertIsNone(latest.json()["baseline_reference"])
        self.assertIsNone(latest.json()["ai_decision_intent"])
        self.assertIsNone(latest.json()["decision_outcome"])
        self.assertEqual(recent.json()["assessments"], [])
        self.assertIsNone(shadow_latest.json()["shadow_decision"])
        self.assertEqual(shadow_recent.json()["shadow_decisions"], [])
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_DECISION_BRIEFS)), 0)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_ASSESSMENTS)), 0)
        self.assertEqual(len(runtime.event_store.by_topic(topics.AI_SHADOW_DECISIONS)), 0)

    async def test_ai_endpoints_expose_bounded_live_translation_when_enabled(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_shadow_mode_enabled=True,
            ai_execution_suggestion_mode="enabled_live",
            trading_product_type="derivatives",
            margin_mode="cross",
            strategy_short_bias_enabled=True,
            default_symbol="BTC-USDT-SWAP",
            allowed_symbols=("BTC-USDT-SWAP",),
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._consecutive_failures = 0
        runtime.ai_service._consecutive_successes = 0
        runtime.ai_service._outcome_review_required = False
        runtime.ai_service._outcome_auto_downgraded = False
        await runtime.decision_engine.run_cycle(runtime.settings.default_symbol, runtime.settings.primary_timeframe)
        app = self._app(runtime)
        with TestClient(app) as client:
            latest = client.get("/ai/latest").json()
            detail = client.get(f"/decision/{client.get('/decision/latest').json()['decision_id']}").json()

        latest_translation = latest["execution_suggestion"]["latest_translation"]
        self.assertEqual(latest_translation["status"], "enabled")
        self.assertTrue(latest_translation["applied_to_live_execution"])
        self.assertEqual(latest["execution_suggestion"]["live_order_type"], "limit")
        self.assertEqual(latest["execution_suggestion"]["live_time_in_force"], "IOC")
        self.assertIsNotNone(latest["execution_suggestion"]["live_limit_price"])
        self.assertEqual(
            detail["ai_execution_suggestion"]["latest_translation"]["applied_live_fields"],
            ["execution_style", "order_type", "limit_price", "time_in_force"],
        )

    async def test_baseline_only_hides_historical_ai_events(self) -> None:
        runtime = await self._runtime(ai_operating_mode="baseline_only")
        brief = AIDecisionBrief(
            decision_id="decision_ai_hidden",
            symbol=runtime.settings.default_symbol,
            timeframe=runtime.settings.primary_timeframe,
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            last_price=100_000.0,
            regime_indicator="trend",
            regime_confidence=0.7,
            composite_alpha_score=0.3,
            momentum_score=0.01,
            volatility_state="medium",
            volatility_value=0.02,
            current_position_qty=0.0,
            current_exposure_side="flat",
            current_open_order_count=0,
            baseline_direction_bias="long",
            baseline_confidence=0.6,
            fee_bps=runtime.settings.paper_taker_fee_bps,
            max_slippage_tolerance_bps=float(runtime.settings.max_slippage_tolerance_bps),
            expected_slippage_proxy_bps=2.0,
            min_net_edge_bps=runtime.settings.strategy_min_net_edge_bps,
            safe_to_trade=True,
            review_required=False,
            halted=False,
            reconciliation_severity="CLEAN",
            reconciliation_halt_required=False,
            market_snapshot_fresh=True,
            account_snapshot_fresh=True,
            execution_condition="normal",
        )
        assessment = AIMarketAssessment(
            decision_id="decision_ai_hidden",
            symbol=runtime.settings.default_symbol,
            regime="trend",
            directional_edge=0.4,
            expected_volatility=0.08,
            confidence=0.8,
            uncertainty=0.2,
            expected_holding_horizon="15m",
            invalidation_conditions=["trend_break", "book_flip"],
            risk_tags=["provider_ok"],
            rationale_summary="historical_assessment",
            operating_mode="ai_primary",
            provider_name="fake",
            output_valid=True,
            fallback_used=False,
            degraded=False,
            calibrated_confidence=0.75,
            baseline_override_recommended=True,
            override_reason_codes=["ai_trend_override"],
            economically_actionable=True,
            estimated_edge_bps=40.0,
            estimated_cost_bps=12.0,
            estimated_net_edge_bps=28.0,
            source_mode="provider",
            execution_condition="normal",
            model_name="fake",
            model_version="1",
            prompt_version="1",
        )
        brief_event = build_envelope(
            topic=topics.AI_DECISION_BRIEFS,
            key=runtime.settings.default_symbol,
            payload_model=brief,
            source_component="test",
        )
        assessment_event = build_envelope(
            topic=topics.AI_ASSESSMENTS,
            key=runtime.settings.default_symbol,
            payload_model=assessment,
            source_component="test",
        )
        runtime.event_store.append(brief_event)
        runtime.event_store.append(assessment_event)
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id="decision_ai_hidden",
                decision_context_ref=brief_event.event_id,
                ai_decision_brief_ref=brief_event.event_id,
                ai_market_assessment_ref=assessment_event.event_id,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            latest = client.get("/ai/latest")
            recent = client.get("/ai/recent?limit=5")
            decision = client.get("/decision/decision_ai_hidden")

        self.assertEqual(latest.status_code, 200)
        self.assertIsNone(latest.json()["brief"])
        self.assertIsNone(latest.json()["assessment"])
        self.assertNotIn("legacy_takeover", latest.json())
        self.assertIsNone(latest.json()["baseline_reference"])
        self.assertIsNone(latest.json()["ai_decision_intent"])
        self.assertIsNone(latest.json()["decision_outcome"])
        self.assertEqual(recent.json()["assessments"], [])
        self.assertIsNone(decision.json()["ai_decision_brief"])
        self.assertIsNone(decision.json()["ai_assessment"])
        self.assertNotIn("legacy_ai_takeover_decision", decision.json())
        self.assertIsNone(decision.json()["baseline_reference"])
        self.assertIsNone(decision.json()["ai_decision_intent"])
        self.assertIsNone(decision.json()["decision_outcome"])

    async def test_recovery_blocks_when_ai_is_degraded_without_auto_downgrade(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service._degraded = True
        runtime.ai_service._degradation_reason = "ai_timeout"
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery")
            health = client.get("/system/health")

        self.assertEqual(recovery.status_code, 200)
        self.assertTrue(recovery.json()["recovery"]["review_required"])
        self.assertIn("ai_degraded_requires_manual_review", recovery.json()["recovery"]["resume_blocked_reasons"])
        blockers = [item["blocker"] for item in health.json()["blockers"]]
        self.assertIn("ai_degraded_requires_manual_review", blockers)

    async def test_recovery_does_not_block_when_ai_auto_downgrade_is_active(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=True,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service._degraded = True
        runtime.ai_service._degradation_reason = "ai_timeout"
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery")

        self.assertEqual(recovery.status_code, 200)
        self.assertFalse(recovery.json()["recovery"]["review_required"])
        self.assertNotIn("ai_degraded_requires_manual_review", recovery.json()["recovery"]["resume_blocked_reasons"])

    async def test_blocker_control_prioritizes_ai_review_over_surface_halt_and_exposes_actions(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._degradation_reason = ""
        runtime.ai_service._outcome_review_required = True
        runtime.ai_service._outcome_degradation_reason = "ai_shadow_underperformed_baseline"
        runtime.kill_switch.halt("operator_test_halt")
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.get("/system/blocker-control")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["primary_blocker"]["blocker"], "ai_degraded_requires_manual_review")
        self.assertTrue(payload["primary_blocker"]["root_cause"])
        self.assertIn("确认恢复 AI 决策", [item["label"] for item in payload["primary_blocker"]["actions"]])
        self.assertIn("改为仅基础策略继续运行", [item["label"] for item in payload["primary_blocker"]["actions"]])
        self.assertTrue(any(item["blocker"] == "kill_switch_active" for item in payload["secondary_blockers"]))

    async def test_blocker_control_and_history_surface_phase1_shadow_blocker(self) -> None:
        runtime = await self._runtime()
        runtime.phase1_shadow_monitor = FakeLaggingPhase1ShadowMonitor()
        runtime.health_service.phase1_shadow_provider = runtime.phase1_shadow_monitor
        app = self._app(runtime)

        with TestClient(app) as client:
            health = client.get("/system/health")
            blocker_control = client.get("/system/blocker-control")
            blocker_history = client.get("/system/blocker-history?limit=5")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(blocker_control.status_code, 200)
        self.assertEqual(blocker_history.status_code, 200)

        health_payload = health.json()
        blocker_payload = blocker_control.json()
        history_payload = blocker_history.json()

        health_blockers = [item["blocker"] for item in health_payload["blockers"]]
        self.assertIn("phase1_shadow_lagging", health_blockers)
        self.assertEqual(blocker_payload["primary_blocker"]["blocker"], "phase1_shadow_lagging")
        self.assertEqual(blocker_payload["primary_blocker"]["subsystem"], "phase1_shadow")
        self.assertIn("查看影子详情", [item["label"] for item in blocker_payload["primary_blocker"]["actions"]])
        self.assertIn("刷新当前状态", [item["label"] for item in blocker_payload["primary_blocker"]["actions"]])
        self.assertIn("已核查，继续阻断", [item["label"] for item in blocker_payload["primary_blocker"]["actions"]])
        self.assertTrue(
            any(
                any(entry.get("blocker") == "phase1_shadow_lagging" for entry in row.get("blockers", []))
                for row in history_payload["history"]
            )
        )

    async def test_phase1_shadow_detail_and_review_action_are_visible_to_operator(self) -> None:
        runtime = await self._runtime()
        runtime.phase1_shadow_monitor = FakeLaggingPhase1ShadowMonitor()
        runtime.health_service.phase1_shadow_provider = runtime.phase1_shadow_monitor
        app = self._app(runtime)

        with TestClient(app) as client:
            blocker_control = client.get("/system/blocker-control").json()
            before = client.get("/system/shadow")
            action = client.post(
                "/system/blocker-actions/acknowledge-phase1-shadow",
                json={
                    "panel_version": blocker_control["panel_version"],
                    "blocker": "phase1_shadow_lagging",
                    "reason": "operator_reviewed_phase1_shadow_and_keeps_blocked",
                },
            )
            after = client.get("/system/shadow")
            history = client.get("/system/shadow/history?limit=10")

        self.assertEqual(before.status_code, 200)
        self.assertEqual(action.status_code, 200)
        self.assertEqual(after.status_code, 200)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(action.json()["action_id"], "acknowledge-phase1-shadow")
        self.assertEqual(action.json()["status"], "completed")

        before_payload = before.json()
        after_payload = after.json()
        history_payload = history.json()
        self.assertEqual(before_payload["status"], "lagging")
        self.assertTrue(before_payload["review_recommended"])
        self.assertIsNone(before_payload["latest_review_action"])
        self.assertEqual(after_payload["latest_review_action"]["action"], "phase1_shadow_review")
        self.assertEqual(after_payload["latest_review_action"]["details"]["snapshot_status"], "lagging")
        self.assertEqual(after_payload["latest_review_action"]["details"]["lag"]["order_backlog"], 2)
        self.assertIn("phase1_shadow_lagging", after_payload["blockers"])
        self.assertTrue(history_payload["history"])
        self.assertEqual(history_payload["history"][0]["entry_type"], "review")
        self.assertEqual(history_payload["history"][0]["details"]["lag"]["order_backlog"], 2)

    async def test_blocker_action_degrade_to_baseline_clears_review_requirement_and_keeps_system_resumable(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._degradation_reason = ""
        runtime.ai_service._outcome_review_required = True
        runtime.ai_service._outcome_degradation_reason = "ai_shadow_underperformed_baseline"
        runtime.kill_switch.halt("operator_test_halt")
        app = self._app(runtime)

        with TestClient(app) as client:
            blocker_control = client.get("/system/blocker-control").json()
            response = client.post(
                "/system/blocker-actions/ai-review-degrade-to-baseline",
                json={
                    "panel_version": blocker_control["panel_version"],
                    "blocker": "ai_degraded_requires_manual_review",
                    "reason": "operator_reject_ai_and_continue_with_baseline",
                },
            )
            recovery = client.get("/system/recovery").json()
            ai_runtime = client.get("/ai/runtime").json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertFalse(recovery["recovery"]["review_required"])
        self.assertTrue(recovery["recovery"]["resume_eligible"])
        self.assertEqual(ai_runtime["manual_override_mode"], "baseline_only")

    async def test_blocker_action_restore_ai_clears_review_requirement_without_baseline_override(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._degradation_reason = ""
        runtime.ai_service._outcome_review_required = True
        runtime.ai_service._outcome_degradation_reason = "ai_shadow_underperformed_baseline"
        app = self._app(runtime)

        with TestClient(app) as client:
            blocker_control = client.get("/system/blocker-control").json()
            response = client.post(
                "/system/blocker-actions/ai-review-restore",
                json={
                    "panel_version": blocker_control["panel_version"],
                    "blocker": "ai_degraded_requires_manual_review",
                    "reason": "operator_restore_ai_after_review",
                },
            )
            recovery = client.get("/system/recovery").json()
            ai_runtime = client.get("/ai/runtime").json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(recovery["recovery"]["review_required"])
        self.assertIsNone(ai_runtime["manual_override_mode"])
        self.assertEqual(ai_runtime["review_resolution"], "operator_restore_ai")

    async def test_blocker_action_rejects_stale_panel_version(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service.provider = FakeShadowProvider()
        runtime.ai_service._degraded = False
        runtime.ai_service._degradation_reason = ""
        runtime.ai_service._outcome_review_required = True
        runtime.ai_service._outcome_degradation_reason = "ai_shadow_underperformed_baseline"
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/system/blocker-actions/ai-review-restore",
                json={
                    "panel_version": "stale-version",
                    "blocker": "ai_degraded_requires_manual_review",
                    "reason": "operator_restore_ai_after_review",
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "blocker_control_state_changed")

    async def test_provider_degraded_blocker_does_not_offer_manual_review_resolution_buttons(self) -> None:
        runtime = await self._runtime(
            ai_operating_mode="ai_primary",
            ai_auto_downgrade_enabled=False,
            ai_provider="openai",
            openai_api_key="test-key",
        )
        runtime.ai_service._degraded = True
        runtime.ai_service._degradation_reason = "ai_timeout"
        app = self._app(runtime)

        with TestClient(app) as client:
            payload = client.get("/system/blocker-control").json()

        primary = payload["primary_blocker"]
        self.assertEqual(primary["blocker"], "ai_degraded_requires_manual_review")
        action_ids = [item["action_id"] for item in primary["actions"]]
        self.assertNotIn("ai-review-restore", action_ids)
        self.assertNotIn("ai-review-degrade-to-baseline", action_ids)
        self.assertIn("open-ai-workbench", action_ids)

    async def test_halt_resume_and_stale_market_blocker_are_visible(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            health_after_halt = client.get("/system/health")
            resumed = client.post("/system/resume", json={"reason": "operator_test_resume"})

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(health_after_halt.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(halted.json()["halted"])
        blockers = [item["blocker"] for item in health_after_halt.json()["blockers"]]
        self.assertIn("kill_switch_active", blockers)
        self.assertFalse(resumed.json()["halted"])

        latest_snapshot = runtime.market_gateway.latest_snapshot(runtime.settings.default_symbol)
        self.assertIsNotNone(latest_snapshot)
        runtime.market_gateway._latest_snapshots[runtime.settings.default_symbol] = latest_snapshot.model_copy(
            update={"snapshot_ts": utc_now() - timedelta(seconds=120)}
        )
        runtime.market_gateway._latest_received_at[runtime.settings.default_symbol] = utc_now() - timedelta(seconds=120)
        with TestClient(app) as client:
            stale_health = client.get("/system/health").json()
        stale_blockers = [item["blocker"] for item in stale_health["blockers"]]
        self.assertIn("market_data_stale", stale_blockers)

    async def test_manual_halt_marks_recovery_as_manually_halted_and_resume_eligible(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            recovery = client.get("/system/recovery")

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.json()["recovery"]["recovery_state"], "manually_halted")
        self.assertTrue(recovery.json()["recovery"]["resume_eligible"])
        self.assertFalse(recovery.json()["recovery"]["safe_to_trade"])

    async def test_resume_ignores_kill_switch_as_the_only_blocker(self) -> None:
        runtime = await self._runtime()
        query = runtime  # keep runtime in scope for intent clarity
        app = self._app(query)
        with TestClient(app) as client:
            halted = client.post("/system/halt", json={"reason": "operator_test_halt"})
            resumed = client.post("/system/resume", json={"reason": "operator_test_resume"})

        self.assertEqual(halted.status_code, 200)
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["halted"])
        self.assertIn(resumed.json()["status"], {"resumed", "already_resumed"})

    async def test_system_health_reports_reconciliation_staleness_consistently(self) -> None:
        runtime = await self._runtime()
        latest_report = runtime.reconciliation_repo.latest()
        self.assertIsNotNone(latest_report)
        runtime.reconciliation_repo.save_report(
            latest_report.model_copy(update={"as_of_ts": utc_now() - timedelta(seconds=601)})
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            health = client.get("/system/health").json()

        blockers = [item["blocker"] for item in health["blockers"]]
        self.assertIn("reconciliation_stale", blockers)
        self.assertFalse(health["subsystems"]["reconciliation"]["fresh"])
        self.assertFalse(health["subsystems"]["reconciliation"]["ready"])
        self.assertIn("reconciliation_stale", health["subsystems"]["reconciliation"]["blockers"])
        self.assertFalse(health["freshness"]["reconciliation_fresh"])

    async def test_operator_auth_enforces_read_write_split_and_reconciliation_validate(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_read_api_key="read-key",
            operator_write_api_key="write-key",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            unauthorized = client.get("/system/health")
            read_allowed = client.get("/system/health", headers={"X-AATS-API-Key": "read-key"})
            read_denied_write = client.post(
                "/system/halt",
                json={"reason": "should_fail"},
                headers={"X-AATS-API-Key": "read-key"},
            )
            write_allowed = client.post(
                "/system/halt",
                json={"reason": "authorized_halt"},
                headers={"X-AATS-API-Key": "write-key"},
            )
            reconciliation_validate = client.post(
                "/reconciliation/validate",
                json={"reason": "startup_check"},
                headers={"X-AATS-API-Key": "write-key"},
            )

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(read_allowed.status_code, 200)
        self.assertEqual(read_denied_write.status_code, 403)
        self.assertEqual(write_allowed.status_code, 200)
        self.assertEqual(reconciliation_validate.status_code, 200)

    async def test_memory_storage_auth_surface_is_not_reported_as_database_backed(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
        )
        runtime.operator_repo.save_user(
            OperatorUserRecord(
                username="admin",
                password_hash=hash_password("secret"),
                role="admin",
            )
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            providers = client.get("/auth/providers")
            login = client.post("/auth/login", json={"username": "admin", "password": "secret"})
            session = client.get("/auth/session")

        self.assertEqual(providers.status_code, 200)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertFalse(providers.json()["database_backed"])
        self.assertFalse(providers.json()["runtime_profile_control_enabled"])
        self.assertFalse(session.json()["database_backed"])

    async def test_session_login_enforces_viewer_and_operator_roles(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("viewer", "viewer-pass"), ("operator", "operator-pass"), ("admin", "admin-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as viewer_client:
            login = viewer_client.post("/auth/login", json={"username": "viewer", "password": "viewer-pass"})
            health = viewer_client.get("/system/health")
            halt_denied = viewer_client.post("/system/halt", json={"reason": "viewer_should_fail"})
            logout = viewer_client.post("/auth/logout")

        with TestClient(app) as operator_client:
            login_operator = operator_client.post("/auth/login", json={"username": "operator", "password": "operator-pass"})
            halt_denied_operator = operator_client.post("/system/halt", json={"reason": "operator_should_fail"})
            session = operator_client.get("/auth/session")

        with TestClient(app) as admin_client:
            login_admin = admin_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            halt_allowed_admin = admin_client.post("/system/halt", json={"reason": "admin_should_work"})

        self.assertEqual(login.status_code, 200)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(halt_denied.status_code, 403)
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(login_operator.status_code, 200)
        self.assertEqual(halt_denied_operator.status_code, 403)
        self.assertEqual(halt_denied_operator.json()["detail"], "operator_admin_access_required")
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json()["authenticated"])
        self.assertEqual(session.json()["role"], "operator")
        self.assertEqual(login_admin.status_code, 200)
        self.assertEqual(halt_allowed_admin.status_code, 200)

    async def test_logout_revokes_existing_session_token(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)
        cookie_name = runtime.settings.operator_session_cookie_name
        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self.assertEqual(login.status_code, 200)
            token = client.cookies.get(cookie_name)
            self.assertIsNotNone(token)
            logout = client.post("/auth/logout")
            self.assertEqual(logout.status_code, 200)

        with TestClient(app) as replay_client:
            replay_client.cookies.set(cookie_name, token)
            whoami = replay_client.get("/auth/whoami")

        self.assertEqual(whoami.status_code, 401)
        self.assertEqual(whoami.json()["detail"], "operator_auth_required")

    async def test_session_login_rejects_invalid_credentials(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "correct-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "admin", "password": "wrong-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_disabled_database_operator_account_cannot_log_in(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "correct-pass")],
        )
        runtime.operator_repo.save_user(
            OperatorUserRecord(
                username="disabled-user",
                password_hash=hash_password("disabled-pass"),
                role="operator",
                enabled=False,
            )
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            failed = client.post("/auth/login", json={"username": "disabled-user", "password": "disabled-pass"})

        self.assertEqual(failed.status_code, 401)
        self.assertEqual(failed.json()["detail"], "operator_login_failed")

    async def test_admin_can_manage_operator_users_and_audit_login_and_crud_actions(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            users = client.get("/auth/users")
            created = client.post(
                "/auth/users",
                json={
                    "username": "viewer2",
                    "password": "viewer-pass",
                    "role": "viewer",
                    "enabled": True,
                },
            )
            updated = client.patch(
                "/auth/users/viewer2",
                json={"role": "operator", "enabled": False},
            )
            deleted = client.delete("/auth/users/viewer2")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(users.status_code, 200)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(deleted.status_code, 200)

        stored_admin = runtime.operator_repo.get_by_username("admin")
        self.assertIsNotNone(stored_admin)
        self.assertIsNotNone(stored_admin.last_login_at)

        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        login_action = next(item for item in reversed(actions) if item["action"] == "login")
        create_action = next(item for item in reversed(actions) if item["action"] == "user_create")
        update_action = next(item for item in reversed(actions) if item["action"] == "user_update")
        delete_action = next(item for item in reversed(actions) if item["action"] == "user_delete")

        self.assertEqual(login_action["actor_identity"], "admin")
        self.assertEqual(login_action["actor_role"], "admin")
        self.assertEqual(create_action["details"]["target_username"], "viewer2")
        self.assertEqual(update_action["details"]["target_username"], "viewer2")
        self.assertEqual(delete_action["details"]["target_username"], "viewer2")

    async def test_strategy_profile_routes_seed_snapshot_and_generate_recommendation(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            snapshot = client.get("/strategy-profiles")
            summary = client.get("/strategy-profiles/summary")
            optimization_reports = client.get("/strategy-profiles/optimization/reports?limit=5&offset=0")
            selection_decisions = client.get("/strategy-profiles/selection-decisions?limit=5&offset=0")
            activation_history = client.get("/strategy-profiles/activation-history?limit=5&offset=0")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(optimization_reports.status_code, 200)
        self.assertEqual(selection_decisions.status_code, 200)
        self.assertEqual(activation_history.status_code, 200)
        snapshot_payload = snapshot.json()
        summary_payload = summary.json()
        self.assertIn("scope", snapshot_payload)
        self.assertIn("safety_state", snapshot_payload)
        self.assertIn("activation", snapshot_payload)
        self.assertIn("active_revision", snapshot_payload)
        self.assertIn("profile_space", snapshot_payload)
        self.assertIn("comparison_report", snapshot_payload)
        self.assertIn("latest_optimization_report", snapshot_payload)
        self.assertIn("latest_selection_decision", snapshot_payload)
        self.assertIn("execution_parameter_suggestion_capability", snapshot_payload)
        self.assertIn("activation_history", snapshot_payload)
        self.assertNotIn("latest_recommendation", snapshot_payload)
        self.assertNotIn("evaluations", snapshot_payload)
        self.assertNotIn("rejections", snapshot_payload)
        self.assertNotIn("auto_rollback_policy", snapshot_payload)
        self.assertNotIn("activation_policy", snapshot_payload)
        self.assertEqual(snapshot_payload["profile_space"]["selection_mode"], "registered_profile_only")
        self.assertFalse(snapshot_payload["profile_space"]["free_form_parameter_generation_enabled"])
        self.assertEqual(
            set(summary_payload.keys()),
            {"activation", "active_revision", "latest_selection_decision", "activation_history"},
        )
        self.assertNotIn("comparison_report", summary_payload)
        self.assertNotIn("latest_optimization_report", summary_payload)
        self.assertNotIn("execution_parameter_suggestion_capability", summary_payload)
        self.assertEqual(optimization_reports.json()["reports"], [])
        self.assertEqual(selection_decisions.json()["decisions"], [])
        self.assertIn("history", activation_history.json())
        return

    async def test_ai_config_summary_route_returns_only_ai_config_page_fields(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            ai_operating_mode="ai_decision_maker_with_profile_control",
            strategy_profile_auto_control_enabled=True,
            ai_shadow_mode_enabled=True,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            summary = client.get("/ai-config/summary")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(summary.status_code, 200)
        payload = summary.json()
        self.assertEqual(set(payload.keys()), {"runtime_profile", "strategy_profile", "ai"})
        self.assertEqual(
            set(payload["runtime_profile"].keys()),
            {"profile_source", "control_plane_status", "current_runtime_payload"},
        )
        self.assertEqual(
            set(payload["strategy_profile"].keys()),
            {"activation", "active_revision", "latest_selection_decision", "activation_history"},
        )
        if payload["strategy_profile"]["activation_history"]:
            self.assertIn("executed_at", payload["strategy_profile"]["activation_history"][0])
            self.assertNotIn("activated_at", payload["strategy_profile"]["activation_history"][0])
        self.assertEqual(
            set(payload["ai"].keys()),
            {
                "configured_operating_mode",
                "effective_operating_mode",
                "shadow_mode_enabled",
                "strategy_profile_auto_control_configured",
                "strategy_profile_auto_control_effective",
                "strategy_profile_auto_control_reason",
                "shadow_summary",
                "latest_profile_control_decision",
            },
        )
        return

    async def test_admin_can_manually_override_ai_operating_mode_with_freeze_and_restore_auto(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            ai_operating_mode="ai_decision_maker_with_profile_control",
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            override = client.post(
                "/ai/operating-mode/override",
                json={"mode": "baseline_only", "reason": "test_manual_override_ai_operating_mode"},
            )
            runtime_after_override = client.get("/ai/runtime")
            restore = client.post(
                "/ai/operating-mode/restore-auto",
                json={"reason": "test_restore_auto_ai_operating_mode"},
            )
            runtime_after_restore = client.get("/ai/runtime")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(override.status_code, 200)
        self.assertEqual(runtime_after_override.status_code, 200)
        override_payload = override.json()
        runtime_payload = runtime_after_override.json()
        self.assertEqual(override_payload["status"], "completed")
        self.assertEqual(runtime_payload["manual_override_mode"], "baseline_only")
        self.assertTrue(runtime_payload["manual_override_active"])
        self.assertIsNotNone(runtime_payload["manual_override_freeze_until"])
        self.assertEqual(runtime_payload["effective_operating_mode"], "baseline_only")

        self.assertEqual(restore.status_code, 200)
        self.assertEqual(runtime_after_restore.status_code, 200)
        restored_payload = runtime_after_restore.json()
        self.assertFalse(restored_payload["manual_override_active"])
        self.assertIsNone(restored_payload["manual_override_mode"])
        self.assertIsNone(restored_payload["manual_override_freeze_until"])
        self.assertEqual(restored_payload["configured_operating_mode"], "ai_decision_maker_with_profile_control")
        self.assertIn(
            restored_payload["effective_operating_mode"],
            {"ai_decision_maker_with_profile_control", "baseline_only"},
        )

        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")
            optimization_reports = client.get("/strategy-profiles/optimization/reports?limit=5&offset=0")
            selection_decisions = client.get("/strategy-profiles/selection-decisions?limit=5&offset=0")
            auto_rollback_policy = client.get("/strategy-profiles/auto-rollback-policy")
            activation_policy = client.get("/strategy-profiles/activation-policy")
            recommendations = client.get("/strategy-profiles/recommendations?limit=5&offset=0")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(evaluated.status_code, 404)
        self.assertEqual(optimization_reports.status_code, 200)
        self.assertEqual(selection_decisions.status_code, 200)
        self.assertEqual(auto_rollback_policy.status_code, 404)
        self.assertEqual(activation_policy.status_code, 404)
        self.assertEqual(recommendations.status_code, 404)
        snapshot_payload = snapshot.json()
        self.assertIn(
            snapshot_payload["activation"]["active_profile_id"],
            {"trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"},
        )
        self.assertTrue(snapshot_payload["revisions"])
        self.assertIn("safety_state", snapshot_payload)
        self.assertIn("profile_space", snapshot_payload)
        self.assertIn("comparison_report", snapshot_payload)
        self.assertIn("control_summary", snapshot_payload)
        self.assertIn("latest_optimization_report", snapshot_payload)
        self.assertIn("latest_selection_decision", snapshot_payload)
        self.assertIn("execution_parameter_suggestion_capability", snapshot_payload)
        self.assertNotIn("auto_rollback_policy", snapshot_payload)
        self.assertNotIn("activation_policy", snapshot_payload)
        self.assertEqual(snapshot_payload["profile_space"]["selection_mode"], "registered_profile_only")
        self.assertFalse(snapshot_payload["profile_space"]["free_form_parameter_generation_enabled"])
        self.assertFalse(snapshot_payload["execution_parameter_suggestion_capability"]["enabled"])
        self.assertTrue(snapshot_payload["comparison_report"]["rows"])
        self.assertEqual(optimization_reports.json()["reports"], [])
        self.assertEqual(selection_decisions.json()["decisions"], [])

    async def test_strategy_profile_provider_output_is_limited_to_registered_profiles(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            removed = {
                "evaluate_now": client.post("/strategy-profiles/auto-tuning/evaluate-now"),
                "recommendations": client.get("/strategy-profiles/recommendations?limit=5&offset=0"),
                "pending_activate": client.post("/strategy-profiles/pending/activate", json={"reason": "obsolete"}),
                "rollback": client.post("/strategy-profiles/rollback", json={"reason": "obsolete"}),
                "auto_rollback_policy": client.get("/strategy-profiles/auto-rollback-policy"),
                "activation_policy": client.get("/strategy-profiles/activation-policy"),
            }

        for response in removed.values():
            self.assertEqual(response.status_code, 404)
        return

        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            ai_provider="openai",
            openai_api_key="test-key",
        )
        app = self._app(runtime)

        async def _bad_profile_response(*args, **kwargs):
            _ = args
            _ = kwargs
            return AIProviderResponse(
                provider_name="test_provider",
                request_id="req_bad_profile",
                latency_ms=12.0,
                payload={
                    "recommended_profile_id": "unregistered_profile",
                    "fallback_profile_id": "trend_normal",
                    "confidence": 0.95,
                    "market_regime_assessment": {
                        "regime": "trend",
                        "volatility_state": "medium",
                        "execution_condition": "normal",
                    },
                    "reason_codes": ["bad_profile_test"],
                    "human_summary": "provider proposed an unknown profile",
                    "risk_notes": ["should_fallback"],
                    "valid_for_minutes": 60,
                },
            )

        with patch(
            "aats.services.ai_service.openai_provider.OpenAIProvider.generate_assessment",
            new=AsyncMock(side_effect=_bad_profile_response),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        recommendation = evaluated.json()["recommendation"]
        self.assertIn(
            recommendation["recommended_profile_id"],
            {"trend_aggressive", "trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"},
        )
        self.assertNotEqual(recommendation["recommended_profile_id"], "unregistered_profile")
        self.assertEqual(recommendation["generated_by"], "winner_engine")
        self.assertEqual(recommendation["ai_advice"]["provider"], "rule_fallback")
        self.assertTrue(recommendation["ai_advice"]["used_fallback"])
        self.assertEqual(
            recommendation["fallback_reason_code"],
            "strategy_profile_provider_recommended_unregistered_profile",
        )

    async def test_strategy_profile_seed_backfills_missing_registered_profile(self) -> None:
        runtime = await self._runtime()
        legacy_profiles = [
            "trend_normal",
            "trend_strict",
            "range_defensive",
            "high_volatility_defensive",
            "execution_degraded_safe",
        ]
        repo = runtime.strategy_profile_repo.__class__()
        payload = strategy_profile_payload_from_settings(runtime.settings)
        for revision in [
            item
            for item in _seed_revisions(settings=runtime.settings, payload=payload)
            if item.profile_id in set(legacy_profiles)
        ]:
            repo.save_revision(revision)

        seed_strategy_profiles(settings=runtime.settings, repo=repo)

        revisions = repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
        )
        self.assertEqual(
            {item.profile_id for item in revisions},
            {
                "trend_aggressive",
                "trend_normal",
                "trend_strict",
                "range_defensive",
                "high_volatility_defensive",
                "execution_degraded_safe",
            },
        )

    async def test_strategy_profile_evaluate_now_can_generate_recommendation_without_auto_switch(self) -> None:
        self.skipTest("旧策略档位建议控制面已删除，只保留主链自动切换与管理员手动切换。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.95,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["range_regime_detected"],
            human_summary="range conditions favor defensive profile",
            risk_notes=["financial_safety_priority"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_no_auto_switch",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post(
                    "/strategy-profiles/auto-tuning/evaluate-now",
                    json={"allow_auto_activation": False},
                )
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertNotIn("auto_activation", evaluated.json())
        self.assertNotIn("auto_rollback", evaluated.json())
        self.assertEqual(
            evaluated.json()["recommendation"]["ai_advice"]["preferred_profile_id"],
            "range_defensive",
        )
        self.assertEqual(evaluated.json()["recommendation"]["selection_source"], "winner_engine")
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_optimization_and_selection_reports_are_versioned(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过页面触发 recommendation 流。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            first = client.post("/strategy-profiles/auto-tuning/evaluate-now").json()
            second = client.post("/strategy-profiles/auto-tuning/evaluate-now").json()

        first_report = first["optimization_report"]
        second_report = second["optimization_report"]
        first_decision = first["selection_decision"]
        second_decision = second["selection_decision"]

        self.assertEqual(first_report["version"], 1)
        self.assertEqual(second_report["version"], 2)
        self.assertEqual(second_report["parent_report_id"], first_report["report_id"])
        self.assertIn(first_decision["decision_status"], {"auto_activation_executed", "stable_keep_active", "recommended_not_executed", "winner_policy_recommended_not_executed", "auto_rollback_recommended", "execution_outcome_recorded"})
        self.assertIn(second_decision["decision_status"], {"auto_activation_executed", "stable_keep_active", "recommended_not_executed", "winner_policy_recommended_not_executed", "execution_outcome_recorded", "auto_rollback_recommended"})
        self.assertIn("execution_state", second_decision)
        self.assertGreater(second_decision["version"], first_decision["version"])
        self.assertIsNotNone(second_decision["parent_decision_id"])

    async def test_strategy_profile_selection_outcome_and_auto_rollback_are_written_back(self) -> None:
        self.skipTest("自动回滚 recommendation 流已删除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.85,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["manual_activation_for_outcome_writeback"],
            human_summary="activate defensive profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_outcome",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        for index in range(3):
            runtime.execution_repo.save_fill(
                FillEvent(
                    fill_id=f"fill_outcome_{index}",
                    decision_id=f"decision_outcome_{index}",
                    intent_id=f"intent_outcome_{index}",
                    client_order_id=f"clord_outcome_{index}",
                    exchange_order_id=f"ex_outcome_{index}",
                    symbol=runtime.settings.default_symbol,
                    venue="PAPER",
                    side="buy",
                    fill_qty=0.001,
                    fill_price=100_000.0,
                    fee_amount=1.0,
                    fee_currency="USDT",
                    liquidity_role="taker",
                    exchange_timestamp=utc_now(),
                    ingestion_timestamp=utc_now(),
                    order_status_after_fill="FILLED",
                )
            )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={
                "safe_to_trade": False,
                "review_required": True,
            }
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(evaluated.status_code, 200)
        selection = evaluated.json()["selection_decision"]
        self.assertEqual(selection["decision_status"], "auto_rollback_recommended")
        self.assertEqual(selection["execution_state"], "executed")
        self.assertEqual(selection["execution_outcome"]["evaluation_status"], "degraded")
        self.assertTrue(selection["auto_rollback_recommendation"]["recommended"])
        self.assertEqual(selection["auto_rollback_recommendation"]["target_profile_id"], "trend_normal")

    async def test_strategy_profile_auto_rollback_policy_can_execute_rollback(self) -> None:
        self.skipTest("自动回滚策略生命周期已从公开控制面移除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_rollback_enabled=True,
            strategy_profile_auto_rollback_review_required_only=False,
            strategy_profile_auto_rollback_min_trade_count=1,
            strategy_profile_auto_rollback_cooldown_seconds=0.0,
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.85,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["manual_activation_for_auto_rollback"],
            human_summary="activate defensive profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_auto_rollback",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        runtime.execution_repo.save_fill(
            FillEvent(
                fill_id="fill_auto_rollback",
                decision_id="decision_auto_rollback",
                intent_id="intent_auto_rollback",
                client_order_id="clord_auto_rollback",
                exchange_order_id="ex_auto_rollback",
                symbol=runtime.settings.default_symbol,
                venue="PAPER",
                side="buy",
                fill_qty=0.001,
                fill_price=100_000.0,
                fee_amount=1.0,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )
        )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={"safe_to_trade": False, "review_required": True}
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(evaluated.json()["auto_rollback"]["status"], "auto_rollback_executed")
        latest_selection = evaluated.json()["selection_decision"]
        self.assertEqual(latest_selection["decision_status"], "auto_rollback_executed")
        self.assertEqual(latest_selection["execution_state"], "rolled_back")
        self.assertTrue(latest_selection["auto_rollback_recommendation"]["executed"])
        self.assertEqual(runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        ).active_profile_id, "trend_normal")

    async def test_strategy_profile_optimization_uses_cross_bucket_replay_scoring(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过公开接口触发优化报告。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        control = StrategyProfileControlService(runtime)
        regime = str((control._tuning_context().baseline or {}).get("regime") or "uncertain")
        for validation_id, healthy in (("replay_a", True), ("replay_b", True), ("replay_c", False)):
            runtime.event_store.append(
                build_envelope(
                    topic=topics.REPLAY_VALIDATIONS,
                    key=runtime.settings.default_symbol,
                    payload_model=ReplayValidationSummary(
                        validation_id=validation_id,
                        validated_at=utc_now(),
                        decision_id=f"decision_{validation_id}",
                        symbol=runtime.settings.default_symbol,
                        regime=regime,
                        active_profile_id="trend_normal" if validation_id != "replay_c" else "trend_strict",
                        product_type=runtime.settings.trading_product_type,
                        margin_mode=runtime.settings.margin_mode,
                        allowed_symbols=runtime.settings.allowed_symbols,
                        replayed_event_count=10,
                        stored_snapshot_count=5,
                        divergence_count=0 if healthy else 2,
                        divergence_density=0.0 if healthy else 0.2,
                        chain_health_score=0.98 if healthy else 0.6,
                        healthy=healthy,
                    ),
                    source_component="test",
                )
            )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        replay_summary = evaluated.json()["optimization_report"]["replay_summary"]
        self.assertEqual(replay_summary["current_cross_bucket"]["count"], 2)
        self.assertEqual(
            evaluated.json()["optimization_report"]["offline_replay_pipeline"]["pipeline_version"],
            "offline_replay_pipeline_v2",
        )
        self.assertEqual(
            [item["window"] for item in evaluated.json()["optimization_report"]["offline_replay_pipeline"]["window_reports"]],
            [10, 20, 50],
        )
        self.assertTrue(
            any(
                item["symbol"] == runtime.settings.default_symbol
                and item["regime"] == regime
                and item["profile_id"] == "trend_normal"
                for item in replay_summary["cross_bucket_scores"]
            )
        )
        trend_normal = next(
            item for item in evaluated.json()["optimization_report"]["candidates"] if item["profile_id"] == "trend_normal"
        )
        self.assertIn("replay_symbol_regime_profile_bucket_available", trend_normal["reasons"])
        self.assertIn("offline_replay_breakdown", trend_normal)
        self.assertIn("experiments", trend_normal["offline_replay_breakdown"])
        self.assertIn("winner_selection_policy", evaluated.json()["optimization_report"])
        self.assertIn("auto_activation", evaluated.json()["optimization_report"]["winner_selection_policy"])

    async def test_strategy_profile_auto_rollback_policy_can_be_persisted_by_scope(self) -> None:
        self.skipTest("自动回滚策略生命周期已从公开控制面移除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_rollback_enabled=False,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            updated = client.post(
                "/strategy-profiles/auto-rollback-policy",
                json={
                    "enabled": True,
                    "review_required_only": False,
                    "min_trade_count": 2,
                    "cooldown_seconds": 60.0,
                    "matrix_allowed_symbols": [runtime.settings.default_symbol],
                    "matrix_allowed_regimes": ["range"],
                    "matrix_allowed_profiles": ["range_defensive"],
                    "reason": "persist_policy",
                },
            )
            fetched = client.get("/strategy-profiles/auto-rollback-policy")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["policy_status"], "settings_fallback")
        self.assertFalse(fetched.json()["enabled"])

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            approved = client.post(
                "/strategy-profiles/auto-rollback-policy/approve",
                json={"reason": "approve_policy"},
            )
            history = client.get("/strategy-profiles/auto-rollback-policy/history?limit=10&offset=0")
            frozen = client.post(
                "/strategy-profiles/auto-rollback-policy/freeze",
                json={"frozen": True, "reason": "freeze_policy"},
            )
            unfrozen = client.post(
                "/strategy-profiles/auto-rollback-policy/freeze",
                json={"frozen": False, "reason": "unfreeze_policy"},
            )
            fetched_after = client.get("/strategy-profiles/auto-rollback-policy")
            snapshot_after = client.get("/strategy-profiles")

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(unfrozen.status_code, 200)
        self.assertTrue(fetched_after.json()["enabled"])
        self.assertEqual(fetched_after.json()["matrix_allowed_profiles"], ["range_defensive"])
        self.assertEqual(history.json()["history"][0]["policy_status"], "approved")
        self.assertTrue(snapshot_after.json()["auto_rollback_policy"]["enabled"])
        self.assertEqual(snapshot_after.json()["auto_rollback_policy"]["matrix_allowed_regimes"], ["range"])
        self.assertFalse(snapshot_after.json()["auto_rollback_policy"]["frozen"])

    async def test_strategy_profile_winner_selection_policy_exposes_blocked_auto_activation_thresholds(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过公开接口暴露 activation policy 细节。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_activation_min_composite_score=9999.0,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        self.assertIn(
            "winner_composite_score_below_threshold",
            evaluated.json()["optimization_report"]["winner_selection_policy"]["auto_activation"]["blocked_reasons"],
        )

    async def test_strategy_profile_activation_policy_can_be_persisted_by_scope(self) -> None:
        self.skipTest("策略档位激活策略已从公开控制面移除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_activation_policy_enabled=False,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            updated = client.post(
                "/strategy-profiles/activation-policy",
                json={
                    "enabled": True,
                    "min_composite_score": 0.1,
                    "min_offline_replay_score": -0.5,
                    "min_recommendation_strength": 0.2,
                    "require_positive_replay_consensus": True,
                    "disallow_when_shadow_review_required": True,
                    "matrix_allowed_symbols": [runtime.settings.default_symbol],
                    "matrix_allowed_regimes": ["trend"],
                    "matrix_allowed_profiles": ["trend_normal"],
                    "reason": "persist_activation_policy",
                },
            )
            fetched = client.get("/strategy-profiles/activation-policy")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["policy_status"], "settings_fallback")
        self.assertFalse(fetched.json()["enabled"])

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            approved = client.post(
                "/strategy-profiles/activation-policy/approve",
                json={"reason": "approve_activation_policy"},
            )
            history = client.get("/strategy-profiles/activation-policy/history?limit=10&offset=0")
            frozen = client.post(
                "/strategy-profiles/activation-policy/freeze",
                json={"frozen": True, "reason": "freeze_activation_policy"},
            )
            unfrozen = client.post(
                "/strategy-profiles/activation-policy/freeze",
                json={"frozen": False, "reason": "unfreeze_activation_policy"},
            )
            fetched_after = client.get("/strategy-profiles/activation-policy")
            snapshot_after = client.get("/strategy-profiles")

        self.assertEqual(approved.status_code, 200)
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(unfrozen.status_code, 200)
        self.assertTrue(fetched_after.json()["enabled"])
        self.assertEqual(fetched_after.json()["matrix_allowed_profiles"], ["trend_normal"])
        self.assertEqual(history.json()["history"][0]["policy_status"], "approved")
        self.assertTrue(snapshot_after.json()["activation_policy"]["enabled"])
        self.assertEqual(snapshot_after.json()["activation_policy"]["matrix_allowed_regimes"], ["trend"])
        self.assertFalse(snapshot_after.json()["activation_policy"]["frozen"])

    async def test_strategy_profile_activation_policy_can_execute_winner_auto_activation_independently(self) -> None:
        self.skipTest("策略档位激活策略已从公开控制面移除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_activation_policy_enabled=True,
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "auto_switch_enabled": False,
                }
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            client.post(
                "/strategy-profiles/activation-policy",
                json={
                    "enabled": True,
                    "min_composite_score": -999.0,
                    "min_offline_replay_score": -999.0,
                    "min_recommendation_strength": -999.0,
                    "require_positive_replay_consensus": False,
                    "disallow_when_shadow_review_required": False,
                    "matrix_allowed_symbols": [runtime.settings.default_symbol],
                    "matrix_allowed_regimes": ["trend", "breakout", "range", "high_volatility", "execution_degraded", "unknown"],
                    "matrix_allowed_profiles": ["trend_aggressive", "trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"],
                    "reason": "allow_independent_activation",
                },
            )
            client.post(
                "/strategy-profiles/activation-policy/approve",
                json={"reason": "approve_independent_activation"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertIn("profile_activation_policy", evaluated.json())
        self.assertEqual(evaluated.json()["profile_activation_policy"]["status"], "blocked")
        self.assertTrue(evaluated.json()["profile_activation_policy"]["blocked_reasons"])
        self.assertIn(
            evaluated.json()["selection_decision"]["decision_status"],
            {"winner_policy_recommended_not_executed", "execution_outcome_recorded"},
        )
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "range_defensive")

    async def test_strategy_profile_activation_policy_does_not_auto_activate_manual_only_profile(self) -> None:
        self.skipTest("策略档位激活策略已从公开控制面移除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_activation_policy_enabled=True,
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "auto_switch_enabled": False,
                }
            )
        )
        app = self._app(runtime)
        original_build_optimization_report = StrategyProfileControlService._build_optimization_report
        manual_only_recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="range_defensive",
            recommended_profile_id="range_defensive",
            confidence=0.4,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["keep_current_profile"],
            human_summary="keep the current conservative profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_manual_only_policy",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        def _force_aggressive_winner(self, *, state, comparison_report, evaluations, context_snapshot=None):
            report = original_build_optimization_report(
                self,
                state=state,
                comparison_report=comparison_report,
                evaluations=evaluations,
                context_snapshot=context_snapshot,
            )
            return report.model_copy(
                update={
                    "recommended_profile_id": "trend_aggressive",
                    "replay_summary": {
                        **(report.replay_summary or {}),
                        "target_symbol": runtime.settings.default_symbol,
                        "target_regime": "trend",
                    },
                    "winner_selection_policy": {
                        **(report.winner_selection_policy or {}),
                        "winner_profile_id": "trend_aggressive",
                        "auto_activation": {"blocked_reasons": []},
                    },
                }
            )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._build_optimization_report",
            new=_force_aggressive_winner,
        ), patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=manual_only_recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                client.post(
                    "/strategy-profiles/activation-policy",
                    json={
                        "enabled": True,
                        "min_composite_score": -999.0,
                        "min_offline_replay_score": -999.0,
                        "min_recommendation_strength": -999.0,
                        "require_positive_replay_consensus": False,
                        "disallow_when_shadow_review_required": False,
                        "matrix_allowed_symbols": [runtime.settings.default_symbol],
                        "matrix_allowed_regimes": ["trend", "breakout", "range", "high_volatility", "execution_degraded", "unknown"],
                        "matrix_allowed_profiles": ["trend_aggressive", "trend_normal", "trend_strict", "range_defensive", "high_volatility_defensive", "execution_degraded_safe"],
                        "reason": "allow_manual_only_candidate_for_policy_test",
                    },
                )
                client.post(
                    "/strategy-profiles/activation-policy/approve",
                    json={"reason": "approve_manual_only_candidate_for_policy_test"},
                )
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertIn("profile_activation_policy", evaluated.json())
        self.assertEqual(evaluated.json()["profile_activation_policy"]["status"], "blocked")
        self.assertIn(
            "strategy_profile_manual_approval_required",
            evaluated.json()["profile_activation_policy"]["blocked_reasons"],
        )
        self.assertIn(
            "strategy_profile_auto_switch_not_allowed",
            evaluated.json()["profile_activation_policy"]["blocked_reasons"],
        )
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "range_defensive")

    async def test_strategy_profile_auto_rollback_matrix_can_block_by_symbol(self) -> None:
        self.skipTest("自动回滚策略生命周期已从公开控制面移除。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_rollback_enabled=True,
            strategy_profile_auto_rollback_review_required_only=False,
            strategy_profile_auto_rollback_min_trade_count=1,
            strategy_profile_auto_rollback_cooldown_seconds=0.0,
            strategy_profile_auto_rollback_allowed_symbols=("ETH-USDT",),
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.85,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["manual_activation_for_matrix_block"],
            human_summary="activate defensive profile",
            risk_notes=[],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_matrix_block",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        runtime.execution_repo.save_fill(
            FillEvent(
                fill_id="fill_matrix_block",
                decision_id="decision_matrix_block",
                intent_id="intent_matrix_block",
                client_order_id="clord_matrix_block",
                exchange_order_id="ex_matrix_block",
                symbol=runtime.settings.default_symbol,
                venue="PAPER",
                side="buy",
                fill_qty=0.001,
                fill_price=100_000.0,
                fee_amount=1.0,
                fee_currency="USDT",
                liquidity_role="taker",
                exchange_timestamp=utc_now(),
                ingestion_timestamp=utc_now(),
                order_status_after_fill="FILLED",
            )
        )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={"safe_to_trade": False, "review_required": True}
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")

        self.assertEqual(evaluated.status_code, 200)
        self.assertNotIn("auto_rollback", evaluated.json())
        self.assertEqual(evaluated.json()["selection_decision"]["decision_status"], "auto_rollback_recommended")

    async def test_strategy_profile_auto_apply_is_blocked_when_review_required(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过页面驱动自动切换评估。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.recovery_status = runtime.recovery_status.model_copy(
            update={
                "safe_to_trade": False,
                "review_required": True,
                "resume_blocked_reasons": ["operator_rebaseline_required"],
            }
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")
            evaluations = client.get("/strategy-profiles/evaluations?limit=5&offset=0")

        self.assertEqual(evaluated.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(evaluations.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertIn("strategy_profile_review_required", evaluated.json()["validation"]["blocked_reasons"])
        self.assertIn("strategy_profile_runtime_not_safe_to_trade", evaluated.json()["validation"]["blocked_reasons"])
        self.assertGreaterEqual(evaluations.json()["total_available"], 1)
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_auto_apply_allows_same_risk_target_when_confidence_high(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过页面驱动自动切换评估。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="trend_strict",
            confidence=0.92,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["trend_signal_moderate"],
            human_summary="keep trend but tighten entries",
            risk_notes=["test_same_risk"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_trend_strict",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        original_build_optimization_report = StrategyProfileControlService._build_optimization_report

        def _force_strict_winner(self, *, state, comparison_report, evaluations, context_snapshot=None):
            report = original_build_optimization_report(
                self,
                state=state,
                comparison_report=comparison_report,
                evaluations=evaluations,
                context_snapshot=context_snapshot,
            )
            return report.model_copy(
                update={
                    "recommended_profile_id": "trend_strict",
                    "winner_selection_policy": {
                        **(report.winner_selection_policy or {}),
                        "winner_profile_id": "trend_strict",
                        "auto_activation": {"blocked_reasons": []},
                    },
                }
            )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._build_optimization_report",
            new=_force_strict_winner,
        ), patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertTrue(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated.json()["validation"]["transition_risk_direction"], "same_risk")
        self.assertEqual(
            evaluated.json()["auto_activation"]["status"],
            "winner_policy_auto_activation_executed",
        )
        self.assertEqual(
            evaluated.json()["auto_activation"]["activation_record"]["reason_code"],
            "winner_selection_policy_auto_activation",
        )
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_strict")

    async def test_strategy_profile_auto_apply_blocks_more_aggressive_target_when_confidence_too_low(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过页面驱动自动切换评估。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "previous_active_revision_id": state.active_revision_id,
                }
            )
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="range_defensive",
            recommended_profile_id="trend_normal",
            confidence=0.86,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["trend_recovery_detected"],
            human_summary="trend recovery supports normal profile",
            risk_notes=["test_more_aggressive"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_trend_normal_low_conf",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        original_build_optimization_report = StrategyProfileControlService._build_optimization_report

        def _force_normal_winner(self, *, state, comparison_report, evaluations, context_snapshot=None):
            report = original_build_optimization_report(
                self,
                state=state,
                comparison_report=comparison_report,
                evaluations=evaluations,
                context_snapshot=context_snapshot,
            )
            return report.model_copy(
                update={
                    "recommended_profile_id": "trend_normal",
                    "winner_selection_policy": {
                        **(report.winner_selection_policy or {}),
                        "winner_profile_id": "trend_normal",
                        "auto_activation": {"blocked_reasons": []},
                    },
                }
            )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._build_optimization_report",
            new=_force_normal_winner,
        ), patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated.json()["validation"]["transition_risk_direction"], "more_aggressive")
        self.assertIn(
            "strategy_profile_auto_switch_aggressive_confidence_too_low",
            evaluated.json()["validation"]["blocked_reasons"],
        )
        self.assertNotIn("auto_activation", evaluated.json())
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "range_defensive")

    async def test_strategy_profile_auto_apply_allows_more_aggressive_target_when_confidence_high(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过页面驱动自动切换评估。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        conservative = runtime.strategy_profile_repo.list_revisions(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            profile_id="range_defensive",
        )[0]
        state = runtime.strategy_profile_repo.activation_state(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
        )
        runtime.strategy_profile_repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": conservative.revision_id,
                    "active_profile_id": conservative.profile_id,
                    "previous_active_revision_id": state.active_revision_id,
                }
            )
        )
        app = self._app(runtime)
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="range_defensive",
            recommended_profile_id="trend_normal",
            confidence=0.93,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="trend",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["trend_recovery_detected", "fee_churn_normalized"],
            human_summary="trend regime recovered and supports normal posture",
            risk_notes=["test_more_aggressive_allowed"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest_trend_normal_high_conf",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )

        original_build_optimization_report = StrategyProfileControlService._build_optimization_report

        def _force_normal_winner(self, *, state, comparison_report, evaluations, context_snapshot=None):
            report = original_build_optimization_report(
                self,
                state=state,
                comparison_report=comparison_report,
                evaluations=evaluations,
                context_snapshot=context_snapshot,
            )
            return report.model_copy(
                update={
                    "recommended_profile_id": "trend_normal",
                    "winner_selection_policy": {
                        **(report.winner_selection_policy or {}),
                        "winner_profile_id": "trend_normal",
                        "auto_activation": {"blocked_reasons": []},
                    },
                }
            )

        with patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._build_optimization_report",
            new=_force_normal_winner,
        ), patch(
            "aats.services.operator.strategy_profiles.StrategyProfileControlService._generate_recommendation",
            new=AsyncMock(return_value=recommendation),
        ):
            with TestClient(app) as client:
                client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
                evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
                snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertTrue(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertEqual(evaluated.json()["validation"]["transition_risk_direction"], "more_aggressive")
        self.assertEqual(
            evaluated.json()["auto_activation"]["status"],
            "winner_policy_auto_activation_executed",
        )
        self.assertEqual(
            evaluated.json()["auto_activation"]["activation_record"]["reason_code"],
            "winner_selection_policy_auto_activation",
        )
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_auto_apply_is_blocked_when_open_orders_exist(self) -> None:
        self.skipTest("旧 evaluate-now 路由已删除，不再通过页面驱动自动切换评估。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_strategy_profile_auto",
                intent_id="intent_strategy_profile_auto",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_strategy_profile_auto",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            evaluated = client.post("/strategy-profiles/auto-tuning/evaluate-now")
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(evaluated.status_code, 200)
        self.assertFalse(evaluated.json()["validation"]["auto_apply_allowed"])
        self.assertIn("strategy_profile_open_orders_present", evaluated.json()["validation"]["blocked_reasons"])
        self.assertNotIn("auto_activation", evaluated.json())
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_normal")

    async def test_strategy_profile_accept_stage_activate_and_reject_are_audited(self) -> None:
        self.skipTest("旧 recommendation 审批流已删除，只保留管理员手动切档。")
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.82,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["range_regime_detected"],
            human_summary="range market",
            risk_notes=["manual_test"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            staged = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "stage_now", "activation_mode": "stage_only"},
            )
            activated = client.post(
                "/strategy-profiles/pending/activate",
                json={"reason": "activate_pending"},
            )

        self.assertEqual(staged.status_code, 200)
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(staged.json()["status"], "accepted_and_staged")
        self.assertEqual(activated.json()["active_revision"]["profile_id"], "range_defensive")
        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "staged_for_activation" for item in selection_decisions))
        self.assertTrue(any(item["decision_status"] == "pending_activation_executed" for item in selection_decisions))
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        accept_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_accept")
        activate_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_activate_pending")
        self.assertEqual(accept_action["status"], "accepted_and_staged")
        self.assertEqual(activate_action["status"], "pending_profile_activated")

        second = recommendation.model_copy(
            update={
                "recommendation_id": "strp_rec_manual_reject",
                "recommended_profile_id": "trend_strict",
                "generated_at": utc_now(),
                "expires_at": utc_now() + timedelta(minutes=120),
            }
        )
        runtime.strategy_profile_repo.save_recommendation(second)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            rejected = client.post(
                f"/strategy-profiles/recommendations/{second.recommendation_id}/reject",
                json={"reason_code": "manual_reject", "reason_detail": "keep current profile"},
            )

        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["status"], "rejected")
        rejections = runtime.event_store.by_topic(topics.STRATEGY_PROFILE_REJECTIONS)
        self.assertEqual(len(rejections), 1)
        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "recommendation_rejected" for item in selection_decisions))

    async def test_admin_can_manually_activate_registered_strategy_profile(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                "/strategy-profiles/profiles/trend_strict/activate",
                json={"reason": "manual_switch_from_ui"},
            )
            snapshot = client.get("/strategy-profiles")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.json()["status"], "manually_activated")
        self.assertEqual(activated.json()["active_revision"]["profile_id"], "trend_strict")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["activation"]["active_profile_id"], "trend_strict")
        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "manual_profile_activation_executed" for item in selection_decisions))
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        activate_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_manual_activate")
        self.assertEqual(activate_action["status"], "profile_manually_activated")
        self.assertEqual(activate_action["details"]["requested_profile_id"], "trend_strict")
        self.assertEqual(activate_action["details"]["active_profile_id"], "trend_strict")

    async def test_admin_can_restore_strategy_profile_auto_switch_after_manual_activate(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            strategy_profile_auto_control_enabled=True,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            activated = client.post(
                "/strategy-profiles/profiles/trend_strict/activate",
                json={"reason": "manual_switch_then_restore_auto"},
            )
            snapshot_after_activate = client.get("/strategy-profiles")
            restored = client.post(
                "/strategy-profiles/restore-auto",
                json={"reason": "manual_restore_auto_strategy_profile_control"},
            )
            snapshot_after_restore = client.get("/strategy-profiles")

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(snapshot_after_activate.status_code, 200)
        self.assertIsNotNone(snapshot_after_activate.json()["activation"]["frozen_until"])

        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["status"], "auto_restored")
        self.assertEqual(snapshot_after_restore.status_code, 200)
        self.assertIsNone(snapshot_after_restore.json()["activation"]["frozen_until"])
        self.assertEqual(snapshot_after_restore.json()["activation"]["active_profile_id"], "trend_strict")

        selection_decisions = [item.payload for item in runtime.event_store.by_topic(topics.STRATEGY_PROFILE_SELECTION_DECISIONS)]
        self.assertTrue(any(item["decision_status"] == "manual_profile_auto_switch_restored" for item in selection_decisions))
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        restore_action = next(item for item in reversed(actions) if item["action"] == "strategy_profile_restore_auto")
        self.assertEqual(restore_action["status"], "profile_auto_switch_restored")
        self.assertEqual(restore_action["details"]["active_profile_id"], "trend_strict")
        self.assertFalse(restore_action["details"]["frozen_by_admin_override"])

    async def test_restore_strategy_profile_auto_is_rejected_when_auto_control_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
            ai_operating_mode="baseline_only",
            strategy_profile_auto_control_enabled=False,
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            restored = client.post(
                "/strategy-profiles/restore-auto",
                json={"reason": "manual_restore_auto_strategy_profile_control"},
            )

        self.assertEqual(restored.status_code, 409)
        self.assertEqual(restored.json()["detail"], "strategy_profile_auto_control_not_enabled")

    async def test_strategy_profile_activation_is_blocked_when_open_orders_exist(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_strategy_profile",
                intent_id="intent_strategy_profile",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_strategy_profile",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            blocked = client.post(
                "/strategy-profiles/profiles/trend_strict/activate",
                json={"reason": "manual_switch_with_open_orders"},
            )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"], "strategy_profile_open_orders_present")
        return

        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass")],
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_strategy_profile",
                intent_id="intent_strategy_profile",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_strategy_profile",
                status="SUBMITTED",
                requested_qty=0.001,
                remaining_qty=0.001,
            )
        )
        recommendation = StrategyProfileRecommendation(
            product_type=runtime.settings.trading_product_type,
            margin_mode=runtime.settings.margin_mode,
            allowed_symbols=runtime.settings.allowed_symbols,
            active_profile_id="trend_normal",
            recommended_profile_id="range_defensive",
            confidence=0.82,
            market_regime_assessment=StrategyProfileMarketRegimeAssessment(
                regime="range",
                volatility_state="medium",
                execution_condition="normal",
            ),
            reason_codes=["range_regime_detected"],
            human_summary="range market",
            risk_notes=["manual_test"],
            valid_for_minutes=120,
            generated_by="test",
            input_digest="digest2",
            input_snapshot={"source": "test"},
            expires_at=utc_now() + timedelta(minutes=120),
        )
        runtime.strategy_profile_repo.save_recommendation(recommendation)
        app = self._app(runtime)

        with TestClient(app) as client:
            client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            blocked = client.post(
                f"/strategy-profiles/recommendations/{recommendation.recommendation_id}/accept",
                json={"reason": "activate_now", "activation_mode": "manual_now"},
            )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"], "strategy_profile_open_orders_present")

    async def test_operator_user_management_requires_admin_and_preserves_last_admin(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass"), ("operator", "operator-pass")],
            operator_write_api_key="write-key",
        )
        app = self._app(runtime)

        with TestClient(app) as operator_client:
            login = operator_client.post("/auth/login", json={"username": "operator", "password": "operator-pass"})
            denied = operator_client.get("/auth/users")

        with TestClient(app) as admin_client:
            admin_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self_disable = admin_client.patch("/auth/users/admin", json={"enabled": False})
            self_delete = admin_client.delete("/auth/users/admin")

        with TestClient(app) as api_key_client:
            write_allowed = api_key_client.get("/auth/users", headers={"X-AATS-API-Key": "write-key"})
            last_admin_disable = api_key_client.patch(
                "/auth/users/admin",
                json={"enabled": False},
                headers={"X-AATS-API-Key": "write-key"},
            )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_admin_access_required")
        self.assertEqual(self_disable.status_code, 409)
        self.assertEqual(self_disable.json()["detail"], "operator_self_disable_forbidden")
        self.assertEqual(self_delete.status_code, 409)
        self.assertEqual(self_delete.json()["detail"], "operator_self_delete_forbidden")
        self.assertEqual(write_allowed.status_code, 200)
        self.assertEqual(last_admin_disable.status_code, 409)
        self.assertEqual(last_admin_disable.json()["detail"], "operator_last_admin_required")

    async def test_session_is_revoked_immediately_when_database_user_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
            operator_users=[("admin", "admin-pass"), ("operator", "operator-pass")],
            operator_write_api_key="write-key",
        )
        operator_user = runtime.operator_repo.get_by_username("operator")
        self.assertIsNotNone(operator_user)
        runtime.operator_repo.save_user(operator_user.model_copy(update={"role": "admin"}))

        app = self._app(runtime)
        with TestClient(app) as session_client:
            login = session_client.post("/auth/login", json={"username": "admin", "password": "admin-pass"})
            self.assertEqual(login.status_code, 200)
            before_disable = session_client.get("/auth/whoami")
            with TestClient(app) as api_key_client:
                disabled = api_key_client.patch(
                    "/auth/users/admin",
                    json={"enabled": False},
                    headers={"X-AATS-API-Key": "write-key"},
                )
            after_disable = session_client.get("/auth/whoami")

        self.assertEqual(before_disable.status_code, 200)
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(after_disable.status_code, 401)
        self.assertEqual(after_disable.json()["detail"], "operator_auth_required")

    async def test_operator_write_is_denied_without_auth_by_default(self) -> None:
        runtime = await self._runtime(operator_unsafe_write_without_auth=False)
        app = self._app(runtime)
        with TestClient(app) as client:
            denied = client.post("/system/halt", json={"reason": "unauthenticated_write"})

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "operator_write_auth_required")

    async def test_unauthenticated_session_remains_anonymous_when_browser_auth_is_disabled(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=False,
            operator_users=[("admin", "solo-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            session = client.get("/auth/session")

        self.assertEqual(session.status_code, 200)
        payload = session.json()
        self.assertFalse(payload["authenticated"])
        self.assertIsNone(payload["identity"])
        self.assertEqual(payload["role"], "anonymous")
        self.assertEqual(payload["auth_source"], "anonymous")

    async def test_auth_providers_no_longer_expose_runtime_bootstrap_state(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=True,
            operator_session_secret="session-secret",
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            providers = client.get("/auth/providers")

        self.assertEqual(providers.status_code, 200)
        payload = providers.json()
        self.assertEqual(payload["configured_roles"], [])
        self.assertFalse(payload["runtime_profile_control_enabled"])
        self.assertNotIn("bootstrap_pending", payload)

    async def test_unauthenticated_session_is_anonymous_without_stored_operator_users(self) -> None:
        runtime = await self._runtime(
            operator_auth_enabled=False,
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            session = client.get("/auth/session")

        self.assertEqual(session.status_code, 200)
        payload = session.json()
        self.assertIsNone(payload["identity"])
        self.assertEqual(payload["role"], "anonymous")
        self.assertEqual(payload["auth_source"], "anonymous")

    async def test_postgres_backed_operator_account_persists_and_allows_login(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            seed_settings = AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "postgres",
                    "database_url": database_url,
                    "database_auto_create_schema": True,
                    "event_persistence_mode": "strict",
                    "enabled_decision_timeframes": ("15m",),
                    "operator_auth_enabled": False,
                }
            )
            runtime = await build_runtime(seed_settings)
            runtime.operator_repo.save_user(
                OperatorUserRecord(
                    username="admin",
                    password_hash=hash_password("correct-pass"),
                    role="admin",
                )
            )
            await runtime.market_gateway.run_local_publisher(
                symbol=seed_settings.default_symbol,
                iterations=2,
                interval_seconds=0.0,
            )
            self.assertEqual(runtime.operator_repo.count(), 1)
            if runtime.database_runtime is not None:
                runtime.database_runtime.dispose()

            settings = seed_settings.model_copy(
                update={
                    "operator_auth_enabled": True,
                    "operator_session_secret": "session-secret",
                }
            )
            recovered_runtime = await build_runtime(settings)
            app = self._app(recovered_runtime)
            with TestClient(app) as client:
                providers = client.get("/auth/providers")
                login = client.post("/auth/login", json={"username": "admin", "password": "correct-pass"})

            self.assertEqual(providers.status_code, 200)
            self.assertEqual(providers.json()["stored_user_count"], 1)
            self.assertFalse(providers.json()["runtime_profile_control_enabled"])
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.json()["identity"], "admin")
            if recovered_runtime.database_runtime is not None:
                recovered_runtime.database_runtime.dispose()

    async def test_database_backed_session_auth_requires_preexisting_admin_user(self) -> None:
        with temporary_postgres_url() as (database_url, _admin_engine, _schema_name):
            settings = AATSSettings.model_validate(
                {
                    "config_profile": "local_demo",
                    "mode": "paper_live",
                    "market_data_backend": "demo",
                    "execution_backend": "paper",
                    "account_backend": "disabled",
                    "account_read_enabled": False,
                    "storage_mode": "postgres",
                    "database_url": database_url,
                    "database_auto_create_schema": True,
                    "event_persistence_mode": "strict",
                    "enabled_decision_timeframes": ("15m",),
                    "operator_auth_enabled": True,
                    "operator_session_secret": "session-secret",
                }
            )

            with self.assertRaisesRegex(ValueError, "operator_session_auth_requires_enabled_admin_user"):
                await build_runtime(settings)

    async def test_mode_hot_swap_is_rejected_and_cancel_is_operator_audited(self) -> None:
        runtime = await self._runtime(
            operator_users=[("admin", "solo-pass")],
        )
        app = self._app(runtime)
        with TestClient(app) as client:
            mode_change = client.post("/system/mode", json={"mode": "guarded_live"})
            latest_order_id = client.get("/orders/latest").json()["order"]["client_order_id"]
            cancel = client.post(
                f"/orders/{latest_order_id}/cancel",
                json={"reason": "ui_cancel_test"},
            )

        self.assertEqual(mode_change.status_code, 409)
        self.assertEqual(
            mode_change.json()["detail"],
            "runtime_mode_hot_swap_not_supported_restart_required",
        )
        self.assertEqual(cancel.status_code, 200)
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        cancel_action = next(item for item in reversed(actions) if item["action"] == "cancel_order")
        self.assertIsNone(cancel_action["actor_identity"])
        self.assertEqual(cancel_action["actor_role"], "anonymous")
        self.assertEqual(cancel_action["auth_source"], "anonymous")
        self.assertEqual(cancel_action["reason"], "ui_cancel_test")
        self.assertEqual(cancel_action["order_id"], latest_order_id)

    async def test_stuck_submission_can_be_resolved_after_restart_when_exchange_confirms_absence(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        stale_ts = utc_now() - timedelta(minutes=10)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_stuck",
                intent_id="intent_restart_stuck",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_stuck",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=stale_ts,
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_stuck/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )
            detail = client.get("/orders/cl_restart_stuck")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["order"]["status"], "FAILED")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()["stuck_submission_resolution"]["eligible"])
        actions = [item.payload for item in runtime.event_store.by_topic(topics.OPERATOR_ACTIONS)]
        resolution_action = next(item for item in reversed(actions) if item["action"] == "resolve_stuck_submission")
        self.assertEqual(resolution_action["reason"], "ui_resolve_stuck_submission")
        self.assertEqual(resolution_action["order_id"], "cl_restart_stuck")

    async def test_stuck_submission_resolution_updates_audit_record_when_decision_audit_exists(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        stale_ts = utc_now() - timedelta(minutes=10)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_stuck_audit",
                intent_id="intent_restart_stuck_audit",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_stuck_audit",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=stale_ts,
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.audit_repo.upsert(
            DecisionAuditRecord(
                decision_id="decision_restart_stuck_audit",
                decision_context_ref="evt_decision_context_seed",
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_stuck_audit/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )

        self.assertEqual(response.status_code, 200)
        audit = runtime.audit_repo.get("decision_restart_stuck_audit")
        self.assertIsNotNone(audit)
        self.assertEqual(len(audit.order_state_refs), 1)
        order_update = runtime.event_store.get(audit.order_state_refs[0])
        self.assertIsNotNone(order_update)
        self.assertEqual(order_update.payload["status"], "FAILED")

    async def test_stuck_submission_resolution_is_rejected_when_exchange_still_has_order(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[
                ExchangeOpenOrder(
                    instrument_id="BTC-USDT",
                    client_order_id="cl_restart_live",
                    exchange_order_id="ord_live",
                    side="buy",
                    order_type="market",
                    status="live",
                    quantity=0.1,
                )
            ],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_live",
                intent_id="intent_restart_live",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_live",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=10),
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_live/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "stuck_submission_resolution_blocked:exchange_order_still_open",
        )

    async def test_stuck_submission_resolution_is_rejected_when_private_ws_saw_order(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": False,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cross",
        )
        FakeOperatorAccountService.PRIVATE_ORDER_ROW = {
            "instId": settings.default_symbol,
            "ordId": "ord_ws_seen",
            "clOrdId": "cl_restart_ws_seen",
            "state": "live",
            "side": "buy",
            "ordType": "limit",
            "sz": "0.1",
            "accFillSz": "0",
            "uTime": "1700000002000",
            "cTime": "1700000001000",
        }
        FakeOperatorAccountService.PRIVATE_ORDER_FILLS = []
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_restart_ws_seen",
                intent_id="intent_restart_ws_seen",
                symbol=settings.default_symbol,
                client_order_id="cl_restart_ws_seen",
                venue="OKX",
                exchange_order_id=None,
                status="SUBMITTING",
                submission_mode="local_order_manager",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=10),
                requested_qty=0.1,
                filled_qty=0.0,
                remaining_qty=0.1,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post(
                "/orders/cl_restart_ws_seen/resolve-stuck-submission",
                json={"reason": "ui_resolve_stuck_submission"},
            )
            detail = client.get("/orders/cl_restart_ws_seen").json()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "stuck_submission_resolution_blocked:exchange_order_seen_via_private_ws",
        )
        self.assertTrue(detail["stuck_submission_resolution"]["private_ws_order_present"])
        FakeOperatorAccountService.PRIVATE_ORDER_ROW = None
        FakeOperatorAccountService.PRIVATE_ORDER_FILLS = None

    async def test_orders_recent_returns_latest_orders_first(self) -> None:
        runtime = await self._runtime()
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_old_recent",
                intent_id="intent_old_recent",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_old_recent",
                venue="PAPER",
                status="FAILED",
                submission_mode="paper_local",
                submitted_ts=None,
                last_update_ts=utc_now() - timedelta(minutes=5),
                requested_qty=1.0,
                filled_qty=0.0,
                remaining_qty=1.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        runtime.execution_repo.save_order_state(
            OrderState(
                decision_id="decision_new_recent",
                intent_id="intent_new_recent",
                symbol=runtime.settings.default_symbol,
                client_order_id="order_new_recent",
                venue="PAPER",
                status="SUBMITTING",
                submission_mode="paper_local",
                submitted_ts=None,
                last_update_ts=utc_now(),
                requested_qty=1.0,
                filled_qty=0.0,
                remaining_qty=1.0,
                product_type="spot",
                margin_mode="cash",
                submission_payload={},
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recent = client.get("/orders/recent?limit=1")
            full_list = client.get("/orders/recent?limit=200").json()
            order_ids = [item["client_order_id"] for item in full_list["orders"]]
            old_index = order_ids.index("order_old_recent")
            next_page = client.get(f"/orders/recent?limit=1&offset={old_index}")

        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.json()["orders"][0]["client_order_id"], "order_new_recent")
        self.assertEqual(recent.json()["offset"], 0)
        self.assertEqual(recent.json()["limit"], 1)
        self.assertTrue(recent.json()["has_more"])
        self.assertLess(order_ids.index("order_new_recent"), order_ids.index("order_old_recent"))
        self.assertEqual(next_page.status_code, 200)
        self.assertEqual(next_page.json()["orders"][0]["client_order_id"], "order_old_recent")
        self.assertEqual(next_page.json()["offset"], old_index)

    async def test_profile_control_summary_report_exposes_cold_start_and_safety_state(self) -> None:
        runtime = await self._runtime()
        await runtime.decision_engine.strategy_profile_service.evaluate_now(allow_auto_activation=False)
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.get("/reports/profile-control-summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("control_summary", payload)
        self.assertIn("evidence", payload["control_summary"])
        self.assertIn("cold_start_active", payload["control_summary"]["evidence"])
        self.assertIn("safety_profile_required", payload["control_summary"])
        self.assertIn("adaptive_controls", payload["control_summary"])
        self.assertIn("risk_budget", payload["control_summary"]["adaptive_controls"])
        self.assertIn("execution_aggressiveness", payload["control_summary"]["adaptive_controls"])
        self.assertIn("active_profile_id", payload["activation"])
        self.assertIn("latest_selection_decision", payload)
        self.assertIn("transition_class", payload["latest_selection_decision"])
        self.assertIn("gating_state", payload["latest_selection_decision"])
        self.assertIn("operator_summary", payload["latest_selection_decision"])

    async def test_execution_latest_uses_normalized_recovery_view(self) -> None:
        runtime = await self._runtime()
        runtime.kill_switch.halt(reason="operator_test_halt")
        runtime.recovery_status = runtime.recovery_status.model_copy(update={"recovery_state": "normal_operation"})
        app = self._app(runtime)

        with TestClient(app) as client:
            execution = client.get("/execution/latest")
            recovery = client.get("/system/recovery")

        self.assertEqual(execution.status_code, 200)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(
            execution.json()["recovery"]["recovery_state"],
            recovery.json()["recovery"]["recovery_state"],
        )
        self.assertEqual(execution.json()["recovery"]["recovery_state"], "manually_halted")

    async def test_execution_errors_hide_stale_failures_from_previous_runtime(self) -> None:
        runtime = await self._runtime()
        stale_error = ExecutionErrorSummary(
            subsystem="execution_engine",
            severity="error",
            message="stale_failure",
            decision_id="decision_old",
            order_id="order_old",
            status="FAILED",
            observed_at=utc_now() - timedelta(hours=2),
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key=runtime.settings.default_symbol,
                payload_model=stale_error,
                source_component="execution_engine",
            )
        )
        runtime.started_at = utc_now()
        app = self._app(runtime)

        with TestClient(app) as client:
            execution_errors = client.get("/execution/errors")

        self.assertEqual(execution_errors.status_code, 200)
        self.assertEqual(execution_errors.json()["errors"], [])

    async def test_operator_histories_are_persisted_for_blockers_and_replay(self) -> None:
        runtime = await self._runtime()
        app = self._app(runtime)
        with TestClient(app) as client:
            client.get("/system/health")
            blockers = client.get("/system/blockers").json()
            decision_id = client.get("/decision/latest").json()["decision_id"]
            replay_validation = client.post(f"/replay/validate/{decision_id}").json()
            replay_recent = client.get("/replay/recent-validations").json()

        self.assertTrue(blockers["recent_history"])
        self.assertEqual(replay_validation["decision_id"], decision_id)
        self.assertTrue(replay_recent["validations"])

    async def test_system_recovery_and_rebaseline_endpoints_expose_operator_recovery_flow(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )

        app = self._app(runtime)
        with TestClient(app) as client:
            recovery_before = client.get("/system/recovery").json()
            rebaseline = client.post("/system/rebaseline", json={"reason": "accept_exchange_state"}).json()
            recovery_after = client.get("/system/recovery").json()

        self.assertIn("recovery_state", recovery_before["recovery"])
        self.assertEqual(rebaseline["status"], "normal_operation")
        self.assertEqual(rebaseline["rebaseline_status"], "rebaseline_completed")
        self.assertIn("auto_resume", rebaseline)
        self.assertIsNotNone(rebaseline["auto_resume"])
        self.assertEqual(recovery_after["recovery"]["recovery_state"], "normal_operation")
        self.assertTrue(recovery_after["recovery"]["resume_eligible"])
        self.assertTrue(recovery_after["recovery"]["safe_to_trade"])
        self.assertIsNotNone(recovery_after["recovery"]["last_rebaseline_action"])

    async def test_recovery_view_uses_latest_account_baseline_for_current_scope(self) -> None:
        runtime = await self._runtime()
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["BTC-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["ETH-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            recovery = client.get("/system/recovery").json()

        self.assertEqual(recovery["latest_account_baseline"]["allowed_symbols"], ["BTC-USDT"])

    async def test_rebaseline_uses_previous_baseline_from_current_scope(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "guarded_simulated_submit_dry_run",
                "mode": "guarded_live",
                "market_data_backend": "demo",
                "execution_backend": "okx",
                "account_backend": "okx",
                "account_read_enabled": True,
                "okx_simulated_trading": True,
                "live_submit_enabled": False,
                "guarded_execution_dry_run": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)

        initial_baseline = runtime.event_store.latest(topics.ACCOUNT_BASELINES)
        self.assertIsNotNone(initial_baseline)
        runtime.event_store.append(
            build_envelope(
                topic=topics.ACCOUNT_BASELINES,
                key="okx",
                payload_model=AccountBaselineSnapshot(
                    account_source="okx",
                    exchange_snapshot_ts=utc_now(),
                    imported_at=utc_now(),
                    product_type="spot",
                    margin_mode="cash",
                    allowed_symbols=["ETH-USDT"],
                    baseline_status="baseline_imported",
                ),
                source_component="test",
            )
        )
        app = self._app(runtime)

        with TestClient(app) as client:
            response = client.post("/system/rebaseline", json={"reason": "accept_exchange_state"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["baseline"]["previous_baseline_ref"],
            initial_baseline.event_id,
        )

    async def test_system_rebaseline_is_rejected_for_paper_local_profile(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "okx",
                "account_read_enabled": True,
                "bootstrap_portfolio_from_exchange": True,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "operator_unsafe_write_without_auth": True,
            }
        )
        FakeOperatorAccountService.SNAPSHOT = ExchangeAccountSnapshot(
            account_source="okx",
            fetched_at=utc_now(),
            balances=[ExchangeBalance(currency="USDT", total=1000.0, available=1000.0, frozen=0.0)],
            positions=[],
            open_orders=[],
            fills=[],
            instruments=[],
            account_mode="cash",
        )
        with patch("aats.bootstrap.config.OKXAccountService", FakeOperatorAccountService):
            runtime = await build_runtime(settings)

        app = self._app(runtime)
        with TestClient(app) as client:
            response = client.post("/system/rebaseline", json={"reason": "paper_profile_rebaseline"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "rebaseline_not_supported_for_runtime_profile")

    async def _runtime(self, operator_users: list[tuple[str, str]] | None = None, **overrides):
        settings = AATSSettings.model_validate(
            {
                "config_profile": "local_demo",
                "mode": "paper_live",
                "market_data_backend": "demo",
                "execution_backend": "paper",
                "account_backend": "disabled",
                "account_read_enabled": False,
                "storage_mode": "memory",
                "event_persistence_mode": "strict",
                "enabled_decision_timeframes": ("15m",),
                "operator_unsafe_write_without_auth": True,
                **overrides,
            }
        )
        runtime = await build_runtime(settings)
        for username, password in operator_users or []:
            role = "admin" if username == "admin" else "operator" if username == "operator" else "viewer"
            runtime.operator_repo.save_user(
                OperatorUserRecord(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=4,
            interval_seconds=0.0,
        )
        return runtime

    @staticmethod
    def _app(runtime) -> FastAPI:
        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(router)
        app.state.runtime = runtime
        return app


if __name__ == "__main__":
    unittest.main()
