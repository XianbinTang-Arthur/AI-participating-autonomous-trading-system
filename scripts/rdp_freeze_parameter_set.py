#!/usr/bin/env python3
"""[DEPRECATED since batch A — 2026-04-17]

原功能：管理 parameter registry（导入候选、冻结 candidate → frozen、
        废弃过期 parameter set）。
替代路径：``POST /rdp/parameters/freeze``（需携带 ``X-Rdp-Apply-Token``，
        action=freeze）。只读查询请用 ``GET /rdp/parameters``。

直接调用将 exit 2。如因 crontab/CI 依赖需过渡，请联系维护者。
"""

from __future__ import annotations

import sys

DEPRECATION_MESSAGE = (
    "此脚本已在 RDP 批次 A 硬化中禁用。\n"
    "原功能：管理 parameter registry，冻结/废弃 parameter set。\n"
    "请改用：POST /rdp/parameters/freeze（需携带 X-Rdp-Apply-Token，"
    "action=freeze）；只读查询请用 GET /rdp/parameters。\n"
    "如需紧急操作员通道，请使用 `scripts/rdp_emit_apply_token.py --action freeze` "
    "获取 token 并通过 API 调用。\n"
    "相关文档：docs/task/rdp_hardening_batch_a_detailed_design.md §3"
)

if __name__ == "__main__":
    print(DEPRECATION_MESSAGE, file=sys.stderr)
    sys.exit(2)
