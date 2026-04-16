"""DB-first governance snapshots and research round snapshots."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import json_dumps, parse_dt, try_governance_db

log = logging.getLogger(__name__)

SNAPSHOT_ARTIFACT_INDEX = "artifact_index"
SNAPSHOT_ACTIVE_ROUND_INDEX = "active_round_index"
SNAPSHOT_QUALITY_MONITOR = "quality_monitor_summary"

ROUND_PHASE_STEP2 = "phase2_step2"
ROUND_PHASE_PHASE3 = "phase3"
ROUND_PHASE_PHASE4 = "phase4"

_SNAPSHOT_FILE_MAP: dict[str, str] = {
    SNAPSHOT_ARTIFACT_INDEX: "artifacts/governance/artifact_index.json",
    SNAPSHOT_ACTIVE_ROUND_INDEX: "artifacts/governance/active_round_index.json",
    SNAPSHOT_QUALITY_MONITOR: "artifacts/governance/quality_monitor_summary.json",
}
_ROUND_PHASE_ROOTS: dict[str, str] = {
    ROUND_PHASE_STEP2: "artifacts/research/step2_rounds",
    ROUND_PHASE_PHASE3: "artifacts/research/attribution_rounds",
    ROUND_PHASE_PHASE4: "artifacts/research/execution_rounds",
}
_ROUND_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to load JSON from %s: %s", path, exc)
        return None


def _find_latest_round_dir(root: Path) -> Path | None:
    if not root.exists():
        return None
    dirs = [item for item in root.iterdir() if item.is_dir()]
    canonical = [item for item in dirs if _ROUND_DIR_PATTERN.match(item.name)]
    target_dirs = canonical or dirs
    dirs = sorted(target_dirs, key=lambda item: item.name, reverse=True)
    return dirs[0] if dirs else None


def _build_round_snapshot_from_dir(
    *,
    round_dir: Path,
    phase: str,
) -> dict[str, Any] | None:
    manifest = _safe_load_json(round_dir / "round_manifest.json")

    if phase == ROUND_PHASE_STEP2:
        if not isinstance(manifest, dict):
            manifest = {
                "round_id": round_dir.name,
                "started_at": None,
                "finished_at": None,
                "overall_status": "completed",
            }
        summary_payload = {
            "family_timeframe_summary": _safe_load_json(round_dir / "family_timeframe_summary.json") or {},
            "scan_comparison_summary": _safe_load_json(round_dir / "scan_comparison_summary.json") or {},
            "parameter_candidates": _safe_load_json(round_dir / "parameter_candidates.json") or {},
        }
        artifacts_payload = {
            "family_timeframe_summary_json": str(round_dir / "family_timeframe_summary.json"),
            "scan_comparison_summary_json": str(round_dir / "scan_comparison_summary.json"),
            "parameter_candidates_json": str(round_dir / "parameter_candidates.json"),
        }
    elif phase == ROUND_PHASE_PHASE3:
        if not isinstance(manifest, dict):
            manifest = {
                "round_id": round_dir.name,
                "started_at": None,
                "finished_at": None,
                "overall_status": "completed",
            }
        combos: dict[str, Any] = {}
        for combo_dir in sorted((item for item in round_dir.iterdir() if item.is_dir()), key=lambda item: item.name):
            combo_summary = _safe_load_json(combo_dir / "attribution_summary.json")
            if isinstance(combo_summary, dict):
                combos[combo_dir.name] = {
                    "attribution_summary": combo_summary,
                }
        top_summary = _safe_load_json(round_dir / "attribution_summary.json")
        summary_payload = {
            "summary_rows": top_summary.get("summary_rows", []) if isinstance(top_summary, dict) else [],
            "combos": combos,
        }
        artifacts_payload = {
            "summary_json": str(round_dir / "attribution_summary.json"),
        }
    elif phase == ROUND_PHASE_PHASE4:
        if not isinstance(manifest, dict):
            manifest = {
                "round_id": round_dir.name,
                "started_at": None,
                "finished_at": None,
                "overall_status": "completed",
            }
        combos = {}
        for combo_dir in sorted((item for item in round_dir.iterdir() if item.is_dir()), key=lambda item: item.name):
            combo_summary = _safe_load_json(combo_dir / "execution_summary.json")
            if isinstance(combo_summary, dict):
                combos[combo_dir.name] = {
                    "cost_summary": combo_summary,
                }
        top_summary = _safe_load_json(round_dir / "execution_summary.json")
        summary_payload = {
            "comparison_rows": top_summary.get("comparison_rows", []) if isinstance(top_summary, dict) else [],
            "cross_findings": top_summary.get("cross_findings", []) if isinstance(top_summary, dict) else [],
            "combos": combos,
        }
        artifacts_payload = {
            "summary_json": str(round_dir / "execution_summary.json"),
        }
    else:
        return None

    return {
        "round_id": manifest.get("round_id", round_dir.name),
        "phase": phase,
        "status": manifest.get("overall_status", manifest.get("status", "completed")),
        "round_path": str(round_dir),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "replay_only": bool(manifest.get("replay_only", False)),
        "manifest": manifest,
        "summary": summary_payload,
        "conclusion": {},
        "artifacts": artifacts_payload,
        "data_source": "file",
    }


def db_upsert_governance_snapshot(
    session: Session,
    *,
    snapshot_type: str,
    payload: dict[str, Any],
) -> None:
    generated_at = parse_dt(payload.get("generated_at")) or _utcnow()
    session.execute(
        text(
            """
            INSERT INTO governance.snapshots
                (snapshot_type, generated_at, payload, created_at, updated_at)
            VALUES
                (:snapshot_type, :generated_at, CAST(:payload AS jsonb), :now, :now)
            ON CONFLICT (snapshot_type) DO UPDATE SET
                generated_at = EXCLUDED.generated_at,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "snapshot_type": snapshot_type,
            "generated_at": generated_at,
            "payload": json_dumps(payload),
            "now": _utcnow(),
        },
    )


def db_load_governance_snapshot(
    session: Session,
    *,
    snapshot_type: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT snapshot_type, generated_at, payload
            FROM governance.snapshots
            WHERE snapshot_type = :snapshot_type
            """
        ),
        {"snapshot_type": snapshot_type},
    ).fetchone()
    if row is None:
        return None
    payload = dict(row.payload or {})
    payload.setdefault("snapshot_type", row.snapshot_type)
    if row.generated_at and "generated_at" not in payload:
        payload["generated_at"] = row.generated_at.isoformat()
    return payload


def save_governance_snapshot(
    *,
    snapshot_type: str,
    payload: dict[str, Any],
) -> bool:
    engine, ok = try_governance_db()
    if not ok:
        return False
    try:
        with Session(engine) as session:
            db_upsert_governance_snapshot(session, snapshot_type=snapshot_type, payload=payload)
            session.commit()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("DB upsert governance snapshot failed (%s): %s", snapshot_type, exc)
        return False
    finally:
        if engine is not None:
            engine.dispose()


def load_governance_snapshot(
    project_root: Path,
    *,
    snapshot_type: str,
) -> dict[str, Any] | None:
    engine, ok = try_governance_db()
    db_reachable = False
    if ok:
        db_reachable = True
        try:
            with Session(engine) as session:
                payload = db_load_governance_snapshot(session, snapshot_type=snapshot_type)
            # DB 是真源：即便返回 None 也要信任（表示"还未写入"），不要回落到
            # 磁盘上遗留的旧 JSON，否则 UI / gate 可能把昨天的快照当作今天的现实。
            if payload is not None:
                payload.setdefault("data_source", "db")
            return payload
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("DB load governance snapshot failed (%s): %s", snapshot_type, exc)
            db_reachable = False
        finally:
            if engine is not None:
                engine.dispose()

    rel_path = _SNAPSHOT_FILE_MAP.get(snapshot_type)
    if not rel_path:
        return None
    payload = _safe_load_json(project_root / rel_path)
    if isinstance(payload, dict):
        # DB 未配置 / 挂了时退化到文件；明确标注为 file_fallback，下游可据此判断
        # 是否应显示"快照来源降级，数据可能是陈旧的"提示。
        payload.setdefault("data_source", "file_fallback" if db_reachable else "file")
        return payload
    return None


def db_upsert_research_round_snapshot(
    session: Session,
    *,
    round_id: str,
    phase: str,
    status: str,
    round_path: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    replay_only: bool = False,
    manifest_payload: dict[str, Any] | None = None,
    summary_payload: dict[str, Any] | None = None,
    conclusion_payload: dict[str, Any] | None = None,
    artifacts_payload: dict[str, Any] | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO governance.research_round_snapshots
                (round_id, phase, status, round_path,
                 started_at, finished_at, replay_only,
                 manifest_payload, summary_payload, conclusion_payload, artifacts_payload,
                 created_at, updated_at)
            VALUES
                (:round_id, :phase, :status, :round_path,
                 :started_at, :finished_at, :replay_only,
                 CAST(:manifest_payload AS jsonb),
                 CAST(:summary_payload AS jsonb),
                 CAST(:conclusion_payload AS jsonb),
                 CAST(:artifacts_payload AS jsonb),
                 :now, :now)
            ON CONFLICT (round_id) DO UPDATE SET
                phase = EXCLUDED.phase,
                status = EXCLUDED.status,
                round_path = EXCLUDED.round_path,
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at,
                replay_only = EXCLUDED.replay_only,
                manifest_payload = EXCLUDED.manifest_payload,
                summary_payload = EXCLUDED.summary_payload,
                conclusion_payload = EXCLUDED.conclusion_payload,
                artifacts_payload = EXCLUDED.artifacts_payload,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "round_id": round_id,
            "phase": phase,
            "status": status,
            "round_path": round_path,
            "started_at": parse_dt(started_at),
            "finished_at": parse_dt(finished_at),
            "replay_only": replay_only,
            "manifest_payload": json_dumps(manifest_payload or {}),
            "summary_payload": json_dumps(summary_payload or {}),
            "conclusion_payload": json_dumps(conclusion_payload or {}),
            "artifacts_payload": json_dumps(artifacts_payload or {}),
            "now": _utcnow(),
        },
    )


def _normalize_round_snapshot_row(row: Any) -> dict[str, Any]:
    manifest_payload = dict(row.manifest_payload or {})
    summary_payload = dict(row.summary_payload or {})
    conclusion_payload = dict(row.conclusion_payload or {})
    artifacts_payload = dict(row.artifacts_payload or {})
    return {
        "round_id": row.round_id,
        "phase": row.phase,
        "status": row.status,
        "round_path": row.round_path,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "replay_only": bool(row.replay_only),
        "manifest": manifest_payload,
        "summary": summary_payload,
        "conclusion": conclusion_payload,
        "artifacts": artifacts_payload,
    }


def db_load_latest_research_round_snapshot(
    session: Session,
    *,
    phase: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT round_id, phase, status, round_path,
                   started_at, finished_at, replay_only,
                   manifest_payload, summary_payload, conclusion_payload, artifacts_payload
            FROM governance.research_round_snapshots
            WHERE phase = :phase
            ORDER BY COALESCE(finished_at, started_at, updated_at, created_at) DESC, round_id DESC
            LIMIT 1
            """
        ),
        {"phase": phase},
    ).fetchone()
    if row is None:
        return None
    return _normalize_round_snapshot_row(row)


def db_list_research_round_snapshots(
    session: Session,
    *,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    if phase:
        rows = session.execute(
            text(
                """
                SELECT round_id, phase, status, round_path,
                       started_at, finished_at, replay_only,
                       manifest_payload, summary_payload, conclusion_payload, artifacts_payload
                FROM governance.research_round_snapshots
                WHERE phase = :phase
                ORDER BY COALESCE(finished_at, started_at, updated_at, created_at) DESC, round_id DESC
                """
            ),
            {"phase": phase},
        ).fetchall()
    else:
        rows = session.execute(
            text(
                """
                SELECT round_id, phase, status, round_path,
                       started_at, finished_at, replay_only,
                       manifest_payload, summary_payload, conclusion_payload, artifacts_payload
                FROM governance.research_round_snapshots
                ORDER BY COALESCE(finished_at, started_at, updated_at, created_at) DESC, round_id DESC
                """
            ),
        ).fetchall()
    return [_normalize_round_snapshot_row(row) for row in rows]


def db_load_research_round_snapshot(
    session: Session,
    *,
    round_id: str,
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT round_id, phase, status, round_path,
                   started_at, finished_at, replay_only,
                   manifest_payload, summary_payload, conclusion_payload, artifacts_payload
            FROM governance.research_round_snapshots
            WHERE round_id = :round_id
            LIMIT 1
            """
        ),
        {"round_id": round_id},
    ).fetchone()
    if row is None:
        return None
    return _normalize_round_snapshot_row(row)


def save_research_round_snapshot(
    *,
    round_id: str,
    phase: str,
    status: str,
    round_path: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    replay_only: bool = False,
    manifest_payload: dict[str, Any] | None = None,
    summary_payload: dict[str, Any] | None = None,
    conclusion_payload: dict[str, Any] | None = None,
    artifacts_payload: dict[str, Any] | None = None,
) -> bool:
    engine, ok = try_governance_db()
    if not ok:
        return False
    try:
        with Session(engine) as session:
            db_upsert_research_round_snapshot(
                session,
                round_id=round_id,
                phase=phase,
                status=status,
                round_path=round_path,
                started_at=started_at,
                finished_at=finished_at,
                replay_only=replay_only,
                manifest_payload=manifest_payload,
                summary_payload=summary_payload,
                conclusion_payload=conclusion_payload,
                artifacts_payload=artifacts_payload,
            )
            session.commit()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("DB upsert research round snapshot failed (%s/%s): %s", phase, round_id, exc)
        return False
    finally:
        if engine is not None:
            engine.dispose()


def load_research_round_snapshot(
    *,
    round_id: str,
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    engine, ok = try_governance_db()
    db_reachable = False
    if ok:
        db_reachable = True
        try:
            with Session(engine) as session:
                snapshot = db_load_research_round_snapshot(session, round_id=round_id)
            # DB 是真源：DB 可达但没有该 round → 直接返回 None，不去扫磁盘。
            # 否则磁盘上残留的历史 round 目录会被当作 DB 里不存在的 round 的"证据"。
            if snapshot is not None:
                snapshot.setdefault("data_source", "db")
            return snapshot
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("DB load research round snapshot failed (%s): %s", round_id, exc)
            db_reachable = False
        finally:
            if engine is not None:
                engine.dispose()

    if project_root is None:
        return None
    for phase, rel_root in _ROUND_PHASE_ROOTS.items():
        round_dir = project_root / rel_root / round_id
        snapshot = _build_round_snapshot_from_dir(round_dir=round_dir, phase=phase)
        if snapshot is not None:
            snapshot.setdefault(
                "data_source", "file_fallback" if db_reachable else "file",
            )
            return snapshot
    return None


def load_latest_research_round_snapshot(
    *,
    phase: str,
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    engine, ok = try_governance_db()
    db_reachable = False
    if ok:
        db_reachable = True
        try:
            with Session(engine) as session:
                snapshot = db_load_latest_research_round_snapshot(session, phase=phase)
            # 同上：DB 可达但该 phase 没记录 → 返回 None，不要从磁盘扫出可能属于
            # 别的 round 目录的 manifest 冒充"最新" round。
            if snapshot is not None:
                snapshot.setdefault("data_source", "db")
            return snapshot
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("DB load research round snapshot failed (%s): %s", phase, exc)
            db_reachable = False
        finally:
            if engine is not None:
                engine.dispose()

    if project_root is None:
        return None
    rel_root = _ROUND_PHASE_ROOTS.get(phase)
    if not rel_root:
        return None
    round_dir = _find_latest_round_dir(project_root / rel_root)
    if round_dir is None:
        return None
    snapshot = _build_round_snapshot_from_dir(round_dir=round_dir, phase=phase)
    if snapshot is not None:
        snapshot.setdefault(
            "data_source", "file_fallback" if db_reachable else "file",
        )
    return snapshot
