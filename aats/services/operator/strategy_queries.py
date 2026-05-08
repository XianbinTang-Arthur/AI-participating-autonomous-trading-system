from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aats.services.operator._parallel import parallel_fetch

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class StrategyQueryFacade:
    _SEGMENT_DIMENSIONS = frozenset({
        "symbol", "market_regime", "volatility_state", "timeframe", "side",
        "execution_action", "position_intent", "active_profile_id",
        "exit_attribution", "risk_protection",
    })

    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def strategy_runtime(self, *, limit: int = 10) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        cache_key = f"strategy_runtime:{self.owner._scope_cache_fragment()}:{normalized_limit}"
        return self.owner._cached_ttl(
            cache_key,
            30,
            lambda: self.owner._build_strategy_runtime(limit=normalized_limit),
        )

    def strategy_runtime_dashboard(self, *, limit: int = 10) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        cache_key = f"strategy_runtime_dashboard:{self.owner._scope_cache_fragment()}:{normalized_limit}"
        return self.owner._cached_ttl(
            cache_key,
            30,
            lambda: self.owner._build_strategy_runtime(
                limit=normalized_limit,
                dashboard_summary_only=True,
            ),
        )

    def strategy_segment_report(
        self,
        *,
        limit: int = 200,
        group_by: tuple[str, ...] = ("symbol", "market_regime", "side", "execution_action"),
    ) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        normalized_group_by_raw = tuple(
            item for item in group_by if item in self._SEGMENT_DIMENSIONS
        ) or ("symbol",)
        cache_key = (
            f"strategy_segment_report:{self.owner._scope_cache_fragment()}:"
            f"{normalized_limit}:{','.join(normalized_group_by_raw)}"
        )
        return self.owner._cached_ttl(
            cache_key,
            60,
            lambda: self._build_strategy_segment_report(limit=normalized_limit, group_by=normalized_group_by_raw),
        )

    def _build_strategy_segment_report(
        self,
        *,
        limit: int = 200,
        group_by: tuple[str, ...] = ("symbol", "market_regime", "side", "execution_action"),
    ) -> dict[str, Any]:
        normalized_group_by = tuple(item for item in group_by if item in self._SEGMENT_DIMENSIONS) or ("symbol",)
        outcomes = list(self.owner._scoped_fill_outcomes())
        outcomes.sort(key=lambda item: item.ingestion_timestamp or item.created_at, reverse=True)
        rows = [self.owner._execution_quality_row(item) for item in outcomes[:limit]]
        grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            segment_payload = {
                "symbol": row.get("symbol"),
                "market_regime": row.get("market_regime"),
                "volatility_state": row.get("volatility_state"),
                "timeframe": (row.get("decision_context") or {}).get("timeframe"),
                "side": row.get("side"),
                "execution_action": row.get("execution_action"),
                "position_intent": row.get("position_intent"),
                "active_profile_id": row.get("active_profile_id"),
                "exit_attribution": row.get("exit_attribution"),
                "risk_protection": row.get("risk_protection"),
            }
            group_key = tuple(segment_payload.get(key) for key in normalized_group_by)
            bucket = grouped.setdefault(
                group_key,
                {
                    "segment": {key: segment_payload.get(key) for key in normalized_group_by},
                    "fill_count": 0,
                    "winning_fill_count": 0,
                    "losing_fill_count": 0,
                    "gross_realized_pnl": Decimal("0"),
                    "net_realized_pnl": Decimal("0"),
                    "total_fees": Decimal("0"),
                    "total_notional": Decimal("0"),
                    "total_adverse_slippage_bps": Decimal("0"),
                    "slippage_observation_count": 0,
                },
            )
            realized = self.owner._to_decimal(row.get("realized_pnl_delta")) or Decimal("0")
            gross = self.owner._to_decimal(row.get("gross_realized_pnl")) or Decimal("0")
            fees = self.owner._to_decimal(row.get("fee_quote_amount")) or Decimal("0")
            notional = self.owner._to_decimal(row.get("fill_notional")) or Decimal("0")
            slippage = self.owner._to_decimal(row.get("adverse_slippage_bps"))
            bucket["fill_count"] += 1
            bucket["gross_realized_pnl"] += gross
            bucket["net_realized_pnl"] += realized
            bucket["total_fees"] += fees
            bucket["total_notional"] += notional
            if realized > self.owner._DECIMAL_EPSILON:
                bucket["winning_fill_count"] += 1
            elif realized < -self.owner._DECIMAL_EPSILON:
                bucket["losing_fill_count"] += 1
            if slippage is not None:
                bucket["total_adverse_slippage_bps"] += slippage
                bucket["slippage_observation_count"] += 1

        segments: list[dict[str, Any]] = []
        for bucket in grouped.values():
            fill_count = int(bucket["fill_count"])
            total_notional = bucket["total_notional"]
            slippage_observation_count = int(bucket["slippage_observation_count"])
            segments.append(
                {
                    "segment": bucket["segment"],
                    "fill_count": fill_count,
                    "winning_fill_count": int(bucket["winning_fill_count"]),
                    "losing_fill_count": int(bucket["losing_fill_count"]),
                    "win_rate": None if fill_count == 0 else round(int(bucket["winning_fill_count"]) / fill_count, 6),
                    "gross_realized_pnl": bucket["gross_realized_pnl"],
                    "net_realized_pnl": bucket["net_realized_pnl"],
                    "total_fees": bucket["total_fees"],
                    "total_notional": total_notional,
                    "fee_to_notional_ratio": None if abs(total_notional) <= self.owner._DECIMAL_EPSILON else bucket["total_fees"] / total_notional,
                    "avg_adverse_slippage_bps": None if slippage_observation_count == 0 else bucket["total_adverse_slippage_bps"] / Decimal(slippage_observation_count),
                }
            )
        segments.sort(
            key=lambda item: (
                self.owner._to_decimal(item.get("net_realized_pnl")) or Decimal("0"),
                item.get("fill_count") or 0,
            ),
            reverse=True,
        )
        return {
            "group_by": list(normalized_group_by),
            "segments": segments,
            "total_available": len(segments),
            "source_fill_count": len(rows),
            "truth_source": "fill_outcomes",
        }

    def strategy_attribution_report(self, *, limit: int = 200) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        cache_key = f"strategy_attribution_report:{self.owner._scope_cache_fragment()}:{normalized_limit}"
        return self.owner._cached_ttl(
            cache_key,
            60,
            lambda: self._build_strategy_attribution_report(limit=normalized_limit),
        )

    def strategy_attribution_dashboard(self, *, limit: int = 200) -> dict[str, Any]:
        normalized_limit = max(int(limit), 1)
        cache_key = f"strategy_attribution_dashboard:{self.owner._scope_cache_fragment()}:{normalized_limit}"
        return self.owner._cached_ttl(
            cache_key,
            60,
            lambda: self._build_strategy_attribution_dashboard(limit=normalized_limit),
        )

    def _build_strategy_attribution_report(self, *, limit: int) -> dict[str, Any]:
        r = parallel_fetch({
            "sleeve_records": lambda: list(self.owner._scoped_sleeve_pnl_records()),
            "outcomes": lambda: list(self.owner._scoped_fill_outcomes()),
            "inventory_summary": self.owner._strategy_sleeve_inventory_summary,
        })
        return self._strategy_attribution_payload(
            sleeve_records=r["sleeve_records"],
            outcomes=r["outcomes"],
            inventory_summary=r["inventory_summary"],
            limit=limit,
            truth_source="sleeve_pnl_records_plus_fill_outcomes_plus_decision_audit",
            dashboard_summary_only=False,
        )

    def _build_strategy_attribution_dashboard(self, *, limit: int) -> dict[str, Any]:
        r = parallel_fetch({
            "sleeve_records": lambda: list(self.owner._scoped_sleeve_pnl_records_recent(limit=limit)),
            "outcomes": lambda: list(self.owner._scoped_fill_outcomes_recent(limit=limit)),
            "inventory_summary": self.owner._strategy_sleeve_inventory_summary,
        })
        return self._strategy_attribution_payload(
            sleeve_records=r["sleeve_records"],
            outcomes=r["outcomes"],
            inventory_summary=r["inventory_summary"],
            limit=limit,
            truth_source="sleeve_pnl_records_recent_plus_fill_outcomes_recent_dashboard_summary",
            dashboard_summary_only=True,
        )

    def _strategy_attribution_payload(
        self,
        *,
        sleeve_records: list[Any],
        outcomes: list[Any],
        inventory_summary: list[dict[str, Any]],
        limit: int,
        truth_source: str,
        dashboard_summary_only: bool,
    ) -> dict[str, Any]:
        sleeve_records.sort(key=lambda item: item.event_timestamp or item.created_at, reverse=True)
        sleeve_rows = sleeve_records[:limit]
        outcomes.sort(key=lambda item: item.ingestion_timestamp or item.created_at, reverse=True)
        rows = [self.owner._execution_quality_row(item) for item in outcomes[:limit]]

        def _bucket_by(key: str, fallback: str) -> list[dict[str, Any]]:
            buckets: dict[str, dict[str, Any]] = {}
            for row in rows:
                bucket_key = str(row.get(key) or fallback)
                bucket = buckets.setdefault(
                    bucket_key,
                    {
                        key: bucket_key,
                        "fill_count": 0,
                        "net_realized_pnl": Decimal("0"),
                        "gross_realized_pnl": Decimal("0"),
                        "total_notional": Decimal("0"),
                        "winning_fill_count": 0,
                        "losing_fill_count": 0,
                    },
                )
                realized = self.owner._to_decimal(row.get("realized_pnl_delta")) or Decimal("0")
                gross = self.owner._to_decimal(row.get("gross_realized_pnl")) or Decimal("0")
                notional = self.owner._to_decimal(row.get("fill_notional")) or Decimal("0")
                bucket["fill_count"] += 1
                bucket["net_realized_pnl"] += realized
                bucket["gross_realized_pnl"] += gross
                bucket["total_notional"] += notional
                if realized > self.owner._DECIMAL_EPSILON:
                    bucket["winning_fill_count"] += 1
                elif realized < -self.owner._DECIMAL_EPSILON:
                    bucket["losing_fill_count"] += 1
            payload = list(buckets.values())
            payload.sort(
                key=lambda item: (
                    self.owner._to_decimal(item.get("net_realized_pnl")) or Decimal("0"),
                    item.get("fill_count") or 0,
                ),
                reverse=True,
            )
            return payload

        protected_rows = [row for row in rows if bool(row.get("risk_protection_active"))]
        unprotected_rows = [row for row in rows if not bool(row.get("risk_protection_active"))]
        constraint_counts: dict[str, int] = {}
        rejection_counts: dict[str, int] = {}
        for row in protected_rows:
            for code in row.get("risk_constraints_applied") or []:
                constraint_counts[str(code)] = constraint_counts.get(str(code), 0) + 1
            for code in row.get("risk_rejection_reasons") or []:
                rejection_counts[str(code)] = rejection_counts.get(str(code), 0) + 1

        top_inventory_sleeve = inventory_summary[0] if inventory_summary else None

        return {
            "summary": {
                "fill_count": len(rows),
                "sleeve_pnl_record_count": len(sleeve_rows),
                "protected_fill_count": len(protected_rows),
                "unprotected_fill_count": len(unprotected_rows),
                "protected_net_realized_pnl": sum((self.owner._to_decimal(item.get("realized_pnl_delta")) or Decimal("0")) for item in protected_rows),
                "unprotected_net_realized_pnl": sum((self.owner._to_decimal(item.get("realized_pnl_delta")) or Decimal("0")) for item in unprotected_rows),
                "combined_net_realized_pnl": sum(((self.owner._to_decimal(item.realized_pnl) or Decimal("0")) + (self.owner._to_decimal(item.funding_fee_amount) or Decimal("0"))) for item in sleeve_rows),
                "funding_fee_net_pnl": sum((self.owner._to_decimal(item.funding_fee_amount) or Decimal("0")) for item in sleeve_rows),
                "top_inventory_sleeve_id": None if top_inventory_sleeve is None else top_inventory_sleeve.get("strategy_sleeve_id"),
                "top_inventory_notional": None if top_inventory_sleeve is None else top_inventory_sleeve.get("inventory_notional"),
            },
            "profitability_by_strategy_sleeve": self.owner._strategy_pnl_bucket_rows(records=sleeve_rows, key_name="strategy_sleeve_id", fallback="unassigned"),
            "profitability_by_allocation": self.owner._strategy_pnl_bucket_rows(records=sleeve_rows, key_name="allocation_id", fallback="unassigned"),
            "profitability_by_strategy_bundle": self.owner._strategy_pnl_bucket_rows(records=sleeve_rows, key_name="strategy_bundle_id", fallback="unassigned"),
            "profitability_by_attribution_type": self.owner._strategy_pnl_bucket_rows(records=sleeve_rows, key_name="attribution_type", fallback="unknown"),
            "profitability_by_strategy_family": self.owner._strategy_pnl_bucket_rows(records=sleeve_rows, key_name="strategy_family", fallback="unknown"),
            "profitability_by_regime": _bucket_by("market_regime", "unknown"),
            "profitability_by_profile": _bucket_by("active_profile_id", "unknown"),
            "profitability_by_strategy_route_action": _bucket_by("strategy_route_action", "unknown"),
            "profitability_by_exit_attribution": _bucket_by("exit_attribution", "unknown"),
            "fill_profitability_by_exit_attribution": _bucket_by("exit_attribution", "unknown"),
            "fill_profitability_by_risk_protection": _bucket_by("risk_protection", "unprotected"),
            "sleeve_inventory_summary": inventory_summary,
            "risk_protection_summary": {
                "top_constraint_codes": [{"code": key, "count": value} for key, value in sorted(constraint_counts.items(), key=lambda item: (-item[1], item[0]))[:10]],
                "top_rejection_codes": [{"code": key, "count": value} for key, value in sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))[:10]],
                "constraints": [{"reason_code": key, "count": value} for key, value in sorted(constraint_counts.items(), key=lambda item: (-item[1], item[0]))[:10]],
                "rejections": [{"reason_code": key, "count": value} for key, value in sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))[:10]],
            },
            "source_fill_count": len(rows),
            "dashboard_summary_only": dashboard_summary_only,
            "truth_source": truth_source,
        }
