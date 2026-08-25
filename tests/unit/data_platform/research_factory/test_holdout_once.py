from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aats.data_platform.research_factory.validation.holdout import (
    HoldoutAccessRequest,
    HoldoutEvaluationOutcome,
    evaluate_holdout_once,
)


class InMemoryLedger:
    def __init__(self) -> None:
        self.claims: set[tuple[str, str]] = set()
        self.events: list[tuple[str, str]] = []
        self.pending: dict[str, tuple[str, str]] = {}

    def begin(self, request: HoldoutAccessRequest) -> str:
        key = (request.candidate_id, request.holdout_content_fingerprint)
        if key in self.claims:
            raise ValueError("holdout_already_accessed")
        self.claims.add(key)
        access_id = str(uuid.uuid4())
        self.pending[access_id] = key
        self.events.append(("begin", access_id))
        return access_id

    def complete(
        self,
        *,
        access_id: str,
        status: str,
        artifact_path: str,
        artifact_sha256: str,
        result_payload: Mapping[str, object],
    ) -> None:
        del artifact_path, artifact_sha256, result_payload
        assert access_id in self.pending
        self.events.append((status, access_id))
        self.pending.pop(access_id)

    def fail(self, *, access_id: str, error_type: str) -> None:
        self.events.append((f"access_failed:{error_type}", access_id))
        self.pending.pop(access_id, None)


@pytest.fixture
def workspace_tmp() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"holdout_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _request() -> HoldoutAccessRequest:
    return HoldoutAccessRequest(
        candidate_id="cand_v2",
        holdout_content_fingerprint="rfseg_" + "a" * 64,
        actor="operator-a",
        reason="final one-time OOS verification",
        git_commit="b" * 40,
    )


def _outcome() -> HoldoutEvaluationOutcome:
    return HoldoutEvaluationOutcome(
        passed=True,
        metrics={"net_return": 0.12, "max_drawdown": 0.05},
        gate_failures=(),
        dataset_fingerprint="rfds_" + "c" * 64,
        execution_evidence_fingerprint="l2_" + "d" * 64,
    )


def test_ledger_claim_happens_before_evaluator_and_artifact_is_read_only(
    workspace_tmp: Path,
) -> None:
    ledger = InMemoryLedger()
    observed_events: list[str] = []

    def evaluator() -> HoldoutEvaluationOutcome:
        assert ledger.events and ledger.events[0][0] == "begin"
        observed_events.append("evaluated")
        return _outcome()

    target = workspace_tmp / "holdout.json"
    result = evaluate_holdout_once(
        ledger=ledger,
        request=_request(),
        artifact_path=target,
        evaluator=evaluator,
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert observed_events == ["evaluated"]
    assert result.status == "evaluated_pass"
    assert target.exists()
    assert target.stat().st_mode & 0o222 == 0
    assert ledger.events[-1][0] == "evaluated_pass"


def test_second_access_is_rejected_before_evaluator(workspace_tmp: Path) -> None:
    ledger = InMemoryLedger()
    evaluate_holdout_once(
        ledger=ledger,
        request=_request(),
        artifact_path=workspace_tmp / "first.json",
        evaluator=_outcome,
    )
    called = False

    def must_not_run() -> HoldoutEvaluationOutcome:
        nonlocal called
        called = True
        return _outcome()

    with pytest.raises(ValueError, match="holdout_already_accessed"):
        evaluate_holdout_once(
            ledger=ledger,
            request=_request(),
            artifact_path=workspace_tmp / "second.json",
            evaluator=must_not_run,
        )
    assert called is False


def test_failed_access_is_consumed_and_records_only_error_type(workspace_tmp: Path) -> None:
    ledger = InMemoryLedger()

    def failing_evaluator() -> HoldoutEvaluationOutcome:
        raise RuntimeError("sensitive detail must not be persisted")

    with pytest.raises(RuntimeError):
        evaluate_holdout_once(
            ledger=ledger,
            request=_request(),
            artifact_path=workspace_tmp / "failed.json",
            evaluator=failing_evaluator,
        )
    assert ledger.events[-1][0] == "access_failed:RuntimeError"
    with pytest.raises(ValueError, match="holdout_already_accessed"):
        evaluate_holdout_once(
            ledger=ledger,
            request=_request(),
            artifact_path=workspace_tmp / "retry.json",
            evaluator=_outcome,
        )


def test_holdout_artifact_refuses_overwrite_after_claim(workspace_tmp: Path) -> None:
    ledger = InMemoryLedger()
    target = workspace_tmp / "existing.json"
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        evaluate_holdout_once(
            ledger=ledger,
            request=_request(),
            artifact_path=target,
            evaluator=_outcome,
        )
    assert target.read_text(encoding="utf-8") == "existing"
    assert ledger.events[-1][0] == "access_failed:FileExistsError"


def test_secret_named_metric_is_rejected() -> None:
    with pytest.raises(ValueError, match="secret_material_forbidden"):
        HoldoutEvaluationOutcome(
            passed=True,
            metrics={"api_key": "not-allowed"},
            gate_failures=(),
            dataset_fingerprint="rfds_" + "c" * 64,
            execution_evidence_fingerprint="l2_" + "d" * 64,
        )
