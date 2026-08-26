"""FS-009 PostgreSQL migration/ledger integration contract.

Runs only with an explicitly enabled isolated Testcontainers database.  It
never reads a repository ``.env.*`` file or connects to an operator database.
"""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

try:
    from testcontainers.community.postgres import (  # type: ignore[import-not-found]
        PostgresContainer,
    )

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import psycopg2  # type: ignore[import-not-found]  # noqa: F401

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False


_SHOULD_RUN = (
    os.getenv("AATS_RUN_POSTGRES_INTEGRATION") == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


@unittest.skipUnless(
    _SHOULD_RUN,
    "need isolated docker + testcontainers + AATS_RUN_POSTGRES_INTEGRATION=1",
)
class Fs009SchemaMigrationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert PostgresContainer is not None
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

    @classmethod
    def tearDownClass(cls) -> None:
        from aats.data_platform.db import reset_engine

        reset_engine()
        cls.container.stop()

    def test_full_chain_is_idempotent_and_rollback_repair_is_ledgered(self) -> None:
        from aats.data_platform.config import ResearchPlatformSettings
        from aats.data_platform.db import (
            apply_rdp_migrations,
            get_engine,
            validate_rdp_schema,
        )
        from aats.data_platform.migrations._batch_b import (
            BATCH_B_STAGES,
            run_batch_b_rollback,
        )

        settings = ResearchPlatformSettings(
            database_url=self.container.get_connection_url(driver="psycopg2"),
            _env_file=None,
        )

        first = apply_rdp_migrations(settings)
        self.assertTrue(first.ok, first.error_message)
        self.assertEqual(
            [stage.stage for stage in first.stages],
            list(BATCH_B_STAGES),
        )
        self.assertTrue(all(stage.applied for stage in first.stages))

        second = apply_rdp_migrations(settings)
        self.assertTrue(second.ok, second.error_message)
        self.assertTrue(all(not stage.applied for stage in second.stages))
        validate_rdp_schema(settings)

        last_stage = BATCH_B_STAGES[-1]
        rollback = run_batch_b_rollback(
            get_engine(settings),
            stages=(last_stage,),
        )
        self.assertTrue(rollback.ok, rollback.error_message)
        with self.assertRaisesRegex(
            RuntimeError,
            r"rdp_schema_(?:orm|migration)_contract_failed",
        ):
            validate_rdp_schema(settings)

        repaired = apply_rdp_migrations(settings)
        self.assertTrue(repaired.ok, repaired.error_message)
        self.assertEqual(
            [stage.stage for stage in repaired.stages if stage.applied],
            [last_stage],
        )
        validate_rdp_schema(settings)

    def test_runtime_lineage_migration_and_schema_guard(self) -> None:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        from aats.storage.session import (
            apply_current_migrations,
            create_database_runtime,
            create_schema,
            validate_runtime_schema,
        )

        base_url = make_url(self.container.get_connection_url(driver="psycopg2"))
        admin_engine = create_engine(
            base_url.render_as_string(hide_password=False),
            future=True,
        )
        schema_name = f"aats_rdp_lineage_{uuid.uuid4().hex[:12]}"
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

        query = dict(base_url.query)
        query["options"] = f"-csearch_path={schema_name}"
        runtime = create_database_runtime(
            base_url.set(query=query).render_as_string(hide_password=False)
        )
        try:
            create_schema(runtime)
            with runtime.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE strategy_sleeve_intents "
                        "DROP COLUMN signal_bar_start CASCADE, "
                        "DROP COLUMN feature_snapshot_ref"
                    )
                )

            applied = apply_current_migrations(runtime)

            self.assertIn(
                "006_strategy_sleeve_intent_attribution_lineage.sql",
                applied,
            )
            validate_runtime_schema(runtime)

            event_1 = datetime(2026, 8, 26, 12, 1, tzinfo=timezone.utc)
            event_2 = event_1 + timedelta(minutes=1)
            with runtime.engine.begin() as connection:
                for index, created_at in enumerate(
                    (event_1 - timedelta(seconds=1), event_2 - timedelta(seconds=1)),
                    start=1,
                ):
                    reconciliation_id = f"recon_{index}"
                    connection.execute(
                        text(
                            """
                            INSERT INTO reconciliation_reports (
                                reconciliation_id, as_of_ts, created_at, severity,
                                halt_required, primary_symbol, payload
                            ) VALUES (
                                :reconciliation_id, :created_at, :created_at, 'normal',
                                FALSE, 'BTC-USDT-SWAP', '{}'::jsonb
                            )
                            """
                        ),
                        {
                            "reconciliation_id": reconciliation_id,
                            "created_at": created_at,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO reconciliation_state_snapshots (
                                snapshot_id, reconciliation_id, primary_symbol,
                                recovery_state, resume_eligible, safe_to_trade,
                                review_required, only_reduce_required, halt_required,
                                bundle_recovery_required, resume_blocked_reasons_json,
                                details, created_at
                            ) VALUES (
                                :snapshot_id, :reconciliation_id, 'BTC-USDT-SWAP',
                                'normal_operation', TRUE, TRUE,
                                FALSE, FALSE, FALSE, FALSE,
                                '[]'::jsonb, '{}'::jsonb, :created_at
                            )
                            """
                        ),
                        {
                            "snapshot_id": f"snapshot_{index}",
                            "reconciliation_id": reconciliation_id,
                            "created_at": created_at,
                        },
                    )

            from aats.data_platform.attribution.alignment import (
                query_reconciliation_snapshots,
            )

            with runtime.session_factory() as session:
                snapshots = query_reconciliation_snapshots(
                    session,
                    symbol="BTC-USDT-SWAP",
                    event_times=[event_1, event_2],
                )
            self.assertEqual(
                [snapshot["snapshot_id"] for snapshot in snapshots],
                ["snapshot_1", "snapshot_2"],
            )

            with runtime.engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE strategy_sleeve_intents "
                        "DROP COLUMN feature_snapshot_ref"
                    )
                )
            with self.assertRaisesRegex(
                RuntimeError,
                r"missing=strategy_sleeve_intents\.feature_snapshot_ref",
            ):
                validate_runtime_schema(runtime)
        finally:
            runtime.dispose()
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            admin_engine.dispose()

    def test_stage_18_registry_is_immutable_and_trade_rebuild_is_repeatable(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.backfill.official_history_importers import (
            register_official_source,
        )
        from aats.data_platform.config import ResearchPlatformSettings
        from aats.data_platform.data_governance.historical_rebuild import (
            execute_historical_rebuild,
            plan_historical_rebuild,
            start_historical_rebuild,
        )
        from aats.data_platform.data_governance.registry import (
            finalize_historical_bundle,
            import_source_record,
            persist_historical_bundle,
            reserve_historical_bundle,
        )
        from aats.data_platform.db import apply_rdp_migrations, get_engine
        from aats.data_platform.jobs.run_registry import create_ingest_run

        settings = ResearchPlatformSettings(
            database_url=self.container.get_connection_url(driver="psycopg2"),
            _env_file=None,
        )
        report = apply_rdp_migrations(settings)
        self.assertTrue(report.ok, report.error_message)

        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=15)
        source_key = f"integration-trades-{uuid.uuid4()}"
        engine = get_engine(settings)
        with Session(engine) as session, session.begin():
            source_id = register_official_source(
                session,
                source_key=source_key,
                source_kind="okx_rest",
                source_locator="/api/v5/market/history-trades",
                timestamp_semantics="OKX trade ts; [start,end) UTC",
            )
            same_source_id = register_official_source(
                session,
                source_key=source_key,
                source_kind="okx_rest",
                source_locator="/api/v5/market/history-trades",
                timestamp_semantics="OKX trade ts; [start,end) UTC",
            )
            self.assertEqual(source_id, same_source_id)
            with self.assertRaisesRegex(
                RuntimeError,
                "official_source_registry_immutable_conflict",
            ):
                register_official_source(
                    session,
                    source_key=source_key,
                    source_kind="okx_rest",
                    source_locator="/api/v5/market/history-trades-changed",
                    timestamp_semantics="OKX trade ts; [start,end) UTC",
                )

            source = import_source_record(
                source_key=source_key,
                source_kind="okx_rest",
                provider="OKX",
                source_locator="/api/v5/market/history-trades",
                coverage_start=start,
                coverage_end=end,
                timestamp_semantics="OKX trade ts; [start,end) UTC",
                schema_version="okx-v5",
                dataset_version="integration-v1",
                transform_version=None,
                git_commit="a" * 40,
                raw_partition_sha256=("a" * 64,),
                row_count=2,
                gaps=(),
                retrieved_at=start + timedelta(hours=1),
            )
            bundle_id, eligibility = persist_historical_bundle(
                session,
                source_id=source_id,
                source=source,
                symbol="BTC-USDT-SWAP",
                role="trades",
                purpose="trade_flow_research",
                coverage_ratio=1.0,
                causal_time_check=True,
            )
            self.assertTrue(eligibility.eligible)

            later_retry = import_source_record(
                source_key=source_key,
                source_kind="okx_rest",
                provider="OKX",
                source_locator="/api/v5/market/history-trades",
                coverage_start=start,
                coverage_end=end,
                timestamp_semantics="OKX trade ts; [start,end) UTC",
                schema_version="okx-v5",
                dataset_version="integration-v1",
                transform_version=None,
                git_commit="a" * 40,
                raw_partition_sha256=("a" * 64,),
                row_count=2,
                gaps=(),
                retrieved_at=start + timedelta(hours=2),
            )
            retry_bundle_id, _ = persist_historical_bundle(
                session,
                source_id=source_id,
                source=later_retry,
                symbol="BTC-USDT-SWAP",
                role="trades",
                purpose="trade_flow_research",
                coverage_ratio=1.0,
                causal_time_check=True,
            )
            self.assertEqual(bundle_id, retry_bundle_id)

            reservation_source = import_source_record(
                source_key=source_key,
                source_kind="okx_rest",
                provider="OKX",
                source_locator="/api/v5/market/history-trades",
                coverage_start=start,
                coverage_end=end,
                timestamp_semantics="OKX trade ts; [start,end) UTC",
                schema_version="okx-v5",
                dataset_version="integration-v1",
                transform_version=None,
                git_commit="a" * 40,
                raw_partition_sha256=("c" * 64,),
                row_count=2,
                gaps=(),
                retrieved_at=start + timedelta(hours=3),
            )
            reserved_id, reservation_fingerprint = reserve_historical_bundle(
                session,
                source_id=source_id,
                source=reservation_source,
                symbol="BTC-USDT-SWAP",
                role="trades",
                purpose="trade_flow_research",
            )
            self.assertIsNotNone(reservation_fingerprint)
            finalized_id, finalized_report = finalize_historical_bundle(
                session,
                bundle_id=reserved_id,
                reservation_fingerprint=reservation_fingerprint,
                source_id=source_id,
                source=reservation_source,
                symbol="BTC-USDT-SWAP",
                role="trades",
                purpose="trade_flow_research",
                coverage_ratio=1.0,
                causal_time_check=True,
            )
            self.assertEqual(reserved_id, finalized_id)
            self.assertTrue(finalized_report.eligible)

            ingest_run_id = create_ingest_run(
                session,
                run_type="backfill",
                dataset_domain="microstructure",
                instrument_type="SWAP",
                symbol="BTC-USDT-SWAP",
                trigger_mode="manual",
            )
            for index, (side, price, size) in enumerate(
                (("buy", "100", "2"), ("sell", "102", "1")),
                start=1,
            ):
                session.execute(
                    text(
                        """
                        INSERT INTO staging.official_trade_history (
                            source_id, symbol, ts, trade_id, px, sz, side,
                            raw_payload, raw_partition_sha256, ingest_run_id
                        ) VALUES (
                            CAST(:source_id AS UUID), 'BTC-USDT-SWAP', :ts,
                            :trade_id, :price, :size, :side, '{}'::jsonb,
                            :raw_sha256, CAST(:ingest_run_id AS UUID)
                        )
                        """
                    ),
                    {
                        "source_id": source_id,
                        "ts": start + timedelta(minutes=index),
                        "trade_id": str(index),
                        "price": price,
                        "size": size,
                        "side": side,
                        "raw_sha256": "a" * 64,
                        "ingest_run_id": ingest_run_id,
                    },
                )

            plan = plan_historical_rebuild(
                session,
                bundle_id=bundle_id,
                git_commit="a" * 40,
            )
            self.assertEqual(start_historical_rebuild(session, plan), "started")
            rebuilt = execute_historical_rebuild(session, plan)
            self.assertEqual(rebuilt.rows_read, 2)
            self.assertEqual(rebuilt.rows_written, 1)
            self.assertEqual(rebuilt.output_table, "silver.historical_trade_flow_15m")
            self.assertEqual(len(rebuilt.output_fingerprint), 64)
            self.assertEqual(
                start_historical_rebuild(session, plan),
                "already_succeeded",
            )
            original_row_fingerprint = session.execute(
                text(
                    "SELECT output_fingerprint "
                    "FROM silver.historical_trade_flow_15m "
                    "WHERE bundle_id = CAST(:bundle_id AS UUID)"
                ),
                {"bundle_id": bundle_id},
            ).scalar_one()
            session.execute(
                text(
                    "UPDATE silver.historical_trade_flow_15m "
                    "SET output_fingerprint = :tampered "
                    "WHERE bundle_id = CAST(:bundle_id AS UUID)"
                ),
                {"bundle_id": bundle_id, "tampered": "0" * 64},
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "historical_rebuild_succeeded_output_fingerprint_mismatch",
            ):
                start_historical_rebuild(session, plan)
            session.execute(
                text(
                    "UPDATE silver.historical_trade_flow_15m "
                    "SET output_fingerprint = :original "
                    "WHERE bundle_id = CAST(:bundle_id AS UUID)"
                ),
                {"bundle_id": bundle_id, "original": original_row_fingerprint},
            )

            row = session.execute(
                text(
                    "SELECT trade_count, total_size, vwap, "
                    "trade_flow_imbalance FROM silver.historical_trade_flow_15m "
                    "WHERE bundle_id = CAST(:bundle_id AS UUID)"
                ),
                {"bundle_id": bundle_id},
            ).one()
            self.assertEqual(row.trade_count, 2)
            self.assertEqual(str(row.total_size), "3.000000000000000000")
            self.assertEqual(str(row.vwap), "100.666666666667")
            self.assertEqual(str(row.trade_flow_imbalance), "0.333333333333")

            session.execute(
                text(
                    "DELETE FROM staging.official_trade_history "
                    "WHERE source_id = CAST(:source_id AS UUID) AND trade_id = '2'"
                ),
                {"source_id": source_id},
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "historical_bundle_source_row_count_mismatch",
            ):
                execute_historical_rebuild(session, plan)

    def test_archive_partition_restores_into_isolated_temp_table(self) -> None:
        import tempfile
        from pathlib import Path

        from sqlalchemy import text
        from sqlalchemy.orm import Session, sessionmaker

        from aats.data_platform.config import ResearchPlatformSettings
        from aats.data_platform.data_governance.archive import (
            ArchiveScope,
            archive_partition,
            register_local_capture_source,
            verify_archive_restore_drill,
        )
        from aats.data_platform.db import apply_rdp_migrations, get_engine, reset_engine
        from aats.data_platform.jobs.run_registry import create_ingest_run

        assert PostgresContainer is not None
        archive_container = PostgresContainer("postgres:16-alpine")
        archive_container.start()
        reset_engine()
        try:
            settings = ResearchPlatformSettings(
                database_url=archive_container.get_connection_url(driver="psycopg2"),
                _env_file=None,
            )
            report = apply_rdp_migrations(settings)
            self.assertTrue(report.ok, report.error_message)
            engine = get_engine(settings)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            day = datetime(2026, 8, 20, tzinfo=timezone.utc)
            symbol = "RESTORE-DRILL-SWAP"
            with Session(engine) as session, session.begin():
                source_id = register_local_capture_source(
                    session,
                    source_key=f"integration-archive-{uuid.uuid4()}",
                    table="bronze.market_trades",
                    schema_version="integration-v1",
                    timestamp_semantics="integration UTC trade timestamp",
                )
                ingest_run_id = create_ingest_run(
                    session,
                    run_type="backfill",
                    dataset_domain="microstructure",
                    instrument_type="SWAP",
                    symbol=symbol,
                    trigger_mode="manual",
                )
                for index in range(2):
                    session.execute(
                        text(
                            "INSERT INTO bronze.market_trades "
                            "(symbol, ts, trade_id, px, sz, side, raw_payload, ingest_run_id) "
                            "VALUES (:symbol, :ts, :trade_id, 100, 1, 'buy', "
                            "CAST(:payload AS jsonb), CAST(:ingest_run_id AS UUID))"
                        ),
                        {
                            "symbol": symbol,
                            "ts": day + timedelta(seconds=index),
                            "trade_id": str(index),
                            "payload": '{"source":"integration"}',
                            "ingest_run_id": ingest_run_id,
                        },
                    )
            scope = ArchiveScope(
                source_id=source_id,
                dataset_name="bronze.market_trades",
                table="bronze.market_trades",
                symbol=symbol,
                coverage_start=day,
                coverage_end=day + timedelta(days=1),
            )
            with tempfile.TemporaryDirectory() as temporary:
                artifact = archive_partition(
                    factory,
                    scope,
                    Path(temporary),
                    minimum_free_bytes=0,
                    batch_size=1,
                )
                restored = verify_archive_restore_drill(
                    factory,
                    scope,
                    Path(artifact.parquet_path),
                    expected_sha256=artifact.sha256,
                    expected_rows=artifact.row_count,
                    batch_size=1,
                )
            self.assertEqual(restored.row_count, 2)
            self.assertEqual(restored.parquet_sha256, artifact.sha256)
            self.assertEqual(restored.dataset_name, "bronze.market_trades")
            with Session(engine) as session:
                source_rows = session.execute(
                    text(
                        "SELECT COUNT(*) FROM bronze.market_trades "
                        "WHERE symbol = :symbol"
                    ),
                    {"symbol": symbol},
                ).scalar_one()
            self.assertEqual(source_rows, 2)
        finally:
            reset_engine()
            archive_container.stop()

    def test_stage_19_source_aware_gold_is_versioned_and_repeatable(self) -> None:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from aats.data_platform.collectors.backfill.official_history_importers import (
            register_official_source,
        )
        from aats.data_platform.config import ResearchPlatformSettings
        from aats.data_platform.data_governance.historical_gold import (
            execute_historical_gold,
            plan_historical_gold,
            start_historical_gold,
        )
        from aats.data_platform.data_governance.registry import (
            import_source_record,
            persist_historical_bundle,
        )
        from aats.data_platform.db import apply_rdp_migrations, get_engine
        from aats.data_platform.jobs.run_registry import create_ingest_run

        settings = ResearchPlatformSettings(
            database_url=self.container.get_connection_url(driver="psycopg2"),
            _env_file=None,
        )
        report = apply_rdp_migrations(settings)
        self.assertTrue(report.ok, report.error_message)
        engine = get_engine(settings)
        start = datetime(2026, 8, 10, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        symbol = "STAGE19-USDT-SWAP"
        candle_version = f"candle-{uuid.uuid4()}"
        funding_version = f"funding-{uuid.uuid4()}"
        with Session(engine) as session, session.begin():
            candle_source_key = f"integration-candle-{uuid.uuid4()}"
            candle_source_id = register_official_source(
                session,
                source_key=candle_source_key,
                source_kind="okx_rest",
                source_locator="/integration/candles",
                timestamp_semantics="confirmed UTC candle opening time",
            )
            candle_source = import_source_record(
                source_key=candle_source_key,
                source_kind="okx_rest",
                provider="OKX",
                source_locator="/integration/candles",
                coverage_start=start,
                coverage_end=end,
                timestamp_semantics="confirmed UTC candle opening time",
                schema_version="integration-v1",
                dataset_version=candle_version,
                transform_version="integration-candle-v1",
                git_commit="a" * 40,
                raw_partition_sha256=("a" * 64,),
                row_count=4,
                gaps=(),
            )
            candle_bundle_id, candle_report = persist_historical_bundle(
                session,
                source_id=candle_source_id,
                source=candle_source,
                symbol=symbol,
                role="candles",
                purpose="ohlcv_research",
                coverage_ratio=1.0,
                causal_time_check=True,
            )
            self.assertTrue(candle_report.eligible)

            funding_source_key = f"integration-funding-{uuid.uuid4()}"
            funding_source_id = register_official_source(
                session,
                source_key=funding_source_key,
                source_kind="okx_rest",
                source_locator="/integration/funding",
                timestamp_semantics="UTC funding settlement time",
            )
            funding_source = import_source_record(
                source_key=funding_source_key,
                source_kind="okx_rest",
                provider="OKX",
                source_locator="/integration/funding",
                coverage_start=start,
                coverage_end=end,
                timestamp_semantics="UTC funding settlement time",
                schema_version="integration-v1",
                dataset_version=funding_version,
                transform_version="integration-funding-v1",
                git_commit="a" * 40,
                raw_partition_sha256=("b" * 64,),
                row_count=1,
                gaps=(),
            )
            funding_bundle_id, funding_report = persist_historical_bundle(
                session,
                source_id=funding_source_id,
                source=funding_source,
                symbol=symbol,
                role="funding",
                purpose="funding_research",
                coverage_ratio=1.0,
                causal_time_check=True,
            )
            self.assertTrue(funding_report.eligible)

            candle_run = create_ingest_run(
                session,
                run_type="backfill",
                dataset_domain="candles",
                instrument_type="SWAP",
                symbol=symbol,
                timeframe="15m",
                trigger_mode="manual",
            )
            for index in range(4):
                session.execute(
                    text(
                        "INSERT INTO silver.market_swap_candles_15m "
                        "(symbol, ts, open, high, low, close, vol, vol_quote, "
                        "confirm, ingest_run_id, dataset_version) VALUES "
                        "(:symbol, :ts, 100, 102, 99, 101, 1, 101, TRUE, "
                        "CAST(:run_id AS UUID), :dataset_version)"
                    ),
                    {
                        "symbol": symbol,
                        "ts": start + timedelta(minutes=15 * index),
                        "run_id": candle_run,
                        "dataset_version": candle_version,
                    },
                )
            funding_run = create_ingest_run(
                session,
                run_type="backfill",
                dataset_domain="funding",
                instrument_type="SWAP",
                symbol=symbol,
                trigger_mode="manual",
            )
            session.execute(
                text(
                    "INSERT INTO silver.market_swap_funding "
                    "(symbol, ts, funding_rate, ingest_run_id, dataset_version) "
                    "VALUES (:symbol, :ts, 0.0001, CAST(:run_id AS UUID), :version)"
                ),
                {
                    "symbol": symbol,
                    "ts": start,
                    "run_id": funding_run,
                    "version": funding_version,
                },
            )

            plan = plan_historical_gold(
                session,
                symbol=symbol,
                timeframe="15m",
                candle_bundle_id=candle_bundle_id,
                funding_bundle_id=funding_bundle_id,
                git_commit="c" * 40,
            )
            state, artifact_id = start_historical_gold(session, plan)
            self.assertEqual(state, "started")
            result = execute_historical_gold(
                session,
                plan,
                artifact_id=artifact_id,
            )
            self.assertEqual(result.rows_written, 4)
            self.assertTrue(result.quality_report["eligible"])
            self.assertEqual(result.artifact_index["input_bundle_count"], 2)
            state, same_artifact_id = start_historical_gold(session, plan)
            self.assertEqual(state, "already_succeeded")
            self.assertEqual(same_artifact_id, artifact_id)
            original_row_fingerprint = session.execute(
                text(
                    "SELECT output_fingerprint FROM gold.historical_replay_bars "
                    "WHERE artifact_id = CAST(:artifact_id AS UUID) ORDER BY ts LIMIT 1"
                ),
                {"artifact_id": artifact_id},
            ).scalar_one()
            session.execute(
                text(
                    "UPDATE gold.historical_replay_bars "
                    "SET output_fingerprint = :tampered WHERE artifact_id = "
                    "CAST(:artifact_id AS UUID) AND ts = :start"
                ),
                {
                    "artifact_id": artifact_id,
                    "start": start,
                    "tampered": "0" * 64,
                },
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "historical_gold_succeeded_artifact_fingerprint_mismatch",
            ):
                start_historical_gold(session, plan)
            session.execute(
                text(
                    "UPDATE gold.historical_replay_bars "
                    "SET output_fingerprint = :original WHERE artifact_id = "
                    "CAST(:artifact_id AS UUID) AND ts = :start"
                ),
                {
                    "artifact_id": artifact_id,
                    "start": start,
                    "original": original_row_fingerprint,
                },
            )
            rows = session.execute(
                text(
                    "SELECT COUNT(*), COUNT(DISTINCT source_candle_bundle_id), "
                    "COUNT(DISTINCT source_funding_bundle_id) "
                    "FROM gold.historical_replay_bars "
                    "WHERE artifact_id = CAST(:artifact_id AS UUID)"
                ),
                {"artifact_id": artifact_id},
            ).one()
            self.assertEqual(tuple(rows), (4, 1, 1))


if __name__ == "__main__":
    unittest.main()
