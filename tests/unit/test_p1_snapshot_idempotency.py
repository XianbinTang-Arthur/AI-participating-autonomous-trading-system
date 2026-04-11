"""Stage 5 单元测试：P1 快照表的并发幂等性 (子任务 5a-2)。

设计说明：
设计文档原文是「给 portfolio_snapshots + reconciliation_state_snapshots 加 row_version
做 OCC」。审查代码后发现两张表都是 append-only：

* portfolio_snapshots: PK 是 sequence_id (Integer autoincrement)，每次 save_snapshot 都
  新建一行，没有 UPDATE 路径。SQLAlchemy version_id_col 在 INSERT 不会触发，纯 INSERT
  场景下 row_version 是死代码。
* reconciliation_state_snapshots: PK 是 snapshot_id (String)，同样只有 INSERT。

所以「P1 OCC」的真正落点是 INSERT 幂等性：
* portfolio_snapshots：autoincrement PK 天然不会冲突，无需改动。
* reconciliation_state_snapshots：snapshot_id 是字符串 PK，多进程 retry 可能撞 PK，
  改成 INSERT ... ON CONFLICT DO NOTHING。

本组测试用纯静态检查（不依赖 Postgres）覆盖以下两层：

1. ORM 元数据层：两张表都不应当出现 row_version 列（防止后续 contributor 误加）。
2. 调用方层：reconciliation_repo_postgres.save_state_snapshot 的源码必须出现
   pg_insert / on_conflict_do_nothing，证明走的是幂等插入路径。
"""
from __future__ import annotations

import inspect

from sqlalchemy import Integer, String

from aats.storage import reconciliation_repo_postgres
from aats.storage.reconciliation_repo_postgres import PostgresReconciliationRepository
from aats.storage.sqlalchemy_models import (
    PortfolioSnapshotModel,
    ReconciliationStateSnapshotModel,
)


# ─────────────────────────────────────────────────────────────────────
# 1) ORM 元数据：append-only 表不应当有 row_version
# ─────────────────────────────────────────────────────────────────────


def test_portfolio_snapshot_model_uses_autoincrement_int_pk_and_no_row_version() -> None:
    """portfolio_snapshots 是 autoincrement PK，无须 row_version。

    这条测试是回归守卫：如果以后有人在不读设计文档的情况下「按照 OCC 蓝图」往
    PortfolioSnapshotModel 加 row_version 列，CI 会立刻拦下并提示读 0007 注释。
    """
    table = PortfolioSnapshotModel.__table__

    # PK 必须是单列 sequence_id (Integer autoincrement)
    pk_columns = list(table.primary_key.columns)
    assert len(pk_columns) == 1, "portfolio_snapshots PK 必须是单列"
    pk = pk_columns[0]
    assert pk.name == "sequence_id"
    assert isinstance(pk.type, Integer)
    assert pk.autoincrement is True or pk.autoincrement == "auto", (
        "sequence_id 必须 autoincrement，否则会失去「天然不冲突」前提"
    )

    # 不能有 row_version 列（5a-2 设计决策）
    assert "row_version" not in table.columns, (
        "portfolio_snapshots 是 append-only autoincrement PK，加 row_version 是死代码 "
        "(SQLAlchemy version_id_col 在 INSERT 不会触发)。请阅读 migrations/0007 注释。"
    )

    # mapper 不应当声明 version_id_col
    mapper = PortfolioSnapshotModel.__mapper__
    assert mapper.version_id_col is None, (
        "PortfolioSnapshotModel 不应注册 version_id_col。详见 migrations/0007 设计说明。"
    )


def test_reconciliation_state_snapshot_model_uses_string_pk_and_no_row_version() -> None:
    """reconciliation_state_snapshots 是 append-only string PK，幂等性靠 ON CONFLICT。"""
    table = ReconciliationStateSnapshotModel.__table__

    pk_columns = list(table.primary_key.columns)
    assert len(pk_columns) == 1, "reconciliation_state_snapshots PK 必须是单列"
    pk = pk_columns[0]
    assert pk.name == "snapshot_id"
    assert isinstance(pk.type, String), "snapshot_id 是字符串 PK，幂等性靠 ON CONFLICT 而非 row_version"

    assert "row_version" not in table.columns, (
        "reconciliation_state_snapshots 是 append-only，应当通过 INSERT ... ON CONFLICT "
        "DO NOTHING 实现幂等，不应当加 row_version。详见 migrations/0007 设计说明。"
    )

    mapper = ReconciliationStateSnapshotModel.__mapper__
    assert mapper.version_id_col is None, (
        "ReconciliationStateSnapshotModel 不应注册 version_id_col。详见 migrations/0007。"
    )


# ─────────────────────────────────────────────────────────────────────
# 2) Repo 层：save_state_snapshot 走 pg_insert + ON CONFLICT 路径
# ─────────────────────────────────────────────────────────────────────


def test_reconciliation_repo_save_state_snapshot_uses_on_conflict_do_nothing() -> None:
    """save_state_snapshot 必须用 pg_insert(...).on_conflict_do_nothing(...)。

    这条测试做静态源码检查：pg_insert 是 Postgres 方言专属语法，无法在 SQLite
    in-memory 测试里执行；走静态检查比拉一个 Postgres testcontainer 更快也更稳定。
    Postgres 端到端的「同一份 snapshot 写两次只成功一次」由集成测试覆盖。
    """
    source = inspect.getsource(PostgresReconciliationRepository.save_state_snapshot)

    assert "pg_insert(" in source, (
        "save_state_snapshot 必须使用 pg_insert（不能 fallback 到 session.add，"
        "否则会失去 ON CONFLICT 幂等性）"
    )
    assert "on_conflict_do_nothing" in source, (
        "save_state_snapshot 必须使用 on_conflict_do_nothing 子句"
    )
    assert 'index_elements=["snapshot_id"]' in source, (
        "on_conflict_do_nothing 必须显式按 snapshot_id PK 指定冲突列，避免误命中其他唯一索引"
    )

    # 模块级 import 也必须有 pg_insert，确保上面的引用可解析
    module_source = inspect.getsource(reconciliation_repo_postgres)
    assert "from sqlalchemy.dialects.postgresql import insert as pg_insert" in module_source, (
        "reconciliation_repo_postgres 必须从 sqlalchemy.dialects.postgresql 导入 insert as pg_insert"
    )


def test_reconciliation_repo_save_state_snapshot_no_session_add() -> None:
    """回归守卫：save_state_snapshot 不应当再走 session.add 旧路径。"""
    source = inspect.getsource(PostgresReconciliationRepository.save_state_snapshot)
    assert "session.add(" not in source, (
        "save_state_snapshot 不应当再走 session.add 旧路径（否则重复 snapshot 会撞 PK 抛 IntegrityError）"
    )
