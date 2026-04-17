"""Step2 research snapshot integrity gate — shared guard.

这是 RDP 所有写入路径（approve / supersede / tuning review）上游共用的
完整性门闸。前端把 UI action 的 enabled 标为 False 只是装饰，任何绕过 UI
的调用（脚本 / curl / 重放）都能把 blocked 决策推进去，所以必须在 server
端再做一次同样的 gate。

两条语义不能偏离：
- **fail-closed**：查询本身失败时（governance DB 不可达 / 加载异常），返回
  固定文案，不把 ``str(exc)`` 塞进面向用户的响应，避免泄漏 DSN / 文件路径
  / SQL 片段等内部细节。具体异常通过 ``logger.exception`` 写日志，由运维
  按堆栈定位。
- **单一真源**：原本 ``rdp_routes.py`` 和 ``strategy_tuning_registry.py``
  各自维护一份几乎字符级相同的拷贝，任一侧漏改就会让两层门闸漂移。现在都
  指向本模块，保证 approve / supersede / tuning review 的裁决一致。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# 固定对外文案：无论具体异常是什么，对用户展示的 reason 都是这一条，避免
# 把内部错误信息（例如 DSN、SQL 片段、文件绝对路径）泄漏出去。
_STEP2_LOOKUP_FAILURE_REASON = (
    "Step2 快照查询失败，无法校验完整性，已按 fail-closed 策略阻止此次操作。"
)

_STEP2_INCOMPLETE_REASON = (
    "Step2 研究快照不完整，当前轮次不能据此做正式审批。"
)


def step2_integrity_blocking_reason(project_root: Path) -> str | None:
    """Return a blocking reason string, or ``None`` if Step2 is healthy.

    Parameters
    ----------
    project_root:
        Project root used by ``load_latest_research_round_snapshot`` to
        locate the research rounds directory.

    Returns
    -------
    str | None
        - ``None`` — Step2 snapshot present and complete → proceed.
        - non-empty ``str`` — user-safe blocking reason. The caller should
          surface this verbatim; it never contains exception detail.

    Notes
    -----
    When the underlying snapshot lookup raises, this logs the exception
    and returns the fixed ``_STEP2_LOOKUP_FAILURE_REASON`` message. It
    deliberately does **not** include ``str(exc)`` in the return value —
    that string flows all the way to the dashboard response body.
    """
    try:
        from aats.data_platform.governance.snapshot_db import (
            ROUND_PHASE_STEP2,
            is_snapshot_incomplete,
            load_latest_research_round_snapshot,
        )

        snapshot = load_latest_research_round_snapshot(
            phase=ROUND_PHASE_STEP2,
            project_root=project_root,
        )
    except Exception:
        logger.exception("step2 integrity check failed to load snapshot")
        return _STEP2_LOOKUP_FAILURE_REASON

    if is_snapshot_incomplete(snapshot):
        return _STEP2_INCOMPLETE_REASON
    return None


__all__ = [
    "step2_integrity_blocking_reason",
]
