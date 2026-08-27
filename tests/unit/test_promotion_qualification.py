from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aats.data_platform.decision_system import promotion_qualification as qualification
from aats.data_platform.decision_system.evidence_bundle import (
    PHASE2_PROMOTION_QUALIFICATION_POLICY,
)
from aats.data_platform.governance.decision_rounds_db import (
    db_load_decision_round_snapshot,
    db_load_decision_round_snapshots,
)

ROUND_ID = "20260827_120000_deadbeef"
SOURCE_ROUND_ID = "phase2_source_001"


def _recommendation(**overrides):
    value = {
        "recommendation_id": "rec_001",
        "recommendation_type": "parameter_upgrade",
        "target_parameter_set_id": "ps_001",
        "source_round_id": SOURCE_ROUND_ID,
        "evidence_bundle_ref": ROUND_ID,
        "family": "independent",
        "timeframe": "1H",
        "symbol": "BTC-USDT-SWAP",
    }
    value.update(overrides)
    return value


def _candidate(**overrides):
    value = {
        "parameter_set_id": "ps_001",
        "source_round_id": SOURCE_ROUND_ID,
        "family": "independent",
        "timeframe": "1hour",
        "symbol": "BTC-USDT-SWAP",
        "decision": "promote_candidate",
    }
    value.update(overrides)
    return value


def _evidence(**phase2_overrides):
    phase2 = {
        "promotion_qualification_policy": (
            PHASE2_PROMOTION_QUALIFICATION_POLICY
        ),
        "combo_stats": {
            "independent_1h": {
                "available": True,
                "family": "independent",
                "timeframe": "1h",
                "combo_key": "independent_1h",
                "total_experiments": 2,
                "experiments_with_openings": 1,
                "max_opening_count": 3,
            }
        },
    }
    phase2.update(phase2_overrides)
    return {"phase2_evidence": phase2}


def _manifest(*, candidates, **overrides):
    finished_at = datetime.now(timezone.utc).isoformat()
    value = {
        "round_id": ROUND_ID,
        "phase": "phase6",
        "status": "succeeded",
        "finished_at": finished_at,
        "scope": {
            "symbol": "BTC-USDT-SWAP",
            "families": ["independent"],
            "timeframes": ["1h"],
        },
        "upgrade_candidates_count": len(candidates),
        "output_refs": {
            "evidence_summary": "evidence_summary.json",
            "upgrade_candidates": "parameter_upgrade_candidates.json",
        },
    }
    value.update(overrides)
    return value


def _snapshot(*, candidates=None, evidence=None, manifest=None):
    candidates = [_candidate()] if candidates is None else candidates
    manifest = manifest or _manifest(candidates=candidates)
    return {
        "round_id": ROUND_ID,
        "finished_at": manifest.get("finished_at"),
        "manifest": manifest,
        "evidence_bundle_summary": evidence or _evidence(),
        "parameter_upgrade_candidates": candidates,
    }


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _write_round(
    project_root: Path,
    *,
    candidates=None,
    evidence=None,
    manifest=None,
) -> Path:
    candidates = [_candidate()] if candidates is None else candidates
    manifest = manifest or _manifest(candidates=candidates)
    round_dir = project_root / "artifacts" / "decision_rounds" / ROUND_ID
    _write_json(round_dir / "round_manifest.json", manifest)
    _write_json(round_dir / "evidence_summary.json", evidence or _evidence())
    _write_json(round_dir / "parameter_upgrade_candidates.json", candidates)
    return round_dir


@pytest.fixture(autouse=True)
def _file_mode(monkeypatch):
    monkeypatch.setattr(
        qualification,
        "has_explicit_governance_db_configuration",
        lambda _root: False,
    )
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        lambda: (None, False),
    )


def test_non_apply_recommendation_does_not_load_any_round(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        Mock(side_effect=AssertionError("DB must not be consulted")),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(
            recommendation_type="pause",
            target_parameter_set_id=None,
            evidence_bundle_ref=None,
        ),
    )

    assert verdict.required is False
    assert verdict.eligible is True
    assert verdict.reason_code == "not_required"
    assert verdict.qualified_round_id is None


def test_legacy_missing_type_with_target_is_apply_capable(tmp_path: Path) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_type=None, evidence_bundle_ref=None),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "evidence_bundle_ref_required"


@pytest.mark.parametrize("recommendation_type", ["pause", "bogus_type"])
def test_non_apply_or_unknown_type_with_target_cannot_bypass_qualification(
    tmp_path: Path,
    recommendation_type: str,
) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_type=recommendation_type),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "recommendation_invalid"


@pytest.mark.parametrize("recommendation_type", [123, "", " pause "])
def test_malformed_non_null_type_is_not_treated_as_legacy_missing(
    tmp_path: Path,
    recommendation_type,
) -> None:
    _write_round(tmp_path)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(recommendation_type=recommendation_type),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "recommendation_invalid"


@pytest.mark.parametrize("target", [123, "", " ps_001 "])
def test_malformed_non_null_target_is_not_treated_as_absent(
    tmp_path: Path,
    target,
) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(
            recommendation_type="pause",
            target_parameter_set_id=target,
        ),
    )

    assert verdict.required is True
    assert verdict.eligible is False
    assert verdict.reason_code == "recommendation_invalid"


def test_exact_file_round_qualifies_and_normalizes_timeframe(tmp_path: Path) -> None:
    _write_round(tmp_path)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.required is True
    assert verdict.eligible is True
    assert verdict.reason_code == "qualified"
    assert verdict.qualified_round_id == ROUND_ID
    payload = verdict.to_dict()
    assert isinstance(payload["qualified_finished_at"], str)
    assert payload == {
        "required": True,
        "eligible": True,
        "reason_code": "qualified",
        "evidence_bundle_ref": ROUND_ID,
        "source_round_id": SOURCE_ROUND_ID,
        "qualified_round_id": ROUND_ID,
        "qualified_finished_at": verdict.qualified_finished_at,
        "detail": "精确引用的 Phase 6 round 与参数升级候选资格一致。",
    }


def test_equivalent_explicit_offsets_are_the_same_canonical_finish_time() -> None:
    finished_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    offset = timezone(-timedelta(hours=4))
    snapshot = {"finished_at": finished_at.astimezone(offset).isoformat()}
    manifest = {"finished_at": finished_at.isoformat()}

    canonical = qualification._canonical_round_finished_at(snapshot, manifest)

    assert canonical == finished_at


@pytest.mark.parametrize(
    ("snapshot_finished_at", "manifest_finished_at"),
    [
        ("2026-08-27T12:00:00", "2026-08-27T12:00:00+00:00"),
        ("2026-08-27T12:00:00+00:00", "2026-08-27T12:01:00+00:00"),
    ],
)
def test_naive_or_inconsistent_finish_times_remain_invalid(
    snapshot_finished_at: str,
    manifest_finished_at: str,
) -> None:
    assert qualification._canonical_round_finished_at(
        {"finished_at": snapshot_finished_at},
        {"finished_at": manifest_finished_at},
    ) is None


@pytest.mark.parametrize(
    ("age_hours", "expected_reason"),
    [
        (167.9, "qualified"),
        (169.0, "promotion_round_stale"),
    ],
)
def test_exact_round_freshness_is_part_of_qualification(
    tmp_path: Path,
    age_hours: float,
    expected_reason: str,
) -> None:
    candidates = [_candidate()]
    finished_at = (
        datetime.now(timezone.utc) - timedelta(hours=age_hours)
    ).isoformat()
    manifest = _manifest(
        candidates=candidates,
        finished_at=finished_at,
    )
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == expected_reason
    assert verdict.eligible is (expected_reason == "qualified")


@pytest.mark.parametrize(
    "evidence_ref",
    [
        "../20260827_120000_deadbeef",
        "artifacts/decision_rounds/20260827_120000_deadbeef",
        "C:\\outside\\20260827_120000_deadbeef",
        "20260827_120000_DEADBEEF",
    ],
)
def test_noncanonical_or_escaping_evidence_ref_fails_closed(
    tmp_path: Path,
    evidence_ref: str,
) -> None:
    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(evidence_bundle_ref=evidence_ref),
    )

    assert verdict.reason_code == "evidence_bundle_ref_invalid"
    assert verdict.eligible is False


@pytest.mark.parametrize(
    ("manifest_change", "reason_code"),
    [
        ({"round_id": "20260827_120001_bad00000"}, "promotion_round_id_mismatch"),
        ({"phase": "phase5"}, "promotion_round_phase_invalid"),
        ({"status": "running"}, "promotion_round_status_invalid"),
        (
            {
                "scope": {
                    "symbol": "ETH-USDT-SWAP",
                    "families": ["independent"],
                    "timeframes": ["1h"],
                }
            },
            "promotion_round_scope_mismatch",
        ),
    ],
)
def test_manifest_identity_phase_status_and_scope_are_strict(
    tmp_path: Path,
    manifest_change: dict,
    reason_code: str,
) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates, **manifest_change)
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


def test_output_ref_cannot_escape_exact_round(tmp_path: Path) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates)
    manifest["output_refs"]["evidence_summary"] = "../evidence_summary.json"
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_output_ref_invalid"


def test_manifest_candidate_count_must_match_exact_output(tmp_path: Path) -> None:
    candidates = [_candidate()]
    manifest = _manifest(candidates=candidates, upgrade_candidates_count=2)
    _write_round(tmp_path, candidates=candidates, manifest=manifest)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_candidate_count_mismatch"


@pytest.mark.parametrize(
    ("phase2_change", "reason_code"),
    [
        (
            {"promotion_qualification_policy": "legacy-policy/v0"},
            "promotion_policy_unsupported",
        ),
        ({"combo_stats": {}}, "promotion_combo_unavailable"),
        (
            {
                "combo_stats": {
                    "independent_1h": {
                        "available": True,
                        "family": "independent",
                        "timeframe": "1h",
                        "combo_key": "independent_1h",
                        "total_experiments": 1,
                        "experiments_with_openings": 0,
                        "max_opening_count": 0,
                    }
                }
            },
            "promotion_combo_unavailable",
        ),
    ],
)
def test_current_phase2_policy_and_opening_stats_are_required(
    tmp_path: Path,
    phase2_change: dict,
    reason_code: str,
) -> None:
    _write_round(tmp_path, evidence=_evidence(**phase2_change))

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


@pytest.mark.parametrize(
    ("candidates", "reason_code"),
    [
        ([], "promotion_candidate_missing"),
        ([_candidate(), _candidate()], "promotion_candidate_ambiguous"),
        ([_candidate(decision="hold")], "promotion_candidate_decision_invalid"),
        (
            [_candidate(symbol="ETH-USDT-SWAP")],
            "promotion_candidate_identity_mismatch",
        ),
        (
            [_candidate(source_round_id="phase2_source_other")],
            "promotion_source_round_mismatch",
        ),
    ],
)
def test_exact_promote_candidate_identity_is_required(
    tmp_path: Path,
    candidates: list[dict],
    reason_code: str,
) -> None:
    _write_round(tmp_path, candidates=candidates)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == reason_code
    assert verdict.eligible is False


def test_nonfinite_evidence_json_fails_closed(tmp_path: Path) -> None:
    round_dir = _write_round(tmp_path)
    (round_dir / "evidence_summary.json").write_text(
        '{"phase2_evidence":{"score":NaN}}',
        encoding="utf-8",
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_evidence_invalid"


class _FakeSessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_reachable_db_missing_exact_round_never_falls_back_to_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    loader = Mock(return_value={})
    monkeypatch.setattr(qualification, "db_load_decision_round_snapshots", loader)

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_not_found"
    loader.assert_called_once()
    engine.dispose.assert_called_once_with()


def test_batch_uses_one_db_session_and_one_exact_batch_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = SimpleNamespace(dispose=Mock())
    db_probe = Mock(return_value=(engine, True))
    monkeypatch.setattr(qualification, "try_governance_db", db_probe)
    session_factory = Mock(return_value=_FakeSessionContext())
    monkeypatch.setattr(qualification, "Session", session_factory)
    loader = Mock(return_value={ROUND_ID: _snapshot()})
    monkeypatch.setattr(qualification, "db_load_decision_round_snapshots", loader)

    verdicts = qualification.evaluate_promotion_qualifications(
        tmp_path,
        [
            _recommendation(recommendation_id="rec_001"),
            _recommendation(recommendation_id="rec_002"),
        ],
    )

    assert set(verdicts) == {"rec_001", "rec_002"}
    assert all(verdict.eligible for verdict in verdicts.values())
    db_probe.assert_called_once_with()
    session_factory.assert_called_once_with(engine)
    loader.assert_called_once_with(
        loader.call_args.args[0],
        round_ids=[ROUND_ID],
    )
    engine.dispose.assert_called_once_with()


def test_batch_loads_distinct_rounds_once_and_marks_missing_exact_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    second_round_id = "20260827_130000_cafebabe"
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    loader = Mock(return_value={ROUND_ID: _snapshot()})
    monkeypatch.setattr(qualification, "db_load_decision_round_snapshots", loader)

    verdicts = qualification.evaluate_promotion_qualifications(
        tmp_path,
        [
            _recommendation(recommendation_id="rec_present"),
            _recommendation(
                recommendation_id="rec_missing",
                evidence_bundle_ref=second_round_id,
            ),
        ],
    )

    assert verdicts["rec_present"].reason_code == "qualified"
    assert verdicts["rec_missing"].reason_code == "promotion_round_not_found"
    loader.assert_called_once_with(
        loader.call_args.args[0],
        round_ids=[ROUND_ID, second_round_id],
    )


def test_db_exception_fails_closed_without_file_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    engine = SimpleNamespace(dispose=Mock())
    monkeypatch.setattr(qualification, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(qualification, "Session", lambda _engine: _FakeSessionContext())
    monkeypatch.setattr(
        qualification,
        "db_load_decision_round_snapshots",
        Mock(side_effect=RuntimeError("database failure")),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_db_error"
    assert verdict.eligible is False


def test_db_probe_exception_fails_closed_without_file_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        Mock(side_effect=RuntimeError("database probe failure")),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_db_error"
    assert verdict.eligible is False


def test_configured_but_unreachable_db_never_falls_back_to_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.setattr(
        qualification,
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        lambda: (None, False),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "promotion_round_db_error"
    assert verdict.eligible is False


def test_unconfigured_default_local_db_allows_offline_file_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_round(tmp_path)
    monkeypatch.delenv("AATS_ACTIVE_PARAMETER_DB_URL", raising=False)
    monkeypatch.delenv("RDP_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        qualification,
        "has_explicit_governance_db_configuration",
        lambda _root: False,
    )
    monkeypatch.setattr(
        qualification,
        "try_governance_db",
        lambda: (None, False),
    )

    verdict = qualification.evaluate_promotion_qualification(
        tmp_path,
        _recommendation(),
    )

    assert verdict.reason_code == "qualified"
    assert verdict.eligible is True


class _ReaderSession:
    def __init__(self, row):
        self.row = row
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return SimpleNamespace(fetchone=lambda: self.row)


class _BatchReaderSession:
    def __init__(self, rows):
        self.rows = rows
        self.execute_count = 0
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.execute_count += 1
        self.statement = str(statement)
        self.params = params
        return SimpleNamespace(fetchall=lambda: self.rows)


def test_exact_id_db_reader_uses_bound_round_id_and_normalizes_snapshot() -> None:
    now = datetime(
        2026,
        8,
        27,
        8,
        tzinfo=timezone(-timedelta(hours=4)),
    )
    row = SimpleNamespace(
        round_id=ROUND_ID,
        started_at=now,
        finished_at=now,
        evidence_summary_json=json.dumps(_evidence()),
        parameter_upgrade_candidates_json=json.dumps([_candidate()]),
        family_timeframe_decisions_json="[]",
        promotion_readiness_json="{}",
        manifest_json=json.dumps(_manifest(candidates=[_candidate()])),
        conclusion_markdown="done",
    )
    session = _ReaderSession(row)

    snapshot = db_load_decision_round_snapshot(session, round_id=ROUND_ID)

    assert snapshot is not None
    assert snapshot["round_id"] == ROUND_ID
    assert snapshot["started_at"] == "2026-08-27T12:00:00+00:00"
    assert snapshot["finished_at"] == "2026-08-27T12:00:00+00:00"
    assert snapshot["manifest"]["phase"] == "phase6"
    assert "WHERE round_id = :round_id" in session.statement
    assert session.params == {"round_id": ROUND_ID}


def test_exact_id_db_reader_returns_none_without_substitution() -> None:
    session = _ReaderSession(None)

    assert db_load_decision_round_snapshot(session, round_id=ROUND_ID) is None
    assert session.params == {"round_id": ROUND_ID}


def test_exact_id_db_batch_reader_uses_one_bound_query() -> None:
    second_round_id = "20260827_130000_cafebabe"
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    def _row(round_id: str):
        manifest = _manifest(candidates=[_candidate()], round_id=round_id)
        return SimpleNamespace(
            round_id=round_id,
            started_at=now,
            finished_at=now,
            evidence_summary_json=json.dumps(_evidence()),
            parameter_upgrade_candidates_json=json.dumps([_candidate()]),
            family_timeframe_decisions_json="[]",
            promotion_readiness_json="{}",
            manifest_json=json.dumps(manifest),
            conclusion_markdown="done",
        )

    session = _BatchReaderSession([_row(ROUND_ID), _row(second_round_id)])

    snapshots = db_load_decision_round_snapshots(
        session,
        round_ids=[second_round_id, ROUND_ID, ROUND_ID],
    )

    assert set(snapshots) == {ROUND_ID, second_round_id}
    assert session.execute_count == 1
    assert "WHERE round_id IN" in session.statement
    assert session.params == {"round_ids": (ROUND_ID, second_round_id)}


def test_duplicate_batch_recommendation_id_is_ineligible(
    tmp_path: Path,
) -> None:
    _write_round(tmp_path)
    recommendations = [_recommendation(), deepcopy(_recommendation())]

    verdicts = qualification.evaluate_promotion_qualifications(
        tmp_path,
        recommendations,
    )

    assert verdicts["rec_001"].reason_code == "recommendation_id_duplicate"
    assert verdicts["rec_001"].eligible is False
