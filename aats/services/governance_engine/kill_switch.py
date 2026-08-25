"""Generation-scoped, fail-closed Kill Switch for the execution boundary.

``KillSwitch`` keeps the legacy synchronous status API while adding an explicit
``RUNNING/HALTING/HALTED/RESUMING/DEGRADED`` state machine. Redis is the
cross-process and restart authority; NATS is a low-latency notification path.
The persistent state is deliberately not sufficient to authorize new risk:
Gateway/monolith must also maintain a short-lived, generation-scoped Redis
permission lease. Execution validates that lease at the final submission fence
and never renews it itself.
Only an execution or monolith instance may acknowledge ``HALTED``, and only
after it has blocked new risk-increasing submissions and drained the final
submission fence.

Risk-increasing adapters call ``risk_increasing_submission_guard`` immediately
around the irreversible exchange request. The guard validates both the local
generation and the authoritative Redis record, and fails closed when authority
is missing, invalid, or unavailable. Validated exchange-enforced reduce-only
actions may use a separately controlled bypass.

Resume is deliberately asymmetric: bootstrapped instances remain blocked until
an explicit operator path durably writes a new ``RUNNING`` generation with
``resume_authorized=true``. A restart, expired key, missed message, or legacy
running payload cannot implicitly re-enable risk-increasing trading.

Designs:
``docs/task/fs_002_kill_switch_p0_remediation_sow_2026_08_24.md`` and
``docs/task/fs_002_short_lived_trading_permission_lease_sow_2026_08_24.md``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from aats.bootstrap.logging import log_event
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_envelope
from aats.schemas.common import EventEnvelope, dump_payload_exact, new_id
from aats.storage.hot_state_store import HotStateStore, NS_SYSTEM, make_key

KILL_SWITCH_REDIS_KEY = make_key(NS_SYSTEM, "kill_switch")
"""Redis key for the kill_switch state. ``aats:hot:system:kill_switch``."""


def kill_switch_permission_key(generation: str) -> str:
    """Return the short-lived risk-increasing permission key for a generation."""
    return make_key(NS_SYSTEM, "kill_switch_permission", generation)

# Redis TTL（秒）。kill_switch 状态需跨重启持久化（I3），但不应永驻 Redis：
# 如果系统停止运行超过 30 天，旧 halt 状态应过期，重启时走 fail-safe halt
# 路径（Redis 空 + 多进程模式 → 保守 halt → 等 NATS 或人工恢复）。
# 30 天远长于任何正常维护窗口，足够安全。
_KILL_SWITCH_REDIS_TTL_SECONDS: int = 30 * 24 * 3600  # 30 days

# Persistent state above is recovery truth, not an online trading permission.
# Keep these safety constants code-owned so a deployment profile cannot silently
# stretch a control-plane partition into an unbounded trading window.
_KILL_SWITCH_PERMISSION_TTL_SECONDS: float = 15.0
_KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS: float = 5.0
_KILL_SWITCH_PERMISSION_TASK_NAME = "aats_kill_switch_permission_lease"
_KILL_SWITCH_PERMISSION_ISSUERS = frozenset({"gateway", "monolith"})

# bootstrap 时 Redis 数据新鲜度阈值（秒）。超过此阈值的 halt 状态仍会被
# 恢复（保守策略），但会 log warning 提醒运维检查。
_KILL_SWITCH_STALENESS_THRESHOLD_SECONDS: float = 48 * 3600  # 48 hours

KILL_SWITCH_EVENT_TYPE = "KillSwitchStateChanged"
"""Event envelope ``event_type`` field for kill_switch state broadcasts."""

KILL_SWITCH_SOURCE_COMPONENT = "aats.governance.kill_switch"
"""Event envelope ``source_component`` for kill_switch state broadcasts."""

KillSwitchPhase = Literal["RUNNING", "HALTING", "HALTED", "RESUMING", "DEGRADED"]


def _require_finite_positive_timestamp(value: Any, field_name: str) -> float:
    """Reject non-finite/corrupt authority clocks before state mutation."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name}_must_be_finite_and_positive")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0.0:
        raise ValueError(f"{field_name}_must_be_finite_and_positive")
    return resolved


class KillSwitchSubmissionBlocked(RuntimeError):
    """Final exchange-submission fence rejected a risk-increasing order."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KillSwitchAuthorityError(ValueError):
    """Authoritative state cannot support a safety-critical transition."""


class KillSwitch:
    """Thread-safe halt/resume switch + 跨进程同步边车（合并版）。

    Uses a single tuple assignment for atomicity — Python guarantees that
    binding a name to a new object is atomic at the bytecode level, so a
    concurrent reader of ``status()`` / ``halted`` will always see a consistent
    (halted, reason) pair.

    sidecar 依赖在 ``bootstrap`` 时绑定，``stop`` 只会清理 owner loop
    引用。频繁读取的 legacy ``_state`` 仍以单个 tuple 保持
    ``halted/reason`` 一致；完整 transition 字段则只在 owner event loop
    上执行写路径。
    """

    def __init__(self) -> None:
        # === 本地 state（始终可用，无需 bootstrap）===
        self._state: tuple[bool, str | None] = (False, None)
        self._phase: KillSwitchPhase = "RUNNING"
        self._generation: str = new_id("ksgen")
        self._set_at_ts: float = time.time()
        # 纯本地/monolith 默认保持历史兼容；多进程 bootstrap 会要求 Redis 中
        # 存在明确的 resume_authorized=true 才允许风险增加提交。
        self._resume_authorized: bool = True
        self._acknowledged_by: str | None = "monolith"
        self._last_authority_error: str | None = None
        # 风险增加订单在最终 place_order 边界持有此锁。execution halt 只有在
        # 本地先阻断并排空该锁后，才能返回 HALTED acknowledgement。
        self._submission_fence = asyncio.Lock()
        self._last_running_generation: str = self._generation

        # === sidecar 配线状态（bootstrap 后才有效）===
        self._hot_state_store: HotStateStore | None = None
        self._bus: EventBus | None = None
        self._process_role: str = "monolith"
        self._logger: logging.Logger | None = None
        # 主 loop 引用，bootstrap 时缓存。worker thread 用 run_coroutine_threadsafe 投递
        self._loop: asyncio.AbstractEventLoop | None = None
        # 本地"已经应用过的最大 set_at_ts"。乱序 NATS 事件用这个去重 + 拒绝退化
        self._last_applied_ts: float = 0.0
        # 写入去重：同一 (halted, reason) 不重复广播
        self._last_published_state: tuple[bool, str | None, str, str] | None = None
        self._last_publish_outcome: dict[str, bool | None] = {
            "redis_written": None,
            "nats_published": None,
        }
        self._fail_closed_on_authority_loss = True
        # Gateway/monolith owns the short-lived permission. The I/O lock orders
        # renewal and best-effort revocation so an in-flight renewal cannot win
        # after an awaited halt revocation in the same process.
        self._trading_permission_task: asyncio.Task[None] | None = None
        self._trading_permission_io_lock = asyncio.Lock()
        self._trading_permission_generation: str | None = None
        self._trading_permission_last_success_monotonic: float | None = None
        self._trading_permission_last_success_ts: float | None = None
        # bootstrap 是否已经成功跑过
        self._bootstrapped: bool = False
        # NATS 订阅是否成功
        self._subscribed: bool = False

    # ──────────────────────────────────────────────────────────────────
    # 本地读路径（永远可用，与 Stage 6 Slice 6.2 之前完全兼容）
    # ──────────────────────────────────────────────────────────────────

    def status(self) -> dict[str, str | bool | None]:
        halted, reason = self._state
        return {"halted": halted, "reason": reason}

    @property
    def halted(self) -> bool:
        return self._state[0]

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def phase(self) -> KillSwitchPhase:
        return self._phase

    def transition_status(self) -> dict[str, Any]:
        halted, reason = self._state
        return {
            "state": self._phase,
            "halted": halted,
            "reason": reason,
            "generation": self._generation,
            "set_at_ts": self._set_at_ts,
            "resume_authorized": self._resume_authorized,
            "enforced": self._phase == "HALTED",
            "acknowledged_by": self._acknowledged_by,
            "last_authority_error": self._last_authority_error,
        }

    # ──────────────────────────────────────────────────────────────────
    # sync 写路径（向后兼容老 API + 自动跨进程同步）
    # ──────────────────────────────────────────────────────────────────

    def halt(self, reason: str = "manual_halt") -> None:
        """同步 halt：本地立即阻断，并异步传播/排空 execution fence。"""
        generation, set_at_ts = self._begin_halt(reason=reason)
        if not self._bootstrapped:
            self._mark_halt_enforced(generation=generation)
            return
        self._dispatch_async_publish(
            halted=True,
            reason=reason,
            phase="HALTING",
            generation=generation,
            set_at_ts=set_at_ts,
            resume_authorized=False,
            finalize_halt=True,
        )

    def resume(self) -> None:
        """同步 resume 仅为纯本地兼容；已 bootstrap 时走严格 async authority。"""
        if not self._bootstrapped:
            self._apply_running(
                generation=new_id("ksgen"),
                set_at_ts=time.time(),
                acknowledged_by=self._process_role,
            )
            return
        self._phase = "RESUMING"
        self._dispatch_async_resume()

    # ──────────────────────────────────────────────────────────────────
    # async 写路径（FastAPI handler / 主 loop 内 await 用）
    # ──────────────────────────────────────────────────────────────────

    async def halt_async(
        self,
        reason: str = "manual_halt",
        *,
        generation: str | None = None,
        set_at_ts: float | None = None,
    ) -> dict[str, Any]:
        """阻断本地风险增加提交，并仅由 execution/monolith 确认 enforcement。"""
        resolved_generation, resolved_ts = self._begin_halt(
            reason=reason,
            generation=generation,
            set_at_ts=set_at_ts,
        )
        propagation = await self._publish(
            halted=True,
            reason=reason,
            phase="HALTING",
            generation=resolved_generation,
            set_at_ts=resolved_ts,
            resume_authorized=False,
        )
        if self._is_execution_authority:
            await self._drain_submission_fence(generation=resolved_generation)
            final_ts = time.time()
            self._set_at_ts = final_ts
            final_propagation = await self._publish(
                halted=True,
                reason=reason,
                phase="HALTED",
                generation=resolved_generation,
                set_at_ts=final_ts,
                resume_authorized=False,
            )
            propagation = self._merge_propagation(propagation, final_propagation)
        result = self.transition_status()
        result["propagation"] = propagation
        return result

    async def resume_async(self) -> dict[str, Any]:
        """显式恢复；权威 Redis 写成功前始终保持阻断。"""
        if self._phase == "RUNNING" and not self.halted and self._resume_authorized:
            if self._permission_lease_required and self._is_permission_lease_owner:
                try:
                    await self._renew_trading_permission_once()
                except KillSwitchAuthorityError:
                    self._latch_degraded("kill_switch_permission_lease_write_failed")
                    raise
            result = self.transition_status()
            result["propagation"] = dict(self._last_publish_outcome)
            return result

        previous_reason = self._state[1]
        generation = new_id("ksgen")
        set_at_ts = time.time()
        self._phase = "RESUMING"
        self._state = (True, previous_reason or "resume_pending")
        self._generation = generation
        self._set_at_ts = set_at_ts
        self._resume_authorized = False
        payload = self._transition_payload(
            halted=False,
            reason=None,
            phase="RUNNING",
            generation=generation,
            set_at_ts=set_at_ts,
            resume_authorized=True,
        )
        redis_written = await self._authoritative_redis_set(payload)
        if not redis_written:
            self._latch_degraded("kill_switch_resume_authority_write_failed")
            raise KillSwitchAuthorityError("kill_switch_resume_authority_write_failed")

        self._apply_running(
            generation=generation,
            set_at_ts=set_at_ts,
            acknowledged_by=self._process_role,
        )
        if self._permission_lease_required and self._is_permission_lease_owner:
            try:
                await self._renew_trading_permission_once()
            except KillSwitchAuthorityError:
                self._latch_degraded("kill_switch_permission_lease_write_failed")
                raise
        nats_published = await self._best_effort_nats_broadcast(payload)
        outcome = {
            "redis_written": True,
            "nats_published": nats_published,
        }
        self._last_published_state = (False, None, "RUNNING", generation)
        self._last_publish_outcome = outcome
        result = self.transition_status()
        result["propagation"] = outcome
        return result

    async def activate_trading_permission_for_authorized_generation(
        self,
        *,
        generation: str,
    ) -> None:
        """Gateway-side completion step for a proxied execution resume.

        The execution process performs reconciliation and writes the durable
        RUNNING generation. Before the Gateway reports that proxied operation
        as successful, it re-reads that exact authority, adopts it locally and
        establishes the permission lease. This avoids a nominally successful
        resume followed by a lease-less submission window.
        """
        if not self._permission_lease_required or not self._is_permission_lease_owner:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_activation_forbidden_for_role"
            )
        expected_generation = str(generation).strip()
        if not expected_generation:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_activation_generation_missing"
            )
        store = self._hot_state_store
        if store is None:
            self._latch_degraded("kill_switch_permission_authority_unavailable")
            raise KillSwitchAuthorityError(
                "kill_switch_permission_authority_unavailable"
            )
        try:
            stored = await store.get(KILL_SWITCH_REDIS_KEY)
        except Exception as exc:
            self._latch_degraded("kill_switch_permission_authority_unavailable")
            raise KillSwitchAuthorityError(
                "kill_switch_permission_authority_unavailable"
            ) from exc
        if not isinstance(stored, dict):
            self._latch_degraded("kill_switch_permission_authority_missing")
            raise KillSwitchAuthorityError(
                "kill_switch_permission_authority_missing"
            )
        try:
            halted = stored["halted"]
            authority_state = str(stored["state"])
            authority_generation = str(stored["generation"])
            set_at_ts = _require_finite_positive_timestamp(
                stored["set_at_ts"],
                "kill_switch_set_at_ts",
            )
            resume_authorized = stored.get("resume_authorized", False)
        except (KeyError, TypeError, ValueError) as exc:
            self._latch_degraded("kill_switch_permission_authority_invalid")
            raise KillSwitchAuthorityError(
                "kill_switch_permission_authority_invalid"
            ) from exc
        if not (
            halted is False
            and authority_state == "RUNNING"
            and resume_authorized is True
            and authority_generation == expected_generation
        ):
            self._latch_degraded(
                "kill_switch_permission_activation_authority_mismatch"
            )
            raise KillSwitchAuthorityError(
                "kill_switch_permission_activation_authority_mismatch"
            )
        self._apply_running(
            generation=authority_generation,
            set_at_ts=set_at_ts,
            acknowledged_by=str(stored.get("source_role") or "authoritative_store"),
        )
        try:
            await self._renew_trading_permission_once()
        except Exception as exc:
            self._latch_degraded("kill_switch_permission_lease_write_failed")
            if isinstance(exc, KillSwitchAuthorityError):
                raise
            raise KillSwitchAuthorityError(
                "kill_switch_permission_lease_write_failed"
            ) from exc

    @property
    def _is_execution_authority(self) -> bool:
        return self._process_role in {"execution", "monolith"}

    @property
    def _is_permission_lease_owner(self) -> bool:
        return self._process_role in _KILL_SWITCH_PERMISSION_ISSUERS

    @property
    def _permission_lease_required(self) -> bool:
        return self._bootstrapped and self._fail_closed_on_authority_loss

    @property
    def trading_permission_background_task(self) -> asyncio.Task[None] | None:
        """Service-owned task registered by ``ApplicationRuntime`` as critical."""
        return self._trading_permission_task

    def _begin_halt(
        self,
        *,
        reason: str,
        generation: str | None = None,
        set_at_ts: float | None = None,
    ) -> tuple[str, float]:
        explicit_set_at_ts = (
            None
            if set_at_ts is None
            else _require_finite_positive_timestamp(
                set_at_ts,
                "kill_switch_set_at_ts",
            )
        )
        if self.halted and self._phase in {"HALTING", "HALTED"}:
            resolved_generation = generation or self._generation
            # 相同/重复 halt 必须幂等；显式传入更新的远端 generation 才推进 fence。
            if generation is None or generation == self._generation:
                self._state = (True, reason)
                return self._generation, self._set_at_ts
        else:
            resolved_generation = generation or new_id("ksgen")
        resolved_ts = (
            time.time()
            if explicit_set_at_ts is None
            else explicit_set_at_ts
        )
        self._state = (True, reason)
        self._phase = "HALTING"
        self._generation = resolved_generation
        self._set_at_ts = resolved_ts
        self._resume_authorized = False
        self._acknowledged_by = None
        self._last_authority_error = None
        self._last_applied_ts = max(self._last_applied_ts, resolved_ts)
        return resolved_generation, resolved_ts

    def _mark_halt_enforced(self, *, generation: str) -> None:
        if generation != self._generation:
            return
        self._phase = "HALTED"
        self._acknowledged_by = self._process_role

    async def _drain_submission_fence(self, *, generation: str) -> None:
        async with self._submission_fence:
            self._mark_halt_enforced(generation=generation)
        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_halt_enforced",
                level="warning",
                process_role=self._process_role,
                generation=generation,
                reason=self._state[1],
            )

    def _apply_running(
        self,
        *,
        generation: str,
        set_at_ts: float,
        acknowledged_by: str | None,
    ) -> None:
        set_at_ts = _require_finite_positive_timestamp(
            set_at_ts,
            "kill_switch_set_at_ts",
        )
        self._state = (False, None)
        self._phase = "RUNNING"
        self._generation = generation
        self._last_running_generation = generation
        self._set_at_ts = set_at_ts
        self._resume_authorized = True
        self._acknowledged_by = acknowledged_by
        self._last_authority_error = None
        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)

    def _latch_degraded(self, reason: str) -> None:
        self._state = (True, reason)
        self._phase = "DEGRADED"
        self._resume_authorized = False
        self._acknowledged_by = None
        self._last_authority_error = reason

    @staticmethod
    def _merge_propagation(
        first: dict[str, bool | None],
        second: dict[str, bool | None],
    ) -> dict[str, bool | None]:
        return {
            key: (
                bool(first.get(key)) or bool(second.get(key))
                if first.get(key) is not None or second.get(key) is not None
                else None
            )
            for key in {"redis_written", "nats_published"}
        }

    def _transition_payload(
        self,
        *,
        halted: bool,
        reason: str | None,
        phase: KillSwitchPhase,
        generation: str,
        set_at_ts: float,
        resume_authorized: bool,
    ) -> dict[str, Any]:
        return {
            "halted": halted,
            "reason": reason if halted else None,
            "state": phase,
            "generation": generation,
            "set_at_ts": set_at_ts,
            "source_role": self._process_role,
            "resume_authorized": resume_authorized,
        }

    @asynccontextmanager
    async def risk_increasing_submission_guard(
        self,
        *,
        expected_generation: str,
    ) -> AsyncIterator[None]:
        """Final execution fence held through the irreversible exchange call."""
        async with self._submission_fence:
            if self.halted:
                raise KillSwitchSubmissionBlocked("kill_switch_active")
            if expected_generation != self._generation:
                raise KillSwitchSubmissionBlocked("kill_switch_generation_changed")
            if self._bootstrapped:
                await self._refresh_authoritative_state_for_submission()
                if self.halted:
                    raise KillSwitchSubmissionBlocked("kill_switch_active")
                if expected_generation != self._generation:
                    raise KillSwitchSubmissionBlocked("kill_switch_generation_changed")
                if self._permission_lease_required:
                    await self._require_trading_permission(
                        expected_generation=expected_generation,
                    )
            yield

    async def _require_trading_permission(self, *, expected_generation: str) -> None:
        """Fail closed unless Redis holds the current generation's live lease."""
        store = self._hot_state_store
        if store is None:
            reason = "kill_switch_permission_unavailable"
            self._latch_degraded(reason)
            raise KillSwitchSubmissionBlocked(reason)
        key = kill_switch_permission_key(expected_generation)
        try:
            stored = await store.get(key)
        except Exception as exc:
            reason = "kill_switch_permission_unavailable"
            self._latch_degraded(reason)
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_permission_read_failed",
                    level="critical",
                    process_role=self._process_role,
                    generation=expected_generation,
                    error_type=type(exc).__name__,
                )
            raise KillSwitchSubmissionBlocked(reason) from exc
        if stored is None:
            reason = "kill_switch_permission_missing"
            self._latch_degraded(reason)
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_permission_missing",
                    level="critical",
                    process_role=self._process_role,
                    generation=expected_generation,
                )
            raise KillSwitchSubmissionBlocked(reason)
        if not isinstance(stored, dict):
            reason = "kill_switch_permission_invalid"
            self._latch_degraded(reason)
            raise KillSwitchSubmissionBlocked(reason)
        try:
            generation = str(stored["generation"])
            issued_by = str(stored["issued_by"])
            _require_finite_positive_timestamp(
                stored["issued_at"],
                "kill_switch_permission_issued_at",
            )
        except (KeyError, TypeError, ValueError) as exc:
            reason = "kill_switch_permission_invalid"
            self._latch_degraded(reason)
            raise KillSwitchSubmissionBlocked(reason) from exc
        if generation != expected_generation:
            reason = "kill_switch_permission_generation_mismatch"
            self._latch_degraded(reason)
            raise KillSwitchSubmissionBlocked(reason)
        if issued_by not in _KILL_SWITCH_PERMISSION_ISSUERS:
            reason = "kill_switch_permission_invalid"
            self._latch_degraded(reason)
            raise KillSwitchSubmissionBlocked(reason)

    async def _refresh_authoritative_state_for_submission(self) -> None:
        store = self._hot_state_store
        if store is None:
            self._latch_degraded("kill_switch_authority_unavailable")
            raise KillSwitchSubmissionBlocked("kill_switch_authority_unavailable")
        try:
            stored = await store.get(KILL_SWITCH_REDIS_KEY)
        except Exception as exc:
            self._latch_degraded("kill_switch_authority_unavailable")
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_submission_authority_read_failed",
                    level="critical",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            raise KillSwitchSubmissionBlocked("kill_switch_authority_unavailable") from exc
        if not isinstance(stored, dict):
            self._latch_degraded("kill_switch_authority_missing")
            raise KillSwitchSubmissionBlocked("kill_switch_authority_missing")
        try:
            halted = bool(stored["halted"])
            generation = str(stored["generation"])
            state = str(stored["state"])
            set_at_ts = _require_finite_positive_timestamp(
                stored["set_at_ts"],
                "kill_switch_set_at_ts",
            )
            resume_authorized = bool(stored.get("resume_authorized", False))
        except (KeyError, TypeError, ValueError) as exc:
            self._latch_degraded("kill_switch_authority_invalid")
            raise KillSwitchSubmissionBlocked("kill_switch_authority_invalid") from exc

        if halted or state != "RUNNING" or not resume_authorized:
            self._state = (True, str(stored.get("reason") or "authoritative_halt"))
            self._phase = "HALTED" if state == "HALTED" else "HALTING"
            self._generation = generation
            self._set_at_ts = set_at_ts
            self._resume_authorized = False
            self._acknowledged_by = str(stored.get("source_role") or "authoritative_store")
            self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
            return
        if generation != self._generation:
            self._apply_running(
                generation=generation,
                set_at_ts=set_at_ts,
                acknowledged_by=str(stored.get("source_role") or "authoritative_store"),
            )

    # ──────────────────────────────────────────────────────────────────
    # 启动 / 关闭
    # ──────────────────────────────────────────────────────────────────

    async def start_trading_permission_lease(self) -> asyncio.Task[None] | None:
        """Start the control-plane lease owner task after peer readiness.

        Execution and non-strict research runtimes intentionally return
        ``None``: they must never mint live trading permission.
        """
        if not self._bootstrapped:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_start_requires_bootstrap"
            )
        if not self._permission_lease_required or not self._is_permission_lease_owner:
            return None
        if self._trading_permission_task is not None:
            return self._trading_permission_task
        if self._running_permission_eligible:
            await self._renew_trading_permission_once()
        task = asyncio.create_task(
            self._trading_permission_lease_loop(),
            name=_KILL_SWITCH_PERMISSION_TASK_NAME,
        )
        self._trading_permission_task = task
        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_permission_lease_started",
                process_role=self._process_role,
                generation=self._generation,
                ttl_seconds=_KILL_SWITCH_PERMISSION_TTL_SECONDS,
                renew_interval_seconds=_KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS,
            )
        return task

    @property
    def _running_permission_eligible(self) -> bool:
        return bool(
            self._permission_lease_required
            and self._is_permission_lease_owner
            and not self.halted
            and self._phase == "RUNNING"
            and self._resume_authorized
        )

    async def _trading_permission_lease_loop(self) -> None:
        """Renew until stopped; surface a critical failure no later than expiry."""
        wait_seconds = _KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS
        while True:
            try:
                if self._running_permission_eligible:
                    await self._renew_trading_permission_once()
                else:
                    await self._best_effort_revoke_trading_permission(
                        generation=self._last_running_generation,
                    )
                wait_seconds = _KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = time.monotonic()
                last_success = (
                    self._trading_permission_last_success_monotonic
                    if self._trading_permission_generation == self._generation
                    else None
                )
                deadline = (
                    last_success + _KILL_SWITCH_PERMISSION_TTL_SECONDS
                    if last_success is not None
                    else now
                )
                if self._logger is not None:
                    log_event(
                        self._logger,
                        "kill_switch_permission_renew_failed",
                        level="critical",
                        process_role=self._process_role,
                        generation=self._generation,
                        error_type=type(exc).__name__,
                        seconds_until_expiry=round(max(0.0, deadline - now), 3),
                    )
                if now >= deadline:
                    self._latch_degraded(
                        "kill_switch_permission_lease_renewal_expired"
                    )
                    raise KillSwitchAuthorityError(
                        "kill_switch_permission_lease_renewal_expired"
                    ) from exc
                wait_seconds = min(
                    _KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS,
                    max(0.001, deadline - now),
                )
            await asyncio.sleep(wait_seconds)

    async def _renew_trading_permission_once(self) -> bool:
        """Validate persistent authority then refresh the matching Redis TTL."""
        if not self._is_permission_lease_owner:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_renew_forbidden_for_role"
            )
        store = self._hot_state_store
        if store is None:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_authority_unavailable"
            )
        if not self._running_permission_eligible:
            await self._best_effort_revoke_trading_permission(
                generation=self._last_running_generation,
            )
            return False

        generation = self._generation
        async with self._trading_permission_io_lock:
            if not self._running_permission_eligible or generation != self._generation:
                await self._delete_trading_permission_locked(generation)
                return False
            try:
                stored = await store.get(KILL_SWITCH_REDIS_KEY)
            except Exception as exc:
                raise KillSwitchAuthorityError(
                    "kill_switch_permission_authority_unavailable"
                ) from exc
            if not isinstance(stored, dict):
                raise KillSwitchAuthorityError(
                    "kill_switch_permission_authority_missing"
                )
            try:
                halted = stored["halted"]
                authority_state = str(stored["state"])
                authority_generation = str(stored["generation"])
                set_at_ts = _require_finite_positive_timestamp(
                    stored["set_at_ts"],
                    "kill_switch_set_at_ts",
                )
                resume_authorized = stored.get("resume_authorized", False)
            except (KeyError, TypeError, ValueError) as exc:
                raise KillSwitchAuthorityError(
                    "kill_switch_permission_authority_invalid"
                ) from exc

            if (
                halted is not False
                or authority_state != "RUNNING"
                or resume_authorized is not True
            ):
                self._state = (
                    True,
                    str(stored.get("reason") or "authoritative_halt"),
                )
                self._phase = "HALTED" if authority_state == "HALTED" else "HALTING"
                self._generation = authority_generation
                self._set_at_ts = set_at_ts
                self._resume_authorized = False
                self._acknowledged_by = str(
                    stored.get("source_role") or "authoritative_store"
                )
                self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
                await self._delete_trading_permission_locked(generation)
                return False
            if authority_generation != generation:
                await self._delete_trading_permission_locked(generation)
                raise KillSwitchAuthorityError(
                    "kill_switch_permission_authority_generation_mismatch"
                )
            # Halt changes local state synchronously. Recheck immediately before
            # SET so the normal awaited halt path orders a subsequent revoke
            # after any renewal already holding this lock.
            if not self._running_permission_eligible or generation != self._generation:
                await self._delete_trading_permission_locked(generation)
                return False

            previous_generation = self._trading_permission_generation
            if previous_generation is not None and previous_generation != generation:
                await self._delete_trading_permission_locked(previous_generation)
            issued_at = time.time()
            try:
                await store.set(
                    kill_switch_permission_key(generation),
                    {
                        "generation": generation,
                        "issued_by": self._process_role,
                        "issued_at": issued_at,
                    },
                    ttl_seconds=_KILL_SWITCH_PERMISSION_TTL_SECONDS,
                )
            except Exception as exc:
                raise KillSwitchAuthorityError(
                    "kill_switch_permission_lease_write_failed"
                ) from exc
            self._trading_permission_generation = generation
            self._trading_permission_last_success_monotonic = time.monotonic()
            self._trading_permission_last_success_ts = issued_at
        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_permission_renewed",
                level="debug",
                process_role=self._process_role,
                generation=generation,
                ttl_seconds=_KILL_SWITCH_PERMISSION_TTL_SECONDS,
            )
        return True

    async def _delete_trading_permission_locked(self, generation: str) -> None:
        store = self._hot_state_store
        if store is None:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_authority_unavailable"
            )
        try:
            await store.delete(kill_switch_permission_key(generation))
        except Exception as exc:
            raise KillSwitchAuthorityError(
                "kill_switch_permission_lease_revoke_failed"
            ) from exc
        if self._trading_permission_generation == generation:
            self._trading_permission_generation = None
            self._trading_permission_last_success_monotonic = None
            self._trading_permission_last_success_ts = None

    async def _best_effort_revoke_trading_permission(
        self,
        *,
        generation: str | None = None,
    ) -> bool | None:
        store = self._hot_state_store
        if store is None:
            return None
        generations = {
            item
            for item in (
                generation,
                self._last_running_generation,
                self._trading_permission_generation,
            )
            if item
        }
        if not generations:
            return True
        try:
            async with self._trading_permission_io_lock:
                for item in generations:
                    await self._delete_trading_permission_locked(item)
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_permission_revoke_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                )
            return False
        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_permission_revoked",
                process_role=self._process_role,
                generations=sorted(generations),
            )
        return True

    async def stop_trading_permission_lease(self) -> None:
        """Stop renewal and revoke the owner's current ephemeral permission."""
        task = self._trading_permission_task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if self._logger is not None:
                    log_event(
                        self._logger,
                        "kill_switch_permission_task_stop_observed_failure",
                        level="warning",
                        process_role=self._process_role,
                        error_type=type(exc).__name__,
                    )
        self._trading_permission_task = None
        if self._is_permission_lease_owner:
            await self._best_effort_revoke_trading_permission(
                generation=self._last_running_generation,
            )

    async def bootstrap(
        self,
        *,
        hot_state_store: HotStateStore,
        bus: EventBus,
        process_role: str,
        logger: logging.Logger,
        fail_closed_on_authority_loss: bool = True,
    ) -> None:
        """启动期 hydration：

        1. 缓存 sidecar deps（``_hot_state_store`` / ``_bus`` / ``_process_role`` /
           ``_logger`` / ``_loop``）
        2. 从 Redis 读 ``aats:hot:system:kill_switch``
        3. 如果存在且 ``halted=True``，更新本地 ``_state``
        4. 订阅 NATS ``system.kill_switch_state`` topic

        ⚠️ 任何步骤的失败都不能阻止 build_runtime 完成。
        """
        self._hot_state_store = hot_state_store
        self._bus = bus
        self._process_role = process_role
        self._logger = logger
        self._loop = asyncio.get_running_loop()
        self._fail_closed_on_authority_loss = fail_closed_on_authority_loss

        # Step 2：从 Redis 读
        _redis_read_failed = False
        try:
            stored: Any = await hot_state_store.get(KILL_SWITCH_REDIS_KEY)
        except Exception as exc:
            log_event(
                logger,
                "kill_switch_bootstrap_redis_failed",
                level="warning",
                process_role=process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            stored = None
            _redis_read_failed = True

        # ── Fail-safe：具有真实交易所提交能力时，Redis 不可用则默认 halt ───
        # Redis 是跨进程与重启的权威记录。无法读取时不能确定是否已被
        # halt，因此资金风险环境锁存 DEGRADED。NATS 只能提供低延迟通知；
        # 它不能单独解除阻断，resume 事件还必须与 Redis 中的显式授权
        # generation 一致。不具有 exchange submission 能力的 paper/backtest
        # runtime 可以通过 ``fail_closed_on_authority_loss=False`` 保持隔离兼容。
        if _redis_read_failed and self._fail_closed_on_authority_loss:
            self._latch_degraded("redis_unavailable_fail_safe")
            log_event(
                logger,
                "kill_switch_bootstrap_fail_safe_halt",
                level="error",
                process_role=process_role,
                reason="redis_unavailable_fail_safe",
                hint="系统将保持 halt；Redis 恢复后仍需完整 RUNNING 授权或显式 operator resume",
            )

        if isinstance(stored, dict):
            try:
                halted = bool(stored.get("halted", False))
                reason = stored.get("reason")
                set_at_ts = _require_finite_positive_timestamp(
                    stored.get("set_at_ts"),
                    "kill_switch_set_at_ts",
                )
                source_role = stored.get("source_role")
                generation_value = stored.get("generation")
                generation = (
                    str(generation_value)
                    if generation_value not in {None, ""}
                    else f"legacy_{set_at_ts:.6f}"
                )
                state = str(stored.get("state") or ("HALTED" if halted else "RUNNING"))
                resume_authorized = bool(stored.get("resume_authorized", False))
                if halted:
                    self._state = (True, str(reason or "bootstrap_from_redis"))
                    self._phase = "HALTED" if self._is_execution_authority else "HALTING"
                    self._generation = generation
                    self._set_at_ts = set_at_ts
                    self._resume_authorized = False
                    self._acknowledged_by = str(source_role or "redis")
                elif state == "RUNNING" and resume_authorized and generation_value not in {None, ""}:
                    self._apply_running(
                        generation=generation,
                        set_at_ts=set_at_ts,
                        acknowledged_by=str(source_role or "redis"),
                    )
                elif self._fail_closed_on_authority_loss:
                    self._latch_degraded("kill_switch_resume_authority_missing")
                self._last_applied_ts = set_at_ts

                # 新鲜度检查：数据超过阈值时 log warning 提醒运维。
                # 仍然正常 hydrate（保守：宁可被旧 halt 卡住也不漏放），
                # 但运维应检查是否需要手动 resume。
                if set_at_ts > 0:
                    age_seconds = time.time() - set_at_ts
                    if age_seconds > _KILL_SWITCH_STALENESS_THRESHOLD_SECONDS:
                        log_event(
                            logger,
                            "kill_switch_bootstrap_stale_state",
                            level="warning",
                            process_role=process_role,
                            halted=halted,
                            reason=reason,
                            set_at_ts=set_at_ts,
                            age_hours=round(age_seconds / 3600, 1),
                            threshold_hours=round(
                                _KILL_SWITCH_STALENESS_THRESHOLD_SECONDS / 3600, 1,
                            ),
                            hint="Redis 中的 kill_switch 状态超过新鲜度阈值，请检查是否需要手动 resume",
                        )

                log_event(
                    logger,
                    "kill_switch_bootstrap_hydrated",
                    process_role=process_role,
                    halted=halted,
                    reason=reason,
                    set_at_ts=set_at_ts,
                    source_role=source_role,
                    state=self._phase,
                    generation=self._generation,
                    resume_authorized=self._resume_authorized,
                )
            except Exception as exc:
                if self._fail_closed_on_authority_loss:
                    self._latch_degraded("kill_switch_bootstrap_parse_failed")
                log_event(
                    logger,
                    "kill_switch_bootstrap_parse_failed",
                    level="warning",
                    process_role=process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        else:
            if not _redis_read_failed and self._fail_closed_on_authority_loss:
                self._latch_degraded("kill_switch_authority_missing")
            log_event(
                logger,
                "kill_switch_bootstrap_empty",
                level="error" if self._fail_closed_on_authority_loss else "info",
                process_role=process_role,
                fail_closed=self.halted,
            )

        # Step 4：订阅 NATS（即便上面失败也要订阅，订阅失败也不抛）
        try:
            await bus.subscribe(topics.KILL_SWITCH_STATE, self._handle_remote_event)
            self._subscribed = True
            log_event(
                logger,
                "kill_switch_subscribed",
                process_role=process_role,
                topic=topics.KILL_SWITCH_STATE,
            )
        except Exception as exc:
            log_event(
                logger,
                "kill_switch_subscribe_failed",
                level="warning",
                process_role=process_role,
                error_type=type(exc).__name__,
                error=str(exc),
            )

        self._bootstrapped = True

    async def stop(self) -> None:
        """Stop lease renewal; persistent authority remains untouched.

        EventBus 当前不支持 unsubscribe。关闭不改长期 kill-switch state，但
        Gateway/monolith 会删除短时 permission；删除失败仍由 Redis TTL 收敛。
        """
        await self.stop_trading_permission_lease()
        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_stopped",
                process_role=self._process_role,
                subscribed=self._subscribed,
                last_applied_ts=self._last_applied_ts,
            )
        self._loop = None

    # ──────────────────────────────────────────────────────────────────
    # 内部：sync 写路径的异步分发
    # ──────────────────────────────────────────────────────────────────

    def _dispatch_async_publish(
        self,
        *,
        halted: bool,
        reason: str | None,
        phase: KillSwitchPhase,
        generation: str,
        set_at_ts: float,
        resume_authorized: bool,
        finalize_halt: bool,
    ) -> None:
        """Schedule a generation-scoped transition from the legacy sync API."""
        bus = self._bus
        loop = self._loop
        if bus is None or loop is None or loop.is_closed():
            return
        if not loop.is_running():
            return

        self._last_applied_ts = max(self._last_applied_ts, set_at_ts)
        coro = self._publish_and_maybe_finalize_halt(
            halted=halted,
            reason=reason,
            phase=phase,
            generation=generation,
            set_at_ts=set_at_ts,
            resume_authorized=resume_authorized,
            finalize_halt=finalize_halt,
        )

        # 判断当前线程是否为主 loop 线程
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            # 主 loop 线程内：fire-and-forget
            try:
                loop.create_task(coro, name="kill_switch_publish")
            except Exception as exc:  # pragma: no cover
                if self._logger is not None:
                    log_event(
                        self._logger,
                        "kill_switch_dispatch_create_task_failed",
                        level="warning",
                        process_role=self._process_role,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                coro.close()
            return

        # 非 loop 线程：投递到主 loop 并等待
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_dispatch_submit_failed",
                    level="warning",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

            coro.close()
            return
        try:
            future.result(timeout=2.0)
        except concurrent.futures.TimeoutError:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_dispatch_timeout",
                    level="warning",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    timeout=2.0,
                )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_dispatch_partial",
                    level="warning",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def _dispatch_async_resume(self) -> None:
        """Run strict resume on the bootstrapped owner loop without relaxing locally."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            self._latch_degraded("kill_switch_resume_dispatch_unavailable")
            return
        coro = self.resume_async()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            try:
                task = loop.create_task(coro, name="kill_switch_resume")
                task.add_done_callback(self._consume_resume_task_result)
            except Exception:
                coro.close()
                self._latch_degraded("kill_switch_resume_dispatch_failed")
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.result(timeout=2.0)
        except concurrent.futures.TimeoutError:
            self._latch_degraded("kill_switch_resume_dispatch_timeout")
        except Exception:
            self._latch_degraded("kill_switch_resume_dispatch_failed")

    def _consume_resume_task_result(self, task: asyncio.Task[dict[str, Any]]) -> None:
        """Consume/log a fire-and-forget strict-resume failure on the owner loop."""
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_resume_async_failed",
                    level="error",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    # ──────────────────────────────────────────────────────────────────
    # 内部：跨进程广播主体（async）
    # ──────────────────────────────────────────────────────────────────

    async def _publish_and_maybe_finalize_halt(
        self,
        *,
        halted: bool,
        reason: str | None,
        phase: KillSwitchPhase,
        generation: str,
        set_at_ts: float,
        resume_authorized: bool,
        finalize_halt: bool,
    ) -> dict[str, bool | None]:
        outcome = await self._publish(
            halted=halted,
            reason=reason,
            phase=phase,
            generation=generation,
            set_at_ts=set_at_ts,
            resume_authorized=resume_authorized,
        )
        if finalize_halt and self._is_execution_authority:
            await self._drain_submission_fence(generation=generation)
            final_ts = time.time()
            self._set_at_ts = final_ts
            final_outcome = await self._publish(
                halted=True,
                reason=reason,
                phase="HALTED",
                generation=generation,
                set_at_ts=final_ts,
                resume_authorized=False,
            )
            return self._merge_propagation(outcome, final_outcome)
        return outcome

    async def _publish(
        self,
        *,
        halted: bool,
        reason: str | None,
        phase: KillSwitchPhase,
        generation: str,
        set_at_ts: float,
        resume_authorized: bool,
    ) -> dict[str, bool | None]:
        """Publish one generation-scoped transition and report each transport."""
        if halted:
            # _begin_halt has already blocked this process synchronously. Revoke
            # the preceding RUNNING generation before either transport write;
            # a failed delete still converges through the server-side TTL.
            await self._best_effort_revoke_trading_permission(
                generation=self._last_running_generation,
            )
        new_state = (halted, reason if halted else None, phase, generation)
        if self._last_published_state == new_state:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_publish_skipped_dedup",
                    process_role=self._process_role,
                    halted=halted,
                    reason=reason,
                    state=phase,
                    generation=generation,
                )
            return dict(self._last_publish_outcome)
        self._last_published_state = new_state

        payload = self._transition_payload(
            halted=halted,
            reason=reason,
            phase=phase,
            generation=generation,
            set_at_ts=set_at_ts,
            resume_authorized=resume_authorized,
        )
        outcome = {
            "redis_written": await self._best_effort_redis_set(payload),
            "nats_published": await self._best_effort_nats_broadcast(payload),
        }
        self._last_publish_outcome = outcome

        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_published",
                process_role=self._process_role,
                halted=halted,
                reason=reason,
                set_at_ts=set_at_ts,
                state=phase,
                generation=generation,
                **outcome,
            )
        return outcome

    async def _authoritative_redis_set(self, payload: dict[str, Any]) -> bool:
        if self._hot_state_store is None:
            return False
        try:
            await self._hot_state_store.set(
                KILL_SWITCH_REDIS_KEY,
                payload,
                ttl_seconds=_KILL_SWITCH_REDIS_TTL_SECONDS,
            )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_authority_set_failed",
                    level="critical",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            return False
        return True

    async def _best_effort_redis_set(self, payload: dict[str, Any]) -> bool | None:
        if self._hot_state_store is None:
            return None
        try:
            await self._hot_state_store.set(
                KILL_SWITCH_REDIS_KEY,
                payload,
                ttl_seconds=_KILL_SWITCH_REDIS_TTL_SECONDS,
            )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_redis_set_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            return False
        return True

    async def _best_effort_nats_broadcast(self, payload: dict[str, Any]) -> bool | None:
        if self._bus is None:
            return None
        try:
            envelope = EventEnvelope(
                event_type=KILL_SWITCH_EVENT_TYPE,
                source_component=KILL_SWITCH_SOURCE_COMPONENT,
                topic=topics.KILL_SWITCH_STATE,
                key=self._process_role,
                payload=dump_payload_exact(payload),
            )
            await self._bus.publish(
                topic=topics.KILL_SWITCH_STATE,
                key=self._process_role,
                payload=envelope.model_dump(mode="json"),
            )
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_nats_publish_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            return False
        return True

    # ──────────────────────────────────────────────────────────────────
    # NATS 远端事件接收
    # ──────────────────────────────────────────────────────────────────

    async def _handle_remote_event(self, message: dict[str, Any]) -> None:
        """订阅 ``system.kill_switch_state`` 后的回调。

        - 校验 ``set_at_ts > self._last_applied_ts``，旧事件忽略（I6）
        - 同一 set_at_ts 去重（idempotent）
        - 来自自己进程的事件忽略（避免回环改本地 cache）
        - apply 失败不抛（订阅 handler 异常会让 NATS 客户端 nak / log）
        """
        try:
            envelope = parse_envelope(message)
            payload = envelope.payload or {}
            set_at_ts = _require_finite_positive_timestamp(
                payload.get("set_at_ts"),
                "kill_switch_set_at_ts",
            )
            halted = bool(payload.get("halted", False))
            reason = payload.get("reason")
            source_role = payload.get("source_role")
            generation = str(payload.get("generation") or f"legacy_{set_at_ts:.6f}")
            phase_value = str(payload.get("state") or ("HALTED" if halted else "RUNNING"))
            if phase_value not in {"RUNNING", "HALTING", "HALTED", "RESUMING", "DEGRADED"}:
                raise ValueError(f"invalid kill switch state: {phase_value}")
            phase: KillSwitchPhase = phase_value  # type: ignore[assignment]
            resume_authorized = bool(payload.get("resume_authorized", False))
        except Exception as exc:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_remote_parse_failed",
                    level="warning",
                    process_role=self._process_role,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            return

        # 自己广播的回环事件：本地早已应用，跳过
        if source_role == self._process_role:
            return

        # set_at_ts 单调性：旧事件不允许退化本地 cache
        if set_at_ts <= self._last_applied_ts:
            if self._logger is not None:
                log_event(
                    self._logger,
                    "kill_switch_remote_skipped_stale",
                    process_role=self._process_role,
                    set_at_ts=set_at_ts,
                    last_applied_ts=self._last_applied_ts,
                    source_role=source_role,
                )
            return

        applied_ts = set_at_ts
        applied_phase = phase
        if halted:
            self._begin_halt(
                reason=str(reason or "remote_halt"),
                generation=generation,
                set_at_ts=set_at_ts,
            )
            if phase == "HALTED":
                self._phase = "HALTED"
                self._acknowledged_by = str(source_role or "remote")
            elif self._is_execution_authority:
                await self._drain_submission_fence(generation=generation)
                final_ts = time.time()
                self._set_at_ts = final_ts
                await self._publish(
                    halted=True,
                    reason=str(reason or "remote_halt"),
                    phase="HALTED",
                    generation=generation,
                    set_at_ts=final_ts,
                    resume_authorized=False,
                )
                applied_ts = final_ts
                applied_phase = "HALTED"
        else:
            if phase != "RUNNING" or not resume_authorized:
                self._latch_degraded("kill_switch_remote_resume_unauthorized")
                return
            if not await self._authoritative_resume_matches(generation=generation):
                self._latch_degraded("kill_switch_remote_resume_authority_mismatch")
                return
            self._apply_running(
                generation=generation,
                set_at_ts=set_at_ts,
                acknowledged_by=str(source_role or "remote"),
            )
        self._last_applied_ts = max(self._last_applied_ts, applied_ts)
        # 同步去重 marker：远端最新状态等同于本地最近一次广播状态，避免下次本进程
        # 写时被错误去重
        self._last_published_state = (
            halted,
            reason if halted else None,
            applied_phase,
            generation,
        )

        if self._logger is not None:
            log_event(
                self._logger,
                "kill_switch_remote_applied",
                process_role=self._process_role,
                halted=halted,
                reason=reason,
                set_at_ts=set_at_ts,
                source_role=source_role,
                state=self._phase,
                generation=generation,
            )

    async def _authoritative_resume_matches(self, *, generation: str) -> bool:
        store = self._hot_state_store
        if store is None:
            return False
        try:
            stored = await store.get(KILL_SWITCH_REDIS_KEY)
        except Exception:
            return False
        if not (
            isinstance(stored, dict)
            and stored.get("halted") is False
            and stored.get("state") == "RUNNING"
            and stored.get("resume_authorized") is True
            and stored.get("generation") == generation
        ):
            return False
        try:
            _require_finite_positive_timestamp(
                stored.get("set_at_ts"),
                "kill_switch_set_at_ts",
            )
        except (TypeError, ValueError):
            return False
        return True

    # ──────────────────────────────────────────────────────────────────
    # 诊断 / 内省
    # ──────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """启动日志 / dashboard 用的内省 dict。"""
        result = {
            "process_role": self._process_role,
            "bootstrapped": self._bootstrapped,
            "subscribed": self._subscribed,
            "last_applied_ts": self._last_applied_ts,
            "fail_closed_on_authority_loss": self._fail_closed_on_authority_loss,
            "kill_switch": self.transition_status(),
        }
        result["last_publish_outcome"] = dict(self._last_publish_outcome)
        lease_task = self._trading_permission_task
        result["trading_permission"] = {
            "required": self._permission_lease_required,
            "owner": self._is_permission_lease_owner,
            "task_running": lease_task is not None and not lease_task.done(),
            "generation": self._trading_permission_generation,
            "last_success_ts": self._trading_permission_last_success_ts,
            "ttl_seconds": _KILL_SWITCH_PERMISSION_TTL_SECONDS,
            "renew_interval_seconds": _KILL_SWITCH_PERMISSION_RENEW_INTERVAL_SECONDS,
        }
        return result
