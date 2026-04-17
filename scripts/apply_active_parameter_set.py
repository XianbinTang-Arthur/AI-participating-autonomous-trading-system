#!/usr/bin/env python3
"""[DEPRECATED since batch A — 2026-04-17]

原功能：从治理层 registry 选取参数并直写 ``active_parameter_sets``
        （包括一条绕过 gate 的批量 frozen 应用动作——该动作已物理删除，
        详见 A-0.6）。
替代路径：``POST /rdp/parameters/apply``（需携带 ``X-Rdp-Apply-Token``，
        action=apply）。只读查询请用 ``GET /rdp/parameters/active``。

直接调用将 exit 2。如因 crontab/CI 依赖需过渡，请联系维护者。

A-0.6 注记：本脚本此前提供的批量绕过 gate 入口已被整体删除，API 层对所有
写操作强制 token + gate。
"""

from __future__ import annotations

import sys

DEPRECATION_MESSAGE = (
    "此脚本已在 RDP 批次 A 硬化中禁用。\n"
    "原功能：直写 active_parameter_sets（含已物理删除的批量绕 gate 动作）。\n"
    "请改用：POST /rdp/parameters/apply（需携带 X-Rdp-Apply-Token，"
    "action=apply）；只读查询请用 GET /rdp/parameters/active。\n"
    "如需紧急操作员通道，请使用 `scripts/rdp_emit_apply_token.py --action apply` "
    "获取 token 并通过 API 调用。\n"
    "相关文档：docs/task/rdp_hardening_batch_a_detailed_design.md §3"
)

if __name__ == "__main__":
    print(DEPRECATION_MESSAGE, file=sys.stderr)
    sys.exit(2)
