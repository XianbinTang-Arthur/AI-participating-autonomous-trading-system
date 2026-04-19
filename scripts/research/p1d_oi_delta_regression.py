"""P1-D Stage 5 预览回归: OI delta × sign(ΔP) 对 realized_return 的 OLS 回归.

这是 P1-D Phase 2A 提前决策的核心证据:
  - 如果 OI delta × sign(ΔP) 特征在 1h / 4h / 1d horizon 上
    R² ≥ 0.010 且 cross-window slope 稳定 → GO hint, Phase 2A 直接上
  - 如果 R² 在 0.005-0.010 → CONDITIONAL hint
  - 如果 R² < 0.005 → NO-GO hint, 可能需要 Tardis.dev trades 更精细 order-flow

特征定义 (对齐 docs/design/p1d_microstructure_feasibility_2026_04_19.md §1.3)
--------------------------------------------------------------------
对每个 1h bar t (OI 原生粒度):
  - oi_delta[t]         = (oi[t] - oi[t-1]) / oi[t-1]
  - price_change[t]     = (close[t] - close[t-1]) / close[t-1]
  - sign_dp[t]          = sign(price_change[t])
  - signed_oi_delta[t]  = oi_delta[t] × sign_dp[t]

y = forward realized return:
  - realized_ret_1h  = (close[t+1] - close[t]) / close[t]
  - realized_ret_4h  = (close[t+4] - close[t]) / close[t]
  - realized_ret_1d  = (close[t+24] - close[t]) / close[t]
  (bps = × 10_000)

为什么用 signed_oi_delta?
  - P1-D 可行性 §1.3: "OI ↑ + price ↑ 同向 → 新多头开仓 (bullish)"
                     "OI ↑ + price ↓ 同向 → 新空头开仓 (bearish)"
                     "OI ↓ + price ↑ 反向 → 空头平仓 (short squeeze, bullish)"
                     "OI ↓ + price ↓ 反向 → 多头平仓 (long liquidation, bearish)"
  - sign(ΔP) × oi_delta 把上述四象限编码为一个方向性指标:
      新多头开仓 (+, +) → signed_oi_delta > 0 (bullish)
      新空头开仓 (+, -) → signed_oi_delta < 0 (bearish)
      空头平仓 (-, +) → signed_oi_delta < 0 (但语义 bullish)
      多头平仓 (-, -) → signed_oi_delta > 0 (但语义 bearish)
  - 在简单 OLS 下 (+, +) 和 (-, +) 系列被折叠; 真正的 4-state regime
    需要 bucket 分析. 本脚本做了 baseline OLS + 4 象限分 regime.

数据源
------
- bronze.market_oi_history_1h: 本次 backfill 刚下的 60 天 OI
- silver.market_swap_candles_15m: 已有的 15m swap candles (33 天)
- 也可 fall back 到 silver.market_swap_candles_1h 做对齐

回归方法 (对齐 p1d_preview_regression_funding_basis.py)
-------------------------------------------------------
- X = signed_oi_delta, oi_delta, abs_oi_delta (3 变体)
- y = realized_bps_{1h, 4h, 1d}
- Split: train 70% / test 30%, 时间顺序, 禁 look-ahead
- Cross-window: 前半 vs 后半, 比 slope 符号
- Sign regime: 4 象限 (signed_oi_delta > 0 / < 0, 与 |oi_delta| > p50)
- 扣成本 mean_net @ q80/q90 (cost=6.0 bps)

用法
----
    python scripts/research/p1d_oi_delta_regression.py \
        --symbol BTC-USDT-SWAP \
        --output docs/research/p1d_oi_delta_regression_2026_04_20.md

退出码: 0 正常, 1 参数错误, 2 数据/DB 错误.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    print("missing python-dotenv; pip install python-dotenv", file=sys.stderr)
    sys.exit(2)

_ENV_SEARCH_ROOTS: list[Path] = [ROOT]
_home = os.environ.get("HOME")
if _home:
    _ENV_SEARCH_ROOTS.append(Path(_home) / "aats")

for env_file in (".env.wsl2", ".env.research", ".env.derivatives.live"):
    for search_root in _ENV_SEARCH_ROOTS:
        env_path = search_root / env_file
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            break


from sqlalchemy import create_engine, text  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────


def resolve_db_url() -> str:
    for key in ("RDP_DATABASE_URL",):
        val = os.environ.get(key)
        if val:
            return val
    user = os.environ.get("POSTGRES_USER", "admin")
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        raise SystemExit(
            "missing credentials: set POSTGRES_PASSWORD or RDP_DATABASE_URL "
            "in .env.wsl2 / .env.research"
        )
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("RDP_POSTGRES_DB", "aats_research")
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


def fetch_oi_history(engine, symbol: str, start_ts: datetime, end_ts: datetime) -> list:
    sql = text(
        "SELECT ts, oi, oi_ccy "
        "FROM bronze.market_oi_history_1h "
        "WHERE symbol = :s AND ts >= :start AND ts <= :end "
        "ORDER BY ts"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": symbol, "start": start_ts, "end": end_ts}).fetchall()


def fetch_swap_candles_1h(
    engine, symbol: str, start_ts: datetime, end_ts: datetime
) -> list:
    """优先用 silver.market_swap_candles_1h. fallback: 从 15m 聚合出 1h."""
    sql_1h = text(
        "SELECT ts, open, high, low, close, vol "
        "FROM silver.market_swap_candles_1h "
        "WHERE symbol = :s AND ts >= :start AND ts <= :end "
        "ORDER BY ts"
    )
    with engine.connect() as conn:
        rows = conn.execute(sql_1h, {"s": symbol, "start": start_ts, "end": end_ts}).fetchall()
        if rows:
            return rows

    # Fallback: aggregate 15m → 1h
    sql_15m = text(
        """
        SELECT
            date_trunc('hour', ts) AS ts,
            (array_agg(open ORDER BY ts ASC))[1] AS open,
            max(high) AS high,
            min(low) AS low,
            (array_agg(close ORDER BY ts DESC))[1] AS close,
            sum(COALESCE(vol, 0)) AS vol
        FROM silver.market_swap_candles_15m
        WHERE symbol = :s AND ts >= :start AND ts <= :end
        GROUP BY date_trunc('hour', ts)
        ORDER BY ts
        """
    )
    with engine.connect() as conn:
        return conn.execute(sql_15m, {"s": symbol, "start": start_ts, "end": end_ts}).fetchall()


# ─────────────────────────────────────────────────────────────────────
# Data normalization
# ─────────────────────────────────────────────────────────────────────


def to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass
class Row:
    ts: datetime
    close: float
    oi: float


def align_oi_and_candles(
    oi_rows: list,
    candle_rows: list,
) -> list[Row]:
    """把 1h OI 与 1h candle 按 ts 对齐.

    注意 OI 的 ts 是 bar 起点 UTC 对齐; candle 同样. 直接按 ts 精确 match.
    若 candle 缺失某个 OI ts, 则丢弃该 OI row (无法算 price change).
    """
    candle_idx = {to_utc(r.ts): float(r.close) for r in candle_rows if r.close is not None}
    out: list[Row] = []
    for r in oi_rows:
        ts = to_utc(r.ts)
        close = candle_idx.get(ts)
        if close is None or close <= 0:
            continue
        try:
            oi = float(r.oi)
        except (TypeError, ValueError):
            continue
        if oi <= 0:
            continue
        out.append(Row(ts=ts, close=close, oi=oi))
    return out


# ─────────────────────────────────────────────────────────────────────
# Feature engineering
# ─────────────────────────────────────────────────────────────────────


@dataclass
class FeatureRow:
    ts: datetime
    close: float
    oi: float
    oi_delta: float | None = None            # (oi[t] - oi[t-1]) / oi[t-1]
    abs_oi_delta: float | None = None        # abs value
    price_change: float | None = None         # (close[t] - close[t-1]) / close[t-1]
    sign_dp: int | None = None                # +1 / -1 / 0
    signed_oi_delta: float | None = None      # oi_delta × sign_dp
    # forward realized returns (bps)
    realized_bps_1h: float | None = None
    realized_bps_4h: float | None = None
    realized_bps_1d: float | None = None


HORIZON_FIELDS = [
    ("1h", "realized_bps_1h", 1),
    ("4h", "realized_bps_4h", 4),
    ("1d", "realized_bps_1d", 24),
]

FEATURE_KEYS = ["signed_oi_delta", "oi_delta", "abs_oi_delta"]


def compute_features(rows: list[Row]) -> list[FeatureRow]:
    """对齐后的 1h bar 序列上计算 OI delta × sign(ΔP) 特征.

    - oi_delta[t]        = (oi[t] - oi[t-1]) / oi[t-1]
    - price_change[t]    = (close[t] - close[t-1]) / close[t-1]
    - sign_dp[t]         = sign(price_change[t])
    - signed_oi_delta[t] = oi_delta[t] × sign_dp[t]

    y 是 forward realized return, 只用 <= 该 bar ts 的特征计算.
    """
    n = len(rows)
    out: list[FeatureRow] = []
    for i, r in enumerate(rows):
        fr = FeatureRow(ts=r.ts, close=r.close, oi=r.oi)

        if i >= 1:
            prev = rows[i - 1]
            if prev.oi > 0:
                fr.oi_delta = (r.oi - prev.oi) / prev.oi
                fr.abs_oi_delta = abs(fr.oi_delta)
            if prev.close > 0:
                fr.price_change = (r.close - prev.close) / prev.close
                if fr.price_change > 0:
                    fr.sign_dp = 1
                elif fr.price_change < 0:
                    fr.sign_dp = -1
                else:
                    fr.sign_dp = 0
            if fr.oi_delta is not None and fr.sign_dp is not None:
                fr.signed_oi_delta = fr.oi_delta * fr.sign_dp

        # forward realized return
        def _fwd(h: int) -> float | None:
            j = i + h
            if j >= n:
                return None
            c_now = r.close
            c_fwd = rows[j].close
            if c_now > 0 and c_fwd > 0:
                return (c_fwd - c_now) / c_now * 10_000.0
            return None

        fr.realized_bps_1h = _fwd(1)
        fr.realized_bps_4h = _fwd(4)
        fr.realized_bps_1d = _fwd(24)

        out.append(fr)
    return out


# ─────────────────────────────────────────────────────────────────────
# Regression helpers
# ─────────────────────────────────────────────────────────────────────


def ols_1d(xs: list[float], ys: list[float]) -> dict:
    n = len(xs)
    if n < 2:
        return {"r2": float("nan"), "slope": float("nan"), "intercept": float("nan"),
                "pearson_r": float("nan"), "n": n}
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0 or syy == 0:
        return {"r2": 0.0, "slope": 0.0, "intercept": my, "pearson_r": 0.0, "n": n}
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / syy
    pearson_r = sxy / math.sqrt(sxx * syy)
    return {"r2": r2, "slope": slope, "intercept": intercept, "pearson_r": pearson_r, "n": n}


def train_test(xs: list[float], ys: list[float], train_frac: float = 0.7) -> dict:
    n = len(xs)
    if n < 20:
        return {"train": {"r2": float("nan"), "n": n}, "test": {"r2": float("nan"), "n": 0}}
    cut = int(n * train_frac)
    train_fit = ols_1d(xs[:cut], ys[:cut])
    tx, ty = xs[cut:], ys[cut:]
    ntest = len(tx)
    slope = train_fit["slope"]
    intercept = train_fit["intercept"]
    if ntest < 2 or slope != slope:
        return {"train": train_fit, "test": {"r2": float("nan"), "n": ntest}}
    my = sum(ty) / ntest
    syy = sum((y - my) ** 2 for y in ty)
    if syy == 0:
        return {"train": train_fit, "test": {"r2": 0.0, "slope": slope, "pearson_r": 0.0, "n": ntest}}
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(tx, ty))
    r2_test = 1.0 - ss_res / syy
    mx_t = sum(tx) / ntest
    sxx = sum((x - mx_t) ** 2 for x in tx)
    sxy = sum((x - mx_t) * (y - my) for x, y in zip(tx, ty))
    pr = sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0
    return {
        "train": train_fit,
        "test": {"r2": r2_test, "slope": slope, "intercept": intercept, "pearson_r": pr, "n": ntest},
    }


def collect_xy(
    rows: list[FeatureRow], feature_key: str, horizon_field: str,
    filter_fn=None,
) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for r in rows:
        x = getattr(r, feature_key, None)
        y = getattr(r, horizon_field, None)
        if x is None or y is None:
            continue
        if filter_fn is not None and not filter_fn(r):
            continue
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def net_bps_above_quantile(
    xs: list[float], ys: list[float], quantile: float, cost_bps: float,
) -> dict:
    """sign(x) * y - cost_bps for samples where |x| >= quantile of |x|."""
    if not xs:
        return {"n": 0}
    abs_x = sorted(abs(v) for v in xs)
    cut_idx = int(quantile * (len(abs_x) - 1))
    thr = abs_x[cut_idx]
    signed_nets = []
    for x, y in zip(xs, ys):
        if abs(x) < thr or x == 0:
            continue
        signal_dir = 1 if x > 0 else -1
        signed = signal_dir * y - cost_bps
        signed_nets.append(signed)
    if not signed_nets:
        return {"n": 0, "threshold_abs": thr}
    n = len(signed_nets)
    mean = sum(signed_nets) / n
    var = sum((v - mean) ** 2 for v in signed_nets) / max(n - 1, 1)
    std = math.sqrt(var)
    wins = sum(1 for v in signed_nets if v > 0)
    return {
        "n": n,
        "threshold_abs": thr,
        "mean_net_bps": mean,
        "std_net_bps": std,
        "win_rate": wins / n,
        "pct_traded": n / len(xs),
    }


def split_windows(rows: list[FeatureRow], n_windows: int = 2) -> list[list[FeatureRow]]:
    if not rows:
        return []
    k = len(rows)
    chunk = k // n_windows
    out = []
    for i in range(n_windows):
        lo = i * chunk
        hi = k if i == n_windows - 1 else (i + 1) * chunk
        out.append(rows[lo:hi])
    return out


def fmt(x: float, digits: int = 4) -> str:
    if x is None or x != x:
        return "nan"
    if abs(x) > 1e8:
        return f"{x:.2e}"
    return f"{x:.{digits}f}"


# ─────────────────────────────────────────────────────────────────────
# 4-quadrant OI regime analysis
# ─────────────────────────────────────────────────────────────────────


def quadrant_analysis(rows: list[FeatureRow], horizon_field: str) -> dict:
    """4 象限: (oi ↑ / oi ↓) × (price ↑ / price ↓).

    返回每象限的 n, mean_realized_bps, std, slope(signed_oi_delta vs realized).
    """
    buckets = {
        "oi↑_price↑ (新多开)": lambda r: (r.oi_delta or 0) > 0 and (r.price_change or 0) > 0,
        "oi↑_price↓ (新空开)": lambda r: (r.oi_delta or 0) > 0 and (r.price_change or 0) < 0,
        "oi↓_price↑ (空平/short_squeeze)": lambda r: (r.oi_delta or 0) < 0 and (r.price_change or 0) > 0,
        "oi↓_price↓ (多平/long_flush)": lambda r: (r.oi_delta or 0) < 0 and (r.price_change or 0) < 0,
    }
    out = {}
    for label, filt in buckets.items():
        ys = []
        for r in rows:
            if filt(r):
                y = getattr(r, horizon_field)
                if y is not None:
                    ys.append(float(y))
        if not ys:
            out[label] = {"n": 0}
            continue
        n = len(ys)
        m = sum(ys) / n
        var = sum((y - m) ** 2 for y in ys) / max(n - 1, 1)
        out[label] = {
            "n": n,
            "mean_realized_bps": m,
            "std_bps": math.sqrt(var),
            "median_bps": sorted(ys)[n // 2],
        }
    return out


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC-USDT-SWAP")
    ap.add_argument("--days", type=int, default=60,
                    help="加载过去 N 天 OI + candle")
    ap.add_argument("--output", default=None)
    ap.add_argument("--cost-bps", type=float, default=6.0)
    args = ap.parse_args()

    url = resolve_db_url()
    engine = create_engine(url)

    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=args.days + 1)

    print(f"[{datetime.now(timezone.utc).isoformat()}] loading OI history {args.symbol}...")
    oi_rows = fetch_oi_history(engine, args.symbol, start_ts, end_ts + timedelta(hours=1))
    print(f"  -> {len(oi_rows)} OI rows")
    if not oi_rows:
        print("ERROR: 无 OI 数据; 先跑 scripts/rdp_backfill_okx_rest_history.py --apply",
              file=sys.stderr)
        return 2

    print(f"[{datetime.now(timezone.utc).isoformat()}] loading 1h swap candles {args.symbol}...")
    candle_rows = fetch_swap_candles_1h(
        engine, args.symbol, start_ts, end_ts + timedelta(hours=1)
    )
    print(f"  -> {len(candle_rows)} candle rows")
    if not candle_rows:
        print("ERROR: 无 swap candles 数据", file=sys.stderr)
        return 2

    rows = align_oi_and_candles(oi_rows, candle_rows)
    print(f"[{datetime.now(timezone.utc).isoformat()}] aligned {len(rows)} 1h bars (OI + close)")

    if len(rows) < 50:
        print("ERROR: aligned samples < 50, 样本太少无法回归", file=sys.stderr)
        return 2

    features = compute_features(rows)
    features_warm = features[1:]  # drop first (oi_delta / price_change 需 t-1)

    nd = sum(1 for r in features_warm if r.signed_oi_delta is not None)
    print(f"  -> {len(features_warm)} feature rows (signed_oi_delta non-null: {nd})")

    # ── 主矩阵 ──
    print("\n" + "=" * 112)
    print("P1-D Stage 5 OI delta 回归 — test R² / slope / Pearson r (train 70% / test 30%)")
    print("=" * 112)

    results: dict[str, dict[str, dict]] = {}
    for fkey in FEATURE_KEYS:
        results[fkey] = {}
        for hname, hfield, _bars in HORIZON_FIELDS:
            xs, ys = collect_xy(features_warm, fkey, hfield)
            res = train_test(xs, ys)
            results[fkey][hname] = res

    header = (f"{'feature':<22} {'horizon':<6} {'n_tr':>5} {'n_te':>5}  "
              f"{'tr R²':>9} {'te R²':>9} {'slope':>12} {'pearson':>9}")
    print(header)
    print("-" * len(header))
    for fkey in FEATURE_KEYS:
        for hname, _hfield, _bars in HORIZON_FIELDS:
            r = results[fkey][hname]
            tr = r.get("train", {})
            te = r.get("test", {})
            print(f"{fkey:<22} {hname:<6} {tr.get('n', 0):>5} {te.get('n', 0):>5}  "
                  f"{fmt(tr.get('r2', float('nan')), 5):>9} "
                  f"{fmt(te.get('r2', float('nan')), 5):>9} "
                  f"{fmt(te.get('slope', float('nan')), 2):>12} "
                  f"{fmt(te.get('pearson_r', float('nan')), 4):>9}")
        print()

    # ── cross-window ──
    print("=" * 112)
    print("Cross-window 稳健性: 前半 vs 后半 — 1h horizon only")
    print("=" * 112)
    halves = split_windows(features_warm, n_windows=2)
    labels = ["first_half", "second_half"]
    cross_header = (f"{'feature':<22} {'window':<14} {'n_tr':>5} {'n_te':>5}  "
                    f"{'tr R²':>9} {'te R²':>9} {'slope':>12} {'pearson':>9}")
    print(cross_header)
    print("-" * len(cross_header))
    for fkey in FEATURE_KEYS:
        for lbl, half in zip(labels, halves):
            xs, ys = collect_xy(half, fkey, "realized_bps_1h")
            r = train_test(xs, ys)
            tr = r.get("train", {})
            te = r.get("test", {})
            print(f"{fkey:<22} {lbl:<14} {tr.get('n', 0):>5} {te.get('n', 0):>5}  "
                  f"{fmt(tr.get('r2', float('nan')), 5):>9} "
                  f"{fmt(te.get('r2', float('nan')), 5):>9} "
                  f"{fmt(te.get('slope', float('nan')), 2):>12} "
                  f"{fmt(te.get('pearson_r', float('nan')), 4):>9}")
        print()

    # ── 4 象限 ──
    print("=" * 112)
    print("4 象限 OI regime 分析 (全样本, 各 horizon)")
    print("=" * 112)
    quad_header = (f"{'quadrant':<32} {'horizon':<6} {'n':>5} "
                   f"{'mean_bps':>12} {'median_bps':>12} {'std_bps':>12}")
    print(quad_header)
    print("-" * len(quad_header))
    for hname, hfield, _bars in HORIZON_FIELDS:
        quads = quadrant_analysis(features_warm, hfield)
        for label, q in quads.items():
            n = q.get("n", 0)
            if n == 0:
                print(f"{label:<32} {hname:<6} {n:>5}  (none)")
            else:
                print(f"{label:<32} {hname:<6} {n:>5} "
                      f"{fmt(q.get('mean_realized_bps', float('nan')), 2):>12} "
                      f"{fmt(q.get('median_bps', float('nan')), 2):>12} "
                      f"{fmt(q.get('std_bps', float('nan')), 2):>12}")
        print()

    # ── 扣成本 net PnL ──
    print("=" * 112)
    print(f"扣成本 mean_net_bps @ q80/q90 (cost={args.cost_bps} bps) — 1h horizon")
    print("=" * 112)
    pnl_header = (f"{'feature':<22} {'quantile':<10} {'n':>5}  "
                  f"{'mean_net_bps':>14} {'std':>10} {'win_rate':>10} {'pct_traded':>12}")
    print(pnl_header)
    print("-" * len(pnl_header))
    q_pnl: dict[str, dict[str, dict]] = {}
    for fkey in FEATURE_KEYS:
        q_pnl[fkey] = {}
        xs, ys = collect_xy(features_warm, fkey, "realized_bps_1h")
        for q in (0.80, 0.90):
            res = net_bps_above_quantile(xs, ys, q, args.cost_bps)
            q_pnl[fkey][f"q{int(q*100)}"] = res
            print(f"{fkey:<22} q{int(q*100):<9} {res.get('n', 0):>5}  "
                  f"{fmt(res.get('mean_net_bps', float('nan')), 2):>14} "
                  f"{fmt(res.get('std_net_bps', float('nan')), 2):>10} "
                  f"{fmt(res.get('win_rate', float('nan')), 3):>10} "
                  f"{fmt(res.get('pct_traded', float('nan')), 3):>12}")

    # ── verdict ──
    print()
    print("=" * 112)
    print("VERDICT 判定")
    print("=" * 112)
    best_feat = None
    best_r2 = float("-inf")
    best_horizon = None
    best_slope = float("nan")
    for fkey in FEATURE_KEYS:
        for hname, _hfield, _bars in HORIZON_FIELDS:
            te = results[fkey][hname]["test"]
            r2 = te.get("r2", float("nan"))
            if r2 == r2 and r2 > best_r2:
                best_r2 = r2
                best_feat = fkey
                best_horizon = hname
                best_slope = te.get("slope", float("nan"))
    if best_feat is None:
        best_r2 = float("nan")
        best_feat = "none"

    # cross-window sign stability for best feat @ 1h
    h1_xs, h1_ys = collect_xy(halves[0], best_feat, "realized_bps_1h") if halves else ([], [])
    h2_xs, h2_ys = collect_xy(halves[1], best_feat, "realized_bps_1h") if len(halves) >= 2 else ([], [])
    h1_fit = train_test(h1_xs, h1_ys)["test"] if h1_xs else {}
    h2_fit = train_test(h2_xs, h2_ys)["test"] if h2_xs else {}
    sign_stable = False
    s1 = h1_fit.get("slope", float("nan"))
    s2 = h2_fit.get("slope", float("nan"))
    if s1 == s1 and s2 == s2:
        sign_stable = (s1 * s2) > 0

    qres = q_pnl.get(best_feat, {}).get("q80", {})
    mean_net = qres.get("mean_net_bps", float("-inf"))

    print(f"best feature × horizon: {best_feat} @ {best_horizon} (test R²={fmt(best_r2, 5)}, "
          f"slope={fmt(best_slope, 2)})")
    print(f"cross-window slope sign stable (1h): {sign_stable}  "
          f"(first={fmt(s1, 2)}, second={fmt(s2, 2)})")
    print(f"q80 mean_net_bps: {fmt(mean_net, 2)}  (n={qres.get('n', 0)})")

    if best_r2 >= 0.010 and sign_stable and mean_net > 2.0 and qres.get("n", 0) >= 50:
        verdict = "GO hint: R² >= 0.010, sign 稳定, q80 mean_net > 2 bps"
    elif best_r2 >= 0.010 and sign_stable:
        verdict = "CONDITIONAL hint: R² >= 0.010 但 q80 mean_net 不足"
    elif best_r2 >= 0.005:
        verdict = "CONDITIONAL hint: 0.005 <= R² < 0.010, marginal"
    else:
        verdict = "NO-GO hint: R² < 0.005"
    print(f"\nverdict: {verdict}")

    # ── markdown output ──
    if args.output:
        write_markdown(
            args.output,
            symbol=args.symbol,
            days=args.days,
            cost_bps=args.cost_bps,
            n_rows=len(features_warm),
            results=results,
            cross_halves=halves,
            q_pnl=q_pnl,
            best_feat=best_feat or "none",
            best_horizon=best_horizon or "1h",
            best_r2=best_r2,
            best_slope=best_slope,
            sign_stable=sign_stable,
            h1_fit=h1_fit,
            h2_fit=h2_fit,
            verdict=verdict,
            features_warm=features_warm,
        )
        print(f"\nwrote markdown report: {args.output}")

    return 0


def write_markdown(
    path: str,
    *,
    symbol: str,
    days: int,
    cost_bps: float,
    n_rows: int,
    results: dict,
    cross_halves: list,
    q_pnl: dict,
    best_feat: str,
    best_horizon: str,
    best_r2: float,
    best_slope: float,
    sign_stable: bool,
    h1_fit: dict,
    h2_fit: dict,
    verdict: str,
    features_warm: list,
) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# P1-D Stage 5 OI Delta × sign(ΔP) 回归 (2026-04-20)")
    lines.append("")
    lines.append("> 项目定位声明: 本文件默认服从 AATS 的统一目标. "
                 "详见 [项目定位声明](../../docs/project_positioning.md).")
    lines.append("")
    lines.append("**Scope**: 用本次 Stage 5 OKX REST 回填的 60 天 `bronze.market_oi_history_1h` "
                 "+ `silver.market_swap_candles_1h` (从 15m 聚合或原生) 做 OI delta × sign(ΔP) "
                 "对 forward realized return 的 OLS 回归, 为 P1-D Phase 2A 门槛提供 hint.")
    lines.append("")
    lines.append(f"**标的**: `{symbol}`, 1h bar (OI 原生粒度) ")
    lines.append(f"**样本**: {n_rows} 行 (warmup 1 bar 后; 实际 OI 深度受限于 OKX ~60 天)")
    lines.append(f"**Cost 假设**: {cost_bps:.1f} bps (taker 5 + slip 1, 与线上一致)")
    lines.append(f"**生成日期**: 2026-04-20")
    lines.append("")

    lines.append("## TL;DR")
    lines.append("")
    lines.append(f"- **最高 R² (test)**: `{best_feat}` @ {best_horizon} — "
                 f"R²={fmt(best_r2, 5)}, slope={fmt(best_slope, 2)}")
    s1 = h1_fit.get("slope", float("nan"))
    s2 = h2_fit.get("slope", float("nan"))
    lines.append(f"- **Cross-window slope sign stable (1h)**: "
                 f"**{'YES' if sign_stable else 'NO'}** "
                 f"(first={fmt(s1, 2)}, second={fmt(s2, 2)})")
    qres = q_pnl.get(best_feat, {}).get("q80", {})
    lines.append(f"- **q80 扣成本 mean_net_bps**: {fmt(qres.get('mean_net_bps', float('nan')), 2)} bps "
                 f"(n={qres.get('n', 0)})")
    lines.append(f"- **P1-D Phase 2A Hint**: **{verdict}**")
    lines.append("")

    lines.append("**门槛参考 (P1-D 可行性 §8.2)**:")
    lines.append("")
    lines.append("- GO: R² ≥ 0.010 且 cross-window sign 稳定 且 q80 mean_net_bps > 2 bps")
    lines.append("- CONDITIONAL: 0.005 ≤ R² < 0.010 或 regime-specific 强 global 弱")
    lines.append("- NO-GO: R² < 0.005 across all features & horizons")
    lines.append("")

    lines.append("## 主矩阵: feature × horizon (test set)")
    lines.append("")
    lines.append("| feature | horizon | n_tr | n_te | train R² | test R² | slope | pearson r |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for fkey in FEATURE_KEYS:
        for hname, _hfield, _bars in HORIZON_FIELDS:
            r = results[fkey][hname]
            tr = r.get("train", {})
            te = r.get("test", {})
            lines.append(
                f"| `{fkey}` | {hname} | {tr.get('n', 0)} | {te.get('n', 0)} | "
                f"{fmt(tr.get('r2', float('nan')), 5)} | "
                f"{fmt(te.get('r2', float('nan')), 5)} | "
                f"{fmt(te.get('slope', float('nan')), 2)} | "
                f"{fmt(te.get('pearson_r', float('nan')), 4)} |"
            )
    lines.append("")

    lines.append("## Cross-window 稳健性 (1h horizon)")
    lines.append("")
    lines.append("分 2 半各自跑 train/test, 比 slope 符号:")
    lines.append("")
    lines.append("| feature | window | n_tr | n_te | test R² | slope | pearson r |")
    lines.append("|---|---|---|---|---|---|---|")
    labels = ["first_half", "second_half"]
    for fkey in FEATURE_KEYS:
        for lbl, half in zip(labels, cross_halves):
            xs, ys = collect_xy(half, fkey, "realized_bps_1h")
            r = train_test(xs, ys)
            tr = r.get("train", {})
            te = r.get("test", {})
            lines.append(
                f"| `{fkey}` | {lbl} | {tr.get('n', 0)} | {te.get('n', 0)} | "
                f"{fmt(te.get('r2', float('nan')), 5)} | "
                f"{fmt(te.get('slope', float('nan')), 2)} | "
                f"{fmt(te.get('pearson_r', float('nan')), 4)} |"
            )
    lines.append("")

    lines.append("## 4 象限 OI regime 分析 (全样本, 每象限 mean_realized_bps)")
    lines.append("")
    lines.append("| quadrant | horizon | n | mean_realized_bps | median_bps | std_bps |")
    lines.append("|---|---|---|---|---|---|")
    for hname, hfield, _bars in HORIZON_FIELDS:
        quads = quadrant_analysis(features_warm, hfield)
        for label, q in quads.items():
            n = q.get("n", 0)
            if n == 0:
                lines.append(f"| {label} | {hname} | 0 | nan | nan | nan |")
            else:
                lines.append(
                    f"| {label} | {hname} | {n} | "
                    f"{fmt(q.get('mean_realized_bps', float('nan')), 2)} | "
                    f"{fmt(q.get('median_bps', float('nan')), 2)} | "
                    f"{fmt(q.get('std_bps', float('nan')), 2)} |"
                )
    lines.append("")

    lines.append(f"## 扣成本 mean_net_bps @ q80 / q90 (cost={cost_bps} bps, 1h horizon)")
    lines.append("")
    lines.append("交易规则: sign(feature) 开仓, abs(feature) >= q80/q90 才入场, 持 1h.")
    lines.append("")
    lines.append("| feature | quantile | n | mean_net_bps | std | win_rate | pct_traded |")
    lines.append("|---|---|---|---|---|---|---|")
    for fkey in FEATURE_KEYS:
        for qkey in ("q80", "q90"):
            res = q_pnl.get(fkey, {}).get(qkey, {})
            lines.append(
                f"| `{fkey}` | {qkey} | {res.get('n', 0)} | "
                f"{fmt(res.get('mean_net_bps', float('nan')), 2)} | "
                f"{fmt(res.get('std_net_bps', float('nan')), 2)} | "
                f"{fmt(res.get('win_rate', float('nan')), 3)} | "
                f"{fmt(res.get('pct_traded', float('nan')), 3)} |"
            )
    lines.append("")

    lines.append("## 诚实判定 & 与 P1-D 预估对比")
    lines.append("")
    lines.append(f"**Verdict**: {verdict}")
    lines.append("")
    lines.append("**P1-D 可行性 §1.3 预估 R²=0.01-0.02** — 本次实测结果见主矩阵.")
    lines.append("")
    lines.append("**解读**:")
    lines.append("- signed_oi_delta 是 OI delta × sign(ΔP), 把 4 象限信号折叠到 1D;")
    lines.append("  4 象限分析更能暴露真正 edge 来自哪个 regime.")
    lines.append("- 4 小时 horizon vs 1 小时 vs 1 天 会有不同性质:")
    lines.append("  - 1h: 同步噪声 + microstructure 相关更强")
    lines.append("  - 4h: OI 动能持续性 (新开仓推动的延迟 move)")
    lines.append("  - 1d: mean reversion / macro noise, 通常 R² 更低")
    lines.append("")

    lines.append("## 方法学 & 限制")
    lines.append("")
    lines.append("- 对齐 `p1d_preview_regression_funding_basis.py`: OLS 1-var, train/test 70/30, 时间顺序.")
    lines.append("- 特征构造只用 <= t bar 的数据, **无 look-ahead**.")
    lines.append("- y 是 forward close-to-close 无成本 bps; 成本仅在 q80/q90 报表扣.")
    lines.append("- 4 象限按 oi_delta 和 price_change 符号切, 观察 non-linear effect.")
    lines.append("- **样本量有限**: 60 天 × 24 = 1440 bar, warmup 1 bar 后 ~1439 个有效点.")
    lines.append("  统计功效 ± 0.005 R² 置信区间约 ±0.004.")
    lines.append("- **OI 深度限制**: OKX REST open-interest-history 实测仅 60 天可回填 "
                 "(vs 我们要求的 90 天). 这是 API 硬约束, 不能突破.")
    lines.append("- Cost-adjusted PnL 假设全量开仓 (no allocator throttle);")
    lines.append("  真正 Phase 2A 还需要 sleeve_allocator 通道 cost + slippage 模型.")
    lines.append("")

    lines.append("## 后续建议 (Phase 2A 路径)")
    lines.append("")
    lines.append("根据本次 R²:")
    lines.append("")
    if "GO" in verdict:
        lines.append("- **GO 路径**: 建议 Phase 2A **直接上** OI delta × sign(ΔP) feature 到 baseline strategy:")
        lines.append("  1. 在 `aats/services/decision/features/` 加 `oi_delta_reason_codes` module")
        lines.append("  2. 配合现有 15m bar 用 forward-fill 把 1h OI delta 映射回 15m")
        lines.append("  3. sleeve_allocator confidence 阈值按 q80 (见上表) 定调参")
        lines.append("  4. 走 calibration → paper → dry-run → shadow → 灰度实盘")
    elif "CONDITIONAL" in verdict:
        lines.append("- **CONDITIONAL 路径**: R² 边缘, Phase 2A 需额外证据支撑:")
        lines.append("  1. 等 30 天 microstructure WS 数据到位后, 做 5m / 1m 粒度 OI 回归 (用 staging.market_oi_funding_ticks)")
        lines.append("  2. 4 象限分析里哪些 regime PnL 显著? 如只 1-2 象限强, 做 regime-gated 版本")
        lines.append("  3. 做 multivariate 回归 (signed_oi_delta + funding_z + basis) 看 marginal R²")
        lines.append("- **不建议直接上**: R² 不够 robust, 容易过拟合历史样本")
    else:
        lines.append("- **NO-GO 路径**: 本特征在 1h 粒度证据不足, 建议:")
        lines.append("  1. **pivot 到 Tardis.dev trades backfill** ($300-600 一次性): 取 30-60 天逐笔 trades 做 order-flow 特征, 按 P1-D §1.2 它是第二重要")
        lines.append("  2. 或等 microstructure WS 真实积累 30 天后做 bbo_imbalance 回归 (P1-D §1.2 第一重要)")
        lines.append("  3. 如果必须用 OI, 降 horizon 到 15m 或 30m, 但我们缺 5m/15m OI 历史; "
                     "需要单独 backfill period=15m 的 `bronze.market_oi_history_15m` (不在 Stage 5 scope)")
    lines.append("")

    lines.append("## 可复现")
    lines.append("")
    lines.append("```bash")
    lines.append("# 前提: scripts/rdp_backfill_okx_rest_history.py --apply 已跑")
    lines.append(f"python scripts/research/p1d_oi_delta_regression.py \\")
    lines.append(f"  --symbol {symbol} --days {days} --cost-bps {cost_bps} \\")
    lines.append(f"  --output {path}")
    lines.append("```")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
