from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from aats.data_platform.governance import snapshot_db
from aats.data_platform.governance._exceptions import DBConflictError


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _CaptureSession:
    def __init__(self, row: Any) -> None:
        self._row = row
        self.statement = ""
        self.params: dict[str, Any] = {}

    def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.statement = str(statement)
        self.params = params
        return _Result(self._row)


@pytest.mark.parametrize(
    "field",
    [
        "phase",
        "status",
        "round_path",
        "started_at",
        "finished_at",
        "replay_only",
    ],
)
def test_research_round_snapshot_conflict_requires_exact_canonical_identity(
    field: str,
) -> None:
    session = _CaptureSession(SimpleNamespace(round_id="phase3_round_1"))

    snapshot_db.db_upsert_research_round_snapshot(
        session,  # type: ignore[arg-type]
        round_id="phase3_round_1",
        phase="phase3",
        status="succeeded",
        round_path="artifacts/research/attribution_rounds/phase3_round_1",
        started_at="2026-08-28T10:00:00+00:00",
        finished_at="2026-08-28T10:05:00+00:00",
        manifest_payload={
            "input_refs": {
                "phase2_round_id": "phase2_round_1",
                "parameter_values_fingerprint": "a" * 64,
            }
        },
        summary_payload={"overall_status": "succeeded"},
        conclusion_payload={"verdict": "pass"},
        artifacts_payload={"attribution_summary": {"sha256": "b" * 64}},
    )

    predicate = (
        f"governance.research_round_snapshots.{field} "
        f"IS NOT DISTINCT FROM EXCLUDED.{field}"
    )
    assert predicate in session.statement
    assert f"{field} = EXCLUDED.{field}" not in session.statement
    assert "RETURNING round_id" in session.statement
    assert json.loads(session.params["manifest_payload"])["input_refs"] == {
        "phase2_round_id": "phase2_round_1",
        "parameter_values_fingerprint": "a" * 64,
    }
    assert len(session.params["typed_json_identity_sha256"]) == 64


@pytest.mark.parametrize(
    "field",
    [
        "manifest_payload",
        "summary_payload",
        "conclusion_payload",
        "artifacts_payload",
    ],
)
def test_research_round_snapshot_json_identity_uses_typed_text_and_digest(
    field: str,
) -> None:
    session = _CaptureSession(SimpleNamespace(round_id="phase3_round_1"))

    snapshot_db.db_upsert_research_round_snapshot(
        session,  # type: ignore[arg-type]
        round_id="phase3_round_1",
        phase="phase3",
        status="succeeded",
        manifest_payload={"metric": 1.0},
    )

    predicate = (
        f"governance.research_round_snapshots.{field}::text "
        f"IS NOT DISTINCT FROM EXCLUDED.{field}::text"
    )
    assert predicate in session.statement
    assert "typed_json_identity_sha256 = COALESCE" in session.statement


def test_research_round_snapshot_identity_drift_fails_closed() -> None:
    session = _CaptureSession(None)

    with pytest.raises(
        DBConflictError,
        match="research_round_snapshot_immutable_identity_conflict",
    ):
        snapshot_db.db_upsert_research_round_snapshot(
            session,  # type: ignore[arg-type]
            round_id="phase4_round_1",
            phase="phase4",
            status="succeeded",
            manifest_payload={"input_refs": {"phase3_round_id": "changed"}},
            artifacts_payload={"cost_summary": {"sha256": "c" * 64}},
        )


def test_save_research_round_snapshot_does_not_commit_identity_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class _Session(_CaptureSession):
        committed = False

        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def commit(self) -> None:
            self.committed = True

    engine = _Engine()
    session = _Session(None)
    monkeypatch.setattr(snapshot_db, "try_governance_db", lambda: (engine, True))
    monkeypatch.setattr(snapshot_db, "Session", lambda _engine: session)

    saved = snapshot_db.save_research_round_snapshot(
        round_id="phase3_round_conflict",
        phase="phase3",
        status="succeeded",
        manifest_payload={"input_refs": {"phase2_round_id": "changed"}},
    )

    assert saved is False
    assert session.committed is False
    assert engine.disposed is True
