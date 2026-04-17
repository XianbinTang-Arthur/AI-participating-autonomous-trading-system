#!/usr/bin/env python3
"""[DEPRECATED since batch A — 2026-04-17]

原功能：手动管理 recommendation 状态（approve/reject/supersede）和
        active decision registry（set-active / deprecate 等）。
替代路径：
  - 审批 recommendation：``POST /rdp/recommendations/{id}/approve``
    （或 reject / supersede 对应端点）
  - 决策注册表更新：``POST /rdp/decisions/update``
  - 只读查询：``GET /rdp/recommendations``、``GET /rdp/decisions``

直接调用将 exit 2。如因 crontab/CI 依赖需过渡，请联系维护者。
"""

from __future__ import annotations

import sys

DEPRECATION_MESSAGE = (
    "此脚本已在 RDP 批次 A 硬化中禁用。\n"
    "原功能：手动管理 recommendation 状态与 active decision registry。\n"
    "请改用：\n"
    "  - 审批 recommendation：POST /rdp/recommendations/{id}/approve"
    "（或 reject / supersede 对应端点）\n"
    "  - 决策注册表更新：POST /rdp/decisions/update\n"
    "  - 只读查询：GET /rdp/recommendations、GET /rdp/decisions\n"
    "如需紧急操作员通道，请使用 `scripts/rdp_emit_apply_token.py` 获取 token "
    "并通过 API 调用。\n"
    "相关文档：docs/task/rdp_hardening_batch_a_detailed_design.md §3"
)

if __name__ == "__main__":
    print(DEPRECATION_MESSAGE, file=sys.stderr)
    sys.exit(2)
