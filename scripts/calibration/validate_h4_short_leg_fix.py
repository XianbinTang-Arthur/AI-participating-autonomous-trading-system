"""H4 方向门控修复验证脚本.

流程:
    1. 从 public.event_store 拉 N 天的 strategy.baseline_assessment 记录
    2. 从 silver.market_swap_candles_15m 拉 15m 线
    3. 对每条 baseline: 分别用 **旧实现** (H4 前) 和 **新实现** (H4 后) 计算
       long/short leg 的 compute_raw_book_score
    4. 匹配 ts -> close_T / close_{T+15m} / close_{T+30m} / close_{T+60m}
    5. 计算 realized_edge_{horizon}_bps = side_sign × (close_{T+h} - close_T) / close_T × 10000
    6. 对 long / short 子集分别跑 OLS 回归 (realized vs score), 对比 H4 前后的 R² 与 slope

验收目标 (docs/design/h4_confidence_direction_gating_2026_04_19.md §8):
    - short R² ≥ 0.01
    - short slope > 0
    - long R² ≥ 0.012 (不严重退化)
    - long slope ≥ +12

用法:
    .venv\\Scripts\\python.exe scripts/calibration/validate_h4_short_leg_fix.py --days 30 --symbol BTC-USDT-SWAP

数据源:
    .env.derivatives.live (POSTGRES_* / AATS_ACTIVE_PARAMETER_DB_URL / RDP_DATABASE_URL)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]

try:
    from dotenv import load_dotenv
except ImportError:
    print("missing python-dotenv; pip install python-dotenv", file=sys.stderr)
    sys.exit(1)

try:
    import numpy as np
    from sqlalchemy import create_engine, text
except ImportError as e:
    print(f"missing dependency: {e}", file=sys.stderr)
    sys.exit(1)


def _load_env() -> None:
    # 走子目录向上找 .env.*
    cur = HERE.parent
    for _ in range(6):
        for name in (".env.derivatives.live", ".env.wsl2"):
            p = cur / name
            if p.exists():
                load_dotenv(p, override=False)
        cur = cur.parent


_load_env()


def _resolve_live_url() -> str:
    """从环境变量构造 aats_live_derivatives 连接 URL。

    优先级: AATS_ACTIVE_PARAMETER_DB_URL > 拼接 POSTGRES_*。
    """
    url = os.getenv("AATS_ACTIVE_PARAMETER_DB_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "admin")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("AATS_LIVE_DB_NAME", "aats_live_derivatives")
    if not pw:
        print("POSTGRES_PASSWORD not set — check .env.derivatives.live / .env.wsl2", file=sys.stderr)
        sys.exit(1)
    return f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}"


def _resolve_research_url() -> str:
    url = os.getenv("RDP_DATABASE_URL")
    if url:
        return url
    user = os.getenv("RDP_DB_USER") or os.getenv("POSTGRES_USER", "admin")
    pw = os.getenv("RDP_DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("RDP_DB_HOST") or os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("RDP_DB_PORT") or os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("RDP_DB_NAME", "aats_research")
    if not pw:
        print("RDP DB credentials not found", file=sys.stderr)
        sys.exit(1)
    return f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}"


# ---------------------------------------------------------------------------
# Scoring 公式双版本
# ---------------------------------------------------------------------------
# 对齐 aats/services/strategy_engines/independent/scoring.py Mode A 权重
_W_ALPHA = 0.34
_W_MOMENTUM = 0.24
_W_TREND = 0.18
_W_MICRO = 0.12
_W_CONFIDENCE = 0.12


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_old_pre_h4(
    *,
    leg: str,
    direction_bias: str,
    confidence: float,
    composite_alpha: float,
    momentum_alpha: float,
    trend_alpha: float,
    micro_alpha: float,
    regime: str,
    volatility_state: str,
) -> float:
    """H4 修复**前**的算分（confidence + regime_bonus + volatility_bonus 方向无关）。"""
    side_sign = 1.0 if leg == "long" else -1.0
    alpha_c = _clamp(max(0.0, side_sign * composite_alpha), 0.0, 1.0)
    mom_c = _clamp(max(0.0, side_sign * momentum_alpha), 0.0, 1.0)
    trend_c = _clamp(max(0.0, side_sign * trend_alpha), 0.0, 1.0)
    micro_c = _clamp(max(0.0, side_sign * micro_alpha), 0.0, 1.0)
    conf_c = _clamp(confidence, 0.0, 1.0)  # 方向无关!
    score = (
        alpha_c * _W_ALPHA + mom_c * _W_MOMENTUM + trend_c * _W_TREND
        + micro_c * _W_MICRO + conf_c * _W_CONFIDENCE
    )
    # 旧 bonus: 两腿同加
    if regime in {"range", "uncertain"}:
        score += 0.04
    if direction_bias == leg:
        score += 0.06
    if volatility_state == "high":
        score += 0.03
    return _clamp(score, 0.0, 1.0)


def score_new_post_h4(
    *,
    leg: str,
    direction_bias: str,
    confidence: float,
    composite_alpha: float,
    momentum_alpha: float,
    trend_alpha: float,
    micro_alpha: float,
    regime: str,
    volatility_state: str,
) -> float:
    """H4 修复**后**的算分（方向无关加项仅 leg_aligned 时计入）。"""
    side_sign = 1.0 if leg == "long" else -1.0
    alpha_c = _clamp(max(0.0, side_sign * composite_alpha), 0.0, 1.0)
    mom_c = _clamp(max(0.0, side_sign * momentum_alpha), 0.0, 1.0)
    trend_c = _clamp(max(0.0, side_sign * trend_alpha), 0.0, 1.0)
    micro_c = _clamp(max(0.0, side_sign * micro_alpha), 0.0, 1.0)
    leg_aligned = direction_bias == leg
    conf_c = _clamp(confidence if leg_aligned else 0.0, 0.0, 1.0)
    score = (
        alpha_c * _W_ALPHA + mom_c * _W_MOMENTUM + trend_c * _W_TREND
        + micro_c * _W_MICRO + conf_c * _W_CONFIDENCE
    )
    # 新 bonus: 方向门控
    if regime in {"range", "uncertain"} and leg_aligned:
        score += 0.04
    if direction_bias == leg:
        score += 0.06
    if volatility_state == "high" and leg_aligned:
        score += 0.03
    return _clamp(score, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Sample 载入
# ---------------------------------------------------------------------------

@dataclass
class BaselineSample:
    ts: datetime
    symbol: str
    regime: str
    direction_bias: str
    volatility_state: str
    confidence: float
    composite_alpha: float
    momentum_alpha: float
    trend_alpha: float
    micro_alpha: float
    # 后补
    close_t: float = 0.0
    close_15m: float | None = None
    close_30m: float | None = None
    close_60m: float | None = None


def load_baselines(live_url: str, symbol: str, start_ts: datetime, end_ts: datetime) -> list[BaselineSample]:
    """拉 public.event_store 里的 strategy.baseline_assessment 记录。"""
    sql = text(
        """
        SELECT
            event_timestamp                                                       AS ts,
            payload::jsonb->>'symbol'                                             AS symbol,
            payload::jsonb->>'regime'                                             AS regime,
            payload::jsonb->>'direction_bias'                                     AS direction_bias,
            payload::jsonb->>'volatility_state'                                   AS volatility_state,
            (payload::jsonb->>'confidence')::float                                AS confidence,
            (payload::jsonb->>'composite_alpha_score')::float                     AS composite_alpha,
            (payload::jsonb->'factor_scores'->>'momentum_alpha')::float           AS momentum_alpha,
            (payload::jsonb->'factor_scores'->>'trend_alpha')::float              AS trend_alpha,
            (payload::jsonb->'factor_scores'->>'microstructure_alpha')::float     AS micro_alpha
        FROM public.event_store
        WHERE topic = 'strategy.baseline_assessment'
          AND symbol = :symbol
          AND event_timestamp >= :start_ts
          AND event_timestamp <= :end_ts
        ORDER BY event_timestamp
        """
    )
    engine = create_engine(live_url, pool_pre_ping=True)
    samples: list[BaselineSample] = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts}).fetchall()
    finally:
        engine.dispose()

    for row in rows:
        if row.composite_alpha is None or row.confidence is None:
            continue
        samples.append(BaselineSample(
            ts=_ensure_tz(row.ts),
            symbol=row.symbol or symbol,
            regime=row.regime or "range",
            direction_bias=row.direction_bias or "flat",
            volatility_state=row.volatility_state or "medium",
            confidence=float(row.confidence),
            composite_alpha=float(row.composite_alpha),
            momentum_alpha=float(row.momentum_alpha or 0.0),
            trend_alpha=float(row.trend_alpha or 0.0),
            micro_alpha=float(row.micro_alpha or 0.0),
        ))
    return samples


def load_candle_index(research_url: str, symbol: str, start_ts: datetime, end_ts: datetime) -> dict[datetime, float]:
    sql = text(
        """
        SELECT ts, close
        FROM silver.market_swap_candles_15m
        WHERE symbol = :symbol
          AND ts >= :start_ts
          AND ts <= :end_ts
        ORDER BY ts
        """
    )
    engine = create_engine(research_url, pool_pre_ping=True)
    index: dict[datetime, float] = {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts}).fetchall()
    finally:
        engine.dispose()
    for row in rows:
        ts = _ensure_tz(row.ts).astimezone(timezone.utc)
        index[ts] = float(row.close)
    return index


def _ensure_tz(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _floor_to_15m(ts: datetime) -> datetime:
    ts_utc = ts.astimezone(timezone.utc)
    minute = (ts_utc.minute // 15) * 15
    return ts_utc.replace(minute=minute, second=0, microsecond=0)


def enrich(samples: list[BaselineSample], candle_index: dict[datetime, float]) -> list[BaselineSample]:
    out: list[BaselineSample] = []
    for s in samples:
        bar = _floor_to_15m(s.ts)
        c0 = candle_index.get(bar)
        if c0 is None or c0 <= 0:
            continue
        s.close_t = c0
        s.close_15m = candle_index.get(bar + timedelta(minutes=15))
        s.close_30m = candle_index.get(bar + timedelta(minutes=30))
        s.close_60m = candle_index.get(bar + timedelta(minutes=60))
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------

def ols_regression(x: np.ndarray, y: np.ndarray) -> dict[str, float]:
    if len(x) < 20:
        return {"n": len(x), "slope": float("nan"), "intercept": float("nan"),
                "r_squared": float("nan"), "pearson_r": float("nan")}
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    ss_xx = float(np.sum((x - x_mean) ** 2))
    ss_xy = float(np.sum((x - x_mean) * (y - y_mean)))
    ss_yy = float(np.sum((y - y_mean) ** 2))
    slope = ss_xy / ss_xx if ss_xx > 0 else float("nan")
    intercept = y_mean - slope * x_mean if not math.isnan(slope) else float("nan")
    y_pred = intercept + slope * x
    ss_res = float(np.sum((y - y_pred) ** 2))
    r2 = 1 - ss_res / ss_yy if ss_yy > 0 else float("nan")
    pearson = ss_xy / math.sqrt(ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else float("nan")
    return {
        "n": len(x), "slope": slope, "intercept": intercept,
        "r_squared": r2, "pearson_r": pearson,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

HORIZONS = [
    ("15m", "close_15m", 15),
    ("30m", "close_30m", 30),
    ("60m", "close_60m", 60),
]


def compute_regression_set(samples: list[BaselineSample], score_fn, label: str) -> dict[str, Any]:
    """对一组 samples + 一种评分函数 (score_old_pre_h4 or score_new_post_h4)，
    计算 long / short 在 3 个 horizon 上的 regression 结果。
    """
    results: dict[str, Any] = {"label": label}

    for leg in ("long", "short"):
        for h_name, field_name, _minutes in HORIZONS:
            xs: list[float] = []
            ys: list[float] = []
            for s in samples:
                c_h = getattr(s, field_name)
                if c_h is None or s.close_t <= 0:
                    continue
                score = score_fn(
                    leg=leg,
                    direction_bias=s.direction_bias,
                    confidence=s.confidence,
                    composite_alpha=s.composite_alpha,
                    momentum_alpha=s.momentum_alpha,
                    trend_alpha=s.trend_alpha,
                    micro_alpha=s.micro_alpha,
                    regime=s.regime,
                    volatility_state=s.volatility_state,
                )
                side_sign = 1.0 if leg == "long" else -1.0
                realized_bps = (c_h - s.close_t) / s.close_t * 10000.0 * side_sign
                xs.append(score)
                ys.append(realized_bps)
            if len(xs) < 20:
                results[f"{leg}_{h_name}"] = {"n": len(xs), "note": "insufficient"}
                continue
            x = np.array(xs)
            y = np.array(ys)
            reg = ols_regression(x, y)
            reg["score_mean"] = float(np.mean(x))
            reg["score_std"] = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
            reg["score_nonzero_pct"] = float(np.mean(x > 0)) * 100.0
            results[f"{leg}_{h_name}"] = reg
    return results


def _aggregate_by_bar(samples: list[BaselineSample]) -> list[BaselineSample]:
    """把同 15m bar 内的 baselines 折叠成 1 个（取最后一个 baseline 的快照）。

    这对齐 short_leg_asymmetry_root_cause 报告的 bar-level 回归方法学：
    每 15m 1 个样本，而不是每 decision 1 个。
    """
    buckets: dict[datetime, list[BaselineSample]] = {}
    for s in samples:
        bar = _floor_to_15m(s.ts)
        buckets.setdefault(bar, []).append(s)
    out: list[BaselineSample] = []
    for bar, group in sorted(buckets.items()):
        # 取最后一个 baseline（最接近 bar close 的评估）
        group_sorted = sorted(group, key=lambda x: x.ts)
        out.append(group_sorted[-1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbol", default="BTC-USDT-SWAP")
    ap.add_argument("--output", default=None, help="optional CSV path for samples")
    ap.add_argument("--aggregate-bar", action="store_true",
                    help="Aggregate baselines to 15m bar level (1 sample per bar, use last baseline in bar). "
                         "Matches short_leg_asymmetry_root_cause report methodology.")
    args = ap.parse_args()

    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=args.days)

    print(f"[{datetime.now(timezone.utc).isoformat()}] loading baselines...")
    live_url = _resolve_live_url()
    research_url = _resolve_research_url()

    samples = load_baselines(live_url, args.symbol, start_ts, end_ts)
    print(f"  → {len(samples)} raw baseline records")
    if len(samples) < 100:
        print("  ! too few samples, aborting", file=sys.stderr)
        return 3

    print(f"[{datetime.now(timezone.utc).isoformat()}] loading candles...")
    candle_index = load_candle_index(research_url, args.symbol, start_ts, end_ts + timedelta(hours=2))
    print(f"  → {len(candle_index)} candle bars")

    print(f"[{datetime.now(timezone.utc).isoformat()}] enriching...")
    enriched = enrich(samples, candle_index)
    print(f"  → {len(enriched)} enriched samples (after ts match)")

    if args.aggregate_bar:
        before = len(enriched)
        enriched = _aggregate_by_bar(enriched)
        print(f"  → {len(enriched)} samples after bar-level aggregation (from {before})")

    if len(enriched) < 50:
        print("  ! too few enriched samples", file=sys.stderr)
        return 3

    # 额外统计：direction_bias 分布
    dir_counts: dict[str, int] = {}
    for s in enriched:
        dir_counts[s.direction_bias] = dir_counts.get(s.direction_bias, 0) + 1
    print(f"  direction_bias distribution: {dir_counts}")

    # composite_alpha 符号分布
    alpha_pos = sum(1 for s in enriched if s.composite_alpha > 0)
    alpha_neg = sum(1 for s in enriched if s.composite_alpha < 0)
    alpha_zero = len(enriched) - alpha_pos - alpha_neg
    print(f"  composite_alpha sign: >0={alpha_pos}, <0={alpha_neg}, =0={alpha_zero}")

    print(f"[{datetime.now(timezone.utc).isoformat()}] computing regressions (OLD pre-H4)...")
    old_res = compute_regression_set(enriched, score_old_pre_h4, "pre-H4")

    print(f"[{datetime.now(timezone.utc).isoformat()}] computing regressions (NEW post-H4)...")
    new_res = compute_regression_set(enriched, score_new_post_h4, "post-H4")

    print("")
    print("=" * 90)
    print("H4 修复验证 — OLS 回归对比 (realized_edge_bps vs leg_score)")
    print("=" * 90)
    print(f"{'leg':<6} {'horizon':<10} {'side':<8} {'n':>5}  {'slope':>10}  {'R²':>8}  {'pearson':>8}  {'score_mean':>10}  {'nz%':>6}")
    print("-" * 90)
    for leg in ("long", "short"):
        for h_name, _, _ in HORIZONS:
            key = f"{leg}_{h_name}"
            for tag, res in (("OLD", old_res), ("NEW", new_res)):
                r = res.get(key, {})
                n = r.get("n", 0)
                if n < 20:
                    continue
                print(f"{leg:<6} {h_name:<10} {tag:<8} {n:>5}  "
                      f"{r.get('slope', float('nan')):>+10.3f}  "
                      f"{r.get('r_squared', float('nan')):>8.5f}  "
                      f"{r.get('pearson_r', float('nan')):>+8.4f}  "
                      f"{r.get('score_mean', 0):>10.4f}  "
                      f"{r.get('score_nonzero_pct', 0):>6.1f}")
            print("")

    # 验收门槛 (15m horizon)
    print("-" * 90)
    print("验收门槛检查 (15m horizon):")
    long_15m_new = new_res.get("long_15m", {})
    short_15m_new = new_res.get("short_15m", {})

    checks = []
    if short_15m_new.get("r_squared", float("-inf")) >= 0.01:
        checks.append(("short R² >= 0.01", True, short_15m_new.get("r_squared")))
    else:
        checks.append(("short R² >= 0.01", False, short_15m_new.get("r_squared")))
    if short_15m_new.get("slope", float("-inf")) > 0:
        checks.append(("short slope > 0", True, short_15m_new.get("slope")))
    else:
        checks.append(("short slope > 0", False, short_15m_new.get("slope")))
    if long_15m_new.get("r_squared", float("-inf")) >= 0.012:
        checks.append(("long R² >= 0.012", True, long_15m_new.get("r_squared")))
    else:
        checks.append(("long R² >= 0.012", False, long_15m_new.get("r_squared")))
    if long_15m_new.get("slope", float("-inf")) >= 12.0:
        checks.append(("long slope >= +12", True, long_15m_new.get("slope")))
    else:
        checks.append(("long slope >= +12", False, long_15m_new.get("slope")))

    all_ok = True
    for name, ok, val in checks:
        sym = "✓" if ok else "✗"
        print(f"  {sym} {name}: {val}")
        all_ok = all_ok and ok

    print("")
    if all_ok:
        print("==> 所有验收门槛通过")
        return 0
    else:
        print("==> 部分门槛未通过")
        return 4


if __name__ == "__main__":
    sys.exit(main())
