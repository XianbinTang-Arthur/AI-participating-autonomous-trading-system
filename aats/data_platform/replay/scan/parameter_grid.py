"""Parameter grid: define and iterate parameter combinations.

Phase 2 设计决策 §11：
- 小规模参数网格
- 以解释性为优先
- 不做复杂搜索算法或黑盒优化器

参数网格维度:
  首批（§15）:
    - min_confirm_ticks: [2, 3, 4]
    - score_stability_threshold: [2.0, 5.0, 10.0]
    - min_safe_net_edge_bps: [5, 10, 15]

  扩展（修复参数衰减问题）:
    - entry_threshold: [0.25, 0.40, 0.55]
    - close_threshold: [0.10, 0.20, 0.30]

  理论组合数: 3^5 = 243, 约束过滤后实际约 216
  (close_threshold > entry_threshold 的组合被 ReplayParameterOverrides 拒绝)

注意:
  其余参数（min_hold_seconds, de_risk_net_edge_bps, cost buffers 等）
  由 Phase 2 Step 2 研究直接推荐，通过 Step 3 合并后全量导入治理层，
  无需在网格中穷举。
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认参数网格
# ---------------------------------------------------------------------------

DEFAULT_PARAMETER_GRID: dict[str, list[Any]] = {
    # 首批参数（信号确认 + 稳定性 + 安全边际）
    "min_confirm_ticks": [2, 3, 4],
    "score_stability_threshold": [2.0, 5.0, 10.0],
    "min_safe_net_edge_bps": [5, 10, 15],
    # 扩展参数（进出场阈值 — P0 影响因子）
    # entry_threshold 生产端默认 0.66, 但 RDP 研究普遍推荐 0.20~0.35
    # close_threshold 必须 <= entry_threshold (由 ReplayParameterOverrides 约束)
    "entry_threshold": [0.25, 0.40, 0.55],
    "close_threshold": [0.10, 0.20, 0.30],
}


def build_grid(
    grid: dict[str, list[Any]] | None = None,
    *,
    base_params: dict[str, Any] | None = None,
) -> list[ReplayParameterOverrides]:
    """从参数网格生成所有组合的 ReplayParameterOverrides 列表。

    自动过滤违反 ReplayParameterOverrides 约束的组合
    （如 close_threshold > entry_threshold）。

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
    skipped = 0
    for combo in combos:
        param_dict = {
            **(base_params or {}),
            **dict(zip(keys, combo)),
        }
        try:
            overrides.append(ReplayParameterOverrides.from_dict(param_dict))
        except (ValueError, TypeError):
            # 约束冲突（如 close_threshold > entry_threshold），跳过此组合
            skipped += 1
            continue

    if skipped > 0:
        log.info(
            "参数网格: %d 个组合通过, %d 个因约束冲突跳过",
            len(overrides), skipped,
        )

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
