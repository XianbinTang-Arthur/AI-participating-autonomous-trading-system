from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aats.bootstrap.config import build_runtime
from aats.bootstrap.settings import AATSSettings
from aats.storage.session import (
    applied_migrations,
    apply_current_migrations,
    create_database_runtime,
    validate_runtime_schema,
)


@unittest.skipUnless(os.getenv("AATS_DATABASE_URL"), "AATS_DATABASE_URL is required for Postgres integration tests")
class TestLegacyPostgresUpgradePath(unittest.IsolatedAsyncioTestCase):
    async def test_current_migrations_upgrade_legacy_schema_in_place(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            self._apply_legacy_schema(runtime)
            self._apply_current_migrations(runtime)
            validate_runtime_schema(runtime)

            with runtime.engine.begin() as connection:
                order_state = connection.execute(
                    text(
                        """
                        SELECT
                            decision_id,
                            symbol,
                            product_type,
                            margin_mode,
                            position_intent,
                            td_mode,
                            position_mode,
                            pos_side,
                            instrument_family,
                            settle_currency
                        FROM order_states
                        WHERE client_order_id = 'legacy_order_1'
                        """
                    )
                ).mappings().one()
                self.assertEqual(order_state["decision_id"], "legacy_decision_1")
                self.assertEqual(order_state["symbol"], "BTC-USDT")
                self.assertEqual(order_state["product_type"], "spot")
                self.assertEqual(order_state["margin_mode"], "cash")
                self.assertEqual(order_state["position_intent"], "open_long")
                self.assertEqual(order_state["td_mode"], "cash")
                self.assertEqual(order_state["position_mode"], "net_mode")
                self.assertEqual(order_state["pos_side"], "net")
                self.assertEqual(order_state["instrument_family"], "BTC-USDT")
                self.assertEqual(order_state["settle_currency"], "USDT")

                audit = connection.execute(
                    text(
                        """
                        SELECT execution_plan_ref, order_state_refs::text AS order_state_refs_text
                        FROM decision_audit_records
                        WHERE decision_id = 'legacy_decision_1'
                        """
                    )
                ).mappings().one()
                self.assertEqual(audit["execution_plan_ref"], "evt_plan_1")
                self.assertIn("evt_order_state_1", audit["order_state_refs_text"])

                snapshot = connection.execute(
                    text(
                        """
                        SELECT product_type, margin_mode, primary_symbol
                        FROM portfolio_snapshots
                        LIMIT 1
                        """
                    )
                ).mappings().one()
                self.assertEqual(snapshot["product_type"], "spot")
                self.assertEqual(snapshot["margin_mode"], "cash")
                self.assertEqual(snapshot["primary_symbol"], "BTC-USDT")

                rec = connection.execute(
                    text(
                        """
                        SELECT decision_id, product_type, margin_mode, primary_symbol
                        FROM reconciliation_reports
                        WHERE reconciliation_id = 'legacy_recon_1'
                        """
                    )
                ).mappings().one()
                self.assertEqual(rec["decision_id"], "legacy_decision_1")
                self.assertEqual(rec["product_type"], "spot")
                self.assertEqual(rec["margin_mode"], "cash")
                self.assertEqual(rec["primary_symbol"], "BTC-USDT")

                widths = connection.execute(
                    text(
                        """
                        SELECT table_name, character_maximum_length
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND column_name = 'state'
                          AND table_name IN ('reservations', 'settlements')
                        ORDER BY table_name
                        """
                    )
                ).mappings().all()
                self.assertEqual(
                    {(row["table_name"], row["character_maximum_length"]) for row in widths},
                    {("reservations", 32), ("settlements", 32)},
                )

                connection.execute(text("DELETE FROM position_lots WHERE lot_id = 'legacy_lot_1'"))
                remaining_lot_events = connection.execute(
                    text("SELECT count(*) FROM lot_events WHERE event_id = 'legacy_lot_event_1'")
                ).scalar_one()
                self.assertEqual(remaining_lot_events, 0)
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_current_migrations_are_versioned_and_not_reapplied(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        try:
            self._apply_legacy_schema(runtime)

            first_applied = self._apply_current_migrations(runtime)
            second_applied = self._apply_current_migrations(runtime)
            versions = applied_migrations(runtime)

            self.assertEqual(
                first_applied,
                [
                    "0001_postgres_latest_schema.sql",
                    "0002_postgres_legacy_upgrade.sql",
                    "0003_postgres_execution_attempt_id_columns.sql",
                    "0004_postgres_exit_execution_repository.sql",
                    "0005_postgres_strategy_execution_bundle_row_version.sql",
                ],
            )
            self.assertEqual(second_applied, [])
            self.assertEqual(
                versions,
                [
                    "0001_postgres_latest_schema.sql",
                    "0002_postgres_legacy_upgrade.sql",
                    "0003_postgres_execution_attempt_id_columns.sql",
                    "0004_postgres_exit_execution_repository.sql",
                    "0005_postgres_strategy_execution_bundle_row_version.sql",
                ],
            )
        finally:
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_runtime_builds_against_upgraded_legacy_schema(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        app_runtime = None
        try:
            self._apply_legacy_schema(runtime)
            self._apply_current_migrations(runtime)
            app_runtime = await build_runtime(
                AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "paper_live",
                        "market_data_backend": "demo",
                        "execution_backend": "paper",
                        "account_backend": "disabled",
                        "account_read_enabled": False,
                        "storage_mode": "postgres",
                        "database_url": runtime.engine.url.render_as_string(hide_password=False),
                        "database_auto_create_schema": False,
                        "database_single_runtime_guard_enabled": False,
                        "event_persistence_mode": "strict",
                    }
                )
            )
            self.assertIsNotNone(app_runtime.execution_repo)
            self.assertIsNotNone(app_runtime.event_store)
            self.assertIsNotNone(app_runtime.audit_repo)
        finally:
            if app_runtime is not None and app_runtime.database_runtime is not None:
                app_runtime.database_runtime.dispose()
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    async def test_runtime_build_auto_applies_current_migrations_to_legacy_schema(self) -> None:
        runtime, admin_engine, schema_name = self._schema_runtime()
        app_runtime = None
        try:
            self._apply_legacy_schema(runtime)
            app_runtime = await build_runtime(
                AATSSettings.model_validate(
                    {
                        "config_profile": "local_demo",
                        "mode": "paper_live",
                        "market_data_backend": "demo",
                        "execution_backend": "paper",
                        "account_backend": "disabled",
                        "account_read_enabled": False,
                        "storage_mode": "postgres",
                        "database_url": runtime.engine.url.render_as_string(hide_password=False),
                        "database_auto_create_schema": False,
                        "database_single_runtime_guard_enabled": False,
                        "event_persistence_mode": "strict",
                    }
                )
            )
            self.assertIsNotNone(app_runtime.execution_repo)
            with runtime.engine.begin() as connection:
                columns = connection.execute(
                    text(
                        """
                        SELECT table_name, column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND (
                                (table_name = 'fill_events' AND column_name IN ('strategy_family', 'strategy_sleeve_id', 'allocation_id'))
                             OR (table_name = 'execution_orders' AND column_name = 'execution_attempt_id')
                             OR (table_name = 'execution_fills' AND column_name = 'execution_attempt_id')
                             OR (table_name = 'fill_outcomes' AND column_name = 'execution_attempt_id')
                          )
                        ORDER BY table_name, column_name
                        """
                    )
                ).mappings().all()
            self.assertEqual(
                [(row["table_name"], row["column_name"]) for row in columns],
                [
                    ("execution_fills", "execution_attempt_id"),
                    ("execution_orders", "execution_attempt_id"),
                    ("fill_events", "allocation_id"),
                    ("fill_events", "strategy_family"),
                    ("fill_events", "strategy_sleeve_id"),
                    ("fill_outcomes", "execution_attempt_id"),
                ],
            )
        finally:
            if app_runtime is not None and app_runtime.database_runtime is not None:
                app_runtime.database_runtime.dispose()
            runtime.dispose()
            self._drop_schema(admin_engine, schema_name)

    @staticmethod
    def _schema_runtime():
        base_url = make_url(os.environ["AATS_DATABASE_URL"])
        schema_name = f"aats_test_legacy_upgrade_{os.urandom(4).hex()}"
        admin_engine = create_engine(base_url.render_as_string(hide_password=False), future=True)
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        query = dict(base_url.query)
        existing_options = query.get("options")
        search_path_option = f"-csearch_path={schema_name}"
        query["options"] = f"{existing_options} {search_path_option}".strip() if existing_options else search_path_option
        scoped_url = base_url.set(query=query).render_as_string(hide_password=False)
        runtime = create_database_runtime(scoped_url)
        return runtime, admin_engine, schema_name

    @staticmethod
    def _apply_current_migrations(runtime) -> list[str]:
        return apply_current_migrations(runtime)

    @staticmethod
    def _apply_legacy_schema(runtime) -> None:
        legacy_sql = """
        CREATE TABLE event_store (
            sequence_id BIGSERIAL PRIMARY KEY,
            event_id VARCHAR(64) NOT NULL UNIQUE,
            schema_version VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            event_type VARCHAR(128) NOT NULL,
            event_timestamp TIMESTAMPTZ NOT NULL,
            source_component VARCHAR(128) NOT NULL,
            topic VARCHAR(128) NOT NULL,
            event_key VARCHAR(128) NOT NULL,
            decision_id VARCHAR(64),
            payload JSONB NOT NULL
        );

        CREATE TABLE portfolio_snapshots (
            sequence_id BIGSERIAL PRIMARY KEY,
            snapshot_ts TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            total_equity NUMERIC(36, 18) NOT NULL,
            realized_pnl NUMERIC(36, 18) NOT NULL,
            unrealized_pnl NUMERIC(36, 18) NOT NULL,
            payload JSONB NOT NULL
        );

        CREATE TABLE order_states (
            client_order_id VARCHAR(64) PRIMARY KEY,
            intent_id VARCHAR(64) NOT NULL UNIQUE,
            exchange_order_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(64) NOT NULL,
            submitted_ts TIMESTAMPTZ,
            last_update_ts TIMESTAMPTZ,
            requested_qty NUMERIC(36, 18) NOT NULL,
            filled_qty NUMERIC(36, 18) NOT NULL,
            remaining_qty NUMERIC(36, 18) NOT NULL,
            average_fill_price NUMERIC(36, 18),
            fees NUMERIC(36, 18) NOT NULL,
            payload JSONB NOT NULL
        );

        CREATE TABLE fill_events (
            fill_id VARCHAR(64) PRIMARY KEY,
            decision_id VARCHAR(64) NOT NULL,
            intent_id VARCHAR(64) NOT NULL,
            client_order_id VARCHAR(64) NOT NULL,
            exchange_order_id VARCHAR(64) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            side VARCHAR(8) NOT NULL,
            fill_qty NUMERIC(36, 18) NOT NULL,
            fill_price NUMERIC(36, 18) NOT NULL,
            fee_amount NUMERIC(36, 18) NOT NULL,
            exchange_timestamp TIMESTAMPTZ NOT NULL,
            ingestion_timestamp TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL
        );

        CREATE TABLE reconciliation_reports (
            reconciliation_id VARCHAR(64) PRIMARY KEY,
            as_of_ts TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            severity VARCHAR(32) NOT NULL,
            halt_required BOOLEAN NOT NULL,
            payload JSONB NOT NULL
        );

        CREATE TABLE decision_audit_records (
            audit_revision_id BIGSERIAL PRIMARY KEY,
            decision_id VARCHAR(64) NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            decision_context_ref VARCHAR(64) NOT NULL,
            baseline_assessment_ref VARCHAR(64),
            ai_market_assessment_ref VARCHAR(64),
            ai_action_proposal_ref VARCHAR(64),
            position_target_ref VARCHAR(64),
            policy_decision_ref VARCHAR(64),
            risk_decision_ref VARCHAR(64),
            order_intent_refs JSONB NOT NULL,
            fill_event_refs JSONB NOT NULL,
            portfolio_delta_ref VARCHAR(64),
            reconciliation_refs JSONB NOT NULL,
            payload JSONB NOT NULL
        );

        CREATE TABLE reservations (
            reservation_id VARCHAR(64) PRIMARY KEY,
            order_id VARCHAR(64) NOT NULL,
            reserve_account_id VARCHAR(64) NOT NULL,
            reserved_amount NUMERIC(36, 18) NOT NULL,
            consumed_amount NUMERIC(36, 18) NOT NULL,
            released_amount NUMERIC(36, 18) NOT NULL,
            state VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE settlements (
            settlement_id VARCHAR(64) PRIMARY KEY,
            fill_id VARCHAR(64) NOT NULL,
            order_id VARCHAR(64) NOT NULL,
            journal_id VARCHAR(64),
            state VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            posted_at TIMESTAMPTZ
        );

        CREATE TABLE position_lots (
            lot_id VARCHAR(64) PRIMARY KEY,
            symbol VARCHAR(64) NOT NULL,
            product_type VARCHAR(16) NOT NULL,
            margin_mode VARCHAR(16) NOT NULL,
            signed_quantity_open NUMERIC(36, 18) NOT NULL,
            entry_price NUMERIC(36, 18) NOT NULL,
            source_fill_id VARCHAR(64) NOT NULL,
            target_leverage DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            exposure_side VARCHAR(16) NOT NULL DEFAULT 'flat',
            status VARCHAR(16) NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::JSONB
        );

        CREATE TABLE lot_events (
            event_id VARCHAR(64) PRIMARY KEY,
            fill_id VARCHAR(64) NOT NULL,
            lot_id VARCHAR(64) NOT NULL REFERENCES position_lots(lot_id),
            symbol VARCHAR(64) NOT NULL,
            event_type VARCHAR(16) NOT NULL,
            quantity NUMERIC(36, 18) NOT NULL,
            entry_price NUMERIC(36, 18) NOT NULL,
            exit_price NUMERIC(36, 18),
            realized_pnl_delta NUMERIC(36, 18) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::JSONB
        );

        INSERT INTO event_store (
            event_id, schema_version, created_at, event_type, event_timestamp, source_component, topic, event_key, decision_id, payload
        ) VALUES (
            'evt_legacy_1', '1.0', now(), 'market_snapshot', now(), 'legacy', 'MARKET_SNAPSHOTS', 'BTC-USDT', 'legacy_decision_1',
            '{"symbol":"BTC-USDT","timeframe":"15m","product_type":"spot","margin_mode":"cash"}'::jsonb
        );

        INSERT INTO portfolio_snapshots (
            snapshot_ts, created_at, total_equity, realized_pnl, unrealized_pnl, payload
        ) VALUES (
            now(), now(), 1000, 0, 0,
            '{"product_type":"spot","margin_mode":"cash","positions":[{"symbol":"BTC-USDT"}]}'::jsonb
        );

        INSERT INTO order_states (
            client_order_id, intent_id, exchange_order_id, created_at, status, submitted_ts, last_update_ts,
            requested_qty, filled_qty, remaining_qty, average_fill_price, fees, payload
        ) VALUES (
            'legacy_order_1', 'legacy_intent_1', 'legacy_exchange_1', now(), 'SUBMITTED', now(), now(),
            1, 0, 1, NULL, 0,
            '{"decision_id":"legacy_decision_1","symbol":"BTC-USDT","product_type":"spot","margin_mode":"cash","position_intent":"open_long","td_mode":"cash","position_mode":"net_mode","pos_side":"net","instrument_family":"BTC-USDT","settle_currency":"USDT","submission_payload":{"tdMode":"cash","positionMode":"net_mode","posSide":"net","instrumentFamily":"BTC-USDT","settleCurrency":"USDT"}}'::jsonb
        );

        INSERT INTO fill_events (
            fill_id, decision_id, intent_id, client_order_id, exchange_order_id, symbol, side, fill_qty, fill_price, fee_amount,
            exchange_timestamp, ingestion_timestamp, created_at, payload
        ) VALUES (
            'legacy_fill_1', 'legacy_decision_1', 'legacy_intent_1', 'legacy_order_1', 'legacy_exchange_1', 'BTC-USDT', 'buy', 1, 100, 0,
            now(), now(), now(),
            '{"product_type":"spot","margin_mode":"cash","position_intent":"open_long","td_mode":"cash","position_mode":"net_mode","pos_side":"net","instrument_family":"BTC-USDT","settle_currency":"USDT"}'::jsonb
        );

        INSERT INTO reconciliation_reports (
            reconciliation_id, as_of_ts, created_at, severity, halt_required, payload
        ) VALUES (
            'legacy_recon_1', now(), now(), 'info', false,
            '{"decision_id":"legacy_decision_1","product_type":"spot","margin_mode":"cash","allowed_symbols":["BTC-USDT"]}'::jsonb
        );

        INSERT INTO decision_audit_records (
            decision_id, updated_at, decision_context_ref, baseline_assessment_ref, ai_market_assessment_ref, ai_action_proposal_ref,
            position_target_ref, policy_decision_ref, risk_decision_ref, order_intent_refs, fill_event_refs, portfolio_delta_ref,
            reconciliation_refs, payload
        ) VALUES (
            'legacy_decision_1', now(), 'ctx_1', NULL, NULL, NULL, NULL, NULL, NULL,
            '["legacy_intent_evt_1"]'::jsonb, '["legacy_fill_1"]'::jsonb, NULL,
            '["legacy_recon_1"]'::jsonb,
            '{"execution_plan_ref":"evt_plan_1","order_state_refs":["evt_order_state_1"]}'::jsonb
        );

        INSERT INTO position_lots (
            lot_id, symbol, product_type, margin_mode, signed_quantity_open, entry_price, source_fill_id,
            target_leverage, exposure_side, status, opened_at, closed_at, updated_at, metadata
        ) VALUES (
            'legacy_lot_1', 'BTC-USDT', 'spot', 'cash', 1, 100, 'legacy_fill_1',
            1.0, 'long', 'OPEN', now(), NULL, now(), '{}'::jsonb
        );

        INSERT INTO lot_events (
            event_id, fill_id, lot_id, symbol, event_type, quantity, entry_price, exit_price, realized_pnl_delta, created_at, payload
        ) VALUES (
            'legacy_lot_event_1', 'legacy_fill_1', 'legacy_lot_1', 'BTC-USDT', 'open', 1, 100, NULL, 0, now(), '{}'::jsonb
        );
        """
        with runtime.engine.begin() as connection:
            raw_connection = connection.connection
            with raw_connection.cursor() as cursor:
                cursor.execute(legacy_sql)

    @staticmethod
    def _drop_schema(admin_engine, schema_name: str) -> None:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()
