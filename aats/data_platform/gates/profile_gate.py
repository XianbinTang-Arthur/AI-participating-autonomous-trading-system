"""Profile-level Gate — 三指标(Sharpe / MaxDD / Activity)预校验。

设计来源: docs/task/rdp_scope_expansion_detailed_design_v3.md §1.4

规则(§6.2 决定):
  - sharpe_ratio    = candidate_sharpe / current_sharpe       必须 ≥ 0.95
  - maxdd_ratio     = candidate_maxdd  / current_maxdd        必须 ≤ 1.05
  - activity_ratio  = candidate_trades / current_trades       必须 ≥ 0.50

三项全过才 allow_apply=True;任一不过在 failures 列出具体数字。

activity_ratio ≥ 0.50 是专门防本次事件的:seed 把 min_signal_edge_bps
设成 13.0 导致实盘几乎不触发(activity 接近 0),但 Sharpe/MaxDD 在空样
本下计算结果"好看"——单独看 Sharpe/MaxDD gate 会通过,加上 activity
才能拦住。
"""

from __future__ import annotations

from dataclasses import dataclass


# Gate 阈值(硬编码常量,改动需走 SOW)
SHARPE_RATIO_MIN = 0.95
MAXDD_RATIO_MAX = 1.05
ACTIVITY_RATIO_MIN = 0.50


@dataclass(frozen=True)
class ProfileGateResult:
    sharpe_ratio: float
    maxdd_ratio: float
    activity_ratio: float
    allow_apply: bool
    failures: tuple[str, ...]


def check_profile_gate(metrics: dict[str, float]) -> ProfileGateResult:
    """运行 profile gate 的三项检查。

    metrics 必须含:
      sharpe_ratio, maxdd_ratio, activity_ratio
    """
    required = {"sharpe_ratio", "maxdd_ratio", "activity_ratio"}
    missing = required - set(metrics.keys())
    if missing:
        raise ValueError(f"metrics missing keys: {sorted(missing)}")

    failures: list[str] = []

    sharpe = float(metrics["sharpe_ratio"])
    maxdd = float(metrics["maxdd_ratio"])
    activity = float(metrics["activity_ratio"])

    if sharpe < SHARPE_RATIO_MIN:
        failures.append(f"sharpe_ratio={sharpe:.3f} < {SHARPE_RATIO_MIN}")
    if maxdd > MAXDD_RATIO_MAX:
        failures.append(f"maxdd_ratio={maxdd:.3f} > {MAXDD_RATIO_MAX}")
    if activity < ACTIVITY_RATIO_MIN:
        failures.append(f"activity_ratio={activity:.3f} < {ACTIVITY_RATIO_MIN}")

    return ProfileGateResult(
        sharpe_ratio=sharpe,
        maxdd_ratio=maxdd,
        activity_ratio=activity,
        allow_apply=not failures,
        failures=tuple(failures),
    )


def compute_metrics_from_replay(
    *,
    current_stats: dict[str, float],
    candidate_stats: dict[str, float],
) -> dict[str, float]:
    """将 current 和 candidate 的原始指标转成三比值。

    current_stats / candidate_stats 需含:
      sharpe(年化), maxdd(负数,越低越差), trades_per_year

    ratio 计算:
      sharpe_ratio  = candidate / current (越高越好)
      maxdd_ratio   = candidate / current (注意 maxdd 是负,ratio > 1 是恶化)
      activity_ratio = candidate / current
    """
    def _safe_div(num: float, den: float) -> float:
        if den == 0:
            return 0.0 if num == 0 else float("inf")
        return num / den

    return {
        "sharpe_ratio": _safe_div(
            float(candidate_stats["sharpe"]),
            float(current_stats["sharpe"]),
        ),
        "maxdd_ratio": _safe_div(
            abs(float(candidate_stats["maxdd"])),
            abs(float(current_stats["maxdd"])),
        ),
        "activity_ratio": _safe_div(
            float(candidate_stats["trades_per_year"]),
            float(current_stats["trades_per_year"]),
        ),
    }
