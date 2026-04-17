"""Governance DB 异常体系（RDP 硬化 A-0.3）.

这组异常专门给 governance 写路径（``_db_update_*`` / ``_db_sync_*`` /
``save_release_history``）用，替换掉旧的 "None = DB 不可用" 模糊语义。

设计要点
--------
- 写路径遇到基础设施级别的 DB 不可达 → 抛 :class:`DBUnavailableError`
  （调用方必须回滚内存态并把异常交给 API / 后台任务，严禁悄悄降级到 JSON 写入，
  那是上一次 split-brain 事故的根因）。
- 写路径遇到约束违反（FK / UQ / CHECK） → 抛 :class:`DBConstraintViolation`
  （API 层映射成 422，代表业务规则被违反，不要静默吞掉当成"DB 偶发失败"）。
- CAS 冲突（UPDATE rowcount == 0）不是异常，继续用 ``bool`` 返回值表达：
  ``True`` = 抢到，``False`` = 别人抢先改了。调用方按既有的 "rollback 内存态"
  模式处理，API 层返回 409。

See: docs/task/rdp_hardening_batch_a_detailed_design.md §5
"""

from __future__ import annotations


class GovernanceDBError(Exception):
    """所有 governance DB 操作失败的基类（便于框架层一次 except）."""


class DBUnavailableError(GovernanceDBError):
    """DB 连接不可达：连接失败、driver 超时、``try_governance_db`` 返回 ``(None, False)``.

    调用方：必须回滚内存态，把异常交给上层；严禁降级到 JSON 写入"假装成功"。
    API 层：映射成 HTTP 503。
    """


class DBConstraintViolation(GovernanceDBError):
    """DB 约束违反：FK / UQ / CHECK / NOT NULL.

    通常是上游业务逻辑 bug 或脏数据，不是基础设施问题。API 层映射成 422。
    """


class DBConflictError(GovernanceDBError):
    """CAS 冲突的异常化包装（可选，现阶段 ``_db_update_rec_status`` 仍用 ``bool`` 返回）.

    仅用于调用方希望用 try/except 处理冲突的场景；默认不抛。
    """
