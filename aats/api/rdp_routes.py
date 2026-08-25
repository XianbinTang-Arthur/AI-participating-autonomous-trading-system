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
  POST /rdp/recommendations/{id}/approve              — 审批 recommendation
  POST /rdp/recommendations/{id}/reject               — 拒绝 recommendation
  POST /rdp/recommendations/{id}/supersede            — 替代 recommendation
  POST /rdp/recommendations/{id}/approve-and-release  — 审批 + gate + release + apply 原子链
  POST /rdp/parameters/apply                          — 应用已批准 recommendation 的参数
  POST /rdp/parameters/rollback                       — 回滚 active parameter set
  POST /rdp/tasks/trigger                             — 触发 RDP workflow 任务
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError

from aats.api._governance_db import governance_session as _governance_session
from aats.api.rdp_apply_token import (
    InvalidTokenError,
    emit_token,
    ttl_seconds,
    verify_token,
)
from aats.api.rdp_control_summary import (
    build_rdp_tuning_overview,
    build_rdp_tuning_proposals,
    build_rdp_workbench_alerts,
    build_rdp_workbench_item_detail,
    build_rdp_workbench_item_evidence,
    build_rdp_workbench_items,
    build_rdp_workbench_overview,
)
from aats.api.auth import OperatorPrincipal, require_read_access, require_write_access
from aats.api.rdp_v2 import rdp_v2_router
from aats.data_platform.governance.step2_integrity_guard import (
    step2_integrity_blocking_reason as _step2_integrity_blocking_reason,
)
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


def _resolve_actor(principal: OperatorPrincipal | None, body_actor: str | None) -> str:
    """把审计追踪的 actor 绑到 session principal 而不是 request body。

    L3 修复 + 防御深度加强：原本写入端点直接用 ``body.actor``（默认 "operator"
    字面量），任何 client 都能伪造 actor 字段，审计链形同虚设。现在优先用
    session identity（只有持有有效 cookie 的用户才能拿到）。

    M-A1-3 补强：只要 ``principal.auth_enabled`` 为真——哪怕 ``identity`` 为空
    或空白字符——都禁止回退到 ``body_actor``，避免将来某个 auth 路径（API key
    / JWT）忘记设置 identity 时，审计 actor 被 request body 静默劫持。这种情况
    下返回 ``f"unknown-{auth_source}"``，让运维能从 actor 字段看出"auth 开着但
    identity 没设"的代码路径 bug，并提醒尽快修复。

    只有 ``auth_enabled=False``（比如 local dev 的 operator_unsafe_write_without_auth）
    这一条路径允许从 body.actor 取值；两者都缺才使用 "operator" 占位。
    """
    if principal is not None and principal.auth_enabled:
        identity = (principal.identity or "").strip()
        if identity:
            return identity
        # auth 启用但 identity 缺失：记录告警并返回 sentinel，严禁泄到 body
        auth_source = getattr(principal, "auth_source", None) or "unknown"
        logger.warning(
            "operator principal auth_enabled=True but identity is empty; "
            "refusing to fall back to body_actor. auth_source=%s",
            auth_source,
        )
        return f"unknown-{auth_source}"
    if body_actor:
        return str(body_actor)
    return "operator"

logger = logging.getLogger(__name__)

rdp_router = APIRouter(
    prefix="/rdp",
    tags=["RDP"],
)
rdp_router.include_router(rdp_v2_router)


def _require_apply_token(required_action: str):
    """生成 FastAPI 依赖：要求请求携带合法的 ``X-Rdp-Apply-Token``。

    A-0.5 收口：apply/rollback/freeze 等写动作不再只靠 session cookie，
    额外要求一枚 HMAC-bound token（签发端 ``/rdp/operator-tokens``）。
    Token 编码了 ``actor``，路由会在业务逻辑里强制 token.actor 与
    session.identity 一致，防止跨 operator 重放。
    """

    def _dep(
        request: Request,
        x_rdp_apply_token: str | None = Header(
            default=None,
            alias="X-Rdp-Apply-Token",
            convert_underscores=False,
        ),
    ) -> str:
        if not x_rdp_apply_token:
            raise HTTPException(
                status_code=403,
                detail={"code": "missing_apply_token", "action": required_action},
            )
        try:
            actor, exp_ts = verify_token(x_rdp_apply_token, required_action)
        except InvalidTokenError as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "invalid_apply_token",
                    "reason": str(exc),
                    "action": required_action,
                },
            ) from None
        request.state.apply_token_actor = actor
        request.state.apply_token_exp_ts = exp_ts
        return actor

    return _dep


def _enforce_token_actor_matches_session(
    *,
    principal: OperatorPrincipal,
    token_actor: str,
    action: str,
) -> None:
    """Session identity 必须等于 token actor——否则返回 403 ``actor_mismatch``。

    仅当 ``auth_enabled=True`` 时强制；本地 dev（``operator_unsafe_write_without_auth``）
    走宽松模式，但 token 仍然需要校验签名/TTL/action（由上游依赖保证）。
    """
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


# Step2 integrity gate 现在由共享模块提供（aats.data_platform.governance
# .step2_integrity_guard），approve / supersede / tuning review 三条写入路径
# 全部走同一个函数，避免历史上本地拷贝漂移的风险。错误信息对用户固定、具体
# 异常只进日志，见该模块注释。


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
    workflow: str = Field(
        ...,
        description=(
            "workflow 名称: data_maintenance / governance_cycle / "
            "research_cycle / decision_cycle / release_cycle"
        ),
    )
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


class EmitTokenRequest(BaseModel):
    action: str = Field(
        ...,
        description="token 绑定的动作：apply / rollback / freeze",
    )


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


class ApproveAndReleaseRequest(BaseModel):
    """``/recommendations/{id}/approve-and-release`` 的请求载荷。

    合并 ``approve`` + ``/releases/create``（已自带 gate + apply）为单次 HTTP
    调用，目的是把 operator 的"审批 → 发布"链路从 2~3 个独立请求收敛到一个
    原子端点。字段按"审批"与"发布"两段划分：

    - ``approval_notes``：写进 recommendation.review_notes
    - ``release_notes`` / ``observation_window_hours``：透传给 release record
    - ``skip_gate`` / ``skip_apply``：与 ``/releases/create`` 同义，保留脚本
      场景的对等能力；UI 入口应保持默认（gate on、apply on）
    """

    actor: str = Field(default="operator", description="操作人")
    approval_notes: str | None = Field(
        default=None, description="审批备注（写进 recommendation.review_notes）",
    )
    release_notes: str | None = Field(
        default=None, description="发布备注（写进 release record.notes）",
    )
    observation_window_hours: int = Field(
        default=24, description="观察窗口时长（小时）",
    )
    skip_gate: bool = Field(default=False, description="跳过 pre-apply gate 检查")
    skip_apply: bool = Field(
        default=False, description="只审批 + 创建 release 但不 apply",
    )


class RunObservationRequest(BaseModel):
    release_id: str = Field(..., description="release_id")
    family: str | None = Field(default=None, description="如不指定，从 release 推断")
    timeframe: str | None = Field(default=None, description="如不指定，从 release 推断")
    window_hours: int = Field(default=24)


class EvaluateRollbackRequest(BaseModel):
    release_id: str = Field(..., description="release_id")
    family: str | None = Field(default=None)
    timeframe: str | None = Field(default=None)


# ── Recommendation 状态流转 shared helpers ─────────────────────────


def _precheck_recommendation_transitionable(
    root: Path,
    recommendation_id: str,
    *,
    expected_current: tuple[str, ...],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """approve / reject / supersede 的统一预检。

    - ``load_recommendation_registry`` 已经 DB-first、JSON fallback；
      对 UI / operator 来说真源是 DB，但 DB 不可达的单机 / 测试场景仍然
      能从 JSON 副本得到一致的 404/409 语义。
    - 命中目标 recommendation 且状态合法 → 返回 ``(registry, rec)`` 元组，
      供 handler 直接复用、不必二次 load（L1：避免每个 approve/reject/supersede
      都跑两遍 DB-first + JSON fallback）。
    - recommendation 不存在 → ``HTTPException(404)``。
    - 状态不在 ``expected_current`` 内 → ``HTTPException(409)``；响应体带上
      ``current_status`` / ``expected_status``，方便 UI / curl 客户端判断。

    注意：这是"快照"预检。真正的 CAS 仍然由底层 helper
    （``approve_recommendation`` 等）通过 ``expected_current_status`` 的
    UPDATE WHERE 子句完成；如果在预检和 transition 之间另一个 operator 抢先
    改写了状态，底层 helper 会返回 ``None``，handler 把那种情况映射成
    第二道 409（"被并发改写"）。
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        find_recommendation,
        load_recommendation_registry,
    )

    reg_path = root / "artifacts/decision_system/recommendation_registry.json"
    registry = load_recommendation_registry(reg_path)
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "ok": False,
                "message": f"未找到 recommendation: {recommendation_id}",
                "recommendation_id": recommendation_id,
            },
        )
    current_status = rec.get("status")
    if current_status not in expected_current:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "message": (
                    f"recommendation 状态为 {current_status!r}，"
                    f"期望: {list(expected_current)!r}"
                ),
                "recommendation_id": recommendation_id,
                "current_status": current_status,
                "expected_status": list(expected_current),
            },
        )
    return registry, rec


# ── Recommendation Approve ─────────────────────────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/approve",
)
async def approve_recommendation_api(
    request: Request,
    recommendation_id: str,
    body: ApprovalRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """审批 recommendation（draft → approved）.

    HTTP 语义:
      - 200 ok: 审批成功
      - 404: recommendation 不存在
      - 409: 状态不是 draft（已被别人审批 / 拒绝 / supersede），或 CAS 竞态
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        approve_recommendation,
        save_recommendation_registry,
    )

    root = _project_root(request)

    # Server-side integrity gate：Step2 快照不完整时拒绝审批，避免 UI-only
    # 禁用按钮被 curl / 脚本 / 重放请求绕过。reject 不受此门闸影响，运营者
    # 仍需要能清理掉 draft 里过期/脏的 recommendation。
    blocking_reason = _step2_integrity_blocking_reason(root)
    if blocking_reason is not None:
        return {
            "ok": False,
            "message": blocking_reason,
            "integrity_blocked": True,
        }

    registry, _snapshot_rec = _precheck_recommendation_transitionable(
        root, recommendation_id, expected_current=("draft",),
    )
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"

    rec = approve_recommendation(
        registry, recommendation_id,
        approved_by=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )

    if rec is None:
        # 预检已经过了，这里 None 只可能来自 CAS race：另一个 operator 在
        # 预检和 transition 之间抢先改写了状态。映射成 409，让客户端刷新 UI
        # 再决定下一步。
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "message": "recommendation 状态在审批过程中被并发改写，请刷新后重试",
                "recommendation_id": recommendation_id,
                "reason": "cas_race",
            },
        )

    save_recommendation_registry(registry, reg_path)
    return {"ok": True, "recommendation": rec}


# ── Recommendation Reject ──────────────────────────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/reject",
)
async def reject_recommendation_api(
    request: Request,
    recommendation_id: str,
    body: ApprovalRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """拒绝 recommendation（draft → rejected）.

    HTTP 语义:
      - 200 ok: 拒绝成功
      - 404: recommendation 不存在
      - 409: 状态不是 draft，或 CAS 竞态
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        reject_recommendation,
        save_recommendation_registry,
    )

    root = _project_root(request)

    registry, _snapshot_rec = _precheck_recommendation_transitionable(
        root, recommendation_id, expected_current=("draft",),
    )
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"

    rec = reject_recommendation(
        registry, recommendation_id,
        rejected_by=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )

    if rec is None:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "message": "recommendation 状态在拒绝过程中被并发改写，请刷新后重试",
                "recommendation_id": recommendation_id,
                "reason": "cas_race",
            },
        )

    save_recommendation_registry(registry, reg_path)
    return {"ok": True, "recommendation": rec}


# ── Recommendation Supersede ───────────────────────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/supersede",
)
async def supersede_recommendation_api(
    request: Request,
    recommendation_id: str,
    body: SupersedeRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """替代 recommendation（标记为 superseded）.

    HTTP 语义:
      - 200 ok: supersede 成功
      - 404: recommendation 不存在
      - 409: 状态不在 (draft, approved) 里（已 superseded / rejected 是终态），
        或 CAS 竞态
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        save_recommendation_registry,
        supersede_recommendation,
    )

    root = _project_root(request)

    # supersede 语义是 "用新 rec 替换 active rec"，新 rec 同样会推进执行链路，
    # 和 approve 的影响面对等，因此必须走同一个 Step2 integrity gate。历史上
    # 只有 approve 加了 gate，supersede 未加，curl 可绕过；这里补齐。
    blocking_reason = _step2_integrity_blocking_reason(root)
    if blocking_reason is not None:
        return {
            "ok": False,
            "message": blocking_reason,
            "integrity_blocked": True,
        }

    registry, _snapshot_rec = _precheck_recommendation_transitionable(
        root, recommendation_id, expected_current=("draft", "approved"),
    )
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"

    rec = supersede_recommendation(
        registry, recommendation_id,
        superseded_by_id=body.superseded_by_id,
        actor=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )

    if rec is None:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "message": "recommendation 状态在 supersede 过程中被并发改写，请刷新后重试",
                "recommendation_id": recommendation_id,
                "reason": "cas_race",
            },
        )

    save_recommendation_registry(registry, reg_path)
    return {"ok": True, "recommendation": rec}


# ── Recommendation Approve + Release 原子链 ───────────────────────


@rdp_router.post(
    "/recommendations/{recommendation_id}/approve-and-release",
)
async def approve_and_release_api(
    request: Request,
    recommendation_id: str,
    body: ApproveAndReleaseRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """审批 + gate + release + apply 一条龙（draft → approved → active）。

    合并历史链路：``/recommendations/{id}/approve`` → ``/releases/create``
    （后者自带 gate + apply）= 2 个独立 HTTP 请求。把这两步折叠成一次调用，
    让 operator UI "审批并发布" 按钮能原子地走完整个治理链。

    语义和 token 策略上，本端点对齐 ``/releases/create`` 而非 ``/parameters/apply``：
    同样走 ``require_write_access`` + Step2 integrity gate，但不额外要求
    ``X-Rdp-Apply-Token``。原因：``/releases/create`` 本身就是"gate + release +
    apply"的官方组合入口且未要求 token；如果给这个语义相同的端点再加一道锁，
    operator 就会被迫走两条政策不一致的路径，反而更乱。若未来把 HMAC token
    推广到所有写动作，``/releases/create`` 与本端点应同步硬化，保持对等。

    失败恢复：
      - 审批失败（404 / CAS race）：recommendation 未变，release 未创建，回 HTTP 错误
      - Step2 integrity 阻断：什么都没做，回 200 ``integrity_blocked=True``
      - Gate 阻断：recommendation 已 approved 不回滚；release record 写入并标记
        ``apply_result=blocked_by_gate``；返回 200 ``ok=False`` + release 详情
      - Apply 失败：同 gate 阻断，release record 标记 ``apply_result=failed``
      - 即"只要 approve 成功，recommendation 就是 approved 的"；下次可单独重试
        ``/parameters/apply`` 或 ``/releases/create``。和现有链路保持一致。

    返回体契约（UI 状态恢复依赖这个）：
      **只要本端点返回 200**，``recommendation`` 字段就是 approve 之后的
      权威状态；前端可以直接用它更新本地缓存而无需二次 ``/rdp/control-summary``
      轮询。网络抖动时 operator 重试可能打到 409 CAS race，那时 ``detail``
      带 ``current_recommendation`` 字段，同样是权威的当前状态。

    HTTP 语义:
      - 200 ok=True：全链路成功（或 ``skip_apply=True`` 的"仅审批 + 创建 release"）
      - 200 ok=False, integrity_blocked=True：Step2 降级，整链拒绝
      - 200 ok=False + release.apply_result=blocked_by_gate：gate 阻断
      - 200 ok=False + release.apply_result=failed：apply 失败
      - 404：recommendation 不存在
      - 409：状态不是 draft，或 CAS 竞态
    """
    from aats.data_platform.decision_system.recommendation_registry import (
        approve_recommendation,
        find_recommendation,
        load_recommendation_registry,
        save_recommendation_registry,
    )
    from aats.data_platform.production_workflow.release_registry import (
        create_parameter_release,
    )

    root = _project_root(request)
    actor = _resolve_actor(principal, body.actor)

    # Step2 integrity gate：approve / release / apply 三条路径各自都有这个
    # 门闸；合并路径只做一次，保证"在任何时刻 Step2 降级都在源头拒绝"，
    # 不会先审批再在 release 阶段失败留下 orphan 的 approved recommendation。
    blocking_reason = _step2_integrity_blocking_reason(root)
    if blocking_reason is not None:
        return {
            "ok": False,
            "message": blocking_reason,
            "integrity_blocked": True,
        }

    # 预检 recommendation 状态；draft → approved 的真 CAS 由底层 helper 保证。
    registry, _snapshot_rec = _precheck_recommendation_transitionable(
        root, recommendation_id, expected_current=("draft",),
    )
    reg_path = root / "artifacts/decision_system/recommendation_registry.json"

    # ── 1. 审批 ─────────────────────────────────────────────────
    approved_rec = approve_recommendation(
        registry, recommendation_id,
        approved_by=actor,
        notes=body.approval_notes,
    )
    if approved_rec is None:
        # 预检过了但 approve 返回 None = CAS race（见 approve helper）。这里把
        # race 映射成 409，让 UI 能刷新后重试。recommendation 本身没变——
        # 重新 load registry 取当前权威状态塞到 detail 里，前端就不必为了
        # "到底成了没" 再多打一次 /control-summary。
        try:
            current_registry = load_recommendation_registry(reg_path)
            current_rec = find_recommendation(current_registry, recommendation_id)
        except Exception:
            # 二次 load 失败不影响 race 的主响应语义，仅放弃附加信息。
            current_rec = None
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "message": "recommendation 状态在审批过程中被并发改写，请刷新后重试",
                "recommendation_id": recommendation_id,
                "reason": "cas_race",
                "current_recommendation": current_rec,
            },
        )
    save_recommendation_registry(registry, reg_path)

    # ── 2+3+4. Gate + release + apply ──────────────────────────
    # approve 已经把 DB/JSON 都推到 approved；create_parameter_release 会再次
    # load_recommendation_registry 命中同一条记录。
    release_result = create_parameter_release(
        root,
        recommendation_id=recommendation_id,
        actor=actor,
        observation_window_hours=body.observation_window_hours,
        notes=body.release_notes,
        run_gate=not body.skip_gate,
        run_apply=not body.skip_apply,
    )

    return {
        "ok": bool(release_result.get("ok")),
        "recommendation": approved_rec,
        "release": release_result.get("release"),
        "gate_result": release_result.get("gate_result"),
        "apply_result": release_result.get("apply_result"),
        "message": release_result.get("message") or "审批并发布完成",
    }


# ── Operator Token 签发 ───────────────────────────────────────────


@rdp_router.post("/operator-tokens")
async def emit_operator_token_api(
    body: EmitTokenRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """签发一次性 apply/rollback/freeze 动作 token（TTL-bounded HMAC）。

    A-0.5：废弃旧的生产写闸 env flag，改以 session-bound HMAC token 作为写动作
    的第二把锁。``principal`` 身份写进 token 载荷，消费端强制要求
    ``session.identity == token.actor``。
    """
    if body.action not in {"apply", "rollback", "freeze"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_action",
                "action": body.action,
                "allowed": ["apply", "rollback", "freeze"],
            },
        )
    actor = (principal.identity or "").strip() or "operator"
    token = emit_token(actor=actor, action=body.action)
    return {
        "token": token,
        "ttl_seconds": ttl_seconds(),
        "action": body.action,
        "actor": actor,
    }


# ── Parameter Apply ────────────────────────────────────────────────


@rdp_router.post(
    "/parameters/apply",
)
async def apply_parameter_api(
    request: Request,
    body: ApplyRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
    token_actor: str = Depends(_require_apply_token("apply")),
) -> dict[str, Any]:
    """从已批准 recommendation 应用参数到 active parameter set."""
    from aats.data_platform.decision_system.active_parameter_apply import (
        apply_approved_recommendation,
    )

    _enforce_token_actor_matches_session(
        principal=principal, token_actor=token_actor, action="apply"
    )

    root = _project_root(request)

    # Server-side integrity gate：与 approve / supersede 对等。approve 时已
    # 有门闸，但 approve → apply 之间 Step2 snapshot 仍可能 degrade（比如
    # 研究数据落库失败），UI 会同步禁用按钮但 curl 可直接打过来。apply 动作
    # 会把批准的 parameter set 推到 active_parameter_sets，影响面比
    # approve 本身大，必须再次强制 server-side 校验。
    blocking_reason = _step2_integrity_blocking_reason(root)
    if blocking_reason is not None:
        return {
            "ok": False,
            "message": blocking_reason,
            "integrity_blocked": True,
        }

    return apply_approved_recommendation(
        root,
        recommendation_id=body.recommendation_id,
        actor=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )


# ── Parameter Rollback ─────────────────────────────────────────────


@rdp_router.post(
    "/parameters/rollback",
)
async def rollback_parameter_api(
    request: Request,
    body: RollbackRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
    token_actor: str = Depends(_require_apply_token("rollback")),
) -> dict[str, Any]:
    """回滚 active parameter set 到上一版本.

    A-0.1 收口后，``rollback_active_parameter_set`` 返回的 ``code`` 被映射到
    HTTP 状态码：

    - ``VALIDATION_FAILED`` / ``NO_PREVIOUS_TARGET`` / ``NO_ACTIVE_SET`` /
      ``ENVIRONMENT_BLOCKED`` → 422（客户端提供的回滚请求不合法）
    - 正常成功 → 200 + ok=True 载荷

    A-0.5 收口：必须携带 ``X-Rdp-Apply-Token: <rollback-token>``，且 token 中
    编码的 actor 必须与 session identity 一致（``auth_enabled=True`` 时）。
    """
    from aats.data_platform.decision_system.active_parameter_apply import (
        rollback_active_parameter_set,
    )

    _enforce_token_actor_matches_session(
        principal=principal, token_actor=token_actor, action="rollback"
    )

    root = _project_root(request)
    result = rollback_active_parameter_set(
        root,
        family=body.family,
        timeframe=body.timeframe,
        to_parameter_set_id=body.to_parameter_set_id,
        actor=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )
    if not result.get("ok") and result.get("code") in {
        "VALIDATION_FAILED",
        "NO_PREVIOUS_TARGET",
        "NO_ACTIVE_SET",
        "ENVIRONMENT_BLOCKED",
    }:
        raise HTTPException(status_code=422, detail=result)
    return result


# ══════════════════════════════════════════════════════════════════
#  Production Workflow 端点
# ══════════════════════════════════════════════════════════════════


# ── Gate ───────────────────────────────────────────────────────────


@rdp_router.post(
    "/gates/run",
    dependencies=[Depends(require_write_access)],
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
)
async def create_release_api(
    request: Request,
    body: CreateReleaseRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """创建 parameter release（gate + apply 完整流程）."""
    from aats.data_platform.production_workflow.release_registry import (
        create_parameter_release,
    )
    root = _project_root(request)

    # Server-side integrity gate：/releases/create 触发 gate + apply 全链路，
    # 比单独 /parameters/apply 影响更大（会生成 parameter_release 行并进入
    # observation 阶段）。approve / apply 已各自有门闸，这里补齐入口层。
    # 与 /parameters/rollback 区别：rollback 是安全操作，Step2 即便挂也必须
    # 让运营能用；release/create 是前向动作，任何 Step2 降级都应先阻断。
    blocking_reason = _step2_integrity_blocking_reason(root)
    if blocking_reason is not None:
        return {
            "ok": False,
            "message": blocking_reason,
            "integrity_blocked": True,
        }

    return create_parameter_release(
        root,
        recommendation_id=body.recommendation_id,
        actor=_resolve_actor(principal, body.actor),
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
    dependencies=[Depends(require_write_access)],
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
        if release is None:
            # release_id 查不到时要让 operator 明确知道是 id 错了，而不是
            # 回一条"无法确定 family/timeframe"把锅甩给 body 参数。
            return {
                "ok": False,
                "message": f"release 未找到: {body.release_id}",
                "reason": "release_not_found",
                "release_id": body.release_id,
            }
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
    dependencies=[Depends(require_write_access)],
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
        if release is None:
            return {
                "ok": False,
                "message": f"release 未找到: {body.release_id}",
                "reason": "release_not_found",
                "release_id": body.release_id,
            }
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
)
async def trigger_task_api(
    request: Request,
    body: TriggerTaskRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """触发 RDP workflow 任务（写入 pending 到任务队列）.

    返回 schema 统一：所有分支都回 ``{ok, task_id, workflow, existing_task, message}``
    五字段（缺省填 None）。``retryable`` 在 DB 故障分支额外附加，前端拿来决定
    是否给 operator "稍后重试" 提示。
    """
    from aats.data_platform.governance.rdp_task_db import (
        VALID_WORKFLOWS,
        WorkflowEnqueueBlockedError,
        db_create_task_if_idle,
    )
    from aats.data_platform.operations.workflow_dispatcher import (
        describe_manual_trigger_availability,
    )

    def _response(
        *,
        ok: bool,
        task_id: str | None = None,
        existing_task: dict[str, Any] | None = None,
        message: str | None = None,
        **extras: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": ok,
            "task_id": task_id,
            "workflow": body.workflow,
            "existing_task": existing_task,
            "message": message,
        }
        payload.update(extras)
        return payload

    if body.workflow not in VALID_WORKFLOWS:
        return _response(
            ok=False,
            message=(
                f"未知的 workflow: {body.workflow}，"
                f"可选: {', '.join(sorted(VALID_WORKFLOWS))}"
            ),
        )

    availability = describe_manual_trigger_availability(
        _project_root(request),
        body.workflow,
    )
    if not availability.get("enabled"):
        return _response(
            ok=False,
            message=str(availability.get("disabled_reason") or "当前 workflow 不能手动触发。"),
            blocked_by_config=True,
        )

    try:
        # 原子创建：has_active_task → create_task 旧路径的 TOCTOU 已由
        # db_create_task_if_idle 的 "INSERT ... ON CONFLICT DO NOTHING"
        # 一条 SQL 吸收，并发 operator 触发不会再打印 IntegrityError。
        with _governance_session() as session:
            task_id, existing = db_create_task_if_idle(
                session,
                workflow=body.workflow,
                requested_by=_resolve_actor(principal, body.actor),
            )
    except WorkflowEnqueueBlockedError:
        return _response(
            ok=False,
            message=(
                f"{body.workflow} 当前处于 golden-path freeze，"
                "不能通过任务队列手动触发。"
            ),
            blocked_by_freeze=True,
        )
    except OperationalError:
        # DB 连接层失败（governance DB 不可达、连接池耗尽等）——operator 需要
        # 知道这是"后端 DB 不通"而不是"业务冲突"，好决定联系运维还是等待。
        # 栈信息仍进日志，用户侧固定文案避免泄漏 SQL 片段。
        logger.exception(
            "trigger_task_api: governance DB unavailable while creating task workflow=%s",
            body.workflow,
        )
        return _response(
            ok=False,
            message="governance 数据库暂时不可达，请稍后重试或联系运维。",
            retryable=True,
        )
    except Exception:
        # H2 风格修复：task 创建异常属于内部错误（事务异常 / 未知错误等），
        # message 直接回显给 caller 会把 SQL 片段、schema 名泄漏出去。固定
        # 文案 + logger.exception 把堆栈留日志（带 workflow 上下文便于排查）。
        logger.exception(
            "trigger_task_api: failed to create task for workflow=%s",
            body.workflow,
        )
        return _response(ok=False, message="创建任务失败，请查看服务端日志。")

    if task_id is None:
        return _response(
            ok=False,
            existing_task=existing,
            message=(
                f"{body.workflow} 已有 {existing['status']} 任务"
                f"（{existing['task_id']}），请等待完成后再触发。"
            ) if existing else f"{body.workflow} 已有活跃任务，请稍后再试。",
        )

    return _response(ok=True, task_id=task_id)


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
    except Exception:
        # 同 H2：不回显 str(exc)，避免 DSN / 表名 / SQL 碎片泄漏。
        logger.exception("Failed to query task status")
        return {
            "ok": False,
            "tasks": [],
            "message": "查询任务状态失败，请查看服务端日志。",
        }

    return {"ok": True, "tasks": tasks}


# ── RDP Control Summary（前端卡片聚合数据）──────────────────────────


@rdp_router.get("/control-summary", dependencies=[Depends(require_read_access)])
async def control_summary_api(request: Request) -> dict[str, Any]:
    """聚合 RDP 控制卡片需要的数据。"""
    from aats.api.rdp_control_summary import build_rdp_control_summary

    return build_rdp_control_summary(request)


@rdp_router.get("/workbench/overview", dependencies=[Depends(require_read_access)])
async def workbench_overview_api(request: Request) -> dict[str, Any]:
    """RDP 工作台首页摘要。"""
    return build_rdp_workbench_overview(request)


@rdp_router.get("/workbench/items", dependencies=[Depends(require_read_access)])
async def workbench_items_api(request: Request) -> dict[str, Any]:
    """RDP 当前待处理事项。"""
    return build_rdp_workbench_items(request)


@rdp_router.get("/workbench/items/{combo_key}", dependencies=[Depends(require_read_access)])
async def workbench_item_detail_api(request: Request, combo_key: str) -> dict[str, Any]:
    """RDP 单个 combo 的当前处理详情。

    状态：a5218fb 预留的 detail drawer 接口，当前前端 store.js 未注册消费者，
    仅可通过 curl 手动调试。前端接入 drawer 时把 ``/rdp/workbench/items/{combo_key}``
    加入 viewSpecs 即可复用 payload-building 逻辑。集成测试
    ``test_workbench_detail_routes_expose_evidence_and_integrity_block`` 仍对
    integrity gate 做回归保护。
    """
    return build_rdp_workbench_item_detail(request, combo_key)


@rdp_router.get("/workbench/evidence/{combo_key}", dependencies=[Depends(require_read_access)])
async def workbench_item_evidence_api(request: Request, combo_key: str) -> dict[str, Any]:
    """RDP 单个 combo 的证据钻取摘要。

    状态：与 ``/workbench/items/{combo_key}`` 同为 a5218fb 预留接口，前端尚未
    接入。保留原因是 integrity gate 的回归测试依赖它，且前端接入成本低。
    """
    return build_rdp_workbench_item_evidence(request, combo_key)


@rdp_router.get("/workbench/alerts", dependencies=[Depends(require_read_access)])
async def workbench_alerts_api(request: Request) -> dict[str, Any]:
    """RDP 数据完整性与系统阻断摘要。"""
    return build_rdp_workbench_alerts(request)


@rdp_router.get("/tuning/overview", dependencies=[Depends(require_read_access)])
async def tuning_overview_api(request: Request) -> dict[str, Any]:
    """自动调优摘要。"""
    return build_rdp_tuning_overview(request)


@rdp_router.get("/tuning/proposals", dependencies=[Depends(require_read_access)])
async def tuning_proposals_api(request: Request) -> dict[str, Any]:
    """待审核自动调优提案。"""
    return build_rdp_tuning_proposals(request)


@rdp_router.post(
    "/tuning/proposals/{proposal_id}/approve",
)
async def approve_tuning_proposal_api(
    request: Request,
    proposal_id: str,
    body: ApprovalRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """批准自动调优提案。"""
    from aats.data_platform.operations.strategy_tuning_registry import (
        review_strategy_tuning_proposal,
    )

    root = _project_root(request)
    return review_strategy_tuning_proposal(
        root,
        proposal_id=proposal_id,
        action="approve",
        reviewer=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )


@rdp_router.post(
    "/tuning/proposals/{proposal_id}/reject",
)
async def reject_tuning_proposal_api(
    request: Request,
    proposal_id: str,
    body: ApprovalRequest,
    principal: OperatorPrincipal = Depends(require_write_access),
) -> dict[str, Any]:
    """拒绝自动调优提案。"""
    from aats.data_platform.operations.strategy_tuning_registry import (
        review_strategy_tuning_proposal,
    )

    root = _project_root(request)
    return review_strategy_tuning_proposal(
        root,
        proposal_id=proposal_id,
        action="reject",
        reviewer=_resolve_actor(principal, body.actor),
        notes=body.notes,
    )
