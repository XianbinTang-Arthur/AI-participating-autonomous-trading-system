"""db_create_task_if_idle 单元回归：关闭 has_active_task → create_task 的 TOCTOU.

旧路径 = 两条 SQL：
  1. SELECT ... WHERE workflow=? AND status IN ('pending','running')
  2. INSERT INTO rdp_task_queue ...

API handler 与 scheduler 在高并发下可能同时通过 step 1，再双双 INSERT；第二
次 INSERT 会撞上 ``ix_rdp_task_one_active_per_workflow`` (partial unique on
workflow WHERE status IN ('pending','running')) 抛 IntegrityError，被上层
except Exception 抹平成 "创建任务失败" 的误导错误。

新路径把判断+插入收敛到 INSERT ... ON CONFLICT DO NOTHING RETURNING，用
partial unique index 的冲突语义直接吸收 race：
  * 抢到索引 → RETURNING 返回 task_id → (task_id, None).
  * 冲突 → RETURNING 为空 → 回查现有 active task → (None, existing_dict).

本测试用 FakeSession 锁定 SQL 调用形状和返回契约，不走 testcontainers。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from aats.data_platform.governance.rdp_task_db import (
    VALID_WORKFLOWS,
    db_create_task_if_idle,
)


class _FakeRow:
    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            setattr(self, key, value)


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def fetchone(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """仅覆盖 db_create_task_if_idle 触达的三条 SQL 形态：
      * INSERT INTO governance.rdp_task_queue ... ON CONFLICT (workflow) WHERE ...
      * SELECT task_id, status ... FROM rdp_task_queue WHERE workflow = ... AND status IN (...)
    """

    def __init__(
        self,
        *,
        insert_succeeds: bool = True,
        existing_active: dict[str, Any] | None = None,
    ) -> None:
        self.insert_succeeds = insert_succeeds
        self.existing_active = existing_active
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()
        self.statements.append((sql, dict(params or {})))

        if sql.startswith("INSERT INTO governance.rdp_task_queue"):
            # 契约校验：SQL 里必须出现 ON CONFLICT ... DO NOTHING 和 RETURNING，
            # 否则就回到了 has_active_task → insert 的旧 TOCTOU 路径。
            assert "ON CONFLICT" in sql, (
                "db_create_task_if_idle 的 INSERT 必须带 ON CONFLICT；"
                "否则并发写会退化成 IntegrityError 路径"
            )
            assert "DO NOTHING" in sql, (
                "ON CONFLICT 分支必须是 DO NOTHING；DO UPDATE 会重写已有活跃任务"
            )
            assert "RETURNING task_id" in sql, (
                "需要 RETURNING task_id 用作 "
                "\"成功/已有冲突\" 的唯一区分信号"
            )
            # 必须以 partial unique index 的谓词匹配
            assert re.search(
                r"ON CONFLICT \(workflow\) WHERE status IN \('pending', 'running'\)",
                sql,
            ), (
                "ON CONFLICT 谓词必须与 ix_rdp_task_one_active_per_workflow "
                "的 WHERE 精确匹配，否则 PostgreSQL 无法选中该 partial index"
            )

            if self.insert_succeeds:
                return _FakeResult([_FakeRow({"task_id": (params or {}).get("task_id")})])
            # ON CONFLICT DO NOTHING → 不返回行
            return _FakeResult([])

        if sql.startswith("SELECT task_id, status"):
            # db_has_active_task 的查询
            if self.existing_active is not None:
                return _FakeResult([_FakeRow(self.existing_active)])
            return _FakeResult([])

        raise AssertionError(f"Unexpected SQL: {sql[:100]}...")


# =====================================================================
# Happy path
# =====================================================================


def test_insert_succeeds_returns_task_id_and_no_existing() -> None:
    session = _FakeSession(insert_succeeds=True)

    task_id, existing = db_create_task_if_idle(session, workflow="research_cycle")

    assert task_id is not None, "insert 成功必须返回非空 task_id"
    assert task_id.startswith("task_"), "task_id 必须保留 task_<hex12> 前缀契约"
    assert existing is None, "insert 成功不应回查 existing"
    # 仅发一条 INSERT，不回查
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["INSERT"], (
        f"成功路径只应发一条 INSERT，实际: {sql_types}"
    )


# =====================================================================
# Conflict path — ON CONFLICT DO NOTHING 命中 existing active
# =====================================================================


def test_conflict_returns_none_task_id_and_queries_existing() -> None:
    """INSERT 被 partial unique index 拦下 → 回查 existing 并返回给 caller."""
    existing = {
        "task_id": "task_existing_abc",
        "status": "running",
        "requested_at": None,
        "started_at": None,
    }
    session = _FakeSession(insert_succeeds=False, existing_active=existing)

    task_id, returned_existing = db_create_task_if_idle(
        session, workflow="research_cycle",
    )

    assert task_id is None, "冲突路径 task_id 必须为 None"
    assert returned_existing is not None
    assert returned_existing["task_id"] == "task_existing_abc"
    assert returned_existing["status"] == "running"

    # 契约：INSERT → 冲突 → SELECT 回查，共两条 SQL
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["INSERT", "SELECT"], (
        f"冲突路径：一条 INSERT + 一条回查 SELECT，实际: {sql_types}"
    )


def test_conflict_without_existing_row_returns_none_existing() -> None:
    """极罕见 race：INSERT 冲突但回查时刚好那行又被清掉 → existing=None，
    caller 需自行兜底（UI 文案已有 fallback）。
    """
    session = _FakeSession(insert_succeeds=False, existing_active=None)

    task_id, existing = db_create_task_if_idle(session, workflow="release_cycle")

    assert task_id is None
    assert existing is None


# =====================================================================
# Invalid workflow rejection
# =====================================================================


def test_invalid_workflow_raises_before_touching_db() -> None:
    """非合法 workflow 必须立刻 raise，不能先插再回滚（浪费 WAL）。"""
    session = _FakeSession(insert_succeeds=True)

    with pytest.raises(ValueError, match="Invalid workflow"):
        db_create_task_if_idle(session, workflow="not_a_workflow")

    assert session.statements == [], (
        "workflow 校验失败时不应触达 DB"
    )


def test_all_valid_workflows_accepted() -> None:
    """契约：VALID_WORKFLOWS 里的每个值都应能被接受。

    若后续 VALID_WORKFLOWS 删除项要同步更新 migration / scheduler tests。
    """
    for wf in VALID_WORKFLOWS:
        session = _FakeSession(insert_succeeds=True)
        task_id, _ = db_create_task_if_idle(session, workflow=wf)
        assert task_id is not None, f"workflow={wf} 应插入成功"


# =====================================================================
# 参数绑定契约
# =====================================================================


def test_requested_by_is_bound_into_insert_params() -> None:
    session = _FakeSession(insert_succeeds=True)

    db_create_task_if_idle(
        session, workflow="decision_cycle", requested_by="scheduler_daemon",
    )

    insert_sql, params = session.statements[0]
    assert insert_sql.startswith("INSERT INTO governance.rdp_task_queue")
    assert params["requested_by"] == "scheduler_daemon"
    assert params["workflow"] == "decision_cycle"
