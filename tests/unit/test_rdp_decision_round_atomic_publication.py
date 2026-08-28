from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from aats.data_platform.decision_system import recommendation_registry
from aats.data_platform.governance._exceptions import (
    DBConflictError,
    DBConstraintViolation,
    DBUnavailableError,
)
from scripts import rdp_run_decision_round


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False
        self.snapshot: dict[str, Any] | None = None

    def dispose(self) -> None:
        self.disposed = True


class _FakeTransaction:
    def __init__(self, session: "_FakeSession") -> None:
        self._session = session

    def __enter__(self) -> "_FakeTransaction":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        if exc_type is None:
            self._session.committed = True
        else:
            self._session.rolled_back = True
        return False


class _FakeSession:
    def __init__(self, engine: _FakeEngine) -> None:
        self.engine = engine
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)


def _publication_inputs() -> dict[str, Any]:
    return {
        "round_id": "20260828_000000_1234abcd",
        "started_at": "2026-08-28T00:00:00+00:00",
        "finished_at": "2026-08-28T00:01:00+00:00",
        "upgrade_candidates": [
            {
                "family": "independent",
                "timeframe": "15m",
                "decision": "promote_candidate",
                "parameter_set_id": "ps_candidate",
                "source_round_id": "20260827_230000_abcdef12",
                "confidence": "high",
                "reason": "qualified",
            }
        ],
        "ft_decisions": [
            {
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "decision": "keep_active",
                "confidence": "high",
                "reasons": ["stable"],
            }
        ],
        "evidence_bundle": {
            "evidence_completeness": {
                "phases_with_data": ["phase2", "phase3", "phase4", "phase5"],
                "completeness_ratio": 1.0,
            }
        },
        "evidence_summary_path": "artifacts/decision_rounds/round/evidence_summary.json",
        "readiness_report": {"readiness": "ready_for_next_live_test"},
        "manifest": {"status": "succeeded"},
        "conclusion_markdown": "# conclusion",
    }


def _install_fake_session(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeEngine, list[_FakeSession]]:
    from aats.data_platform.governance import decision_rounds_db
    import sqlalchemy.orm

    engine = _FakeEngine()
    sessions: list[_FakeSession] = []

    def session_factory(received_engine: _FakeEngine) -> _FakeSession:
        assert received_engine is engine
        session = _FakeSession(received_engine)
        sessions.append(session)
        return session

    monkeypatch.setattr(
        recommendation_registry,
        "try_governance_db",
        lambda: (engine, True),
    )
    monkeypatch.setattr(sqlalchemy.orm, "Session", session_factory)
    monkeypatch.setattr(
        decision_rounds_db,
        "db_acquire_decision_round_publication_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        decision_rounds_db,
        "db_load_decision_round_snapshot",
        lambda *_args, **_kwargs: engine.snapshot,
    )
    return engine, sessions


def _snapshot_from_write(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "round_id": kwargs["round_id"],
        "started_at": kwargs["started_at"],
        "finished_at": kwargs["finished_at"],
        "evidence_bundle_summary": kwargs["evidence_bundle_summary"],
        "parameter_upgrade_candidates": kwargs[
            "parameter_upgrade_candidates"
        ],
        "family_timeframe_decisions": kwargs[
            "family_timeframe_decisions"
        ],
        "promotion_readiness_assessment": kwargs[
            "promotion_readiness_assessment"
        ],
        "manifest": kwargs["manifest"],
        "conclusion_markdown": kwargs["conclusion_markdown"],
    }


def test_managed_decision_round_publishes_every_record_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import (
        decision_rounds_db,
        operational_state_db,
        recommendations_db,
    )

    engine, sessions = _install_fake_session(monkeypatch)
    events: list[tuple[str, _FakeSession, Any]] = []

    def insert_recommendation(
        session: _FakeSession,
        *,
        recommendation: dict[str, Any],
    ) -> None:
        events.append(("recommendation", session, recommendation))

    def upsert_active(session: _FakeSession, **kwargs: Any) -> bool:
        events.append(("active", session, kwargs))
        return True

    def upsert_bundle(
        session: _FakeSession,
        entry: dict[str, Any],
    ) -> None:
        events.append(("bundle", session, entry))

    def upsert_snapshot(session: _FakeSession, **kwargs: Any) -> None:
        events.append(("snapshot", session, kwargs))
        engine.snapshot = _snapshot_from_write(kwargs)

    monkeypatch.setattr(
        recommendations_db,
        "db_insert_recommendation_superseding_drafts",
        insert_recommendation,
    )
    monkeypatch.setattr(
        recommendations_db,
        "db_upsert_active_decision",
        upsert_active,
    )
    monkeypatch.setattr(
        operational_state_db,
        "db_insert_decision_evidence_bundle",
        upsert_bundle,
    )
    monkeypatch.setattr(
        decision_rounds_db,
        "db_upsert_decision_round_snapshot",
        upsert_snapshot,
    )

    stats = recommendation_registry.publish_managed_decision_round(
        **_publication_inputs()
    )

    assert stats == {
        "recommendations_added": 2,
        "decisions_updated": 1,
        "bundles_registered": 1,
    }
    assert len(sessions) == 1
    assert sessions[0].committed is True
    assert sessions[0].rolled_back is False
    assert engine.disposed is True
    assert [event[0] for event in events] == [
        "recommendation",
        "recommendation",
        "active",
        "bundle",
        "snapshot",
    ]
    assert {id(event[1]) for event in events} == {id(sessions[0])}
    informational_rec = next(
        event[2]
        for event in events
        if event[0] == "recommendation"
        and event[2]["recommendation_type"] == "keep_active"
    )
    active_kwargs = next(event[2] for event in events if event[0] == "active")
    assert active_kwargs["last_recommendation_id"] == informational_rec[
        "recommendation_id"
    ]


def test_sticky_pause_conflict_rolls_back_recommendations_and_omits_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import (
        decision_rounds_db,
        operational_state_db,
        recommendations_db,
    )

    engine, sessions = _install_fake_session(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(
        recommendations_db,
        "db_insert_recommendation_superseding_drafts",
        lambda *_args, **_kwargs: events.append("recommendation"),
    )
    monkeypatch.setattr(
        recommendations_db,
        "db_upsert_active_decision",
        lambda *_args, **_kwargs: events.append("active") or False,
    )
    monkeypatch.setattr(
        operational_state_db,
        "db_insert_decision_evidence_bundle",
        lambda *_args, **_kwargs: events.append("bundle"),
    )
    monkeypatch.setattr(
        decision_rounds_db,
        "db_upsert_decision_round_snapshot",
        lambda *_args, **_kwargs: events.append("snapshot"),
    )

    with pytest.raises(DBConflictError, match="sticky_pause"):
        recommendation_registry.publish_managed_decision_round(
            **_publication_inputs()
        )

    assert len(sessions) == 1
    assert sessions[0].committed is False
    assert sessions[0].rolled_back is True
    assert engine.disposed is True
    assert events == ["recommendation", "recommendation", "active"]


def test_snapshot_failure_rolls_back_all_prior_control_plane_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import (
        decision_rounds_db,
        operational_state_db,
        recommendations_db,
    )

    engine, sessions = _install_fake_session(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(
        recommendations_db,
        "db_insert_recommendation_superseding_drafts",
        lambda *_args, **_kwargs: events.append("recommendation"),
    )
    monkeypatch.setattr(
        recommendations_db,
        "db_upsert_active_decision",
        lambda *_args, **_kwargs: events.append("active") or True,
    )
    monkeypatch.setattr(
        operational_state_db,
        "db_insert_decision_evidence_bundle",
        lambda *_args, **_kwargs: events.append("bundle"),
    )

    def fail_snapshot(*_args: Any, **_kwargs: Any) -> None:
        events.append("snapshot")
        raise IntegrityError("statement", {}, RuntimeError("injected"))

    monkeypatch.setattr(
        decision_rounds_db,
        "db_upsert_decision_round_snapshot",
        fail_snapshot,
    )

    with pytest.raises(
        DBConstraintViolation,
        match="decision_round_atomic_publication_constraint_violation",
    ):
        recommendation_registry.publish_managed_decision_round(
            **_publication_inputs()
        )

    assert len(sessions) == 1
    assert sessions[0].committed is False
    assert sessions[0].rolled_back is True
    assert engine.disposed is True
    assert events[-2:] == ["bundle", "snapshot"]


def test_exact_retry_is_zero_write_and_typed_drift_fails_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.governance import (
        decision_rounds_db,
        operational_state_db,
        recommendations_db,
    )

    engine, sessions = _install_fake_session(monkeypatch)
    state: dict[str, Any] = {
        "recommendations": [],
        "bundle": None,
    }
    writes: list[str] = []

    def insert_recommendation(
        _session: _FakeSession,
        *,
        recommendation: dict[str, Any],
    ) -> None:
        writes.append("recommendation")
        state["recommendations"].append(
            {**recommendation, "status": "draft"}
        )

    def upsert_active(_session: _FakeSession, **_kwargs: Any) -> bool:
        writes.append("active")
        return True

    def insert_bundle(
        _session: _FakeSession,
        entry: dict[str, Any],
    ) -> None:
        writes.append("bundle")
        state["bundle"] = dict(entry)

    def upsert_snapshot(_session: _FakeSession, **kwargs: Any) -> None:
        writes.append("snapshot")
        engine.snapshot = _snapshot_from_write(kwargs)

    monkeypatch.setattr(
        recommendations_db,
        "db_insert_recommendation_superseding_drafts",
        insert_recommendation,
    )
    monkeypatch.setattr(
        recommendations_db,
        "db_upsert_active_decision",
        upsert_active,
    )
    monkeypatch.setattr(
        recommendations_db,
        "db_find_recommendations_for_evidence_bundle",
        lambda *_args, **_kwargs: list(state["recommendations"]),
    )
    monkeypatch.setattr(
        operational_state_db,
        "db_insert_decision_evidence_bundle",
        insert_bundle,
    )
    monkeypatch.setattr(
        operational_state_db,
        "db_get_decision_evidence_bundle",
        lambda *_args, **_kwargs: state["bundle"],
    )
    monkeypatch.setattr(
        decision_rounds_db,
        "db_upsert_decision_round_snapshot",
        upsert_snapshot,
    )

    first_inputs = _publication_inputs()
    first_inputs["readiness_report"]["typed_value"] = 1
    first_stats = recommendation_registry.publish_managed_decision_round(
        **first_inputs
    )
    writes_after_first = list(writes)

    retry_inputs = _publication_inputs()
    retry_inputs["readiness_report"]["typed_value"] = 1
    retry_stats = recommendation_registry.publish_managed_decision_round(
        **retry_inputs
    )
    assert retry_stats == first_stats
    assert writes == writes_after_first
    assert retry_inputs["manifest"] == first_inputs["manifest"]

    drift_inputs = _publication_inputs()
    drift_inputs["readiness_report"]["typed_value"] = 1.0
    with pytest.raises(
        DBConflictError,
        match="decision_round_publication_identity_conflict",
    ):
        recommendation_registry.publish_managed_decision_round(
            **drift_inputs
        )
    assert writes == writes_after_first
    assert len(sessions) == 3
    assert sessions[0].committed is True
    assert sessions[1].committed is True
    assert sessions[2].rolled_back is True


def test_managed_publication_rejects_db_unavailable_without_file_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recommendation_registry,
        "try_governance_db",
        lambda: (None, False),
    )

    with pytest.raises(DBUnavailableError, match="managed_db_unavailable"):
        recommendation_registry.publish_managed_decision_round(
            **_publication_inputs()
        )


def test_legacy_batch_updater_cannot_reenter_managed_multi_transaction_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        recommendation_registry,
        "load_recommendation_registry",
        lambda _path: {
            "recommendations": [],
            "_governance_storage_mode": "managed_db",
        },
    )
    monkeypatch.setattr(
        recommendation_registry,
        "add_recommendation",
        lambda *_args, **_kwargs: pytest.fail(
            "managed legacy updater must stop before the first write"
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="require publish_managed_decision_round",
    ):
        recommendation_registry.update_registries_from_round(
            round_id="20260828_000000_1234abcd",
            upgrade_candidates=[],
            ft_decisions=[],
            evidence_bundle={"evidence_completeness": {}},
            rec_registry_path=tmp_path / "recommendations.json",
            decision_registry_path=tmp_path / "decisions.json",
            bundle_index_path=tmp_path / "bundles.json",
            evidence_summary_path="evidence_summary.json",
        )


def test_offline_round_preserves_nonpromotion_parameter_and_replaces_promote(
    tmp_path: Path,
) -> None:
    rec_path = tmp_path / "recommendations.json"
    decision_path = tmp_path / "decisions.json"
    bundle_path = tmp_path / "bundles.json"
    decision_path.write_text(
        json.dumps(
            {
                "version": 0,
                "decisions": [
                    {
                        "family": "independent",
                        "symbol": "BTC-USDT-SWAP",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "current_status": "observe",
                        "active_parameter_set_id": "ps_old_keep",
                    },
                    {
                        "family": "directional",
                        "symbol": "BTC-USDT-SWAP",
                        "timeframe": "1h",
                        "combo_key": "directional_1h",
                        "current_status": "observe",
                        "active_parameter_set_id": "ps_old_pause",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    recommendation_registry.update_registries_from_round(
        round_id="20260828_010000_1234abcd",
        upgrade_candidates=[],
        ft_decisions=[
            {
                "family": "independent",
                "timeframe": "15m",
                "decision": "keep_active",
                "confidence": "high",
                "reasons": ["stable"],
            },
            {
                "family": "directional",
                "timeframe": "1H",
                "decision": "pause",
                "confidence": "low",
                "reasons": ["risk"],
            },
        ],
        evidence_bundle={"evidence_completeness": {}},
        rec_registry_path=rec_path,
        decision_registry_path=decision_path,
        bundle_index_path=bundle_path,
        evidence_summary_path="evidence_summary.json",
        offline_only=True,
    )
    preserved = json.loads(decision_path.read_text(encoding="utf-8"))
    preserved_by_combo = {
        item["combo_key"]: item for item in preserved["decisions"]
    }
    assert (
        preserved_by_combo["independent_15m"]["active_parameter_set_id"]
        == "ps_old_keep"
    )
    assert (
        preserved_by_combo["directional_1h"]["active_parameter_set_id"]
        == "ps_old_pause"
    )

    recommendation_registry.update_registries_from_round(
        round_id="20260828_010100_1234abcd",
        upgrade_candidates=[
            {
                "family": "independent",
                "timeframe": "15m",
                "decision": "promote_candidate",
                "parameter_set_id": "ps_new",
                "source_round_id": "20260828_005900_abcdef12",
                "confidence": "high",
                "reason": "qualified",
            }
        ],
        ft_decisions=[
            {
                "family": "independent",
                "timeframe": "15m",
                "decision": "keep_active",
                "confidence": "high",
                "reasons": ["stable"],
            }
        ],
        evidence_bundle={"evidence_completeness": {}},
        rec_registry_path=rec_path,
        decision_registry_path=decision_path,
        bundle_index_path=bundle_path,
        evidence_summary_path="evidence_summary.json",
        offline_only=True,
    )
    replaced = json.loads(decision_path.read_text(encoding="utf-8"))
    replaced_by_combo = {
        item["combo_key"]: item for item in replaced["decisions"]
    }
    assert (
        replaced_by_combo["independent_15m"]["active_parameter_set_id"]
        == "ps_new"
    )


def test_main_db_publication_failure_has_no_succeeded_manifest_or_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_decision_round.py",
            "--artifact-root",
            str(tmp_path),
            "--no-print-summary",
        ],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "build_evidence_bundle",
        lambda *_args, **_kwargs: {
            "evidence_completeness": {
                "phases_with_data": [],
                "completeness_ratio": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "_load_decision_parameter_sets",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "select_parameter_upgrade_candidates",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "decide_all_family_timeframes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "evaluate_promotion_readiness",
        lambda *_args, **_kwargs: {
            "readiness": "not_ready_more_research_needed",
            "overall_confidence": "low",
            "checks_passed": 0,
            "checks_total": 1,
            "blockers": ["missing evidence"],
        },
    )

    def write_conclusion(*, output_path: Path, **_kwargs: Any) -> None:
        output_path.write_text("# conclusion", encoding="utf-8")

    monkeypatch.setattr(
        rdp_run_decision_round,
        "build_phase6_conclusion",
        write_conclusion,
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "publish_managed_decision_round",
        lambda **_kwargs: (_ for _ in ()).throw(
            DBUnavailableError("injected")
        ),
    )

    assert rdp_run_decision_round.main() == 3
    assert list(
        (tmp_path / "artifacts/decision_rounds").glob("*/round_manifest.json")
    ) == []
    assert rdp_run_decision_round._DECISION_RESULT_PREFIX not in capsys.readouterr().out


def test_main_rejects_offline_mode_when_managed_db_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_decision_round.py",
            "--artifact-root",
            str(tmp_path),
            "--offline-file-mode",
        ],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "build_evidence_bundle",
        lambda *_args, **_kwargs: pytest.fail(
            "offline managed-mode rejection must occur before artifact reads"
        ),
    )

    assert rdp_run_decision_round.main() == 2
    assert (tmp_path / "artifacts").exists() is False


def test_explicit_offline_mode_is_labeled_and_never_calls_managed_publisher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_decision_round.py",
            "--artifact-root",
            str(tmp_path),
            "--offline-file-mode",
            "--no-print-summary",
        ],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "has_explicit_governance_db_configuration",
        lambda _root: False,
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "build_evidence_bundle",
        lambda *_args, **_kwargs: {
            "evidence_completeness": {
                "phases_with_data": [],
                "completeness_ratio": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "select_parameter_upgrade_candidates",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "decide_all_family_timeframes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "evaluate_promotion_readiness",
        lambda *_args, **_kwargs: {
            "readiness": "not_ready_more_research_needed",
            "overall_confidence": "low",
            "checks_passed": 0,
            "checks_total": 1,
            "blockers": ["offline development"],
        },
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "build_phase6_conclusion",
        lambda *, output_path, **_kwargs: output_path.write_text(
            "# conclusion",
            encoding="utf-8",
        ),
    )
    monkeypatch.setattr(
        rdp_run_decision_round,
        "publish_managed_decision_round",
        lambda **_kwargs: pytest.fail(
            "explicit offline mode must not call the managed publisher"
        ),
    )

    assert rdp_run_decision_round.main() == 0
    manifests = list(
        (tmp_path / "artifacts/decision_rounds").glob("*/round_manifest.json")
    )
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert manifest["publication_mode"] == "offline_file_only"
    assert "no managed DB truth" in manifest["notes"]
