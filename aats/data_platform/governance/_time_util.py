"""统一的 ISO 时间解析工具（RDP 硬化 A-0.4）.

所有 governance / production_workflow / operations / api.rdp_* 模块解析 ISO
字符串必须用 :func:`parse_iso_datetime_utc`，不允许直接 ``datetime.fromisoformat()``
或自定义 ``_parse_iso_datetime`` helper。

设计原则:
  - 输入 ``None`` / 空串 → 返回 ``None``（调用方决定如何处理缺失）
  - 输入非法格式 → 抛 ``ValueError``（不静默吞；上一次 P0 bug 就是静默吞导致 gate 被绕过）
  - 输入 naive 字符串 → 视为 UTC（加 tzinfo），不抛错但记 WARN log（发现即暴露）
  - 输入 tz-aware → 转换为 UTC

See: docs/task/rdp_hardening_batch_a_detailed_design.md §6
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def parse_iso_datetime_utc(
    value: Optional[str | datetime],
    *,
    context: str = "",
) -> Optional[datetime]:
    """解析 ISO-8601 字符串为 UTC tz-aware ``datetime``.

    Args:
      value: ISO-8601 字符串，或已经是 ``datetime`` 对象；``None`` / 空串直接返回 ``None``。
      context: 出错时写入异常/日志的上下文标签，例如 ``"gate_rules.created_at"``。

    Returns:
      tz-aware UTC ``datetime``，或 ``None``（输入缺失时）。

    Raises:
      ValueError: 输入字符串不是合法 ISO-8601 格式（含原值 + context）。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            log.warning(
                "parse_iso_datetime_utc: naive datetime object at %s — assuming UTC",
                context or "<unknown>",
            )
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    s = str(value).strip()
    if not s:
        return None

    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(
            f"illegal_iso_datetime:{value!r} at {context or '<unknown>'}"
        ) from exc

    if dt.tzinfo is None:
        log.warning(
            "parse_iso_datetime_utc: naive datetime %r at %s — assuming UTC",
            value,
            context or "<unknown>",
        )
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt
