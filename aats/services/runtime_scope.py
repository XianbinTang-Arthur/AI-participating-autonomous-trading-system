from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.execution import FillEvent, OrderState
from aats.schemas.portfolio import FillOutcomeRecord, FundingFeeRecord, PortfolioSnapshot, SleevePnLRecord, is_baseline_snapshot
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import MarginModelType, ProductType


@dataclass(frozen=True, slots=True)
class RuntimeStateScope:
    product_type: ProductType
    margin_mode: MarginModelType
    allowed_symbols: tuple[str, ...]
    default_symbol: str
    smart_arbitrage_spot_margin_modes: tuple[MarginModelType, ...] = ("cash",)

    def symbol_allowed(self, symbol: str | None) -> bool:
        if not symbol:
            return False
        if self.allowed_symbols:
            return symbol in self.allowed_symbols
        return symbol == self.default_symbol


def runtime_state_scope(settings: AATSSettings) -> RuntimeStateScope:
    smart_arbitrage_spot_margin_modes: tuple[MarginModelType, ...] = ("cash",)
    configured_margin_mode = getattr(settings, "smart_arbitrage_margin_short_spot_margin_mode", "cross")
    if (
        settings.smart_arbitrage_margin_short_enabled
        or settings.smart_arbitrage_negative_basis_mode == "margin_backed"
    ) and configured_margin_mode in {"cross", "isolated"}:
        smart_arbitrage_spot_margin_modes = ("cash", configured_margin_mode)
    return RuntimeStateScope(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
        allowed_symbols=settings.expanded_allowed_symbols(),
        default_symbol=settings.default_symbol,
        smart_arbitrage_spot_margin_modes=smart_arbitrage_spot_margin_modes,
    )


def filter_order_states(order_states: list[OrderState], scope: RuntimeStateScope) -> list[OrderState]:
    return [order for order in order_states if order_state_matches_scope(order, scope)]


def filter_fills(fills: list[FillEvent], scope: RuntimeStateScope) -> list[FillEvent]:
    return [fill for fill in fills if fill_event_matches_scope(fill, scope)]


def filter_fill_outcomes(outcomes: list[FillOutcomeRecord], scope: RuntimeStateScope) -> list[FillOutcomeRecord]:
    return [outcome for outcome in outcomes if fill_outcome_matches_scope(outcome, scope)]


def filter_funding_fee_records(records: list[FundingFeeRecord], scope: RuntimeStateScope) -> list[FundingFeeRecord]:
    return [record for record in records if funding_fee_record_matches_scope(record, scope)]


def latest_matching_snapshot(
    snapshots: list[PortfolioSnapshot],
    scope: RuntimeStateScope,
) -> PortfolioSnapshot | None:
    for snapshot in reversed(snapshots):
        if portfolio_snapshot_matches_scope(snapshot, scope):
            return snapshot
    return None


def snapshots_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    limit: int | None = None,
) -> list[PortfolioSnapshot]:
    if hasattr(repo, "history_for_scope"):
        return repo.history_for_scope(scope=scope, limit=limit)
    rows = filter_snapshots(repo.history(), scope)
    if limit is not None:
        rows = rows[-limit:]
    return rows


def latest_snapshot_for_scope(repo, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
    if hasattr(repo, "latest_for_scope"):
        return repo.latest_for_scope(scope=scope)
    return latest_matching_snapshot(repo.history(), scope)


def latest_baseline_for_scope(repo, scope: RuntimeStateScope) -> PortfolioSnapshot | None:
    """Return the most recent baseline snapshot matching *scope*.

    If the repo implements ``latest_baseline_for_scope`` (e.g. Postgres with
    a JSON-aware query), delegate to it.  Otherwise fall back to scanning all
    scoped snapshots and filtering by ``is_baseline_snapshot``.
    """
    repo_method = getattr(repo, "latest_baseline_for_scope", None)
    if callable(repo_method):
        return repo_method(scope=scope)
    candidates = [s for s in snapshots_for_scope(repo, scope) if is_baseline_snapshot(s)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (s.snapshot_ts, s.created_at))


def filter_snapshots(
    snapshots: list[PortfolioSnapshot],
    scope: RuntimeStateScope,
) -> list[PortfolioSnapshot]:
    return [snapshot for snapshot in snapshots if portfolio_snapshot_matches_scope(snapshot, scope)]


def latest_matching_reconciliation(
    reports: list[ReconciliationReport],
    scope: RuntimeStateScope,
) -> ReconciliationReport | None:
    for report in reversed(reports):
        if reconciliation_report_matches_scope(report, scope):
            return report
    return None


def reconciliation_reports_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    limit: int | None = None,
) -> list[ReconciliationReport]:
    if hasattr(repo, "history_for_scope"):
        return repo.history_for_scope(scope=scope, limit=limit)
    rows = [report for report in repo.history() if reconciliation_report_matches_scope(report, scope)]
    if limit is not None:
        rows = rows[-limit:]
    return rows


def latest_reconciliation_for_scope(repo, scope: RuntimeStateScope) -> ReconciliationReport | None:
    if hasattr(repo, "latest_for_scope"):
        return repo.latest_for_scope(scope=scope)
    return latest_matching_reconciliation(repo.history(), scope)


def execution_truth_repo_for_runtime(runtime: Any):
    """Return the current execution fact read model for runtime diagnostics.

    ``runtime.execution_repo`` can still be the legacy write-path repository in
    transitional live profiles. Read-only diagnostics should prefer the
    dedicated execution truth repository when bootstrap provides one.
    """

    truth_repo = getattr(runtime, "execution_truth_repo", None)
    if truth_repo is not None:
        return truth_repo
    reconciliation_repo = getattr(runtime, "reconciliation_execution_repo", None)
    if reconciliation_repo is not None:
        return reconciliation_repo
    return getattr(runtime, "execution_repo", None)


def portfolio_snapshot_matches_scope(
    snapshot: PortfolioSnapshot,
    scope: RuntimeStateScope,
) -> bool:
    if snapshot.product_type != scope.product_type:
        return False
    if snapshot.margin_mode != scope.margin_mode:
        return False
    # Portfolio snapshots represent account-wide state. Extra balances or positions in
    # the same product/margin posture should not make the current runtime ignore the
    # snapshot for recovery or decision context bootstrapping.
    return True


def reconciliation_report_matches_scope(
    report: ReconciliationReport,
    scope: RuntimeStateScope,
) -> bool:
    if report.product_type is None or report.margin_mode is None:
        return scope.product_type == "spot" and scope.margin_mode == "cash"
    if report.product_type != scope.product_type:
        return False
    if report.margin_mode != scope.margin_mode:
        return False
    if not report.allowed_symbols:
        return True
    return all(scope.symbol_allowed(symbol) for symbol in report.allowed_symbols)


def order_state_matches_scope(
    order: OrderState,
    scope: RuntimeStateScope,
) -> bool:
    if not scope.symbol_allowed(order.symbol):
        return False
    order_product_type = inferred_order_state_product_type(order)
    order_margin_mode = inferred_order_state_margin_mode(order)
    if scope.product_type == "derivatives" and getattr(order, "strategy_family", None) == "smart_arbitrage":
        if order_product_type == "spot":
            return order_margin_mode in scope.smart_arbitrage_spot_margin_modes
        return order_margin_mode == scope.margin_mode
    if order_product_type != scope.product_type:
        return False
    return order_margin_mode == scope.margin_mode


def order_states_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = None,
    open_only: bool = False,
) -> list[OrderState]:
    if hasattr(repo, "order_states_for_scope"):
        return repo.order_states_for_scope(
            scope=scope,
            statuses=statuses,
            limit=limit,
            open_only=open_only,
        )
    rows = repo.open_order_states() if open_only else repo.order_states()
    rows = filter_order_states(rows, scope)
    if statuses is not None:
        allowed = {status.upper() for status in statuses}
        rows = [row for row in rows if row.status.upper() in allowed]
    if limit is not None:
        rows = rows[-limit:]
    return rows


def fill_event_matches_scope(
    fill: FillEvent,
    scope: RuntimeStateScope,
) -> bool:
    if not scope.symbol_allowed(fill.symbol):
        return False
    if scope.product_type == "derivatives" and getattr(fill, "strategy_family", None) == "smart_arbitrage":
        if fill.product_type == "spot":
            return fill.margin_mode in scope.smart_arbitrage_spot_margin_modes
        return fill.margin_mode == scope.margin_mode
    if fill.product_type != scope.product_type:
        return False
    return fill.margin_mode == scope.margin_mode


def fills_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    since=None,
    limit: int | None = None,
) -> list[FillEvent]:
    if hasattr(repo, "fills_for_scope"):
        return repo.fills_for_scope(scope=scope, since=since, limit=limit)
    rows = repo.fills_since(since=since) if since is not None else repo.fills()
    rows = filter_fills(rows, scope)
    if limit is not None:
        rows = rows[-limit:]
    return rows


def fill_outcome_matches_scope(
    outcome: FillOutcomeRecord,
    scope: RuntimeStateScope,
) -> bool:
    if not scope.symbol_allowed(outcome.symbol):
        return False
    if scope.product_type == "derivatives" and getattr(outcome, "strategy_family", None) == "smart_arbitrage":
        if outcome.product_type == "spot":
            return outcome.margin_mode in scope.smart_arbitrage_spot_margin_modes
        return outcome.margin_mode == scope.margin_mode
    if outcome.product_type != scope.product_type:
        return False
    return outcome.margin_mode == scope.margin_mode


def fill_outcomes_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    since=None,
    limit: int | None = None,
) -> list[FillOutcomeRecord]:
    if hasattr(repo, "outcomes_for_scope"):
        return repo.outcomes_for_scope(scope=scope, since=since, limit=limit)
    rows = repo.outcomes()
    if since is not None:
        rows = [outcome for outcome in rows if outcome.created_at >= since]
    rows = filter_fill_outcomes(rows, scope)
    if limit is not None:
        rows = rows[-limit:]
    return rows


def funding_fee_record_matches_scope(
    record: FundingFeeRecord,
    scope: RuntimeStateScope,
) -> bool:
    if record.product_type != scope.product_type:
        return False
    if record.margin_mode != scope.margin_mode:
        return False
    if record.symbol in {None, ""}:
        return True
    return scope.symbol_allowed(record.symbol)


def funding_fee_records_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    since=None,
    limit: int | None = None,
) -> list[FundingFeeRecord]:
    if hasattr(repo, "records_for_scope"):
        return repo.records_for_scope(scope=scope, since=since, limit=limit)
    rows = repo.records()
    if since is not None:
        rows = [record for record in rows if record.created_at >= since]
    rows = filter_funding_fee_records(rows, scope)
    if limit is not None:
        rows = rows[-limit:]
    return rows


def sleeve_pnl_record_matches_scope(
    record: SleevePnLRecord,
    scope: RuntimeStateScope,
) -> bool:
    if record.product_type != scope.product_type:
        return False
    if record.margin_mode != scope.margin_mode:
        return False
    if record.symbol in {None, ""}:
        return True
    return scope.symbol_allowed(record.symbol)


def filter_sleeve_pnl_records(
    records: list[SleevePnLRecord],
    scope: RuntimeStateScope,
) -> list[SleevePnLRecord]:
    return [record for record in records if sleeve_pnl_record_matches_scope(record, scope)]


def sleeve_pnl_records_for_scope(
    repo,
    scope: RuntimeStateScope,
    *,
    since=None,
    limit: int | None = None,
) -> list[SleevePnLRecord]:
    if hasattr(repo, "records_for_scope"):
        return repo.records_for_scope(scope=scope, since=since, limit=limit)
    rows = repo.records()
    if since is not None:
        rows = [record for record in rows if record.created_at >= since]
    rows = filter_sleeve_pnl_records(rows, scope)
    if limit is not None:
        rows = rows[-limit:]
    return rows


def inferred_order_state_product_type(order: OrderState) -> ProductType:
    payload = order.submission_payload or {}
    payload_product_type = payload.get("productType")
    if payload_product_type in {"spot", "derivatives"}:
        return payload_product_type  # type: ignore[return-value]
    symbol = order.symbol or str(payload.get("instId", ""))
    inferred = infer_product_type_from_symbol(symbol)
    explicit = getattr(order, "product_type", None)
    if explicit == "derivatives":
        return "derivatives"
    if inferred == "derivatives":
        return "derivatives"
    return "spot"


def inferred_order_state_margin_mode(order: OrderState) -> MarginModelType:
    payload = order.submission_payload or {}
    payload_margin_mode = payload.get("marginMode")
    if payload_margin_mode in {"cash", "cross", "isolated"}:
        return payload_margin_mode  # type: ignore[return-value]
    td_mode = payload.get("tdMode")
    if td_mode in {"cash", "cross", "isolated"}:
        return td_mode  # type: ignore[return-value]
    explicit = getattr(order, "margin_mode", None)
    if explicit in {"cash", "cross", "isolated"}:
        if inferred_order_state_product_type(order) == "derivatives" and explicit == "cash":
            return "cross"
        return explicit
    if inferred_order_state_product_type(order) == "derivatives":
        return "cross"
    return "cash"


def infer_product_type_from_symbol(symbol: str | None) -> ProductType:
    if not symbol:
        return "spot"
    normalized = symbol.upper()
    if normalized.endswith("-SWAP"):
        return "derivatives"
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return "derivatives"
    return "spot"


def scoped_portfolio_event(events: list[Any], scope: RuntimeStateScope) -> Any | None:
    for event in reversed(events):
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        try:
            snapshot = PortfolioSnapshot.model_validate(payload)
        except Exception:
            continue
        if portfolio_snapshot_matches_scope(snapshot, scope):
            return event
    return None


def topic_events_for_scope(
    event_store,
    topic: str,
    scope: RuntimeStateScope,
    *,
    limit: int | None = None,
) -> list[Any]:
    if hasattr(event_store, "by_topic_scoped"):
        return event_store.by_topic_scoped(topic, scope=scope, limit=limit)
    rows = event_store.by_topic(topic)
    rows = [event for event in rows if _event_matches_scope(event, scope)]
    if limit is not None:
        rows = rows[-limit:]
    return rows


def latest_topic_event_for_scope(event_store, topic: str, scope: RuntimeStateScope, *, key: str | None = None):
    if hasattr(event_store, "latest_by_topic_scoped"):
        return event_store.latest_by_topic_scoped(topic, scope=scope, key=key)
    rows = topic_events_for_scope(event_store, topic, scope)
    if key is not None:
        rows = [event for event in rows if event.key == key]
    return rows[-1] if rows else None


def event_matches_scope(event: Any, scope: RuntimeStateScope) -> bool:
    return _event_matches_scope(event, scope)


def _event_matches_scope(event: Any, scope: RuntimeStateScope) -> bool:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return False
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    symbol = payload.get("symbol") or details.get("symbol")
    allowed_symbols = payload.get("allowed_symbols") or details.get("allowed_symbols")
    product_type = payload.get("product_type") or details.get("product_type")
    margin_mode = payload.get("margin_mode") or details.get("margin_mode")
    strategy_family = payload.get("strategy_family")
    if product_type is None and isinstance(symbol, str):
        product_type = infer_product_type_from_symbol(symbol)
    if margin_mode is None and product_type == "spot":
        margin_mode = "cash"
    if scope.product_type == "derivatives" and strategy_family == "smart_arbitrage":
        if product_type == "spot":
            if margin_mode not in scope.smart_arbitrage_spot_margin_modes:
                return False
        else:
            if product_type is not None and product_type != scope.product_type:
                return False
            if margin_mode is not None and margin_mode != scope.margin_mode:
                return False
    else:
        if product_type is not None and product_type != scope.product_type:
            return False
        if margin_mode is not None and margin_mode != scope.margin_mode:
            return False
    if isinstance(symbol, str):
        return scope.symbol_allowed(symbol)
    if isinstance(allowed_symbols, list) and allowed_symbols:
        return all(isinstance(item, str) and scope.symbol_allowed(item) for item in allowed_symbols)
    return True
