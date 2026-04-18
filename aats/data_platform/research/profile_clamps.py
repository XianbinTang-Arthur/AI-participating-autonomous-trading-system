"""Profile-level 参数的 clamp 范围 — 唯一来源 (single source of truth)。

设计动机(R1-04):
  strategy_profile_seed.py 的 _clamp_float 硬编码 clamp 范围,
  Profile research job 需要读相同 clamp 来圈 grid。如果两边 drift,
  research 会产出 seed 拒绝的 candidate,治理链路白跑一圈。

本模块是唯一入口,seed + research 都调 :func:`get_profile_clamps`,
保证无死代码、无配置漂移。

新增 profile 时:
  1. 在 PROFILE_CLAMPS 字典加一行,每个关键参数配一个 ClampRange
  2. seed 里对应的 _clamp_float(...) 改成 get_profile_clamps(profile_id)[key]
  3. research 侧从同样的 get_profile_clamps 读 clamp
"""

from __future__ import annotations

from typing import TypedDict


class ClampRange(TypedDict):
    lo: float
    hi: float


# Profile 的白名单 clamp。覆盖 strategy_profile_seed.py 里所有 _clamp_float 的键。
# 精确范围来源:strategy_profile_seed.py 各 profile 的 _clamp_float 调用。
PROFILE_CLAMPS: dict[str, dict[str, ClampRange]] = {
    "trend_normal": {
        "strategy_entry_min_signal_edge_bps": {"lo": 1.5, "hi": 13.0},
        "strategy_entry_alpha_min": {"lo": 0.10, "hi": 0.30},
        "strategy_min_net_edge_bps": {"lo": 2.0, "hi": 10.0},
    },
    "trend_fast": {
        "strategy_entry_min_signal_edge_bps": {"lo": 1.0, "hi": 10.0},
        "strategy_entry_alpha_min": {"lo": 0.08, "hi": 0.25},
        "strategy_min_net_edge_bps": {"lo": 1.5, "hi": 8.0},
    },
    "trend_slow": {
        "strategy_entry_min_signal_edge_bps": {"lo": 2.0, "hi": 15.0},
        "strategy_entry_alpha_min": {"lo": 0.12, "hi": 0.35},
        "strategy_min_net_edge_bps": {"lo": 2.5, "hi": 12.0},
    },
    "balanced": {
        "strategy_entry_min_signal_edge_bps": {"lo": 1.5, "hi": 12.0},
        "strategy_entry_alpha_min": {"lo": 0.10, "hi": 0.28},
        "strategy_min_net_edge_bps": {"lo": 2.0, "hi": 9.0},
    },
    "defensive": {
        "strategy_entry_min_signal_edge_bps": {"lo": 2.5, "hi": 18.0},
        "strategy_entry_alpha_min": {"lo": 0.15, "hi": 0.40},
        "strategy_min_net_edge_bps": {"lo": 3.0, "hi": 14.0},
    },
}


def get_profile_clamps(profile_id: str) -> dict[str, ClampRange]:
    """返回指定 profile 的 clamp 字典。找不到 profile 抛 KeyError。

    用法:
        clamps = get_profile_clamps("trend_normal")
        lo = clamps["strategy_entry_min_signal_edge_bps"]["lo"]
        hi = clamps["strategy_entry_min_signal_edge_bps"]["hi"]
    """
    if profile_id not in PROFILE_CLAMPS:
        raise KeyError(
            f"unknown profile_id: {profile_id!r}; "
            f"known profiles: {sorted(PROFILE_CLAMPS)}"
        )
    return PROFILE_CLAMPS[profile_id]


def clamp_value(profile_id: str, key: str, raw_value: float) -> float:
    """便捷函数:把一个值夹到 profile 的 clamp 区间内。

    seed 层调用:
        from aats.data_platform.research.profile_clamps import clamp_value
        value = clamp_value("trend_normal", "strategy_entry_min_signal_edge_bps", raw)
    """
    clamps = get_profile_clamps(profile_id)
    if key not in clamps:
        raise KeyError(
            f"clamp key {key!r} not defined for profile {profile_id!r}; "
            f"known keys: {sorted(clamps)}"
        )
    rng = clamps[key]
    return max(rng["lo"], min(rng["hi"], raw_value))


def is_in_clamp(profile_id: str, key: str, value: float) -> bool:
    """判断 value 是否在 profile 的 clamp 区间内(闭区间)。"""
    clamps = get_profile_clamps(profile_id)
    rng = clamps[key]
    return rng["lo"] <= value <= rng["hi"]


def clamp_violation_direction(
    profile_id: str, key: str, value: float
) -> str | None:
    """返回 value 越界方向:'above_upper' / 'below_lower' / None(在界内)。

    用于 streak 判断(§1.2)。
    """
    clamps = get_profile_clamps(profile_id)
    rng = clamps[key]
    if value > rng["hi"]:
        return "above_upper"
    if value < rng["lo"]:
        return "below_lower"
    return None
