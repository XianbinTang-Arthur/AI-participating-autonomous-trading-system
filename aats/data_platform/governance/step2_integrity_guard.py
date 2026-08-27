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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aats.data_platform.governance._time_util import parse_iso_datetime_utc

logger = logging.getLogger(__name__)


# 固定对外文案：无论具体异常是什么，对用户展示的 reason 都是这一条，避免
# 把内部错误信息（例如 DSN、SQL 片段、文件绝对路径）泄漏出去。
_STEP2_LOOKUP_FAILURE_REASON = (
    "Step2 快照查询失败，无法校验完整性，已按 fail-closed 策略阻止此次操作。"
)

_STEP2_INCOMPLETE_REASON = (
    "Step2 研究快照不完整，当前轮次不能据此做正式审批。"
)

_STEP2_STATUS_REASON = (
    "Step2 研究快照未成功完成，当前轮次不能据此做正式审批。"
)

_STEP2_MANIFEST_REASON = (
    "Step2 研究快照的 manifest 缺失或与轮次不一致，当前轮次不能据此做正式审批。"
)

_STEP2_FINISHED_AT_REASON = (
    "Step2 研究快照的完成时间缺失、无效或位于未来，当前轮次不能据此做正式审批。"
)

_MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)

# 当 governance 库和磁盘都没有任何 Step2 round 时（fresh deploy / 迁库丢数据 /
# 研究目录被清），``load_latest_research_round_snapshot`` 返回 ``None``。这时
# 必须显式阻塞：``is_snapshot_incomplete(None)`` 会返回 ``False``（因为它被
# 其它消费者共用，保持该契约），所以这里不能把 None 交给它判断——否则 gate
# 在最该锁死的时刻（完全没有证据）反而放行，破坏 fail-closed 语义。
_STEP2_MISSING_REASON = (
    "Step2 研究快照不存在，无可用证据，已按 fail-closed 策略阻止此次操作。"
)


def assess_step2_integrity(project_root: Path) -> dict[str, object]:
    """Return the shared Step2 integrity decision used by UI and write APIs."""
    try:
        from aats.data_platform.governance.snapshot_db import (
            ROUND_PHASE_STEP2,
            is_snapshot_incomplete,
            load_latest_research_round_snapshot,
        )

        snapshot = load_latest_research_round_snapshot(
            phase=ROUND_PHASE_STEP2,
            project_root=project_root,
            require_managed_db_truth=True,
        )
    except Exception:
        logger.exception("step2 integrity check failed to load snapshot")
        return {
            "ok": False,
            "code": "lookup_failed",
            "reason": _STEP2_LOOKUP_FAILURE_REASON,
        }

    if snapshot is None:
        return {
            "ok": False,
            "code": "snapshot_missing",
            "reason": _STEP2_MISSING_REASON,
        }
    if is_snapshot_incomplete(snapshot):
        return {
            "ok": False,
            "code": "manifest_missing_on_disk",
            "reason": _STEP2_INCOMPLETE_REASON,
        }
    if snapshot.get("status") != "succeeded":
        return {
            "ok": False,
            "code": "snapshot_status_invalid",
            "reason": _STEP2_STATUS_REASON,
        }
    manifest = snapshot.get("manifest")
    round_id = snapshot.get("round_id")
    if (
        type(manifest) is not dict
        or not manifest
        or not isinstance(round_id, str)
        or not round_id.strip()
        or manifest.get("round_id") != round_id
    ):
        return {
            "ok": False,
            "code": "snapshot_manifest_invalid",
            "reason": _STEP2_MANIFEST_REASON,
        }
    finished_at_raw = snapshot.get("finished_at")
    try:
        finished_at = parse_iso_datetime_utc(
            finished_at_raw,
            context="step2_integrity_guard.finished_at",
        )
    except (TypeError, ValueError):
        finished_at = None
    if (
        finished_at is None
        or finished_at > datetime.now(timezone.utc) + _MAX_FUTURE_CLOCK_SKEW
    ):
        return {
            "ok": False,
            "code": "snapshot_finished_at_invalid",
            "reason": _STEP2_FINISHED_AT_REASON,
        }
    return {"ok": True, "code": None, "reason": None}


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
    assessment = assess_step2_integrity(project_root)
    return None if assessment["ok"] else str(assessment["reason"])


__all__ = [
    "assess_step2_integrity",
    "step2_integrity_blocking_reason",
]
