"""Parameter grid: define and iterate parameter combinations.

Phase 2 设计决策 §11：
- 小规模参数网格
- 以解释性为优先
- 不做复杂搜索算法或黑盒优化器

首批冻结 3 个参数（§15）：
- min_confirm_ticks: [2, 3, 4]
- score_stability_threshold: [2.0, 5.0, 10.0]
- min_safe_net_edge_bps: [5, 10, 15]
"""

from __future__ import annotations

import itertools
from typing import Any

from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides


# ---------------------------------------------------------------------------
# 默认参数网格（Phase 2 首批）
# ---------------------------------------------------------------------------

DEFAULT_PARAMETER_GRID: dict[str, list[Any]] = {
    "min_confirm_ticks": [2, 3, 4],
    "score_stability_threshold": [2.0, 5.0, 10.0],
    "min_safe_net_edge_bps": [5, 10, 15],
}


def build_grid(
    grid: dict[str, list[Any]] | None = None,
) -> list[ReplayParameterOverrides]:
    """从参数网格生成所有组合的 ReplayParameterOverrides 列表。

    示例：
        grid = {
            "min_confirm_ticks": [2, 3],
            "min_safe_net_edge_bps": [5, 10],
        }
        -> 4 个组合: (2,5), (2,10), (3,5), (3,10)
    """
    if grid is None:
        grid = DEFAULT_PARAMETER_GRID

    keys = list(grid.keys())
    values = list(grid.values())
    combos = list(itertools.product(*values))

    overrides: list[ReplayParameterOverrides] = []
    for combo in combos:
        param_dict = dict(zip(keys, combo))
        overrides.append(ReplayParameterOverrides.from_dict(param_dict))

    return overrides


def grid_to_json(grid: dict[str, list[Any]] | None = None) -> dict[str, list[Any]]:
    """返回可 JSON 序列化的参数网格字典。"""
    if grid is None:
        grid = DEFAULT_PARAMETER_GRID
    return {k: [_jsonable(v) for v in vs] for k, vs in grid.items()}


def combo_label(params: ReplayParameterOverrides) -> str:
    """生成人可读的参数组合标签。"""
    d = params.to_dict()
    parts = [f"{k}={v}" for k, v in sorted(d.items()) if k != "extra"]
    return "__".join(parts) if parts else "default"


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return v
    if isinstance(v, int):
        return v
    return str(v)
