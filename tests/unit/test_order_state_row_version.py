"""Stage 5 单元测试：OrderStateModel 乐观并发版本号 (row_version)。

子任务 5a-1：给 order_states 表加 SQLAlchemy version_id_col。
本组测试只覆盖以下三个层面，**不依赖 Postgres**：

1. ORM 元数据层：OrderStateModel 必须把 row_version 声明为 Mapped 列，
   并且通过 __mapper_args__ 把它绑定到 mapper 的 version_id_col。
2. 物理 schema 层：migration 文件 0006 必须存在并且包含 ALTER TABLE 子句。
3. 调用方层：execution_engine outbox 在 commit 抛 StaleDataError 时
   会重新打开 session 重试，3 次仍然失败才把异常往外传。

Postgres 端到端的"两个 session 并发更新只允许一个成功"的实际行为
在集成测试 (tests/integration/test_execution_outbox_postgres.py 等) 里
有对应覆盖；本文件专注于"接线是否正确"的快速回归。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.orm.exc import StaleDataError

from aats.schemas.execution import OrderObligation, OrderState
from aats.storage.sqlalchemy_models import OrderStateModel
from aats.services.execution_engine.outbox import PostgresExecutionOutboxPublisher


# ─────────────────────────────────────────────────────────────────────
# 1) ORM 元数据：row_version 列与 version_id_col 绑定
# ─────────────────────────────────────────────────────────────────────


def test_order_state_model_has_row_version_column_with_bigint_default_one() -> None:
    """row_version 必须是 BIGINT NOT NULL DEFAULT 1。"""
    table = OrderStateModel.__table__
    assert "row_version" in table.columns, "OrderStateModel 必须新增 row_version 列"

    column = table.columns["row_version"]
    assert isinstance(column.type, BigInteger), (
        f"row_version 应当是 BigInteger（与 strategy_execution_bundles 一致），实际 {type(column.type).__name__}"
    )
    assert column.nullable is False, "row_version 必须 NOT NULL，避免被 NULL 绕过 OCC"

    default_value = None
    if column.default is not None and getattr(column.default, "arg", None) is not None:
        default_value = column.default.arg
    assert default_value == 1, f"row_version 默认值必须是 1（首次插入），实际 {default_value!r}"


def test_order_state_mapper_uses_row_version_as_version_id_col() -> None:
    """SQLAlchemy mapper 必须把 row_version 注册为 version_id_col。

    没有这一步，UPDATE 不会自动 WHERE row_version=N + SET row_version=N+1，
    OCC 整个 fall through，列存在但毫无作用。
    """
    mapper = OrderStateModel.__mapper__
    version_col = mapper.version_id_col
    assert version_col is not None, "OrderStateModel 必须设置 __mapper_args__['version_id_col']"
    assert version_col is OrderStateModel.__table__.columns["row_version"], (
        "version_id_col 必须指向 order_states.row_version 列本身"
    )


# ─────────────────────────────────────────────────────────────────────
# 2) Migration 文件存在性
# ─────────────────────────────────────────────────────────────────────


def test_migration_0006_adds_row_version_to_order_states() -> None:
    """migration 0006 必须给 order_states 加 row_version 列，并提供回滚说明。"""
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "0006_postgres_order_states_row_version.sql"
    )
    assert migration_path.exists(), f"缺失 migration 文件：{migration_path}"

    sql = migration_path.read_text(encoding="utf-8")
    # 关键 SQL 语句必须出现
    assert "ALTER TABLE order_states" in sql
    assert "ADD COLUMN" in sql and "row_version" in sql
    assert "BIGINT NOT NULL DEFAULT 1" in sql
    # 回滚说明（注释里的 DROP COLUMN）
    assert "DROP COLUMN" in sql, "migration 必须在注释里给出 down migration（DROP COLUMN）"
    # 设计澄清：注释里说明为什么不加到 execution_fills / fill_events
    assert "execution_fills" in sql, "migration 必须解释为何 execution_fills 不需要 row_version"


# ─────────────────────────────────────────────────────────────────────
# 3) Outbox 层：StaleDataError → 重试
# ─────────────────────────────────────────────────────────────────────


def _make_order_state() -> OrderState:
    return OrderState(
        decision_id="dec-row-version-1",
        intent_id="intent-row-version-1",
        client_order_id="cord-row-version-1",
        symbol="BTC-USDT",
        status="SUBMITTED",
        requested_qty=Decimal("1"),
        filled_qty=Decimal("0"),
        remaining_qty=Decimal("1"),
        fees=Decimal("0"),
        created_at=datetime.now(timezone.utc),
    )


class _StubPublisher:
    """绕过 dataclass 必填字段，只测 retry 控制流。

    PostgresExecutionOutboxPublisher 是 slots dataclass，构造时需要十几个
    依赖。retry 控制流只需要 self._MAX_PERSIST_ATTEMPTS 和 self.logger，
    所以这里捏一个最小 stub，直接复用真实的 _persist_order_state_with_retry
    方法（通过未绑定方法调用）。
    """

    _MAX_PERSIST_ATTEMPTS = PostgresExecutionOutboxPublisher._MAX_PERSIST_ATTEMPTS

    def __init__(self) -> None:
        import logging

        self.logger = logging.getLogger("test.execution_outbox.row_version")
        self.calls: list[OrderState] = []
        self.fail_times = 0
        self.return_state: OrderState | None = None

    def _persist_order_state_sync(
        self,
        *,
        order_state: OrderState,
        key: str,
        obligation: OrderObligation | None,
    ) -> OrderState:
        self.calls.append(order_state)
        if len(self.calls) <= self.fail_times:
            raise StaleDataError("UPDATE", 0, 1, "test-injected stale data")
        return self.return_state or order_state


def test_outbox_persist_order_state_retries_until_success() -> None:
    """前两次 commit 撞 StaleDataError，第三次成功 → 总共 3 次调用，最终返回成功结果。"""
    stub = _StubPublisher()
    stub.fail_times = 2
    state = _make_order_state()
    stub.return_state = state

    result = PostgresExecutionOutboxPublisher._persist_order_state_with_retry(
        stub,  # type: ignore[arg-type]
        order_state=state,
        key=state.symbol,
        obligation=None,
    )

    assert result is state
    assert len(stub.calls) == 3, "应当重试 3 次（前 2 次 stale + 第 3 次成功）"


def test_outbox_persist_order_state_raises_after_max_attempts() -> None:
    """连续 3 次 StaleDataError 应当把异常往上抛（不再吞掉）。"""
    stub = _StubPublisher()
    stub.fail_times = 5  # 远超 MAX
    state = _make_order_state()

    with pytest.raises(StaleDataError):
        PostgresExecutionOutboxPublisher._persist_order_state_with_retry(
            stub,  # type: ignore[arg-type]
            order_state=state,
            key=state.symbol,
            obligation=None,
        )

    assert len(stub.calls) == PostgresExecutionOutboxPublisher._MAX_PERSIST_ATTEMPTS, (
        "失败时应当严格执行 MAX 次后停止"
    )


def test_outbox_persist_order_state_no_retry_on_success_path() -> None:
    """成功路径不重试，调用 1 次即返回。"""
    stub = _StubPublisher()
    stub.fail_times = 0
    state = _make_order_state()
    stub.return_state = state

    result = PostgresExecutionOutboxPublisher._persist_order_state_with_retry(
        stub,  # type: ignore[arg-type]
        order_state=state,
        key=state.symbol,
        obligation=None,
    )

    assert result is state
    assert len(stub.calls) == 1, "成功路径必须只调用一次，避免无谓的重试开销"
