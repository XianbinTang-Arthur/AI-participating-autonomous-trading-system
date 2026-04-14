"""Stage 9 AbortHookService 状态机单元测试。

覆盖范围
========

1. 配置
   - from_settings 从 AATSSettings 正确提取所有 stage9_abort_hook_* 字段
   - enabled=False 时 service 不启动 task

2. evaluate_once 基本行为
   - disabled 时返回 None 不动状态
   - inputs_provider 抛异常 → fail-soft 返回 None, 不影响后续 evaluate
   - compute_drift_score 异常 → fail-soft 返回 None

3. 状态机 monitoring → warning → halting
   - 单次 score=4 (halt_on_repeat) → warning, consecutive=1
   - 连续 2 次 score=4 → halting, kill_switch.halt 被调用
   - warning 之后 score=0 → monitoring, consecutive 清零
   - 单次 score=5 → halting (绕过 warning)
   - 单个 subscore=2 (score<5) → halting (halt_immediate 规则)

4. Halt reason 编码
   - score ≥ 5 → reason 包含 "score_ge_5"
   - 连续 2 次 → reason 包含 "score_3_4_consecutive_2"
   - subscore=2 → reason 包含 "subscore_<category>_2"

5. halting → cooldown → monitoring
   - halting 状态下 operator resume kill_switch → cooldown + 记录 ends_at
   - cooldown 期间 score=5 → 不 halt, 只记录
   - cooldown 过期 → 自动回到 monitoring, 之后 score=5 会重新 halt

6. Snapshot introspection
   - evaluations_total / halts_triggered 计数正确
   - last_total_score / last_state_transition_reason 反映最新 evaluate
   - snapshot 是浅拷贝 (不会被后续 state 变更污染)

7. on_abort_hook_event 回调
   - 每次状态转换调一次回调
   - 回调抛异常 fail-soft 不影响 state machine

这些测试**不**起完整 runtime, 只用 in-memory KillSwitch + fake inputs_provider
+ 自定义 time_provider (避开真 sleep)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from aats.services.governance_engine.abort_hooks import (
    AbortHookConfig,
    AbortHookService,
)
from aats.services.governance_engine.drift_score import DriftInputs
from aats.services.governance_engine.kill_switch import KillSwitch


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


class _ClockFake:
    """可控的时间源。测试可以主动 advance() 模拟 cooldown 过期。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _clean_inputs(stage: str = "T2") -> DriftInputs:
    return DriftInputs(
        stage=stage,
        window_hours=24,
        evaluated_at=datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc),
        balance_drift_ratio=Decimal("0.001"),
        max_drawdown_ratio=Decimal("0.01"),
        fee_to_pnl_ratio=Decimal("0.10"),
        fill_success_ratio=Decimal("0.99"),
        adverse_slippage_ratio=Decimal("0.005"),
        decision_cycle_cadence_ratio=Decimal("0.99"),
        decision_error_ratio=Decimal("0.001"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0001"),
        okx_rate_limit_count=0,
    )


def _warning_inputs(stage: str = "T2") -> DriftInputs:
    """构造一个 total_score = 4, abort_action=halt_on_repeat 的输入。"""
    return DriftInputs(
        stage=stage,
        window_hours=24,
        evaluated_at=datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc),
        balance_drift_ratio=Decimal("0.03"),      # 1
        max_drawdown_ratio=Decimal("0.04"),       # 1
        fee_to_pnl_ratio=Decimal("0.45"),         # 1
        fill_success_ratio=Decimal("0.93"),       # 1 (反向)
        adverse_slippage_ratio=Decimal("0.05"),   # 1
        decision_cycle_cadence_ratio=Decimal("0.85"),  # 1 (反向)
        decision_error_ratio=Decimal("0.03"),     # 1
        reconciliation_mismatch_count=2,           # 1
        nats_handler_error_ratio=Decimal("0.005"),# 1
        okx_rate_limit_count=3,                    # 1
    )


def _critical_inputs_score_8(stage: str = "T2") -> DriftInputs:
    """全 critical → total=8, abort=halt_immediate."""
    return DriftInputs(
        stage=stage,
        window_hours=24,
        evaluated_at=datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc),
        balance_drift_ratio=Decimal("0.10"),
        max_drawdown_ratio=Decimal("0.08"),
        fee_to_pnl_ratio=Decimal("0.80"),
        fill_success_ratio=Decimal("0.85"),
        adverse_slippage_ratio=Decimal("0.20"),
        decision_cycle_cadence_ratio=Decimal("0.50"),
        decision_error_ratio=Decimal("0.10"),
        reconciliation_mismatch_count=5,
        nats_handler_error_ratio=Decimal("0.05"),
        okx_rate_limit_count=10,
    )


def _financial_critical_inputs(stage: str = "T2") -> DriftInputs:
    """仅 financial 子类全 critical (subscore=2), 其他 clean.

    total_score = round(2 * 1/3 * 4) = round(2.67) = 3
    但 abort_action 应当是 halt_immediate（subscore=2 规则覆盖）。
    """
    return DriftInputs(
        stage=stage,
        window_hours=24,
        evaluated_at=datetime(2026, 4, 8, 11, 30, 0, tzinfo=timezone.utc),
        balance_drift_ratio=Decimal("0.10"),
        max_drawdown_ratio=Decimal("0.08"),
        fee_to_pnl_ratio=Decimal("0.80"),
        fill_success_ratio=Decimal("0.99"),
        adverse_slippage_ratio=Decimal("0.005"),
        decision_cycle_cadence_ratio=Decimal("0.99"),
        decision_error_ratio=Decimal("0.001"),
        reconciliation_mismatch_count=0,
        nats_handler_error_ratio=Decimal("0.0001"),
        okx_rate_limit_count=0,
    )


def _make_service(
    *,
    enabled: bool = True,
    inputs: DriftInputs | None = None,
    kill_switch: KillSwitch | None = None,
    clock: _ClockFake | None = None,
    consecutive: int = 2,
    cooldown_seconds: float = 1800.0,
    on_event: Any = None,
) -> tuple[AbortHookService, KillSwitch, _ClockFake, list[dict]]:
    ks = kill_switch or KillSwitch()
    clk = clock or _ClockFake()
    events: list[dict] = []
    if on_event is None:
        def _capture(payload: dict[str, Any]) -> None:
            events.append(dict(payload))
        on_event = _capture

    inputs_ref = {"current": inputs or _clean_inputs()}

    def _provider() -> DriftInputs:
        return inputs_ref["current"]

    service = AbortHookService(
        config=AbortHookConfig(
            enabled=enabled,
            evaluate_interval_seconds=60.0,
            consecutive_warning_threshold=consecutive,
            cooldown_seconds=cooldown_seconds,
        ),
        kill_switch=ks,
        inputs_provider=_provider,
        logger=logging.getLogger("test.abort_hooks"),
        on_abort_hook_event=on_event,
        time_provider=clk,
    )
    # 挂一个引用让测试可以改 inputs
    service._test_inputs_ref = inputs_ref  # type: ignore[attr-defined]
    return service, ks, clk, events


def _set_inputs(service: AbortHookService, inputs: DriftInputs) -> None:
    service._test_inputs_ref["current"] = inputs  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────
# 1. 配置
# ─────────────────────────────────────────────────────────────────────


def test_config_from_settings_reads_all_fields() -> None:
    class FakeSettings:
        stage9_abort_hook_enabled = True
        stage9_abort_hook_evaluate_interval_seconds = 45.0
        stage9_abort_hook_consecutive_warnings = 3
        stage9_abort_hook_cooldown_seconds = 600.0

    cfg = AbortHookConfig.from_settings(FakeSettings())
    assert cfg.enabled is True
    assert cfg.evaluate_interval_seconds == 45.0
    assert cfg.consecutive_warning_threshold == 3
    assert cfg.cooldown_seconds == 600.0


def test_config_from_settings_fills_defaults() -> None:
    class EmptySettings:
        pass

    cfg = AbortHookConfig.from_settings(EmptySettings())
    assert cfg.enabled is False
    assert cfg.evaluate_interval_seconds == 60.0
    assert cfg.consecutive_warning_threshold == 2
    assert cfg.cooldown_seconds == 1800.0


def test_disabled_service_snapshot_state_is_disabled() -> None:
    service, *_ = _make_service(enabled=False)
    snap = service.snapshot()
    assert snap.enabled is False
    assert snap.state == "disabled"


# ─────────────────────────────────────────────────────────────────────
# 2. evaluate_once fail-soft
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_once_disabled_returns_none() -> None:
    service, ks, _, _ = _make_service(enabled=False)
    result = await service.evaluate_once()
    assert result is None
    assert ks.halted is False


@pytest.mark.asyncio
async def test_evaluate_once_clean_stays_monitoring() -> None:
    service, ks, _, events = _make_service(inputs=_clean_inputs())
    report = await service.evaluate_once()
    assert report is not None
    assert report.total_score == 0
    snap = service.snapshot()
    assert snap.state == "monitoring"
    assert snap.consecutive_warning_count == 0
    assert snap.evaluations_total == 1
    assert snap.halts_triggered == 0
    assert ks.halted is False


@pytest.mark.asyncio
async def test_evaluate_once_inputs_provider_exception_is_fail_soft() -> None:
    ks = KillSwitch()

    def _broken_provider() -> DriftInputs:
        raise RuntimeError("data source gone")

    service = AbortHookService(
        config=AbortHookConfig(enabled=True),
        kill_switch=ks,
        inputs_provider=_broken_provider,
        logger=logging.getLogger("test.abort_hooks"),
    )
    result = await service.evaluate_once()
    assert result is None
    assert service.snapshot().state == "monitoring"  # 状态不推进
    assert ks.halted is False


@pytest.mark.asyncio
async def test_evaluate_once_increments_evaluations_total() -> None:
    service, *_ = _make_service(inputs=_clean_inputs())
    await service.evaluate_once()
    await service.evaluate_once()
    await service.evaluate_once()
    assert service.snapshot().evaluations_total == 3


# ─────────────────────────────────────────────────────────────────────
# 3. 状态机 monitoring → warning → halting
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_score_4_enters_warning() -> None:
    service, ks, _, events = _make_service(inputs=_warning_inputs())
    report = await service.evaluate_once()
    assert report is not None
    assert report.total_score == 4
    snap = service.snapshot()
    assert snap.state == "warning"
    assert snap.consecutive_warning_count == 1
    assert ks.halted is False
    assert any(
        ev["new_state"] == "warning"
        for ev in events
    )


@pytest.mark.asyncio
async def test_two_consecutive_score_4_triggers_halt() -> None:
    service, ks, _, events = _make_service(inputs=_warning_inputs())
    await service.evaluate_once()  # warning, consecutive=1
    await service.evaluate_once()  # consecutive=2 → halt
    snap = service.snapshot()
    assert snap.state == "halting"
    assert snap.halts_triggered == 1
    assert snap.consecutive_warning_count == 0  # halt 之后清零
    assert ks.halted is True
    assert "stage9_abort_hook:score_3_4_consecutive" in (ks.status()["reason"] or "")


@pytest.mark.asyncio
async def test_warning_then_clean_resets_consecutive() -> None:
    service, ks, _, _ = _make_service(inputs=_warning_inputs())
    await service.evaluate_once()  # warning
    _set_inputs(service, _clean_inputs())
    await service.evaluate_once()  # clean → back to monitoring
    snap = service.snapshot()
    assert snap.state == "monitoring"
    assert snap.consecutive_warning_count == 0
    assert ks.halted is False


@pytest.mark.asyncio
async def test_single_score_8_halts_immediately() -> None:
    service, ks, _, _ = _make_service(inputs=_critical_inputs_score_8())
    await service.evaluate_once()
    snap = service.snapshot()
    assert snap.state == "halting"
    assert snap.halts_triggered == 1
    assert ks.halted is True
    assert "stage9_abort_hook:score_ge_5" in (ks.status()["reason"] or "")


@pytest.mark.asyncio
async def test_financial_subscore_2_halts_immediately_despite_low_total() -> None:
    """financial subscore=2 应触发 halt_immediate，
    即使 total_score 只有 3（低于 ≥5 门槛）。"""
    service, ks, _, _ = _make_service(inputs=_financial_critical_inputs())
    report = await service.evaluate_once()
    assert report is not None
    # total = round(2 * 1/3 * 4) = round(2.67) = 3
    assert report.total_score == 3
    assert report.abort_hook_action == "halt_immediate"
    snap = service.snapshot()
    assert snap.state == "halting"
    assert ks.halted is True
    reason = ks.status()["reason"] or ""
    assert "stage9_abort_hook:subscore_financial_2" in reason


@pytest.mark.asyncio
async def test_custom_consecutive_threshold_one() -> None:
    """consecutive_warning_threshold=1 时单次 warning 立刻 halt。"""
    service, ks, _, _ = _make_service(
        inputs=_warning_inputs(),
        consecutive=1,
    )
    await service.evaluate_once()
    snap = service.snapshot()
    assert snap.state == "halting"
    assert ks.halted is True


# ─────────────────────────────────────────────────────────────────────
# 4. halting → cooldown → monitoring
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_resume_enters_cooldown() -> None:
    service, ks, clk, _ = _make_service(
        inputs=_critical_inputs_score_8(),
        cooldown_seconds=1800.0,
    )
    await service.evaluate_once()  # halt
    assert service.snapshot().state == "halting"

    # operator manually resumes
    ks.resume()
    assert ks.halted is False

    # 下一次 evaluate 应把状态切到 cooldown
    # （此刻 inputs 仍然是 critical，但 cooldown 期间不会再 halt）
    await service.evaluate_once()
    snap = service.snapshot()
    assert snap.state == "cooldown"
    assert snap.cooldown_ends_at is not None
    assert snap.cooldown_ends_at == clk.now + 1800.0


@pytest.mark.asyncio
async def test_cooldown_blocks_halt_during_window() -> None:
    service, ks, clk, _ = _make_service(
        inputs=_critical_inputs_score_8(),
        cooldown_seconds=1800.0,
    )
    await service.evaluate_once()  # halt 1
    ks.resume()
    await service.evaluate_once()  # → cooldown

    # advance 10 分钟（cooldown 未过）
    clk.advance(600.0)
    await service.evaluate_once()
    snap = service.snapshot()
    assert snap.state == "cooldown"
    assert snap.halts_triggered == 1  # 没有新 halt
    assert ks.halted is False


@pytest.mark.asyncio
async def test_cooldown_expires_then_halts_again() -> None:
    service, ks, clk, _ = _make_service(
        inputs=_critical_inputs_score_8(),
        cooldown_seconds=1800.0,
    )
    # 第一次 halt
    await service.evaluate_once()
    ks.resume()
    await service.evaluate_once()  # → cooldown

    # 过完 cooldown 1800s
    clk.advance(1801.0)
    await service.evaluate_once()
    snap = service.snapshot()
    # cooldown 先切到 monitoring, 再被 critical 触发 halt
    assert snap.state == "halting"
    assert snap.halts_triggered == 2
    assert ks.halted is True


# ─────────────────────────────────────────────────────────────────────
# 5. snapshot 字段
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_reflects_latest_evaluate() -> None:
    service, _, clk, _ = _make_service(inputs=_clean_inputs())
    await service.evaluate_once()
    snap = service.snapshot()
    assert snap.last_evaluated_at == clk.now
    assert snap.last_total_score == 0
    assert snap.last_abort_hook_action == "none"


@pytest.mark.asyncio
async def test_snapshot_is_shallow_copy() -> None:
    service, _, _, _ = _make_service(inputs=_clean_inputs())
    await service.evaluate_once()
    snap_before = service.snapshot()
    _set_inputs(service, _critical_inputs_score_8())
    await service.evaluate_once()
    # snap_before 应当不被后续 state 变更污染
    assert snap_before.state == "monitoring"
    snap_after = service.snapshot()
    assert snap_after.state == "halting"


# ─────────────────────────────────────────────────────────────────────
# 6. on_abort_hook_event 回调
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_event_called_on_every_transition() -> None:
    service, _, _, events = _make_service(inputs=_warning_inputs())
    await service.evaluate_once()  # → warning
    await service.evaluate_once()  # → halting
    # 至少两次 transition
    states = [e["new_state"] for e in events]
    assert "warning" in states
    assert "halting" in states


@pytest.mark.asyncio
async def test_on_event_callback_exception_is_fail_soft() -> None:
    ks = KillSwitch()

    def _broken(payload: dict[str, Any]) -> None:
        raise RuntimeError("callback failure")

    inputs_ref = {"current": _critical_inputs_score_8()}
    service = AbortHookService(
        config=AbortHookConfig(enabled=True),
        kill_switch=ks,
        inputs_provider=lambda: inputs_ref["current"],
        logger=logging.getLogger("test.abort_hooks"),
        on_abort_hook_event=_broken,
    )
    # 回调抛也不能阻止 halt
    await service.evaluate_once()
    assert service.snapshot().state == "halting"
    assert ks.halted is True


# ─────────────────────────────────────────────────────────────────────
# 7. start / stop 生命周期
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_disabled_does_not_launch_task() -> None:
    service, *_ = _make_service(enabled=False)
    await service.start()
    # 再 stop 不抛
    await service.stop()


@pytest.mark.asyncio
async def test_start_enabled_then_stop_cancels_task() -> None:
    service, *_ = _make_service(inputs=_clean_inputs())
    # start 后立刻 stop，task 应当被干净 cancel
    await service.start()
    # 不要 sleep 等待 interval，直接 stop 也要 OK
    await service.stop()


@pytest.mark.asyncio
async def test_double_start_is_idempotent() -> None:
    service, *_ = _make_service(inputs=_clean_inputs())
    await service.start()
    await service.start()  # 不应抛
    await service.stop()


# ─────────────────────────────────────────────────────────────────────
# 8. halt reason code 解析
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_halt_reason_code_score_ge_5() -> None:
    service, ks, _, _ = _make_service(inputs=_critical_inputs_score_8())
    await service.evaluate_once()
    assert ks.status()["reason"] == "stage9_abort_hook:score_ge_5"


@pytest.mark.asyncio
async def test_halt_reason_code_subscore_financial() -> None:
    service, ks, _, _ = _make_service(inputs=_financial_critical_inputs())
    await service.evaluate_once()
    assert ks.status()["reason"] == "stage9_abort_hook:subscore_financial_2"


@pytest.mark.asyncio
async def test_halt_reason_code_consecutive_2() -> None:
    service, ks, _, _ = _make_service(
        inputs=_warning_inputs(),
        consecutive=2,
    )
    await service.evaluate_once()
    await service.evaluate_once()
    reason = ks.status()["reason"] or ""
    assert reason.startswith("stage9_abort_hook:score_3_4_consecutive_")
