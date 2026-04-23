"""Evidence scorecard emitter for backtest CLI.

从一次 :class:`BacktestResult` 出发，派生一个结构化 JSON scorecard，供
``docs/governance/alpha_evidence_gate.md`` 要求的人工 research 评审消费。

严格 scope
----------
* 本模块只做**数值**派生：IR / hit rate / drawdown / cost breakdown / fills。
* **绝不**输出 verdict / go / no-go / pass / fail / archive 一类的判定字段或
  文案 —— gate 本身要求"人类最终拍板", 自动化不得隐含结论。
* 纯函数、无 I/O、无 logging, 不修改 backtest 运行语义。

顶层键（任务单锁定）
--------------------
``meta`` / ``oos`` / ``cross_window`` / ``cost_adjusted`` / ``regime_slice``

每段的具体字段见各 builder 的 docstring。所有时间戳用 UTC ISO-8601。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from aats.data_platform.replay.backtest.cost_validator import CostDiagnostic
from aats.data_platform.replay.backtest.equity_builder import EquityPoint
from aats.data_platform.replay.backtest.harness import BacktestResult


_BPS_DENOM = Decimal("10000")
_DEFAULT_CROSS_WINDOW_SLICES = 3
_HIT_EPS = 1e-12


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


def _to_utc_iso(ts: datetime) -> str:
    aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


def _parse_iso_to_ms(iso: str) -> int | None:
    try:
        cleaned = iso.replace("Z", "+00:00")
        ts = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _bar_returns(points: Sequence[EquityPoint]) -> list[_BarReturn]:
    """r_i = equity_i - equity_{i-1}; 首点无前值, 丢弃。"""
    out: list[_BarReturn] = []
    prev: Decimal | None = None
    for p in points:
        if prev is None:
            prev = p.equity
            continue
        out.append(_BarReturn(ts_ms=p.ts_ms, delta=float(p.equity - prev)))
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
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0.0:
        return 0.0
    return mean / math.sqrt(var)


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
        dd_raw = peak - e
        if dd_raw <= 0:
            continue
        denom = max(abs(peak), Decimal("1"))
        dd_bps = dd_raw / denom * _BPS_DENOM
        if dd_bps > max_dd:
            max_dd = dd_bps
    return float(max_dd)


def _fills_in_ms_set(
    diagnostics: Sequence[CostDiagnostic],
    ms_set: set[int],
) -> int:
    count = 0
    for d in diagnostics:
        ms = _parse_iso_to_ms(d.decision_id)
        if ms is not None and ms in ms_set:
            count += 1
    return count


def _empty_slice() -> dict[str, Any]:
    return {
        "start": None,
        "end": None,
        "ir": 0.0,
        "hit_rate": 0.0,
        "fills": 0,
    }


def _empty_cross_slice() -> dict[str, Any]:
    slot = _empty_slice()
    slot["max_drawdown_bps"] = 0.0
    return slot


def _slice_stats(
    points: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
) -> dict[str, Any]:
    if not points:
        return _empty_slice()
    returns = _bar_returns(points)
    ms_set = {p.ts_ms for p in points}
    return {
        "start": _ms_to_iso(points[0].ts_ms),
        "end": _ms_to_iso(points[-1].ts_ms),
        "ir": _information_ratio(r.delta for r in returns),
        "hit_rate": _hit_rate(r.delta for r in returns),
        "fills": _fills_in_ms_set(diagnostics, ms_set),
    }


def _cross_slice_stats(
    points: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
) -> dict[str, Any]:
    base = _slice_stats(points, diagnostics)
    base["max_drawdown_bps"] = (
        _max_drawdown_bps(p.equity for p in points) if points else 0.0
    )
    return base


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_meta(
    result: BacktestResult,
    generated_at: datetime,
) -> dict[str, Any]:
    cfg = result.config
    return {
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "dataset_version": cfg.dataset_version,
        "family": cfg.family,
        "order_type": cfg.order_type,
        "contract_multiplier": str(cfg.contract_multiplier),
        "start_ts": _to_utc_iso(result.start_ts),
        "end_ts": _to_utc_iso(result.end_ts),
        "generated_at": _to_utc_iso(generated_at),
        "total_bars": result.summary.bar_count,
        "total_fills": result.fills_count,
        "total_decisions": result.decisions_count,
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
    if not curve:
        return {
            "split_method": "explicit" if split_ts is not None else "time_midpoint",
            "split_ts": _to_utc_iso(split_ts) if split_ts is not None else None,
            "train": _empty_slice(),
            "test": _empty_slice(),
        }

    if split_ts is not None:
        split_ms = int(
            (
                split_ts
                if split_ts.tzinfo
                else split_ts.replace(tzinfo=timezone.utc)
            )
            .astimezone(timezone.utc)
            .timestamp()
            * 1000
        )
        train_points = [p for p in curve if p.ts_ms < split_ms]
        test_points = [p for p in curve if p.ts_ms >= split_ms]
        return {
            "split_method": "explicit",
            "split_ts": _to_utc_iso(split_ts),
            "train": _slice_stats(train_points, diagnostics),
            "test": _slice_stats(test_points, diagnostics),
        }

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

    return {
        "split_method": "time_midpoint",
        "split_ts": _ms_to_iso(mid_ms),
        "train": _slice_stats(train_points, diagnostics),
        "test": _slice_stats(test_points, diagnostics),
    }


def _build_cross_window(
    curve: Sequence[EquityPoint],
    diagnostics: Sequence[CostDiagnostic],
    slice_count: int,
) -> list[dict[str, Any]]:
    """固定 ``slice_count`` 条非重叠时间片 (至少 3), 按时间等分。"""
    slice_count = max(slice_count, _DEFAULT_CROSS_WINDOW_SLICES)

    if not curve:
        return [_empty_cross_slice() for _ in range(slice_count)]

    first_ts = curve[0].ts_ms
    last_ts = curve[-1].ts_ms
    span = last_ts - first_ts

    # Span == 0 (单 bar / 相同时间戳): 按 index 均分, 少数 bar 时大部分片为空
    if span == 0:
        n = len(curve)
        step = max(n // slice_count, 1)
        out: list[dict[str, Any]] = []
        for i in range(slice_count):
            lo = i * step
            hi = (i + 1) * step if i < slice_count - 1 else n
            pts = list(curve[lo:hi])
            out.append(_cross_slice_stats(pts, diagnostics))
        return out

    slices: list[dict[str, Any]] = []
    for i in range(slice_count):
        lo_ms = first_ts + i * span // slice_count
        hi_ms = (
            last_ts
            if i == slice_count - 1
            else first_ts + (i + 1) * span // slice_count
        )
        include_hi = i == slice_count - 1
        if include_hi:
            pts = [p for p in curve if lo_ms <= p.ts_ms <= hi_ms]
        else:
            pts = [p for p in curve if lo_ms <= p.ts_ms < hi_ms]
        slices.append(_cross_slice_stats(pts, diagnostics))
    return slices


def _build_cost_adjusted(
    result: BacktestResult,
    diagnostics: Sequence[CostDiagnostic],
) -> dict[str, Any]:
    """Cost 分解 — 全部复用 BacktestResult 已有的 cost 口径, 不新增估算。

    字段语义:
        * ``fee_bps``        — 每笔 fill 的实际 fee_bps 均值 (FillResult.fee_bps)
        * ``slip_bps``       — 与 FillSimulator 当前 order_type 语义对齐的滑点假设:
          ``ioc`` → ``config.ioc_slippage_bps``; ``post_only`` / ``bounded_limit`` → 0
        * ``exec_buffer_bps``— 策略 per-decision 假设 cost 减去 (fee + slip) 剩余
          部分, 代表"决策端为兜底保留的 execution buffer"
        * ``realized_edge_bps`` — 决策层估的 gross 信号 edge =
          mean(assumed_net_edge_bps) + mean(assumed_cost_bps)
        * ``net_edge_bps``   — 套用实际 fee 后的 net edge =
          mean(actual_net_edge_bps) (来自 CostValidator)
    """
    slip_bps = (
        float(result.config.ioc_slippage_bps)
        if result.config.order_type == "ioc"
        else 0.0
    )
    if not diagnostics:
        return {
            "realized_edge_bps": 0.0,
            "fee_bps": 0.0,
            "slip_bps": slip_bps,
            "exec_buffer_bps": 0.0,
            "net_edge_bps": 0.0,
        }

    n = len(diagnostics)
    fee_bps = sum(d.actual_cost_bps for d in diagnostics) / n
    assumed_cost = sum(d.assumed_cost_bps for d in diagnostics) / n
    assumed_net = sum(d.assumed_net_edge_bps for d in diagnostics) / n
    actual_net = sum(d.actual_net_edge_bps for d in diagnostics) / n

    exec_buffer_bps = assumed_cost - fee_bps - slip_bps
    realized_edge_bps = assumed_net + assumed_cost

    return {
        "realized_edge_bps": realized_edge_bps,
        "fee_bps": fee_bps,
        "slip_bps": slip_bps,
        "exec_buffer_bps": exec_buffer_bps,
        "net_edge_bps": actual_net,
    }


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
    empty_bucket = {"ir": 0.0, "fills": 0}
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
            },
            "high": {
                "ir": _information_ratio(r.delta for r in high),
                "fills": _fills_in_ms_set(
                    diagnostics, {r.ts_ms for r in high}
                ),
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
        可直接 ``json.dump`` 的嵌套 dict; 顶层键:
        ``meta`` / ``oos`` / ``cross_window`` / ``cost_adjusted`` /
        ``regime_slice``。
    """
    if generated_at is None:
        generated_at = datetime.now(tz=timezone.utc)
    curve = result.equity_curve
    diagnostics = result.cost_diagnostics

    return {
        "meta": _build_meta(result, generated_at),
        "oos": _build_oos(curve, diagnostics, split_ts),
        "cross_window": _build_cross_window(
            curve, diagnostics, cross_window_slices
        ),
        "cost_adjusted": _build_cost_adjusted(result, diagnostics),
        "regime_slice": _build_regime_slice(curve, diagnostics),
    }


__all__ = ["build_scorecard"]
