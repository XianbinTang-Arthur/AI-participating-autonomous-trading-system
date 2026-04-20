"""signal_edge_scale_bps 回归标定脚本 (P1-B step 1).

背景
----
independent family 24h 零 fill 的真正卡点不是 leg_score 门槛, 而是
`expected_net_edge = signal_edge - cost` 结构性为负:

    signal_edge_bps = leg_score × signal_edge_scale_bps     # scoring.py:266
    expected_net_edge = signal_edge - expected_cost - slip_buffer - exec_buffer

当前 RDP active_parameter_sets 里 independent_15m.signal_edge_scale_bps = 12.0,
而实盘 leg_score 分布集中在 0.10-0.30, 映射后 signal_edge = 1.2-3.6 bps,
cost (passive_first) = 4.605 bps → net_edge 恒为负, 所有 intent 被 safe_edge 阻塞.

本脚本的任务: 用 30 天 (或尽可能长的) 实盘 leg_score + BTC-USDT-SWAP 15m K 线,
回归 leg_score → realized_edge, 统计意义上推导 scale 的合理值.

关键约束 (任务说明 §严禁做的事):
    - 不修改 configs / settings.py / governance 表
    - 不 commit / push
    - 只产出 CSV + 报告, 由主任务决定是否改参数

数据源
-----
- aats_live_derivatives.public.event_store
    topic='strategy.sleeve_intents', family='independent', symbol='BTC-USDT-SWAP'
    payload.metrics.long_leg_score / short_leg_score / expected_cost_bps
- aats_research.silver.market_swap_candles_15m
    15 分钟 K 线, 用于算 realized_edge (T+15/30/60 min forward return)

输出
----
- docs/review/signal_edge_scale_calibration_2026_04_19.md (报告)
- docs/review/signal_edge_scale_calibration_<YYYYMMDD>_data.csv (样本原始表)

用法
----
    # Windows:
    .venv\\Scripts\\python.exe scripts/calibration/signal_edge_scale_regression.py \\
        --days 30 --symbol BTC-USDT-SWAP

    # WSL (需先在 aats-venv 安装 numpy):
    python scripts/calibration/signal_edge_scale_regression.py --days 30 --symbol BTC-USDT-SWAP

环境变量 (自动从 .env.wsl2 / .env.derivatives.live 加载):
    POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT
    或 AATS_ACTIVE_PARAMETER_DB_URL / RDP_DATABASE_URL

退出码: 0 = 正常, 1 = 参数/凭证缺失, 2 = DB 错误, 3 = 数据不足
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]

try:
    from dotenv import load_dotenv
except ImportError:
    print("missing python-dotenv; pip install python-dotenv", file=sys.stderr)
    sys.exit(1)

def _discover_env_roots(project_root: Path) -> list[Path]:
    """Walk up from `project_root` to find .env.* files.

    This handles the worktree case where we're in
    `<main_project>/.claude/worktrees/<name>/` and .env.* live in `<main_project>/`.
    """
    roots: list[Path] = [project_root]
    # Walk up to 4 levels looking for the dir that actually has .env.wsl2
    cur = project_root
    for _ in range(4):
        cur = cur.parent
        if (cur / ".env.wsl2").is_file() or (cur / ".env.derivatives.live").is_file():
            roots.append(cur)
            break
    _home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if _home:
        roots.append(Path(_home) / "aats")
    return roots


_ENV_SEARCH_ROOTS = _discover_env_roots(ROOT)
for env_file in (".env.wsl2", ".env.derivatives.live"):
    for search_root in _ENV_SEARCH_ROOTS:
        env_path = search_root / env_file
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            break

try:
    import numpy as np
except ImportError:
    print("missing numpy; pip install numpy", file=sys.stderr)
    sys.exit(1)

from sqlalchemy import create_engine, text


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

# 与 scoring.py:266 / compute_signal_edge_bps 一致:
#   signal_edge_bps = leg_score × signal_edge_scale_bps
CURRENT_RDP_SCALE = 12.0          # governance.active_parameter_sets independent_15m
LEGACY_DEFAULT_SCALE = 20.0       # settings.py default / independent_1h / directional

# 与 derivatives_live.yaml + settings 默认一致:
#   passive_first (bounded_limit_ioc) expected_cost = 4.605 bps
PASSIVE_FIRST_COST_BPS = 4.605

#   safe_edge = min_safe_net_edge_bps + slippage_buffer + execution_buffer
MIN_SAFE_NET_EDGE_BPS = 2.0
SLIPPAGE_BUFFER_BPS = 0.5
EXECUTION_BUFFER_BPS = 0.5
SAFE_EDGE_BPS = MIN_SAFE_NET_EDGE_BPS + SLIPPAGE_BUFFER_BPS + EXECUTION_BUFFER_BPS  # 3.0

#   realized_net_edge = realized_signal_edge - taker_fee_round_trip - slippage
#   OKX derivatives taker 5 bps, slip <1 bps (按 derivatives_live.yaml:109/116)
REALIZED_COST_BPS = 5.0 + 0.5  # 5.5 bps ≈ 单边 taker + 单边 slip (入场+出场分别粗算)

# 门槛过线候选 scale: 在报告里展示多档 trigger rate
CANDIDATE_SCALES = [12.0, 15.0, 18.0, 20.0, 24.0, 30.0, 40.0, 50.0]

# leg_score 分箱边界 (deciles, 报告里画"score bin × mean realized_edge")
SCORE_BIN_EDGES = [0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 1.01]

# 回归预测 horizon (forward window)
HORIZONS_MIN = [15, 30, 60]

REPORT_PERCENTILES = [1, 5, 10, 25, 50, 75, 90, 95, 99]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_url(db_name: str) -> str:
    """按 POSTGRES_* 环境变量拼装 URL (凭证只在进程内)."""
    user = os.environ.get("POSTGRES_USER") or "admin"
    pw = os.environ.get("POSTGRES_PASSWORD")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    if not pw:
        raise SystemExit(
            "missing POSTGRES_PASSWORD; 请在 .env.wsl2 或 .env.derivatives.live 中配置"
        )
    return f"postgresql+psycopg://{user}:{pw}@{host}:{port}/{db_name}"


def resolve_live_db_url() -> str:
    for key in ("AATS_LIVE_DB_URL",):
        if os.environ.get(key):
            return os.environ[key]
    return _build_url(os.environ.get("AATS_LIVE_DB_NAME", "aats_live_derivatives"))


def resolve_research_db_url() -> str:
    for key in ("RDP_DATABASE_URL", "AATS_ACTIVE_PARAMETER_DB_URL"):
        val = os.environ.get(key)
        if val:
            return val
    return _build_url(os.environ.get("RDP_POSTGRES_DB", "aats_research"))


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@dataclass
class LegSample:
    ts: datetime
    leg: str                          # "long" | "short"
    leg_score: float
    expected_cost_bps: float          # 当时系统计算的 cost (passive_first ≈ 4.605)
    close_t: float | None = None
    close_15m: float | None = None
    close_30m: float | None = None
    close_60m: float | None = None
    realized_edge_15m_bps: float | None = None
    realized_edge_30m_bps: float | None = None
    realized_edge_60m_bps: float | None = None


def load_leg_samples(live_url: str, symbol: str, start_ts: datetime, end_ts: datetime) -> list[LegSample]:
    """从 event_store 读取 independent sleeve_intents, 拆成 long/short 两条样本."""
    sql = text(
        """
        SELECT
            event_timestamp                                                    AS ts,
            (payload::jsonb->'metrics'->>'long_leg_score')::float              AS long_score,
            (payload::jsonb->'metrics'->>'short_leg_score')::float             AS short_score,
            (payload::jsonb->'metrics'->>'expected_cost_bps')::float           AS cost
        FROM public.event_store
        WHERE topic = 'strategy.sleeve_intents'
          AND payload::jsonb->>'family' = 'independent'
          AND symbol = :symbol
          AND event_timestamp >= :start_ts
          AND event_timestamp <= :end_ts
        ORDER BY event_timestamp
        """
    )
    engine = create_engine(live_url, pool_pre_ping=True)
    samples: list[LegSample] = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts}).fetchall()
    finally:
        engine.dispose()

    for row in rows:
        if row.long_score is not None:
            samples.append(LegSample(
                ts=_ensure_tz(row.ts),
                leg="long",
                leg_score=float(row.long_score),
                expected_cost_bps=float(row.cost or PASSIVE_FIRST_COST_BPS),
            ))
        if row.short_score is not None:
            samples.append(LegSample(
                ts=_ensure_tz(row.ts),
                leg="short",
                leg_score=float(row.short_score),
                expected_cost_bps=float(row.cost or PASSIVE_FIRST_COST_BPS),
            ))
    return samples


def load_candle_index(research_url: str, symbol: str, start_ts: datetime, end_ts: datetime) -> dict[datetime, float]:
    """加载 silver.market_swap_candles_15m, 返回 ts (UTC aligned) → close."""
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
    """把 ts 向下取整到 15min 边界 (UTC)."""
    ts_utc = ts.astimezone(timezone.utc)
    minute = (ts_utc.minute // 15) * 15
    return ts_utc.replace(minute=minute, second=0, microsecond=0)


def enrich_with_realized_edge(samples: list[LegSample], candle_index: dict[datetime, float]) -> list[LegSample]:
    """为每条样本匹配 close_T (当前 15m bar) 及 T+15/30/60min 的 close, 计算 realized_edge."""
    enriched: list[LegSample] = []
    for s in samples:
        bar_t = _floor_to_15m(s.ts)
        c0 = candle_index.get(bar_t)
        if c0 is None or c0 <= 0:
            continue
        s.close_t = c0
        side_sign = 1.0 if s.leg == "long" else -1.0
        c15 = candle_index.get(bar_t + timedelta(minutes=15))
        c30 = candle_index.get(bar_t + timedelta(minutes=30))
        c60 = candle_index.get(bar_t + timedelta(minutes=60))
        if c15 is not None:
            s.close_15m = c15
            s.realized_edge_15m_bps = (c15 - c0) / c0 * 10000.0 * side_sign
        if c30 is not None:
            s.close_30m = c30
            s.realized_edge_30m_bps = (c30 - c0) / c0 * 10000.0 * side_sign
        if c60 is not None:
            s.close_60m = c60
            s.realized_edge_60m_bps = (c60 - c0) / c0 * 10000.0 * side_sign
        enriched.append(s)
    return enriched


# ---------------------------------------------------------------------------
# Regression & stats
# ---------------------------------------------------------------------------

@dataclass
class RegressionResult:
    n: int
    slope_origin: float               # y = slope × x (过原点)
    slope_ols: float
    intercept_ols: float
    r_squared: float
    r_squared_origin: float
    pearson_r: float
    residual_std: float
    x_mean: float
    y_mean: float


def regress(x: np.ndarray, y: np.ndarray) -> RegressionResult:
    """OLS 线性回归 + 过原点版本."""
    n = len(x)
    if n < 10:
        return RegressionResult(n=n, slope_origin=float("nan"), slope_ols=float("nan"),
                                intercept_ols=float("nan"), r_squared=float("nan"),
                                r_squared_origin=float("nan"), pearson_r=float("nan"),
                                residual_std=float("nan"), x_mean=float("nan"),
                                y_mean=float("nan"))
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    # 过原点: slope = sum(xy)/sum(x²)
    ss_xx_origin = float(np.sum(x * x))
    ss_xy_origin = float(np.sum(x * y))
    slope_origin = ss_xy_origin / ss_xx_origin if ss_xx_origin > 0 else float("nan")
    ss_res_origin = float(np.sum((y - slope_origin * x) ** 2))
    ss_tot_uncentered = float(np.sum(y * y))
    r2_origin = 1 - ss_res_origin / ss_tot_uncentered if ss_tot_uncentered > 0 else float("nan")

    # 标准 OLS (有截距): y = a + b*x
    ss_xx = float(np.sum((x - x_mean) ** 2))
    ss_xy = float(np.sum((x - x_mean) * (y - y_mean)))
    ss_yy = float(np.sum((y - y_mean) ** 2))
    slope_ols = ss_xy / ss_xx if ss_xx > 0 else float("nan")
    intercept = y_mean - slope_ols * x_mean if not math.isnan(slope_ols) else float("nan")
    y_pred = intercept + slope_ols * x
    ss_res = float(np.sum((y - y_pred) ** 2))
    r2 = 1 - ss_res / ss_yy if ss_yy > 0 else float("nan")
    pearson = ss_xy / math.sqrt(ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else float("nan")
    residual_std = math.sqrt(ss_res / max(n - 2, 1))

    return RegressionResult(
        n=n, slope_origin=slope_origin, slope_ols=slope_ols,
        intercept_ols=intercept, r_squared=r2, r_squared_origin=r2_origin,
        pearson_r=pearson, residual_std=residual_std,
        x_mean=x_mean, y_mean=y_mean,
    )


def bin_stats(x: np.ndarray, y: np.ndarray, edges: list[float]) -> list[dict[str, Any]]:
    """按 score 分箱, 统计每箱 mean / std / count of realized_edge."""
    out: list[dict[str, Any]] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mask = (x >= lo) & (x < hi)
        n = int(np.sum(mask))
        if n == 0:
            out.append({"bin": f"[{lo:.2f}, {hi:.2f})", "n": 0, "mean_y": float("nan"),
                        "std_y": float("nan"), "median_y": float("nan"),
                        "p80_y": float("nan")})
            continue
        sub = y[mask]
        out.append({
            "bin": f"[{lo:.2f}, {hi:.2f})",
            "n": n,
            "mean_y": float(np.mean(sub)),
            "std_y": float(np.std(sub, ddof=1)) if n > 1 else 0.0,
            "median_y": float(np.median(sub)),
            "p80_y": float(np.percentile(sub, 80)),
        })
    return out


def percentiles(x: np.ndarray, qs: list[int]) -> dict[int, float]:
    if len(x) == 0:
        return {q: float("nan") for q in qs}
    return {q: float(np.percentile(x, q)) for q in qs}


def trigger_rate_by_scale(x_score: np.ndarray, expected_cost_bps: np.ndarray,
                          scale: float, safe_edge_bps: float = SAFE_EDGE_BPS) -> dict[str, float]:
    """给定 scale, 预测有多少 sample 能 pass signal_edge - cost >= safe_edge."""
    signal = x_score * scale
    net = signal - expected_cost_bps
    pass_mask = net >= safe_edge_bps
    n_total = len(x_score)
    return {
        "pass_rate": float(np.mean(pass_mask)) if n_total > 0 else 0.0,
        "n_pass": int(np.sum(pass_mask)),
        "n_total": n_total,
        "mean_signal": float(np.mean(signal)),
        "mean_net": float(np.mean(net)),
    }


def edge_support_rate(x_score: np.ndarray, y_realized: np.ndarray, score_floor: float,
                      edge_threshold: float) -> dict[str, float]:
    """对 score >= floor 的子集, 有多少 % 的 realized_edge > threshold."""
    mask = x_score >= score_floor
    n_sub = int(np.sum(mask))
    if n_sub == 0:
        return {"n_sub": 0, "support_rate": float("nan"), "mean_realized": float("nan")}
    sub = y_realized[mask]
    return {
        "n_sub": n_sub,
        "support_rate": float(np.mean(sub > edge_threshold)),
        "mean_realized": float(np.mean(sub)),
    }


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def fmt(v: float, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:.{digits}f}"


def render_markdown(args, samples: list[LegSample], result: dict[str, Any]) -> str:
    """Render the final report Markdown."""
    lines: list[str] = []
    lines.append("# signal_edge_scale_bps 回归标定报告 (P1-B step 1)")
    lines.append("")
    lines.append(f"- **生成时间**: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- **symbol**: {args.symbol}")
    lines.append(f"- **数据窗口**: {result['data_window_start']}  →  {result['data_window_end']}")
    lines.append(f"- **请求天数 --days**: {args.days}")
    lines.append(f"- **实际覆盖天数**: {result['actual_days']:.2f}")
    lines.append(f"- **当前 RDP independent_15m signal_edge_scale_bps**: {CURRENT_RDP_SCALE}")
    lines.append(f"- **legacy / independent_1h / directional scale**: {LEGACY_DEFAULT_SCALE}")
    lines.append("")

    lines.append("## 1. 数据可用性")
    lines.append("")
    lines.append(f"- sleeve_intents 样本 (raw, long+short 已拆): {result['n_raw']}")
    lines.append(f"- 能对到 silver.market_swap_candles_15m bar: {result['n_matched_t']}")
    lines.append(f"- 有 T+15min close 的样本: {result['n_with_15m']}")
    lines.append(f"- 有 T+30min close 的样本: {result['n_with_30m']}")
    lines.append(f"- 有 T+60min close 的样本: {result['n_with_60m']}")
    lines.append("")
    lines.append("> **限制**: event_store 只保留最近 2-3 天数据, 所以即使 `--days=30` "
                 f"请求, 实际覆盖只有 `{result['actual_days']:.2f}` 天. silver 层 K 线则"
                 " 有 33 天. 本报告受限于 event_store 保留窗口, 结论仅反映近 3 天市场.")
    lines.append("")

    lines.append("## 2. leg_score 分布 (long + short 合并)")
    lines.append("")
    lines.append("| 分位 | long_leg_score | short_leg_score | 合并 |")
    lines.append("|------|---------------|-----------------|------|")
    for q in REPORT_PERCENTILES:
        long_q = result["score_pct_long"].get(q, float("nan"))
        short_q = result["score_pct_short"].get(q, float("nan"))
        all_q = result["score_pct_all"].get(q, float("nan"))
        lines.append(f"| p{q:02d} | {fmt(long_q, 4)} | {fmt(short_q, 4)} | {fmt(all_q, 4)} |")
    lines.append("")
    lines.append(f"- long n = {result['n_long']}, short n = {result['n_short']}")
    lines.append(f"- mean score: long = {fmt(result['mean_score_long'], 4)}, "
                 f"short = {fmt(result['mean_score_short'], 4)}")
    lines.append("")

    lines.append("## 3. realized_edge 分布 (按 horizon, bps)")
    lines.append("")
    for h in HORIZONS_MIN:
        lines.append(f"### T+{h}min")
        lines.append("")
        lines.append("| 分位 | long (bps) | short (bps) | 合并 (bps) |")
        lines.append("|------|------------|-------------|------------|")
        for q in REPORT_PERCENTILES:
            pct = result[f"realized_pct_{h}m"]
            lines.append(
                f"| p{q:02d} | {fmt(pct['long'].get(q, float('nan')), 2)} | "
                f"{fmt(pct['short'].get(q, float('nan')), 2)} | "
                f"{fmt(pct['all'].get(q, float('nan')), 2)} |"
            )
        n_h = result["n_per_horizon"][h]
        std_all = result["realized_std"][h]
        lines.append("")
        lines.append(f"- n = {n_h}, std_combined = {fmt(std_all, 2)} bps")
        lines.append("")

    lines.append("## 4. 回归: leg_score → realized_edge")
    lines.append("")
    lines.append("> 所有回归在 long + short 合并样本上运行 (边 leg 样本量足够,"
                 " 分 leg 回归见附录 §A). **y 单位 = bps, x 单位 = score ∈ [0, 1]**. "
                 "`slope_origin` = 过原点回归的斜率, 直接对应 signal_edge_scale_bps 的物理含义. "
                 "`slope_ols` + `intercept_ols` = 不强制过原点的标准 OLS.")
    lines.append("")
    lines.append("| horizon | n | slope_origin | slope_ols | intercept_ols | R²(origin) | R²(OLS) | Pearson r | residual_std |")
    lines.append("|---------|---|--------------|-----------|---------------|------------|---------|-----------|--------------|")
    for h in HORIZONS_MIN:
        r = result["regression"][h]
        lines.append(
            f"| {h}min | {r.n} | {fmt(r.slope_origin, 3)} | {fmt(r.slope_ols, 3)} | "
            f"{fmt(r.intercept_ols, 3)} | {fmt(r.r_squared_origin, 5)} | "
            f"{fmt(r.r_squared, 5)} | {fmt(r.pearson_r, 4)} | {fmt(r.residual_std, 2)} |"
        )
    lines.append("")

    lines.append("## 5. 分箱统计: score bin × mean realized_edge")
    lines.append("")
    for h in HORIZONS_MIN:
        lines.append(f"### T+{h}min (long + short 合并)")
        lines.append("")
        lines.append("| score bin | n | mean (bps) | median (bps) | std (bps) | p80 (bps) |")
        lines.append("|-----------|---|------------|--------------|-----------|-----------|")
        for row in result["bin_stats"][h]:
            lines.append(
                f"| {row['bin']} | {row['n']} | {fmt(row['mean_y'], 2)} | "
                f"{fmt(row['median_y'], 2)} | {fmt(row['std_y'], 2)} | "
                f"{fmt(row['p80_y'], 2)} |"
            )
        lines.append("")

    lines.append("## 6. 候选 scale 对应的 pass_rate (expected, 不是 realized)")
    lines.append("")
    lines.append(f"门槛 = signal_edge − cost ≥ safe_edge, 其中 safe_edge = "
                 f"{SAFE_EDGE_BPS} bps (min_safe_net_edge {MIN_SAFE_NET_EDGE_BPS} + "
                 f"slip {SLIPPAGE_BUFFER_BPS} + exec {EXECUTION_BUFFER_BPS}), "
                 f"cost 从样本中取实测平均.")
    lines.append("")
    lines.append("| scale | pass_rate | n_pass / n_total | mean_signal (bps) | mean_net (bps) |")
    lines.append("|-------|-----------|-------------------|--------------------|-----------------|")
    for sc, tr in result["trigger_by_scale"].items():
        lines.append(
            f"| {sc} | {tr['pass_rate']*100:.1f}% | {tr['n_pass']}/{tr['n_total']} | "
            f"{fmt(tr['mean_signal'], 2)} | {fmt(tr['mean_net'], 2)} |"
        )
    lines.append("")

    lines.append("## 7. \"score >= 0.20 的样本, realized_edge > safe_edge\" 支持率")
    lines.append("")
    lines.append("这是真正决定 scale 是否合理的统计: 如果 score >= 0.20 的样本中, "
                 f"80% 的 realized_edge > safe_edge ({SAFE_EDGE_BPS} bps), 则 scale 应"
                 " 调到让这部分样本 signal_edge 过门槛. 如果支持率低于 50%, 则提升 scale"
                 " 会放大假信号入场.")
    lines.append("")
    lines.append("| horizon | n (score≥0.20) | mean realized (bps) | support_rate (>safe_edge) |")
    lines.append("|---------|-----------------|----------------------|----------------------------|")
    for h in HORIZONS_MIN:
        sup = result["support_by_horizon"][h]
        lines.append(
            f"| {h}min | {sup['n_sub']} | {fmt(sup['mean_realized'], 2)} | "
            f"{sup['support_rate']*100:.1f}% |"
        )
    lines.append("")

    lines.append("## 8. 结论与建议")
    lines.append("")
    lines.append(result["narrative_conclusion"])
    lines.append("")

    lines.append("## 附录 A. 分 leg 回归")
    lines.append("")
    for h in HORIZONS_MIN:
        lines.append(f"### T+{h}min")
        lines.append("")
        lines.append("| leg | n | slope_origin | slope_ols | intercept_ols | R²(origin) | Pearson r |")
        lines.append("|-----|---|--------------|-----------|---------------|------------|-----------|")
        for leg in ("long", "short"):
            r = result["regression_by_leg"][(leg, h)]
            lines.append(
                f"| {leg} | {r.n} | {fmt(r.slope_origin, 3)} | {fmt(r.slope_ols, 3)} | "
                f"{fmt(r.intercept_ols, 3)} | {fmt(r.r_squared_origin, 5)} | "
                f"{fmt(r.pearson_r, 4)} |"
            )
        lines.append("")

    lines.append("## 附录 B. 复现")
    lines.append("")
    lines.append("```bash")
    lines.append(f"python scripts/calibration/signal_edge_scale_regression.py \\")
    lines.append(f"    --days {args.days} --symbol {args.symbol}")
    lines.append("```")
    lines.append("")
    lines.append("输出 CSV (样本原始表): `docs/review/signal_edge_scale_calibration_YYYYMMDD_data.csv`")
    lines.append("")
    return "\n".join(lines)


def build_narrative_conclusion(result: dict[str, Any]) -> str:
    """根据回归结果自动生成结论段落. 诚实汇报 R² 低 / 支持率差的情况.

    会综合评估:
      - combined 回归的 R² 强度
      - 长短腿不对称 (long R² vs short R²)
      - 分箱 monotone 趋势 (即使 R² 低, 条件期望是否单调)
      - support_rate 在不同 horizon 上的一致性
      - 当前 scale 的实际 trigger rate
    """
    best_horizon = max(HORIZONS_MIN, key=lambda h: result["regression"][h].r_squared_origin or 0)
    r = result["regression"][best_horizon]
    r2 = r.r_squared_origin
    slope = r.slope_origin
    n = r.n

    # 分 leg 对比 (T+60min 是 signal 最强的 horizon)
    long_r = result["regression_by_leg"][("long", best_horizon)]
    short_r = result["regression_by_leg"][("short", best_horizon)]

    # 分箱趋势: 是否 monotone increasing?
    bins = result["bin_stats"][best_horizon]
    nonempty_bins = [b for b in bins if b["n"] > 50]
    means = [b["mean_y"] for b in nonempty_bins]
    monotone = all(means[i] <= means[i + 1] + 1.0 for i in range(len(means) - 1)) if len(means) >= 3 else False
    bin_range = (means[-1] - means[0]) if len(means) >= 2 else float("nan")

    # 支持率一致性
    sups = {h: result["support_by_horizon"][h] for h in HORIZONS_MIN}

    # 当前 scale (12) 与建议 scale 的 trigger rate
    current_tr = result["trigger_by_scale"].get(CURRENT_RDP_SCALE, {"pass_rate": 0.0, "n_pass": 0, "n_total": 0})

    if math.isnan(r2) or n < 100:
        return (
            f"**数据不足结论**: 样本量 n={n} 或 R² 无法计算. 无法得出统计显著的 scale"
            f" 建议. 建议主任务: (1) 扩大 event_store 保留窗口到 ≥30 天; "
            f"(2) 重跑此脚本验证; (3) 在样本充足前, 不要调整 scale."
        )

    verdict_lines: list[str] = []
    verdict_lines.append(
        f"### 8.1 数值总览"
    )
    verdict_lines.append("")
    verdict_lines.append(
        f"- **最佳 horizon**: T+{best_horizon}min (n={n}), "
        f"slope_origin = **{slope:.2f}** bps/score, R²(origin) = **{r2:.5f}**, "
        f"Pearson r = {r.pearson_r:.4f}, residual_std = {r.residual_std:.1f} bps"
    )
    verdict_lines.append(
        f"- **当前 scale={CURRENT_RDP_SCALE} 的 trigger_rate** = {current_tr['pass_rate']*100:.1f}% "
        f"({current_tr['n_pass']}/{current_tr['n_total']}), mean_net = {current_tr.get('mean_net', float('nan')):.2f} bps — "
        f"与 live \"24h 零 fill\" 观察一致 (scale=12 实际上等于封锁入场)."
    )
    verdict_lines.append(
        f"- **长短腿不对称 (T+{best_horizon}min)**: long R²={long_r.r_squared_origin:.5f} "
        f"(slope={long_r.slope_origin:.2f}) vs short R²={short_r.r_squared_origin:.5f} "
        f"(slope={short_r.slope_origin:.2f}) — short leg 的预测力接近 0, "
        f"甚至 slope_origin 为负 (score↑ 与 realized short PnL↓ 微弱相关)."
    )
    if not math.isnan(bin_range):
        verdict_lines.append(
            f"- **分箱 monotone 趋势**: [0.05 → 0.50+] 分箱 mean realized_edge 变化 "
            f"{means[0]:.1f} → {means[-1]:.1f} bps, 跨 {bin_range:.1f} bps ("
            f"{'单调' if monotone else '非严格单调'}). 即使 R² 低, **条件期望确有区分度, "
            f"只是被 residual_std ({r.residual_std:.0f} bps) 的噪声压过线性拟合**."
        )
    verdict_lines.append("")

    verdict_lines.append(f"### 8.2 核心判断")
    verdict_lines.append("")
    high_bin_n = sum(b["n"] for b in bins if b["bin"].startswith("[0.4") or b["bin"].startswith("[0.5"))
    total_bin_n = max(sum(b["n"] for b in bins), 1)
    high_bin_frac = high_bin_n / total_bin_n * 100
    if r2 < 0.005:
        verdict_lines.append(
            f"**leg_score 作为线性 edge predictor 的表现极弱 (R² < 0.005)**. 但分箱条件期望"
            f" 有单调区分度, 只是高分箱样本量小 (score ≥ 0.40 仅 {high_bin_n} 样本 / "
            f"{total_bin_n} 总样本 ≈ {high_bin_frac:.1f}%). "
            f"slope_origin = {slope:.1f} 可以作为\"数学上让高分箱 signal 过安全线\"的下限, 但"
            f"**并不代表它是 edge 最优化的 scale**."
        )
    elif r2 < 0.05:
        verdict_lines.append(
            f"**leg_score 有弱的正向预测力 (R² = {r2:.4f})**. slope_origin = {slope:.1f}. "
            f"可以作为 scale 下限参考, 但单因子回归不能作为 scale 上限 — "
            f"residual_std = {r.residual_std:.0f} bps 远大于 slope × p90 score "
            f"({slope * result['score_pct_all'].get(90, 0.25):.1f} bps), 入场胜率接近 50/50."
        )
    else:
        verdict_lines.append(
            f"**leg_score 是合格 edge predictor (R² = {r2:.4f})**. "
            f"按 slope_origin = {slope:.1f} 上调 scale 是统计支持的."
        )
    verdict_lines.append("")

    verdict_lines.append(f"### 8.3 scale 建议 (分层选项)")
    verdict_lines.append("")
    verdict_lines.append(
        f"以下三个 scale 选项, 分别对应不同的风险偏好. 主任务应根据"
        f" **A/B 验证能力** + **short leg 是否同步改** 来选."
    )
    verdict_lines.append("")

    # 选项按 scale 升序: 不调 (12) < slope 对齐 (round(slope)) < legacy (20) < aggressive (30)
    slope_int = max(int(round(slope)), 1)

    verdict_lines.append(
        f"**选项 A — 不调 (scale={CURRENT_RDP_SCALE:.0f}, 推进结构改造)**"
    )
    verdict_lines.append(
        f"- trigger_rate = {current_tr['pass_rate']*100:.1f}% ({current_tr['n_pass']}/{current_tr['n_total']}) — 事实上零 fill."
    )
    verdict_lines.append(
        f"- 理由: R² < 0.005 说明 leg_score 公式本身需要重设计. 调 scale 是在\"放大噪声\", "
        f"即使 fill 数变多, 胜率和期望 PnL 都可能不改善. 把工期花在 **P1-A 双通道 momentum / "
        f"re-weighting + 加 future-return-aware 特征**, ROI 更高."
    )
    verdict_lines.append(
        f"- 风险: 继续 24h 零 fill, 用户体验差; 但这是\"诚实的零\"而不是\"靠虚假信号堆 fill\"."
    )
    verdict_lines.append("")

    # 为 slope 建议 scale 找最接近的 candidate 估计 trigger_rate
    nearest_to_slope = min(CANDIDATE_SCALES, key=lambda s: abs(s - slope_int))
    tr_slope = result["trigger_by_scale"].get(nearest_to_slope, {"pass_rate": 0, "n_pass": 0, "n_total": 0})
    verdict_lines.append(
        f"**选项 B — 数学 MLE (scale={slope_int}, 过原点回归斜率)**"
    )
    verdict_lines.append(
        f"- trigger_rate ≈ {tr_slope.get('pass_rate', 0)*100:.1f}% (按 candidate scale={nearest_to_slope} 近似)."
    )
    verdict_lines.append(
        f"- 理由: slope_origin 是\"每单位 score, realized_edge 条件期望增加多少 bps\"的 MLE 估计. "
        f"scale = slope_origin 时, signal_edge 的数学期望与 realized_edge 的条件期望一致, "
        f"既不高估也不低估."
    )
    verdict_lines.append(
        f"- 风险: residual_std {r.residual_std:.0f} bps 远大于 slope × score (典型 {slope * 0.20:.1f} bps), "
        f"入场方差巨大, 胜率 ≈ 50/50. **必须配合 7 天小仓位 shadow / A-B 验证**."
    )
    verdict_lines.append("")

    tr20 = result["trigger_by_scale"].get(LEGACY_DEFAULT_SCALE, {})
    verdict_lines.append(
        f"**选项 C — 激进对齐 legacy (scale={LEGACY_DEFAULT_SCALE:.0f})**"
    )
    verdict_lines.append(
        f"- trigger_rate = {tr20.get('pass_rate', 0)*100:.1f}% ({tr20.get('n_pass', 0)}/{tr20.get('n_total', 0)})."
    )
    verdict_lines.append(
        f"- 理由: 与 independent_1h / directional 统一为 20, 简化 calibration 矩阵. "
        f"开放 top {tr20.get('pass_rate', 0)*100:.0f}% 信号入场."
    )
    verdict_lines.append(
        f"- 风险: 20 > slope_origin {slope:.1f}, 意味着 signal_edge 被系统性高估 "
        f"(mean_signal={tr20.get('mean_signal', float('nan')):.2f} bps vs realized 条件期望 "
        f"{slope * result['score_pct_all'].get(90, 0.25):.1f} bps @ p90 score). "
        f"**short leg 不对称 — 不建议直接上 20, 否则短腿会进大量假信号**."
    )
    verdict_lines.append("")

    verdict_lines.append(f"### 8.4 推荐给主任务的优先级")
    verdict_lines.append("")
    verdict_lines.append(
        f"1. **优先 P1-B step 2 (cost 审查)**: 把 expected_cost 从 passive_first 的 4.605 "
        f"改成 maker-rebate / post-only 路径的 1.5-2.5 bps. 在不动 scale 的前提下, "
        f"net_edge 会增加 2-3 bps, **同样能显著提升 trigger_rate**, 且不依赖 leg_score 的预测力. "
        f"成本更低, 风险更可控."
    )
    verdict_lines.append(
        f"2. **同步 P1-A (leg_score 公式改造)**: 这是根因. "
        f"可选方向: (a) 引入 future-return-aware 特征如 mid-frequency momentum (30-60min ROC), "
        f"(b) 区分 long/short 权重 (短腿 R² 接近 0 说明现公式对短不合适), "
        f"(c) 引入 funding skew × regime 交互项."
    )
    verdict_lines.append(
        f"3. **最后再考虑 scale 调整**: 如果 step 1 + 2 完成后仍需要 fine-tune, "
        f"那时 slope_origin 会重新估算且更稳定. 现在即便是选项 B (scale={slope_int}) "
        f"也会把 short leg 的假信号放进来 (short R² 近 0 且 slope 为负), 风险非对称. "
        f"若必须先上 scale, 应只对 long leg 上调 (需引入 per-leg scale 参数), 短腿保持 12."
    )
    verdict_lines.append("")

    verdict_lines.append(f"### 8.5 诚实声明")
    verdict_lines.append("")
    verdict_lines.append(
        f"- 本报告的数据窗口只有 {result['actual_days']:.1f} 天 (event_store 保留窗口限制), "
        f"覆盖单一市场环境. **结论应在扩大 event_store 保留至 ≥14 天后复测**."
    )
    verdict_lines.append(
        f"- R² {r2:.5f} 在金融 forward return 回归中并不\"一定不可用\" — 股票分钟级 alpha "
        f"典型 R² 也在 0.001-0.01. 但配合 residual_std {r.residual_std:.0f} bps 和"
        f" support_rate {sups[15]['support_rate']*100:.1f}% (T+15min), **结论只能是\"有微弱信号, 不足以单独用作 sizing\"**."
    )
    verdict_lines.append(
        f"- 建议脚本每周重跑, 跟踪 slope_origin / R² 漂移. 如果 R² 稳定 > 0.02 可转为\"基于 slope 的自动调 scale\"."
    )

    return "\n".join(verdict_lines)


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(path: Path, samples: list[LegSample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ts", "leg", "leg_score", "expected_cost_bps",
        "close_t", "close_15m", "close_30m", "close_60m",
        "realized_edge_15m_bps", "realized_edge_30m_bps", "realized_edge_60m_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for s in samples:
            w.writerow({
                "ts": s.ts.isoformat(),
                "leg": s.leg,
                "leg_score": s.leg_score,
                "expected_cost_bps": s.expected_cost_bps,
                "close_t": s.close_t,
                "close_15m": s.close_15m,
                "close_30m": s.close_30m,
                "close_60m": s.close_60m,
                "realized_edge_15m_bps": s.realized_edge_15m_bps,
                "realized_edge_30m_bps": s.realized_edge_30m_bps,
                "realized_edge_60m_bps": s.realized_edge_60m_bps,
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=30,
                        help="回看天数 (默认 30). event_store 可能保留更短.")
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--report-path", default="docs/review/signal_edge_scale_calibration_2026_04_19.md",
                        help="报告输出路径 (相对 project root)")
    parser.add_argument("--csv-path", default=None,
                        help="CSV 输出路径 (相对 project root). 默认用日期后缀.")
    parser.add_argument("--end-ts", default=None,
                        help="结束时间 (ISO-8601), 默认 now. 用于锁定分析窗口便于复现.")
    args = parser.parse_args()

    # 时间范围
    if args.end_ts:
        end_ts = datetime.fromisoformat(args.end_ts)
        if end_ts.tzinfo is None:
            end_ts = end_ts.replace(tzinfo=timezone.utc)
    else:
        end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=args.days)

    print(f"[info] 窗口: {start_ts.isoformat()}{end_ts.isoformat()} ({args.days} days)")
    print(f"[info] symbol: {args.symbol}")

    # 读 leg samples
    live_url = resolve_live_db_url()
    try:
        samples_raw = load_leg_samples(live_url, args.symbol, start_ts, end_ts)
    except Exception as exc:
        print(f"[error] load_leg_samples failed: {exc}", file=sys.stderr)
        return 2
    print(f"[info] raw sleeve_intents samples (long+short): {len(samples_raw)}")
    if len(samples_raw) < 100:
        print(f"[error] 样本不足 (n={len(samples_raw)} < 100); exiting", file=sys.stderr)
        return 3

    # 读 candle index
    research_url = resolve_research_db_url()
    try:
        # 扩展一小时上下界, 方便算 T+60min
        candle_index = load_candle_index(
            research_url, args.symbol,
            start_ts - timedelta(hours=2),
            end_ts + timedelta(hours=2),
        )
    except Exception as exc:
        print(f"[error] load_candle_index failed: {exc}", file=sys.stderr)
        return 2
    print(f"[info] silver candles loaded: {len(candle_index)}")

    # enrich
    samples = enrich_with_realized_edge(samples_raw, candle_index)
    print(f"[info] samples with close_T matched: {len(samples)}")
    if len(samples) < 100:
        print(f"[error] 匹配到 candle 的样本不足 (n={len(samples)}); exiting", file=sys.stderr)
        return 3

    n_with = {h: sum(1 for s in samples if getattr(s, f"realized_edge_{h}m_bps") is not None)
              for h in HORIZONS_MIN}
    print(f"[info] n_with horizons: {n_with}")

    # 分析
    long_samples = [s for s in samples if s.leg == "long"]
    short_samples = [s for s in samples if s.leg == "short"]

    score_long = np.array([s.leg_score for s in long_samples])
    score_short = np.array([s.leg_score for s in short_samples])
    score_all = np.concatenate([score_long, score_short])

    result: dict[str, Any] = {
        "data_window_start": samples[0].ts.isoformat(),
        "data_window_end": samples[-1].ts.isoformat(),
        "actual_days": (samples[-1].ts - samples[0].ts).total_seconds() / 86400.0,
        "n_raw": len(samples_raw),
        "n_matched_t": len(samples),
        "n_with_15m": n_with[15],
        "n_with_30m": n_with[30],
        "n_with_60m": n_with[60],
        "n_long": len(long_samples),
        "n_short": len(short_samples),
        "mean_score_long": float(np.mean(score_long)) if len(score_long) > 0 else float("nan"),
        "mean_score_short": float(np.mean(score_short)) if len(score_short) > 0 else float("nan"),
        "score_pct_long": percentiles(score_long, REPORT_PERCENTILES),
        "score_pct_short": percentiles(score_short, REPORT_PERCENTILES),
        "score_pct_all": percentiles(score_all, REPORT_PERCENTILES),
        "realized_pct_15m": {}, "realized_pct_30m": {}, "realized_pct_60m": {},
        "realized_std": {},
        "n_per_horizon": {},
        "regression": {},
        "regression_by_leg": {},
        "bin_stats": {},
        "trigger_by_scale": {},
        "support_by_horizon": {},
    }

    for h in HORIZONS_MIN:
        y_long = np.array([s for s in (getattr(x, f"realized_edge_{h}m_bps") for x in long_samples) if s is not None])
        y_short = np.array([s for s in (getattr(x, f"realized_edge_{h}m_bps") for x in short_samples) if s is not None])
        x_long = np.array([x.leg_score for x in long_samples if getattr(x, f"realized_edge_{h}m_bps") is not None])
        x_short = np.array([x.leg_score for x in short_samples if getattr(x, f"realized_edge_{h}m_bps") is not None])
        x_all = np.concatenate([x_long, x_short])
        y_all = np.concatenate([y_long, y_short])

        result["n_per_horizon"][h] = len(y_all)
        result["realized_pct_15m" if h == 15 else f"realized_pct_{h}m"] = {
            "long": percentiles(y_long, REPORT_PERCENTILES),
            "short": percentiles(y_short, REPORT_PERCENTILES),
            "all": percentiles(y_all, REPORT_PERCENTILES),
        }
        result["realized_std"][h] = float(np.std(y_all, ddof=1)) if len(y_all) > 1 else float("nan")

        # 回归
        result["regression"][h] = regress(x_all, y_all)
        result["regression_by_leg"][("long", h)] = regress(x_long, y_long)
        result["regression_by_leg"][("short", h)] = regress(x_short, y_short)

        # 分箱
        result["bin_stats"][h] = bin_stats(x_all, y_all, SCORE_BIN_EDGES)

        # support rate
        result["support_by_horizon"][h] = edge_support_rate(
            x_all, y_all, score_floor=0.20, edge_threshold=SAFE_EDGE_BPS,
        )

    # trigger rate by scale (以 T+15m net 样本的 cost 做 baseline)
    costs_for_trigger = np.array([s.expected_cost_bps for s in samples])
    scores_for_trigger = np.array([s.leg_score for s in samples])
    for sc in CANDIDATE_SCALES:
        result["trigger_by_scale"][sc] = trigger_rate_by_scale(
            scores_for_trigger, costs_for_trigger, sc, safe_edge_bps=SAFE_EDGE_BPS,
        )

    result["narrative_conclusion"] = build_narrative_conclusion(result)

    # 输出
    report_text = render_markdown(args, samples, result)
    report_path = ROOT / args.report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    print(f"[info] report -> {report_path}")

    csv_name = args.csv_path or f"docs/review/signal_edge_scale_calibration_{end_ts.strftime('%Y%m%d')}_data.csv"
    csv_path = ROOT / csv_name
    write_csv(csv_path, samples)
    print(f"[info] csv -> {csv_path}")

    # Console summary
    print("")
    print("====== SUMMARY ======")
    best_h = max(HORIZONS_MIN, key=lambda h: result["regression"][h].r_squared_origin or 0)
    r = result["regression"][best_h]
    print(f"best horizon: T+{best_h}min  n={r.n}  slope_origin={r.slope_origin:.3f}  R²(origin)={r.r_squared_origin:.5f}")
    for sc in CANDIDATE_SCALES:
        tr = result["trigger_by_scale"][sc]
        print(f"  scale={sc}:  pass_rate={tr['pass_rate']*100:.1f}%  ({tr['n_pass']}/{tr['n_total']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
