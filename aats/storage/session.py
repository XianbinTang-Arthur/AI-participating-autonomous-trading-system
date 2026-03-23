from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aats.storage.sqlalchemy_models import Base


_EXPECTED_NUMERIC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("portfolio_snapshots", "total_equity"),
    ("portfolio_snapshots", "realized_pnl"),
    ("portfolio_snapshots", "unrealized_pnl"),
    ("order_states", "requested_qty"),
    ("order_states", "filled_qty"),
    ("order_states", "remaining_qty"),
    ("order_states", "average_fill_price"),
    ("order_states", "fees"),
    ("fill_events", "fill_qty"),
    ("fill_events", "fill_price"),
    ("fill_events", "fee_amount"),
    ("fill_outcomes", "fill_qty"),
    ("fill_outcomes", "fill_price"),
    ("fill_outcomes", "fill_notional"),
    ("fill_outcomes", "fee_amount"),
    ("fill_outcomes", "starting_position_qty"),
    ("fill_outcomes", "starting_avg_entry_price"),
    ("fill_outcomes", "ending_position_qty"),
    ("fill_outcomes", "ending_avg_entry_price"),
    ("fill_outcomes", "realized_pnl_delta"),
    ("fill_outcomes", "fee_delta"),
    ("funding_fee_records", "amount"),
    ("funding_fee_records", "balance_after"),
    ("order_obligations", "reserved_amount"),
    ("order_obligations", "consumed_amount"),
    ("order_obligations", "released_amount"),
    ("execution_orders", "requested_qty"),
    ("execution_orders", "limit_price"),
    ("execution_fills", "fill_qty"),
    ("execution_fills", "fill_price"),
    ("execution_fills", "fee_amount"),
    ("ledger_entries", "amount"),
    ("reservations", "reserved_amount"),
    ("reservations", "consumed_amount"),
    ("reservations", "released_amount"),
    ("position_lots", "signed_quantity_open"),
    ("position_lots", "entry_price"),
    ("lot_events", "quantity"),
    ("lot_events", "entry_price"),
    ("lot_events", "exit_price"),
    ("lot_events", "realized_pnl_delta"),
)


@dataclass(slots=True)
class DatabaseRuntime:
    engine: Engine
    session_factory: sessionmaker[Session]
    runtime_lock_key: int | None = None
    runtime_lock_connection: Connection | None = None

    def acquire_single_runtime_lock(self, lock_key: int) -> None:
        if self.engine.dialect.name != "postgresql":
            return
        if self.runtime_lock_connection is not None and self.runtime_lock_key == lock_key:
            return
        connection = self.engine.connect()
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": lock_key},
        ).scalar()
        if not bool(acquired):
            connection.close()
            raise RuntimeError("database_single_runtime_lock_not_acquired")
        self.runtime_lock_key = lock_key
        self.runtime_lock_connection = connection

    def dispose(self) -> None:
        if self.runtime_lock_connection is not None and self.runtime_lock_key is not None:
            try:
                self.runtime_lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.runtime_lock_key},
                )
            finally:
                self.runtime_lock_connection.close()
                self.runtime_lock_connection = None
                self.runtime_lock_key = None
        self.engine.dispose()


def create_database_runtime(database_url: str) -> DatabaseRuntime:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "postgresql":
        raise ValueError("database_url_must_use_postgresql")

    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
    )
    return DatabaseRuntime(
        engine=engine,
        session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
    )


def create_schema(runtime: DatabaseRuntime) -> None:
    Base.metadata.create_all(runtime.engine)


def validate_runtime_schema(runtime: DatabaseRuntime) -> None:
    if runtime.engine.dialect.name != "postgresql":
        return
    expected = {(table, column): ("numeric", 36, 18) for table, column in _EXPECTED_NUMERIC_COLUMNS}
    expected_pairs_sql = ", ".join(f"('{table}', '{column}')" for table, column in _EXPECTED_NUMERIC_COLUMNS)
    query = text(
        f"""
        SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND (table_name, column_name) IN ({expected_pairs_sql})
        """
    )
    with runtime.engine.connect() as connection:
        rows = connection.execute(query).mappings().all()
    actual = {
        (row["table_name"], row["column_name"]): (
            row["data_type"],
            row["numeric_precision"],
            row["numeric_scale"],
        )
        for row in rows
    }
    missing = [f"{table}.{column}" for table, column in expected if (table, column) not in actual]
    mismatches = [
        (
            f"{table}.{column}",
            actual[(table, column)],
        )
        for table, column in expected
        if (table, column) in actual and actual[(table, column)] != expected[(table, column)]
    ]
    if missing or mismatches:
        mismatch_text = ", ".join(f"{name}={value}" for name, value in mismatches)
        missing_text = ", ".join(missing)
        raise RuntimeError(
            "database_schema_validation_failed"
            + (f": missing={missing_text}" if missing_text else "")
            + (f": mismatched={mismatch_text}" if mismatch_text else "")
        )
