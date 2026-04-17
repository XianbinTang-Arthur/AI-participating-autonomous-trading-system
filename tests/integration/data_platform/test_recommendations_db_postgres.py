"""M-R3：governance.recommendations DB API 的真 Postgres round-trip。

覆盖 P0-1 阶段 A 新增 / 对齐的 DB 接口：

- ``db_upsert_recommendation``        — INSERT ... ON CONFLICT DO UPDATE 幂等
- ``db_get_recommendation``           — 按 id 读单条（别名 ``db_find_recommendation``）
- ``db_list_recommendations``         — 带 filter + LIMIT/OFFSET 分页
- ``db_count_recommendations``        — 带 filter 的 COUNT(*)
- ``db_transition_recommendation_status``
      — ``UPDATE ... WHERE status IN (...)`` CAS：真 Postgres 上验证 rowcount 行为

单测里有 ``_FakeRecSession`` 的 SQL 子集模拟（``test_recommendations_db.py``），
但 Fake session 没法证明：

  1. governance schema 在 fresh DB 上能幂等创建（``create_rdp_schema`` 调了
     ``_migrate_governance_recommendations``）
  2. ``ON CONFLICT (recommendation_id) DO UPDATE`` 和 ``UPDATE ... WHERE status IN``
     的 SQL 真的跑在 Postgres 上（方言 / 约束 / 类型都对）
  3. 分页 / COUNT 的 ``ORDER BY created_at DESC`` 能按索引走，而不是顺序扫描
     （这里只验证结果正确，性能靠 Grafana / EXPLAIN 手工盯）

**运行条件**：
- 需要 docker daemon
- 需要 ``pip install -e .[postgres-integration]`` 或等价的 testcontainers + psycopg2
- 需要环境变量 ``AATS_RUN_POSTGRES_INTEGRATION=1``

WSL2 上推荐入口：

    AATS_RUN_POSTGRES_INTEGRATION=1 pytest \
        tests/integration/data_platform/test_recommendations_db_postgres.py -x -q
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

# 软依赖检查：testcontainers + psycopg2 可能没装
try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover - 没装就跳过
    PostgresContainer = None  # type: ignore[assignment,misc]
    _TESTCONTAINERS_AVAILABLE = False

try:
    import psycopg2  # type: ignore[import-not-found]  # noqa: F401

    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PSYCOPG2_AVAILABLE = False


_INTEGRATION_ENV_FLAG = "AATS_RUN_POSTGRES_INTEGRATION"
_SHOULD_RUN = (
    os.getenv(_INTEGRATION_ENV_FLAG) == "1"
    and _TESTCONTAINERS_AVAILABLE
    and _PSYCOPG2_AVAILABLE
)


def _make_rec_payload(
    rec_id: str,
    *,
    status: str = "draft",
    family: str = "independent",
    timeframe: str = "15m",
    rec_type: str = "parameter_upgrade",
    created_at: str | None = None,
) -> dict:
    return {
        "recommendation_id": rec_id,
        "family": family,
        "symbol": "BTC-USDT-SWAP",
        "timeframe": timeframe,
        "recommendation_type": rec_type,
        "target_parameter_set_id": "ps_candidate_1",
        "confidence": "high",
        "reason": "M-R3 roundtrip",
        "evidence_bundle_ref": None,
        "status": status,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[postgres-integration] to run",
)
class TestRecommendationsDbPostgresRoundTrip(unittest.TestCase):
    """governance.recommendations DB API 端到端 round-trip。"""

    container: "PostgresContainer"
    engine: object

    @classmethod
    def setUpClass(cls) -> None:
        # postgres:16 与 WSL2 dev stack (aats-postgres) 版本一致
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

        from sqlalchemy import create_engine

        from aats.data_platform.rdp_models import create_rdp_schema

        url = cls.container.get_connection_url()
        # testcontainers 默认返回 postgresql+psycopg2:// 或 postgresql://，
        # SQLAlchemy 2.0 两者都认
        cls.engine = create_engine(url, future=True)
        create_rdp_schema(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine") and cls.engine is not None:
            cls.engine.dispose()  # type: ignore[attr-defined]
        if hasattr(cls, "container") and cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        # 每个测试开始前清空 recommendations 表（governance schema 已由 setUpClass 建好）
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(text("TRUNCATE TABLE governance.recommendations"))

    # ------------------------------------------------------------------
    # upsert + get
    # ------------------------------------------------------------------

    def test_upsert_then_get_roundtrip(self) -> None:
        """插入后按 id 取回，字段 round-trip 不丢。"""
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_get_recommendation,
            db_upsert_recommendation,
        )

        payload = _make_rec_payload("rec_m_r3_roundtrip")
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(session, **payload)

        with Session(self.engine) as session:  # type: ignore[arg-type]
            row = db_get_recommendation(session, "rec_m_r3_roundtrip")

        self.assertIsNotNone(row)
        assert row is not None  # for type checkers
        self.assertEqual(row["recommendation_id"], "rec_m_r3_roundtrip")
        self.assertEqual(row["family"], "independent")
        self.assertEqual(row["timeframe"], "15m")
        self.assertEqual(row["recommendation_type"], "parameter_upgrade")
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["confidence"], "high")

    def test_upsert_is_idempotent_on_conflict(self) -> None:
        """重复 upsert 同一个 recommendation_id 不报错，字段被刷新。"""
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_get_recommendation,
            db_upsert_recommendation,
        )

        first = _make_rec_payload("rec_m_r3_conflict")
        first["reason"] = "first version"
        second = _make_rec_payload("rec_m_r3_conflict")
        second["reason"] = "second version"

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(session, **first)
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(session, **second)

        with Session(self.engine) as session:  # type: ignore[arg-type]
            row = db_get_recommendation(session, "rec_m_r3_conflict")

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["reason"], "second version")

    def test_get_returns_none_for_unknown_id(self) -> None:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import db_get_recommendation

        with Session(self.engine) as session:  # type: ignore[arg-type]
            row = db_get_recommendation(session, "rec_nonexistent")

        self.assertIsNone(row)

    # ------------------------------------------------------------------
    # db_transition_recommendation_status — CAS
    # ------------------------------------------------------------------

    def test_transition_draft_to_approved_succeeds(self) -> None:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_get_recommendation,
            db_transition_recommendation_status,
            db_upsert_recommendation,
        )

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(session, **_make_rec_payload("rec_m_r3_approve"))

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            ok = db_transition_recommendation_status(
                session,
                recommendation_id="rec_m_r3_approve",
                new_status="approved",
                expected_current_status="draft",
                actor="operator_alice",
                at=datetime.now(timezone.utc),
                notes="CAS happy path",
            )
        self.assertTrue(ok)

        with Session(self.engine) as session:  # type: ignore[arg-type]
            row = db_get_recommendation(session, "rec_m_r3_approve")
        assert row is not None
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["approved_by"], "operator_alice")
        self.assertEqual(row["review_notes"], "CAS happy path")

    def test_transition_with_wrong_expected_status_returns_false(self) -> None:
        """CAS 前置失败：rec 现在是 approved，期望 draft，不能改写。"""
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_get_recommendation,
            db_transition_recommendation_status,
            db_upsert_recommendation,
        )

        payload = _make_rec_payload("rec_m_r3_cas_miss", status="approved")
        payload["approved_by"] = "earlier_operator"
        payload["approved_at"] = datetime.now(timezone.utc).isoformat()
        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(session, **payload)

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            ok = db_transition_recommendation_status(
                session,
                recommendation_id="rec_m_r3_cas_miss",
                new_status="rejected",
                expected_current_status="draft",  # 故意错
                actor="operator_bob",
                at=datetime.now(timezone.utc),
                notes="should not land",
            )
        self.assertFalse(ok)

        # DB 里应当保持 approved，而不是被错误地覆盖为 rejected
        with Session(self.engine) as session:  # type: ignore[arg-type]
            row = db_get_recommendation(session, "rec_m_r3_cas_miss")
        assert row is not None
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["approved_by"], "earlier_operator")

    def test_transition_accepts_tuple_of_expected_statuses(self) -> None:
        """supersede 允许 ``(draft, approved)`` 两种起点；真 DB 上 IN 子句要正确。"""
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_get_recommendation,
            db_transition_recommendation_status,
            db_upsert_recommendation,
        )

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            db_upsert_recommendation(
                session,
                **_make_rec_payload("rec_m_r3_supersede_from_approved", status="approved"),
            )

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            ok = db_transition_recommendation_status(
                session,
                recommendation_id="rec_m_r3_supersede_from_approved",
                new_status="superseded",
                expected_current_status=("draft", "approved"),
                actor="operator_carol",
                at=datetime.now(timezone.utc),
                superseded_by_recommendation_id="rec_successor_1",
            )
        self.assertTrue(ok)

        with Session(self.engine) as session:  # type: ignore[arg-type]
            row = db_get_recommendation(session, "rec_m_r3_supersede_from_approved")
        assert row is not None
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(row["superseded_by_recommendation_id"], "rec_successor_1")

    def test_transition_returns_false_for_unknown_recommendation(self) -> None:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_transition_recommendation_status,
        )

        with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
            ok = db_transition_recommendation_status(
                session,
                recommendation_id="rec_does_not_exist",
                new_status="approved",
                expected_current_status="draft",
                actor="operator_dave",
                at=datetime.now(timezone.utc),
            )
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # list + count
    # ------------------------------------------------------------------

    def test_list_and_count_with_filters(self) -> None:
        """按 family + status 过滤，LIMIT/OFFSET 分页走 created_at DESC。"""
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_count_recommendations,
            db_list_recommendations,
            db_upsert_recommendation,
        )

        base = datetime.now(timezone.utc) - timedelta(hours=1)
        seeded = [
            ("rec_ind_draft_a", "draft", "independent", "15m", base),
            ("rec_ind_draft_b", "draft", "independent", "1h", base + timedelta(minutes=5)),
            ("rec_ind_draft_c", "draft", "independent", "4h", base + timedelta(minutes=10)),
            ("rec_ind_approved", "approved", "independent", "15m", base + timedelta(minutes=15)),
            ("rec_dir_draft", "draft", "directional", "15m", base + timedelta(minutes=20)),
        ]
        for rec_id, status, family, timeframe, created in seeded:
            payload = _make_rec_payload(
                rec_id,
                status=status,
                family=family,
                timeframe=timeframe,
                created_at=created.isoformat(),
            )
            with Session(self.engine) as session, session.begin():  # type: ignore[arg-type]
                db_upsert_recommendation(session, **payload)

        # 1) 只过 family=independent + status=draft → 3 条
        with Session(self.engine) as session:  # type: ignore[arg-type]
            count = db_count_recommendations(session, family="independent", status="draft")
            page_all = db_list_recommendations(
                session, family="independent", status="draft",
            )
        self.assertEqual(count, 3)
        self.assertEqual(len(page_all), 3)
        # ORDER BY created_at DESC：最新的在前
        ids = [row["recommendation_id"] for row in page_all]
        self.assertEqual(ids, ["rec_ind_draft_c", "rec_ind_draft_b", "rec_ind_draft_a"])

        # 2) LIMIT / OFFSET 分页
        with Session(self.engine) as session:  # type: ignore[arg-type]
            first_page = db_list_recommendations(
                session, family="independent", status="draft", limit=2, offset=0,
            )
            second_page = db_list_recommendations(
                session, family="independent", status="draft", limit=2, offset=2,
            )
        self.assertEqual(
            [r["recommendation_id"] for r in first_page],
            ["rec_ind_draft_c", "rec_ind_draft_b"],
        )
        self.assertEqual(
            [r["recommendation_id"] for r in second_page],
            ["rec_ind_draft_a"],
        )

        # 3) 不加 filter 时应当返回全部 5 条
        with Session(self.engine) as session:  # type: ignore[arg-type]
            self.assertEqual(db_count_recommendations(session), 5)

        # 4) 按 status=approved 单独过
        with Session(self.engine) as session:  # type: ignore[arg-type]
            approved = db_list_recommendations(session, status="approved")
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["recommendation_id"], "rec_ind_approved")
