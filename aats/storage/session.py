from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

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
    ("sleeve_pnl_records", "realized_pnl"),
    ("sleeve_pnl_records", "fee_amount"),
    ("sleeve_pnl_records", "funding_fee_amount"),
    ("sleeve_pnl_records", "inventory_move_qty"),
    ("exit_execution_intents", "target_exit_quantity"),
    ("exit_execution_intents", "aggregated_filled_quantity"),
    ("exit_execution_intents", "open_child_working_quantity"),
    ("exit_execution_intents", "open_child_unknown_quantity"),
    ("exit_execution_intents", "remaining_dispatchable_quantity"),
    ("exit_execution_intents", "remaining_unresolved_quantity"),
    ("exit_execution_child_refs", "planned_quantity"),
    ("exit_execution_child_refs", "known_filled_quantity"),
    ("exit_execution_child_refs", "remaining_quantity_estimate"),
    ("strategy_sleeve_intents", "budget_multiplier"),
    ("strategy_sleeve_intents", "allocator_weight"),
    ("sleeve_budget_profiles", "quote_budget_limit"),
    ("sleeve_budget_profiles", "margin_budget_limit"),
    ("sleeve_budget_profiles", "notional_cap"),
    ("sleeve_budget_profiles", "max_symbol_notional"),
    ("sleeve_budget_profiles", "max_drawdown_usdt"),
    ("sleeve_budget_profiles", "allocator_base_weight"),
    ("sleeve_budget_assignments", "active_budget_multiplier"),
    ("sleeve_budget_assignments", "allocator_base_weight"),
    ("sleeve_budget_assignments", "effective_quote_budget_limit"),
    ("sleeve_budget_assignments", "effective_margin_budget_limit"),
    ("sleeve_budget_assignments", "effective_notional_cap"),
    ("sleeve_budget_assignments", "effective_max_symbol_notional"),
    ("allocator_budget_snapshots", "requested_notional"),
    ("allocator_budget_snapshots", "approved_notional"),
    ("allocator_budget_snapshots", "requested_delta_qty"),
    ("allocator_budget_snapshots", "approved_delta_qty"),
    ("allocator_budget_snapshots", "budget_multiplier"),
    ("allocator_budget_snapshots", "allocator_weight"),
    ("allocator_budget_snapshots", "quote_budget_limit"),
    ("allocator_budget_snapshots", "margin_budget_limit"),
    ("allocator_budget_snapshots", "notional_cap"),
    ("allocator_budget_snapshots", "max_symbol_notional"),
    ("allocator_budget_snapshots", "portfolio_requested_notional"),
    ("allocator_budget_snapshots", "portfolio_approved_notional"),
    ("allocator_budget_snapshots", "portfolio_budget_cut_notional"),
    ("allocator_conflict_resolutions", "gross_requested_qty"),
    ("allocator_conflict_resolutions", "net_approved_qty"),
    ("allocator_conflict_resolutions", "blocked_qty"),
    ("allocator_conflict_resolutions", "protected_notional"),
    ("allocator_conflict_resolutions", "reduced_notional"),
    ("allocator_netting_decisions", "gross_buy_qty"),
    ("allocator_netting_decisions", "gross_sell_qty"),
    ("allocator_netting_decisions", "net_approved_qty"),
    ("portfolio_allocation_decisions", "portfolio_requested_notional"),
    ("portfolio_allocation_decisions", "portfolio_approved_notional"),
    ("portfolio_allocation_decisions", "portfolio_budget_cut_notional"),
    ("portfolio_allocation_decisions", "expected_edge_bps"),
    ("portfolio_allocation_decisions", "expected_cost_bps"),
    ("strategy_execution_bundles", "gross_requested_exposure"),
    ("strategy_execution_bundles", "net_approved_exposure"),
    ("strategy_execution_bundles", "expected_cost_bps"),
    ("strategy_execution_bundles", "expected_edge_bps"),
)

_SCHEMA_MIGRATIONS_TABLE = "schema_migrations"


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


def scoped_runtime_lock_key(
    *,
    database_url: str,
    base_lock_key: int,
    process_role: str | None = None,
) -> int:
    """派生数据库 advisory lock key。

    多进程切片化之后，单一全局锁会让 4 个进程互相阻塞。引入 process_role 之后，
    每个 role（gateway/market/decision/execution/monolith）派生独立的 lock_key，
    确保：
      - 每个 role 仍然只能有一份运行（防止重复启动同一进程）
      - 4 个 role 之间互不阻塞
    process_role=None 等价于 monolith，保持向后兼容（与重构前行为一致）。
    """
    parsed = make_url(database_url)
    query = dict(parsed.query)
    options = str(query.get("options") or "")
    search_path = "public"
    for token in options.split():
        if not token.startswith("-csearch_path="):
            continue
        candidate = token.split("=", 1)[1].strip()
        if candidate:
            search_path = candidate
        break
    role_token = (process_role or "monolith").strip().lower() or "monolith"
    seed = (
        f"{int(base_lock_key)}|{parsed.drivername}|{parsed.host or ''}|{parsed.port or ''}|"
        f"{parsed.database or ''}|{search_path}|{role_token}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    derived = int.from_bytes(digest[:8], byteorder="big", signed=False) & ((1 << 63) - 1)
    return derived or int(base_lock_key)


def create_database_runtime(database_url: str) -> DatabaseRuntime:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "postgresql":
        raise ValueError("database_url_must_use_postgresql")

    # 2026-04-20 S4 改动（gateway_slow_query_systematic_fix_sow.md §S4.3）：
    # pool_size 10→15, max_overflow 20→45. 理由：_parallel.py 嵌套守卫改为
    # "本地小池" 后，线程总数上限从 12 变成 12 × (1 + 4) = 60, 需要 DB 连接池
    # 相应匹配。PG max_connections=200 充裕，不会引爆。
    #
    # 2026-04-21 idle-in-transaction 安全网（生产诊断发现）：
    # 观察到 gateway 在极端 parallel_fetch 场景下（recovery_view 137s +
    # _cached_ttl singleflight 25s 惊群 → 5 批 followers 各起 9 路 fanout）
    # 累积 15+ 个 idle-in-transaction 连接，最老 21 分钟没 rollback。诊断
    # 数据：每个正常查询的 session.close()→rollback 延迟 150ms-3.5s（应为
    # ms 级）——这是 Python GIL + psycopg2 sync I/O + 61 thread 并发的系统性
    # 瓶颈。稳态下能 drain，但极端场景下累积超过 pool_size+max_overflow=60
    # 就会 pool_timeout。
    #
    # connect_args 加 idle_in_transaction_session_timeout=60000 让 PG 服务端
    # 在 60s 内没 rollback/commit 的 tx 被自动 terminate，强制回收连接。这是
    # **安全网**，不替代 Python 侧慢查询治理（P2-2 SOW 会继续做）。
    # 60s = 比 _cached_ttl singleflight_wait(25s) × 2 长，比 gateway API
    # 合理响应上限（30s）×2 长，正常场景不会误杀。
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=15,
        max_overflow=45,
        pool_timeout=30,
        connect_args={
            "options": "-c idle_in_transaction_session_timeout=60000",
        },
    )
    return DatabaseRuntime(
        engine=engine,
        session_factory=sessionmaker(bind=engine, expire_on_commit=False, future=True),
    )


def create_schema(runtime: DatabaseRuntime) -> None:
    Base.metadata.create_all(runtime.engine)


def apply_current_migrations(runtime: DatabaseRuntime) -> list[str]:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    applied_versions: list[str] = []
    with runtime.engine.begin() as connection:
        _ensure_schema_migrations_table(connection)
        applied = _applied_migration_checksums(connection)
        raw_connection = connection.connection
        with raw_connection.cursor() as cursor:
            for migration_path in sorted(migrations_dir.glob("*.sql")):
                version = migration_path.name
                sql = migration_path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                recorded_checksum = applied.get(version)
                if recorded_checksum is not None:
                    if recorded_checksum != checksum:
                        raise RuntimeError(f"database_migration_checksum_mismatch:{version}")
                    continue
                cursor.execute(sql)
                connection.execute(
                    text(
                        f"""
                        INSERT INTO {_SCHEMA_MIGRATIONS_TABLE} (version, checksum, applied_at)
                        VALUES (:version, :checksum, NOW())
                        """
                    ),
                    {"version": version, "checksum": checksum},
                )
                applied_versions.append(version)
    return applied_versions


def applied_migrations(runtime: DatabaseRuntime) -> list[str]:
    with runtime.engine.begin() as connection:
        _ensure_schema_migrations_table(connection)
        rows = connection.execute(
            text(
                f"""
                SELECT version
                FROM {_SCHEMA_MIGRATIONS_TABLE}
                ORDER BY applied_at, version
                """
            )
        ).scalars().all()
    return [str(row) for row in rows]


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


def _ensure_schema_migrations_table(connection: Connection) -> None:
    connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA_MIGRATIONS_TABLE} (
                version VARCHAR(256) PRIMARY KEY,
                checksum VARCHAR(128) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    )


def _applied_migration_checksums(connection: Connection) -> dict[str, str]:
    rows = connection.execute(
        text(
            f"""
            SELECT version, checksum
            FROM {_SCHEMA_MIGRATIONS_TABLE}
            """
        )
    ).mappings().all()
    return {str(row["version"]): str(row["checksum"]) for row in rows}
