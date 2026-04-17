#!/usr/bin/env python3
"""[DEPRECATED since batch A — 2026-04-17]

原功能：组合审批 + 自动 apply（draft → approved → active parameter set 写入
        + 审计日志 + 后续重启提示）。
替代路径：两步调用 —
  1. ``POST /rdp/recommendations/{id}/approve``
  2. ``POST /rdp/releases/create``（需携带 ``X-Rdp-Apply-Token``，action=apply）

直接调用将 exit 2。如因 crontab/CI 依赖需过渡，请联系维护者。
"""

from __future__ import annotations

import sys

DEPRECATION_MESSAGE = (
    "此脚本已在 RDP 批次 A 硬化中禁用。\n"
    "原功能：组合审批 recommendation 并自动 apply 到 active parameter set。\n"
    "请改用：两步调用 —\n"
    "  1. POST /rdp/recommendations/{id}/approve\n"
    "  2. POST /rdp/releases/create（需携带 X-Rdp-Apply-Token，action=apply）\n"
    "如需紧急操作员通道，请使用 `scripts/rdp_emit_apply_token.py --action apply` "
    "获取 token 并通过 API 调用。\n"
    "相关文档：docs/task/rdp_hardening_batch_a_detailed_design.md §3"
)

if __name__ == "__main__":
    print(DEPRECATION_MESSAGE, file=sys.stderr)
    sys.exit(2)
