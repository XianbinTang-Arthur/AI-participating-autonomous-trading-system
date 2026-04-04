"""RDP API 路由.

为 Operator / UI 提供 RDP 子系统的只读查询 + 受控写入接口。

只读端点（GET，require_read_access）:
  GET /rdp/health                       — RDP 子系统健康状态
  GET /rdp/parameters/active            — 当前 active parameter sets
  GET /rdp/parameters/apply-history     — parameter apply/rollback 操作历史
  GET /rdp/attribution/latest           — 最近 attribution 结论
  GET /rdp/execution/latest             — 最近 execution realism 结论
  GET /rdp/decisions/latest             — 当前 family/timeframe 决策状态
  GET /rdp/recommendations/latest       — 最近 recommendations
  GET /rdp/recommendations/history      — recommendations 完整历史（含审批记录）
  GET /rdp/decision-round/latest        — 最近 decision round 完整结论
  GET /rdp/readiness                    — Promotion readiness 评估

写入端点（POST，require_write_access）:
  POST /rdp/recommendations/{id}/approve    — 审批 recommendation
  POST /rdp/recommendations/{id}/reject     — 拒绝 recommendation
  POST /rdp/recommendations/{id}/supersede  — 替代 recommendation
  POST /rdp/parameters/apply                — 应用已批准 recommendation 的参数
  POST /rdp/parameters/rollback             — 回滚 active parameter set
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from aats.api.auth import require_read_access, require_write_access
from aats.services.operator.rdp_queries import (
    query_active_parameter_sets,
    query_latest_attribution,
    query_latest_decision_round,
    query_latest_decisions,
    query_latest_execution_realism,
    query_latest_recommendations,
    query_promotion_readiness,
    query_rdp_health,
)

rdp_router = APIRouter(
    prefix="/rdp",
    tags=["RDP"],
)


def _project_root(request: Request) -> Path:
    """从 runtime settings 或 cwd 解析项目根目录."""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        # 尝试从 RDP settings 获取
        try:
            from aats.data_platform.config import get_settings as get_rdp_settings
            rdp_settings = get_rdp_settings()
            root = Path(rdp_settings.project_root).resolve()
            if root.exists():
                return root
        except Exception:
            pass
    # 默认 cwd
    return Path(".").resolve()


# ══════════════════════════════════════════════════════════════════
#  只读端点（Operator 观察面）
# ══════════════════════════════════════════════════════════════════


# ── Health ─────────────────────────────────────────────────────────


@rdp_router.get("/health", dependencies=[Depends(require_read_access)])
async def rdp_health(request: Request) -> dict[str, Any]:
    """RDP 子系统健康状态."""
    root = _project_root(request)
    return query_rdp_health(root)


# ── Active Parameters ──────────────────────────────────────────────


@rdp_router.get("/parameters/active", dependencies=[Depends(require_read_access)])
async def active_parameters(request: Request) -> dict[str, Any]:
    """当前 active parameter sets."""
    root = _project_root(request)
    return query_active_parameter_sets(root)


# ── Apply History ──────────────────────────────────────────────────


@rdp_router.get("/parameters/apply-history", dependencies=[Depends(require_read_access)])
async def parameter_apply_history(request: Request) -> dict[str, Any]:
    """Parameter apply/rollback 操作历史."""
    from aats.data_platform.decision_system.active_parameter_apply import (
        load_apply_history,
    )
    root = _project_root(request)
    history = load_apply_history(root)
    ops = history.get("operations", [])
    return {
        "total_operations": len(ops),
        "operations": list(reversed(ops)),  # 最新的在前
    }


# ── Attribution ────────────────────────────────────────────────────


@rdp_router.get("/attribution/latest", dependencies=[Depends(require_read_access)])
async def latest_attribution(request: Request) -> dict[str, Any]:
    """最近一次 attribution round 结论."""
    root = _project_root(request)
    return query_latest_attribution(root)


# ── Execution Realism ──────────────────────────────────────────────


@rdp_router.get("/execution/latest", dependencies=[Depends(require_read_access)])
async def latest_execution_realism(request: Request) -> dict[str, Any]:
    """最近一次 execution realism round 结论."""
    root = _project_root(request)
    return query_latest_execution_realism(root)


# ── Family/Timeframe Decisions ─────────────────────────────────────


@rdp_router.get("/decisions/latest", dependencies=[Depends(require_read_access)])
async def latest_decisions(request: Request) -> dict[str, Any]:
    """当前 family/timeframe 运营决策状态."""
    root = _project_root(request)
    return query_latest_decisions(root)


# ── Recommendations ────────────────────────────────────────────────


@rdp_router.get("/recommendations/latest", dependencies=[Depends(require_read_access)])
async def latest_recommendations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """最近的 recommendations."""
    root = _project_root(request)
    return query_latest_recommendations(
        root, limit=limit, status_filter=status,
    )


@rdp_router.get("/recommendations/history", dependencies=[Depends(require_read_access)])
async def recommendations_history(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Recommendations 完整历史（含审批元信息）."""
    root = _project_root(request)
    return query_latest_recommendations(root, limit=limit, status_filter=None)


# ── Decision Round ─────────────────────────────────────────────────


@rdp_router.get("/decision-round/latest", dependencies=[Depends(require_read_access)])
async def latest_decision_round(request: Request) -> dict[str, Any]:
    """最近一次 decision round 完整结论."""
    root = _project_root(request)
    return query_latest_decision_round(root)


# ── Promotion Readiness ────────────────────────────────────────────


@rdp_router.get("/readiness", dependencies=[Depends(require_read_access)])
async def promotion_readiness(request: Request) -> dict[str, Any]:
    """Promotion readiness 评估."""
    root = _project_root(request)
    return query_promotion_readiness(root)


# ══════════════════════════════════════════════════════════════════
#  写入端点（Operator 审批 + Apply/Rollback）
# ══════════════════════════════════════════════════════════════════


# ── 请求体模型 ─────────────────────────────────────────────────────


class ApprovalRequest(BaseModel):
    actor: str = Field(default="operator", description="操作人")
    notes: str | None = Field(default=None, description="审批备注")


class SupersedeRequest(BaseModel):
    actor: str = Field(default="operator", description="操作人")
    superseded_by_id: str | None = Field(
        default=None, description="替代此 recommendation 的新 recommendation_id",
    )
    notes: str | None = Field(default=None, description="备注")


class ApplyRequest(BaseModel):
    recommendation_id: str = Field(..., description="已批准的 recommendation_id")
    actor: str = Field(default="operator", description="操作人")
    notes: str | None = Field(default=None, description="操作备注")


class RollbackRequest(BaseModel):
    family: str = Field(..., description="策略家族: independent / directional")
    timeframe: str = Field(..., description="时间框架: 15m / 1h")
    to_parameter_set_id: str | None = Field(
        default=None,
        description="指定回滚目标（不指定则自动回滚到上一版）",
    )
    actor: str = Field(default="operator", description="操作人")
    notes: str | None = Field(default=None, description="操作备注")


# ── Recommendation Approve ─────────────────────────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/approve",
    dependencies=[Depends(require_write_access)],
)
async def approve_recommendation_api(
    request: Request,
    recommendation_id: str,
    body: ApprovalRequest,
) -> dict[str, Any]:
    """审批 recommendation（draft → approved）."""
    from aats.data_platform.decision_system.recommendation_registry import (
        approve_recommendation,
        load_recommendation_registry,
        save_recommendation_registry,
    )

    root = _project_root(request)
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"
    registry = load_recommendation_registry(reg_path)

    rec = approve_recommendation(
        registry, recommendation_id,
        approved_by=body.actor,
        notes=body.notes,
    )

    if rec is None:
        return {"ok": False, "message": f"未找到 recommendation: {recommendation_id}"}

    save_recommendation_registry(registry, reg_path)
    return {"ok": True, "recommendation": rec}


# ── Recommendation Reject ──────────────────────────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/reject",
    dependencies=[Depends(require_write_access)],
)
async def reject_recommendation_api(
    request: Request,
    recommendation_id: str,
    body: ApprovalRequest,
) -> dict[str, Any]:
    """拒绝 recommendation（draft → rejected）."""
    from aats.data_platform.decision_system.recommendation_registry import (
        load_recommendation_registry,
        reject_recommendation,
        save_recommendation_registry,
    )

    root = _project_root(request)
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"
    registry = load_recommendation_registry(reg_path)

    rec = reject_recommendation(
        registry, recommendation_id,
        rejected_by=body.actor,
        notes=body.notes,
    )

    if rec is None:
        return {"ok": False, "message": f"未找到 recommendation: {recommendation_id}"}

    save_recommendation_registry(registry, reg_path)
    return {"ok": True, "recommendation": rec}


# ── Recommendation Supersede ───────────────────────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/supersede",
    dependencies=[Depends(require_write_access)],
)
async def supersede_recommendation_api(
    request: Request,
    recommendation_id: str,
    body: SupersedeRequest,
) -> dict[str, Any]:
    """替代 recommendation（标记为 superseded）."""
    from aats.data_platform.decision_system.recommendation_registry import (
        load_recommendation_registry,
        save_recommendation_registry,
        supersede_recommendation,
    )

    root = _project_root(request)
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"
    registry = load_recommendation_registry(reg_path)

    rec = supersede_recommendation(
        registry, recommendation_id,
        superseded_by_id=body.superseded_by_id,
        actor=body.actor,
        notes=body.notes,
    )

    if rec is None:
        return {"ok": False, "message": f"未找到 recommendation: {recommendation_id}"}

    save_recommendation_registry(registry, reg_path)
    return {"ok": True, "recommendation": rec}


# ── Parameter Apply ────────────────────────────────────────────────


@rdp_router.post(
    "/parameters/apply",
    dependencies=[Depends(require_write_access)],
)
async def apply_parameter_api(
    request: Request,
    body: ApplyRequest,
) -> dict[str, Any]:
    """从已批准 recommendation 应用参数到 active parameter set."""
    from aats.data_platform.decision_system.active_parameter_apply import (
        apply_approved_recommendation,
    )

    root = _project_root(request)
    return apply_approved_recommendation(
        root,
        recommendation_id=body.recommendation_id,
        actor=body.actor,
        notes=body.notes,
    )


# ── Parameter Rollback ─────────────────────────────────────────────


@rdp_router.post(
    "/parameters/rollback",
    dependencies=[Depends(require_write_access)],
)
async def rollback_parameter_api(
    request: Request,
    body: RollbackRequest,
) -> dict[str, Any]:
    """回滚 active parameter set 到上一版本."""
    from aats.data_platform.decision_system.active_parameter_apply import (
        rollback_active_parameter_set,
    )

    root = _project_root(request)
    return rollback_active_parameter_set(
        root,
        family=body.family,
        timeframe=body.timeframe,
        to_parameter_set_id=body.to_parameter_set_id,
        actor=body.actor,
        notes=body.notes,
    )
