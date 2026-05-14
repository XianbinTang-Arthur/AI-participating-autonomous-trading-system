from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aats.bootstrap.metrics import MetricsRegistry
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.operator import ExecutionErrorSummary, ProcessingFailureRecord
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.portfolio_service.decimals import to_decimal_or_none as _to_decimal


@dataclass(slots=True)
class DerivativesLiveGuardService:
    settings: Any
    kill_switch: KillSwitch
    account_service: Any
    event_store: Any
    metrics: MetricsRegistry
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    last_auto_halt_at: Any = None
    last_risk_snapshot_seen_at: Any = None
    risk_snapshot_missing_started_at: Any = None
    risk_snapshot_missing_count: int = 0

    _EPSILON: Decimal = field(default=Decimal("1e-12"), init=False, repr=False)

    def snapshot(self) -> dict[str, Any]:
        if not self.last_snapshot:
            return self._base_snapshot(status="idle")
        return dict(self.last_snapshot)

    def status(self) -> dict[str, Any]:
        return self.snapshot()

    def reset_transient_risk_snapshot_state(self, *, reason: str) -> None:
        self.risk_snapshot_missing_started_at = None
        self.risk_snapshot_missing_count = 0
        self.last_auto_halt_at = None
        if self.last_snapshot:
            self.last_snapshot = {
                **self.last_snapshot,
                "risk_snapshot_stage": "reset",
                "risk_snapshot_missing_seconds": None,
                "risk_snapshot_missing_count": 0,
                "warnings": list(
                    dict.fromkeys(
                        [
                            *(
                                item
                                for item in self.last_snapshot.get("warnings", [])
                                if item != "derivatives_risk_snapshot_missing_grace_active"
                            ),
                            f"risk_snapshot_missing_timer_reset:{reason}",
                        ]
                    )
                ),
                "auto_halt_required": False,
                "auto_halt_reasons": [
                    item
                    for item in self.last_snapshot.get("auto_halt_reasons", [])
                    if item != "derivatives_risk_snapshot_missing_auto_halt"
                ],
                "blockers": [
                    item
                    for item in self.last_snapshot.get("blockers", [])
                    if item != "derivatives_risk_snapshot_missing_auto_halt"
                ],
                "halted": (
                    self.kill_switch.halted
                    and self.kill_switch.status().get("reason") == "derivatives_live_risk_auto_halt"
                ),
            }

    def evaluate_now(self) -> dict[str, Any]:
        if not self._applicable():
            self.last_snapshot = self._base_snapshot(
                status="not_applicable",
                summary="当前不是合约 guarded_live 运行线，不需要启用合约实盘自动保护。",
            )
            return dict(self.last_snapshot)

        try:
            account_status = dict(self.account_service.status())
        except Exception as exc:
            account_status = {
                "connected": False,
                "fresh": False,
                "ready": False,
                "last_error": f"account_status_unavailable:{exc.__class__.__name__}",
                "detail": "account_status_unavailable",
                "blockers": ["account_status_unavailable"],
            }
        if account_status.get("last_error") or not bool(account_status.get("ready", False)):
            blockers = [
                str(item).strip()
                for item in (account_status.get("blockers") or [])
                if str(item).strip()
            ]
            blockers.append("account_state_unready")
            self.last_snapshot = self._base_snapshot(
                status="unavailable",
                connected=bool(account_status.get("connected", False)),
                fresh=bool(account_status.get("fresh", False)),
                ready=False,
                blockers=list(dict.fromkeys(blockers)),
                summary="当前交易所账户状态不可用，合约实盘保护不能使用旧账户快照判断保证金和仓位状态。",
                detail=(
                    text_or_none(account_status.get("last_error"))
                    or text_or_none(account_status.get("detail"))
                    or "account_state_unready"
                ),
                only_reduce_required=True,
                only_reduce_reasons=["account_state_unready"],
                risk_snapshot_available=False,
                risk_snapshot_stage="unavailable",
            )
            return dict(self.last_snapshot)

        snapshot = self.account_service.latest_snapshot()
        if snapshot is None:
            self.last_snapshot = self._base_snapshot(
                status="unavailable",
                connected=bool(account_status.get("connected", False)),
                fresh=bool(account_status.get("fresh", False)),
                ready=False,
                blockers=["account_snapshot_missing"],
                summary="当前拿不到交易所账户快照，合约实盘保护无法确认保证金和仓位状态。",
                detail=text_or_none(account_status.get("detail")) or "account_snapshot_missing",
            )
            return dict(self.last_snapshot)

        liquidation_summary = self._liquidation_summary(snapshot)
        nearest_gap = liquidation_summary["nearest_liquidation_gap_ratio"]
        current_margin_usage = self._current_initial_margin_usage(snapshot)
        current_exposure = self._current_derivatives_exposure(snapshot)
        only_reduce_threshold = Decimal(str(self.settings.derivatives_only_reduce_trigger_margin_fraction))
        auto_halt_margin_threshold = Decimal(str(self.settings.derivatives_auto_halt_margin_usage_fraction))
        only_reduce_gap_threshold = Decimal(str(self.settings.liquidation_buffer_fraction))
        auto_halt_gap_threshold = Decimal(str(self.settings.derivatives_auto_halt_liquidation_gap_fraction))
        risk_snapshot_available = current_margin_usage is not None
        risk_snapshot_stage = "healthy"
        risk_snapshot_missing_seconds: float | None = None

        only_reduce_reasons: list[str] = []
        auto_halt_reasons: list[str] = []
        warnings: list[str] = []

        if not risk_snapshot_available:
            if snapshot.positions:
                self.risk_snapshot_missing_count += 1
                if self.risk_snapshot_missing_started_at is None:
                    self.risk_snapshot_missing_started_at = utc_now()
                risk_snapshot_missing_seconds = max(
                    (utc_now() - self.risk_snapshot_missing_started_at).total_seconds(),
                    0.0,
                )
                if risk_snapshot_missing_seconds < float(self.settings.derivatives_risk_snapshot_grace_seconds):
                    risk_snapshot_stage = "grace"
                    warnings.append("derivatives_risk_snapshot_missing_grace_active")
                elif risk_snapshot_missing_seconds < float(self.settings.derivatives_risk_snapshot_auto_halt_after_seconds):
                    risk_snapshot_stage = "only_reduce"
                    only_reduce_reasons.append("derivatives_risk_snapshot_missing_requires_only_reduce")
                else:
                    risk_snapshot_stage = "auto_halt"
                    auto_halt_reasons.append("derivatives_risk_snapshot_missing_auto_halt")
            else:
                self.risk_snapshot_missing_started_at = None
                self.risk_snapshot_missing_count = 0
        else:
            self.last_risk_snapshot_seen_at = getattr(snapshot, "fetched_at", None) or utc_now()
            self.risk_snapshot_missing_started_at = None
            self.risk_snapshot_missing_count = 0
            if current_margin_usage >= only_reduce_threshold - self._EPSILON:
                only_reduce_reasons.append("derivatives_margin_usage_requires_only_reduce")
            if current_margin_usage >= auto_halt_margin_threshold - self._EPSILON:
                auto_halt_reasons.append("derivatives_margin_buffer_auto_halt")

        if nearest_gap is not None:
            if nearest_gap <= only_reduce_gap_threshold + self._EPSILON:
                only_reduce_reasons.append("derivatives_liquidation_gap_requires_only_reduce")
            if nearest_gap <= auto_halt_gap_threshold + self._EPSILON:
                auto_halt_reasons.append("derivatives_liquidation_proximity_auto_halt")

        auto_halt_required = bool(auto_halt_reasons)
        only_reduce_required = bool(only_reduce_reasons)
        blockers = list(dict.fromkeys(auto_halt_reasons))

        if auto_halt_required:
            status = "critical"
            summary = "当前保证金缓冲、强平距离或风险快照状态已经进入自动停机区间，系统必须保持暂停。"
            detail = " / ".join(self._reason_copy(code) for code in auto_halt_reasons)
        elif only_reduce_required:
            status = "warning"
            summary = "当前保证金缓冲偏紧，或合约风险快照持续缺失，系统只允许继续减仓或平仓。"
            detail = " / ".join(self._reason_copy(code) for code in only_reduce_reasons)
        elif risk_snapshot_stage == "grace":
            status = "degraded"
            summary = "当前合约风险快照短时缺失，系统先自动收缩风险预算并降低执行侵略性。"
            detail = "如果风险快照持续缺失，系统会升级到只减仓，再升级到自动停机。"
        else:
            status = "healthy"
            summary = "当前合约保证金缓冲、风险快照和强平距离都仍在自动保护阈值之外。"
            detail = warnings[0] if warnings else "derivatives_runtime_guard_healthy"

        payload = self._base_snapshot(
            status=status,
            connected=bool(account_status.get("connected", True)),
            fresh=bool(account_status.get("fresh", True)),
            ready=not auto_halt_required and not only_reduce_required,
            blockers=blockers,
            summary=summary,
            detail=detail,
            current_initial_margin_usage_fraction=current_margin_usage,
            nearest_liquidation_gap_ratio=nearest_gap,
            closest_position=liquidation_summary["closest_position"],
            only_reduce_required=only_reduce_required,
            only_reduce_reasons=list(dict.fromkeys(only_reduce_reasons)),
            auto_halt_required=auto_halt_required,
            auto_halt_reasons=list(dict.fromkeys(auto_halt_reasons)),
            warnings=warnings,
            risk_snapshot_available=risk_snapshot_available,
            risk_snapshot_stage=risk_snapshot_stage,
            risk_snapshot_missing_seconds=risk_snapshot_missing_seconds,
            risk_snapshot_missing_count=self.risk_snapshot_missing_count,
            last_risk_snapshot_seen_at=self.last_risk_snapshot_seen_at,
            thresholds={
                "only_reduce_margin_usage_fraction": only_reduce_threshold,
                "auto_halt_margin_usage_fraction": auto_halt_margin_threshold,
                "only_reduce_liquidation_gap_fraction": only_reduce_gap_threshold,
                "auto_halt_liquidation_gap_fraction": auto_halt_gap_threshold,
                "risk_snapshot_grace_seconds": float(self.settings.derivatives_risk_snapshot_grace_seconds),
                "risk_snapshot_auto_halt_after_seconds": float(
                    self.settings.derivatives_risk_snapshot_auto_halt_after_seconds
                ),
            },
            current_derivatives_exposure=current_exposure,
        )
        if auto_halt_required:
            payload["halted"] = True
            self._trigger_halt(payload)
        else:
            payload["halted"] = (
                self.kill_switch.halted
                and self.kill_switch.status().get("reason") == "derivatives_live_risk_auto_halt"
            )
        self.last_snapshot = payload
        return dict(payload)

    def _applicable(self) -> bool:
        return (
            self.settings.mode == "guarded_live"
            and self.settings.trading_product_type == "derivatives"
            and self.settings.execution_backend == "okx"
            and bool(self.settings.derivatives_runtime_guard_enabled)
        )

    def _base_snapshot(
        self,
        *,
        status: str,
        connected: bool = True,
        fresh: bool = True,
        ready: bool = True,
        blockers: list[str] | None = None,
        summary: str | None = None,
        detail: str | None = None,
        current_initial_margin_usage_fraction: Decimal | None = None,
        nearest_liquidation_gap_ratio: Decimal | None = None,
        closest_position: dict[str, Any] | None = None,
        only_reduce_required: bool = False,
        only_reduce_reasons: list[str] | None = None,
        auto_halt_required: bool = False,
        auto_halt_reasons: list[str] | None = None,
        warnings: list[str] | None = None,
        thresholds: dict[str, Any] | None = None,
        current_derivatives_exposure: dict[str, Any] | None = None,
        risk_snapshot_available: bool = True,
        risk_snapshot_stage: str = "healthy",
        risk_snapshot_missing_seconds: float | None = None,
        risk_snapshot_missing_count: int = 0,
        last_risk_snapshot_seen_at: Any = None,
    ) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.derivatives_runtime_guard_enabled),
            "status": status,
            "connected": connected,
            "fresh": fresh,
            "ready": ready,
            "blockers": blockers or [],
            "summary": summary,
            "detail": detail,
            "current_initial_margin_usage_fraction": current_initial_margin_usage_fraction,
            "nearest_liquidation_gap_ratio": nearest_liquidation_gap_ratio,
            "closest_position": closest_position,
            "only_reduce_required": only_reduce_required,
            "only_reduce_reasons": only_reduce_reasons or [],
            "auto_halt_required": auto_halt_required,
            "auto_halt_reasons": auto_halt_reasons or [],
            "warnings": warnings or [],
            "current_derivatives_exposure": current_derivatives_exposure or {},
            "risk_snapshot_available": risk_snapshot_available,
            "risk_snapshot_stage": risk_snapshot_stage,
            "risk_snapshot_missing_seconds": risk_snapshot_missing_seconds,
            "risk_snapshot_missing_count": risk_snapshot_missing_count,
            "last_risk_snapshot_seen_at": last_risk_snapshot_seen_at,
            "thresholds": thresholds or {},
            "halted": self.kill_switch.halted,
            "kill_switch_reason": self.kill_switch.status().get("reason"),
            "last_auto_halt_at": self.last_auto_halt_at,
            "last_evaluated_at": utc_now(),
        }

    def _current_initial_margin_usage(self, snapshot: Any) -> Decimal | None:
        risk_snapshot = getattr(snapshot, "risk_snapshot", None)
        initial_margin = None
        if risk_snapshot is not None:
            initial_margin = _to_decimal(getattr(risk_snapshot, "initial_margin_requirement", None))
        if risk_snapshot is not None and initial_margin is None:
            position_margins = [
                resolved
                for resolved in (
                    _to_decimal(getattr(position, "margin_allocated", None))
                    for position in getattr(snapshot, "positions", [])
                )
                if resolved is not None and resolved > self._EPSILON
            ]
            if position_margins:
                initial_margin = sum(position_margins, Decimal("0"))

        equity_base = None
        if risk_snapshot is not None:
            for candidate in (
                getattr(risk_snapshot, "adjusted_equity", None),
                getattr(risk_snapshot, "total_equity", None),
                getattr(risk_snapshot, "available_equity", None),
            ):
                resolved = _to_decimal(candidate)
                if resolved is not None and resolved > self._EPSILON:
                    equity_base = resolved
                    break
        if risk_snapshot is not None and equity_base is None:
            usdt_balances = [
                resolved
                for resolved in (
                    _to_decimal(getattr(balance, "total", None))
                    for balance in getattr(snapshot, "balances", [])
                    if str(getattr(balance, "currency", "")).upper() == "USDT"
                )
                if resolved is not None and resolved > self._EPSILON
            ]
            if usdt_balances:
                equity_base = sum(usdt_balances, Decimal("0"))
        if initial_margin is None or equity_base is None or equity_base <= self._EPSILON:
            return None
        return initial_margin / equity_base

    def _liquidation_summary(self, snapshot: Any) -> dict[str, Any]:
        closest_gap: Decimal | None = None
        closest_position: dict[str, Any] | None = None
        for position in getattr(snapshot, "positions", []):
            mark_price = _to_decimal(getattr(position, "mark_price", None))
            liquidation_price = _to_decimal(getattr(position, "liquidation_price", None))
            qty = _to_decimal(getattr(position, "quantity", None))
            if (
                mark_price is None
                or liquidation_price is None
                or mark_price <= self._EPSILON
                or liquidation_price <= self._EPSILON
                or qty is None
                or abs(qty) <= self._EPSILON
            ):
                continue
            side = str(getattr(position, "side", None) or getattr(position, "pos_side", None) or "").strip().lower()
            if side == "short" or (side not in {"long", "short"} and qty < -self._EPSILON):
                gap = (liquidation_price - mark_price) / mark_price
            else:
                gap = (mark_price - liquidation_price) / mark_price
            if closest_gap is None or gap < closest_gap:
                closest_gap = gap
                closest_position = {
                    "symbol": getattr(position, "symbol", None),
                    "pos_side": getattr(position, "side", None),
                    "margin_mode": getattr(position, "margin_mode", None),
                    "mark_price": mark_price,
                    "liquidation_price": liquidation_price,
                    "margin_ratio": _to_decimal(getattr(position, "margin_ratio", None)),
                    "margin_allocated": _to_decimal(getattr(position, "margin_allocated", None)),
                    "maintenance_margin": _to_decimal(getattr(position, "maintenance_margin", None)),
                }
        return {
            "nearest_liquidation_gap_ratio": closest_gap,
            "closest_position": closest_position,
        }

    def _trigger_halt(self, snapshot: dict[str, Any]) -> None:
        reason = "derivatives_live_risk_auto_halt"
        if self.kill_switch.halted and self.kill_switch.status().get("reason") == reason:
            return
        # Stage 6 Slice 6.4：合并的 KillSwitch 自动跨进程广播
        self.kill_switch.halt(reason=reason)
        self.last_auto_halt_at = utc_now()
        self.metrics.increment("derivatives_live_guard_auto_halts")
        breach_codes = ",".join(snapshot.get("auto_halt_reasons") or [])
        self.event_store.append(
            build_envelope(
                topic=topics.EXECUTION_ERROR_SUMMARIES,
                key="derivatives_live_guard",
                payload_model=ExecutionErrorSummary(
                    subsystem="derivatives_live_guard",
                    severity="error",
                    message=f"derivatives_live_guard_halted:{breach_codes}",
                    observed_at=self.last_auto_halt_at,
                ),
                source_component="derivatives_live_guard",
            )
        )
        self.event_store.append(
            build_envelope(
                topic=topics.PROCESSING_FAILURES,
                key="derivatives_live_guard",
                payload_model=ProcessingFailureRecord(
                    subsystem="derivatives_live_guard",
                    stage="runtime_guard",
                    severity="error",
                    message="合约实盘自动保护已触发暂停。",
                    retriable=False,
                    observed_at=self.last_auto_halt_at,
                    details={
                        "auto_halt_reasons": list(snapshot.get("auto_halt_reasons") or []),
                        "only_reduce_reasons": list(snapshot.get("only_reduce_reasons") or []),
                        "current_initial_margin_usage_fraction": (
                            None
                            if snapshot.get("current_initial_margin_usage_fraction") is None
                            else str(snapshot["current_initial_margin_usage_fraction"])
                        ),
                        "nearest_liquidation_gap_ratio": (
                            None
                            if snapshot.get("nearest_liquidation_gap_ratio") is None
                            else str(snapshot["nearest_liquidation_gap_ratio"])
                        ),
                        "closest_position": snapshot.get("closest_position"),
                        "risk_snapshot_stage": snapshot.get("risk_snapshot_stage"),
                        "risk_snapshot_missing_seconds": snapshot.get("risk_snapshot_missing_seconds"),
                    },
                ),
                source_component="derivatives_live_guard",
            )
        )

    @staticmethod
    def _reason_copy(code: str) -> str:
        messages = {
            "derivatives_margin_usage_requires_only_reduce": "当前保证金占用已经进入 only-reduce 区间",
            "derivatives_liquidation_gap_requires_only_reduce": "当前最近仓位已经进入强平缓冲区",
            "derivatives_risk_snapshot_missing_requires_only_reduce": "当前缺少合约风险快照，系统只允许继续减仓",
            "derivatives_risk_snapshot_missing_auto_halt": "合约风险快照持续缺失时间过长，系统已经升级为自动停机",
            "derivatives_margin_buffer_auto_halt": "当前保证金占用已经进入自动停机阈值",
            "derivatives_liquidation_proximity_auto_halt": "当前最近仓位距离强平过近，已经进入自动停机阈值",
            "derivatives_risk_snapshot_missing_grace_active": "合约风险快照短时缺失，系统先进入平滑降级观察期",
        }
        return messages.get(code, code)

    def _current_derivatives_exposure(self, snapshot: Any) -> dict[str, Any]:
        positions = list(getattr(snapshot, "positions", []) or [])
        if not positions:
            return {
                "long_notional": Decimal("0"),
                "short_notional": Decimal("0"),
                "gross_notional": Decimal("0"),
                "net_notional": Decimal("0"),
                "gross_leverage": Decimal("0"),
                "net_leverage": Decimal("0"),
            }
        long_notional = Decimal("0")
        short_notional = Decimal("0")
        for position in positions:
            notional = _to_decimal(getattr(position, "notional_usd", None))
            if notional is None or abs(notional) <= self._EPSILON:
                quantity = abs(_to_decimal(getattr(position, "quantity", None)) or Decimal("0"))
                reference_price = (
                    _to_decimal(getattr(position, "mark_price", None))
                    or _to_decimal(getattr(position, "average_entry_price", None))
                    or Decimal("0")
                )
                notional = quantity * reference_price
            side = str(getattr(position, "side", "") or "").strip().lower()
            quantity = _to_decimal(getattr(position, "quantity", None)) or Decimal("0")
            if side == "short" or quantity < -self._EPSILON:
                short_notional += abs(notional)
            else:
                long_notional += abs(notional)
        gross_notional = long_notional + short_notional
        net_notional = abs(long_notional - short_notional)
        equity_base = self._equity_base_for_snapshot(snapshot)
        gross_leverage = (
            gross_notional / equity_base
            if equity_base is not None and equity_base > self._EPSILON
            else Decimal("0")
        )
        net_leverage = (
            net_notional / equity_base
            if equity_base is not None and equity_base > self._EPSILON
            else Decimal("0")
        )
        return {
            "long_notional": long_notional,
            "short_notional": short_notional,
            "gross_notional": gross_notional,
            "net_notional": net_notional,
            "gross_leverage": gross_leverage,
            "net_leverage": net_leverage,
        }

    def _equity_base_for_snapshot(self, snapshot: Any) -> Decimal | None:
        risk_snapshot = getattr(snapshot, "risk_snapshot", None)
        if risk_snapshot is not None:
            for candidate in (
                getattr(risk_snapshot, "adjusted_equity", None),
                getattr(risk_snapshot, "total_equity", None),
                getattr(risk_snapshot, "available_equity", None),
            ):
                resolved = _to_decimal(candidate)
                if resolved is not None and resolved > self._EPSILON:
                    return resolved
        balances = [
            resolved
            for resolved in (
                _to_decimal(getattr(balance, "total", None))
                for balance in getattr(snapshot, "balances", [])
                if str(getattr(balance, "currency", "")).upper() == "USDT"
            )
            if resolved is not None and resolved > self._EPSILON
        ]
        if balances:
            return sum(balances, Decimal("0"))
        return None


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
