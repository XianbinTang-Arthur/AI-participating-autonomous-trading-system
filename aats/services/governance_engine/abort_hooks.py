"""Stage 9 AbortHookService —— 定期评估 drift score 并在命中时 halt。

设计文档
========
docs/task/stage_9_abort_hooks_design.md §5

核心不变量
==========
I1 **fail-soft**: ``evaluate_once`` / ``_loop`` 里的任何异常都**不允许**
   propagate。异常会被 log_event 记录，状态机不推进（下一次周期重试）。
I2 AbortHookService 是 sidecar 而不是核心路径。它挂掉 main trading loop
   必须照常跑，只是失去了 stage9 的自动 halt 能力（trial_guard 仍然兜底）。
I3 halt 之后进入 30 分钟 cooldown，期间 score 再次命中只记录不 halt。
   避免一次 transient OKX 事故把系统反复 halt/resume 打脏 event store。
I4 与 trial_guard 一样只在 decision + execution + monolith role 下启动。
   gateway 不做业务决策，market 只推行情，对它们来说 drift 分数无意义。
   跨进程 halt 靠 KillSwitch 自己的 NATS 广播。
I5 sidecar **不持有** drift_score 的可变状态 —— 每次 evaluate 从 provider
   callback 重新收集 DriftInputs，纯函数 compute_drift_score 负责计算。
   AbortHookService 自己只维护状态机 (monitoring / warning / halting /
   resumed / cooldown) 与 consecutive_warning_count 两个计数器。

为什么用 provider callback 模式
================================
与 ForwardTrialGuardService 对齐：让单测能用 in-memory fake provider
驱动，而不必起完整 runtime。provider 有两个：

- ``inputs_provider``: Callable[[], DriftInputs]  —— 收集当前 live 数据
- ``on_abort_hook_event``: Callable[[dict], None]  —— 可选，每次状态转换
  时被调用，单测用它断言 "是否 halt"、"是否 resumed"。生产环境一般传
  None 让 sidecar 自己走 log_event + kill_switch.halt。

checklist-4 只实现状态机；inputs_provider 的"从 live portfolio/execution/
health service 收集数据"的胶水留给 checklist-4 收尾 slice 或 checklist-5。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from aats.bootstrap.telemetry import start_span
from aats.services.governance_engine.drift_score import (
    DriftInputs,
    DriftReport,
    compute_drift_score,
)
from aats.services.governance_engine.kill_switch import KillSwitch

# ─────────────────────────────────────────────────────────────────────
# 状态机枚举
# ─────────────────────────────────────────────────────────────────────

AbortHookState = Literal[
    "disabled",     # settings.stage9_abort_hook_enabled is False
    "monitoring",   # 正常监测中，没有 warning
    "warning",      # 刚命中一次 [3,4]，等看第二次
    "halting",      # 已经 halt，等 operator resume
    "cooldown",     # operator resumed，30 分钟内不触发新 halt
]


_DEFAULT_CONSECUTIVE_WARNINGS = 2
_DEFAULT_COOLDOWN_SECONDS = 1800.0
_DEFAULT_EVALUATE_INTERVAL = 60.0


# ─────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AbortHookConfig:
    """AbortHookService 的完整配置。

    工厂方法 ``from_settings`` 从 AATSSettings 里提取所有 stage9_abort_hook_*
    字段。直接构造用于测试。
    """

    enabled: bool
    evaluate_interval_seconds: float = _DEFAULT_EVALUATE_INTERVAL
    consecutive_warning_threshold: int = _DEFAULT_CONSECUTIVE_WARNINGS
    cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS

    @classmethod
    def from_settings(cls, settings: Any) -> "AbortHookConfig":
        return cls(
            enabled=bool(getattr(settings, "stage9_abort_hook_enabled", False)),
            evaluate_interval_seconds=float(
                getattr(settings, "stage9_abort_hook_evaluate_interval_seconds", _DEFAULT_EVALUATE_INTERVAL)
            ),
            consecutive_warning_threshold=int(
                getattr(settings, "stage9_abort_hook_consecutive_warnings", _DEFAULT_CONSECUTIVE_WARNINGS)
            ),
            cooldown_seconds=float(
                getattr(settings, "stage9_abort_hook_cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS)
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# 状态机上下文
# ─────────────────────────────────────────────────────────────────────


@dataclass
class AbortHookSnapshot:
    """给外部 introspection（/system/abort_hook/state endpoint / test）用。

    所有字段都是 JSON-friendly。sidecar 内部不持有这个对象的引用，每次
    `snapshot()` 都重新生成一个浅拷贝。
    """

    enabled: bool
    state: AbortHookState
    consecutive_warning_count: int
    cooldown_ends_at: float | None
    last_evaluated_at: float | None
    last_total_score: int | None
    last_abort_hook_action: str | None
    last_state_transition_reason: str | None
    evaluations_total: int
    halts_triggered: int


# ─────────────────────────────────────────────────────────────────────
# Sidecar
# ─────────────────────────────────────────────────────────────────────


class AbortHookService:
    """定期评估 drift score + 自动 halt 的后台 sidecar。

    生命周期
    --------
    1. 构造: 纯数据装配（不启动 task）
    2. `start()`: 如果 enabled=True，launch 后台 _loop task
    3. `_loop` 每 evaluate_interval_seconds 调一次 `evaluate_once`
    4. `evaluate_once` 调 inputs_provider 收集数据 → compute_drift_score →
       _transition 推进状态机 → 可能调 kill_switch.halt
    5. `stop()`: 取消后台 task，等待 graceful shutdown

    线程安全性
    ----------
    所有 public 方法在主 loop 线程内运行（async）。`snapshot()` 是同步的
    但它只读 self 的字段不加锁 —— Python 对 assign 是原子的，读到半新半
    旧的快照不会崩只会略微不一致，这在 introspection 场景是可接受的。
    """

    def __init__(
        self,
        *,
        config: AbortHookConfig,
        kill_switch: KillSwitch,
        inputs_provider: Callable[[], DriftInputs],
        logger: logging.Logger,
        on_abort_hook_event: Optional[Callable[[dict[str, Any]], None]] = None,
        time_provider: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._kill_switch = kill_switch
        self._inputs_provider = inputs_provider
        self._logger = logger
        self._on_event = on_abort_hook_event
        self._time = time_provider

        # 状态机
        self._state: AbortHookState = "disabled" if not config.enabled else "monitoring"
        self._consecutive_warnings = 0
        self._cooldown_ends_at: float | None = None
        self._last_evaluated_at: float | None = None
        self._last_total_score: int | None = None
        self._last_abort_hook_action: str | None = None
        self._last_state_transition_reason: str | None = None
        self._evaluations_total = 0
        self._halts_triggered = 0

        # G-1C：缓存最后一次 DriftReport，供 /system/drift-report 端点查询
        self._last_drift_report: DriftReport | None = None

        # 后台 task
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动后台 task。如果 enabled=False，no-op。"""
        if not self._config.enabled:
            _log_event_safe(
                self._logger,
                "abort_hook_service_disabled",
                interval_seconds=self._config.evaluate_interval_seconds,
            )
            return
        if self._task is not None:
            return  # 已经 start 过
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(), name="aats_stage9_abort_hook"
        )
        _log_event_safe(
            self._logger,
            "abort_hook_service_started",
            interval_seconds=self._config.evaluate_interval_seconds,
            consecutive_threshold=self._config.consecutive_warning_threshold,
            cooldown_seconds=self._config.cooldown_seconds,
        )

    async def stop(self) -> None:
        """优雅停止。不抛。"""
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover
            _log_event_safe(
                self._logger,
                "abort_hook_service_stop_error",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        finally:
            self._task = None
            self._stop_event = None

    async def _loop(self) -> None:
        interval = max(1.0, float(self._config.evaluate_interval_seconds))
        while True:
            try:
                await self.evaluate_once()
            except Exception as exc:  # pragma: no cover
                # fail-soft：异常不能拖死 loop
                _log_event_safe(
                    self._logger,
                    "abort_hook_loop_unexpected_error",
                    level="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

    # ──────────────────────────────────────────────────────────────
    # 评估（public, 可被 probe / test 直接调）
    # ──────────────────────────────────────────────────────────────

    async def evaluate_once(self) -> DriftReport | None:
        """评估一次。出错不抛，返回 None。

        返回 DriftReport 供调用方 introspection（probe 脚本会 print 它）。
        """
        if not self._config.enabled:
            return None

        with start_span("governance.abort_hooks.evaluate_once"):
            return await self._evaluate_once_body()

    async def _evaluate_once_body(self) -> DriftReport | None:
        """evaluate_once 的内部实现——在 OTel span 上下文中执行。

        由 evaluate_once() 调用，拆分出来以避免给整个函数体增加缩进层级。
        不要直接调用此方法，请使用 evaluate_once()。
        """
        now = self._time()

        # 1. 收集 inputs（provider 抛错 → fail-soft 返 None）
        try:
            inputs = self._inputs_provider()
        except Exception as exc:
            _log_event_safe(
                self._logger,
                "abort_hook_inputs_provider_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

        # 2. 计算 drift score（纯函数，理论上不会抛，但万一也要 fail-soft）
        try:
            report = compute_drift_score(inputs)
        except Exception as exc:
            _log_event_safe(
                self._logger,
                "abort_hook_compute_drift_failed",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

        self._evaluations_total += 1
        self._last_evaluated_at = now
        self._last_total_score = report.total_score
        self._last_abort_hook_action = report.abort_hook_action
        self._last_drift_report = report

        # 3. 状态机推进
        try:
            self._advance_state(report=report, now=now)
        except Exception as exc:  # pragma: no cover
            _log_event_safe(
                self._logger,
                "abort_hook_state_machine_failed",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
            )

        return report

    # ──────────────────────────────────────────────────────────────
    # 状态机核心
    # ──────────────────────────────────────────────────────────────

    def _advance_state(self, *, report: DriftReport, now: float) -> None:
        """把一次 evaluation 的结果推进到下一个状态。

        规则（设计文档 §5.1 / §5.2）：
        - disabled: 不动
        - cooldown: 如果 now ≥ cooldown_ends_at，自动回到 monitoring；
          否则忽略 action（不推进 warning 计数）
        - halting: operator 手动 resume → cooldown；否则保持
        - monitoring / warning:
          * action=halt_immediate → halting
          * action=halt_on_repeat → warning（consecutive+=1，到阈值 halt）
          * action=warning (score==2) → monitoring（不升级但也不 halt）
          * action=none → monitoring（reset consecutive）
        """
        current = self._state

        # disabled 直接跳过（已经在 evaluate_once 开头挡过了，这里再防御一层）
        if current == "disabled":
            return

        # cooldown 逻辑
        if current == "cooldown":
            if self._cooldown_ends_at is not None and now >= self._cooldown_ends_at:
                self._transition(
                    "monitoring",
                    reason=f"cooldown_ended_at_{int(now)}",
                )
                # fallthrough 继续看 new action（允许 cooldown 结束后
                # 立刻根据新的 score 再次 halt）
            else:
                # 仍在 cooldown —— 只记日志不推进
                if report.abort_hook_action in ("halt_immediate", "halt_on_repeat"):
                    _log_event_safe(
                        self._logger,
                        "abort_hook_score_breach_during_cooldown",
                        level="info",
                        total_score=report.total_score,
                        abort_hook_action=report.abort_hook_action,
                        cooldown_remaining=(self._cooldown_ends_at or 0) - now,
                    )
                return

        # halting 状态：kill_switch.halted 如果被 operator 手动 resume，
        # abort_hook 要相应进入 cooldown
        if self._state == "halting":
            if not self._kill_switch.halted:
                self._cooldown_ends_at = now + self._config.cooldown_seconds
                self._transition(
                    "cooldown",
                    reason=f"operator_resumed_cooldown_until_{int(self._cooldown_ends_at)}",
                )
            return

        # monitoring / warning 推进
        action = report.abort_hook_action

        if action == "halt_immediate":
            self._trigger_halt(
                report=report,
                reason_code=self._halt_reason_code_immediate(report),
            )
            return

        if action == "halt_on_repeat":
            self._consecutive_warnings += 1
            if self._consecutive_warnings >= self._config.consecutive_warning_threshold:
                self._trigger_halt(
                    report=report,
                    reason_code=(
                        f"score_3_4_consecutive_{self._consecutive_warnings}"
                    ),
                )
                return
            # 还没到阈值，进入/保持 warning 状态
            self._transition(
                "warning",
                reason=f"score={report.total_score}_consecutive={self._consecutive_warnings}",
            )
            return

        # warning (score==2) 或 none：reset consecutive，回到 monitoring
        if self._consecutive_warnings > 0:
            self._transition(
                "monitoring",
                reason=f"drift_cleared_reset_consecutive_from_{self._consecutive_warnings}",
            )
        elif self._state != "monitoring":
            self._transition("monitoring", reason="action_none_back_to_monitoring")
        self._consecutive_warnings = 0

    def _trigger_halt(self, *, report: DriftReport, reason_code: str) -> None:
        """调 kill_switch.halt + 切到 halting 状态。"""
        full_reason = f"stage9_abort_hook:{reason_code}"
        # KillSwitch.halt 是 fail-soft 的，永不抛
        try:
            self._kill_switch.halt(reason=full_reason)
        except Exception as exc:  # pragma: no cover
            _log_event_safe(
                self._logger,
                "abort_hook_kill_switch_halt_failed",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
                reason=full_reason,
            )
        self._halts_triggered += 1
        self._consecutive_warnings = 0  # halt 之后计数清零
        self._transition(
            "halting",
            reason=full_reason,
            breakdown={
                "total_score": report.total_score,
                "state": report.state,
                "subscores": {
                    name: sub.value for name, sub in report.subscores.items()
                },
            },
        )

    @staticmethod
    def _halt_reason_code_immediate(report: DriftReport) -> str:
        """halt_immediate 时决定更具体的 reason code。

        优先级：
        1. 如果 total ≥ 5 → score_ge_5
        2. 否则必然是某个 subscore 达到了 2.0 —— 返回 subscore_<category>_2
        """
        if report.total_score >= 5:
            return "score_ge_5"
        for name, sub in report.subscores.items():
            if sub.value >= 2.0 - 1e-9:
                return f"subscore_{name}_2"
        return "unknown"  # defensive, 不应该到达

    def _transition(
        self,
        new_state: AbortHookState,
        *,
        reason: str,
        breakdown: dict[str, Any] | None = None,
    ) -> None:
        old_state = self._state
        self._state = new_state
        self._last_state_transition_reason = reason

        event_payload: dict[str, Any] = {
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason,
            "total_score": self._last_total_score,
        }
        if breakdown:
            event_payload["breakdown"] = breakdown

        _log_event_safe(
            self._logger,
            "abort_hook_state_transition",
            **event_payload,
        )

        if self._on_event is not None:
            try:
                self._on_event(event_payload)
            except Exception as exc:  # pragma: no cover
                _log_event_safe(
                    self._logger,
                    "abort_hook_on_event_callback_failed",
                    level="warning",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # ──────────────────────────────────────────────────────────────
    # Introspection
    # ──────────────────────────────────────────────────────────────

    def snapshot(self) -> AbortHookSnapshot:
        return AbortHookSnapshot(
            enabled=self._config.enabled,
            state=self._state,
            consecutive_warning_count=self._consecutive_warnings,
            cooldown_ends_at=self._cooldown_ends_at,
            last_evaluated_at=self._last_evaluated_at,
            last_total_score=self._last_total_score,
            last_abort_hook_action=self._last_abort_hook_action,
            last_state_transition_reason=self._last_state_transition_reason,
            evaluations_total=self._evaluations_total,
            halts_triggered=self._halts_triggered,
        )

    def drift_report_dict(self) -> dict[str, Any] | None:
        """返回最近一次 DriftReport 的 JSON-serializable dict，供 HTTP 端点使用。

        如果还没有执行过评估则返回 None。
        """
        if self._last_drift_report is None:
            return None
        try:
            return self._last_drift_report.to_dict()
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────
# 内部 helpers
# ─────────────────────────────────────────────────────────────────────


def _log_event_safe(
    logger: logging.Logger,
    event: str,
    *,
    level: str = "info",
    **kwargs: Any,
) -> None:
    """log_event 的 fail-soft wrapper。

    测试环境里可能用 stdlib logging.Logger；生产环境是 aats.bootstrap.logging
    里增强过的 logger。统一走 getattr 防护，不想让一个 logging 的小 bug
    把 sidecar 推回到 transient error 重试循环。
    """
    try:
        from aats.bootstrap.logging import log_event  # lazy import 避免循环
        log_event(logger, event, level=level, **kwargs)
    except Exception:
        try:
            msg = f"{event} " + " ".join(f"{k}={v}" for k, v in kwargs.items())
            if level == "error":
                logger.error(msg)
            elif level == "warning":
                logger.warning(msg)
            else:
                logger.info(msg)
        except Exception:
            pass


__all__ = [
    "AbortHookConfig",
    "AbortHookService",
    "AbortHookSnapshot",
    "AbortHookState",
]
