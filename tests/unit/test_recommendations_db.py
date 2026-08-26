"""单测：governance.recommendations 的 CRUD / state transition / 分页 API。

P0-1 阶段 A 新增的 DB 层契约必须先在这里锁定，再往 rdp_routes.py 切流量。
采用轻量 ``_FakeRecSession``（不依赖 testcontainers）复现：

- ``INSERT ... ON CONFLICT (recommendation_id) DO UPDATE`` 的 upsert 语义
- ``UPDATE ... WHERE status IN (...)`` 的 CAS 语义与 rowcount
- ``SELECT ... ORDER BY created_at DESC LIMIT/OFFSET`` 的分页
- ``SELECT COUNT(*) ... WHERE ...`` 的统计

WSL2 testcontainers 路径负责跑真实 Postgres 的幂等 / migrate 验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aats.data_platform.governance.recommendations_db import (
    db_count_recommendations,
    db_find_recommendation,
    db_get_recommendation,
    db_list_recommendations,
    db_transition_recommendation_status,
    db_upsert_recommendation,
)


# =====================================================================
# Fake Session
# =====================================================================


class _FakeRow:
    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            setattr(self, key, value)


class _FakeResult:
    def __init__(self, rows: list[_FakeRow], rowcount: int | None = None) -> None:
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def fetchall(self) -> list[_FakeRow]:
        return list(self._rows)

    def fetchone(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None


_REC_COLUMNS: tuple[str, ...] = (
    "recommendation_id", "family", "symbol", "timeframe",
    "recommendation_type", "target_parameter_set_id",
    "source_round_id",
    "confidence", "reason", "evidence_bundle_ref",
    "status",
    "approved_by", "approved_at", "review_notes",
    "rejected_by", "rejected_at",
    "superseded_by", "superseded_at", "superseded_by_recommendation_id",
    "created_at",
)


class _FakeRecSession:
    """只覆盖本文件测试触达的 SQL 子集。

    维护一个 ``dict[recommendation_id, row]``；execute() 根据 SQL 前缀分派到
    _insert / _update / _count / _select。
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    # dispatcher --------------------------------------------------------

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()
        p = params or {}
        if sql.startswith("INSERT INTO governance.recommendations"):
            return self._insert(p)
        if sql.startswith("UPDATE governance.recommendations"):
            return self._update(sql, p)
        if sql.startswith("SELECT COUNT(*)"):
            return self._count(p)
        if sql.startswith("SELECT"):
            return self._select(sql, p)
        raise AssertionError(f"Unexpected SQL in fake rec session: {sql[:80]}...")

    # INSERT ... ON CONFLICT ... DO UPDATE ------------------------------

    def _insert(self, params: dict[str, Any]) -> _FakeResult:
        rid = params["rec_id"]
        row = {
            "recommendation_id": rid,
            "family": params.get("family"),
            "symbol": params.get("symbol"),
            "timeframe": params.get("timeframe"),
            "recommendation_type": params.get("rec_type"),
            "target_parameter_set_id": params.get("target_ps_id"),
            "source_round_id": params.get("source_round_id"),
            "confidence": params.get("confidence"),
            "reason": params.get("reason"),
            "evidence_bundle_ref": params.get("evidence_ref"),
            "status": params.get("status"),
            "approved_by": params.get("approved_by"),
            "approved_at": params.get("approved_at"),
            "review_notes": params.get("review_notes"),
            "rejected_by": params.get("rejected_by"),
            "rejected_at": params.get("rejected_at"),
            "superseded_by": params.get("superseded_by"),
            "superseded_at": params.get("superseded_at"),
            "superseded_by_recommendation_id": params.get("superseded_by_rec_id"),
            "created_at": params.get("created_at"),
        }
        # EXCLUDED.* 赋值 → 每列一律用新值覆盖（这里的测试不涉及 COALESCE 语义）
        existing = self.rows.get(rid, {})
        existing.update(row)
        self.rows[rid] = existing
        return _FakeResult([])

    # UPDATE ... WHERE recommendation_id = :rec_id AND status ... -------

    def _update(self, sql: str, params: dict[str, Any]) -> _FakeResult:
        rid = params["rec_id"]
        existing = self.rows.get(rid)
        if existing is None:
            return _FakeResult([], rowcount=0)

        expected_set: set[str] | None = None
        if "expected_status" in params:
            expected_set = {params["expected_status"]}
        else:
            exp_keys = [k for k in params if k.startswith("expected_status_")]
            if exp_keys:
                expected_set = {params[k] for k in exp_keys}
        if expected_set is not None and existing.get("status") not in expected_set:
            return _FakeResult([], rowcount=0)

        # SET 子句解析：形如 "col = :param" 列表
        set_clause = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        for assignment in set_clause.split(","):
            assignment = assignment.strip()
            col, _, ref = assignment.partition("=")
            col = col.strip()
            ref = ref.strip().lstrip(":")
            if ref in params:
                existing[col] = params[ref]
        return _FakeResult([], rowcount=1)

    # SELECT COUNT(*) ---------------------------------------------------

    def _count(self, params: dict[str, Any]) -> _FakeResult:
        matched = [r for r in self.rows.values() if self._matches_filter(r, params)]
        return _FakeResult([_FakeRow({"cnt": len(matched)})])

    # SELECT (both find by id and list) ---------------------------------

    def _select(self, sql: str, params: dict[str, Any]) -> _FakeResult:
        if "rec_id" in params:
            # 单条 by id
            row = self.rows.get(params["rec_id"])
            rows = [row] if row else []
        else:
            rows = [r for r in self.rows.values() if self._matches_filter(r, params)]
            rows.sort(
                key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                reverse="DESC" in sql.upper(),
            )
            if "limit" in params:
                offset = params.get("offset", 0)
                rows = rows[offset:offset + params["limit"]]
        fake_rows = [
            _FakeRow({col: r.get(col) for col in _REC_COLUMNS})
            for r in rows
        ]
        return _FakeResult(fake_rows)

    @staticmethod
    def _matches_filter(row: dict[str, Any], params: dict[str, Any]) -> bool:
        if "family" in params and row.get("family") != params["family"]:
            return False
        if "timeframe" in params and row.get("timeframe") != params["timeframe"]:
            return False
        if "status" in params and row.get("status") != params["status"]:
            return False
        if "rec_type" in params and row.get("recommendation_type") != params["rec_type"]:
            return False
        return True


# =====================================================================
# Helpers
# =====================================================================


def _seed_draft(
    session: _FakeRecSession,
    rec_id: str,
    *,
    family: str = "independent",
    timeframe: str = "15m",
    created_at: datetime | None = None,
    recommendation_type: str = "parameter_upgrade",
) -> None:
    """播一条 draft recommendation 用于测试。"""
    db_upsert_recommendation(
        session,  # type: ignore[arg-type]
        recommendation_id=rec_id,
        family=family,
        timeframe=timeframe,
        recommendation_type=recommendation_type,
        confidence="high",
        reason="seeded for test",
        target_parameter_set_id=None,
        evidence_bundle_ref="round_xyz",
        status="draft",
        created_at=(created_at or datetime.now(timezone.utc)).isoformat(),
    )


# =====================================================================
# upsert
# =====================================================================


def test_upsert_recommendation_inserts_new_row() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    assert "rec_A" in session.rows
    assert session.rows["rec_A"]["status"] == "draft"
    assert session.rows["rec_A"]["family"] == "independent"


def test_upsert_recommendation_round_trips_source_round_id() -> None:
    session = _FakeRecSession()
    db_upsert_recommendation(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_lineage",
        family="independent",
        timeframe="15m",
        recommendation_type="parameter_upgrade",
        confidence="high",
        reason="lineage test",
        target_parameter_set_id="ps_lineage",
        source_round_id="round_source_001",
    )

    row = db_get_recommendation(session, "rec_lineage")  # type: ignore[arg-type]

    assert row is not None
    assert row["source_round_id"] == "round_source_001"


def test_upsert_sql_does_not_erase_existing_source_round() -> None:
    class _CaptureSession:
        statement = ""

        def execute(self, statement, _params):
            self.statement = str(statement)

    session = _CaptureSession()
    db_upsert_recommendation(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_lineage",
        family="independent",
        timeframe="15m",
        recommendation_type="parameter_upgrade",
        confidence="high",
        reason="lineage test",
        source_round_id=None,
    )

    assert "source_round_id                = COALESCE(" in session.statement
    assert "governance.recommendations.source_round_id" in session.statement


def test_upsert_recommendation_overwrites_existing_fields() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A", family="independent", timeframe="15m")
    db_upsert_recommendation(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_A",
        family="independent",
        timeframe="15m",
        recommendation_type="parameter_upgrade",
        confidence="medium",  # 变了
        reason="updated reason",
        status="draft",
    )
    assert session.rows["rec_A"]["confidence"] == "medium"
    assert session.rows["rec_A"]["reason"] == "updated reason"


def test_upsert_recommendation_rejects_invalid_status() -> None:
    session = _FakeRecSession()
    with pytest.raises(ValueError, match="非法 recommendation status"):
        db_upsert_recommendation(
            session,  # type: ignore[arg-type]
            recommendation_id="rec_bad",
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="",
            status="bogus",
        )


# =====================================================================
# db_transition_recommendation_status — CAS hits
# =====================================================================


def test_transition_draft_to_approved_sets_approved_fields() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    updated = db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_A",
        new_status="approved",
        expected_current_status="draft",
        actor="alice",
        at="2026-04-17T09:30:00+00:00",
        notes="looks good",
    )
    assert updated is True
    row = session.rows["rec_A"]
    assert row["status"] == "approved"
    assert row["approved_by"] == "alice"
    assert row["review_notes"] == "looks good"
    assert isinstance(row["approved_at"], datetime)
    # reject-侧字段不能被动过
    assert row["rejected_by"] is None
    assert row["superseded_by"] is None


def test_transition_draft_to_rejected_sets_rejected_fields() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    updated = db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_A",
        new_status="rejected",
        expected_current_status="draft",
        actor="bob",
        notes="bad evidence",
    )
    assert updated is True
    row = session.rows["rec_A"]
    assert row["status"] == "rejected"
    assert row["rejected_by"] == "bob"
    assert row["review_notes"] == "bad evidence"
    assert row["approved_by"] is None


def test_transition_to_superseded_records_replacement_id() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_OLD")
    updated = db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_OLD",
        new_status="superseded",
        expected_current_status=("draft", "approved"),
        actor="system",
        superseded_by_recommendation_id="rec_NEW",
    )
    assert updated is True
    row = session.rows["rec_OLD"]
    assert row["status"] == "superseded"
    assert row["superseded_by"] == "system"
    assert row["superseded_by_recommendation_id"] == "rec_NEW"


def test_transition_uses_explicit_at_timestamp() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    updated = db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_A",
        new_status="approved",
        expected_current_status="draft",
        actor="alice",
        at="2026-04-17T10:00:00+00:00",
    )
    assert updated is True
    approved_at = session.rows["rec_A"]["approved_at"]
    assert isinstance(approved_at, datetime)
    assert approved_at == datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)


# =====================================================================
# db_transition_recommendation_status — CAS misses
# =====================================================================


def test_transition_cas_miss_on_wrong_current_status_returns_false() -> None:
    """rec 已经是 approved，再尝试 draft→approved 必须 False 而不是幂等通过。"""
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    # 先用一次合法转移把它推到 approved
    db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_A",
        new_status="approved",
        expected_current_status="draft",
        actor="alice",
    )
    # 第二个 operator 再试一次 draft→approved，应该 CAS 未命中
    again = db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_A",
        new_status="approved",
        expected_current_status="draft",
        actor="carol",
    )
    assert again is False
    # 原 approver 不能被第二次调用覆写
    assert session.rows["rec_A"]["approved_by"] == "alice"


def test_transition_cas_miss_on_nonexistent_rec_returns_false() -> None:
    session = _FakeRecSession()
    updated = db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_ghost",
        new_status="approved",
        expected_current_status="draft",
        actor="alice",
    )
    assert updated is False


def test_transition_rejects_invalid_new_status() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    with pytest.raises(ValueError, match="非法 recommendation status"):
        db_transition_recommendation_status(
            session,  # type: ignore[arg-type]
            recommendation_id="rec_A",
            new_status="bogus",
            expected_current_status="draft",
            actor="alice",
        )


# =====================================================================
# db_get_recommendation
# =====================================================================


def test_get_recommendation_returns_row_when_exists() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    rec = db_get_recommendation(session, "rec_A")  # type: ignore[arg-type]
    assert rec is not None
    assert rec["recommendation_id"] == "rec_A"
    assert rec["status"] == "draft"


def test_get_recommendation_returns_none_for_missing_id() -> None:
    session = _FakeRecSession()
    assert db_get_recommendation(session, "rec_ghost") is None  # type: ignore[arg-type]


def test_get_recommendation_is_alias_for_find() -> None:
    """db_get_recommendation 必须保持与 db_find_recommendation 行为一致。"""
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    assert (
        db_get_recommendation(session, "rec_A")  # type: ignore[arg-type]
        == db_find_recommendation(session, "rec_A")  # type: ignore[arg-type]
    )


# =====================================================================
# db_list_recommendations — filter / pagination / order
# =====================================================================


def test_list_recommendations_orders_by_created_at_desc() -> None:
    session = _FakeRecSession()
    base = datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc)
    _seed_draft(session, "rec_OLD", created_at=base)
    _seed_draft(session, "rec_MID", created_at=base + timedelta(hours=1))
    _seed_draft(session, "rec_NEW", created_at=base + timedelta(hours=2))

    rows = db_list_recommendations(session)  # type: ignore[arg-type]
    assert [r["recommendation_id"] for r in rows] == ["rec_NEW", "rec_MID", "rec_OLD"]


def test_list_recommendations_filters_by_status() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_draft_1")
    _seed_draft(session, "rec_draft_2")
    _seed_draft(session, "rec_approved")
    db_transition_recommendation_status(
        session,  # type: ignore[arg-type]
        recommendation_id="rec_approved",
        new_status="approved",
        expected_current_status="draft",
        actor="alice",
    )
    drafts = db_list_recommendations(session, status="draft")  # type: ignore[arg-type]
    approved = db_list_recommendations(session, status="approved")  # type: ignore[arg-type]
    assert {r["recommendation_id"] for r in drafts} == {"rec_draft_1", "rec_draft_2"}
    assert [r["recommendation_id"] for r in approved] == ["rec_approved"]


def test_list_recommendations_filters_by_family_and_timeframe() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_ind_15m", family="independent", timeframe="15m")
    _seed_draft(session, "rec_ind_1h", family="independent", timeframe="1h")
    _seed_draft(session, "rec_clust_15m", family="clustered", timeframe="15m")

    ind_only = db_list_recommendations(session, family="independent")  # type: ignore[arg-type]
    tf_only = db_list_recommendations(session, timeframe="15m")  # type: ignore[arg-type]
    assert {r["recommendation_id"] for r in ind_only} == {"rec_ind_15m", "rec_ind_1h"}
    assert {r["recommendation_id"] for r in tf_only} == {"rec_ind_15m", "rec_clust_15m"}


def test_list_recommendations_filters_by_recommendation_type() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_upgrade", recommendation_type="parameter_upgrade")
    _seed_draft(session, "rec_review", recommendation_type="require_review")
    upgrades = db_list_recommendations(
        session,  # type: ignore[arg-type]
        recommendation_type="parameter_upgrade",
    )
    assert [r["recommendation_id"] for r in upgrades] == ["rec_upgrade"]


def test_list_recommendations_respects_limit_and_offset() -> None:
    session = _FakeRecSession()
    base = datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        _seed_draft(session, f"rec_{i}", created_at=base + timedelta(minutes=i))

    # 最新两条
    page1 = db_list_recommendations(session, limit=2, offset=0)  # type: ignore[arg-type]
    assert [r["recommendation_id"] for r in page1] == ["rec_4", "rec_3"]
    # 接下来两条
    page2 = db_list_recommendations(session, limit=2, offset=2)  # type: ignore[arg-type]
    assert [r["recommendation_id"] for r in page2] == ["rec_2", "rec_1"]
    # 末尾
    page3 = db_list_recommendations(session, limit=2, offset=4)  # type: ignore[arg-type]
    assert [r["recommendation_id"] for r in page3] == ["rec_0"]


def test_list_recommendations_rejects_negative_pagination() -> None:
    session = _FakeRecSession()
    with pytest.raises(ValueError, match="limit 必须 >= 0"):
        db_list_recommendations(session, limit=-1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="offset 必须 >= 0"):
        db_list_recommendations(session, offset=-1)  # type: ignore[arg-type]


# =====================================================================
# db_count_recommendations
# =====================================================================


def test_count_recommendations_returns_zero_for_no_matches() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_A")
    assert db_count_recommendations(session, status="approved") == 0  # type: ignore[arg-type]


def test_count_recommendations_matches_filters() -> None:
    session = _FakeRecSession()
    _seed_draft(session, "rec_ind_1", family="independent", timeframe="15m")
    _seed_draft(session, "rec_ind_2", family="independent", timeframe="1h")
    _seed_draft(session, "rec_clust_1", family="clustered", timeframe="15m")
    assert db_count_recommendations(session) == 3  # type: ignore[arg-type]
    assert db_count_recommendations(session, family="independent") == 2  # type: ignore[arg-type]
    assert db_count_recommendations(session, timeframe="1h") == 1  # type: ignore[arg-type]
    assert db_count_recommendations(  # type: ignore[arg-type]
        session, family="independent", timeframe="15m",
    ) == 1
