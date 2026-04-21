"""Baseline composite_alpha_score 等分位标定脚本.

背景
----
P0 → P2.7 的权重重分配把 composite_alpha_score 从 5 个 alpha 分量的线性组合
变成 9 个分量 (新增 basis/funding/oi/ls)。单分量权重下压 → composite 分布整体
收窄；另外 momentum 从 `(close-open)/open` 改成 ROC(5)、volatility 从振幅改成
ATR(14)，单 alpha 的分布形状也变了. 下游多处硬编码 `|composite| >= T_old`
阈值原本按旧分布经验标定，直接套用会让 direction_bias=flat 比例激增.

本脚本做 **等分位标定** (quantile-matching): 对每个 T_old，找到它在旧分布
里对应的分位 q，再在新分布里取同分位 q 的值作为 T_new——保持相同的触发率.

场景 A = 当前代码 (P2.7 权重) 直接跑真实历史 K 线
场景 B = 同一批 momentum/trend/regime/mtf/micro × 旧权重 (0.34/0.22/0.17/0.12/0.15),
        不含 basis/funding/oi/ls (旧版本根本没这四个 alpha).

简化假设 (见任务文档 §二):
  - basis_alpha = 0 (mark_price = last_price, 回放无历史 mark-price)
  - oi_alpha = 0 (缺 open-interest 历史)
  - ls_alpha = 0 (缺 long-short ratio 历史, 默认 flag 关)
  - funding_alpha 使用真实 RDP funding 数据
  - momentum/volatility 用新 ROC/ATR 路径，不 revert 到 P0 前瞬时算法

用法
----
    cd <project_root>
    python scripts/calibration/baseline_composite_alpha_distribution.py \\
        --days 30 --symbol BTC-USDT-SWAP \\
        --output docs/calibration/baseline_weight_recalibration_2026_04_19.md

环境变量 (从 .env.wsl2 / .env.derivatives.live 自动加载):
    POSTGRES_USER, POSTGRES_PASSWORD (OR RDP_DATABASE_URL / AATS_ACTIVE_PARAMETER_DB_URL)

退出码: 0 = 正常，1 = 参数错误，2 = 数据/DB 错误
"""

from __future__ import annotations

import argparse
import bisect
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

# 加载 env. 搜索顺序: 项目 ROOT (当前 worktree) → $HOME/aats (WSL2 native checkout).
# Windows worktree 场景下 .env.* 不一定在 ROOT 下 (用户把凭证只存在 native
# checkout 侧), 所以第二个搜索路径是必要的.
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

from aats.schemas.market import KlineBar, MarketSnapshot  # noqa: E402
from aats.services.feature_engine.calculator import FeatureCalculator  # noqa: E402

# 旧权重 (P0 前，5 个分量). 场景 B 计算使用.
OLD_WEIGHTS = {
    "momentum": 0.34,
    "trend": 0.22,
    "regime": 0.17,
    "multi_tf": 0.12,
    "micro": 0.15,
}

# 新权重 (P2.7，9 个分量). 仅供报告引用，实际由 FeatureCalculator 内部应用.
NEW_WEIGHTS = {
    "momentum": 0.24,
    "trend": 0.17,
    "regime": 0.12,
    "multi_tf": 0.08,
    "micro": 0.09,
    "basis": 0.10,
    "funding": 0.07,
    "oi": 0.07,
    "ls": 0.06,
}

# 要标定的阈值 T_old. 来源 = 任务文档 §三. 同一数值多处使用时只列一次；
# 报告里会按使用点列表展开.
THRESHOLDS_TO_CALIBRATE: list[tuple[str, float, str]] = [
    ("baseline_breakout", 0.08, "derivatives_live.yaml:209 strategy_baseline_breakout_alpha_threshold"),
    ("baseline_trend", 0.14, "derivatives_live.yaml:210 strategy_baseline_trend_alpha_threshold"),
    ("baseline_range", 0.16, "derivatives_live.yaml:211 strategy_baseline_range_alpha_threshold"),
    ("baseline_uncertain", 0.26, "derivatives_live.yaml:212 strategy_baseline_uncertain_alpha_threshold"),
    ("alpha_decay_reduce", 0.12, "settings.py:536 strategy_position_alpha_decay_reduce_alpha"),
    ("alpha_decay_exit", 0.06, "settings.py:538 strategy_position_alpha_decay_exit_alpha"),
    ("profile_high_vol_ceiling", 0.45, "strategy_profiles.py:899 high_volatility_defensive 触发 |composite| < 0.45"),
    ("profile_defensive", 0.14, "strategy_profiles.py:906 range_defensive 触发 |composite| < 0.14"),
    ("profile_aggressive", 0.55, "strategy_profiles.py:913 trend_aggressive 触发 composite >= 0.55"),
    ("profile_normal", 0.24, "strategy_profiles.py:920 trend_normal 触发 composite >= 0.24"),
    ("intent_fit_band_low", 0.12, "strategy_profiles.py:1834 trend_strict 区间下限"),
    ("intent_fit_band_high", 0.22, "strategy_profiles.py:1834/1836 trend_strict 上限 / trend_normal 下限"),
]

REPORTED_PERCENTILES = [5, 10, 25, 50, 60, 65, 70, 75, 80, 85, 90, 95, 99]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def resolve_db_url() -> str:
    """优先用 RDP_DATABASE_URL / AATS_ACTIVE_PARAMETER_DB_URL, 否则按 POSTGRES_* 组装.

    凭证仅在进程内使用, 不打印.
    """
    for key in ("AATS_ACTIVE_PARAMETER_DB_URL", "RDP_DATABASE_URL"):
        val = os.environ.get(key)
        if val:
            return val
    user = os.environ.get("POSTGRES_USER", "aats")
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


def load_candles(engine, table: str, symbol: str, start_ts: datetime, end_ts: datetime):
    sql = text(
        f"SELECT ts, open, high, low, close, COALESCE(vol, 0) AS volume "
        f"FROM {table} WHERE symbol = :s AND ts >= :start AND ts <= :end_ts ORDER BY ts"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": symbol, "start": start_ts, "end_ts": end_ts}).fetchall()


def load_funding(engine, symbol: str, start_ts: datetime, end_ts: datetime):
    sql = text(
        "SELECT ts, funding_rate FROM silver.market_swap_funding "
        "WHERE symbol = :s AND ts >= :start AND ts <= :end_ts ORDER BY ts"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": symbol, "start": start_ts, "end_ts": end_ts}).fetchall()


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def _ensure_tz(ts: datetime) -> datetime:
    """DB 返回的 timestamptz 通常带 tz; 若缺失则补 UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _kline_bar(row, fallback_ts: datetime) -> KlineBar:
    ts = _ensure_tz(row.ts) if row.ts is not None else fallback_ts
    return KlineBar(
        open=Decimal(str(row.open)),
        high=Decimal(str(row.high)),
        low=Decimal(str(row.low)),
        close=Decimal(str(row.close)),
        volume=Decimal(str(row.volume or 0)),
        ts=ts,
    )


def _build_snapshot(
    symbol: str,
    snapshot_ts: datetime,
    bar_15m: KlineBar,
    bar_1h: KlineBar,
    funding_rate: Decimal | None,
) -> MarketSnapshot:
    last = bar_15m.close
    # 中性 ticker / 中性 orderbook. 让 LiquidityAnalyzer 得到
    # 一个稳定 liquidity_score —— 回放没 real tick, 我们只关心 composite 的
    # 相对形状.
    spread_bp = Decimal("0.0001")  # 1bp 买卖差
    bid = last * (Decimal("1") - spread_bp)
    ask = last * (Decimal("1") + spread_bp)
    depth_entries = {
        "bids": [[str(bid), "1.0"], [str(bid * Decimal("0.999")), "1.0"]],
        "asks": [[str(ask), "1.0"], [str(ask * Decimal("1.001")), "1.0"]],
    }
    return MarketSnapshot(
        symbol=symbol,
        exchange="OKX",
        snapshot_ts=_ensure_tz(snapshot_ts),
        best_bid=bid,
        best_ask=ask,
        last_price=last,
        bid_size=Decimal("1.0"),
        ask_size=Decimal("1.0"),
        volume_24h=bar_15m.volume,
        kline_15m=bar_15m,
        kline_1h=bar_1h,
        recent_trades=[],
        orderbook_depth=depth_entries,
        mark_price=last,  # basis_alpha = 0
        funding_rate=funding_rate,  # 真实 funding 或 None
        open_interest=None,  # oi_alpha = 0
    )


# ---------------------------------------------------------------------------
# Funding helper: 对每个 bar 用最新一条 funding_rate (bisect).
# ---------------------------------------------------------------------------

@dataclass
class FundingTimeline:
    """按时间戳有序的 funding_rate 序列, 支持 "as of ts" 查询."""

    timestamps: list[datetime]
    rates: list[Decimal]

    def latest_as_of(self, ts: datetime) -> Decimal | None:
        if not self.timestamps:
            return None
        idx = bisect.bisect_right(self.timestamps, ts) - 1
        if idx < 0:
            return None
        return self.rates[idx]


def build_funding_timeline(rows) -> FundingTimeline:
    timestamps = [_ensure_tz(r.ts) for r in rows]
    rates = [Decimal(str(r.funding_rate)) for r in rows]
    return FundingTimeline(timestamps=timestamps, rates=rates)


# ---------------------------------------------------------------------------
# Quantile utilities
# ---------------------------------------------------------------------------

def percentile(sorted_vals: list[float], q: float) -> float:
    """q ∈ [0,1] → linear-interpolated percentile."""
    if not sorted_vals:
        return float("nan")
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    pos = q * (len(sorted_vals) - 1)
    lo_i = int(pos)
    hi_i = min(lo_i + 1, len(sorted_vals) - 1)
    frac = pos - lo_i
    return sorted_vals[lo_i] * (1 - frac) + sorted_vals[hi_i] * frac


def quantile_of(sorted_vals: list[float], target: float) -> float:
    """反查: target 在 sorted_vals 中的分位 q ∈ [0,1]."""
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    if target <= sorted_vals[0]:
        return 0.0
    if target >= sorted_vals[-1]:
        return 1.0
    idx = bisect.bisect_left(sorted_vals, target)
    lo, hi = sorted_vals[idx - 1], sorted_vals[idx]
    if hi == lo:
        return (idx - 1) / (n - 1)
    frac = (target - lo) / (hi - lo)
    return (idx - 1 + frac) / (n - 1)


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------

@dataclass
class BarFeatures:
    ts: datetime
    composite_new: float
    composite_old: float
    momentum: float
    trend: float
    regime: float
    multi_tf: float
    micro: float
    basis: float
    funding: float
    oi: float
    ls: float
    liquidity_scale: float


def align_1h_bars(bars_1h: list[KlineBar]) -> dict[datetime, KlineBar]:
    """把 1h bar 按 UTC-hour 索引, 后续 15m bar 取所属 hour."""
    mapping: dict[datetime, KlineBar] = {}
    for bar in bars_1h:
        if bar.ts is None:
            continue
        hour = bar.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        mapping[hour] = bar
    return mapping


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def replay(
    symbol: str,
    bars_15m: list[KlineBar],
    bars_1h_index: dict[datetime, KlineBar],
    funding_tl: FundingTimeline,
    warmup_bars: int,
    verbose: bool = False,
) -> list[BarFeatures]:
    calc = FeatureCalculator()
    calc.register_rolling_state(symbol=symbol, timeframe="15m")
    calc.register_rolling_state(symbol=symbol, timeframe="1h")

    # 预热: 前 warmup_bars 根 15m, 以及对应 1h 的所有早期 bars.
    # 简化起见, 直接跑 calculate() 但不收集 — 内部 rolling state 自然累积.
    features: list[BarFeatures] = []
    skipped_missing_1h = 0
    skipped_exceptions = 0

    for i, bar15 in enumerate(bars_15m):
        ts = bar15.ts
        if ts is None:
            continue
        ts_utc = ts.astimezone(timezone.utc)
        hour_key = ts_utc.replace(minute=0, second=0, microsecond=0)
        bar1h = bars_1h_index.get(hour_key)
        if bar1h is None:
            # 找最近的较早 1h bar (minute boundary 不一定对齐 — 取不晚于 ts 的最新 1h)
            past_keys = [k for k in bars_1h_index.keys() if k <= hour_key]
            if not past_keys:
                skipped_missing_1h += 1
                continue
            bar1h = bars_1h_index[max(past_keys)]

        funding = funding_tl.latest_as_of(ts)
        snap = _build_snapshot(symbol, ts, bar15, bar1h, funding)
        try:
            snap_feat = calc.calculate(snap)
        except Exception as exc:  # pragma: no cover — 数据异常保护
            skipped_exceptions += 1
            if verbose:
                print(f"  calc error @ {ts}: {exc}", file=sys.stderr)
            continue

        if i < warmup_bars:
            continue  # 不收集 prewarm 期的样本 (ROC/ATR 未 ready)

        af = snap_feat.analysis_context.alpha_factors
        ls_val = float(getattr(af, "ls_alpha", 0.0) or 0.0)

        # 场景 B: 用同一批 5 个 alpha, 套旧权重
        old_raw = (
            af.momentum_alpha * OLD_WEIGHTS["momentum"]
            + af.trend_alpha * OLD_WEIGHTS["trend"]
            + af.regime_alpha * OLD_WEIGHTS["regime"]
            + af.multi_timeframe_alpha * OLD_WEIGHTS["multi_tf"]
            + af.microstructure_alpha * OLD_WEIGHTS["micro"]
        ) * af.liquidity_scale
        composite_old = _clamp(float(old_raw), -1.0, 1.0)

        features.append(BarFeatures(
            ts=ts_utc,
            composite_new=float(snap_feat.composite_alpha_score),
            composite_old=composite_old,
            momentum=float(af.momentum_alpha),
            trend=float(af.trend_alpha),
            regime=float(af.regime_alpha),
            multi_tf=float(af.multi_timeframe_alpha),
            micro=float(af.microstructure_alpha),
            basis=float(af.basis_alpha),
            funding=float(af.funding_alpha),
            oi=float(af.oi_alpha),
            ls=ls_val,
            liquidity_scale=float(af.liquidity_scale),
        ))

    if skipped_missing_1h or skipped_exceptions:
        print(
            f"replay warnings: skipped_missing_1h={skipped_missing_1h} "
            f"skipped_exceptions={skipped_exceptions}",
            file=sys.stderr,
        )
    return features


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_markdown_report(
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    sample_count: int,
    warmup_bars: int,
    features: list[BarFeatures],
    abs_new_sorted: list[float],
    abs_old_sorted: list[float],
    mappings: list[dict],
    today: datetime,
) -> str:
    def fmt(x: float) -> str:
        return f"{x:.4f}" if x == x else "nan"  # NaN-safe

    def dist_row(label: str, new_val: float, old_val: float) -> str:
        if old_val == 0:
            ratio = "∞"
        else:
            ratio = f"{new_val / old_val:.2f}"
        return f"| {label} | {fmt(old_val)} | {fmt(new_val)} | {ratio} |"

    lines: list[str] = []
    lines.append("# Baseline composite_alpha_score 权重重分配后的等分位标定报告")
    lines.append("")
    lines.append(f"生成日期: {today.strftime('%Y-%m-%d')}  ")
    lines.append(f"标的: `{symbol}`  ")
    lines.append(
        f"采样期间: `{window_start.strftime('%Y-%m-%d %H:%M UTC')}` ~ "
        f"`{window_end.strftime('%Y-%m-%d %H:%M UTC')}`  "
    )
    lines.append(
        f"有效样本数: `{sample_count}` 个 15m bar (前 `{warmup_bars}` 根 prewarm, "
        f"ROC(5)/ATR(14) ready 后开始采集)"
    )
    lines.append("")
    lines.append("## 权重对比")
    lines.append("")
    lines.append("| Alpha 分量 | 旧权重 (P0 前) | 新权重 (P2.7 后) |")
    lines.append("|---|---|---|")
    all_keys = ["momentum", "trend", "regime", "multi_tf", "micro", "basis", "funding", "oi", "ls"]
    for k in all_keys:
        lines.append(f"| {k} | {OLD_WEIGHTS.get(k, '—')} | {NEW_WEIGHTS.get(k, '—')} |")
    lines.append("")

    lines.append("## `|composite_alpha|` 分布摘要")
    lines.append("")
    lines.append("| 分位 | 旧公式 | 新公式 | 比值 (新/旧) |")
    lines.append("|---|---|---|---|")
    if abs_new_sorted and abs_old_sorted:
        lines.append(dist_row("min", abs_new_sorted[0], abs_old_sorted[0]))
        for p in REPORTED_PERCENTILES:
            q = p / 100.0
            lines.append(dist_row(f"P{p}", percentile(abs_new_sorted, q), percentile(abs_old_sorted, q)))
        lines.append(dist_row("max", abs_new_sorted[-1], abs_old_sorted[-1]))
    lines.append("")

    lines.append("## T_old → T_new 映射 (等分位)")
    lines.append("")
    lines.append("查阅方式: `T_old` 在旧分布里对应分位 `q`, 新分布里同 `q` 分位给出 `T_new`。")
    lines.append("")
    lines.append("| Name | T_old | 旧分位 q | T_new | 使用位置 |")
    lines.append("|---|---|---|---|---|")
    for m in mappings:
        lines.append(
            f"| `{m['name']}` | {fmt(m['t_old'])} | "
            f"{m['q_old'] * 100:.1f}% | {fmt(m['t_new'])} | {m['usage']} |"
        )
    lines.append("")

    lines.append("## 建议改动")
    lines.append("")
    lines.append("### `configs/strategy_profiles/derivatives_live.yaml`")
    lines.append("")
    lines.append("```yaml")
    yaml_map = {
        "baseline_breakout": "strategy_baseline_breakout_alpha_threshold",
        "baseline_trend": "strategy_baseline_trend_alpha_threshold",
        "baseline_range": "strategy_baseline_range_alpha_threshold",
        "baseline_uncertain": "strategy_baseline_uncertain_alpha_threshold",
    }
    for m in mappings:
        if m["name"] in yaml_map:
            lines.append(f"{yaml_map[m['name']]}: {fmt(m['t_new'])}  # 原 {fmt(m['t_old'])}")
    lines.append("```")
    lines.append("")
    lines.append("### `aats/bootstrap/settings.py` (alpha_decay defaults)")
    lines.append("")
    lines.append("```python")
    settings_map = {
        "alpha_decay_reduce": "strategy_position_alpha_decay_reduce_alpha",
        "alpha_decay_exit": "strategy_position_alpha_decay_exit_alpha",
    }
    for m in mappings:
        if m["name"] in settings_map:
            lines.append(f"{settings_map[m['name']]}: float = {fmt(m['t_new'])}  # 原 {fmt(m['t_old'])}")
    lines.append("```")
    lines.append("")
    lines.append("### `aats/bootstrap/settings.py` (新增 profile auto-switch 字段, default = 标定值)")
    lines.append("")
    lines.append("```python")
    new_field_map = [
        ("profile_high_vol_ceiling", "strategy_profile_auto_switch_high_vol_alpha_ceiling"),
        ("profile_defensive", "strategy_profile_auto_switch_alpha_defensive_threshold"),
        ("profile_aggressive", "strategy_profile_auto_switch_alpha_aggressive_threshold"),
        ("profile_normal", "strategy_profile_auto_switch_alpha_normal_threshold"),
        ("intent_fit_band_low", "strategy_profile_intent_fit_alpha_band_low"),
        ("intent_fit_band_high", "strategy_profile_intent_fit_alpha_band_high"),
    ]
    mapping_by_name = {m["name"]: m for m in mappings}
    for name, field in new_field_map:
        m = mapping_by_name[name]
        lines.append(f"{field}: float = {fmt(m['t_new'])}  # 原硬编码 {fmt(m['t_old'])}")
    lines.append("```")
    lines.append("")

    lines.append("## 假设与风险")
    lines.append("")
    lines.append("- 回放中 `basis_alpha = 0` (mark_price = last_price, RDP 暂无 mark-price 历史).")
    lines.append("- 回放中 `oi_alpha = 0` (RDP 暂无 open-interest 历史).")
    lines.append("- 回放中 `ls_alpha = 0` (long-short ratio 依赖 poller, 默认 flag 关).")
    lines.append("- `funding_alpha` 使用真实 RDP funding 数据, 每 bar 取最近一条 `silver.market_swap_funding`.")
    lines.append("- 场景 B (旧权重) 使用新 ROC(5)/ATR(14) 路径得到的 momentum/trend/regime/multi_tf/micro, 非 P0 前瞬时路径 (任务文档 §二 接受此简化).")
    lines.append("- 因此新分布相对真实生产分布偏窄 (缺 basis/oi 贡献) → 标定结果偏紧, 属保守方向.")
    lines.append("")
    lines.append("## 采样元数据")
    lines.append("")
    lines.append(f"- liquidity_scale 样本 p10={percentile(sorted(f.liquidity_scale for f in features), 0.10):.3f} "
                 f"p50={percentile(sorted(f.liquidity_scale for f in features), 0.50):.3f} "
                 f"p90={percentile(sorted(f.liquidity_scale for f in features), 0.90):.3f}")
    mean_f = sum(f.funding for f in features) / max(len(features), 1)
    lines.append(f"- funding_alpha 均值 {mean_f:.4f} (真实贡献, 其余四个 optional alpha 恒 0)")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="最近多少天 (default 30)")
    parser.add_argument("--symbol", type=str, default="BTC-USDT-SWAP", help="标的")
    parser.add_argument("--warmup-bars", type=int, default=30, help="prewarm 丢弃多少根 (default 30)")
    parser.add_argument("--output", type=str, required=True, help="markdown 报告输出路径")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 1

    db_url = resolve_db_url()
    engine = create_engine(db_url)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=args.days)

    symbol = args.symbol
    print(f"[calibration] symbol={symbol} window={start.isoformat()} ~ {now.isoformat()}")

    try:
        rows_15m = load_candles(engine, "silver.market_swap_candles_15m", symbol, start, now)
        rows_1h = load_candles(engine, "silver.market_swap_candles_1h", symbol, start - timedelta(days=2), now)
        rows_fund = load_funding(engine, symbol, start - timedelta(days=2), now)
    except Exception as exc:
        print(f"DB query failed: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    if not rows_15m:
        print("no 15m data loaded — fallback to 7-day window", file=sys.stderr)
        return 2

    bars_15m = [_kline_bar(r, r.ts) for r in rows_15m]
    bars_1h = [_kline_bar(r, r.ts) for r in rows_1h]
    bars_1h_idx = align_1h_bars(bars_1h)
    funding_tl = build_funding_timeline(rows_fund)

    print(f"[calibration] loaded 15m={len(bars_15m)} 1h={len(bars_1h)} funding={len(rows_fund)}")

    features = replay(
        symbol=symbol,
        bars_15m=bars_15m,
        bars_1h_index=bars_1h_idx,
        funding_tl=funding_tl,
        warmup_bars=args.warmup_bars,
        verbose=args.verbose,
    )
    if len(features) < 500:
        print(f"insufficient samples after prewarm: {len(features)}", file=sys.stderr)
        return 2

    abs_new = sorted(abs(f.composite_new) for f in features)
    abs_old = sorted(abs(f.composite_old) for f in features)

    print(f"[calibration] usable samples={len(features)}")
    print(f"[calibration] |composite_new|: min={abs_new[0]:.4f} P50={percentile(abs_new, 0.5):.4f} P90={percentile(abs_new, 0.9):.4f} max={abs_new[-1]:.4f}")
    print(f"[calibration] |composite_old|: min={abs_old[0]:.4f} P50={percentile(abs_old, 0.5):.4f} P90={percentile(abs_old, 0.9):.4f} max={abs_old[-1]:.4f}")

    mappings: list[dict] = []
    for name, t_old, usage in THRESHOLDS_TO_CALIBRATE:
        q_old = quantile_of(abs_old, t_old)
        t_new = percentile(abs_new, q_old)
        mappings.append({"name": name, "t_old": t_old, "q_old": q_old, "t_new": t_new, "usage": usage})
        print(f"  {name:<25s} T_old={t_old:.4f} q={q_old*100:5.1f}% → T_new={t_new:.4f}  ({usage})")

    today = datetime.now(timezone.utc)
    md = render_markdown_report(
        symbol=symbol,
        window_start=bars_15m[0].ts or start,
        window_end=bars_15m[-1].ts or now,
        sample_count=len(features),
        warmup_bars=args.warmup_bars,
        features=features,
        abs_new_sorted=abs_new,
        abs_old_sorted=abs_old,
        mappings=mappings,
        today=today,
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md, encoding="utf-8")
    print(f"[calibration] report written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
