"""Evidence scorecard emitter for backtest CLI.

从一次 :class:`BacktestResult` 出发，派生一个结构化 JSON scorecard，供
``docs/governance/alpha_evidence_gate.md`` 要求的人工 research 评审消费。

严格 scope
----------
* 本模块只做**数值**派生：IR / hit rate / drawdown / cost breakdown / fills。
* **绝不**输出 verdict / go / no-go / pass / fail / archive 一类的判定字段或
  文案 —— gate 本身要求"人类最终拍板", 自动化不得隐含结论。
* 纯函数、无 I/O、无 logging, 不修改 backtest 运行语义。

顶层键（v2 schema）
-------------------
``artifact_kind`` / ``artifact_schema_version`` / ``meta`` / ``oos`` /
``cross_window`` / ``cost_adjusted`` / ``regime_slice``

每段的具体字段见各 builder 的 docstring。所有时间戳用 UTC ISO-8601。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from aats.data_platform.replay.backtest.cost_validator import CostDiagnostic
from aats.data_platform.replay.backtest.equity_builder import (
    REPLAY_RISK_METRIC_POLICY_ID,
    EquityPoint,
)
from aats.data_platform.replay.backtest.harness import (
    BacktestResult,
    validate_backtest_result_units,
)
from aats.data_platform.replay.backtest.numeric import (
    finite_float,
    validate_finite_numbers,
)
from aats.domain.instrument_contract import (
    INSTRUMENT_ARITHMETIC_POLICY_ID,
    instrument_arithmetic_context,
)


_BPS_DENOM = Decimal("10000")
_DEFAULT_CROSS_WINDOW_SLICES = 3
_HIT_EPS = 1e-12
_MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000  # calendar year (crypto trades 24/7)
SCORECARD_ARTIFACT_KIND = "backtest_evidence_scorecard"
SCORECARD_SCHEMA_VERSION = "backtest-evidence-scorecard/v2"
SCORECARD_FILL_MODEL_VERSION = "ohlcv_participation_cap_contract_v3"
SCORECARD_TOP_LEVEL_KEYS = frozenset(
    {
        "artifact_kind",
        "artifact_schema_version",
        "meta",
        "oos",
        "cross_window",
        "cost_adjusted",
        "regime_slice",
    }
)
SCORECARD_META_KEYS = frozenset(
    {
        "symbol",
        "timeframe",
        "dataset_version",
        "family",
        "order_type",
        "execution_model_version",
        "fill_model_version",
        "spot_buy_fee_asset",
        "market_data_granularity",
        "execution_realism_limitations",
        "instrument_arithmetic_policy_id",
        "contract_lineage_status",
        "instrument_contract_fingerprint",
        "instrument_contract",
        "settlement_currency",
        "start_ts",
        "end_ts",
        "generated_at",
        "total_bars",
        "total_fills",
        "total_decisions",
        "resolved_parameters",
        "adapter_identity",
        "adapter_algorithm_version",
        "fill_attribution_status",
        "cadence_gap_count",
        "risk_metric_policy_id",
    }
)
SCORECARD_INSTRUMENT_CONTRACT_KEYS = frozenset(
    {
        "symbol",
        "instrument_type",
        "contract_type",
        "base_currency",
        "quote_currency",
        "settle_currency",
        "contract_value",
        "contract_multiplier",
        "contract_value_currency",
        "lot_size",
        "min_size",
        "tick_size",
    }
)
SCORECARD_RESOLVED_PARAMETER_KEYS = frozenset(
    {
        "min_confirm_ticks",
        "score_stability_threshold",
        "min_safe_net_edge_bps",
        "signal_edge_scale_bps",
        "directional_trend_weight",
        "directional_return_clamp_bps",
        "entry_threshold",
        "close_threshold",
        "scale_in_threshold",
        "short_entry_threshold",
        "short_close_threshold",
        "strategy_short_bias_enabled",
        "min_hold_seconds",
        "rebalance_cooldown_seconds",
        "max_thesis_age_seconds",
        "de_risk_net_edge_bps",
        "failed_thesis_net_edge_bps",
        "catastrophic_failed_thesis_buffer_bps",
        "expected_slippage_buffer_bps",
        "expected_execution_buffer_bps",
        "max_acceptable_cost_bps",
        "min_score_drawdown_bps",
        "min_liquidity_quality",
        "limit_offset_bps_entry",
        "noise_buffer_bps",
        "cost_config",
        "extra",
    }
)
SCORECARD_RESOLVED_COST_KEYS = frozenset(
    {
        "taker_fee_bps",
        "slippage_bps",
        "maker_fee_bps",
        "execution_style",
        "passive_bias",
        "maker_taker_bias",
    }
)
SCORECARD_OOS_KEYS = frozenset({"split_method", "split_ts", "train", "test"})
SCORECARD_SLICE_KEYS = frozenset(
    {
        "start",
        "end",
        "ir",
        "ir_annualized",
        "sharpe_ratio",
        "hit_rate",
        "fills",
        "sample_n",
        "max_drawdown_bps",
    }
)
SCORECARD_COST_BUCKET_KEYS = frozenset(
    {
        "realized_edge_bps",
        "fee_bps",
        "slip_bps",
        "exec_buffer_bps",
        "net_edge_bps",
    }
)
SCORECARD_COST_ADJUSTED_KEYS = frozenset(
    {*SCORECARD_COST_BUCKET_KEYS, "train", "test", "sensitivity"}
)
SCORECARD_SENSITIVITY_KEYS = frozenset({"overall", "train", "test"})
SCORECARD_SENSITIVITY_BUCKET_KEYS = frozenset(
    {
        "net_edge_fee_up_20pct_bps",
        "net_edge_slip_plus_0_5bps_bps",
    }
)
SCORECARD_REGIME_KEYS = frozenset({"vol"})
SCORECARD_VOL_KEYS = frozenset({"low", "high"})
SCORECARD_REGIME_BUCKET_KEYS = frozenset({"ir", "fills", "sample_n"})


def _has_exact_nested_scorecard_schema(scorecard: dict[str, Any]) -> bool:
    oos = scorecard.get("oos")
    cross = scorecard.get("cross_window")
    cost = scorecard.get("cost_adjusted")
    regime = scorecard.get("regime_slice")
    if (
        not isinstance(oos, dict)
        or set(oos) != SCORECARD_OOS_KEYS
        or any(
            not isinstance(oos.get(name), dict)
            or set(oos[name]) != SCORECARD_SLICE_KEYS
            for name in ("train", "test")
        )
        or not isinstance(cross, list)
        or len(cross) < _DEFAULT_CROSS_WINDOW_SLICES
        or any(
            not isinstance(window, dict)
            or set(window) != SCORECARD_SLICE_KEYS
            for window in cross
        )
        or not isinstance(cost, dict)
        or set(cost) != SCORECARD_COST_ADJUSTED_KEYS
        or any(
            not isinstance(cost.get(name), dict)
            or set(cost[name]) != SCORECARD_COST_BUCKET_KEYS
            for name in ("train", "test")
        )
    ):
        return False
    sensitivity = cost.get("sensitivity")
    if (
        not isinstance(sensitivity, dict)
        or set(sensitivity) != SCORECARD_SENSITIVITY_KEYS
        or any(
            not isinstance(sensitivity.get(name), dict)
            or set(sensitivity[name]) != SCORECARD_SENSITIVITY_BUCKET_KEYS
            for name in SCORECARD_SENSITIVITY_KEYS
        )
    ):
        return False
    vol = regime.get("vol") if isinstance(regime, dict) else None
    return bool(
        isinstance(regime, dict)
        and set(regime) == SCORECARD_REGIME_KEYS
        and isinstance(vol, dict)
        and set(vol) == SCORECARD_VOL_KEYS
        and all(
            isinstance(vol.get(name), dict)
            and set(vol[name]) == SCORECARD_REGIME_BUCKET_KEYS
            for name in SCORECARD_VOL_KEYS
        )
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BarReturn:
    """Bar-level equity delta used as the per-bar return proxy."""

    ts_ms: int
    delta: float


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _require_utc_datetime(ts: datetime, *, field_name: str) -> datetime:
    if (
        not isinstance(ts, datetime)
        or ts.tzinfo is None
        or ts.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"scorecard_{field_name}_must_be_utc")
    return ts


def _to_utc_iso(ts: datetime) -> str:
    return _require_utc_datetime(ts, field_name="timestamp").isoformat()


def _parse_iso_to_ms(iso: str) -> int | None:
    try:
        cleaned = iso.replace("Z", "+00:00")
        ts = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        return None
    return int(ts.timestamp() * 1000)


def _bar_returns(points: Sequence[EquityPoint]) -> list[_BarReturn]:
    """r_i = equity_i - equity_{i-1}; 首点无前值, 丢弃。"""
    out: list[_BarReturn] = []
    prev: Decimal | None = None
    for p in points:
        if prev is None:
            prev = p.equity
            continue
        with instrument_arithmetic_context():
            delta = p.equity - prev
        out.append(
            _BarReturn(
                ts_ms=p.ts_ms,
                delta=finite_float(
                    delta,
                    reason="scorecard_metric_non_finite",
                ),
            )
        )
        prev = p.equity
    return out


def _information_ratio(returns: Iterable[float]) -> float:
    """mean / sample_stdev (ddof=1), 无年化因子。

    少于 2 个样本或 stdev == 0 时返回 0.0。
    """
    vals = list(returns)
    n = len(vals)
    if n < 2:
        return 0.0
    mean = finite_float(
        sum(vals) / n,
        reason="scorecard_metric_non_finite",
    )
    var = finite_float(
        sum((v - mean) ** 2 for v in vals) / (n - 1),
        reason="scorecard_metric_non_finite",
    )
    if var <= 0.0:
        return 0.0
    return finite_float(
        mean / math.sqrt(var),
        reason="scorecard_metric_non_finite",
    )


def _annualization_factor(ts_ms_seq: Sequence[int]) -> float:
    """段内 bar 间距中位数推算的年化期数。

    退化情形 (样本 < 2 / 全部同刻 / 非正间距) 返回 0.0, 上层据此将
    ir_annualized / sharpe_ratio 置零。
    """
    if len(ts_ms_seq) < 2:
        return 0.0
    deltas = [
        ts_ms_seq[i] - ts_ms_seq[i - 1]
        for i in range(1, len(ts_ms_seq))
        if ts_ms_seq[i] > ts_ms_seq[i - 1]
    ]
    if not deltas:
        return 0.0
    deltas.sort()
    n = len(deltas)
    median = (
        deltas[n // 2]
        if n % 2 == 1
        else (deltas[n // 2 - 1] + deltas[n // 2]) / 2.0
    )
    if median <= 0:
        return 0.0
    return finite_float(
        _MS_PER_YEAR / median,
        reason="scorecard_metric_non_finite",
    )


def _hit_rate(returns: Iterable[float], eps: float = _HIT_EPS) -> float:
    """r > 0 的比例, 分母为 |r| > eps 的样本数; 全零样本返回 0.0。"""
    active = [v for v in returns if abs(v) > eps]
    if not active:
        return 0.0
    wins = sum(1 for v in active if v > 0)
    return wins / len(active)


def _max_drawdown_bps(equities: Iterable[Decimal]) -> float:
    """相对本段内历史峰值的最大回撤 (bps), 分母 max(|peak|, 1)。"""
    peak: Decimal | None = None
    max_dd = Decimal("0")
    for e in equities:
        if peak is None or e > peak:
            peak = e
        if peak is None:
            continue
        with instrument_arithmetic_context():
            dd_raw = peak - e
        if dd_raw <= 0:
            continue
        denom = max(peak.copy_abs(), Decimal("1"))
        with instrument_arithmetic_context():
            dd_bps = dd_raw / denom * _BPS_DENOM
        if dd_bps > max_dd:
            max_dd = dd_bps
    return finite_float(max_dd, reason="scorecard_metric_non_finite")


def _fills_in_ms_set(
    diagnostics: Sequence[CostDiagnostic],
    ms_set: set[int],
) -> int:
    """Count fills whose explicit equity-attribution point belongs to a slice."""

    count = 0
    for d in diagnostics:
        ms = _diagnostic_attribution_ms(d)
        if ms is not None and ms in ms_set:
            count += 1
    return count


def _diagnostic_attribution_ms(diagnostic: CostDiagnostic) -> int | None:
    """Return the equity point that owns a fill, with legacy fallbacks.

    New harness artifacts bind each fill to the first equity point whose PnL
    includes that fill.  Historical artifacts predate these timestamps, so
    only those records fall back to fill/resolution/decision time in order.
    """

    if diagnostic.equity_attribution_ts_ms is not None:
        return diagnostic.equity_attribution_ts_ms
    if diagnostic.fill_ts_ms is not None:
        return diagnostic.fill_ts_ms
    if diagnostic.resolved_at_ts_ms is not None:
        return diagnostic.resolved_at_ts_ms
    return _parse_iso_to_ms(diagnostic.decision_id)


def _uses_explicit_fill_attribution(
    diagnostics: Sequence[CostDiagnostic],
) -> bool:
    return any(
        diagnostic.resolved_at_ts_ms is not None
        or diagnostic.fill_ts_ms is not None
        or diagnostic.equity_attribution_ts_ms is not None
        for diagnostic in diagnostics
    )


def _validate_explicit_fill_attribution(
    diagnostics: Sequence[CostDiagnostic],
    curve: Sequence[EquityPoint],
    *,
    expected_fills: int,
) -> None:
    """Fail closed when a new artifact cannot assign every fill exactly once."""

    if not _uses_explicit_fill_attribution(diagnostics):
        return
    if len(diagnostics) != expected_fills:
        raise ValueError("scorecard_fill_diagnostic_count_mismatch")
    point_timestamps = [point.ts_ms for point in curve]
    if len(set(point_timestamps)) != len(point_timestamps):
        raise ValueError("scorecard_equity_attribution_timestamp_not_unique")
    return_timestamps = set(point_timestamps[1:])
    for diagnostic in diagnostics:
        if (
            diagnostic.resolved_at_ts_ms is None
            or diagnostic.fill_ts_ms is None
            or diagnostic.equity_attribution_ts_ms is None
        ):
            raise ValueError("scorecard_fill_attribution_incomplete")
        if diagnostic.equity_attribution_ts_ms not in return_timestamps:
            raise ValueError("scorecard_fill_attribution_missing_equity_interval")


def _empty_slice() -> dict[str, Any]:
    return {
        "start": None,
        "end": None,
        "ir": 0.0,
        "ir_annualized": 0.0,
        "sharpe_ratio": 0.0,
        "hit_rate": 0.0,
        "fills": 0,
        "sample_n": 0,
    }


def _empty_cross_slice() -> dict[str, Any]:
    slot = _empty_slice()
    slot["max_drawdown_bps"] = 0.0
    return slot


def _slice_stats(
    points: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
    *,
    prior_point: EquityPoint | None = None,
) -> dict[str, Any]:
    # Return 按终点时间归属。OOS test / cross-window 传入紧邻切片之前的
    # baseline point，使切分边界上的收益既不会丢失，也不会被重复计入。
    if not points:
        return _empty_slice()
    metric_points = (
        [prior_point, *points] if prior_point is not None else list(points)
    )
    returns = _bar_returns(metric_points)
    ms_set = {p.ts_ms for p in points}
    deltas = [r.delta for r in returns]
    ir = _information_ratio(deltas)
    factor = _annualization_factor([p.ts_ms for p in metric_points])
    # IR 与 Sharpe 在 bar-level (risk-free = 0) 口径下同式: mean/stdev * sqrt(factor)
    annualized = (
        finite_float(
            ir * math.sqrt(factor),
            reason="scorecard_metric_non_finite",
        )
        if factor > 0
        else 0.0
    )
    return {
        "start": _ms_to_iso(points[0].ts_ms),
        "end": _ms_to_iso(points[-1].ts_ms),
        "ir": ir,
        "ir_annualized": annualized,
        "sharpe_ratio": annualized,
        "hit_rate": _hit_rate(deltas),
        "fills": _fills_in_ms_set(diagnostics, ms_set),
        "sample_n": len(returns),
    }


def _cross_slice_stats(
    points: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
    *,
    prior_point: EquityPoint | None = None,
) -> dict[str, Any]:
    base = _slice_stats(
        points,
        diagnostics,
        prior_point=prior_point,
    )
    metric_points = (
        [prior_point, *points] if prior_point is not None else list(points)
    )
    base["max_drawdown_bps"] = (
        _max_drawdown_bps(p.equity for p in metric_points) if points else 0.0
    )
    return base


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_meta(
    result: BacktestResult,
    generated_at: datetime,
    *,
    fill_attribution_status: str,
) -> dict[str, Any]:
    cfg = result.config
    contract = cfg.instrument_contract
    if contract is None:
        raise ValueError("replay_instrument_contract_required")
    if contract.contract_type != "spot":
        raise ValueError("contract_aware_derivative_artifact_unavailable")
    if contract.instrument_type != "SPOT":
        raise ValueError("margin_replay_artifact_unavailable")
    if cfg.fill_model_version != SCORECARD_FILL_MODEL_VERSION:
        raise ValueError("scorecard_fill_model_version_unsupported")
    resolved_parameters = asdict(result.resolved_parameters)
    if set(resolved_parameters) != SCORECARD_RESOLVED_PARAMETER_KEYS:
        raise ValueError("scorecard_resolved_parameter_schema_mismatch")
    return {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "dataset_version": cfg.dataset_version,
        "family": cfg.family,
        "order_type": cfg.order_type,
        "execution_model_version": cfg.execution_model_version,
        "fill_model_version": cfg.fill_model_version,
        "spot_buy_fee_asset": cfg.spot_buy_fee_asset,
        "market_data_granularity": "ohlcv",
        "execution_realism_limitations": [
            "no_l2_depth",
            "no_spread_or_queue_position",
            "no_market_impact_calibration",
            "fixed_slippage_bps",
            "volume_participation_proxy_only",
        ],
        "instrument_arithmetic_policy_id": INSTRUMENT_ARITHMETIC_POLICY_ID,
        "contract_lineage_status": "calculation_contract_only_unverified",
        "instrument_contract_fingerprint": contract.fingerprint,
        "instrument_contract": {
            "symbol": contract.symbol,
            "instrument_type": contract.instrument_type,
            "contract_type": contract.contract_type,
            "base_currency": contract.base_currency,
            "quote_currency": contract.quote_currency,
            "settle_currency": contract.settle_currency,
            "contract_value": str(contract.contract_value),
            "contract_multiplier": str(contract.contract_multiplier),
            "contract_value_currency": contract.contract_value_currency,
            "lot_size": str(contract.lot_size),
            "min_size": str(contract.min_size),
            "tick_size": str(contract.tick_size),
        },
        "settlement_currency": contract.settle_currency,
        "start_ts": _to_utc_iso(result.start_ts),
        "end_ts": _to_utc_iso(result.end_ts),
        "generated_at": _to_utc_iso(generated_at),
        "total_bars": result.summary.bar_count,
        "total_fills": result.fills_count,
        "total_decisions": result.decisions_count,
        "resolved_parameters": resolved_parameters,
        "adapter_identity": result.adapter_identity,
        "adapter_algorithm_version": result.adapter_algorithm_version,
        "fill_attribution_status": fill_attribution_status,
        "cadence_gap_count": result.cadence_gap_count,
        "risk_metric_policy_id": REPLAY_RISK_METRIC_POLICY_ID,
    }


def _build_oos(
    curve: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
    split_ts: datetime | None,
) -> dict[str, Any]:
    """Train/test split; 显式 ``split_ts`` 优先, 否则按 time-midpoint 兜底。

    * 当 ``split_ts`` 给定时, ``split_method = "explicit"``, 以该时刻为界
      (train: ts_ms < split_ms, test: ts_ms >= split_ms); 不做 index 兜底,
      调用方对 split 位置负责, 一侧为空是合法结果。
    * 当 ``split_ts`` 为 None 时, 使用原 time-midpoint 语义并保留 index
      兜底, 以防所有点同 ts 的退化情形。
    """
    train_points, test_points, split_method, resolved_split_ts = (
        _partition_oos_curve(curve, split_ts)
    )
    if not curve:
        return {
            "split_method": split_method,
            "split_ts": resolved_split_ts,
            "train": _empty_cross_slice(),
            "test": _empty_cross_slice(),
        }

    return {
        "split_method": split_method,
        "split_ts": resolved_split_ts,
        "train": _cross_slice_stats(train_points, diagnostics),
        "test": _cross_slice_stats(
            test_points,
            diagnostics,
            prior_point=train_points[-1] if train_points and test_points else None,
        ),
    }


def _partition_oos_curve(
    curve: Sequence[EquityPoint],
    split_ts: datetime | None,
) -> tuple[list[EquityPoint], list[EquityPoint], str, str | None]:
    """Return the single train/test partition used by all OOS consumers."""

    if not curve:
        return (
            [],
            [],
            "explicit" if split_ts is not None else "time_midpoint",
            _to_utc_iso(split_ts) if split_ts is not None else None,
        )

    if split_ts is not None:
        split_ms = int(
            _require_utc_datetime(
                split_ts,
                field_name="split_ts",
            ).timestamp()
            * 1000
        )
        train_points = [p for p in curve if p.ts_ms < split_ms]
        test_points = [p for p in curve if p.ts_ms >= split_ms]
        return train_points, test_points, "explicit", _to_utc_iso(split_ts)

    first_ts = curve[0].ts_ms
    last_ts = curve[-1].ts_ms
    mid_ms = first_ts + (last_ts - first_ts) // 2

    train_points = [p for p in curve if p.ts_ms < mid_ms]
    test_points = [p for p in curve if p.ts_ms >= mid_ms]

    # 退化: 所有点同 ts 时 mid_ms == first_ts, 全部落入 test — 用 index-半分做兜底
    if not train_points or not test_points:
        half = max(len(curve) // 2, 1)
        train_points = list(curve[:half])
        test_points = list(curve[half:])

    return train_points, test_points, "time_midpoint", _ms_to_iso(mid_ms)


def _build_cross_window(
    curve: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
    slice_count: int,
    *,
    prior_point: EquityPoint | None,
) -> list[dict[str, Any]]:
    """Split OOS test returns by endpoint into contiguous time windows."""
    slice_count = max(slice_count, _DEFAULT_CROSS_WINDOW_SLICES)

    if not curve:
        return [_empty_cross_slice() for _ in range(slice_count)]

    # The validated curve is strictly ordered and fixed-cadence.  Index
    # partitioning therefore yields genuine contiguous time slices while also
    # avoiding empty interior buckets caused by integer timestamp boundaries.
    n = len(curve)
    slices: list[dict[str, Any]] = []
    for i in range(slice_count):
        lo = i * n // slice_count
        hi = (i + 1) * n // slice_count
        pts = list(curve[lo:hi])
        baseline = curve[lo - 1] if lo > 0 else prior_point
        slices.append(
            _cross_slice_stats(
                pts,
                diagnostics,
                prior_point=baseline if pts else None,
            )
        )
    return slices


def _cost_bucket(
    diagnostics: Sequence[CostDiagnostic],
    slip_bps: float,
) -> dict[str, Any]:
    """单个 diagnostics 分桶的 5 字段 cost 结构; 空桶返回稳定零值。

    新版 harness 会把实际 fee/slippage 分项写入 diagnostic；历史 artifact
    没有分项时，保留旧行为：actual_cost 作为 fee、``slip_bps`` 使用调用方
    根据 order type 提供的固定值。
    """
    if not diagnostics:
        return {
            "realized_edge_bps": 0.0,
            "fee_bps": 0.0,
            "slip_bps": slip_bps,
            "exec_buffer_bps": 0.0,
            "net_edge_bps": 0.0,
        }
    n = len(diagnostics)
    fee_bps = (
        sum(
            d.actual_cost_bps
            if d.actual_fee_bps is None
            else d.actual_fee_bps
            for d in diagnostics
        )
        / n
    )
    actual_slip_bps = (
        sum(
            slip_bps
            if d.actual_slippage_bps is None
            else d.actual_slippage_bps
            for d in diagnostics
        )
        / n
    )
    assumed_cost = sum(d.assumed_cost_bps for d in diagnostics) / n
    assumed_net = sum(d.assumed_net_edge_bps for d in diagnostics) / n
    actual_net = sum(d.actual_net_edge_bps for d in diagnostics) / n
    return {
        "realized_edge_bps": assumed_net + assumed_cost,
        "fee_bps": fee_bps,
        "slip_bps": actual_slip_bps,
        "exec_buffer_bps": assumed_cost - fee_bps - actual_slip_bps,
        "net_edge_bps": actual_net,
    }


def _cost_sensitivity(
    bucket: dict[str, Any],
    *,
    is_empty: bool,
) -> dict[str, float]:
    """压力测试: fee 上调 20% / slip +0.5bps 后的 net edge。

    空桶统一返回稳定零值, 避免在 slip_bps 继承自 order_type 时产生误导性
    负值。
    """
    if is_empty:
        return {
            "net_edge_fee_up_20pct_bps": 0.0,
            "net_edge_slip_plus_0_5bps_bps": 0.0,
        }
    realized = bucket["realized_edge_bps"]
    fee = bucket["fee_bps"]
    slip = bucket["slip_bps"]
    exec_buf = bucket["exec_buffer_bps"]
    adverse_fee = fee + abs(fee) * 0.2
    return {
        "net_edge_fee_up_20pct_bps": realized - adverse_fee - slip - exec_buf,
        "net_edge_slip_plus_0_5bps_bps": realized - fee - (slip + 0.5) - exec_buf,
    }


def _split_diagnostics(
    diagnostics: Sequence[CostDiagnostic],
    curve: Sequence[EquityPoint],
    split_ts: datetime | None,
) -> tuple[list[CostDiagnostic], list[CostDiagnostic]]:
    """按 OOS 相同的 split 规则把 diagnostics 切成 train / test。

    * 显式 ``split_ts`` 优先 (ts_ms < split_ms → train, 其余 → test)
    * 否则按 curve 的 time-midpoint 兜底
    * 无 curve 且无 split_ts 时无法定义分界 — 全部归 train, test 留空
    * 新产物按 ``equity_attribution_ts_ms``；旧产物才回退 fill/resolution/
      ``decision_id``，与 ``_fills_in_ms_set`` 一致
    * 无法取得时间的旧记录保守忽略
    """
    if split_ts is not None:
        split_ms = int(
            _require_utc_datetime(
                split_ts,
                field_name="split_ts",
            ).timestamp()
            * 1000
        )
    elif curve:
        first_ts = curve[0].ts_ms
        last_ts = curve[-1].ts_ms
        split_ms = first_ts + (last_ts - first_ts) // 2
    else:
        return list(diagnostics), []

    train: list[CostDiagnostic] = []
    test: list[CostDiagnostic] = []
    for d in diagnostics:
        ms = _diagnostic_attribution_ms(d)
        if ms is None:
            continue
        if ms < split_ms:
            train.append(d)
        else:
            test.append(d)
    return train, test


def _build_cost_adjusted(
    result: BacktestResult,
    diagnostics: Sequence[CostDiagnostic],
    curve: Sequence[EquityPoint],
    split_ts: datetime | None,
) -> dict[str, Any]:
    """Cost 分解 — 全部复用 BacktestResult 已有的 cost 口径, 不新增估算。

    字段语义:
        * ``fee_bps``        — 每笔 fill 的实际 fee_bps 均值 (FillResult.fee_bps)
        * ``slip_bps``       — 新 artifact 取每笔 fill 实际记录值；历史 artifact
          回退到 order_type 固定值（``ioc`` / ``bounded_limit`` 使用
          ``config.ioc_slippage_bps``，``post_only`` 为 0）
        * ``exec_buffer_bps``— 策略 per-decision 假设 cost 减去 (fee + slip) 剩余
          部分, 代表"决策端为兜底保留的 execution buffer"
        * ``realized_edge_bps`` — 决策层估的 gross 信号 edge =
          mean(assumed_net_edge_bps) + mean(assumed_cost_bps)
        * ``net_edge_bps``   — 套用实际 fee + fixed slippage 后的 net edge =
          mean(actual_net_edge_bps) (来自 CostValidator)

    顶层保留既有 5 字段 (overall aggregate) 以维持向后兼容; 另外挂
    ``train`` / ``test`` 两个子对象, 切分规则与 OOS 对齐 (explicit
    ``split_ts`` 优先, 否则 time-midpoint)。
    """
    slip_bps = (
        finite_float(
            result.config.ioc_slippage_bps,
            reason="scorecard_metric_non_finite",
        )
        if result.config.order_type in {"ioc", "bounded_limit"}
        else 0.0
    )
    overall = _cost_bucket(diagnostics, slip_bps)
    train_diags, test_diags = _split_diagnostics(diagnostics, curve, split_ts)
    train_bucket = _cost_bucket(train_diags, slip_bps)
    test_bucket = _cost_bucket(test_diags, slip_bps)
    overall["train"] = train_bucket
    overall["test"] = test_bucket
    overall["sensitivity"] = {
        "overall": _cost_sensitivity(overall, is_empty=not diagnostics),
        "train": _cost_sensitivity(train_bucket, is_empty=not train_diags),
        "test": _cost_sensitivity(test_bucket, is_empty=not test_diags),
    }
    return overall


def _build_regime_slice(
    curve: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
) -> dict[str, Any]:
    """按 |bar return| 中位数切 2 bucket (低波 / 高波), 输出 IR + fills。

    说明 (仅供调用方理解, 不落入 JSON 文案):
    v0.1 scorecard 在纯 equity-curve 上工作, 采用"本 bar PnL 绝对值"作为
    realized vol 的代理。更贴近 governance 建议的独立 realized vol 指标
    将在后续迭代接入 (需要 bar close / ATR, 超出本任务 scope)。
    """
    empty_bucket = {"ir": 0.0, "fills": 0, "sample_n": 0}
    if len(curve) < 2:
        return {
            "vol": {
                "low": dict(empty_bucket),
                "high": dict(empty_bucket),
            }
        }

    returns = _bar_returns(curve)
    if not returns:
        return {
            "vol": {
                "low": dict(empty_bucket),
                "high": dict(empty_bucket),
            }
        }

    abs_vals = sorted(abs(r.delta) for r in returns)
    n = len(abs_vals)
    if n % 2 == 1:
        median = abs_vals[n // 2]
    else:
        median = (abs_vals[n // 2 - 1] + abs_vals[n // 2]) / 2.0

    low = [r for r in returns if abs(r.delta) <= median]
    high = [r for r in returns if abs(r.delta) > median]

    return {
        "vol": {
            "low": {
                "ir": _information_ratio(r.delta for r in low),
                "fills": _fills_in_ms_set(
                    diagnostics, {r.ts_ms for r in low}
                ),
                "sample_n": len(low),
            },
            "high": {
                "ir": _information_ratio(r.delta for r in high),
                "fills": _fills_in_ms_set(
                    diagnostics, {r.ts_ms for r in high}
                ),
                "sample_n": len(high),
            },
        }
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_scorecard(
    result: BacktestResult,
    *,
    generated_at: datetime | None = None,
    cross_window_slices: int = _DEFAULT_CROSS_WINDOW_SLICES,
    split_ts: datetime | None = None,
) -> dict[str, Any]:
    """从 ``BacktestResult`` 派生 evidence scorecard dict。

    Args:
        result: 已完成的 backtest 运行结果。
        generated_at: 生成时间戳, 缺省取当前 UTC; 测试中显式注入以保 determinism。
        cross_window_slices: cross-window 分片数, 最小 3。
        split_ts: OOS train/test 切分点; 给定时 ``oos.split_method = "explicit"``,
            None 时回退到 time-midpoint。显式传入以消除 "magic split" 隐含语义。

    Returns:
        可直接 ``json.dump`` 的嵌套 dict；顶层含强制 artifact kind/schema，
        以及 ``meta`` / ``oos`` / ``cross_window`` / ``cost_adjusted`` /
        ``regime_slice``。
    """
    if generated_at is None:
        generated_at = datetime.now(tz=timezone.utc)
    _require_utc_datetime(generated_at, field_name="generated_at")
    if split_ts is not None:
        _require_utc_datetime(split_ts, field_name="split_ts")
    # A v2 scorecard is derived evidence, not a migration wrapper around an
    # arbitrary in-memory result.  Validate the basic shape first so the
    # scorecard-specific attribution errors below stay stable, then require the
    # complete backtest-run/v2 timeline/cost contract before emitting v2.
    validate_backtest_result_units(result)
    curve = result.equity_curve
    diagnostics = result.cost_diagnostics
    explicit_fill_attribution = _uses_explicit_fill_attribution(diagnostics)
    if len(diagnostics) != result.fills_count:
        raise ValueError("scorecard_fill_diagnostic_count_mismatch")
    if result.fills_count and not explicit_fill_attribution:
        raise ValueError("scorecard_fill_attribution_incomplete")
    _validate_explicit_fill_attribution(
        diagnostics,
        curve,
        expected_fills=result.fills_count,
    )
    validate_backtest_result_units(result, require_complete_artifact=True)

    try:
        oos_train_curve, oos_test_curve, _, _ = _partition_oos_curve(
            curve,
            split_ts,
        )
        scorecard = {
            "artifact_kind": SCORECARD_ARTIFACT_KIND,
            "artifact_schema_version": SCORECARD_SCHEMA_VERSION,
            "meta": _build_meta(
                result,
                generated_at,
                fill_attribution_status="explicit_v1",
            ),
            "oos": _build_oos(curve, diagnostics, split_ts),
            "cross_window": _build_cross_window(
                oos_test_curve,
                diagnostics,
                cross_window_slices,
                prior_point=(
                    oos_train_curve[-1]
                    if oos_train_curve and oos_test_curve
                    else None
                ),
            ),
            "cost_adjusted": _build_cost_adjusted(
                result, diagnostics, curve, split_ts
            ),
            "regime_slice": _build_regime_slice(curve, diagnostics),
        }
    except ArithmeticError as exc:
        raise ValueError("scorecard_metric_non_finite") from exc
    expected_return_samples = max(len(curve) - 1, 0)
    oos_return_samples = (
        scorecard["oos"]["train"]["sample_n"]
        + scorecard["oos"]["test"]["sample_n"]
    )
    cross_return_samples = sum(
        window["sample_n"] for window in scorecard["cross_window"]
    )
    regime_return_samples = sum(
        bucket["sample_n"]
        for bucket in scorecard["regime_slice"]["vol"].values()
    )
    if (
        oos_return_samples != expected_return_samples
        or cross_return_samples != scorecard["oos"]["test"]["sample_n"]
        or regime_return_samples != expected_return_samples
    ):
        raise ValueError("scorecard_return_attribution_partition_mismatch")
    if explicit_fill_attribution:
        oos_fill_count = (
            scorecard["oos"]["train"]["fills"]
            + scorecard["oos"]["test"]["fills"]
        )
        cross_fill_count = sum(
            window["fills"] for window in scorecard["cross_window"]
        )
        regime_fill_count = sum(
            bucket["fills"]
            for bucket in scorecard["regime_slice"]["vol"].values()
        )
        if (
            oos_fill_count != result.fills_count
            or cross_fill_count != scorecard["oos"]["test"]["fills"]
            or regime_fill_count != result.fills_count
        ):
            raise ValueError("scorecard_fill_attribution_partition_mismatch")
    meta = scorecard["meta"]
    resolved_parameters = meta["resolved_parameters"]
    if (
        set(scorecard) != SCORECARD_TOP_LEVEL_KEYS
        or set(meta) != SCORECARD_META_KEYS
        or set(meta["instrument_contract"])
        != SCORECARD_INSTRUMENT_CONTRACT_KEYS
        or set(resolved_parameters) != SCORECARD_RESOLVED_PARAMETER_KEYS
        or set(resolved_parameters["cost_config"])
        != SCORECARD_RESOLVED_COST_KEYS
        or not _has_exact_nested_scorecard_schema(scorecard)
    ):
        raise ValueError("scorecard_v2_schema_mismatch")
    validate_finite_numbers(
        scorecard,
        reason="scorecard_metric_non_finite",
    )
    return scorecard


__all__ = [
    "SCORECARD_ARTIFACT_KIND",
    "SCORECARD_COST_ADJUSTED_KEYS",
    "SCORECARD_COST_BUCKET_KEYS",
    "SCORECARD_FILL_MODEL_VERSION",
    "SCORECARD_INSTRUMENT_CONTRACT_KEYS",
    "SCORECARD_META_KEYS",
    "SCORECARD_OOS_KEYS",
    "SCORECARD_REGIME_BUCKET_KEYS",
    "SCORECARD_REGIME_KEYS",
    "SCORECARD_RESOLVED_COST_KEYS",
    "SCORECARD_RESOLVED_PARAMETER_KEYS",
    "SCORECARD_SCHEMA_VERSION",
    "SCORECARD_SENSITIVITY_BUCKET_KEYS",
    "SCORECARD_SENSITIVITY_KEYS",
    "SCORECARD_SLICE_KEYS",
    "SCORECARD_TOP_LEVEL_KEYS",
    "SCORECARD_VOL_KEYS",
    "build_scorecard",
]
