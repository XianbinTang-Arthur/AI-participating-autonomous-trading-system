"""Profile-level recommendation 跨库 apply saga。

背景(R1-20 + R2-01 + R2-08):
  apply 一次必须同时写两个库:
    - research DB (governance.active_parameter_sets / parameter_apply_history)
    - live DB (strategy_profile_activation / strategy_profile_activation_history)
  否则实盘不会吃到新 threshold,RDP 又空转一轮。

Saga 设计:
  Step 1 [research] UPSERT active_parameter_sets(scope='profile', scope_ref=profile_id)
  Step 2 [research] INSERT parameter_apply_history (operation_id 幂等键)
  Step 3 [live]     FOR UPDATE strategy_profile_activation → baseline 校验 →
                    jsonb_set 白名单 key → commit
  Step 4 [live]     INSERT strategy_profile_activation_history (审计事件)

幂等性(R2-08):
  - operation_id = UUID4,在 find_or_create_saga_operation 里生成并存 DB
  - 重试调用 apply_profile_saga 时,已完成的 step 会跳过
  - Step 3 失败后重放,Step 1/2 幂等;Step 4 独立完成

安全性(R2-01):
  - Step 3 前 FOR UPDATE 目标行,防并发篡改
  - 白名单硬编码,禁止其他 key 被修改
  - baseline 校验:threshold_patches["from"] 必须等于 live 当前值,否则 abort
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)


# payload 白名单 — 只有这三个 key 可以被 saga 修改
PAYLOAD_WHITELIST: frozenset[str] = frozenset({
    "strategy_entry_min_signal_edge_bps",
    "strategy_entry_alpha_min",
    "strategy_min_net_edge_bps",
})


# =============================================================================
# Exceptions
# =============================================================================

class SagaError(Exception):
    """Saga 通用错误。"""


class Step3BaselineDriftError(SagaError):
    """Step 3 前校验发现 live payload 与 research 期望 baseline 不一致。

    触发原因通常是:
      - operator 在 approve 后手动改了 live payload
      - 前一次 apply 已写入 live(重复 apply 检查漏了)
      - profile_research 读 baseline 时拿的是过期快照

    处理:saga abort,发 P1 告警,由 operator 手动 reconcile。
    """


class Step3TargetNotFoundError(SagaError):
    """live DB 里找不到对应 profile 的 activation 行。"""


class WhitelistViolationError(SagaError):
    """threshold_patches 含非白名单 key。"""


# =============================================================================
# Saga operation lifecycle
# =============================================================================

@dataclass
class SagaOperation:
    operation_id: str
    recommendation_id: str
    scope: str
    actor: str
    step1_done_at: datetime | None = None
    step2_done_at: datetime | None = None
    step3_done_at: datetime | None = None
    step4_done_at: datetime | None = None
    last_error: str | None = None

    @property
    def is_complete(self) -> bool:
        return all([
            self.step1_done_at, self.step2_done_at,
            self.step3_done_at, self.step4_done_at,
        ])


def find_or_create_saga_operation(
    research_session: Any,
    *,
    recommendation_id: str,
    scope: str,
    actor: str,
) -> SagaOperation:
    """找 recommendation_id 的最近一次 in-progress saga,或新建一条。

    以 recommendation_id 为查询键避免 R2-08 问题(target_parameter_set_id
    可能因 supersede 改变)。只有 step4 未完成的行被视为"可续跑"。
    """
    # 先尝试找未完成的 op
    row = research_session.execute(text("""
        SELECT operation_id, recommendation_id, scope, actor,
               step1_done_at, step2_done_at, step3_done_at, step4_done_at,
               last_error
        FROM governance.apply_saga_operations
        WHERE recommendation_id = :rid
          AND step4_done_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
    """), {"rid": recommendation_id}).first()

    if row is not None:
        return SagaOperation(
            operation_id=row.operation_id,
            recommendation_id=row.recommendation_id,
            scope=row.scope,
            actor=row.actor,
            step1_done_at=row.step1_done_at,
            step2_done_at=row.step2_done_at,
            step3_done_at=row.step3_done_at,
            step4_done_at=row.step4_done_at,
            last_error=row.last_error,
        )

    # 新建
    op_id = uuid.uuid4().hex
    research_session.execute(text("""
        INSERT INTO governance.apply_saga_operations
            (operation_id, recommendation_id, scope, actor)
        VALUES (:oid, :rid, :scope, :actor)
    """), {
        "oid": op_id,
        "rid": recommendation_id,
        "scope": scope,
        "actor": actor,
    })
    research_session.commit()

    return SagaOperation(
        operation_id=op_id,
        recommendation_id=recommendation_id,
        scope=scope,
        actor=actor,
    )


def _mark_step_done(
    research_session: Any, *, operation_id: str, step: int
) -> None:
    col = f"step{step}_done_at"
    research_session.execute(text(f"""
        UPDATE governance.apply_saga_operations
        SET {col} = NOW(), last_error = NULL
        WHERE operation_id = :oid
    """), {"oid": operation_id})


def _mark_saga_error(
    research_session: Any, *, operation_id: str, error: str
) -> None:
    research_session.execute(text("""
        UPDATE governance.apply_saga_operations
        SET last_error = :err
        WHERE operation_id = :oid
    """), {"oid": operation_id, "err": error[:500]})


# =============================================================================
# Individual steps
# =============================================================================

def step1_upsert_active(
    research_session: Any,
    *,
    profile_id: str,
    parameter_set_id: str,
    values: dict[str, Any],
    recommendation_id: str,
    actor: str,
) -> None:
    """Step 1: UPSERT governance.active_parameter_sets(scope='profile')。"""
    research_session.execute(text("""
        INSERT INTO governance.active_parameter_sets
            (scope, scope_ref, family, timeframe,
             parameter_set_id, values,
             approval_recommendation_id, applied_by, applied_at, updated_at)
        VALUES
            ('profile', :pid, NULL, NULL,
             :psid, :vals::jsonb,
             :rid, :actor, NOW(), NOW())
        ON CONFLICT (scope_ref) WHERE scope = 'profile' DO UPDATE SET
            parameter_set_id = EXCLUDED.parameter_set_id,
            values = EXCLUDED.values,
            approval_recommendation_id = EXCLUDED.approval_recommendation_id,
            applied_by = EXCLUDED.applied_by,
            applied_at = NOW(),
            updated_at = NOW()
    """), {
        "pid": profile_id,
        "psid": parameter_set_id,
        "vals": json.dumps(values, ensure_ascii=False),
        "rid": recommendation_id,
        "actor": actor,
    })


def step2_append_history(
    research_session: Any,
    *,
    operation_id: str,
    profile_id: str,
    to_parameter_set_id: str,
    from_parameter_set_id: str | None,
    recommendation_id: str,
    actor: str,
) -> None:
    """Step 2: INSERT parameter_apply_history (幂等键 operation_id)。"""
    research_session.execute(text("""
        INSERT INTO governance.parameter_apply_history
            (operation_id, operation_type, scope, scope_ref,
             family, timeframe,
             from_parameter_set_id, to_parameter_set_id,
             recommendation_id, actor)
        VALUES
            (:oid, 'apply', 'profile', :pid,
             NULL, NULL,
             :from_ps, :to_ps,
             :rid, :actor)
        ON CONFLICT (operation_id) DO NOTHING
    """), {
        "oid": operation_id,
        "pid": profile_id,
        "from_ps": from_parameter_set_id,
        "to_ps": to_parameter_set_id,
        "rid": recommendation_id,
        "actor": actor,
    })


def step3_update_live_payload(
    live_session: Any,
    *,
    profile_id: str,
    threshold_patches: dict[str, dict[str, float]],
    operation_id: str,
) -> None:
    """Step 3: 安全 merge 到 strategy_profile_activation.payload。

    threshold_patches 形如:
        {"strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.2}, ...}

    流程:
      1. 白名单检查
      2. SELECT FOR UPDATE 目标行
      3. baseline 校验:所有 key 的 live 值必须 == patches[key]["from"]
      4. jsonb_set 逐 key 写入
    """
    for key in threshold_patches:
        if key not in PAYLOAD_WHITELIST:
            raise WhitelistViolationError(
                f"patch key {key!r} not in whitelist {sorted(PAYLOAD_WHITELIST)}"
            )

    # SELECT FOR UPDATE
    row = live_session.execute(text("""
        SELECT activation_id, payload, product_type, margin_mode
        FROM strategy_profile_activation
        WHERE payload->>'profile_id' = :pid
        FOR UPDATE
    """), {"pid": profile_id}).first()

    if row is None:
        raise Step3TargetNotFoundError(
            f"no strategy_profile_activation row for profile_id={profile_id!r}"
        )

    payload = row.payload if not isinstance(row.payload, str) else json.loads(row.payload)

    # Baseline check
    drift_errors: list[str] = []
    for key, patch in threshold_patches.items():
        current = payload.get(key)
        expected = patch["from"]
        if current != expected:
            drift_errors.append(
                f"{key}: live={current!r} != expected_from={expected!r}"
            )
    if drift_errors:
        raise Step3BaselineDriftError(
            f"profile {profile_id!r} baseline drift: {'; '.join(drift_errors)}"
        )

    # jsonb_set 逐 key
    for key, patch in threshold_patches.items():
        new_val = patch["to"]
        live_session.execute(text("""
            UPDATE strategy_profile_activation
            SET payload = jsonb_set(payload, ARRAY[:key], to_jsonb(:val::numeric))
            WHERE activation_id = :aid
        """), {
            "aid": row.activation_id,
            "key": key,
            "val": new_val,
        })


def step4_append_live_history(
    live_session: Any,
    *,
    profile_id: str,
    operation_id: str,
    threshold_patches: dict[str, dict[str, float]],
    actor: str,
) -> None:
    """Step 4: 写 strategy_profile_activation_history 审计事件。"""
    # 读 product_type / margin_mode 用于 index
    row = live_session.execute(text("""
        SELECT product_type, margin_mode, payload
        FROM strategy_profile_activation
        WHERE payload->>'profile_id' = :pid
    """), {"pid": profile_id}).first()

    if row is None:
        raise Step3TargetNotFoundError(f"profile {profile_id!r} missing post-step3")

    event_id = f"rdp_apply:{operation_id}"
    audit_payload = {
        "event_type": "rdp_apply",
        "operation_id": operation_id,
        "profile_id": profile_id,
        "actor": actor,
        "threshold_patches": threshold_patches,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }

    live_session.execute(text("""
        INSERT INTO strategy_profile_activation_history
            (activation_event_id, product_type, margin_mode, executed_at, payload)
        VALUES
            (:eid, :pt, :mm, NOW(), :pl::jsonb)
        ON CONFLICT (activation_event_id) DO NOTHING
    """), {
        "eid": event_id,
        "pt": row.product_type,
        "mm": row.margin_mode,
        "pl": json.dumps(audit_payload, ensure_ascii=False),
    })


# =============================================================================
# Orchestrator
# =============================================================================

@dataclass
class SagaResult:
    operation_id: str
    ok: bool
    error: str | None = None
    steps_completed: int = 0  # 0..4


def apply_profile_saga(
    *,
    research_session: Any,
    live_session: Any,
    saga_op: SagaOperation,
    profile_id: str,
    parameter_set_id: str,
    values: dict[str, Any],
    from_parameter_set_id: str | None,
    threshold_patches: dict[str, dict[str, float]],
) -> SagaResult:
    """按序执行 saga 四步。

    幂等:已完成的 step 会跳过。任一 step 失败,记 last_error + 返回 ok=False。
    caller 可再次调用 apply_profile_saga(同 saga_op)重放。
    """
    recommendation_id = saga_op.recommendation_id
    actor = saga_op.actor
    operation_id = saga_op.operation_id

    try:
        # Step 1
        if saga_op.step1_done_at is None:
            step1_upsert_active(
                research_session,
                profile_id=profile_id,
                parameter_set_id=parameter_set_id,
                values=values,
                recommendation_id=recommendation_id,
                actor=actor,
            )
            _mark_step_done(research_session, operation_id=operation_id, step=1)
            research_session.commit()

        # Step 2
        if saga_op.step2_done_at is None:
            step2_append_history(
                research_session,
                operation_id=operation_id,
                profile_id=profile_id,
                to_parameter_set_id=parameter_set_id,
                from_parameter_set_id=from_parameter_set_id,
                recommendation_id=recommendation_id,
                actor=actor,
            )
            _mark_step_done(research_session, operation_id=operation_id, step=2)
            research_session.commit()

        # Step 3 — live DB
        if saga_op.step3_done_at is None:
            step3_update_live_payload(
                live_session,
                profile_id=profile_id,
                threshold_patches=threshold_patches,
                operation_id=operation_id,
            )
            live_session.commit()
            _mark_step_done(research_session, operation_id=operation_id, step=3)
            research_session.commit()

        # Step 4 — live DB
        if saga_op.step4_done_at is None:
            step4_append_live_history(
                live_session,
                profile_id=profile_id,
                operation_id=operation_id,
                threshold_patches=threshold_patches,
                actor=actor,
            )
            live_session.commit()
            _mark_step_done(research_session, operation_id=operation_id, step=4)
            research_session.commit()

        return SagaResult(
            operation_id=operation_id,
            ok=True,
            steps_completed=4,
        )

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        log.error("apply_profile_saga failed: op=%s err=%s", operation_id, error_msg)
        try:
            live_session.rollback()
        except Exception:
            pass
        try:
            research_session.rollback()
            _mark_saga_error(research_session, operation_id=operation_id, error=error_msg)
            research_session.commit()
        except Exception:
            log.exception("failed to record saga error")

        # reconstruct steps_completed from DB (best-effort)
        steps_completed = sum(
            1 for t in [
                saga_op.step1_done_at, saga_op.step2_done_at,
                saga_op.step3_done_at, saga_op.step4_done_at,
            ] if t is not None
        )
        return SagaResult(
            operation_id=operation_id,
            ok=False,
            error=error_msg,
            steps_completed=steps_completed,
        )
