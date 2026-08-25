"""One-time sealed-holdout evaluation with a durable pre-read ledger claim."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_FINGERPRINT_RE = re.compile(r"^rfseg_[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_TERMINAL_STATUSES = frozenset({"evaluated_pass", "evaluated_fail", "access_failed"})
_SECRET_MARKERS = (
    "api_key",
    "authorization_header",
    "database_url",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True, slots=True)
class HoldoutAccessRequest:
    candidate_id: str
    holdout_content_fingerprint: str
    actor: str
    reason: str
    git_commit: str

    def __post_init__(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.candidate_id):
            raise ValueError("candidate_id_invalid")
        if not _FINGERPRINT_RE.fullmatch(self.holdout_content_fingerprint):
            raise ValueError("holdout_content_fingerprint_invalid")
        if not _SAFE_ID_RE.fullmatch(self.actor):
            raise ValueError("actor_invalid")
        if not self.reason.strip():
            raise ValueError("reason_required")
        if not _GIT_COMMIT_RE.fullmatch(self.git_commit):
            raise ValueError("git_commit_invalid")


@dataclass(frozen=True, slots=True)
class HoldoutEvaluationOutcome:
    passed: bool
    metrics: Mapping[str, Any]
    gate_failures: tuple[str, ...]
    dataset_fingerprint: str
    execution_evidence_fingerprint: str

    def __post_init__(self) -> None:
        if not self.dataset_fingerprint.strip():
            raise ValueError("dataset_fingerprint_required")
        if not self.execution_evidence_fingerprint.strip():
            raise ValueError("execution_evidence_fingerprint_required")
        object.__setattr__(self, "metrics", dict(self.metrics))
        object.__setattr__(self, "gate_failures", tuple(self.gate_failures))
        _reject_secret_material(self.metrics)
        if self.passed and self.gate_failures:
            raise ValueError("passing_holdout_cannot_have_gate_failures")
        if not self.passed and not self.gate_failures:
            raise ValueError("failing_holdout_requires_gate_failures")


@dataclass(frozen=True, slots=True)
class HoldoutEvaluationResult:
    access_id: str
    candidate_id: str
    status: str
    artifact_path: str
    artifact_sha256: str
    evaluated_at: datetime


class HoldoutLedger(Protocol):
    def begin(self, request: HoldoutAccessRequest) -> str: ...

    def complete(
        self,
        *,
        access_id: str,
        status: str,
        artifact_path: str,
        artifact_sha256: str,
        result_payload: Mapping[str, Any],
    ) -> None: ...

    def fail(self, *, access_id: str, error_type: str) -> None: ...


class SQLHoldoutLedger:
    """Postgres-backed ledger; ``begin`` commits before any holdout callback."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def begin(self, request: HoldoutAccessRequest) -> str:
        access_id = str(uuid.uuid4())
        row = self._session.execute(
            text(
                """
                INSERT INTO governance.research_holdout_access_ledger (
                    access_id, candidate_id, holdout_content_fingerprint,
                    actor, reason, git_commit, status
                )
                VALUES (
                    :access_id, :candidate_id, :fingerprint,
                    :actor, :reason, :git_commit, 'access_started'
                )
                ON CONFLICT (candidate_id, holdout_content_fingerprint) DO NOTHING
                RETURNING access_id
                """
            ),
            {
                "access_id": access_id,
                "candidate_id": request.candidate_id,
                "fingerprint": request.holdout_content_fingerprint,
                "actor": request.actor,
                "reason": request.reason.strip(),
                "git_commit": request.git_commit,
            },
        ).first()
        if row is None:
            self._session.rollback()
            raise ValueError("holdout_already_accessed")
        # This commit is the safety boundary: evaluation must never run first.
        self._session.commit()
        return access_id

    def complete(
        self,
        *,
        access_id: str,
        status: str,
        artifact_path: str,
        artifact_sha256: str,
        result_payload: Mapping[str, Any],
    ) -> None:
        if status not in {"evaluated_pass", "evaluated_fail"}:
            raise ValueError("invalid_holdout_completion_status")
        result = self._session.execute(
            text(
                """
                UPDATE governance.research_holdout_access_ledger
                SET status = :status,
                    artifact_path = :artifact_path,
                    artifact_sha256 = :artifact_sha256,
                    result_payload = CAST(:result_payload AS jsonb),
                    completed_at = NOW(),
                    error_message = NULL
                WHERE access_id = :access_id AND status = 'access_started'
                """
            ),
            {
                "access_id": access_id,
                "status": status,
                "artifact_path": artifact_path,
                "artifact_sha256": artifact_sha256,
                "result_payload": json.dumps(result_payload, ensure_ascii=False),
            },
        )
        if result.rowcount != 1:
            self._session.rollback()
            raise RuntimeError("holdout_terminal_transition_conflict")
        self._session.commit()

    def fail(self, *, access_id: str, error_type: str) -> None:
        self._session.rollback()
        result = self._session.execute(
            text(
                """
                UPDATE governance.research_holdout_access_ledger
                SET status = 'access_failed',
                    error_message = :error_type,
                    completed_at = NOW()
                WHERE access_id = :access_id AND status = 'access_started'
                """
            ),
            {"access_id": access_id, "error_type": error_type[:128]},
        )
        if result.rowcount == 1:
            self._session.commit()
        else:
            self._session.rollback()


def _reject_secret_material(value: Any, *, path: str = "result") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in _SECRET_MARKERS):
                raise ValueError(f"secret_material_forbidden:{path}.{key}")
            _reject_secret_material(item, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")


def _write_artifact_once(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link creates the final name only if it does not already exist.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return digest


def evaluate_holdout_once(
    *,
    ledger: HoldoutLedger,
    request: HoldoutAccessRequest,
    artifact_path: Path,
    evaluator: Callable[[], HoldoutEvaluationOutcome],
    evaluated_at: datetime | None = None,
) -> HoldoutEvaluationResult:
    """Claim the fingerprint, then and only then invoke the data-reading callback."""

    access_id = ledger.begin(request)
    timestamp = evaluated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        ledger.fail(access_id=access_id, error_type="naive_evaluated_at")
        raise ValueError("evaluated_at_must_be_timezone_aware")
    try:
        outcome = evaluator()
        status = "evaluated_pass" if outcome.passed else "evaluated_fail"
        if status not in _TERMINAL_STATUSES:
            raise AssertionError("unreachable_holdout_status")
        payload = {
            "format_version": 1,
            "access_id": access_id,
            "candidate_id": request.candidate_id,
            "holdout_content_fingerprint": request.holdout_content_fingerprint,
            "status": status,
            "metrics": dict(outcome.metrics),
            "gate_failures": list(outcome.gate_failures),
            "dataset_fingerprint": outcome.dataset_fingerprint,
            "execution_evidence_fingerprint": outcome.execution_evidence_fingerprint,
            "evaluated_at": timestamp.astimezone(UTC).isoformat(),
            "git_commit": request.git_commit,
            "authorization_boundary": (
                "research holdout evidence only; no live-trading authorization"
            ),
        }
        _reject_secret_material(payload)
        artifact_sha256 = _write_artifact_once(artifact_path, payload)
        ledger.complete(
            access_id=access_id,
            status=status,
            artifact_path=artifact_path.as_posix(),
            artifact_sha256=artifact_sha256,
            result_payload=payload,
        )
        return HoldoutEvaluationResult(
            access_id=access_id,
            candidate_id=request.candidate_id,
            status=status,
            artifact_path=artifact_path.as_posix(),
            artifact_sha256=artifact_sha256,
            evaluated_at=timestamp.astimezone(UTC),
        )
    except Exception as exc:
        ledger.fail(access_id=access_id, error_type=type(exc).__name__)
        raise
