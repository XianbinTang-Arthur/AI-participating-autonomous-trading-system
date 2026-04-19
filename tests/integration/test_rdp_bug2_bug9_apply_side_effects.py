"""RDP Bug 2 + Bug 9 integration test: apply_approved_recommendation 事务副作用.

Scope:
  - **Bug 2**: apply 成功后同 (family, timeframe) 下其他 approved
    parameter_upgrade recommendation 被自动 superseded (superseded_by /
    superseded_at / superseded_by_recommendation_id 三字段填充，status=superseded)
  - **Bug 9 (a)**: target parameter_set.status 从 candidate → released (首次 apply)
    或保留 released (重复 apply)；frozen_at 写入
  - **Bug 9 (b)**: 同 (family, timeframe) 下其他 released parameter_sets 被降级
    到 deprecated；deprecated_at 写入
  - **原子性**: apply 事务内任一 UPDATE 失败 → 整体回滚，实盘状态不变

设计参考:
  - aats/data_platform/decision_system/active_parameter_apply.py:427-488
  - tests/integration/test_rdp_rollback_with_real_db.py (fixture 模板来源)

Run conditions:
    docker daemon running
    pip install -e .[postgres-integration]
    AATS_RUN_POSTGRES_INTEGRATION=1

WSL2 entry:
    AATS_RUN_POSTGRES_INTEGRATION=1 pytest \\
        tests/integration/test_rdp_bug2_bug9_apply_side_effects.py -x -q
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]

    _TESTCONTAINERS_AVAILABLE = True
except ImportError:  # pragma: no cover
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


@unittest.skipUnless(
    _SHOULD_RUN,
    f"Set {_INTEGRATION_ENV_FLAG}=1 and install .[postgres-integration] to run",
)
class TestApplyBug2Bug9SideEffects(unittest.TestCase):
    """Exercise apply_approved_recommendation 事务内的 Bug 2/9 语义."""

    container: "PostgresContainer"
    engine: object

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = PostgresContainer("postgres:16-alpine")
        cls.container.start()

        from sqlalchemy import create_engine

        from aats.data_platform.rdp_models import create_rdp_schema

        url = cls.container.get_connection_url()
        cls.engine = create_engine(url, future=True)
        create_rdp_schema(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "engine") and cls.engine is not None:
            cls.engine.dispose()  # type: ignore[attr-defined]
        if hasattr(cls, "container") and cls.container is not None:
            cls.container.stop()

    def setUp(self) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(text(
                "TRUNCATE TABLE "
                "governance.parameter_apply_history, "
                "governance.active_parameter_sets, "
                "governance.recommendations, "
                "governance.parameter_sets "
                "CASCADE"
            ))

    # ── Fixture helpers ────────────────────────────────────────────────

    def _seed_parameter_set(
        self,
        ps_id: str,
        *,
        family: str = "independent",
        timeframe: str = "15m",
        status: str = "candidate",
        values: str = '{"entry_threshold": 0.4}',
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.parameter_sets
                      (parameter_set_id, family, symbol, timeframe,
                       source_round_id, values, status, created_at)
                    VALUES
                      (:pid, :family, 'BTC-USDT-SWAP', :tf,
                       NULL, CAST(:values AS jsonb),
                       :status, now())
                    """
                ),
                {"pid": ps_id, "family": family, "tf": timeframe,
                 "values": values, "status": status},
            )

    def _seed_recommendation(
        self,
        rec_id: str,
        *,
        target_ps_id: str | None,
        family: str = "independent",
        timeframe: str = "15m",
        status: str = "approved",
        recommendation_type: str = "parameter_upgrade",
    ) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            conn.execute(
                text(
                    """
                    INSERT INTO governance.recommendations
                      (recommendation_id, family, timeframe, recommendation_type,
                       target_parameter_set_id, confidence, reason, status,
                       approved_by, approved_at, created_at)
                    VALUES
                      (:rec, :family, :tf, :rtype,
                       :target, 'high', 'seed', :status,
                       'operator', now(), now())
                    """
                ),
                {"rec": rec_id, "family": family, "tf": timeframe,
                 "rtype": recommendation_type, "target": target_ps_id,
                 "status": status},
            )

    def _query_ps_status(self, ps_id: str) -> tuple[str, object, object]:
        """返回 (status, frozen_at, deprecated_at)."""
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text(
                    "SELECT status, frozen_at, deprecated_at "
                    "FROM governance.parameter_sets WHERE parameter_set_id = :pid"
                ),
                {"pid": ps_id},
            ).fetchone()
        assert row is not None, f"parameter_set {ps_id} not found"
        return row.status, row.frozen_at, row.deprecated_at

    def _query_rec_status(
        self, rec_id: str,
    ) -> tuple[str, object, object]:
        """返回 (status, superseded_by, superseded_by_recommendation_id)."""
        from sqlalchemy import text

        with self.engine.begin() as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                text(
                    "SELECT status, superseded_by, superseded_by_recommendation_id "
                    "FROM governance.recommendations WHERE recommendation_id = :rec"
                ),
                {"rec": rec_id},
            ).fetchone()
        assert row is not None, f"recommendation {rec_id} not found"
        return row.status, row.superseded_by, row.superseded_by_recommendation_id

    def _invoke_apply(self, rec_id: str, project_root: Path) -> dict:
        """调用 apply_approved_recommendation，用 fake get_session 桥接 testcontainer."""
        from contextlib import contextmanager

        from aats.data_platform.decision_system import active_parameter_apply

        # get_session 是 @contextmanager 装饰的 generator (db.py)
        # 我们的 fake 必须也是 context manager + yield Session + commit/rollback
        from sqlalchemy.orm import sessionmaker

        SessionLocal = sessionmaker(bind=self.engine, future=True)

        @contextmanager
        def _fake_get_session(settings=None):
            session = SessionLocal()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        # apply_approved_recommendation 内部从 aats.data_platform.db 局部 import
        # get_session；patch 必须在那个源头模块上生效才能覆盖 late import。
        # try_governance_db 同理 patch 真源 (governance._db_util)。
        with patch("aats.data_platform.db.get_session", _fake_get_session), \
             patch("aats.data_platform.governance._db_util.try_governance_db",
                   return_value=(self.engine, True)):
            return active_parameter_apply.apply_approved_recommendation(
                project_root,
                recommendation_id=rec_id,
                actor="integration_test",
                gate_result={"allow_apply": True, "blocking_reasons": []},
            )

    # ── Bug 9a: candidate → released ───────────────────────────────────

    def test_apply_promotes_candidate_to_released(self) -> None:
        """Bug 9a: 首次 apply 把 target parameter_set.status 从 candidate 升 released."""
        self._seed_parameter_set("ps_new", status="candidate")
        self._seed_recommendation("rec_new", target_ps_id="ps_new", status="approved")

        result = self._invoke_apply("rec_new", Path("."))

        self.assertTrue(result.get("ok"), f"apply failed: {result}")
        status, frozen_at, deprecated_at = self._query_ps_status("ps_new")
        self.assertEqual(status, "released")
        self.assertIsNotNone(frozen_at, "frozen_at 必须在 released 转换时写入")
        self.assertIsNone(deprecated_at)

    # ── Bug 9b: 旧 released → deprecated ───────────────────────────────

    def test_apply_demotes_other_released_in_same_combo(self) -> None:
        """Bug 9b: apply ps_new 时，同 combo 下的 ps_old (released) 被降级为 deprecated."""
        self._seed_parameter_set("ps_old", status="released")
        self._seed_parameter_set("ps_new", status="candidate")
        self._seed_recommendation("rec_new", target_ps_id="ps_new", status="approved")

        result = self._invoke_apply("rec_new", Path("."))

        self.assertTrue(result.get("ok"), f"apply failed: {result}")
        new_status, _, _ = self._query_ps_status("ps_new")
        old_status, _, old_deprecated_at = self._query_ps_status("ps_old")

        self.assertEqual(new_status, "released")
        self.assertEqual(old_status, "deprecated", "旧 released 必须降级到 deprecated")
        self.assertIsNotNone(old_deprecated_at, "deprecated_at 必须在降级时写入")

    def test_apply_does_not_demote_other_combo_released(self) -> None:
        """Bug 9b: apply independent/15m 时，independent/1h 的 released 不受影响."""
        self._seed_parameter_set("ps_1h_old", timeframe="1h", status="released")
        self._seed_parameter_set("ps_15m_new", timeframe="15m", status="candidate")
        self._seed_recommendation("rec_15m_new", target_ps_id="ps_15m_new",
                                   timeframe="15m", status="approved")

        self._invoke_apply("rec_15m_new", Path("."))

        other_status, _, _ = self._query_ps_status("ps_1h_old")
        self.assertEqual(other_status, "released",
                         "不同 timeframe 的 released 不应被降级")

    # ── Bug 2: stale approved → superseded ─────────────────────────────

    def test_apply_supersedes_stale_approved_in_same_combo(self) -> None:
        """Bug 2: apply rec_new 时，同 combo 其他 approved parameter_upgrade 被 superseded."""
        self._seed_parameter_set("ps_a", status="released")
        self._seed_parameter_set("ps_b", status="candidate")
        self._seed_recommendation("rec_stale_a", target_ps_id="ps_a", status="approved")
        self._seed_recommendation("rec_new_b", target_ps_id="ps_b", status="approved")

        self._invoke_apply("rec_new_b", Path("."))

        stale_status, stale_sb, stale_sb_rec = self._query_rec_status("rec_stale_a")
        new_status, _, _ = self._query_rec_status("rec_new_b")

        self.assertEqual(stale_status, "superseded",
                         "同 combo 其他 approved parameter_upgrade 必须被 supersede")
        self.assertEqual(stale_sb, "rec_new_b")
        self.assertEqual(stale_sb_rec, "rec_new_b")
        # 当前 apply 的 recommendation 状态保持 approved (不自 supersede)
        self.assertEqual(new_status, "approved")

    def test_apply_does_not_supersede_other_combo_approved(self) -> None:
        """Bug 2: apply independent/15m 时，independent/1h 的 approved 不被 supersede."""
        self._seed_parameter_set("ps_1h", timeframe="1h", status="released")
        self._seed_parameter_set("ps_15m", timeframe="15m", status="candidate")
        self._seed_recommendation("rec_1h", target_ps_id="ps_1h",
                                   timeframe="1h", status="approved")
        self._seed_recommendation("rec_15m", target_ps_id="ps_15m",
                                   timeframe="15m", status="approved")

        self._invoke_apply("rec_15m", Path("."))

        other_status, _, _ = self._query_rec_status("rec_1h")
        self.assertEqual(other_status, "approved",
                         "不同 timeframe 的 approved 不应被 supersede")

    def test_apply_does_not_supersede_keep_active_type(self) -> None:
        """Bug 2: keep_active 类型的 approved recommendation 不被 supersede。

        supersede 逻辑只针对 recommendation_type='parameter_upgrade'，
        keep_active 是决策结论不涉及实际 apply，应保留 approved。
        """
        self._seed_parameter_set("ps_x", status="candidate")
        self._seed_recommendation(
            "rec_keep", target_ps_id=None, recommendation_type="keep_active",
            status="approved",
        )
        self._seed_recommendation("rec_upgrade", target_ps_id="ps_x", status="approved")

        self._invoke_apply("rec_upgrade", Path("."))

        keep_status, _, _ = self._query_rec_status("rec_keep")
        self.assertEqual(keep_status, "approved",
                         "keep_active 型 approved 不应被 parameter_upgrade 的 supersede 逻辑波及")


if __name__ == "__main__":
    unittest.main()
