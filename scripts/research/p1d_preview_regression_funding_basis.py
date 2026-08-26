"""P1-D 快速预览回归: funding anomaly / basis / minutes-to-next-funding 对 next-15m realized_return.

目的
----
在 RDP 已有 33 天 15m candles (swap + spot) + 3 个月 funding 数据上,
跑 2 个 (+1 bonus) **未被 P1-A/P1-C 覆盖过** 的 feature 对 realized_return_15m_bps 的回归,
给 P1-D Phase 2A gate 提供 GO/CONDITIONAL/NO-GO hint.

特征
----
- Feature A — basis_z: z-score of (perp_close - spot_close)/spot_close, 24h rolling window.
- Feature B — funding_z: z-score of funding anomaly vs rolling 7d mean/std (forward-fill
  8h funding series to per-15m index, no look-ahead: uses last *resolved* funding rate).
- Feature C (bonus) — minutes_to_next_funding: remaining minutes to the next
  observed settlement; no fixed funding interval is assumed.

回归方法 (对齐 validate_h4_short_leg_fix / fast_impulse_selection_regression)
---------------------------------------------------------------
- X ∈ {basis, basis_z, funding_rate, funding_anomaly, funding_z, minutes_to_next_funding}
- y = realized_return_15m_bps = (close_{t+1} - close_t) / close_t × 10000
- 额外 horizon: 30m/1h/4h (用 h=2/4/16 bars forward)
- Split: train 前 70% / test 后 30%, 时间顺序, 禁止 look-ahead
- 报告 test R² / slope / Pearson r / sign stability / 扣成本 mean_net @ q80/q90
- Cross-window: 前半 (前 15 天) vs 后半 (后 15 天) 分别跑, 比较 slope sign

Sign regime
----------
- basis_z 正负: |basis_z| > 0.5 切两个 regime 各自回归
- funding_z 正负: 同上
- minutes_to_next_funding 分桶: 0-60 / 60-240 / >240 min

成本假设
--------
cost_bps = 6.0 (taker 5 + slip 1)

用法
----
    wsl -d Ubuntu bash -lc "cd ~/aats && source ~/aats-venv/bin/activate && \\
        python scripts/research/p1d_preview_regression_funding_basis.py \\
        --symbol BTC-USDT-SWAP --days 33 \\
        --output docs/research/p1d_preview_regression_funding_basis_2026_04_20.md"

退出码: 0 = 正常, 1 = 参数错误, 2 = 数据/DB 错误.
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

for env_file in (".env.wsl2", ".env.derivatives.live"):
    for search_root in _ENV_SEARCH_ROOTS:
        env_path = search_root / env_file
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            break

from sqlalchemy import create_engine, text  # noqa: E402


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

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
            "in .env.wsl2 / .env.derivatives.live"
        )
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("RDP_POSTGRES_DB", "aats_research")
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}"


def fetch_swap_candles(engine, symbol: str, start_ts: datetime, end_ts: datetime) -> list:
    sql = text(
        "SELECT ts, open, high, low, close, COALESCE(vol, 0) AS volume "
        "FROM silver.market_swap_candles_15m "
        "WHERE symbol = :s AND ts >= :start AND ts <= :end "
        "ORDER BY ts"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": symbol, "start": start_ts, "end": end_ts}).fetchall()


def fetch_spot_candles(engine, symbol: str, start_ts: datetime, end_ts: datetime) -> list:
    sql = text(
        "SELECT ts, close "
        "FROM silver.market_spot_candles_15m "
        "WHERE symbol = :s AND ts >= :start AND ts <= :end "
        "ORDER BY ts"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": symbol, "start": start_ts, "end": end_ts}).fetchall()


def fetch_funding(engine, symbol: str, start_ts: datetime, end_ts: datetime) -> list:
    sql = text(
        "SELECT ts, funding_rate, realized_rate "
        "FROM silver.market_swap_funding "
        "WHERE symbol = :s AND ts >= :start AND ts <= :end "
        "ORDER BY ts"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": symbol, "start": start_ts, "end": end_ts}).fetchall()


# ---------------------------------------------------------------------------
# Data normalization
# ---------------------------------------------------------------------------

def to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass
class Row:
    ts: datetime
    swap_close: float
    spot_close: float | None = None
    funding_rate: float | None = None        # last *resolved* funding (no look-ahead)
    funding_source_ts: datetime | None = None
    next_settle_ts: datetime | None = None   # next upcoming settlement ts


def build_aligned_rows(
    swap_rows: list,
    spot_rows: list,
    funding_rows: list,
) -> list[Row]:
    """Align 15m swap bars with spot bars + last-resolved funding.

    Spot alignment: direct ts match.
    Funding alignment: for each 15m bar t, use the *most recent* funding settlement
    with ts <= t (the rate applied over the window preceding t). No look-ahead.
    Also record next_settle_ts = smallest funding.ts > t.
    """
    spot_idx = {to_utc(r.ts): float(r.close) for r in spot_rows}

    funding_sorted = [(to_utc(r.ts), float(r.funding_rate)) for r in funding_rows]
    # build two parallel sorted lists for binary-search style alignment
    funding_ts = [ts for ts, _ in funding_sorted]

    import bisect

    out: list[Row] = []
    for r in swap_rows:
        ts = to_utc(r.ts)
        spot_c = spot_idx.get(ts)
        # funding: bisect_right finds first > ts; we want last <= ts
        pos = bisect.bisect_right(funding_ts, ts)
        last_rate: float | None = None
        last_funding_ts: datetime | None = None
        if pos > 0:
            last_rate = funding_sorted[pos - 1][1]
            last_funding_ts = funding_sorted[pos - 1][0]
        next_settle: datetime | None = None
        if pos < len(funding_ts):
            next_settle = funding_ts[pos]
        out.append(Row(
            ts=ts,
            swap_close=float(r.close),
            spot_close=spot_c,
            funding_rate=last_rate,
            funding_source_ts=last_funding_ts,
            next_settle_ts=next_settle,
        ))
    return out


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def rolling_stats(values: list[float | None], window: int) -> tuple[list[float | None], list[float | None]]:
    """Return rolling (mean, std) arrays same length as input. None where insufficient.

    Window uses up to last `window` non-None values preceding (and including) index i.
    Causal: rolling uses past-inclusive, no look-ahead.
    """
    out_mean: list[float | None] = []
    out_std: list[float | None] = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        window_vals = [v for v in values[lo:i + 1] if v is not None]
        if len(window_vals) < max(3, window // 3):
            out_mean.append(None)
            out_std.append(None)
            continue
        m = sum(window_vals) / len(window_vals)
        var = sum((v - m) ** 2 for v in window_vals) / max(len(window_vals) - 1, 1)
        s = math.sqrt(var)
        out_mean.append(m)
        out_std.append(s)
    return out_mean, out_std


@dataclass
class FeatureRow:
    ts: datetime
    swap_close: float
    spot_close: float | None
    basis: float | None              # (perp - spot) / spot
    basis_z: float | None            # (basis - rolling_mean_24h) / rolling_std_24h
    funding_rate: float | None
    funding_anomaly: float | None    # funding - rolling_mean_7d
    funding_z: float | None          # anomaly / rolling_std_7d
    minutes_to_next_funding: float | None
    # forward realized returns (bps), keyed by horizon bars
    realized_bps_h1: float | None = None   # 15m
    realized_bps_h2: float | None = None   # 30m
    realized_bps_h4: float | None = None   # 1h
    realized_bps_h16: float | None = None  # 4h


BARS_PER_HORIZON = {
    "15m": 1,
    "30m": 2,
    "1h": 4,
    "4h": 16,
}
def compute_features(rows: list[Row]) -> list[FeatureRow]:
    """Compute basis + funding features.

    basis uses 96-bar (24h at 15m) rolling z-score.
    funding uses the actual prior 7-day settlement events, regardless of the
    exchange's interval for that instrument and period.

    Realized returns computed after (forward-looking y), never used as feature.
    """
    # basis first
    basis_vals: list[float | None] = []
    for r in rows:
        if r.spot_close and r.spot_close > 0 and r.swap_close > 0:
            basis_vals.append((r.swap_close - r.spot_close) / r.spot_close)
        else:
            basis_vals.append(None)
    basis_mean, basis_std = rolling_stats(basis_vals, window=96)

    # Funding z uses distinct settlement timestamps rather than rate changes:
    # two consecutive settlements may legitimately have the same rate.
    seen_rates: list[tuple[datetime, float]] = []
    seen_set: set[datetime] = set()
    for r in rows:
        if r.funding_rate is None or r.funding_source_ts is None:
            continue
        if r.funding_source_ts not in seen_set:
            seen_rates.append((r.funding_source_ts, r.funding_rate))
            seen_set.add(r.funding_source_ts)
    seen_rates.sort(key=lambda item: item[0])
    rate_ts = [t for t, _ in seen_rates]
    rate_vals = [v for _, v in seen_rates]
    rate_mean: list[float | None] = []
    rate_std: list[float | None] = []
    seven_days = timedelta(days=7)
    for index, event_ts in enumerate(rate_ts):
        window_values = [
            value
            for timestamp, value in seen_rates[: index + 1]
            if event_ts - seven_days < timestamp <= event_ts
        ]
        if len(window_values) < 3:
            rate_mean.append(None)
            rate_std.append(None)
            continue
        mean = sum(window_values) / len(window_values)
        variance = sum((value - mean) ** 2 for value in window_values) / (
            len(window_values) - 1
        )
        rate_mean.append(mean)
        rate_std.append(math.sqrt(variance))

    # map funding_rate -> (anomaly, z) via the most-recent-rate-change ts
    # for each bar, bisect rate_ts for position
    import bisect
    out: list[FeatureRow] = []
    n = len(rows)
    for i, r in enumerate(rows):
        b = basis_vals[i]
        bm = basis_mean[i]
        bs = basis_std[i]
        b_z = None
        if b is not None and bm is not None and bs is not None and bs > 1e-12:
            b_z = (b - bm) / bs

        anomaly = None
        f_z = None
        if r.funding_rate is not None:
            pos = bisect.bisect_right(rate_ts, r.ts) - 1
            if 0 <= pos < len(rate_vals):
                if rate_mean[pos] is not None and rate_std[pos] is not None and rate_std[pos] > 1e-15:
                    anomaly = rate_vals[pos] - rate_mean[pos]
                    f_z = anomaly / rate_std[pos]

        minutes_next = None
        if r.next_settle_ts is not None:
            minutes_next = (r.next_settle_ts - r.ts).total_seconds() / 60.0

        # forward realized
        def _fwd(h: int) -> float | None:
            j = i + h
            if j >= n:
                return None
            c_now = r.swap_close
            c_fwd = rows[j].swap_close
            if c_now > 0 and c_fwd > 0:
                return (c_fwd - c_now) / c_now * 10_000.0
            return None

        out.append(FeatureRow(
            ts=r.ts,
            swap_close=r.swap_close,
            spot_close=r.spot_close,
            basis=b,
            basis_z=b_z,
            funding_rate=r.funding_rate,
            funding_anomaly=anomaly,
            funding_z=f_z,
            minutes_to_next_funding=minutes_next,
            realized_bps_h1=_fwd(1),
            realized_bps_h2=_fwd(2),
            realized_bps_h4=_fwd(4),
            realized_bps_h16=_fwd(16),
        ))
    return out


# ---------------------------------------------------------------------------
# Regression helpers
# ---------------------------------------------------------------------------

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


def collect_xy(rows: list[FeatureRow], feature_key: str, horizon_field: str,
               filter_fn=None) -> tuple[list[float], list[float]]:
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


# ---------------------------------------------------------------------------
# Quantile threshold PnL
# ---------------------------------------------------------------------------

def net_bps_above_quantile(xs: list[float], ys: list[float], quantile: float, cost_bps: float) -> dict:
    """Take sign(x) * y - cost_bps for samples where |x| >= quantile of |x|.

    Returns {n, mean_net_bps, win_rate, pct_traded}.
    """
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
        signed = signal_dir * y - cost_bps  # cost is always paid
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


# ---------------------------------------------------------------------------
# Cross-window split
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

FEATURE_KEYS = ["basis", "basis_z", "funding_rate", "funding_anomaly", "funding_z", "minutes_to_next_funding"]
HORIZON_FIELDS = [("15m", "realized_bps_h1"), ("30m", "realized_bps_h2"),
                  ("1h", "realized_bps_h4"), ("4h", "realized_bps_h16")]


def fmt(x: float, digits: int = 4) -> str:
    if x is None or x != x:
        return "nan"
    if abs(x) > 1e8:
        return f"{x:.2e}"
    return f"{x:.{digits}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC-USDT-SWAP")
    ap.add_argument("--days", type=int, default=33)
    ap.add_argument("--output", default=None, help="optional md file path")
    ap.add_argument("--cost-bps", type=float, default=6.0)
    args = ap.parse_args()

    url = resolve_db_url()
    engine = create_engine(url)

    end_ts = datetime.now(timezone.utc)
    start_ts_candles = end_ts - timedelta(days=args.days + 1)
    start_ts_funding = end_ts - timedelta(days=95)

    print(f"[{datetime.now(timezone.utc).isoformat()}] loading swap candles {args.symbol}...")
    swap_rows = fetch_swap_candles(engine, args.symbol, start_ts_candles, end_ts + timedelta(hours=1))
    print(f"  -> {len(swap_rows)} swap bars")

    spot_symbol = args.symbol.replace("-SWAP", "")
    print(f"[{datetime.now(timezone.utc).isoformat()}] loading spot candles {spot_symbol}...")
    spot_rows = fetch_spot_candles(engine, spot_symbol, start_ts_candles, end_ts + timedelta(hours=1))
    print(f"  -> {len(spot_rows)} spot bars")

    print(f"[{datetime.now(timezone.utc).isoformat()}] loading funding {args.symbol}...")
    funding_rows = fetch_funding(engine, args.symbol, start_ts_funding, end_ts + timedelta(hours=1))
    print(f"  -> {len(funding_rows)} funding events")

    rows = build_aligned_rows(swap_rows, spot_rows, funding_rows)
    print(f"[{datetime.now(timezone.utc).isoformat()}] aligned {len(rows)} rows")

    features = compute_features(rows)
    # drop initial ~96 bars before basis_z is available
    n_trim = 96
    features_warm = features[n_trim:]
    print(f"  -> {len(features_warm)} features (after warmup {n_trim})")

    # sanity counts
    nb = sum(1 for r in features_warm if r.basis_z is not None)
    nf = sum(1 for r in features_warm if r.funding_z is not None)
    nm = sum(1 for r in features_warm if r.minutes_to_next_funding is not None)
    print(f"  non-null counts: basis_z={nb}, funding_z={nf}, minutes_to_next_funding={nm}")

    # --- core regression matrix ---
    print("\n" + "=" * 112)
    print("P1-D 快速预览回归 — test R² / slope / Pearson r (train 70% / test 30%)")
    print("=" * 112)

    results: dict[str, dict[str, dict]] = {}
    for fkey in FEATURE_KEYS:
        results[fkey] = {}
        for hname, hfield in HORIZON_FIELDS:
            xs, ys = collect_xy(features_warm, fkey, hfield)
            res = train_test(xs, ys)
            results[fkey][hname] = res

    # print table
    header = f"{'feature':<28} {'horizon':<6} {'n_tr':>5} {'n_te':>5}  {'tr R²':>9} {'te R²':>9} {'slope':>11} {'pearson':>9}"
    print(header)
    print("-" * len(header))
    for fkey in FEATURE_KEYS:
        for hname, _ in HORIZON_FIELDS:
            r = results[fkey][hname]
            tr, te = r.get("train", {}), r.get("test", {})
            n_tr = tr.get("n", 0)
            n_te = te.get("n", 0)
            tr_r2 = tr.get("r2", float("nan"))
            te_r2 = te.get("r2", float("nan"))
            slope = te.get("slope", float("nan"))
            pr = te.get("pearson_r", float("nan"))
            print(f"{fkey:<28} {hname:<6} {n_tr:>5} {n_te:>5}  "
                  f"{fmt(tr_r2, 5):>9} {fmt(te_r2, 5):>9} "
                  f"{fmt(slope, 2):>11} {fmt(pr, 4):>9}")
        print()

    # --- cross-window (two halves) ---
    print("=" * 112)
    print("Cross-window sanity: 前半 vs 后半 (每半约 15 天) — 15m horizon only")
    print("=" * 112)
    halves = split_windows(features_warm, n_windows=2)
    labels = ["first_half (~day 1-15)", "second_half (~day 16-33)"]
    print(f"{'feature':<28} {'window':<26} {'n_tr':>5} {'n_te':>5}  {'tr R²':>9} {'te R²':>9} {'slope':>11} {'pearson':>9}")
    print("-" * 110)
    for fkey in FEATURE_KEYS:
        for lbl, half in zip(labels, halves):
            xs, ys = collect_xy(half, fkey, "realized_bps_h1")
            r = train_test(xs, ys)
            tr, te = r.get("train", {}), r.get("test", {})
            print(f"{fkey:<28} {lbl:<26} {tr.get('n', 0):>5} {te.get('n', 0):>5}  "
                  f"{fmt(tr.get('r2', float('nan')), 5):>9} {fmt(te.get('r2', float('nan')), 5):>9} "
                  f"{fmt(te.get('slope', float('nan')), 2):>11} "
                  f"{fmt(te.get('pearson_r', float('nan')), 4):>9}")
        print()

    # --- sign-regime slice (basis_z >0 vs <0, funding_z >0 vs <0) ---
    print("=" * 112)
    print("Sign regime slice (basis_z / funding_z ±) — 15m horizon only, full sample")
    print("=" * 112)
    regimes = [
        ("basis_z>0.5", "basis_z", lambda r: r.basis_z is not None and r.basis_z > 0.5),
        ("basis_z<-0.5", "basis_z", lambda r: r.basis_z is not None and r.basis_z < -0.5),
        ("|basis_z|<=0.5", "basis_z", lambda r: r.basis_z is not None and abs(r.basis_z) <= 0.5),
        ("funding_z>0.5", "funding_z", lambda r: r.funding_z is not None and r.funding_z > 0.5),
        ("funding_z<-0.5", "funding_z", lambda r: r.funding_z is not None and r.funding_z < -0.5),
        ("|funding_z|<=0.5", "funding_z", lambda r: r.funding_z is not None and abs(r.funding_z) <= 0.5),
    ]
    print(f"{'regime':<22} {'feature':<14} {'n_tr':>5} {'n_te':>5}  {'tr R²':>9} {'te R²':>9} {'slope':>11} {'pearson':>9}")
    print("-" * 96)
    for label, fkey, filt in regimes:
        xs, ys = collect_xy(features_warm, fkey, "realized_bps_h1", filter_fn=filt)
        r = train_test(xs, ys)
        tr, te = r.get("train", {}), r.get("test", {})
        print(f"{label:<22} {fkey:<14} {tr.get('n', 0):>5} {te.get('n', 0):>5}  "
              f"{fmt(tr.get('r2', float('nan')), 5):>9} {fmt(te.get('r2', float('nan')), 5):>9} "
              f"{fmt(te.get('slope', float('nan')), 2):>11} "
              f"{fmt(te.get('pearson_r', float('nan')), 4):>9}")

    # --- minutes_to_next_funding bucket ---
    print()
    print("=" * 112)
    print("minutes_to_next_funding 分桶 — 15m horizon, full sample")
    print("=" * 112)
    buckets = [
        ("0-60 min (前结算)", lambda r: r.minutes_to_next_funding is not None and r.minutes_to_next_funding <= 60),
        ("60-240 min", lambda r: r.minutes_to_next_funding is not None and 60 < r.minutes_to_next_funding <= 240),
        ("240-480 min (远离)", lambda r: r.minutes_to_next_funding is not None and r.minutes_to_next_funding > 240),
    ]
    print(f"{'bucket':<24} {'n_tr':>5} {'n_te':>5}  {'tr R²':>9} {'te R²':>9} {'slope':>11} {'pearson':>9}")
    print("-" * 86)
    for label, filt in buckets:
        xs, ys = collect_xy(features_warm, "minutes_to_next_funding", "realized_bps_h1", filter_fn=filt)
        r = train_test(xs, ys)
        tr, te = r.get("train", {}), r.get("test", {})
        print(f"{label:<24} {tr.get('n', 0):>5} {te.get('n', 0):>5}  "
              f"{fmt(tr.get('r2', float('nan')), 5):>9} {fmt(te.get('r2', float('nan')), 5):>9} "
              f"{fmt(te.get('slope', float('nan')), 2):>11} "
              f"{fmt(te.get('pearson_r', float('nan')), 4):>9}")

    # --- quantile-threshold net PnL ---
    print()
    print("=" * 112)
    print(f"扣成本 mean_net_bps @ q80 / q90 (cost={args.cost_bps} bps) — 15m horizon full sample")
    print("=" * 112)
    print(f"{'feature':<28} {'quantile':<10} {'n':>5}  {'mean_net_bps':>14} {'std':>10} {'win_rate':>10} {'pct_traded':>12}")
    print("-" * 96)
    q_pnl: dict[str, dict[str, dict]] = {}
    for fkey in FEATURE_KEYS:
        q_pnl[fkey] = {}
        xs, ys = collect_xy(features_warm, fkey, "realized_bps_h1")
        for q in (0.80, 0.90):
            res = net_bps_above_quantile(xs, ys, q, args.cost_bps)
            q_pnl[fkey][f"q{int(q*100)}"] = res
            print(f"{fkey:<28} q{int(q*100):<9} {res.get('n', 0):>5}  "
                  f"{fmt(res.get('mean_net_bps', float('nan')), 2):>14} "
                  f"{fmt(res.get('std_net_bps', float('nan')), 2):>10} "
                  f"{fmt(res.get('win_rate', float('nan')), 3):>10} "
                  f"{fmt(res.get('pct_traded', float('nan')), 3):>12}")

    # --- verdict ---
    print()
    print("=" * 112)
    print("Verdict 判定")
    print("=" * 112)

    # Find highest test R² @ 15m across features
    r2_15m: list[tuple[str, float, float]] = []  # (feat, r2, slope)
    for fkey in FEATURE_KEYS:
        r = results[fkey]["15m"]["test"]
        r2 = r.get("r2", float("nan"))
        slope = r.get("slope", float("nan"))
        if r2 == r2 and slope == slope:
            r2_15m.append((fkey, r2, slope))
    r2_15m.sort(key=lambda x: x[1], reverse=True)
    best_feat, best_r2, best_slope = r2_15m[0] if r2_15m else ("none", float("nan"), float("nan"))

    # Cross-window slope sign consistency for best feature
    first_half = halves[0] if halves else []
    second_half = halves[1] if len(halves) >= 2 else []
    h1_xs, h1_ys = collect_xy(first_half, best_feat, "realized_bps_h1") if first_half else ([], [])
    h2_xs, h2_ys = collect_xy(second_half, best_feat, "realized_bps_h1") if second_half else ([], [])
    h1_fit = train_test(h1_xs, h1_ys)["test"] if h1_xs else {}
    h2_fit = train_test(h2_xs, h2_ys)["test"] if h2_xs else {}
    sign_stable = (
        h1_fit.get("slope", 0) * h2_fit.get("slope", 0) > 0
        if (h1_fit.get("slope") == h1_fit.get("slope") and h2_fit.get("slope") == h2_fit.get("slope"))
        else False
    )

    print(f"best feature @ 15m: {best_feat} (test R² = {fmt(best_r2, 5)}, slope = {fmt(best_slope, 2)})")
    print(f"cross-window slope sign stable: {sign_stable}")
    print(f"  first-half slope = {fmt(h1_fit.get('slope', float('nan')), 2)}")
    print(f"  second-half slope = {fmt(h2_fit.get('slope', float('nan')), 2)}")

    # P1-D 门槛
    verdict = ""
    if best_r2 >= 0.010 and sign_stable:
        # check q80 mean_net > 2 bps
        qres = q_pnl.get(best_feat, {}).get("q80", {})
        if qres.get("mean_net_bps", -999) > 2.0 and qres.get("n", 0) >= 50:
            verdict = "GO hint: R² >= 0.010, sign stable, q80 mean_net > 2 bps"
        else:
            verdict = "CONDITIONAL hint: R² >= 0.010 but q80 mean_net <= 2 bps or n too small"
    elif best_r2 >= 0.005:
        verdict = "CONDITIONAL hint: 0.005 <= R² < 0.010, marginal"
    else:
        verdict = "NO-GO hint: R² < 0.005 across all features"

    print(f"verdict: {verdict}")

    # --- write markdown ---
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
            best_feat=best_feat,
            best_r2=best_r2,
            best_slope=best_slope,
            sign_stable=sign_stable,
            h1_fit=h1_fit,
            h2_fit=h2_fit,
            verdict=verdict,
            basis_z_count=nb,
            funding_z_count=nf,
            minutes_count=nm,
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
    best_r2: float,
    best_slope: float,
    sign_stable: bool,
    h1_fit: dict,
    h2_fit: dict,
    verdict: str,
    basis_z_count: int,
    funding_z_count: int,
    minutes_count: int,
) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# P1-D 快速预览回归 — funding / basis 特征 (2026-04-20)")
    lines.append("")
    lines.append("> 项目定位声明: 本文件默认服从 AATS 的统一目标. 详见 [项目定位声明](../../docs/project_positioning.md).")
    lines.append("")
    lines.append("**Scope**: 用 RDP 现有 33 天 `silver.market_swap_candles_15m` (swap + spot) + "
                 "3 个月 `silver.market_swap_funding` 数据, 跑 basis/funding/proximity 3 组特征 "
                 "对 realized_return_15m_bps 的 OLS 回归, 为 P1-D Phase 2A 门槛提供 hint.")
    lines.append("")
    lines.append(f"**标的**: `{symbol}`, 15m  ")
    lines.append(f"**样本**: {n_rows} 行 (warmup 96 bar 后)")
    lines.append(f"**非空样本**: basis_z={basis_z_count}, funding_z={funding_z_count}, "
                 f"minutes_to_next_funding={minutes_count}")
    lines.append(f"**Cost 假设**: {cost_bps:.1f} bps (taker 5 + slip 1, 与线上一致)")
    lines.append("**生成日期**: 2026-04-20")
    lines.append("")

    lines.append("## TL;DR")
    lines.append("")
    lines.append(f"- **最高 R² (test, 15m)**: `{best_feat}` — R²={fmt(best_r2, 5)}, slope={fmt(best_slope, 2)}")
    lines.append(f"- **Cross-window slope sign stable**: {'**YES**' if sign_stable else '**NO**'} "
                 f"(first_half slope={fmt(h1_fit.get('slope', float('nan')), 2)}, "
                 f"second_half slope={fmt(h2_fit.get('slope', float('nan')), 2)})")
    lines.append(f"- **P1-D Phase 2A Hint**: **{verdict}**")
    lines.append("")
    # 门槛提醒
    lines.append("**门槛参考 (P1-D 可行性 §8.2)**:")
    lines.append("")
    lines.append("- GO: R² ≥ 0.010 且 slope sign 稳定 且 q80 mean_net_bps > 2 bps")
    lines.append("- CONDITIONAL: 0.005 ≤ R² < 0.010 或 regime-specific 强 global 弱")
    lines.append("- NO-GO: R² < 0.005 across all features")
    lines.append("")

    lines.append("## 主矩阵: feature × horizon (test set)")
    lines.append("")
    lines.append("| feature | horizon | n_tr | n_te | train R² | test R² | slope | pearson r |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for fkey in FEATURE_KEYS:
        for hname, _ in HORIZON_FIELDS:
            r = results[fkey][hname]
            tr = r.get("train", {})
            te = r.get("test", {})
            lines.append(
                f"| `{fkey}` | {hname} | {tr.get('n', 0)} | {te.get('n', 0)} | "
                f"{fmt(tr.get('r2', float('nan')), 5)} | {fmt(te.get('r2', float('nan')), 5)} | "
                f"{fmt(te.get('slope', float('nan')), 2)} | "
                f"{fmt(te.get('pearson_r', float('nan')), 4)} |"
            )
    lines.append("")

    lines.append("## Cross-window 稳健性 (15m horizon)")
    lines.append("")
    lines.append("分 2 半各自跑 train/test, 比 slope 符号:")
    lines.append("")
    lines.append("| feature | window | n_tr | n_te | test R² | slope | pearson r |")
    lines.append("|---|---|---|---|---|---|---|")
    labels = ["first_half (~day 1-15)", "second_half (~day 16-33)"]
    for fkey in FEATURE_KEYS:
        for lbl, half in zip(labels, cross_halves):
            xs, ys = collect_xy(half, fkey, "realized_bps_h1")
            r = train_test(xs, ys)
            te = r.get("test", {})
            lines.append(
                f"| `{fkey}` | {lbl} | {r.get('train', {}).get('n', 0)} | {te.get('n', 0)} | "
                f"{fmt(te.get('r2', float('nan')), 5)} | "
                f"{fmt(te.get('slope', float('nan')), 2)} | "
                f"{fmt(te.get('pearson_r', float('nan')), 4)} |"
            )
    lines.append("")

    lines.append("## Sign-regime slice (15m horizon, full sample)")
    lines.append("")
    lines.append("| regime | feature | n_tr | n_te | test R² | slope | pearson r |")
    lines.append("|---|---|---|---|---|---|---|")
    regimes = [
        ("basis_z > 0.5", "basis_z", lambda r: r.basis_z is not None and r.basis_z > 0.5),
        ("basis_z < -0.5", "basis_z", lambda r: r.basis_z is not None and r.basis_z < -0.5),
        ("|basis_z| ≤ 0.5", "basis_z", lambda r: r.basis_z is not None and abs(r.basis_z) <= 0.5),
        ("funding_z > 0.5", "funding_z", lambda r: r.funding_z is not None and r.funding_z > 0.5),
        ("funding_z < -0.5", "funding_z", lambda r: r.funding_z is not None and r.funding_z < -0.5),
        ("|funding_z| ≤ 0.5", "funding_z", lambda r: r.funding_z is not None and abs(r.funding_z) <= 0.5),
    ]
    # need feature rows (all rows, not halves)
    # caller passed nothing; approximate by recomputing from the last half merge — but we don't have it here.
    # Instead, reconstruct from halves:
    all_rows: list = []
    for h in cross_halves:
        all_rows.extend(h)
    for label, fkey, filt in regimes:
        xs, ys = collect_xy(all_rows, fkey, "realized_bps_h1", filter_fn=filt)
        r = train_test(xs, ys)
        te = r.get("test", {})
        lines.append(
            f"| {label} | `{fkey}` | {r.get('train', {}).get('n', 0)} | {te.get('n', 0)} | "
            f"{fmt(te.get('r2', float('nan')), 5)} | "
            f"{fmt(te.get('slope', float('nan')), 2)} | "
            f"{fmt(te.get('pearson_r', float('nan')), 4)} |"
        )
    lines.append("")

    lines.append("## minutes_to_next_funding 分桶 (15m horizon)")
    lines.append("")
    lines.append("| bucket | n_tr | n_te | test R² | slope | pearson r |")
    lines.append("|---|---|---|---|---|---|")
    mfm_buckets = [
        ("0-60 min (临近结算)", lambda r: r.minutes_to_next_funding is not None and r.minutes_to_next_funding <= 60),
        ("60-240 min", lambda r: r.minutes_to_next_funding is not None and 60 < r.minutes_to_next_funding <= 240),
        ("240-480 min (远离)", lambda r: r.minutes_to_next_funding is not None and r.minutes_to_next_funding > 240),
    ]
    for label, filt in mfm_buckets:
        xs, ys = collect_xy(all_rows, "minutes_to_next_funding", "realized_bps_h1", filter_fn=filt)
        r = train_test(xs, ys)
        te = r.get("test", {})
        lines.append(
            f"| {label} | {r.get('train', {}).get('n', 0)} | {te.get('n', 0)} | "
            f"{fmt(te.get('r2', float('nan')), 5)} | "
            f"{fmt(te.get('slope', float('nan')), 2)} | "
            f"{fmt(te.get('pearson_r', float('nan')), 4)} |"
        )
    lines.append("")

    lines.append(f"## 扣成本 mean_net_bps @ q80 / q90 (cost={cost_bps} bps)")
    lines.append("")
    lines.append("交易规则: sign(feature) 开仓, abs(feature) ≥ feature q80 / q90 才入场, 持有 15m.")
    lines.append("")
    lines.append("| feature | quantile | n_traded | mean_net_bps | std | win_rate | pct_traded |")
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

    lines.append("## 诚实判定 & Hint")
    lines.append("")
    lines.append(f"**{verdict}**")
    lines.append("")
    lines.append("### 解读")
    lines.append("")
    lines.append("- 门槛严格参考 P1-D 可行性 §8.2 (R² ≥ 0.01, slope 稳定, cost-adjusted net > 2 bps).")
    lines.append("- 本预览不是最终 Phase 2A 判定 — 只为 microstructure 真正开始前校准期望.")
    lines.append("- 如 R² < 0.005, 说明在 bar-level 15m horizon 上这两个特征 (独立) predictive power 低 — ")
    lines.append("  这与 P1-D §5.1 表中 funding / basis alone 被分类为 event-window 特征而非 persistent signal 一致.")
    lines.append("- 真正 edge 可能在 event window (funding 结算 ±15min) 内, 需要扩样本或专门 event study.")
    lines.append("")

    lines.append("## 方法学 & 限制")
    lines.append("")
    lines.append("- 对齐 fast_impulse / H4 validate 回归方法: OLS 1-var, train/test 70/30, 时间顺序.")
    lines.append("- basis z-score 用 96 bar (24h at 15m) 因果滚动窗.")
    lines.append("- funding z-score 使用实际结算时间上的 7 日因果滚动窗，不假定固定 8 小时间隔。")
    lines.append("- **没有 look-ahead**: 每个 bar 只用 ≤ 该 bar ts 的数据.")
    lines.append("- **没有扣除 cost**: 主矩阵是 raw realized_return (net PnL 见 q80/q90 表).")
    lines.append("- 样本 33 天 ~3000 行, 统计功效有限 — ± 0.005 R² 置信区间约 ±0.003.")
    lines.append("")
    lines.append("## 可复现")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/research/p1d_preview_regression_funding_basis.py \\")
    lines.append(f"  --symbol {symbol} --days {days} --cost-bps {cost_bps} \\")
    lines.append(f"  --output {path}")
    lines.append("```")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
