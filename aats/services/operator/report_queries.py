from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class ReportQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def profitability_overview(self, *, limit: int = 100) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        outcomes = list(self.owner._scoped_fill_outcomes())
        outcomes.sort(key=lambda item: item.ingestion_timestamp or item.created_at, reverse=True)
        closed_rows = [self.owner._execution_quality_row(item) for item in outcomes[:normalized_limit]]
        execution_quality = self.execution_quality_report(limit=normalized_limit, offset=0)
        execution_quality_summary = dict(execution_quality.get("summary") or {})

        funding_records = list(self.owner._scoped_funding_fee_records())
        funding_records.sort(
            key=lambda item: self.owner._funding_fee_event_timestamp(item) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        funding_rows = [
            self.owner._funding_fee_profitability_row(item)
            for item in funding_records[:normalized_limit]
        ]

        realized_events = [*closed_rows, *funding_rows]
        realized_events.sort(key=self.owner._profitability_event_sort_key, reverse=True)
        realized_events = realized_events[:normalized_limit]

        gross_realized_pnl = sum(
            [
                self.owner._to_decimal(item.get("gross_realized_pnl"))
                or self.owner._to_decimal(item.get("trading_gross_realized_delta"))
                or Decimal("0")
                for item in closed_rows
            ],
            start=Decimal("0"),
        )
        net_realized_pnl = sum(
            [
                self.owner._to_decimal(item.get("realized_pnl_delta"))
                or self.owner._to_decimal(item.get("trading_net_realized_delta"))
                or Decimal("0")
                for item in closed_rows
            ],
            start=Decimal("0"),
        )
        funding_fee_net_pnl = sum(
            [self.owner._to_decimal(item.get("funding_fee_delta")) or Decimal("0") for item in funding_rows],
            start=Decimal("0"),
        )
        funding_fee_summary = self.owner._profitability_funding_fee_summary(funding_records)

        return {
            "summary": {
                "closed_fill_count": len(closed_rows),
                "gross_realized_pnl": gross_realized_pnl,
                "net_realized_pnl": net_realized_pnl,
                "funding_fee_count": len(funding_rows),
                "funding_fee_income_count": int(funding_fee_summary.get("income_count") or 0),
                "funding_fee_expense_count": int(funding_fee_summary.get("expense_count") or 0),
                "funding_fee_net_pnl": funding_fee_net_pnl,
                "combined_net_realized_pnl": net_realized_pnl + funding_fee_net_pnl,
                "fee_to_notional_ratio": execution_quality_summary.get("fee_to_notional_ratio"),
                "avg_fee_ratio": execution_quality_summary.get("avg_fee_ratio"),
                "high_slippage_count": execution_quality_summary.get("high_slippage_count"),
                "high_slippage_ratio": execution_quality_summary.get("high_slippage_ratio"),
                "slow_submit_to_fill_count": execution_quality_summary.get("slow_submit_to_fill_count"),
                "slow_submit_to_fill_ratio": execution_quality_summary.get("slow_submit_to_fill_ratio"),
            },
            "recent_closed_fills": closed_rows,
            "recent_realized_events": realized_events,
            "execution_quality": execution_quality,
            "funding_fee_summary": funding_fee_summary,
            "truth_source": "fill_outcomes_plus_funding_fee_records",
        }

    def execution_quality_report(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        source_rows = (
            self.owner._phase5_fill_rows(limit=None)
            if self.owner._phase5_control_plane_enabled()
            else list(reversed(self.owner._scoped_fills()))
        )
        rows = [self.owner._execution_quality_row(fill) for fill in source_rows]
        paged = rows[offset : offset + limit]
        return {
            "rows": paged,
            "limit": limit,
            "offset": offset,
            "total_available": len(rows),
            "has_more": offset + len(paged) < len(rows),
            "truth_source": "execution_fill_repo_v2" if self.owner._phase5_control_plane_enabled() else "execution_repo",
            "summary": self.owner._execution_quality_summary(rows),
        }

    def execution_attempt_report(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        source_rows = (
            self.owner._phase5_fill_rows(limit=None)
            if self.owner._phase5_control_plane_enabled()
            else list(reversed(self.owner._scoped_fills()))
        )
        rows = [self.owner._execution_quality_row(fill) for fill in source_rows]
        attempt_rows = self.owner._execution_attempt_rows(rows)
        paged = attempt_rows[offset : offset + limit]
        return {
            "rows": paged,
            "limit": limit,
            "offset": offset,
            "total_available": len(attempt_rows),
            "has_more": offset + len(paged) < len(attempt_rows),
            "truth_source": "execution_fill_repo_v2" if self.owner._phase5_control_plane_enabled() else "execution_repo",
            "summary": self.owner._execution_attempt_summary(rows),
        }

    def forward_validation_report(self, *, window_days: int = 7, period_count: int = 4) -> dict[str, Any]:
        normalized_window_days = max(int(window_days), 1)
        normalized_period_count = max(int(period_count), 1)
        cache_key = f"forward_validation:{self.owner._scope_cache_fragment()}:{normalized_window_days}:{normalized_period_count}"
        return self.owner._cached_ttl(
            cache_key,
            30,
            lambda: self.owner._build_forward_validation_report(
                window_days=normalized_window_days,
                period_count=normalized_period_count,
            ),
        )

    def scaling_readiness_report(
        self,
        *,
        window_days: int = 7,
        period_count: int = 4,
        forward_validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_window_days = max(int(window_days), 1)
        normalized_period_count = max(int(period_count), 1)
        if forward_validation is not None:
            return self.owner._build_scaling_readiness_report(
                window_days=normalized_window_days,
                period_count=normalized_period_count,
                forward_validation=forward_validation,
            )
        cache_key = f"scaling_readiness:{self.owner._scope_cache_fragment()}:{normalized_window_days}:{normalized_period_count}"
        return self.owner._cached_ttl(
            cache_key,
            30,
            lambda: self.owner._build_scaling_readiness_report(
                window_days=normalized_window_days,
                period_count=normalized_period_count,
                forward_validation=None,
            ),
        )

    def trial_review_packet(
        self,
        *,
        profitability_limit: int = 100,
        anomaly_limit: int = 100,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        normalized_profitability_limit = max(int(profitability_limit), 1)
        normalized_anomaly_limit = max(int(anomaly_limit), 1)
        normalized_segment_limit = max(int(segment_limit), 1)
        normalized_window_days = max(int(window_days), 1)
        normalized_period_count = max(int(period_count), 1)
        cache_key = (
            f"trial_review_packet:{self.owner._scope_cache_fragment()}:"
            f"{normalized_profitability_limit}:{normalized_anomaly_limit}:"
            f"{normalized_segment_limit}:{normalized_window_days}:{normalized_period_count}"
        )
        return self.owner._cached_ttl(
            cache_key,
            45,
            lambda: self.owner._build_trial_review_packet(
                profitability_limit=normalized_profitability_limit,
                anomaly_limit=normalized_anomaly_limit,
                segment_limit=normalized_segment_limit,
                window_days=normalized_window_days,
                period_count=normalized_period_count,
            ),
        )

    def trial_review_summary(
        self,
        *,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        normalized_segment_limit = max(int(segment_limit), 1)
        normalized_window_days = max(int(window_days), 1)
        normalized_period_count = max(int(period_count), 1)
        cache_key = f"trial_review_summary:{self.owner._scope_cache_fragment()}:{normalized_segment_limit}:{normalized_window_days}:{normalized_period_count}"
        return self.owner._cached_ttl(
            cache_key,
            30,
            lambda: self.owner._build_trial_review_summary(
                segment_limit=normalized_segment_limit,
                window_days=normalized_window_days,
                period_count=normalized_period_count,
            ),
        )

    def trial_review_details(
        self,
        *,
        profitability_limit: int = 100,
        anomaly_limit: int = 100,
        segment_limit: int = 100,
        window_days: int = 7,
        period_count: int = 4,
    ) -> dict[str, Any]:
        normalized_profitability_limit = max(int(profitability_limit), 1)
        normalized_anomaly_limit = max(int(anomaly_limit), 1)
        normalized_segment_limit = max(int(segment_limit), 1)
        normalized_window_days = max(int(window_days), 1)
        normalized_period_count = max(int(period_count), 1)
        cache_key = (
            f"trial_review_details:{self.owner._scope_cache_fragment()}:"
            f"{normalized_profitability_limit}:{normalized_anomaly_limit}:"
            f"{normalized_segment_limit}:{normalized_window_days}:{normalized_period_count}"
        )
        return self.owner._cached_ttl(
            cache_key,
            45,
            lambda: self.owner._build_trial_review_details(
                profitability_limit=normalized_profitability_limit,
                anomaly_limit=normalized_anomaly_limit,
                segment_limit=normalized_segment_limit,
                window_days=normalized_window_days,
                period_count=normalized_period_count,
            ),
        )

    def trial_review_history(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return self.owner._trial_review_history_payload(limit=limit, offset=offset)

    def execution_anomaly_report(self, *, limit: int = 100) -> dict[str, Any]:
        rows = self.execution_quality_report(limit=limit, offset=0)["rows"]
        flagged_rows: list[dict[str, Any]] = []
        summary = {
            "high_slippage_count": 0,
            "high_fee_ratio_count": 0,
            "slow_decision_to_submit_count": 0,
            "slow_submit_to_fill_count": 0,
        }
        for row in rows:
            flags: list[str] = []
            slippage = self.owner._to_decimal(row.get("adverse_slippage_bps"))
            fee_ratio = self.owner._to_decimal(row.get("fee_ratio"))
            decision_to_submit_latency_ms = row.get("decision_to_submit_latency_ms")
            submit_to_exchange_fill_latency_ms = row.get("submit_to_exchange_fill_latency_ms")

            if slippage is not None and slippage > self.owner._to_decimal(max(self.owner.runtime.settings.max_slippage_tolerance_bps * 0.5, 2)):
                flags.append("high_adverse_slippage")
                summary["high_slippage_count"] += 1
            if fee_ratio is not None and fee_ratio > self.owner._to_decimal("0.001"):
                flags.append("high_fee_ratio")
                summary["high_fee_ratio_count"] += 1
            if isinstance(decision_to_submit_latency_ms, (int, float)) and decision_to_submit_latency_ms > 10_000:
                flags.append("slow_decision_to_submit")
                summary["slow_decision_to_submit_count"] += 1
            if isinstance(submit_to_exchange_fill_latency_ms, (int, float)) and submit_to_exchange_fill_latency_ms > 10_000:
                flags.append("slow_submit_to_fill")
                summary["slow_submit_to_fill_count"] += 1
            if flags:
                flagged_rows.append({**row, "anomaly_flags": flags})

        return {
            "summary": summary,
            "rows": flagged_rows,
            "evaluated_fill_count": len(rows),
            "truth_source": "fill_outcomes",
        }
