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


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


@dataclass(slots=True)
class DerivativesLiveGuardService:
    settings: Any
    kill_switch: KillSwitch
    account_service: Any
    event_store: Any
    metrics: MetricsRegistry
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    last_auto_halt_at: Any = None

    _EPSILON: Decimal = field(default=Decimal("1e-12"), init=False, repr=False)

    def snapshot(self) -> dict[str, Any]:
        if not self.last_snapshot:
            return self._base_snapshot(status="idle")
        return dict(self.last_snapshot)

    def status(self) -> dict[str, Any]:
        return self.snapshot()

    def evaluate_now(self) -> dict[str, Any]:
        if not self._applicable():
            self.last_snapshot = self._base_snapshot(
                status="not_applicable",
                summary="当前不是合约 guarded_live 运行线，不需要启用合约实盘自动保护。",
            )
            return dict(self.last_snapshot)

        account_status = dict(self.account_service.status())
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
        only_reduce_threshold = Decimal(str(self.settings.derivatives_only_reduce_trigger_margin_fraction))
        auto_halt_margin_threshold = Decimal(str(self.settings.derivatives_auto_halt_margin_usage_fraction))
        only_reduce_gap_threshold = Decimal(str(self.settings.liquidation_buffer_fraction))
        auto_halt_gap_threshold = Decimal(str(self.settings.derivatives_auto_halt_liquidation_gap_fraction))

        only_reduce_reasons: list[str] = []
        auto_halt_reasons: list[str] = []
        warnings: list[str] = []

        if current_margin_usage is None:
            if snapshot.positions:
                only_reduce_reasons.append("derivatives_risk_snapshot_missing_requires_only_reduce")
                warnings.append("当前拿不到合约风险快照，只允许继续减仓或平仓。")
        else:
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
            summary = "当前保证金缓冲或强平距离已经进入自动停机区间，系统必须保持暂停。"
            detail = " / ".join(self._reason_copy(code) for code in auto_halt_reasons)
        elif only_reduce_required:
            status = "warning"
            summary = "当前保证金缓冲偏紧，系统只允许继续减仓或平仓。"
            detail = " / ".join(self._reason_copy(code) for code in only_reduce_reasons)
        else:
            status = "healthy"
            summary = "当前合约保证金缓冲和强平距离都还在自动保护阈值之外。"
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
            thresholds={
                "only_reduce_margin_usage_fraction": only_reduce_threshold,
                "auto_halt_margin_usage_fraction": auto_halt_margin_threshold,
                "only_reduce_liquidation_gap_fraction": only_reduce_gap_threshold,
                "auto_halt_liquidation_gap_fraction": auto_halt_gap_threshold,
            },
        )
        if auto_halt_required:
            payload["halted"] = True
            self._trigger_halt(payload)
        else:
            payload["halted"] = self.kill_switch.halted and self.kill_switch.status().get("reason") == "derivatives_live_risk_auto_halt"
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
            "thresholds": thresholds or {},
            "halted": self.kill_switch.halted,
            "kill_switch_reason": self.kill_switch.status().get("reason"),
            "last_auto_halt_at": self.last_auto_halt_at,
            "last_evaluated_at": utc_now(),
        }

    def _current_initial_margin_usage(self, snapshot: Any) -> Decimal | None:
        risk_snapshot = getattr(snapshot, "risk_snapshot", None)
        if risk_snapshot is None or getattr(risk_snapshot, "initial_margin_requirement", None) is None:
            return None
        initial_margin = _to_decimal(risk_snapshot.initial_margin_requirement)
        equity_base = None
        for candidate in (
            getattr(risk_snapshot, "adjusted_equity", None),
            getattr(risk_snapshot, "total_equity", None),
            getattr(risk_snapshot, "available_equity", None),
        ):
            resolved = _to_decimal(candidate)
            if resolved is not None and resolved > self._EPSILON:
                equity_base = resolved
                break
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
            if qty > 0:
                gap = (mark_price - liquidation_price) / mark_price
            else:
                gap = (liquidation_price - mark_price) / mark_price
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
            "derivatives_risk_snapshot_missing_requires_only_reduce": "当前缺少合约风险快照，只允许继续减仓",
            "derivatives_margin_buffer_auto_halt": "当前保证金占用已经进入自动停机阈值",
            "derivatives_liquidation_proximity_auto_halt": "当前最近仓位距离强平过近，已进入自动停机阈值",
        }
        return messages.get(code, code)


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
