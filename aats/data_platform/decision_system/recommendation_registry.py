"""Phase 6-E: Recommendation / Decision Registry 管理.

让 recommendation 成为受治理对象：
  - recommendation_registry.json: 所有历史建议
  - active_decision_registry.json: 当前 family/tf 运营状态
  - evidence_bundle_index.json: evidence bundle 引用索引

数据存储策略:
  - 受管环境以 DB 为唯一真源；DB 不可用或查询失败时拒绝陈旧文件回退
  - 文件只作为审计副本，以及明确离线开发模式的兼容读源
  - DB 配置入口: AATS_ACTIVE_PARAMETER_DB_URL / RDP_DATABASE_URL
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._db_util import (
    has_explicit_governance_db_configuration,
    try_governance_db,
)
from aats.data_platform.governance._exceptions import (
    DBConflictError,
    DBConstraintViolation,
    DBUnavailableError,
)
from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)

log = logging.getLogger(__name__)

_STORAGE_MODE_FIELD = "_governance_storage_mode"
_STORAGE_MODE_MANAGED_DB = "managed_db"
_STORAGE_MODE_OFFLINE = "offline"
_MISSING_FIELD = object()


def _snapshot_fields(
    record: dict[str, Any],
    field_names: tuple[str, ...],
) -> dict[str, Any]:
    return {
        field_name: record[field_name] if field_name in record else _MISSING_FIELD
        for field_name in field_names
    }


def _restore_fields(record: dict[str, Any], snapshot: dict[str, Any]) -> None:
    for field_name, value in snapshot.items():
        if value is _MISSING_FIELD:
            record.pop(field_name, None)
        else:
            record[field_name] = value


class RecommendationRegistryCASConflict(RuntimeError):
    """JSON 审计镜像的 version 基线已被另一 writer 推进。"""


# ── DB 辅助 ──────────────────────────────────────────────────────────

def _db_sync_recommendation(rec: dict[str, Any]) -> None:
    """将单个 recommendation dict 同步到 governance DB.

    A-0.3 硬纪律：DB 是真源，文件只是审计副本。DB 不可达或约束违反一律抛
    异常交给上层，不允许悄悄降级到 "仅写文件" —— 那是上一次 split-brain 事故
    的根因（approved 记录仅出现在 JSON，DB 里没落库，后续流程读 DB 就看不到）。

    Raises
    ------
    DBUnavailableError
        ``try_governance_db`` 失败或执行时连接挂了，基础设施级问题。
    DBConstraintViolation
        IntegrityError（FK / UQ / CHECK）；一般是上游脏数据或 schema bug。
    """
    from sqlalchemy.exc import IntegrityError, OperationalError
    from sqlalchemy.orm import Session

    from aats.data_platform.governance.recommendations_db import db_upsert_recommendation

    engine, ok = try_governance_db()
    if not ok:
        raise DBUnavailableError(
            f"governance DB unavailable while syncing recommendation {rec.get('recommendation_id')!r}"
        )
    try:
        with Session(engine) as session, session.begin():
            db_upsert_recommendation(
                session,
                recommendation_id=rec["recommendation_id"],
                family=rec["family"],
                timeframe=rec["timeframe"],
                recommendation_type=rec.get("recommendation_type", "require_review"),
                confidence=rec.get("confidence", "low"),
                reason=rec.get("reason", ""),
                symbol=rec.get("symbol", "BTC-USDT-SWAP"),
                target_parameter_set_id=rec.get("target_parameter_set_id"),
                source_round_id=rec.get("source_round_id"),
                evidence_bundle_ref=rec.get("evidence_bundle_ref"),
                status=rec.get("status", "draft"),
                approved_by=rec.get("approved_by"),
                approved_at=rec.get("approved_at"),
                review_notes=rec.get("review_notes"),
                rejected_by=rec.get("rejected_by"),
                rejected_at=rec.get("rejected_at"),
                superseded_by=rec.get("superseded_by"),
                superseded_at=rec.get("superseded_at"),
                superseded_by_recommendation_id=rec.get("superseded_by_recommendation_id"),
                created_at=rec.get("created_at"),
            )
    except IntegrityError as exc:
        raise DBConstraintViolation(
            "recommendation_write_constraint_violation: "
            f"{rec.get('recommendation_id')!r}"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            "recommendation_write_db_unavailable: "
            f"{rec.get('recommendation_id')!r}"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()


def _db_add_recommendation_atomic(rec: dict[str, Any]) -> dict[str, Any]:
    """Commit one draft replacement and return the canonical DB snapshot."""

    from sqlalchemy.exc import IntegrityError, OperationalError
    from sqlalchemy.orm import Session

    from aats.data_platform.governance.recommendations_db import (
        db_insert_recommendation_superseding_drafts,
        db_load_recommendation_registry,
    )

    engine, ok = try_governance_db()
    if not ok or engine is None:
        raise DBUnavailableError(
            "governance DB unavailable while atomically adding recommendation"
        )
    try:
        with Session(engine) as session, session.begin():
            db_insert_recommendation_superseding_drafts(
                session,
                recommendation=rec,
            )
            canonical = db_load_recommendation_registry(session)
            if not isinstance(canonical.get("recommendations"), list):
                raise DBConstraintViolation(
                    "recommendation_atomic_insert_readback_invalid"
                )
            return canonical
    except DBConflictError:
        raise
    except IntegrityError as exc:
        raise DBConstraintViolation(
            "recommendation_atomic_insert_constraint_violation"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            "recommendation_atomic_insert_db_unavailable"
        ) from exc
    finally:
        engine.dispose()


def _db_update_rec_status(
    rec: dict[str, Any],
    *,
    expected_current_status: str | tuple[str, ...] | None = None,
) -> bool:
    """将 recommendation 状态变更同步到 DB.

    A-0.3 清扫后不再返回 ``None``：DB 不可达一律抛 :class:`DBUnavailableError`，
    调用方必须显式回滚内存态、把异常抛给 API 层（503）或后台任务。

    Returns
    -------
    True
        UPDATE 生效（rowcount > 0）。
    False
        CAS 冲突：UPDATE 没命中预期前置状态（另一 operator 已经抢先改写）。
        调用方回滚内存本次改动即可，不当作异常。

    Raises
    ------
    DBUnavailableError
        ``try_governance_db`` 失败或运行时连接异常（基础设施问题）。
    DBConstraintViolation
        IntegrityError（FK / UQ / CHECK）。

    Notes
    -----
    每次状态写入都会绑定 recommendation 的完整业务身份；调用方再传
    ``expected_current_status`` 时，同时获得状态与身份双重 CAS。任何同 ID 行
    替换、证据引用变化或并发状态变化都会返回 ``False``，由调用方回滚内存态。
    """
    from sqlalchemy.exc import IntegrityError, OperationalError
    from sqlalchemy.orm import Session

    from aats.data_platform.governance.recommendations_db import db_update_recommendation_status

    engine, ok = try_governance_db()
    if not ok:
        raise DBUnavailableError(
            f"governance DB unavailable while updating recommendation {rec.get('recommendation_id')!r}"
        )
    try:
        with Session(engine) as session, session.begin():
            return db_update_recommendation_status(
                session,
                rec["recommendation_id"],
                status=rec["status"],
                approved_by=rec.get("approved_by"),
                approved_at=rec.get("approved_at"),
                review_notes=rec.get("review_notes"),
                rejected_by=rec.get("rejected_by"),
                rejected_at=rec.get("rejected_at"),
                superseded_by=rec.get("superseded_by"),
                superseded_at=rec.get("superseded_at"),
                superseded_by_recommendation_id=rec.get("superseded_by_recommendation_id"),
                expected_current_status=expected_current_status,
                expected_identity={
                    "family": rec.get("family"),
                    "symbol": rec.get("symbol"),
                    "timeframe": rec.get("timeframe"),
                    "recommendation_type": rec.get("recommendation_type"),
                    "target_parameter_set_id": rec.get("target_parameter_set_id"),
                    "source_round_id": rec.get("source_round_id"),
                    "confidence": rec.get("confidence"),
                    "reason": rec.get("reason"),
                    "evidence_bundle_ref": rec.get("evidence_bundle_ref"),
                },
            )
    except IntegrityError as exc:
        raise DBConstraintViolation(
            "recommendation_status_constraint_violation: "
            f"{rec.get('recommendation_id')!r}"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            "recommendation_status_db_unavailable: "
            f"{rec.get('recommendation_id')!r}"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()


def _db_sync_active_decision(
    family: str, timeframe: str, current_status: str,
    active_parameter_set_id: str | None = None,
    preserve_existing_active_parameter_set: bool = False,
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """将 active decision UPSERT 同步到 DB.

    A-0.3：不再吞异常。DB 不可达或约束违反一律抛给上层。
    """
    from sqlalchemy.exc import IntegrityError, OperationalError
    from sqlalchemy.orm import Session

    from aats.data_platform.governance.recommendations_db import db_upsert_active_decision

    engine, ok = try_governance_db()
    if not ok:
        raise DBUnavailableError(
            f"governance DB unavailable while syncing active_decision {family}/{timeframe}"
        )
    try:
        with Session(engine) as session, session.begin():
            updated = db_upsert_active_decision(
                session,
                family=family, timeframe=timeframe,
                current_status=current_status,
                active_parameter_set_id=active_parameter_set_id,
                preserve_existing_active_parameter_set=(
                    preserve_existing_active_parameter_set
                ),
                last_recommendation_id=last_recommendation_id,
                notes=notes,
            )
            if updated is not True:
                raise DBConstraintViolation(
                    f"active_decision {family}/{timeframe} safety pause is sticky; "
                    "automatic overwrite rejected"
                )
    except IntegrityError as exc:
        raise DBConstraintViolation(
            f"active_decision_constraint_violation: {family}/{timeframe}"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            f"active_decision_db_unavailable: {family}/{timeframe}"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()

# ── Recommendation 状态 ──────────────────────────────────────────────

RECOMMENDATION_STATUSES = ("draft", "approved", "rejected", "superseded")

RECOMMENDATION_TYPES = (
    "parameter_upgrade",
    "keep_active",
    "lower_priority",
    "pause",
    "require_review",
)

# RDP Bug 5 语义分离: status=approved 在不同 recommendation_type 下语义不同。
#
#   parameter_upgrade + approved:  "待 apply 或已 apply 为 live"
#       - 经 release_cycle 触发 apply，写入 active_parameter_sets
#       - 同 combo 下旧 approved 在新 apply 成功后被 superseded (见 Bug 2)
#
#   keep_active + approved:         "本轮 decision_round 的结论是保持现状"
#       - 不涉及参数变更，不进 release_cycle
#       - 是 decision_round 产出的 "证据记录"，不是"可执行提案"
#
#   lower_priority / pause / require_review + approved:
#       类似 keep_active，仅记录决策结论
#
# UI/监控/查询层应按 recommendation_type 过滤，而非单纯按 status。
# 下面两个常量提供语义分离：
_APPLY_TRIGGERING_TYPES = frozenset({"parameter_upgrade"})
_INFORMATIONAL_TYPES = frozenset({"keep_active", "lower_priority", "pause", "require_review"})


def is_apply_triggering_type(recommendation_type: str) -> bool:
    """判断某 recommendation_type 是否会触发 parameter apply（即可实际改变 live 参数）。

    parameter_upgrade → True（approved 后进 release_cycle）
    其他（keep_active 等） → False（approved 仅代表决策结论已确认）
    """
    return str(recommendation_type) in _APPLY_TRIGGERING_TYPES


def count_live_approvals(recommendations: list[dict]) -> dict[str, int]:
    """按类型统计"活着"的 approved recommendations.

    Returns a dict with keys:
      - ``apply_triggering_live``: parameter_upgrade approved 且 superseded_by=None
          （这些才是 UI 应该展示的 "ready/live" 条目）
      - ``informational``: 非 parameter_upgrade 的 approved
          （keep_active 等决策结论，不需要 apply）
      - ``draft``: 所有 status=draft
      - ``superseded``: 所有 status=superseded
      - ``rejected``: 所有 status=rejected

    这个 helper 给 operator UI / 监控查询用，避免 "SELECT COUNT(*)
    WHERE status='approved'" 把 informational 和 live 混成一个数字。
    """
    result = {
        "apply_triggering_live": 0,
        "informational": 0,
        "draft": 0,
        "superseded": 0,
        "rejected": 0,
    }
    for rec in recommendations:
        status = rec.get("status")
        if status == "draft":
            result["draft"] += 1
        elif status == "rejected":
            result["rejected"] += 1
        elif status == "superseded":
            result["superseded"] += 1
        elif status == "approved":
            rec_type = rec.get("recommendation_type", "")
            if is_apply_triggering_type(rec_type) and not rec.get("superseded_by"):
                result["apply_triggering_live"] += 1
            else:
                result["informational"] += 1
    return result


# ── ID 生成 ──────────────────────────────────────────────────────────


def _make_recommendation_id() -> str:
    return f"rec_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


# ── Recommendation Registry ──────────────────────────────────────────


def _read_disk_base_version(path: pathlib.Path) -> int:
    """读审计副本文件里的 version 字段。文件不存在/损坏都返回 0。

    专门服务 DB-first 路径：DB 加载不带 version（DB 没这个字段），但审计副本
    文件自己维护 version 计数器用于 CAS。必须把磁盘 version 戳到内存 registry
    上，否则下一次 ``save_recommendation_registry`` 会误报 CAS 冲突
    （磁盘=N>0，内存 base=0）把 HTTP 500 抛回 UI——此时 DB 写入其实已经成功。
    """
    if not path.exists():
        return 0
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    raw = data.get("version", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def load_recommendation_registry(path: pathlib.Path, *, skip_db: bool = False) -> dict[str, Any]:
    """加载 recommendation registry.

    受管环境以 DB 为唯一真源，DB 不可用或查询失败时拒绝过期 JSON 回退。
    仅明确的离线文件模式（包括 ``skip_db=True``）允许读取文件或空 registry。
    """
    if not skip_db:
        try:
            project_root = path.resolve(strict=False).parents[2]
        except (IndexError, OSError, RuntimeError, ValueError):
            project_root = None
        db_is_managed_truth = has_explicit_governance_db_configuration(project_root)
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.recommendations_db import db_load_recommendation_registry

                with Session(engine) as session:
                    registry = db_load_recommendation_registry(session)
                log.info("从数据库加载 recommendation registry (%d recommendations)",
                         len(registry.get("recommendations", [])))
                # DB 查询成功后，空表也是权威结果。不得让 JSON 审计副本复活已从
                # DB 删除的 recommendation。version 仅用于随后刷新审计副本的 CAS。
                registry["version"] = _read_disk_base_version(path)
                registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_MANAGED_DB
                return registry
            except Exception as exc:
                if db_is_managed_truth:
                    raise DBUnavailableError(
                        "governance DB recommendation read failed; stale JSON fallback denied"
                    ) from exc
                log.warning(
                    "recommendation_registry: 离线开发 DB 读取失败 (%s)，"
                    "fallback 到文件（stale）",
                    type(exc).__name__,
                )
            finally:
                if engine is not None:
                    engine.dispose()
        elif db_is_managed_truth:
            raise DBUnavailableError(
                "governance DB unavailable; stale recommendation JSON fallback denied"
            )

    if not path.exists():
        return {
            "generated_at": None,
            "recommendations": [],
            _STORAGE_MODE_FIELD: _STORAGE_MODE_OFFLINE,
        }
    with path.open(encoding="utf-8") as f:
        registry = json.load(f)
    if not isinstance(registry, dict):
        raise ValueError("recommendation registry must be a JSON object")
    registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_OFFLINE
    return registry


def save_recommendation_registry(
    registry: dict[str, Any],
    path: pathlib.Path,
    *,
    fail_on_cas_read_error: bool = False,
) -> None:
    """原子写 JSON-only recommendation registry.

    单机兼容（DB 不可达）路径下没有事务保护；两个脚本并发做
    load → mutate → save 会让后写方静默吞掉前写方的变更。这里在落盘前读
    磁盘当前 version，与内存 registry 的 base version 比对：
      * 一致 → 正常 bump + 落盘
      * 不一致（base=N，磁盘=N+k，k>0）→ 抛 RuntimeError，把并发冲突显式
        抛给 caller，迫使它重跑 load → mutate 序列。

    ``fail_on_cas_read_error`` 仅供 DB 已经提交后的审计镜像刷新使用。该模式下，
    旧镜像无法读取或 JSON 结构已损坏时不冒险覆盖，而是把失败交给上层标记
    degraded。默认值保留历史的离线纯文件语义。

    注意：这不是严格 CAS，因为 read+write 之间仍有 TOCTOU 窗口——要做到
    强序列化需要 DB 事务或文件级锁。但该检查能捕获最常见的 human-sequential
    竞态（operator 手动跑两遍脚本），相比原先的 silent clobber 是显著改善。
    """
    from aats.data_platform.governance._atomic_io import atomic_json_write

    expected_base_version = int(registry.get("version", 0))
    if path.exists():
        try:
            with path.open(encoding="utf-8") as fh:
                on_disk = json.load(fh)
        except (OSError, ValueError) as exc:
            if fail_on_cas_read_error:
                raise RuntimeError(
                    "recommendation_registry 审计镜像基础副本不可读；"
                    "拒绝跳过 CAS 覆盖"
                ) from exc
            log.warning(
                "recommendation_registry: CAS 读磁盘 version 失败 (%s)，"
                "跳过 CAS 按 base_version=%d 继续；磁盘可能被外部进程损坏",
                exc, expected_base_version,
            )
        else:
            if not isinstance(on_disk, dict) and fail_on_cas_read_error:
                raise RuntimeError(
                    "recommendation_registry 审计镜像必须是 JSON object；"
                    "拒绝覆盖损坏副本"
                )
            disk_version = (
                int(on_disk.get("version", 0)) if isinstance(on_disk, dict) else 0
            )
            if disk_version != expected_base_version:
                raise RecommendationRegistryCASConflict(
                    f"recommendation_registry CAS 冲突：磁盘 version={disk_version}，"
                    f"内存 base_version={expected_base_version}；"
                    "另一操作已抢先写入，请重新 load→mutate→save"
                )

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    registry["version"] = expected_base_version + 1
    # Storage routing is request-local metadata.  Persisting it would let a
    # copied audit mirror dictate the next process' trust mode, so the disk
    # payload deliberately excludes it.
    disk_payload = {
        key: value
        for key, value in registry.items()
        if key != _STORAGE_MODE_FIELD
    }
    atomic_json_write(disk_payload, path)
    log.info("保存 recommendation registry -> %s (v%d, %d items)",
             path, registry["version"], len(registry.get("recommendations", [])))


def _load_canonical_recommendation_registry_for_audit_mirror(
    path: pathlib.Path,
) -> dict[str, Any]:
    """从 governance DB 重读完整 recommendation 真值，禁止文件 fallback。"""
    from sqlalchemy.orm import Session

    from aats.data_platform.governance.recommendations_db import (
        db_load_recommendation_registry,
    )

    engine, ok = try_governance_db()
    if not ok:
        raise DBUnavailableError(
            "governance DB unavailable during recommendation audit-mirror readback"
        )
    try:
        with Session(engine) as session:
            registry = db_load_recommendation_registry(session)
    finally:
        if engine is not None:
            engine.dispose()

    if not isinstance(registry, dict) or not isinstance(
        registry.get("recommendations"), list,
    ):
        raise DBConstraintViolation(
            "canonical recommendation registry readback returned malformed payload"
        )

    # DB 不存文件 version。只在完整 DB 快照已经读出后对齐当前磁盘
    # base；如果另一 writer 在此后抢先落盘，save 的 CAS 会把本次标记
    # degraded，不会用旧快照覆盖新镜像。
    registry["version"] = _read_disk_base_version(path)
    return registry


def refresh_recommendation_audit_mirror_after_db_commit(
    path: pathlib.Path,
    *,
    recommendation_id: str,
    transition: str,
) -> bool:
    """在 recommendation 状态 DB CAS 已提交后刷新 JSON 审计镜像。

    此函数只能在 :func:`_db_update_rec_status` 成功返回之后调用。此时 DB
    是 canonical truth，JSON 只是可修复的审计镜像；因此 CAS 竞态、损坏文件、
    序列化错误或 I/O 失败都不能把已提交的业务转移伪装成 HTTP 失败。
    CAS 冲突会最多两次重读 DB 后重试，防止较早的并发 writer 先落盘后
    把较新转移永久遗漏在镜像外。最终失败只记录安全的异常类型并返回
    ``False``，让调用方显式回传 degraded 状态。

    请求开始时的 registry 快照可能漏掉其他已提交的并发转移，因此
    本函数刻意不接收该快照；每次刷新必须先从 DB 重读整份 canonical
    registry，再对 JSON 镜像执行 version CAS。

    离线纯文件调用方必须继续直接使用 :func:`save_recommendation_registry`；
    该路径不会吞掉 CAS 或写入失败。
    """
    max_cas_attempts = 3
    for attempt in range(1, max_cas_attempts + 1):
        try:
            canonical_registry = (
                _load_canonical_recommendation_registry_for_audit_mirror(path)
            )
            save_recommendation_registry(
                canonical_registry,
                path,
                fail_on_cas_read_error=True,
            )
            return True
        except RecommendationRegistryCASConflict as exc:
            if attempt < max_cas_attempts:
                log.warning(
                    "recommendation audit mirror CAS retry after canonical DB commit: "
                    "recommendation_id=%s transition=%s attempt=%d/%d",
                    recommendation_id,
                    transition,
                    attempt,
                    max_cas_attempts,
                )
                continue
            failure_type = type(exc).__name__
        except Exception as exc:
            # 不输出 exception 正文或 traceback：底层 DB driver 消息可能带连接
            # 元数据。类型 + recommendation/transition 足以用于安全告警聚合。
            failure_type = type(exc).__name__
        log.error(
            "recommendation audit mirror degraded after canonical DB commit: "
            "recommendation_id=%s transition=%s path=%s failure_type=%s",
            recommendation_id,
            transition,
            path,
            failure_type,
        )
        return False
    return False


def create_recommendation(
    *,
    family: str,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str,
    recommendation_type: str,
    target_parameter_set_id: str | None = None,
    source_round_id: str | None = None,
    confidence: str,
    reason: str,
    evidence_bundle_ref: str | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    if status != "draft":
        raise ValueError(
            "recommendation 初始 status 只能为 draft；终态必须通过专用 CAS 流程"
        )
    return {
        "recommendation_id": _make_recommendation_id(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "recommendation_type": recommendation_type,
        "target_parameter_set_id": target_parameter_set_id,
        "source_round_id": source_round_id,
        "confidence": confidence,
        "reason": reason,
        "evidence_bundle_ref": evidence_bundle_ref,
        "status": status,
    }


def add_recommendation(
    registry: dict[str, Any], rec: dict[str, Any],
) -> None:
    """将新 recommendation 加入 registry.

    同一 ``(family, symbol, timeframe, recommendation_type)`` 下已有的
    **draft** 建议会被自动标记为 ``superseded``，避免审批队列无限膨胀。

    不同类型的 recommendation 需要并存。例如同一轮里既可能有
    ``parameter_upgrade``，也可能有 ``pause`` / ``require_review``，
    前者代表“本轮生成了候选参数”，后者代表“当前治理建议不要推进”。
    这两类信息不能互相覆盖，否则 operator 会误以为系统没有产出参数候选。

    已 approved / rejected 等终态记录不受影响。
    """
    new_id = rec.get("recommendation_id")
    storage_mode = registry.get(_STORAGE_MODE_FIELD)
    if storage_mode not in {_STORAGE_MODE_MANAGED_DB, _STORAGE_MODE_OFFLINE}:
        storage_mode = (
            _STORAGE_MODE_MANAGED_DB
            if has_explicit_governance_db_configuration()
            else _STORAGE_MODE_OFFLINE
        )
    managed_db = storage_mode == _STORAGE_MODE_MANAGED_DB

    immutable_fields = (
        "family",
        "symbol",
        "timeframe",
        "recommendation_type",
        "target_parameter_set_id",
        "source_round_id",
        "confidence",
        "reason",
        "evidence_bundle_ref",
    )
    for existing in registry.get("recommendations", []):
        if existing.get("recommendation_id") != new_id:
            continue
        if any(existing.get(field) != rec.get(field) for field in immutable_fields):
            raise DBConflictError("recommendation_immutable_identity_conflict")
        if rec.get("status", "draft") == "draft" and managed_db:
            _db_sync_recommendation(rec)
        elif existing.get("status") != rec.get("status"):
            raise DBConflictError("recommendation_lifecycle_conflict")
        return

    if rec.get("status", "draft") != "draft":
        raise ValueError(
            "新增 recommendation 必须为 draft；禁止绕过审批状态机"
        )
    terminal_audit_fields = (
        "approved_by",
        "approved_at",
        "rejected_by",
        "rejected_at",
        "superseded_by",
        "superseded_at",
        "superseded_by_recommendation_id",
    )
    if any(rec.get(field) is not None for field in terminal_audit_fields):
        raise ValueError("draft recommendation 不能携带终态审计字段")

    if managed_db:
        canonical = _db_add_recommendation_atomic(rec)
        registry["recommendations"] = canonical["recommendations"]
        registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_MANAGED_DB
        return

    # Explicit file-only development mode keeps the legacy local state
    # machine.  The subsequent save_recommendation_registry() version CAS is
    # the concurrency boundary; managed deployments never enter this branch.
    new_scope = tuple(
        rec.get(field)
        for field in ("family", "symbol", "timeframe", "recommendation_type")
    )
    superseded_at = rec.get("created_at") or datetime.now(timezone.utc).isoformat()
    for existing in registry.get("recommendations", []):
        existing_scope = tuple(
            existing.get(field)
            for field in ("family", "symbol", "timeframe", "recommendation_type")
        )
        if existing.get("status") == "draft" and existing_scope == new_scope:
            existing["status"] = "superseded"
            existing["superseded_at"] = superseded_at
            existing["superseded_by"] = "system"
            existing["superseded_by_recommendation_id"] = new_id
    registry.setdefault("recommendations", []).append(rec)
    registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_OFFLINE


def find_recommendation(
    registry: dict[str, Any], recommendation_id: str,
) -> dict[str, Any] | None:
    """按 recommendation_id 查找."""
    for rec in registry.get("recommendations", []):
        if rec.get("recommendation_id") == recommendation_id:
            return rec
    return None


# ── Recommendation 状态流转 ──────────────────────────────────────────


def approve_recommendation(
    registry: dict[str, Any],
    recommendation_id: str,
    *,
    approved_by: str = "operator",
    notes: str | None = None,
    project_root: pathlib.Path | None = None,
) -> dict[str, Any] | None:
    """将 recommendation 从 draft → approved.

    Returns
    -------
    dict | None  被审批的 recommendation，找不到或状态非 draft 返回 None
    """
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        log.warning("approve: recommendation %s 不存在", recommendation_id)
        return None

    if rec["status"] != "draft":
        log.warning(
            "approve: recommendation %s 状态为 %s（非 draft），拒绝审批",
            recommendation_id, rec["status"],
        )
        return None

    # Apply-capable recommendations may advance only when their own exact
    # evidence round satisfies the current promotion contract.  This runs
    # before the in-memory transition and before the DB CAS write.  ``cwd`` is
    # retained only for backwards-compatible direct callers; API/workflow
    # callers pass their explicit project root.
    from aats.data_platform.decision_system.promotion_guard import (
        require_promotion_qualification,
    )

    require_promotion_qualification(project_root or pathlib.Path.cwd(), rec)

    # 保留 rollback 快照，DB 返回 False（竞态）时回滚内存状态，保证 JSON 与
    # DB 在并发情况下仍然一致。
    prev_snapshot = _snapshot_fields(
        rec,
        ("status", "approved_by", "approved_at", "review_notes"),
    )
    rec["status"] = "approved"
    rec["approved_by"] = approved_by
    rec["approved_at"] = datetime.now(timezone.utc).isoformat()
    if notes:
        rec["review_notes"] = notes
    try:
        db_result = _db_update_rec_status(rec, expected_current_status="draft")
    except (DBUnavailableError, DBConstraintViolation):
        _restore_fields(rec, prev_snapshot)
        raise
    if db_result is False:
        # 并发：另一 operator 已经抢先转移了状态，回滚本次修改，让 JSON 保持
        # 与 DB 一致；调用方收到 None 后应提示"状态已被他人改写"。
        _restore_fields(rec, prev_snapshot)
        log.warning(
            "approve: recommendation %s 状态被其他进程抢先改写，已回滚内存状态",
            recommendation_id,
        )
        return None
    return rec


def reject_recommendation(
    registry: dict[str, Any],
    recommendation_id: str,
    *,
    rejected_by: str = "operator",
    notes: str | None = None,
) -> dict[str, Any] | None:
    """将 recommendation 从 draft → rejected."""
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        log.warning("reject: recommendation %s 不存在", recommendation_id)
        return None

    if rec["status"] != "draft":
        log.warning(
            "reject: recommendation %s 状态为 %s（非 draft），拒绝驳回",
            recommendation_id, rec["status"],
        )
        return None

    prev_snapshot = _snapshot_fields(
        rec,
        ("status", "rejected_by", "rejected_at", "review_notes"),
    )
    rec["status"] = "rejected"
    rec["rejected_by"] = rejected_by
    rec["rejected_at"] = datetime.now(timezone.utc).isoformat()
    if notes:
        rec["review_notes"] = notes
    try:
        db_result = _db_update_rec_status(rec, expected_current_status="draft")
    except (DBUnavailableError, DBConstraintViolation):
        _restore_fields(rec, prev_snapshot)
        raise
    if db_result is False:
        _restore_fields(rec, prev_snapshot)
        log.warning(
            "reject: recommendation %s 状态被其他进程抢先改写，已回滚内存状态",
            recommendation_id,
        )
        return None
    return rec


def supersede_recommendation(
    registry: dict[str, Any],
    recommendation_id: str,
    *,
    superseded_by_id: str | None = None,
    actor: str = "system",
    notes: str | None = None,
) -> dict[str, Any] | None:
    """将 recommendation 标记为 superseded.

    当新 recommendation 替代旧 recommendation 时使用。
    """
    rec = find_recommendation(registry, recommendation_id)
    if rec is None:
        log.warning("supersede: recommendation %s 不存在", recommendation_id)
        return None

    # 和 approve/reject 一致的幂等保护：已经 superseded / rejected 的记录再
    # 被 supersede 会把 superseded_at / superseded_by 覆盖成第二个 actor，
    # 审计链里第一次 supersede 的痕迹就丢了。拒绝二次推进。
    # - "draft" / "approved" 可以被合法 supersede（前者自动收尾，后者被新
    #   recommendation 替代）
    # - "superseded" / "rejected" 是终态，拒绝
    if rec["status"] not in {"draft", "approved"}:
        log.warning(
            "supersede: recommendation %s 状态为 %s（非 draft/approved），拒绝 supersede",
            recommendation_id, rec["status"],
        )
        return None

    prev_snapshot = _snapshot_fields(
        rec,
        (
            "status",
            "superseded_at",
            "superseded_by",
            "superseded_by_recommendation_id",
            "review_notes",
        ),
    )
    rec["status"] = "superseded"
    rec["superseded_at"] = datetime.now(timezone.utc).isoformat()
    rec["superseded_by"] = actor
    if superseded_by_id:
        rec["superseded_by_recommendation_id"] = superseded_by_id
    if notes:
        rec["review_notes"] = notes
    try:
        db_result = _db_update_rec_status(
            rec, expected_current_status=("draft", "approved"),
        )
    except (DBUnavailableError, DBConstraintViolation):
        _restore_fields(rec, prev_snapshot)
        raise
    if db_result is False:
        _restore_fields(rec, prev_snapshot)
        log.warning(
            "supersede: recommendation %s 状态被其他进程抢先改写，已回滚内存状态",
            recommendation_id,
        )
        return None
    return rec


# ── Active Decision Registry ────────────────────────────────────────


def load_active_decision_registry(path: pathlib.Path, *, skip_db: bool = False) -> dict[str, Any]:
    """加载 active decision registry.

    受管环境以 DB 为唯一真源；空表是权威空结果，查询失败或不可达时
    拒绝陈旧 JSON。只有明确离线模式（包括 ``skip_db=True``）允许文件读。
    """
    if not skip_db:
        try:
            project_root = path.resolve(strict=False).parents[2]
        except (IndexError, OSError, RuntimeError, ValueError):
            project_root = None
        db_is_managed_truth = has_explicit_governance_db_configuration(project_root)
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.recommendations_db import db_load_active_decisions

                with Session(engine) as session:
                    registry = db_load_active_decisions(session)
                if not isinstance(registry, dict):
                    registry = {"generated_at": None, "decisions": []}
                registry.setdefault("decisions", [])
                log.info(
                    "从数据库加载 active decision registry (%d decisions)",
                    len(registry["decisions"]),
                )
                registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_MANAGED_DB
                return registry
            except Exception as exc:
                if db_is_managed_truth:
                    raise DBUnavailableError(
                        "governance DB active-decision read failed; stale JSON fallback denied"
                    ) from exc
                log.warning(
                    "active_decision_registry: 离线开发 DB 读取失败 (%s)，"
                    "fallback 到文件（stale）",
                    type(exc).__name__,
                )
            finally:
                if engine is not None:
                    engine.dispose()
        elif db_is_managed_truth:
            raise DBUnavailableError(
                "governance DB unavailable; stale active-decision JSON fallback denied"
            )

    if not path.exists():
        return {
            "generated_at": None,
            "decisions": [],
            _STORAGE_MODE_FIELD: _STORAGE_MODE_OFFLINE,
        }
    with path.open(encoding="utf-8") as f:
        registry = json.load(f)
    if not isinstance(registry, dict):
        raise ValueError("active decision registry must be a JSON object")
    registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_OFFLINE
    return registry


def save_active_decision_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    registry["version"] = registry.get("version", 0) + 1
    atomic_json_write(
        {
            key: value
            for key, value in registry.items()
            if key != _STORAGE_MODE_FIELD
        },
        path,
    )
    log.info("保存 active decision registry -> %s (v%d, %d items)",
             path, registry["version"], len(registry.get("decisions", [])))


def upsert_active_decision(
    registry: dict[str, Any],
    *,
    family: str,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str,
    current_status: str,
    active_parameter_set_id: str | None = None,
    preserve_existing_active_parameter_set: bool = False,
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """更新或插入 family/timeframe 的 active decision."""
    storage_mode = registry.get(_STORAGE_MODE_FIELD)
    if storage_mode not in {_STORAGE_MODE_MANAGED_DB, _STORAGE_MODE_OFFLINE}:
        storage_mode = (
            _STORAGE_MODE_MANAGED_DB
            if has_explicit_governance_db_configuration()
            else _STORAGE_MODE_OFFLINE
        )
    decisions = registry.setdefault("decisions", [])
    # timeframe 归一到小写一次，所有查找/写入/下游 DB 同步都走同一份，
    # 否则 "1H" / "1h" 会在 existing-match 阶段错位：combo_key 走小写但
    # `d.get("timeframe") == timeframe` 用原始大小写，命不中就会新建一行
    # 造成同一 (family, timeframe) 下多条 active_decisions 并存。
    timeframe_norm = timeframe.lower()
    combo_key = f"{family}_{timeframe_norm}"

    existing = None
    for d in decisions:
        if (
            d.get("family") == family
            and str(d.get("timeframe") or "").lower() == timeframe_norm
        ):
            existing = d
            break

    now = datetime.now(timezone.utc).isoformat()

    # A-0.3: DB-first，失败时必须回滚内存态。existing 路径需要保留整条快照用于恢复，
    # new 路径则记录"我们 append 的索引"以便 pop 掉——否则 DB 不可达时内存里残留
    # 一条 DB 无法回放的 active_decision，产生 split-brain。
    prev_snapshot: dict[str, Any] | None = None
    appended_index: int | None = None

    effective_active_parameter_set_id = active_parameter_set_id
    if (
        preserve_existing_active_parameter_set
        and active_parameter_set_id is not None
    ):
        raise ValueError(
            "active_decision_preserve_parameter_set_cannot_replace"
        )
    if preserve_existing_active_parameter_set:
        effective_active_parameter_set_id = (
            existing.get("active_parameter_set_id")
            if existing is not None
            else None
        )

    if existing:
        prev_snapshot = dict(existing)
        existing["current_status"] = current_status
        existing["active_parameter_set_id"] = (
            effective_active_parameter_set_id
        )
        existing["last_recommendation_id"] = last_recommendation_id
        existing["last_updated_at"] = now
        existing["timeframe"] = timeframe_norm
        existing["combo_key"] = combo_key
        if notes:
            existing["notes"] = notes
    else:
        appended_index = len(decisions)
        decisions.append({
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe_norm,
            "combo_key": combo_key,
            "current_status": current_status,
            "active_parameter_set_id": effective_active_parameter_set_id,
            "last_recommendation_id": last_recommendation_id,
            "last_updated_at": now,
            "notes": notes,
        })

    if storage_mode == _STORAGE_MODE_MANAGED_DB:
        try:
            _db_sync_active_decision(
                family, timeframe_norm, current_status,
                active_parameter_set_id=active_parameter_set_id,
                preserve_existing_active_parameter_set=(
                    preserve_existing_active_parameter_set
                ),
                last_recommendation_id=last_recommendation_id,
                notes=notes,
            )
        except (DBUnavailableError, DBConstraintViolation):
            if existing is not None and prev_snapshot is not None:
                existing.clear()
                existing.update(prev_snapshot)
            elif appended_index is not None:
                # 防御性：删除本次 append 的行，不把 DB 失败伪装成内存成功。
                if appended_index < len(decisions):
                    decisions.pop(appended_index)
            raise
    registry[_STORAGE_MODE_FIELD] = storage_mode


# ── Evidence Bundle Index ────────────────────────────────────────────


def load_evidence_bundle_index(
    path: pathlib.Path,
    *,
    skip_db: bool = False,
) -> dict[str, Any]:
    engine = None
    ok = False
    if not skip_db:
        engine, ok = try_governance_db()
    if ok:
        try:
            from sqlalchemy.orm import Session

            from aats.data_platform.governance.operational_state_db import (
                db_load_decision_evidence_bundle_index,
            )

            with Session(engine) as session:
                registry = db_load_decision_evidence_bundle_index(session)
            if isinstance(registry, dict):
                registry.setdefault("bundles", [])
                registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_MANAGED_DB
                return registry
        except Exception as exc:
            log.warning(
                "evidence_bundle_index: DB read failed (%s), fallback to file",
                type(exc).__name__,
            )
        finally:
            if engine is not None:
                engine.dispose()
    if not path.exists():
        return {
            "generated_at": None,
            "bundles": [],
            _STORAGE_MODE_FIELD: _STORAGE_MODE_OFFLINE,
        }
    with path.open(encoding="utf-8") as f:
        registry = json.load(f)
    if not isinstance(registry, dict):
        raise ValueError("evidence bundle index must be a JSON object")
    registry[_STORAGE_MODE_FIELD] = _STORAGE_MODE_OFFLINE
    return registry


def save_evidence_bundle_index(
    index: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    index["generated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(
        {
            key: value
            for key, value in index.items()
            if key != _STORAGE_MODE_FIELD
        },
        path,
    )
    if index.get(_STORAGE_MODE_FIELD) == _STORAGE_MODE_OFFLINE:
        return
    engine, ok = try_governance_db()
    if not ok:
        return
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.operational_state_db import (
            db_upsert_decision_evidence_bundle,
        )

        with Session(engine) as session, session.begin():
            for bundle in index.get("bundles", []):
                db_upsert_decision_evidence_bundle(session, bundle)
    except Exception as exc:
        log.warning(
            "evidence_bundle_index: DB sync failed (%s)",
            type(exc).__name__,
        )
    finally:
        if engine is not None:
            engine.dispose()


def register_evidence_bundle(
    index: dict[str, Any],
    *,
    round_id: str,
    evidence_summary_path: str,
    phases_with_data: list[str],
    completeness_ratio: float,
) -> None:
    index.setdefault("bundles", []).append({
        "round_id": round_id,
        "evidence_summary_path": evidence_summary_path,
        "phases_with_data": phases_with_data,
        "completeness_ratio": completeness_ratio,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# ── 从 decision round 结果批量更新 ──────────────────────────────────


def _build_round_recommendations(
    *,
    round_id: str,
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize the exact draft set owned by one decision round."""

    recommendations: list[dict[str, Any]] = []
    for candidate in upgrade_candidates:
        candidate_decision = candidate.get("decision")
        if candidate_decision not in {"promote_candidate", "reject"}:
            continue
        recommendations.append(
            create_recommendation(
                family=candidate["family"],
                timeframe=candidate["timeframe"],
                recommendation_type=(
                    "parameter_upgrade"
                    if candidate_decision == "promote_candidate"
                    else "pause"
                ),
                target_parameter_set_id=candidate.get("parameter_set_id"),
                source_round_id=candidate.get("source_round_id"),
                confidence=candidate.get("confidence", "low"),
                reason=candidate.get("reason", ""),
                evidence_bundle_ref=round_id,
            )
        )

    for decision in ft_decisions:
        recommendation_type = decision.get("decision", "require_review")
        if recommendation_type not in RECOMMENDATION_TYPES:
            continue
        recommendations.append(
            create_recommendation(
                family=decision["family"],
                timeframe=decision["timeframe"],
                recommendation_type=recommendation_type,
                confidence=decision.get("confidence", "low"),
                reason="; ".join(decision.get("reasons", [])),
                evidence_bundle_ref=round_id,
            )
        )
    return recommendations


_CONTROL_PLANE_PUBLICATION_FIELD = "control_plane_publication"
_RECOMMENDATION_PUBLICATION_IDENTITY_FIELDS = (
    "recommendation_id",
    "created_at",
    "family",
    "symbol",
    "timeframe",
    "recommendation_type",
    "target_parameter_set_id",
    "source_round_id",
    "confidence",
    "reason",
    "evidence_bundle_ref",
)


def _canonical_json_value(value: Any) -> Any:
    """Match the canonical JSON projection stored by the snapshot DB layer."""

    return json.loads(canonical_typed_json_bytes(value).decode("utf-8"))


def _canonical_publication_timestamp(value: str, *, context: str) -> str:
    from aats.data_platform.governance._time_util import (
        parse_iso_datetime_utc,
    )

    parsed = parse_iso_datetime_utc(value, context=context)
    if parsed is None:
        raise ValueError(f"{context}_required")
    return parsed.astimezone(timezone.utc).isoformat()


def _recommendation_publication_identity(
    recommendation: dict[str, Any],
    *,
    producer_index: int | None = None,
) -> dict[str, Any]:
    identity = {
        field: recommendation.get(field)
        for field in _RECOMMENDATION_PUBLICATION_IDENTITY_FIELDS
    }
    identity["timeframe"] = str(identity.get("timeframe") or "").lower()
    identity["symbol"] = str(
        identity.get("symbol") or "BTC-USDT-SWAP"
    ).upper()
    if identity.get("created_at") is not None:
        identity["created_at"] = _canonical_publication_timestamp(
            str(identity["created_at"]),
            context="decision_round.recommendation.created_at",
        )
    if producer_index is not None:
        identity["producer_index"] = producer_index
    return _canonical_json_value(identity)


def _materialize_managed_round_recommendations(
    *,
    round_id: str,
    finished_at: str,
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build deterministic recommendation identities for a managed round."""

    recommendations = _build_round_recommendations(
        round_id=round_id,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
    )
    for producer_index, recommendation in enumerate(recommendations):
        recommendation["created_at"] = finished_at
        identity_seed = {
            "schema_version": "aats.phase6.recommendation_identity.v1",
            "round_id": round_id,
            "producer_index": producer_index,
            "recommendation": {
                field: recommendation.get(field)
                for field in _RECOMMENDATION_PUBLICATION_IDENTITY_FIELDS
                if field not in {"recommendation_id", "created_at"}
            },
        }
        digest = typed_json_sha256(identity_seed)
        recommendation["recommendation_id"] = f"rec_{digest}"
    return recommendations


def _build_control_plane_publication(
    *,
    round_id: str,
    recommendations: list[dict[str, Any]],
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
    bundle_entry: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the immutable mapping between snapshot and mutable control plane."""

    promoted_by_combo: dict[str, str] = {}
    for candidate in upgrade_candidates:
        if candidate.get("decision") != "promote_candidate":
            continue
        combo_key = (
            f"{candidate['family']}_"
            f"{str(candidate['timeframe']).lower()}"
        )
        parameter_set_id = candidate.get("parameter_set_id")
        if not isinstance(parameter_set_id, str) or not parameter_set_id:
            raise ValueError(
                f"promote_candidate_parameter_set_id_required:{combo_key}"
            )
        if combo_key in promoted_by_combo:
            raise ValueError(
                f"duplicate_promote_candidate_for_combo:{combo_key}"
            )
        promoted_by_combo[combo_key] = parameter_set_id

    last_recommendation_by_combo: dict[str, str] = {}
    recommendation_mapping: list[dict[str, Any]] = []
    for producer_index, recommendation in enumerate(recommendations):
        combo_key = (
            f"{recommendation['family']}_"
            f"{str(recommendation['timeframe']).lower()}"
        )
        last_recommendation_by_combo[combo_key] = str(
            recommendation["recommendation_id"]
        )
        recommendation_mapping.append(
            _recommendation_publication_identity(
                recommendation,
                producer_index=producer_index,
            )
        )

    active_mapping: list[dict[str, Any]] = []
    seen_active_combos: set[str] = set()
    for decision in sorted(
        ft_decisions,
        key=lambda item: (
            str(item.get("family") or "").lower(),
            str(item.get("timeframe") or "").lower(),
        ),
    ):
        family = str(decision.get("family") or "")
        timeframe = str(decision.get("timeframe") or "").lower()
        if not family or not timeframe:
            raise ValueError("active_decision_identity_invalid")
        current_status = decision.get("decision")
        if not isinstance(current_status, str) or not current_status:
            raise ValueError("active_decision_status_invalid")
        combo_key = f"{family}_{timeframe}"
        if combo_key in seen_active_combos:
            raise ValueError(f"duplicate_active_decision_combo:{combo_key}")
        seen_active_combos.add(combo_key)
        replaces_parameter_set = combo_key in promoted_by_combo
        active_mapping.append(
            {
                "family": family,
                "symbol": str(
                    decision.get("symbol") or "BTC-USDT-SWAP"
                ).upper(),
                "timeframe": timeframe,
                "combo_key": combo_key,
                "current_status": current_status,
                "active_parameter_set_policy": (
                    "replace_from_promote_candidate"
                    if replaces_parameter_set
                    else "preserve_existing"
                ),
                "active_parameter_set_id": promoted_by_combo.get(combo_key),
                "last_recommendation_id": (
                    last_recommendation_by_combo.get(combo_key)
                ),
                "notes": f"Decision round {round_id}",
            }
        )

    publication = {
        "schema_version": "aats.phase6.control_plane_publication.v1",
        "recommendations": recommendation_mapping,
        "active_decisions": active_mapping,
        "evidence_bundle": _canonical_json_value(bundle_entry),
    }
    return _canonical_json_value(publication), promoted_by_combo


def _expected_managed_round_snapshot(
    *,
    round_id: str,
    started_at: str,
    finished_at: str,
    evidence_bundle: dict[str, Any],
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
    readiness_report: dict[str, Any],
    manifest: dict[str, Any],
    conclusion_markdown: str,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "evidence_bundle_summary": _canonical_json_value(evidence_bundle),
        "parameter_upgrade_candidates": _canonical_json_value(
            upgrade_candidates
        ),
        "family_timeframe_decisions": _canonical_json_value(ft_decisions),
        "promotion_readiness_assessment": _canonical_json_value(
            readiness_report
        ),
        "manifest": _canonical_json_value(manifest),
        "conclusion_markdown": conclusion_markdown,
    }


def _typed_publication_digest(snapshot_identity: dict[str, Any]) -> str:
    """Hash JSON with Python numeric types preserved (``1`` != ``1.0``)."""

    return typed_json_sha256(snapshot_identity)


def planned_registry_stats(
    *,
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
) -> dict[str, int]:
    """Return deterministic publication counts without mutating a truth source."""

    recommendation_count = sum(
        candidate.get("decision") in {"promote_candidate", "reject"}
        for candidate in upgrade_candidates
    ) + sum(
        decision.get("decision", "require_review") in RECOMMENDATION_TYPES
        for decision in ft_decisions
    )
    return {
        "recommendations_added": int(recommendation_count),
        "decisions_updated": len(ft_decisions),
        "bundles_registered": 1,
    }


def publish_managed_decision_round(
    *,
    round_id: str,
    started_at: str,
    finished_at: str,
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
    evidence_bundle: dict[str, Any],
    evidence_summary_path: str,
    readiness_report: dict[str, Any],
    manifest: dict[str, Any],
    conclusion_markdown: str,
) -> dict[str, int]:
    """Publish every canonical Phase 6 record in one DB transaction.

    The immutable decision snapshot is the commit marker. Draft replacement,
    active-decision CAS and the evidence-bundle row share its transaction, so
    any failure rolls back the complete round. Managed JSON registries are
    repairable audit mirrors and intentionally do not participate in this
    PostgreSQL control-plane transaction.
    """

    from sqlalchemy.exc import IntegrityError, OperationalError
    from sqlalchemy.orm import Session

    from aats.data_platform.governance.decision_rounds_db import (
        db_acquire_decision_round_publication_lock,
        db_load_decision_round_snapshot,
        db_upsert_decision_round_snapshot,
    )
    from aats.data_platform.governance.operational_state_db import (
        db_get_decision_evidence_bundle,
        db_insert_decision_evidence_bundle,
    )
    from aats.data_platform.governance.recommendations_db import (
        db_find_recommendations_for_evidence_bundle,
        db_insert_recommendation_superseding_drafts,
        db_upsert_active_decision,
    )

    if (
        not isinstance(round_id, str)
        or not round_id.strip()
        or len(round_id) > 128
    ):
        raise ValueError("decision_round_publication_round_id_invalid")
    started_at_canonical = _canonical_publication_timestamp(
        started_at,
        context="decision_round.started_at",
    )
    finished_at_canonical = _canonical_publication_timestamp(
        finished_at,
        context="decision_round.finished_at",
    )
    if datetime.fromisoformat(finished_at_canonical) < datetime.fromisoformat(
        started_at_canonical
    ):
        raise ValueError("decision_round_finished_before_started")
    recommendations = _materialize_managed_round_recommendations(
        round_id=round_id,
        finished_at=finished_at_canonical,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
    )
    stats = planned_registry_stats(
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
    )
    completeness = evidence_bundle.get("evidence_completeness", {})
    bundle_entry = {
        "round_id": round_id,
        "evidence_summary_path": evidence_summary_path,
        "phases_with_data": list(completeness.get("phases_with_data", [])),
        "completeness_ratio": float(
            completeness.get("completeness_ratio", 0) or 0.0
        ),
        "created_at": finished_at_canonical,
    }
    control_plane_publication, promoted_by_combo = (
        _build_control_plane_publication(
            round_id=round_id,
            recommendations=recommendations,
            upgrade_candidates=upgrade_candidates,
            ft_decisions=ft_decisions,
            bundle_entry=bundle_entry,
        )
    )

    publication_manifest = _canonical_json_value(manifest)
    if not isinstance(publication_manifest, dict):
        raise ValueError("decision_round_manifest_invalid")
    supplied_mapping = publication_manifest.get(
        _CONTROL_PLANE_PUBLICATION_FIELD
    )
    if (
        supplied_mapping is not None
        and _typed_publication_digest({"value": supplied_mapping})
        != _typed_publication_digest({"value": control_plane_publication})
    ):
        raise DBConflictError(
            "decision_round_control_plane_publication_identity_conflict"
        )
    publication_manifest[_CONTROL_PLANE_PUBLICATION_FIELD] = (
        control_plane_publication
    )
    supplied_digest = publication_manifest.pop(
        "publication_identity_sha256",
        None,
    )
    digest_identity = _expected_managed_round_snapshot(
        round_id=round_id,
        started_at=started_at_canonical,
        finished_at=finished_at_canonical,
        evidence_bundle=evidence_bundle,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
        readiness_report=readiness_report,
        manifest=publication_manifest,
        conclusion_markdown=conclusion_markdown,
    )
    publication_digest = _typed_publication_digest(digest_identity)
    if supplied_digest is not None and supplied_digest != publication_digest:
        raise DBConflictError(
            "decision_round_publication_digest_conflict"
        )
    publication_manifest["publication_identity_sha256"] = (
        publication_digest
    )
    expected_snapshot = _expected_managed_round_snapshot(
        round_id=round_id,
        started_at=started_at_canonical,
        finished_at=finished_at_canonical,
        evidence_bundle=evidence_bundle,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
        readiness_report=readiness_report,
        manifest=publication_manifest,
        conclusion_markdown=conclusion_markdown,
    )

    engine, ok = try_governance_db()
    if not ok or engine is None:
        raise DBUnavailableError("decision_round_managed_db_unavailable")
    try:
        with Session(engine) as session, session.begin():
            db_acquire_decision_round_publication_lock(
                session,
                round_id=round_id,
            )
            existing_snapshot = db_load_decision_round_snapshot(
                session,
                round_id=round_id,
            )
            if existing_snapshot is not None:
                existing_manifest = existing_snapshot.get("manifest")
                if not isinstance(existing_manifest, dict):
                    raise DBConflictError(
                        "decision_round_publication_identity_conflict"
                    )
                existing_digest = existing_manifest.get(
                    "publication_identity_sha256"
                )
                existing_digest_manifest = dict(existing_manifest)
                existing_digest_manifest.pop(
                    "publication_identity_sha256",
                    None,
                )
                existing_digest_identity = dict(existing_snapshot)
                existing_digest_identity["manifest"] = (
                    existing_digest_manifest
                )
                if (
                    existing_digest != publication_digest
                    or _typed_publication_digest(existing_digest_identity)
                    != existing_digest
                    or _typed_publication_digest(existing_snapshot)
                    != _typed_publication_digest(expected_snapshot)
                ):
                    raise DBConflictError(
                        "decision_round_publication_identity_conflict"
                    )

                actual_bundle = db_get_decision_evidence_bundle(
                    session,
                    round_id=round_id,
                )
                if (
                    actual_bundle is None
                    or _typed_publication_digest({"value": actual_bundle})
                    != _typed_publication_digest(
                        {"value": bundle_entry}
                    )
                ):
                    raise DBConflictError(
                        "decision_round_evidence_bundle_identity_conflict"
                    )

                actual_recommendations = (
                    db_find_recommendations_for_evidence_bundle(
                        session,
                        evidence_bundle_ref=round_id,
                    )
                )
                actual_by_id = {
                    str(item.get("recommendation_id")): item
                    for item in actual_recommendations
                }
                expected_recommendations = control_plane_publication[
                    "recommendations"
                ]
                if set(actual_by_id) != {
                    str(item["recommendation_id"])
                    for item in expected_recommendations
                }:
                    raise DBConflictError(
                        "decision_round_recommendation_mapping_conflict"
                    )
                for expected_recommendation in expected_recommendations:
                    recommendation_id = str(
                        expected_recommendation["recommendation_id"]
                    )
                    actual_recommendation = actual_by_id.get(
                        recommendation_id
                    )
                    if actual_recommendation is None:
                        raise DBConflictError(
                            "decision_round_recommendation_mapping_conflict"
                        )
                    actual_identity = _recommendation_publication_identity(
                        actual_recommendation,
                        producer_index=int(
                            expected_recommendation["producer_index"]
                        ),
                    )
                    if (
                        _typed_publication_digest({"value": actual_identity})
                        != _typed_publication_digest(
                            {"value": expected_recommendation}
                        )
                    ):
                        raise DBConflictError(
                            "decision_round_recommendation_mapping_conflict"
                        )
                # active_decisions is deliberately not compared here: it is a
                # mutable head and may have advanced after this immutable round.
                # The snapshot mapping records the operation originally made.
            else:
                indexed_recommendations = list(enumerate(recommendations))
                indexed_recommendations.sort(
                    key=lambda item: (
                        str(item[1].get("family") or "").lower(),
                        str(
                            item[1].get("symbol") or "BTC-USDT-SWAP"
                        ).upper(),
                        str(item[1].get("timeframe") or "").lower(),
                        str(item[1].get("recommendation_type") or ""),
                        item[0],
                    )
                )
                for _index, recommendation in indexed_recommendations:
                    db_insert_recommendation_superseding_drafts(
                        session,
                        recommendation=recommendation,
                    )

                for active_publication in control_plane_publication[
                    "active_decisions"
                ]:
                    combo_key = str(active_publication["combo_key"])
                    replaces_parameter_set = (
                        active_publication["active_parameter_set_policy"]
                        == "replace_from_promote_candidate"
                    )
                    updated = db_upsert_active_decision(
                        session,
                        family=str(active_publication["family"]),
                        symbol=str(active_publication["symbol"]),
                        timeframe=str(active_publication["timeframe"]),
                        current_status=str(
                            active_publication["current_status"]
                        ),
                        active_parameter_set_id=(
                            promoted_by_combo.get(combo_key)
                        ),
                        preserve_existing_active_parameter_set=(
                            not replaces_parameter_set
                        ),
                        last_recommendation_id=(
                            active_publication["last_recommendation_id"]
                        ),
                        notes=str(active_publication["notes"]),
                    )
                    if updated is not True:
                        # An automatic round may never clear a sticky safety
                        # pause. Any mismatch aborts every round write.
                        raise DBConflictError(
                            "active_decision_sticky_pause_conflict:"
                            f"{combo_key}"
                        )

                db_insert_decision_evidence_bundle(session, bundle_entry)
                db_upsert_decision_round_snapshot(
                    session,
                    round_id=round_id,
                    started_at=started_at_canonical,
                    finished_at=finished_at_canonical,
                    evidence_bundle_summary=expected_snapshot[
                        "evidence_bundle_summary"
                    ],
                    parameter_upgrade_candidates=expected_snapshot[
                        "parameter_upgrade_candidates"
                    ],
                    family_timeframe_decisions=expected_snapshot[
                        "family_timeframe_decisions"
                    ],
                    promotion_readiness_assessment=expected_snapshot[
                        "promotion_readiness_assessment"
                    ],
                    manifest=publication_manifest,
                    conclusion_markdown=conclusion_markdown,
                )
                stored_snapshot = db_load_decision_round_snapshot(
                    session,
                    round_id=round_id,
                )
                if (
                    stored_snapshot is None
                    or _typed_publication_digest(stored_snapshot)
                    != _typed_publication_digest(expected_snapshot)
                ):
                    raise DBConflictError(
                        "decision_round_publication_commit_marker_conflict"
                    )
        manifest.clear()
        manifest.update(publication_manifest)
        return stats
    except DBConflictError:
        raise
    except IntegrityError as exc:
        raise DBConstraintViolation(
            "decision_round_atomic_publication_constraint_violation"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            "decision_round_atomic_publication_db_unavailable"
        ) from exc
    finally:
        try:
            engine.dispose()
        except Exception as exc:  # pragma: no cover - defensive pool cleanup
            # The transaction outcome is already canonical.  Pool cleanup is
            # not a business write and must not turn a committed round into a
            # false process failure.
            log.warning(
                "decision_round_engine_dispose_degraded failure_type=%s",
                type(exc).__name__,
            )


def update_registries_from_round(
    *,
    round_id: str,
    upgrade_candidates: list[dict[str, Any]],
    ft_decisions: list[dict[str, Any]],
    evidence_bundle: dict[str, Any],
    rec_registry_path: pathlib.Path,
    decision_registry_path: pathlib.Path,
    bundle_index_path: pathlib.Path,
    evidence_summary_path: str,
    offline_only: bool = False,
) -> dict[str, int]:
    """从 decision round 结果批量更新三个 registry.

    Returns
    -------
    dict  统计: recommendations_added, decisions_updated, bundles_registered
    """
    stats = {"recommendations_added": 0, "decisions_updated": 0, "bundles_registered": 0}

    # 1. Recommendation registry
    rec_reg = (
        load_recommendation_registry(rec_registry_path, skip_db=True)
        if offline_only
        else load_recommendation_registry(rec_registry_path)
    )
    if rec_reg.get(_STORAGE_MODE_FIELD) == _STORAGE_MODE_MANAGED_DB:
        raise RuntimeError(
            "managed decision rounds require publish_managed_decision_round"
        )

    for recommendation in _build_round_recommendations(
        round_id=round_id,
        upgrade_candidates=upgrade_candidates,
        ft_decisions=ft_decisions,
    ):
        add_recommendation(rec_reg, recommendation)
        stats["recommendations_added"] += 1

    save_recommendation_registry(rec_reg, rec_registry_path)

    # 2. Active decision registry
    dec_reg = (
        load_active_decision_registry(decision_registry_path, skip_db=True)
        if offline_only
        else load_active_decision_registry(decision_registry_path)
    )

    # 参数升级建议 → 关联 parameter_set_id
    promoted_by_ft: dict[str, str] = {}
    for uc in upgrade_candidates:
        if uc.get("decision") == "promote_candidate":
            ft_key = f"{uc['family']}_{uc['timeframe'].lower()}"
            parameter_set_id = uc.get("parameter_set_id")
            if not isinstance(parameter_set_id, str) or not parameter_set_id:
                raise ValueError(
                    f"promote_candidate_parameter_set_id_required:{ft_key}"
                )
            if ft_key in promoted_by_ft:
                raise ValueError(
                    f"duplicate_promote_candidate_for_combo:{ft_key}"
                )
            promoted_by_ft[ft_key] = parameter_set_id

    last_rec_ids: dict[str, str] = {}
    for rec in rec_reg.get("recommendations", []):
        ft_key = f"{rec['family']}_{rec['timeframe'].lower()}"
        last_rec_ids[ft_key] = rec["recommendation_id"]

    for ftd in ft_decisions:
        ft_key = f"{ftd['family']}_{str(ftd['timeframe']).lower()}"
        upsert_active_decision(
            dec_reg,
            family=ftd["family"],
            timeframe=ftd["timeframe"],
            current_status=ftd["decision"],
            active_parameter_set_id=promoted_by_ft.get(ft_key),
            preserve_existing_active_parameter_set=(
                ft_key not in promoted_by_ft
            ),
            last_recommendation_id=last_rec_ids.get(ft_key),
            notes=f"Decision round {round_id}",
        )
        stats["decisions_updated"] += 1

    save_active_decision_registry(dec_reg, decision_registry_path)

    # 3. Evidence bundle index
    bi = (
        load_evidence_bundle_index(bundle_index_path, skip_db=True)
        if offline_only
        else load_evidence_bundle_index(bundle_index_path)
    )
    completeness = evidence_bundle.get("evidence_completeness", {})
    register_evidence_bundle(
        bi,
        round_id=round_id,
        evidence_summary_path=evidence_summary_path,
        phases_with_data=completeness.get("phases_with_data", []),
        completeness_ratio=completeness.get("completeness_ratio", 0),
    )
    save_evidence_bundle_index(bi, bundle_index_path)
    stats["bundles_registered"] += 1
    # Do not replay the full in-memory registries here.  Every newly created
    # recommendation and every active-decision transition is already persisted
    # DB-first by ``add_recommendation`` / ``upsert_active_decision``.  A final
    # batch upsert would be a stale snapshot writer capable of resurrecting a
    # recommendation concurrently superseded or approved by another actor.

    return stats
