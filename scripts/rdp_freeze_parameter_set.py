#!/usr/bin/env python3
"""[DEPRECATED since batch A — 2026-04-17]

原功能：管理 parameter registry（导入候选、冻结 candidate → frozen、
        废弃过期 parameter set）。
替代路径：**暂未 API 化**。此脚本原本的冻结 / 废弃 / 候选导入均为治理写动作，
        批次 A 已断开直连 DB 通道；对应 API 端点规划在后续批次补齐
        （见 rdp_full_hardening_sow.md）。只读查询请用
        ``GET /rdp/parameters/active``。

直接调用将 exit 2。如运维场景确需手动冻结或导入候选，请联系维护者走
DB 维护路径；如因 crontab/CI 依赖需过渡，请联系维护者。
"""

from __future__ import annotations

import sys

DEPRECATION_MESSAGE = (
    "此脚本已在 RDP 批次 A 硬化中禁用。\n"
    "原功能：管理 parameter registry，冻结/废弃 parameter set。\n"
    "目前这些写动作暂未 API 化（规划在后续批次补齐）；"
    "只读查询请用 GET /rdp/parameters/active。\n"
    "如运维场景确需手动冻结或导入候选，请联系维护者走 DB 维护路径。\n"
    "相关文档：docs/task/rdp_hardening_batch_a_detailed_design.md §3"
)

if __name__ == "__main__":
    print(DEPRECATION_MESSAGE, file=sys.stderr)
    sys.exit(2)
