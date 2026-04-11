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
  GET /rdp/tasks/status                 — 最近任务状态
  GET /rdp/control-summary              — RDP 控制卡片聚合数据

写入端点（POST，require_write_access）:
  POST /rdp/recommendations/{id}/approve    — 审批 recommendation
  POST /rdp/recommendations/{id}/reject     — 拒绝 recommendation
  POST /rdp/recommendations/{id}/supersede  — 替代 recommendation
  POST /rdp/parameters/apply                — 应用已批准 recommendation 的参数
  POST /rdp/parameters/rollback             — 回滚 active parameter set
  POST /rdp/tasks/trigger                   — 触发 RDP workflow 任务
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
        except Exception as exc:
            logger.warning("Failed to resolve project root from RDP settings: %s", exc)
    # 默认 cwd
    return Path(".").resolve()


def _governance_db_url() -> str | None:
    """获取 governance schema 所在数据库的连接串.

    优先 AATS_ACTIVE_PARAMETER_DB_URL，其次 RDP_DATABASE_URL。
    """
    url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if url:
        return url
    try:
        from aats.data_platform.config import get_settings as get_rdp_settings
        return get_rdp_settings().database_url
    except Exception:
        return None


_governance_engine_cache: dict[str, Any] = {}   # url → Engine singleton


def _get_governance_engine(url: str) -> Any:
    """返回 URL 对应的缓存 Engine，避免每次请求重建连接池."""
    from sqlalchemy import create_engine

    cached = _governance_engine_cache.get(url)
    if cached is not None:
        return cached
    engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=1)
    _governance_engine_cache[url] = engine
    return engine


@contextlib.contextmanager
def _governance_session() -> Iterator[Any]:
    """创建一个连接 governance schema 的轻量 session.

    gateway 容器通过 AATS_ACTIVE_PARAMETER_DB_URL 连接 aats_research，
    本地开发通过 RDP_DATABASE_URL (.env.research) 连接。
    Engine 按 URL 缓存，避免每次请求创建/销毁连接池。
    """
    url = _governance_db_url()
    if not url:
        raise RuntimeError("No governance DB URL available "
                           "(AATS_ACTIVE_PARAMETER_DB_URL / RDP_DATABASE_URL)")
    from sqlalchemy.orm import Session, sessionmaker

    engine = _get_governance_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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


class TriggerTaskRequest(BaseModel):
    workflow: str = Field(..., description="workflow 名称: data_maintenance / research_cycle")
    actor: str = Field(default="operator", description="操作人")


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


class CreateReleaseRequest(BaseModel):
    recommendation_id: str = Field(..., description="已批准的 recommendation_id")
    actor: str = Field(default="operator", description="操作人")
    observation_window_hours: int = Field(default=24, description="观察窗口时长（小时）")
    notes: str | None = Field(default=None, description="操作备注")
    skip_gate: bool = Field(default=False, description="跳过 gate 检查")
    skip_apply: bool = Field(default=False, description="只创建 release 不 apply")


class RunObservationRequest(BaseModel):
    release_id: str = Field(..., description="release_id")
    family: str | None = Field(default=None, description="如不指定，从 release 推断")
    timeframe: str | None = Field(default=None, description="如不指定，从 release 推断")
    window_hours: int = Field(default=24)


class EvaluateRollbackRequest(BaseModel):
    release_id: str = Field(..., description="release_id")
    family: str | None = Field(default=None)
    timeframe: str | None = Field(default=None)


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


# ══════════════════════════════════════════════════════════════════
#  Production Workflow 端点
# ══════════════════════════════════════════════════════════════════


# ── Gate ───────────────────────────────────────────────────────────


@rdp_router.post(
    "/gates/run",
    dependencies=[Depends(require_read_access)],
)
async def run_gate_api(
    request: Request,
    body: ApplyRequest,
) -> dict[str, Any]:
    """运行 pre-apply gate 检查."""
    from aats.data_platform.production_workflow.pre_apply_gate import (
        run_pre_apply_gate,
    )
    root = _project_root(request)
    return run_pre_apply_gate(root, body.recommendation_id)


# ── Releases ───────────────────────────────────────────────────────


@rdp_router.post(
    "/releases/create",
    dependencies=[Depends(require_write_access)],
)
async def create_release_api(
    request: Request,
    body: CreateReleaseRequest,
) -> dict[str, Any]:
    """创建 parameter release（gate + apply 完整流程）."""
    from aats.data_platform.production_workflow.release_registry import (
        create_parameter_release,
    )
    root = _project_root(request)
    return create_parameter_release(
        root,
        recommendation_id=body.recommendation_id,
        actor=body.actor,
        observation_window_hours=body.observation_window_hours,
        notes=body.notes,
        run_gate=not body.skip_gate,
        run_apply=not body.skip_apply,
    )


@rdp_router.get(
    "/releases/latest",
    dependencies=[Depends(require_read_access)],
)
async def latest_releases(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """最近的 parameter releases."""
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )
    root = _project_root(request)
    history = load_release_history(root)
    releases = list(reversed(history.get("releases", [])))
    return {
        "total": len(releases),
        "releases": releases[:limit],
    }


@rdp_router.get(
    "/releases/history",
    dependencies=[Depends(require_read_access)],
)
async def releases_history(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """完整 release 历史."""
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )
    root = _project_root(request)
    history = load_release_history(root)
    releases = list(reversed(history.get("releases", [])))
    return {
        "total": len(releases),
        "releases": releases[:limit],
    }


# ── Observation ────────────────────────────────────────────────────


@rdp_router.post(
    "/observations/run",
    dependencies=[Depends(require_read_access)],
)
async def run_observation_api(
    request: Request,
    body: RunObservationRequest,
) -> dict[str, Any]:
    """运行 post-apply observation 检查."""
    from aats.data_platform.production_workflow.observation_window import (
        run_observation,
    )
    from aats.data_platform.production_workflow.release_registry import (
        find_release,
        load_release_history,
    )

    root = _project_root(request)
    family = body.family
    timeframe = body.timeframe

    if not family or not timeframe:
        history = load_release_history(root)
        release = find_release(history, body.release_id)
        if release:
            family = family or release.get("family")
            timeframe = timeframe or release.get("timeframe")

    if not family or not timeframe:
        return {"ok": False, "message": "无法确定 family/timeframe"}

    return run_observation(
        root,
        release_id=body.release_id,
        family=family,
        timeframe=timeframe,
        window_hours=body.window_hours,
    )


# ── Rollback Recommendation ───────────────────────────────────────


@rdp_router.post(
    "/rollback-recommendation/evaluate",
    dependencies=[Depends(require_read_access)],
)
async def evaluate_rollback_api(
    request: Request,
    body: EvaluateRollbackRequest,
) -> dict[str, Any]:
    """评估是否建议 rollback."""
    from aats.data_platform.production_workflow.release_registry import (
        find_release,
        load_release_history,
    )
    from aats.data_platform.production_workflow.rollback_policy import (
        evaluate_rollback_recommendation,
    )

    root = _project_root(request)
    family = body.family
    timeframe = body.timeframe

    if not family or not timeframe:
        history = load_release_history(root)
        release = find_release(history, body.release_id)
        if release:
            family = family or release.get("family")
            timeframe = timeframe or release.get("timeframe")

    if not family or not timeframe:
        return {"ok": False, "message": "无法确定 family/timeframe"}

    return evaluate_rollback_recommendation(
        root,
        release_id=body.release_id,
        family=family,
        timeframe=timeframe,
    )


# ══════════════════════════════════════════════════════════════════
#  RDP Task Queue 端点（UI 触发 workflow + 状态查询）
# ═══════════════════════════════════════════��══════════════════════


@rdp_router.post(
    "/tasks/trigger",
    dependencies=[Depends(require_write_access)],
)
async def trigger_task_api(
    request: Request,
    body: TriggerTaskRequest,
) -> dict[str, Any]:
    """触发 RDP workflow 任务（写入 pending 到任务队列）."""
    from aats.data_platform.governance.rdp_task_db import (
        VALID_WORKFLOWS,
        db_create_task,
        db_has_active_task,
    )

    if body.workflow not in VALID_WORKFLOWS:
        return {
            "ok": False,
            "message": f"未知的 workflow: {body.workflow}，"
                       f"可选: {', '.join(sorted(VALID_WORKFLOWS))}",
        }

    try:
        with _governance_session() as session:
            active = db_has_active_task(session, body.workflow)
            if active:
                return {
                    "ok": False,
                    "message": f"{body.workflow} 已有 {active['status']} 任务"
                               f"（{active['task_id']}），请等待完成后再触发。",
                    "existing_task": active,
                }
            task_id = db_create_task(
                session,
                workflow=body.workflow,
                requested_by=body.actor,
            )
    except Exception as exc:
        logger.exception("Failed to create task: %s", exc)
        return {"ok": False, "message": f"创建任务失败: {exc}"}

    return {"ok": True, "task_id": task_id, "workflow": body.workflow}


@rdp_router.get("/tasks/status", dependencies=[Depends(require_read_access)])
async def task_status_api(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """查询最近 RDP 任务状态."""
    from aats.data_platform.governance.rdp_task_db import db_get_recent_tasks

    try:
        with _governance_session() as session:
            tasks = db_get_recent_tasks(session, limit=limit)
    except Exception as exc:
        logger.warning("Failed to query task status: %s", exc)
        return {"ok": False, "tasks": [], "message": str(exc)}

    return {"ok": True, "tasks": tasks}


# ── RDP Control Summary（前端卡片聚合数据）──────────────────────────


@rdp_router.get("/control-summary", dependencies=[Depends(require_read_access)])
async def control_summary_api(request: Request) -> dict[str, Any]:
    """聚合 RDP 控制卡片需要的数据: 任务状态 + 待审批 + active 参数."""
    return _rdp_control_summary(request)


def _rdp_control_summary(request: Request) -> dict[str, Any]:
    """内部实现，同时供 dashboard bundle handler 调用."""
    root = _project_root(request)

    # 1) 最近任务（按 workflow 分组，取每类最新一条）
    tasks_by_workflow: dict[str, Any] = {}
    tasks_error: str | None = None
    try:
        from aats.data_platform.governance.rdp_task_db import db_get_recent_tasks

        with _governance_session() as session:
            recent = db_get_recent_tasks(session, limit=20)
        for t in recent:
            wf = t["workflow"]
            if wf not in tasks_by_workflow:
                tasks_by_workflow[wf] = t
    except Exception as exc:
        logger.warning("control-summary: task query failed: %s", exc)
        tasks_error = str(exc)

    # 2) 待处理 recommendations（draft 待审批 + approved 待应用）
    pending_recommendations: list[dict[str, Any]] = []
    try:
        for status_filter in ("draft", "approved"):
            recs_data = query_latest_recommendations(
                root, limit=50, status_filter=status_filter,
            )
            for rec in recs_data.get("recommendations", []):
                pending_recommendations.append({
                    "recommendation_id": rec.get("recommendation_id"),
                    "family": rec.get("family"),
                    "timeframe": rec.get("timeframe"),
                    "action": rec.get("action"),
                    "status": rec.get("status", status_filter),
                    "target_parameter_set_id": rec.get("target_parameter_set_id"),
                    "source_round_id": rec.get("source_round_id"),
                    "created_at": rec.get("created_at"),
                })
    except Exception as exc:
        logger.warning("control-summary: recommendations query failed: %s", exc)

    # 3) 当前 active 参数
    active_parameters: dict[str, Any] = {}
    try:
        from aats.data_platform.governance.active_params_db import (
            db_load_active_registry,
        )

        with _governance_session() as session:
            registry = db_load_active_registry(session)
        active_parameters = registry.get("active_sets", {})
    except Exception as exc:
        logger.warning("control-summary: active params query failed: %s", exc)
        # fallback 到文件
        try:
            params_data = query_active_parameter_sets(root)
            active_parameters = params_data.get("active_sets", {})
        except Exception:
            pass

    return {
        "tasks": tasks_by_workflow,
        "tasks_error": tasks_error,
        "pending_recommendations": pending_recommendations,
        "active_parameters": active_parameters,
    }
