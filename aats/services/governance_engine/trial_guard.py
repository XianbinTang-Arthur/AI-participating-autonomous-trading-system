from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

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
class ForwardTrialGuardService:
    settings: Any
    kill_switch: KillSwitch
    event_store: Any
    metrics: MetricsRegistry
    profitability_provider: Callable[[int], dict[str, Any]]
    anomaly_provider: Callable[[int], dict[str, Any]]
    last_snapshot: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        if not self.last_snapshot:
            return self._base_snapshot(status="idle")
        return dict(self.last_snapshot)

    def evaluate_now(self) -> dict[str, Any]:
        if not self.settings.trial_guard_enabled:
            self.last_snapshot = self._base_snapshot(status="disabled")
            return dict(self.last_snapshot)

        lookback = max(int(self.settings.trial_guard_lookback_fills), 1)
        profitability = self.profitability_provider(lookback)
        anomalies = self.anomaly_provider(lookback)
        recent_rows = list(profitability.get("recent_closed_fills") or [])
        recent_realized_events = list(profitability.get("recent_realized_events") or [])
        summary = dict(profitability.get("summary") or {})
        anomaly_summary = dict(anomalies.get("summary") or {})
        now = utc_now()
        daily_cutoff = now - timedelta(hours=24)

        daily_trading_net_realized = Decimal("0")
        daily_funding_fee_net = Decimal("0")
        consecutive_losses = 0
        for row in recent_rows:
            pnl = _to_decimal(row.get("realized_pnl_delta")) or Decimal("0")
            observed_at = row.get("ingestion_timestamp") or row.get("exchange_fill_timestamp")
            if isinstance(observed_at, datetime) and observed_at >= daily_cutoff:
                daily_trading_net_realized += pnl
            if pnl < Decimal("0"):
                consecutive_losses += 1
            else:
                break

        for row in recent_realized_events:
            if str(row.get("event_kind") or "") != "funding_fee":
                continue
            observed_at = row.get("event_timestamp")
            if isinstance(observed_at, datetime) and observed_at >= daily_cutoff:
                daily_funding_fee_net += _to_decimal(row.get("funding_fee_delta")) or Decimal("0")

        daily_combined_net_realized = daily_trading_net_realized + daily_funding_fee_net

        fill_count = int(summary.get("closed_fill_count") or 0)
        fee_ratio = _to_decimal(summary.get("fee_to_notional_ratio"))
        high_slippage_count = int(anomaly_summary.get("high_slippage_count") or 0)
        slow_submit_to_fill_count = int(anomaly_summary.get("slow_submit_to_fill_count") or 0)
        high_slippage_ratio = (
            None if fill_count == 0 else round(high_slippage_count / fill_count, 6)
        )
        slow_submit_to_fill_ratio = (
            None if fill_count == 0 else round(slow_submit_to_fill_count / fill_count, 6)
        )

        breaches: list[dict[str, Any]] = []
        if fill_count >= int(self.settings.trial_guard_min_closed_fills):
            if daily_combined_net_realized <= -Decimal(str(self.settings.trial_guard_max_daily_loss_usdt)):
                breaches.append(
                    {
                        "code": "trial_guard_daily_loss_limit",
                        "value": daily_combined_net_realized,
                        "threshold": -Decimal(str(self.settings.trial_guard_max_daily_loss_usdt)),
                    }
                )
            if consecutive_losses >= int(self.settings.trial_guard_max_consecutive_losses):
                breaches.append(
                    {
                        "code": "trial_guard_consecutive_losses",
                        "value": consecutive_losses,
                        "threshold": int(self.settings.trial_guard_max_consecutive_losses),
                    }
                )
            if fee_ratio is not None and fee_ratio >= Decimal(str(self.settings.trial_guard_max_fee_to_notional_ratio)):
                breaches.append(
                    {
                        "code": "trial_guard_fee_drag_limit",
                        "value": fee_ratio,
                        "threshold": Decimal(str(self.settings.trial_guard_max_fee_to_notional_ratio)),
                    }
                )
            if (
                high_slippage_ratio is not None
                and high_slippage_ratio >= float(self.settings.trial_guard_max_high_slippage_ratio)
            ):
                breaches.append(
                    {
                        "code": "trial_guard_high_slippage_ratio",
                        "value": high_slippage_ratio,
                        "threshold": float(self.settings.trial_guard_max_high_slippage_ratio),
                    }
                )
            if (
                slow_submit_to_fill_ratio is not None
                and slow_submit_to_fill_ratio >= float(self.settings.trial_guard_max_slow_submit_to_fill_ratio)
            ):
                breaches.append(
                    {
                        "code": "trial_guard_slow_fill_ratio",
                        "value": slow_submit_to_fill_ratio,
                        "threshold": float(self.settings.trial_guard_max_slow_submit_to_fill_ratio),
                    }
                )

        status = "breached" if breaches else ("monitoring" if fill_count >= int(self.settings.trial_guard_min_closed_fills) else "warming_up")
        recommended_action = (
            "保持暂停并复核最近成交质量与净收益。"
            if breaches
            else "继续小资金试盘并观察收益、费用和滑点。"
        )
        snapshot = self._base_snapshot(
            status=status,
            fill_count=fill_count,
            daily_net_realized=daily_combined_net_realized,
            daily_trading_net_realized=daily_trading_net_realized,
            daily_funding_fee_net=daily_funding_fee_net,
            daily_combined_net_realized=daily_combined_net_realized,
            consecutive_losses=consecutive_losses,
            fee_to_notional_ratio=fee_ratio,
            high_slippage_ratio=high_slippage_ratio,
            slow_submit_to_fill_ratio=slow_submit_to_fill_ratio,
            breaches=breaches,
            recommended_action=recommended_action,
        )
        if breaches:
            snapshot["halted"] = True
            self._trigger_halt(snapshot)
        else:
            snapshot["halted"] = self.kill_switch.halted and self.kill_switch.status().get("reason") == "trial_guard_threshold_breached"
        self.last_snapshot = snapshot
        return dict(snapshot)

    def _trigger_halt(self, snapshot: dict[str, Any]) -> None:
        reason = "trial_guard_threshold_breached"
        if not self.kill_switch.halted:
            self.kill_switch.halt(reason=reason)
            self.metrics.increment("trial_guard_halts")
            self.event_store.append(
                build_envelope(
                    topic=topics.EXECUTION_ERROR_SUMMARIES,
                    key="trial_guard",
                    payload_model=ExecutionErrorSummary(
                        subsystem="trial_guard",
                        severity="warning",
                        message=f"trial_guard_halted:{','.join(item['code'] for item in snapshot['breaches'])}",
                        observed_at=utc_now(),
                    ),
                    source_component="trial_guard",
                )
            )
            self.event_store.append(
                build_envelope(
                    topic=topics.PROCESSING_FAILURES,
                    key="trial_guard",
                    payload_model=ProcessingFailureRecord(
                        subsystem="trial_guard",
                        stage="threshold_evaluation",
                        severity="warning",
                        message="试盘守护已触发自动暂停。",
                        retriable=False,
                        observed_at=utc_now(),
                        details={
                            "breaches": snapshot["breaches"],
                            "daily_net_realized": str(snapshot["daily_net_realized"]),
                            "daily_trading_net_realized": str(snapshot["daily_trading_net_realized"]),
                            "daily_funding_fee_net": str(snapshot["daily_funding_fee_net"]),
                            "consecutive_losses": snapshot["consecutive_losses"],
                            "fee_to_notional_ratio": (
                                None if snapshot["fee_to_notional_ratio"] is None else str(snapshot["fee_to_notional_ratio"])
                            ),
                        },
                    ),
                    source_component="trial_guard",
                )
            )

    def _base_snapshot(
        self,
        *,
        status: str,
        fill_count: int = 0,
        daily_net_realized: Decimal = Decimal("0"),
        daily_trading_net_realized: Decimal = Decimal("0"),
        daily_funding_fee_net: Decimal = Decimal("0"),
        daily_combined_net_realized: Decimal = Decimal("0"),
        consecutive_losses: int = 0,
        fee_to_notional_ratio: Decimal | None = None,
        high_slippage_ratio: float | None = None,
        slow_submit_to_fill_ratio: float | None = None,
        breaches: list[dict[str, Any]] | None = None,
        recommended_action: str | None = None,
    ) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.trial_guard_enabled),
            "status": status,
            "profile_label": "small_capital_forward_test",
            "active_config_profile": self.settings.config_profile,
            "profile_active": self.settings.config_profile == "forward_test_small_capital",
            "halted": self.kill_switch.halted,
            "reason": self.kill_switch.status().get("reason"),
            "lookback_fills": int(self.settings.trial_guard_lookback_fills),
            "min_closed_fills": int(self.settings.trial_guard_min_closed_fills),
            "fill_count": fill_count,
            "daily_net_realized": daily_net_realized,
            "daily_trading_net_realized": daily_trading_net_realized,
            "daily_funding_fee_net": daily_funding_fee_net,
            "daily_combined_net_realized": daily_combined_net_realized,
            "consecutive_losses": consecutive_losses,
            "fee_to_notional_ratio": fee_to_notional_ratio,
            "high_slippage_ratio": high_slippage_ratio,
            "slow_submit_to_fill_ratio": slow_submit_to_fill_ratio,
            "breaches": breaches or [],
            "thresholds": {
                "max_daily_loss_usdt": self.settings.trial_guard_max_daily_loss_usdt,
                "max_consecutive_losses": self.settings.trial_guard_max_consecutive_losses,
                "max_fee_to_notional_ratio": self.settings.trial_guard_max_fee_to_notional_ratio,
                "max_high_slippage_ratio": self.settings.trial_guard_max_high_slippage_ratio,
                "max_slow_submit_to_fill_ratio": self.settings.trial_guard_max_slow_submit_to_fill_ratio,
            },
            "recommended_action": recommended_action,
            "last_evaluated_at": utc_now(),
            "summary": self._summary(
                status=status,
                fill_count=fill_count,
                breaches=breaches or [],
            ),
        }

    @staticmethod
    def _summary(*, status: str, fill_count: int, breaches: list[dict[str, Any]]) -> str:
        if status == "disabled":
            return "试盘守护未启用。"
        if status == "warming_up":
            return f"试盘守护正在预热，当前已记录 {fill_count} 笔已完成成交，尚未达到自动停机判定样本量。"
        if status == "breached":
            return f"试盘守护已触发自动暂停，原因：{', '.join(item['code'] for item in breaches)}。"
        return "试盘守护正在运行，当前未触发自动停机阈值。"
