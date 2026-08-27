"""Approved-only release cycle for RDP parameter governance."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aats.data_platform.decision_system.evidence_bundle import make_combo_key
from aats.data_platform.decision_system.promotion_qualification import (
    PromotionQualificationVerdict,
    evaluate_promotion_qualifications,
)
from aats.data_platform.decision_system.recommendation_registry import (
    load_recommendation_registry,
)
from aats.data_platform.operations.environment_guard import get_current_environment
from aats.data_platform.governance._time_util import parse_iso_datetime_utc
from aats.data_platform.production_workflow.release_registry import (
    create_parameter_release,
    load_release_history,
)

log = logging.getLogger(__name__)

_RELEASE_CYCLE_ROOT = Path("artifacts/production_workflow/release_cycles")
_AUTO_RELEASE_TYPES = frozenset({"parameter_upgrade"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_cycle_id() -> str:
    return f"relcy_{_utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _canonical_approval_instant(value: Any, *, field: str) -> datetime:
    if not isinstance(value, (str, datetime)):
        raise ValueError(f"{field}_missing")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{field}_naive")
    else:
        token = value.strip()
        if not token or not (token.endswith("Z") or token.endswith("+00:00")):
            raise ValueError(f"{field}_not_canonical_utc")
    parsed = parse_iso_datetime_utc(value, context=f"release_cycle.{field}")
    if parsed is None:
        raise ValueError(f"{field}_missing")
    return parsed


def _approval_sort_key(
    recommendation: dict[str, Any],
) -> tuple[datetime, datetime]:
    approved_raw = recommendation.get("approved_at")
    created_raw = recommendation.get("created_at")
    authorization = _canonical_approval_instant(
        approved_raw if approved_raw is not None else created_raw,
        field="approved_at_or_created_at",
    )
    created = (
        _canonical_approval_instant(created_raw, field="created_at")
        if created_raw is not None
        else datetime.min.replace(tzinfo=timezone.utc)
    )
    return authorization, created


def _release_cycle_dir(project_root: Path, cycle_id: str) -> Path:
    return project_root / _RELEASE_CYCLE_ROOT / cycle_id


def _build_markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "# Release Cycle Report",
        "",
        f"- Cycle ID: `{result.get('cycle_id')}`",
        f"- Environment: `{result.get('environment')}`",
        f"- Started At: `{result.get('started_at')}`",
        f"- Finished At: `{result.get('finished_at')}`",
        f"- Dry Run: `{result.get('dry_run')}`",
        f"- Reviewed Recommendations: `{result.get('reviewed_count', 0)}`",
        f"- Eligible Recommendations: `{result.get('eligible_count', 0)}`",
        f"- Selected Recommendations: `{result.get('selected_count', 0)}`",
        f"- Created Releases: `{result.get('created_release_count', 0)}`",
        f"- Gate Blocked: `{result.get('blocked_count', 0)}`",
        f"- Failures: `{result.get('failed_count', 0)}`",
        "",
        "## Results",
        "",
        "| Combo | Recommendation | Outcome | Detail |",
        "|---|---|---|---|",
    ]
    for item in result.get("results", []):
        lines.append(
            "| {combo} | `{rec}` | `{outcome}` | {detail} |".format(
                combo=item.get("combo_key") or "-",
                rec=item.get("recommendation_id") or "-",
                outcome=item.get("outcome") or "-",
                detail=item.get("detail") or "-",
            ),
        )
    return "\n".join(lines) + "\n"


def _save_cycle_result(project_root: Path, result: dict[str, Any]) -> dict[str, str]:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    cycle_dir = _release_cycle_dir(project_root, result["cycle_id"])
    cycle_dir.mkdir(parents=True, exist_ok=True)

    summary_path = cycle_dir / "release_cycle_summary.json"
    atomic_json_write(result, summary_path)

    report_path = cycle_dir / "release_cycle_report.md"
    report_path.write_text(_build_markdown_report(result), encoding="utf-8")

    return {
        "summary_path": str(summary_path),
        "report_path": str(report_path),
    }


def _select_release_candidates(
    registry: dict[str, Any],
    release_history: dict[str, Any],
    *,
    qualification_verdicts: dict[str, PromotionQualificationVerdict],
) -> dict[str, Any]:
    # 成功和未收口（pending / unknown）release 都必须隔离。pending 可能表示
    # active-parameter 事务已经成功、但最终 release 状态回写失败；自动重试会
    # 再次应用旧目标，甚至覆盖期间发生的 rollback。只有明确
    # blocked_by_gate / failed 的已知未生效结果才允许下一周期重试。
    # 语义必须与 rdp_control_summary.py 的 released_success_recommendation_ids
    # 保持一致，否则会出现 "UI 说还能发，release_cycle 永远跳过" 的矛盾。
    existing_release_by_rec: dict[str, dict[str, Any]] = {}
    unresolved_release_by_rec: dict[str, dict[str, Any]] = {}
    unresolved_release_by_combo: dict[str, dict[str, Any]] = {}
    for release in release_history.get("releases", []):
        recommendation_id = release.get("recommendation_id")
        if (
            recommendation_id
            and release.get("apply_result") not in {"blocked_by_gate", "failed"}
        ):
            existing_release_by_rec[recommendation_id] = release
        if release.get("apply_result") not in {
            "success",
            "blocked_by_gate",
            "failed",
        }:
            if recommendation_id:
                unresolved_release_by_rec[str(recommendation_id)] = release
            release_combo = make_combo_key(
                release.get("family"), release.get("timeframe")
            )
            if release_combo:
                unresolved_release_by_combo[release_combo] = release

    reviewed_count = 0
    skipped: list[dict[str, Any]] = []
    approved_by_combo: dict[str, list[dict[str, Any]]] = {}

    for recommendation in registry.get("recommendations", []):
        reviewed_count += 1
        recommendation_id = recommendation.get("recommendation_id")
        combo_key = make_combo_key(
            recommendation.get("family"),
            recommendation.get("timeframe"),
        )
        if recommendation.get("status") != "approved":
            continue
        if recommendation.get("recommendation_type") not in _AUTO_RELEASE_TYPES:
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": "不是自动发布类型",
                },
            )
            continue
        if not combo_key:
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": None,
                    "outcome": "skipped",
                    "detail": "family/timeframe 不完整",
                },
            )
            continue
        approved_by_combo.setdefault(combo_key, []).append(recommendation)

    selected: list[dict[str, Any]] = []
    for combo_key in sorted(approved_by_combo):
        recommendations = approved_by_combo[combo_key]
        combo_unresolved = unresolved_release_by_combo.get(combo_key)
        if combo_unresolved is None:
            for recommendation in recommendations:
                rec_id = str(recommendation.get("recommendation_id") or "")
                if rec_id in unresolved_release_by_rec:
                    combo_unresolved = unresolved_release_by_rec[rec_id]
                    break
        if combo_unresolved is not None:
            for recommendation in recommendations:
                skipped.append(
                    {
                        "recommendation_id": recommendation.get(
                            "recommendation_id"
                        ),
                        "combo_key": combo_key,
                        "outcome": "audit_only",
                        "detail": "组合存在未收口 release，需先完成 reconciliation",
                        "reason_code": "combo_release_reconciliation_required",
                        "release_id": combo_unresolved.get("release_id"),
                        "apply_result": combo_unresolved.get("apply_result"),
                    }
                )
            continue

        keyed: list[tuple[tuple[datetime, datetime], dict[str, Any]]] = []
        invalid_order = False
        if len(recommendations) == 1:
            keyed.append(
                (
                    (
                        datetime.min.replace(tzinfo=timezone.utc),
                        datetime.min.replace(tzinfo=timezone.utc),
                    ),
                    recommendations[0],
                )
            )
        else:
            for recommendation in recommendations:
                try:
                    keyed.append((_approval_sort_key(recommendation), recommendation))
                except (TypeError, ValueError):
                    invalid_order = True
        if invalid_order:
            for recommendation in recommendations:
                skipped.append(
                    {
                        "recommendation_id": recommendation.get(
                            "recommendation_id"
                        ),
                        "combo_key": combo_key,
                        "outcome": "audit_only",
                        "detail": "批准时间缺失或不是 canonical UTC",
                        "reason_code": "approval_timestamp_invalid",
                    }
                )
            continue
        keyed.sort(key=lambda item: item[0], reverse=True)
        if len(keyed) > 1 and keyed[0][0] == keyed[1][0]:
            for _key, recommendation in keyed:
                skipped.append(
                    {
                        "recommendation_id": recommendation.get(
                            "recommendation_id"
                        ),
                        "combo_key": combo_key,
                        "outcome": "audit_only",
                        "detail": "同一组合存在无法确定先后的并列批准授权",
                        "reason_code": "ambiguous_approval_order",
                    }
                )
            continue

        recommendation = keyed[0][1]
        recommendation_id = recommendation.get("recommendation_id")
        for _key, older in keyed[1:]:
            skipped.append(
                {
                    "recommendation_id": older.get("recommendation_id"),
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": "同一组合已有更新的 approved recommendation",
                },
            )
        verdict = qualification_verdicts.get(str(recommendation_id or ""))
        if verdict is None or verdict.eligible is not True:
            reason_code = (
                verdict.reason_code
                if verdict is not None
                else "promotion_qualification_not_evaluated"
            )
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "audit_only",
                    "detail": f"最新参数升级资格不满足: {reason_code}",
                    "reason_code": reason_code,
                    "promotion_qualification": (
                        verdict.to_dict() if verdict is not None else None
                    ),
                },
            )
            continue
        if len(recommendations) == 1:
            try:
                _approval_sort_key(recommendation)
            except (TypeError, ValueError):
                skipped.append(
                    {
                        "recommendation_id": recommendation_id,
                        "combo_key": combo_key,
                        "outcome": "audit_only",
                        "detail": "批准时间缺失或不是 canonical UTC",
                        "reason_code": "approval_timestamp_invalid",
                    }
                )
                continue
        if not recommendation.get("target_parameter_set_id"):
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "audit_only",
                    "detail": "最新批准授权缺少 target_parameter_set_id",
                    "reason_code": "latest_approval_target_missing",
                }
            )
            continue
        if recommendation_id in existing_release_by_rec:
            existing = existing_release_by_rec[recommendation_id]
            apply_result = existing.get("apply_result")
            skipped.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "skipped",
                    "detail": (
                        "已存在成功 release 记录"
                        if apply_result == "success"
                        else "存在未收口 release，需先完成 reconciliation"
                    ),
                    "release_id": existing.get("release_id"),
                    "apply_result": apply_result,
                }
            )
            continue
        selected.append(recommendation)

    return {
        "reviewed_count": reviewed_count,
        "eligible": selected,
        "skipped": skipped,
    }


def _release_cycle_lock_failure(
    *,
    cycle_id: str,
    environment: str,
    dry_run: bool,
    started_at: datetime,
    error: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "cycle_id": cycle_id,
        "environment": environment,
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "finished_at": _utcnow().isoformat(),
        "reviewed_count": 0,
        "eligible_count": 0,
        "selected_count": 0,
        "created_release_count": 0,
        "blocked_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
        "error": error,
    }


def run_release_cycle(
    project_root: Path,
    *,
    actor: str = "release_cycle",
    dry_run: bool = False,
    save_results: bool = True,
) -> dict[str, Any]:
    started_at = _utcnow()
    cycle_id = _make_cycle_id()
    environment = get_current_environment()

    # 跨进程互斥：同一时刻只允许一个 release_cycle 在跑，避免两个进程对同一条
    # approved recommendation 并发 create_parameter_release 导致重复发布。
    # DB 不可用或锁查询异常时必须阻断；资本动作不能退化为无锁运行。
    lock_engine = None
    lock_session = None
    if not dry_run:
        try:
            from sqlalchemy.orm import Session as SQLSession

            from aats.data_platform.governance._db_util import try_governance_db
            from aats.data_platform.governance.operational_state_db import (
                try_acquire_release_cycle_lock,
            )

            lock_engine, lock_ok = try_governance_db()
            if not lock_ok or lock_engine is None:
                return _release_cycle_lock_failure(
                    cycle_id=cycle_id,
                    environment=environment,
                    dry_run=dry_run,
                    started_at=started_at,
                    error="release_cycle 治理数据库锁不可用，已阻断发布",
                )
            lock_session = SQLSession(lock_engine)
            if not try_acquire_release_cycle_lock(lock_session):
                lock_session.close()
                lock_session = None
                lock_engine.dispose()
                lock_engine = None
                return _release_cycle_lock_failure(
                    cycle_id=cycle_id,
                    environment=environment,
                    dry_run=dry_run,
                    started_at=started_at,
                    error="另一个 release_cycle 正在运行（advisory lock 被持有）",
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.error(
                "release_cycle advisory lock 获取失败，已阻断发布 (%s)",
                type(exc).__name__,
            )
            if lock_session is not None:
                lock_session.close()
            lock_session = None
            if lock_engine is not None:
                lock_engine.dispose()
            lock_engine = None
            return _release_cycle_lock_failure(
                cycle_id=cycle_id,
                environment=environment,
                dry_run=dry_run,
                started_at=started_at,
                error="release_cycle 并发锁获取失败，已阻断发布",
            )

    try:
        return _run_release_cycle_locked(
            project_root,
            actor=actor,
            dry_run=dry_run,
            save_results=save_results,
            started_at=started_at,
            cycle_id=cycle_id,
            environment=environment,
        )
    finally:
        if lock_session is not None:
            try:
                from aats.data_platform.governance.operational_state_db import (
                    release_release_cycle_lock,
                )

                release_release_cycle_lock(lock_session)
            finally:
                lock_session.close()
        if lock_engine is not None:
            lock_engine.dispose()


def _run_release_cycle_locked(
    project_root: Path,
    *,
    actor: str,
    dry_run: bool,
    save_results: bool,
    started_at: datetime,
    cycle_id: str,
    environment: str,
) -> dict[str, Any]:
    registry_path = project_root / "artifacts/decision_system/recommendation_registry.json"
    registry = load_recommendation_registry(registry_path)
    release_history = load_release_history(project_root)
    recommendations = [
        recommendation
        for recommendation in (registry.get("recommendations") or [])
        if isinstance(recommendation, dict)
    ]
    qualification_verdicts = evaluate_promotion_qualifications(
        project_root,
        recommendations,
    )
    selection = _select_release_candidates(
        registry,
        release_history,
        qualification_verdicts=qualification_verdicts,
    )

    results: list[dict[str, Any]] = list(selection["skipped"])
    created_release_count = 0
    blocked_count = 0
    failed_count = 0

    for recommendation in selection["eligible"]:
        recommendation_id = str(recommendation.get("recommendation_id"))
        combo_key = make_combo_key(
            recommendation.get("family"),
            recommendation.get("timeframe"),
        )

        if dry_run:
            results.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "dry_run",
                    "detail": "仅评估，不创建 release",
                },
            )
            continue

        try:
            release_result = create_parameter_release(
                project_root,
                recommendation_id=recommendation_id,
                actor=actor,
                run_gate=True,
                run_apply=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            failed_count += 1
            log.exception("release cycle failed for %s", recommendation_id)
            results.append(
                {
                    "recommendation_id": recommendation_id,
                    "combo_key": combo_key,
                    "outcome": "failed",
                    "detail": str(exc),
                },
            )
            continue

        release = release_result.get("release") or {}
        apply_result = release.get("apply_result")
        outcome = "release_created"
        detail = release_result.get("message") or "release created"
        if apply_result == "success":
            created_release_count += 1
        elif apply_result == "blocked_by_gate":
            blocked_count += 1
            outcome = "blocked_by_gate"
            detail = release_result.get("message") or "gate blocked apply"
        elif release_result.get("ok") is False:
            failed_count += 1
            outcome = "failed"

        results.append(
            {
                "recommendation_id": recommendation_id,
                "combo_key": combo_key,
                "outcome": outcome,
                "detail": detail,
                "release_id": release.get("release_id"),
                "apply_result": apply_result,
                "gate_status": release.get("gate_status"),
            },
        )

    finished_at = _utcnow()
    result: dict[str, Any] = {
        "ok": failed_count == 0,
        "cycle_id": cycle_id,
        "environment": environment,
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "reviewed_count": selection["reviewed_count"],
        "eligible_count": len(selection["eligible"]),
        "selected_count": len(selection["eligible"]),
        "created_release_count": created_release_count,
        "blocked_count": blocked_count,
        "failed_count": failed_count,
        "skipped_count": len(selection["skipped"]),
        "results": results,
    }

    if save_results:
        result["artifacts"] = _save_cycle_result(project_root, result)

    return result
