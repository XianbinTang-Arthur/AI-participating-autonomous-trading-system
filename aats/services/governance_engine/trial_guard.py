from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable

from aats.bootstrap.metrics import MetricsRegistry
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.operator import ExecutionErrorSummary, ProcessingFailureRecord
from aats.services.accounting import try_fill_fee_cost_in_quote
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.portfolio_service.decimals import to_decimal_or_none as _to_decimal
from aats.services.runtime_scope import event_matches_scope, runtime_state_scope


def _coerce_datetime(value: Any) -> datetime | None:
    """Trial-guard timestamp coerce; illegal → None for resilient evaluation.

    Trial-guard iterates profitability samples; a single malformed timestamp
    should be dropped from the sample, not crash the guard.
    """
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    if not isinstance(value, (str, datetime)):
        return None
    try:
        return parse_iso_datetime_utc(value, context="trial_guard")
    except ValueError:
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
    manual_reset_after: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        if not self.last_snapshot:
            return self._base_snapshot(status="idle")
        return dict(self.last_snapshot)

    def manual_reset(self, *, effective_after: datetime | None = None) -> dict[str, Any]:
        self.manual_reset_after = effective_after or utc_now()
        self.last_snapshot = {}
        return self.evaluate_now()

    def preview_manual_reset(self, *, effective_after: datetime | None = None) -> dict[str, Any]:
        return self._evaluate(manual_reset_after_override=effective_after or utc_now(), persist=False)

    def evaluate_now(self) -> dict[str, Any]:
        return self._evaluate(manual_reset_after_override=None, persist=True)

    def _evaluate(
        self,
        *,
        manual_reset_after_override: datetime | None,
        persist: bool,
    ) -> dict[str, Any]:
        if not self.settings.trial_guard_enabled:
            snapshot = self._base_snapshot(status="disabled")
            if persist:
                self.last_snapshot = snapshot
            return dict(snapshot)
        if not self._trial_observation_active():
            snapshot = self._base_snapshot(status="inactive_for_runtime")
            if persist:
                self.last_snapshot = snapshot
            return dict(snapshot)

        lookback = max(int(self.settings.trial_guard_lookback_fills), 1)
        profitability = self.profitability_provider(lookback)
        anomalies = self.anomaly_provider(lookback)
        recent_rows = list(profitability.get("recent_closed_fills") or [])
        recent_realized_events = list(profitability.get("recent_realized_events") or [])
        summary = dict(profitability.get("summary") or {})
        anomaly_summary = dict(anomalies.get("summary") or {})
        anomaly_rows = list(anomalies.get("rows") or [])
        manual_reset_after = self._effective_manual_reset_cutoff(manual_reset_after_override=manual_reset_after_override)
        if manual_reset_after is not None:
            recent_rows = self._filter_rows_after(
                recent_rows,
                cutoff=manual_reset_after,
                timestamp_keys=("ingestion_timestamp", "exchange_fill_timestamp"),
            )
            recent_realized_events = self._filter_rows_after(
                recent_realized_events,
                cutoff=manual_reset_after,
                timestamp_keys=("event_timestamp",),
            )
            anomaly_rows = self._filter_rows_after(
                anomaly_rows,
                cutoff=manual_reset_after,
                timestamp_keys=("ingestion_timestamp", "exchange_fill_timestamp"),
            )
            recent_fill_ids = {str(row.get("fill_id") or "") for row in recent_rows if row.get("fill_id")}
            anomaly_fill_ids_present = any(row.get("fill_id") for row in anomaly_rows)
            if recent_fill_ids and anomaly_fill_ids_present:
                anomaly_rows = [
                    row for row in anomaly_rows if str(row.get("fill_id") or "") in recent_fill_ids
                ]
        now = utc_now()
        daily_cutoff = now - timedelta(hours=24)

        daily_trading_net_realized = Decimal("0")
        daily_funding_fee_net = Decimal("0")
        consecutive_losses = 0
        consecutive_loss_counted = False
        for row in recent_rows:
            pnl = _to_decimal(row.get("realized_pnl_delta")) or Decimal("0")
            observed_at = _coerce_datetime(row.get("ingestion_timestamp") or row.get("exchange_fill_timestamp"))
            if observed_at is not None and observed_at >= daily_cutoff:
                daily_trading_net_realized += pnl
            if not consecutive_loss_counted:
                if pnl < Decimal("0"):
                    consecutive_losses += 1
                else:
                    consecutive_loss_counted = True

        for row in recent_realized_events:
            if str(row.get("event_kind") or "") != "funding_fee":
                continue
            observed_at = _coerce_datetime(row.get("event_timestamp"))
            if observed_at is not None and observed_at >= daily_cutoff:
                daily_funding_fee_net += _to_decimal(row.get("funding_fee_delta")) or Decimal("0")

        daily_combined_net_realized = daily_trading_net_realized + daily_funding_fee_net

        fill_count = len(recent_rows) if manual_reset_after is not None else int(summary.get("closed_fill_count") or 0)
        if manual_reset_after is not None:
            fee_ratio = self._fee_to_notional_ratio(recent_rows)
            high_slippage_count = sum(
                1 for row in anomaly_rows if "high_adverse_slippage" in list(row.get("anomaly_flags") or [])
            )
            slow_submit_to_fill_count = sum(
                1 for row in anomaly_rows if "slow_submit_to_fill" in list(row.get("anomaly_flags") or [])
            )
        else:
            fee_ratio = _to_decimal(summary.get("fee_to_notional_ratio"))
            summary_high_slippage_count = summary.get("high_slippage_count")
            summary_slow_fill_count = summary.get("slow_submit_to_fill_count")
            high_slippage_count = int(
                summary_high_slippage_count
                if summary_high_slippage_count is not None
                else anomaly_summary.get("high_slippage_count") or 0
            )
            slow_submit_to_fill_count = int(
                summary_slow_fill_count
                if summary_slow_fill_count is not None
                else anomaly_summary.get("slow_submit_to_fill_count") or 0
            )
        high_slippage_ratio = None if fill_count == 0 else round(high_slippage_count / fill_count, 6)
        slow_submit_to_fill_ratio = None if fill_count == 0 else round(slow_submit_to_fill_count / fill_count, 6)

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

        previous_status = str(self.last_snapshot.get("status") or "").lower()
        kill_switch_reason = str(self.kill_switch.status().get("reason") or "")
        status = "breached" if breaches else ("monitoring" if fill_count >= int(self.settings.trial_guard_min_closed_fills) else "warming_up")
        if (
            not breaches
            and fill_count >= int(self.settings.trial_guard_min_closed_fills)
            and manual_reset_after is None
            and (previous_status == "breached" or kill_switch_reason == "trial_guard_threshold_breached")
        ):
            status = "recovered"

        recommended_action = (
            "先核对最近成交、费用拖累和试盘守护阈值命中情况，再决定何时恢复自动运行。"
            if breaches
            else "试盘守护已人工重置，当前先用更小样本重新观察，再决定是否恢复放量。"
            if manual_reset_after is not None
            else "继续保持小资金试盘，观察收益、费用和滑点是否稳定。"
        )

        snapshot = self._base_snapshot(
            status=status,
            fill_count=fill_count,
            daily_trading_net_realized=daily_trading_net_realized,
            daily_funding_fee_net=daily_funding_fee_net,
            daily_combined_net_realized=daily_combined_net_realized,
            consecutive_losses=consecutive_losses,
            fee_to_notional_ratio=fee_ratio,
            high_slippage_ratio=high_slippage_ratio,
            slow_submit_to_fill_ratio=slow_submit_to_fill_ratio,
            breaches=breaches,
            recommended_action=recommended_action,
            manual_reset_after=manual_reset_after,
        )
        if breaches and persist:
            snapshot["halted"] = True
            self._trigger_halt(snapshot)
        else:
            snapshot["halted"] = self.kill_switch.halted and self.kill_switch.status().get("reason") == "trial_guard_threshold_breached"
        if persist:
            self.last_snapshot = snapshot
        return dict(snapshot)

    def _effective_manual_reset_cutoff(self, *, manual_reset_after_override: datetime | None = None) -> datetime | None:
        latest_recorded = self._latest_manual_reset_cutoff()
        local_cutoff = manual_reset_after_override if manual_reset_after_override is not None else self.manual_reset_after
        if latest_recorded is None:
            return local_cutoff
        if local_cutoff is None:
            return latest_recorded
        return max(latest_recorded, local_cutoff)

    def _latest_manual_reset_cutoff(self) -> datetime | None:
        events = self._operator_action_events()
        if not events:
            return None
        for item in reversed(events):
            payload = getattr(item, "payload", None)
            if not isinstance(payload, dict):
                continue
            if str(payload.get("action") or "") != "trial_guard_manual_reset":
                continue
            details = dict(payload.get("details") or {})
            cutoff = _coerce_datetime(details.get("effective_after"))
            if cutoff is not None:
                return cutoff
            return _coerce_datetime(payload.get("created_at"))
        return None

    def _operator_action_events(self) -> list[Any]:
        by_topic_scoped = getattr(self.event_store, "by_topic_scoped", None)
        if callable(by_topic_scoped):
            try:
                return list(by_topic_scoped(topics.OPERATOR_ACTIONS, scope=runtime_state_scope(self.settings)))
            except Exception:
                pass
        by_topic = getattr(self.event_store, "by_topic", None)
        if not callable(by_topic):
            return []
        scope = runtime_state_scope(self.settings)
        return [item for item in by_topic(topics.OPERATOR_ACTIONS) if event_matches_scope(item, scope)]

    @staticmethod
    def _filter_rows_after(
        rows: list[dict[str, Any]],
        *,
        cutoff: datetime,
        timestamp_keys: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for row in rows:
            observed_at = None
            for key in timestamp_keys:
                observed_at = _coerce_datetime(row.get(key))
                if observed_at is not None:
                    break
            if observed_at is not None and observed_at >= cutoff:
                filtered.append(row)
        return filtered

    @staticmethod
    def _fee_to_notional_ratio(rows: list[dict[str, Any]]) -> Decimal | None:
        total_fees = sum((ForwardTrialGuardService._fee_cost_in_quote(item) or Decimal("0")) for item in rows)
        total_notional = sum((_to_decimal(item.get("fill_notional")) or Decimal("0")) for item in rows)
        if abs(total_notional) <= Decimal("1e-12"):
            return None
        return total_fees / total_notional

    @staticmethod
    def _fee_cost_in_quote(row: dict[str, Any]) -> Decimal | None:
        fee_quote_amount = _to_decimal(row.get("fee_quote_amount"))
        if fee_quote_amount is not None:
            return abs(fee_quote_amount)
        fee_delta = _to_decimal(row.get("fee_delta"))
        if fee_delta is not None:
            return abs(fee_delta)
        fee_amount = _to_decimal(row.get("fee_amount"))
        if fee_amount is None:
            return None
        symbol = row.get("symbol")
        side = row.get("side")
        fill_price = row.get("fill_price")
        if symbol in {None, ""} or side in {None, ""} or fill_price in {None, ""}:
            return abs(fee_amount)
        fee_cost, _fee_error = try_fill_fee_cost_in_quote(
            SimpleNamespace(
                symbol=symbol,
                fee_amount=fee_amount,
                fee_currency=row.get("fee_currency"),
                venue=row.get("venue") or "OKX",
                side=side,
                fill_price=fill_price,
            )
        )
        return None if fee_cost is None else abs(fee_cost)

    def _trial_observation_active(self) -> bool:
        mode = str(getattr(self.settings, "mode", "") or "").lower()
        config_profile = str(getattr(self.settings, "config_profile", "") or "")
        return mode == "guarded_live" or config_profile == "forward_test_small_capital"

    def _trial_observation_label(self) -> str | None:
        mode = str(getattr(self.settings, "mode", "") or "").lower()
        config_profile = str(getattr(self.settings, "config_profile", "") or "")
        if mode == "guarded_live":
            return "guarded_live_small_capital"
        if config_profile == "forward_test_small_capital":
            return "forward_test_small_capital"
        return None

    def _trigger_halt(self, snapshot: dict[str, Any]) -> None:
        reason = "trial_guard_threshold_breached"
        if not self.kill_switch.halted:
            # Stage 6 Slice 6.4：合并的 KillSwitch 内部自动处理本地 cache + 跨进程
            # 广播。worker thread 路径走 run_coroutine_threadsafe；测试 / 启动期早期
            # 未 bootstrap 时退到 local-only。无需 if/else fallback。
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
        daily_trading_net_realized: Decimal = Decimal("0"),
        daily_funding_fee_net: Decimal = Decimal("0"),
        daily_combined_net_realized: Decimal = Decimal("0"),
        consecutive_losses: int = 0,
        fee_to_notional_ratio: Decimal | None = None,
        high_slippage_ratio: float | None = None,
        slow_submit_to_fill_ratio: float | None = None,
        breaches: list[dict[str, Any]] | None = None,
        recommended_action: str | None = None,
        manual_reset_after: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_breaches = [self._breach_detail(item) for item in breaches or []]
        hard_stop_active = bool(normalized_breaches)
        observation_flow_active = bool(self.settings.trial_guard_enabled) and self._trial_observation_active()
        observation_label = self._trial_observation_label() if observation_flow_active else None
        enabled_for_runtime = observation_flow_active
        return {
            "enabled": bool(self.settings.trial_guard_enabled),
            "enabled_for_runtime": enabled_for_runtime,
            "status": status,
            "profile_label": observation_label,
            "active_config_profile": self.settings.config_profile,
            "profile_active": enabled_for_runtime,
            "trial_observation_active": observation_flow_active,
            "trial_observation_label": observation_label,
            "halted": self.kill_switch.halted,
            "reason": self.kill_switch.status().get("reason"),
            "lookback_fills": int(self.settings.trial_guard_lookback_fills),
            "min_closed_fills": int(self.settings.trial_guard_min_closed_fills),
            "fill_count": fill_count,
            "daily_net_realized": daily_combined_net_realized,
            "daily_trading_net_realized": daily_trading_net_realized,
            "daily_funding_fee_net": daily_funding_fee_net,
            "daily_combined_net_realized": daily_combined_net_realized,
            "consecutive_losses": consecutive_losses,
            "fee_to_notional_ratio": fee_to_notional_ratio,
            "high_slippage_ratio": high_slippage_ratio,
            "slow_submit_to_fill_ratio": slow_submit_to_fill_ratio,
            "breaches": normalized_breaches,
            "hard_stop": {
                "active": hard_stop_active,
                "halt_required": hard_stop_active,
                "resume_blocked": hard_stop_active,
                "breach_count": len(normalized_breaches),
                "summary": self._hard_stop_summary(status=status, breaches=normalized_breaches),
                "operator_guidance": self._operator_guidance(status=status, breaches=normalized_breaches),
            },
            "recovery_requirements": self._recovery_requirements(
                status=status,
                fill_count=fill_count,
                breaches=normalized_breaches,
            ),
            "thresholds": {
                "max_daily_loss_usdt": self.settings.trial_guard_max_daily_loss_usdt,
                "max_consecutive_losses": self.settings.trial_guard_max_consecutive_losses,
                "max_fee_to_notional_ratio": self.settings.trial_guard_max_fee_to_notional_ratio,
                "max_high_slippage_ratio": self.settings.trial_guard_max_high_slippage_ratio,
                "max_slow_submit_to_fill_ratio": self.settings.trial_guard_max_slow_submit_to_fill_ratio,
            },
            "manual_reset_active": manual_reset_after is not None,
            "manual_reset_effective_after": manual_reset_after,
            "recommended_action": recommended_action,
            "last_evaluated_at": utc_now(),
            "summary": self._summary(status=status, fill_count=fill_count, breaches=normalized_breaches),
        }

    @staticmethod
    def _summary(*, status: str, fill_count: int, breaches: list[dict[str, Any]]) -> str:
        if status == "disabled":
            return "试盘守护未启用。"
        if status == "inactive_for_runtime":
            return "试盘守护虽然已配置，但当前运行线不处在试盘观察流程中，因此这条线不会因为试盘守护而自动停机。"
        if status == "warming_up":
            return f"试盘守护仍在预热，当前已记录 {fill_count} 笔已完成成交，尚未达到正式监控所需样本量。"
        if status == "recovered":
            return "试盘守护已从 breached 状态恢复，当前不再命中硬停机阈值。"
        if status == "breached":
            return f"试盘守护已触发自动停机，原因：{', '.join(str(item['code']) for item in breaches)}。"
        return "试盘守护正在监控，当前未触发自动停机阈值。"

    @staticmethod
    def _breach_detail(item: dict[str, Any]) -> dict[str, Any]:
        code = str(item.get("code") or "trial_guard_unknown")
        value = item.get("value")
        threshold = item.get("threshold")
        title_map = {
            "trial_guard_daily_loss_limit": "最近 24 小时组合净收益触碰试盘止损线",
            "trial_guard_consecutive_losses": "连续亏损笔数达到试盘守护上限",
            "trial_guard_fee_drag_limit": "手续费拖累超过试盘守护阈值",
            "trial_guard_high_slippage_ratio": "高滑点样本占比超过试盘守护阈值",
            "trial_guard_slow_fill_ratio": "慢成交样本占比超过试盘守护阈值",
        }
        summary_map = {
            "trial_guard_daily_loss_limit": "最近 24 小时的交易净收益与资金费合计已经跌破允许的试盘亏损上限。",
            "trial_guard_consecutive_losses": "最近连续闭合成交里，亏损序列已经长到试盘守护不允许继续自动试盘的程度。",
            "trial_guard_fee_drag_limit": "最近样本里的手续费拖累已经高到继续试盘很难判断策略本身是否有效。",
            "trial_guard_high_slippage_ratio": "最近成交里高滑点样本占比过高，说明执行质量当前不适合继续自动试盘。",
            "trial_guard_slow_fill_ratio": "最近成交里 submit 到 fill 的慢成交占比过高，当前执行链路不适合继续自动试盘。",
        }
        return {
            **item,
            "title": title_map.get(code, code),
            "summary": summary_map.get(code, "当前试盘守护已经命中一条硬停机阈值。"),
            "current_value": value,
            "threshold_value": threshold,
        }

    @staticmethod
    def _hard_stop_summary(*, status: str, breaches: list[dict[str, Any]]) -> str:
        if status == "breached":
            titles = [str(item.get("title") or item.get("code") or "") for item in breaches]
            joined = "；".join(title for title in titles if title)
            return joined or "当前试盘守护已触发自动停机。"
        if status == "inactive_for_runtime":
            return "当前运行线不属于试盘观察流程，试盘守护不会在这条线上主动触发硬停机。"
        if status == "recovered":
            return "当前试盘守护已经不再命中硬停机阈值，可继续结合恢复状态判断是否允许恢复自动运行。"
        if status == "warming_up":
            return "试盘守护还在预热阶段，当前样本不足，只适合继续小资金观察。"
        if status == "disabled":
            return "试盘守护当前未启用。"
        return "当前试盘守护正在监控，尚未触发硬停机。"

    @staticmethod
    def _operator_guidance(*, status: str, breaches: list[dict[str, Any]]) -> str:
        if status == "breached":
            return "先去风险与恢复页确认试盘守护阻断，再到策略判断页查看试盘审查和最近成交，确认触发原因是否已经自然解除。"
        if status == "inactive_for_runtime":
            return "当前运行线不是试盘观察流程。如果你打算让试盘守护真正生效，请切到试盘运行线后再观察是否需要继续小资金试盘。"
        if status == "recovered":
            return "试盘守护本身已经恢复，但仍需结合恢复状态、对账和其他 blocker 决定是否允许恢复自动运行。"
        if status == "warming_up":
            return "当前样本还不够，继续保持小资金和人工盯盘，不要把预热阶段误当成可以放量。"
        if status == "disabled":
            return "如果这条线本来就是试盘场景，先确认是否应该启用试盘守护。"
        return "继续观察最近成交、费用拖累和滑点质量，确认是否还能维持小资金试盘。"

    def _recovery_requirements(
        self,
        *,
        status: str,
        fill_count: int,
        breaches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        if status == "warming_up":
            missing = max(int(self.settings.trial_guard_min_closed_fills) - int(fill_count), 0)
            items.append(
                {
                    "code": "trial_guard_warmup_sample_requirement",
                    "title": "继续积累已完成成交样本",
                    "requirement": f"至少还需要 {missing} 笔已完成成交，试盘守护才会进入正式监控。",
                }
            )
        elif status == "inactive_for_runtime":
            items.append(
                {
                    "code": "trial_guard_inactive_for_runtime",
                    "title": "当前运行线未启用试盘观察流程",
                    "requirement": "如果要让试盘守护真正介入自动停机，请切到试盘观察流程对应的运行线后再继续。",
                }
            )
        elif status == "breached":
            requirement_map = {
                "trial_guard_daily_loss_limit": "最近 24 小时组合净收益需要重新回到试盘亏损上限以上。",
                "trial_guard_consecutive_losses": "连续亏损序列需要被新的非亏损闭合成交打断。",
                "trial_guard_fee_drag_limit": "手续费拖累比例需要回落到试盘守护阈值以下。",
                "trial_guard_high_slippage_ratio": "高滑点样本占比需要回落到试盘守护阈值以下。",
                "trial_guard_slow_fill_ratio": "慢成交样本占比需要回落到试盘守护阈值以下。",
            }
            for breach in breaches:
                code = str(breach.get("code") or "")
                items.append(
                    {
                        "code": code,
                        "title": str(breach.get("title") or code),
                        "requirement": requirement_map.get(code, "当前触发的试盘守护阈值需要先自然解除，才能继续恢复自动运行。"),
                    }
                )
        elif status == "recovered":
            items.append(
                {
                    "code": "trial_guard_recovered",
                    "title": "试盘守护已恢复",
                    "requirement": "试盘守护本身已不再阻断恢复，后续只需继续确认对账、恢复状态和其他 blocker。",
                }
            )
        else:
            items.append(
                {
                    "code": "trial_guard_monitoring",
                    "title": "试盘守护正在监控",
                    "requirement": "当前没有命中试盘守护硬停机阈值，可以继续结合其他恢复条件判断是否允许运行。",
                }
            )
        return {
            "resume_allowed": status != "breached",
            "items": items,
        }
