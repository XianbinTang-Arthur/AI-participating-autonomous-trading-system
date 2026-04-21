"""profile_apply_saga 的 step 级别单测 — mock sessions。

覆盖关键安全性场景:
  * whitelist 违反 → WhitelistViolationError
  * baseline drift → Step3BaselineDriftError
  * target 找不到 → Step3TargetNotFoundError
  * happy path saga 全 4 步完成
  * saga 幂等性(已完成的 step 不重跑)
  * saga 失败 → last_error 被记录,steps_completed 正确
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aats.data_platform.governance.profile_apply_saga import (
    PAYLOAD_WHITELIST,
    SagaOperation,
    Step3BaselineDriftError,
    Step3TargetNotFoundError,
    WhitelistViolationError,
    apply_profile_saga,
    step3_update_live_payload,
)


def _mk_live_session(payload: dict | None):
    """Mock live session 里 strategy_profile_activation 的返回值。"""
    sess = MagicMock()
    if payload is None:
        sess.execute.return_value.first.return_value = None
    else:
        row = MagicMock()
        row.activation_id = "act-123"
        row.payload = payload
        row.product_type = "SWAP"
        row.margin_mode = "cross"
        sess.execute.return_value.first.return_value = row
    return sess


def test_whitelist_violation_rejects() -> None:
    sess = _mk_live_session({"strategy_entry_min_signal_edge_bps": 13.0})
    with pytest.raises(WhitelistViolationError):
        step3_update_live_payload(
            sess,
            profile_id="trend_normal",
            threshold_patches={
                # 非白名单 key
                "risk_position_max_notional": {"from": 100, "to": 200},
            },
            operation_id="op-1",
        )


def test_target_not_found() -> None:
    sess = _mk_live_session(None)
    with pytest.raises(Step3TargetNotFoundError):
        step3_update_live_payload(
            sess,
            profile_id="unknown",
            threshold_patches={
                "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.0},
            },
            operation_id="op-1",
        )


def test_baseline_drift_detected() -> None:
    # live 显示 10.0,但 research 以为 13.0 → drift
    sess = _mk_live_session({"strategy_entry_min_signal_edge_bps": 10.0})
    with pytest.raises(Step3BaselineDriftError):
        step3_update_live_payload(
            sess,
            profile_id="trend_normal",
            threshold_patches={
                "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 8.0},
            },
            operation_id="op-1",
        )


def test_baseline_match_runs_jsonb_set() -> None:
    sess = _mk_live_session({"strategy_entry_min_signal_edge_bps": 13.0})
    step3_update_live_payload(
        sess,
        profile_id="trend_normal",
        threshold_patches={
            "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.0},
        },
        operation_id="op-1",
    )
    # 预期两次 execute:SELECT FOR UPDATE + UPDATE jsonb_set
    assert sess.execute.call_count == 2


# -----------------------------------------------------------------------------
# apply_profile_saga orchestrator
# -----------------------------------------------------------------------------

class _ResearchSessionStub:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.calls: list[str] = []

    def execute(self, stmt, params=None):  # noqa: ANN001
        self.calls.append(str(stmt)[:60])
        result = MagicMock()
        result.first.return_value = None
        result.rowcount = 1
        return result

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_saga_happy_path_completes_4_steps() -> None:
    research = _ResearchSessionStub()
    live = _mk_live_session({"strategy_entry_min_signal_edge_bps": 13.0})

    saga_op = SagaOperation(
        operation_id="op-happy",
        recommendation_id="rec-1",
        scope="profile",
        actor="alice",
    )

    result = apply_profile_saga(
        research_session=research,
        live_session=live,
        saga_op=saga_op,
        profile_id="trend_normal",
        parameter_set_id="ps-1",
        values={"strategy_entry_min_signal_edge_bps": 10.0},
        from_parameter_set_id="ps-0",
        threshold_patches={
            "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.0},
        },
    )
    assert result.ok
    assert result.steps_completed == 4
    assert research.commits >= 4  # 每步都 commit


def test_saga_baseline_drift_returns_failure() -> None:
    research = _ResearchSessionStub()
    live = _mk_live_session({"strategy_entry_min_signal_edge_bps": 7.0})  # drift

    saga_op = SagaOperation(
        operation_id="op-drift",
        recommendation_id="rec-drift",
        scope="profile",
        actor="alice",
    )

    result = apply_profile_saga(
        research_session=research,
        live_session=live,
        saga_op=saga_op,
        profile_id="trend_normal",
        parameter_set_id="ps-1",
        values={"strategy_entry_min_signal_edge_bps": 10.0},
        from_parameter_set_id="ps-0",
        threshold_patches={
            "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.0},
        },
    )
    assert not result.ok
    assert "Step3BaselineDriftError" in (result.error or "")
    assert research.rollbacks >= 1


def test_saga_skips_completed_steps() -> None:
    """已标记完成的 step 不应再执行 SQL。"""
    research = _ResearchSessionStub()
    live = _mk_live_session({"strategy_entry_min_signal_edge_bps": 13.0})

    # step1/2/3 都标记已完成,只剩 step4
    from datetime import datetime, timezone
    saga_op = SagaOperation(
        operation_id="op-resume",
        recommendation_id="rec-resume",
        scope="profile",
        actor="alice",
        step1_done_at=datetime.now(timezone.utc),
        step2_done_at=datetime.now(timezone.utc),
        step3_done_at=datetime.now(timezone.utc),
    )

    result = apply_profile_saga(
        research_session=research,
        live_session=live,
        saga_op=saga_op,
        profile_id="trend_normal",
        parameter_set_id="ps-1",
        values={"strategy_entry_min_signal_edge_bps": 10.0},
        from_parameter_set_id=None,
        threshold_patches={
            "strategy_entry_min_signal_edge_bps": {"from": 13.0, "to": 10.0},
        },
    )
    assert result.ok
    # 只应该跑 step4(INSERT history) + 一次 mark_step_done
    # research session 的 execute 只在 step4 和 _mark_step_done 被调用
    # Upsert active / append apply_history / baseline check 不应跑 → research 端 <= 2 条 execute
    research_inserts = [c for c in research.calls if "INSERT INTO governance" in c or "UPDATE governance.apply_saga" in c]
    # step4 = mark + step4 insert live history(live side) → research 端主要是 _mark_step_done
    assert len(research_inserts) <= 2


def test_payload_whitelist_is_hardcoded_3_keys() -> None:
    assert PAYLOAD_WHITELIST == frozenset({
        "strategy_entry_min_signal_edge_bps",
        "strategy_entry_alpha_min",
        "strategy_min_net_edge_bps",
    })
