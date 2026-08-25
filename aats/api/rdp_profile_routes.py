"""Profile-scope RDP API routes (v3 §1.5).

Routes:
  GET  /rdp/profile-recommendations                列出 profile-scope 建议
  GET  /rdp/profile-recommendations/{id}           详情
  POST /rdp/profile-recommendations/{id}/approve   session only
  POST /rdp/profile-recommendations/{id}/reject    session only
  POST /rdp/profile-recommendations/{id}/gate      运行 3 指标 gate 预检
  POST /rdp/profile-recommendations/{id}/release   session only
  POST /rdp/profile-recommendations/{id}/apply     token v2 + fail-closed until runtime activation/readback exists
  POST /rdp/profile-recommendations/{id}/rollback  token v2 + fail-closed until reverse saga exists
  GET  /rdp/profile-type-reviews                   profile_type_review 列表
  POST /rdp/profile-type-reviews/{id}/resolve      session only

  GET  /rdp/sleeve-advice/recent                   sleeve 观察视图(R2-14)
  POST /rdp/sleeve-advice/{id}/mark-reviewed       UI-only 标记
  POST /rdp/sleeve-advice/{id}/approve             403 "observation-only"
  POST /rdp/sleeve-advice/{id}/release             403 "observation-only"
  POST /rdp/sleeve-advice/{id}/apply               403 "observation-only"

apply / rollback 需 X-Rdp-Apply-Token(v2 格式):
  token payload = actor|action|scope|recommendation_id|exp_ts|sig

approve / reject / release / review_resolve 仅需 session cookie(与 combo 流一致)。

v2 token 防重放(R2-04): 一枚 combo token 拿来 apply profile 会被 scope_mismatch
拒绝,反之亦然。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from aats.api._governance_db import governance_session as _governance_session
from aats.api.auth import (
    OperatorPrincipal,
    require_read_access,
    require_write_access,
)
from aats.api.rdp_apply_token import InvalidTokenError, verify_token
from aats.data_platform.gates.profile_gate import (
    check_profile_gate,
    compute_metrics_from_replay,
)

logger = logging.getLogger(__name__)

profile_router = APIRouter(prefix="/rdp", tags=["RDP-Profile"])


# =============================================================================
# Helpers
# =============================================================================

def _enforce_token_actor(
    *, principal: OperatorPrincipal, token_actor: str, action: str,
) -> None:
    """Session identity == token actor。auth 禁用时放行。"""
    if not principal.auth_enabled:
        return
    session_id = (principal.identity or "").strip()
    if session_id and session_id == token_actor:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "actor_mismatch",
            "action": action,
            "session_actor": session_id,
            "token_actor": token_actor,
        },
    )


def _enforce_dual_operator(
    *,
    principal: OperatorPrincipal,
    approver: str | None,
    action: str,
) -> None:
    """Apply / rollback 需要 approver != applier(R2-04 双人签)。"""
    if not principal.auth_enabled:
        return
    if not approver:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_approver_recorded",
                "action": action,
                "message": "recommendation 没有 approver 字段,拒绝 apply",
            },
        )
    if (principal.identity or "").strip() == approver.strip():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approver_equals_applier",
                "action": action,
                "approver": approver,
            },
        )


def _load_profile_rec(session: Any, rec_id: str) -> dict[str, Any]:
    row = session.execute(text("""
        SELECT recommendation_id, scope, scope_ref, recommendation_type,
               target_parameter_set_id,
               confidence, reason, status, review_notes,
               approved_by, approved_at, created_at
        FROM governance.recommendations
        WHERE recommendation_id = :rid AND scope = 'profile'
    """), {"rid": rec_id}).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "profile_rec_not_found", "recommendation_id": rec_id},
        )
    notes = row.review_notes
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except Exception:
            notes = {"raw": notes}
    return {
        "recommendation_id": row.recommendation_id,
        "scope": row.scope,
        "scope_ref": row.scope_ref,
        "recommendation_type": row.recommendation_type,
        "target_parameter_set_id": row.target_parameter_set_id,
        "confidence": row.confidence,
        "reason": row.reason,
        "status": row.status,
        "review_notes": notes,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _load_parameter_set(session: Any, ps_id: str) -> dict[str, Any]:
    row = session.execute(text("""
        SELECT parameter_set_id, scope, scope_ref, values, status
        FROM governance.parameter_sets
        WHERE parameter_set_id = :psid
    """), {"psid": ps_id}).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "parameter_set_not_found", "parameter_set_id": ps_id},
        )
    vals = row.values if not isinstance(row.values, str) else json.loads(row.values)
    return {
        "parameter_set_id": row.parameter_set_id,
        "scope": row.scope,
        "scope_ref": row.scope_ref,
        "values": vals,
        "status": row.status,
    }


def _load_current_active_profile_values(
    session: Any, profile_id: str,
) -> tuple[str | None, dict[str, Any]]:
    """读当前 active profile 的 parameter_set_id + values。"""
    row = session.execute(text("""
        SELECT parameter_set_id, values
        FROM governance.active_parameter_sets
        WHERE scope = 'profile' AND scope_ref = :pid
    """), {"pid": profile_id}).first()
    if row is None:
        return None, {}
    vals = row.values if not isinstance(row.values, str) else json.loads(row.values)
    return row.parameter_set_id, vals


def _compute_threshold_patches(
    *,
    current_values: dict[str, Any],
    new_values: dict[str, Any],
) -> dict[str, dict[str, float]]:
    """算 saga step3 的 threshold_patches。从白名单里挑有变化的 key。"""
    from aats.data_platform.governance.profile_apply_saga import PAYLOAD_WHITELIST

    patches: dict[str, dict[str, float]] = {}
    for key in PAYLOAD_WHITELIST:
        cur = current_values.get(key)
        new = new_values.get(key)
        if new is None:
            continue
        if cur != new:
            patches[key] = {"from": float(cur) if cur is not None else None, "to": float(new)}
    return patches


# =============================================================================
# Read endpoints
# =============================================================================

@profile_router.get(
    "/profile-recommendations",
    dependencies=[Depends(require_read_access)],
)
async def list_profile_recommendations(status: str | None = None) -> dict[str, Any]:
    where_parts = ["scope = 'profile'"]
    params: dict[str, Any] = {}
    if status:
        where_parts.append("status = :status")
        params["status"] = status
    sql = f"""
        SELECT recommendation_id, scope_ref, recommendation_type, confidence,
               reason, status, target_parameter_set_id,
               approved_by, approved_at, created_at
        FROM governance.recommendations
        WHERE {' AND '.join(where_parts)}
        ORDER BY created_at DESC
        LIMIT 200
    """
    with _governance_session() as session:
        rows = session.execute(text(sql), params).all()
    items = [
        {
            "recommendation_id": r.recommendation_id,
            "profile_id": r.scope_ref,
            "recommendation_type": r.recommendation_type,
            "confidence": r.confidence,
            "reason": r.reason,
            "status": r.status,
            "target_parameter_set_id": r.target_parameter_set_id,
            "approved_by": r.approved_by,
            "approved_at": r.approved_at.isoformat() if r.approved_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"total": len(items), "recommendations": items}


@profile_router.get(
    "/profile-recommendations/{rec_id}",
    dependencies=[Depends(require_read_access)],
)
async def get_profile_recommendation(rec_id: str) -> dict[str, Any]:
    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        if rec.get("target_parameter_set_id"):
            rec["candidate_values"] = _load_parameter_set(
                session, rec["target_parameter_set_id"],
            )["values"]
        cur_ps, cur_vals = _load_current_active_profile_values(
            session, rec["scope_ref"],
        )
    rec["current_parameter_set_id"] = cur_ps
    rec["current_values"] = cur_vals
    return rec


# =============================================================================
# Approve / Reject
# =============================================================================

class _ApproveRequest(BaseModel):
    notes: str | None = None


@profile_router.post("/profile-recommendations/{rec_id}/approve")
async def approve_profile_rec(
    rec_id: str,
    body: _ApproveRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    actor = (principal.identity or "").strip() or "operator"
    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        if rec["status"] != "draft":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rec_status_not_draft",
                    "current_status": rec["status"],
                },
            )
        result = session.execute(text("""
            UPDATE governance.recommendations
            SET status = 'approved',
                approved_by = :actor,
                approved_at = NOW(),
                review_notes = COALESCE(review_notes, '{}'::jsonb) ||
                    jsonb_build_object('approve_notes', :notes)
            WHERE recommendation_id = :rid AND status = 'draft'
        """), {"actor": actor, "rid": rec_id, "notes": body.notes or ""})
        if result.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "concurrent_state_change", "rec_id": rec_id},
            )
        session.commit()
    logger.info("profile approve: rec=%s actor=%s", rec_id, actor)
    return {"ok": True, "recommendation_id": rec_id, "status": "approved"}


class _RejectRequest(BaseModel):
    reason: str | None = None


@profile_router.post("/profile-recommendations/{rec_id}/reject")
async def reject_profile_rec(
    rec_id: str,
    body: _RejectRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    actor = (principal.identity or "").strip() or "operator"
    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        if rec["status"] != "draft":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rec_status_not_draft",
                    "current_status": rec["status"],
                },
            )
        result = session.execute(text("""
            UPDATE governance.recommendations
            SET status = 'rejected',
                rejected_by = :actor, rejected_at = NOW(),
                review_notes = COALESCE(review_notes, '{}'::jsonb) ||
                    jsonb_build_object('reject_reason', :reason)
            WHERE recommendation_id = :rid AND status = 'draft'
        """), {"actor": actor, "rid": rec_id, "reason": body.reason or ""})
        if result.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "concurrent_state_change", "rec_id": rec_id},
            )
        session.commit()
    return {"ok": True, "recommendation_id": rec_id, "status": "rejected"}


# =============================================================================
# Gate
# =============================================================================

class _GateRequest(BaseModel):
    current_stats: dict[str, float]
    candidate_stats: dict[str, float]


@profile_router.post(
    "/profile-recommendations/{rec_id}/gate",
    dependencies=[Depends(require_write_access)],
)
async def run_profile_gate(
    rec_id: str, body: _GateRequest,
) -> dict[str, Any]:
    """调用 profile_gate.check_profile_gate(metrics)。"""
    metrics = compute_metrics_from_replay(
        current_stats=body.current_stats,
        candidate_stats=body.candidate_stats,
    )
    result = check_profile_gate(metrics)
    return {
        "recommendation_id": rec_id,
        "metrics": metrics,
        "allow_apply": result.allow_apply,
        "failures": list(result.failures),
    }


# =============================================================================
# Release
# =============================================================================

@profile_router.post("/profile-recommendations/{rec_id}/release")
async def release_profile_rec(
    rec_id: str,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        if rec["status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rec_status_not_approved",
                    "current_status": rec["status"],
                },
            )
        result = session.execute(text("""
            UPDATE governance.recommendations
            SET status = 'released'
            WHERE recommendation_id = :rid AND status = 'approved'
        """), {"rid": rec_id})
        if result.rowcount == 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "concurrent_state_change", "rec_id": rec_id},
            )
        session.commit()
    return {"ok": True, "recommendation_id": rec_id, "status": "released"}


# =============================================================================
# Apply (saga) / Rollback
# =============================================================================

@profile_router.post(
    "/profile-recommendations/{rec_id}/apply",
    status_code=501,
)
async def apply_profile_rec(
    rec_id: str,
    request: Request,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    token_actor = _extract_profile_token(request, action="apply", rec_id=rec_id)
    _enforce_token_actor(principal=principal, token_actor=token_actor, action="apply")

    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        if rec["status"] != "released":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "rec_status_not_released",
                    "current_status": rec["status"],
                },
            )
        _enforce_dual_operator(
            principal=principal, approver=rec.get("approved_by"), action="apply",
        )
    # FS-001: research/live SQL 完成不能证明 execution runtime 已加载目标参数。
    # 现有 Saga 查询错误的 profile_id 字段，并写入不属于 activation schema 的
    # threshold；在 execution-owned generation/ack/readback 完成前只能零写入拒绝。
    logger.warning(
        "profile apply rejected fail-closed: rec=%s status=%s actor=%s",
        rec_id,
        rec.get("status"),
        token_actor,
    )
    raise HTTPException(
        status_code=501,
        detail={
            "code": "profile_apply_not_implemented",
            "message": (
                "Profile 参数应用暂不可用：安全的运行时激活、代际确认与读回尚未实现。"
            ),
            "recommendation_id": rec_id,
            "current_status": rec.get("status"),
            "retryable": False,
        },
    )


class _RollbackRequest(BaseModel):
    to_parameter_set_id: str | None = None


@profile_router.post(
    "/profile-recommendations/{rec_id}/rollback",
    status_code=501,
)
async def rollback_profile_rec(
    rec_id: str,
    body: _RollbackRequest,
    request: Request,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    token_actor = _extract_profile_token(request, action="rollback", rec_id=rec_id)
    _enforce_token_actor(principal=principal, token_actor=token_actor, action="rollback")

    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        _enforce_dual_operator(
            principal=principal, approver=rec.get("approved_by"), action="rollback",
        )
    # FS-001: 不得把只更新 recommendation 状态表述为真实回滚。
    # 当前没有 execution-owned reverse saga 或 runtime readback 契约；
    # 在这些安全边界完成前只能明确、无写入地 fail-closed。
    logger.warning(
        "profile rollback rejected fail-closed: rec=%s status=%s actor=%s explicit_target=%s",
        rec_id,
        rec.get("status"),
        token_actor,
        body.to_parameter_set_id is not None,
    )
    raise HTTPException(
        status_code=501,
        detail={
            "code": "profile_rollback_not_implemented",
            "message": (
                "Profile 参数回滚暂不可用：安全的反向 Saga 与运行时读回尚未实现。"
            ),
            "recommendation_id": rec_id,
            "requested_parameter_set_id": body.to_parameter_set_id,
            "current_status": rec.get("status"),
            "retryable": False,
        },
    )


# =============================================================================
# profile_type_review
# =============================================================================

@profile_router.get(
    "/profile-type-reviews",
    dependencies=[Depends(require_read_access)],
)
async def list_profile_type_reviews() -> dict[str, Any]:
    with _governance_session() as session:
        rows = session.execute(text("""
            SELECT recommendation_id, scope_ref, reason, status,
                   review_notes, created_at
            FROM governance.recommendations
            WHERE scope = 'profile' AND recommendation_type = 'profile_type_review'
            ORDER BY created_at DESC
            LIMIT 100
        """)).all()
    items = [
        {
            "recommendation_id": r.recommendation_id,
            "profile_id": r.scope_ref,
            "reason": r.reason,
            "status": r.status,
            "review_notes": (
                json.loads(r.review_notes) if isinstance(r.review_notes, str)
                else r.review_notes
            ),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"total": len(items), "reviews": items}


class _ResolveRequest(BaseModel):
    resolution: str  # 'accept_upgrade' / 'dismiss' / 'defer'
    notes: str | None = None


@profile_router.post("/profile-type-reviews/{rec_id}/resolve")
async def resolve_profile_type_review(
    rec_id: str,
    body: _ResolveRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    actor = (principal.identity or "").strip() or "operator"
    if body.resolution not in ("accept_upgrade", "dismiss", "defer"):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_resolution", "got": body.resolution},
        )
    with _governance_session() as session:
        rec = _load_profile_rec(session, rec_id)
        session.execute(text("""
            UPDATE governance.recommendations
            SET status = 'resolved',
                approved_by = :actor, approved_at = NOW(),
                review_notes = COALESCE(review_notes, '{}'::jsonb) ||
                    jsonb_build_object('resolution', :res, 'notes', :notes)
            WHERE recommendation_id = :rid
        """), {
            "actor": actor, "rid": rec_id,
            "res": body.resolution, "notes": body.notes or "",
        })
        session.execute(text("""
            DELETE FROM governance.profile_type_review_streak WHERE profile_id = :pid
        """), {"pid": rec["scope_ref"]})
        session.commit()
    return {"ok": True, "recommendation_id": rec_id, "resolution": body.resolution}


# =============================================================================
# Sleeve (observation-only, R2-14)
# =============================================================================

@profile_router.get(
    "/sleeve-advice/recent",
    dependencies=[Depends(require_read_access)],
)
async def list_sleeve_advice() -> dict[str, Any]:
    """只读;视图 vw_sleeve_advice_recent 由 batch_b_04 创建。"""
    with _governance_session() as session:
        rows = session.execute(text("""
            SELECT recommendation_id, scope_ref AS sleeve_name,
                   reason, status, review_notes, created_at
            FROM governance.vw_sleeve_advice_recent
            ORDER BY created_at DESC
            LIMIT 50
        """)).all()
    items = [
        {
            "recommendation_id": r.recommendation_id,
            "sleeve_name": r.sleeve_name,
            "reason": r.reason,
            "status": r.status,
            "review_notes": (
                json.loads(r.review_notes) if isinstance(r.review_notes, str)
                else r.review_notes
            ),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"total": len(items), "items": items}


class _MarkReviewedRequest(BaseModel):
    notes: str | None = None


@profile_router.post("/sleeve-advice/{rec_id}/mark-reviewed")
async def mark_sleeve_advice_reviewed(
    rec_id: str,
    body: _MarkReviewedRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    actor = (principal.identity or "").strip() or "operator"
    with _governance_session() as session:
        session.execute(text("""
            UPDATE governance.recommendations
            SET review_notes = COALESCE(review_notes, '{}'::jsonb) ||
                    jsonb_build_object('marked_reviewed_by', :actor,
                                       'marked_reviewed_at', NOW()::text,
                                       'marked_reviewed_notes', :notes)
            WHERE recommendation_id = :rid AND scope = 'sleeve'
        """), {"actor": actor, "rid": rec_id, "notes": body.notes or ""})
        session.commit()
    return {"ok": True, "recommendation_id": rec_id, "marked_reviewed_by": actor}


# R2-14: apply / release / approve 在 sleeve scope 下都是 403 observation-only。
def _sleeve_observation_only(action: str):
    async def _handler(rec_id: str) -> dict[str, Any]:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "observation_only",
                "scope": "sleeve",
                "action": action,
                "message": (
                    "sleeve-scope recommendations 仅供观察,不能直接 apply/"
                    "release/approve;通过 combo-level parameter 变更间接生效"
                ),
            },
        )
    return _handler


profile_router.add_api_route(
    "/sleeve-advice/{rec_id}/approve",
    _sleeve_observation_only("approve"),
    methods=["POST"],
    dependencies=[Depends(require_write_access)],
)
profile_router.add_api_route(
    "/sleeve-advice/{rec_id}/release",
    _sleeve_observation_only("release"),
    methods=["POST"],
    dependencies=[Depends(require_write_access)],
)
profile_router.add_api_route(
    "/sleeve-advice/{rec_id}/apply",
    _sleeve_observation_only("apply"),
    methods=["POST"],
    dependencies=[Depends(require_write_access)],
)


# =============================================================================
# Token extractor — 写动作共用
# =============================================================================

def _extract_profile_token(
    request: Request, *, action: str, rec_id: str,
) -> str:
    """从 header 取 token,要求 v2 格式(scope='profile', rec_id 绑定)。"""
    token = request.headers.get("X-Rdp-Apply-Token")
    if not token:
        raise HTTPException(
            status_code=403,
            detail={"code": "missing_apply_token", "action": action},
        )
    try:
        actor, _exp = verify_token(
            token, action,
            required_scope="profile",
            required_recommendation_id=rec_id,
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "invalid_apply_token",
                "reason": str(exc),
                "action": action,
            },
        ) from None
    return actor


__all__ = ["profile_router"]
