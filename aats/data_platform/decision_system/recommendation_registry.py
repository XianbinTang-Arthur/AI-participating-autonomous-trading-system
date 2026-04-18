"""Phase 6-E: Recommendation / Decision Registry 管理.

让 recommendation 成为受治理对象：
  - recommendation_registry.json: 所有历史建议
  - active_decision_registry.json: 当前 family/tf 运营状态
  - evidence_bundle_index.json: evidence bundle 引用索引

数据存储策略（DB-first + 文件 fallback）:
  - 写入: 同时写 DB + 文件（DB 失败不阻塞文件写入）
  - 读取: DB 优先 → 文件 fallback
  - DB 开关: 环境变量 AATS_ACTIVE_PARAMETER_DB_URL
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aats.data_platform.governance._db_util import try_governance_db
from aats.data_platform.governance._exceptions import (
    DBConstraintViolation,
    DBUnavailableError,
)

log = logging.getLogger(__name__)


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
            f"recommendation {rec.get('recommendation_id')!r} violated DB constraint: {exc}"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            f"DB operational error while syncing recommendation {rec.get('recommendation_id')!r}: {exc}"
        ) from exc
    finally:
        if engine is not None:
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
    调用方必须传 ``expected_current_status`` 才能拿到 CAS 竞态信号；不传则
    相当于退化成 best-effort（只返回 ``True``，不会返回 ``False``），历史行为
    保留是为了兼容早期读路径，新代码不要依赖。
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
            )
    except IntegrityError as exc:
        raise DBConstraintViolation(
            f"recommendation {rec.get('recommendation_id')!r} status update violated constraint: {exc}"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            f"DB operational error while updating recommendation {rec.get('recommendation_id')!r}: {exc}"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()


def _db_sync_active_decision(
    family: str, timeframe: str, current_status: str,
    active_parameter_set_id: str | None = None,
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
            db_upsert_active_decision(
                session,
                family=family, timeframe=timeframe,
                current_status=current_status,
                active_parameter_set_id=active_parameter_set_id,
                last_recommendation_id=last_recommendation_id,
                notes=notes,
            )
    except IntegrityError as exc:
        raise DBConstraintViolation(
            f"active_decision {family}/{timeframe} violated constraint: {exc}"
        ) from exc
    except OperationalError as exc:
        raise DBUnavailableError(
            f"DB operational error while syncing active_decision {family}/{timeframe}: {exc}"
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

    优先级: DB → 文件 → 空 registry。skip_db=True 跳过 DB 直接读文件。
    """
    if not skip_db:
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.recommendations_db import db_load_recommendation_registry

                with Session(engine) as session:
                    registry = db_load_recommendation_registry(session)
                if registry.get("recommendations"):
                    log.info("从数据库加载 recommendation registry (%d recommendations)",
                             len(registry["recommendations"]))
                    # DB 返回体没有 version 字段——把审计副本当前 version 戳进去,
                    # 让 save 的 CAS 检查对齐磁盘基线。不戳会导致第二次 save 必 500。
                    registry["version"] = _read_disk_base_version(path)
                    return registry
                log.debug("recommendation_registry: DB 为空，fallback 到文件")
            except Exception as exc:
                log.warning("recommendation_registry: DB 读取失败 (%s)，fallback 到文件", exc)
            finally:
                if engine is not None:
                    engine.dispose()

    if not path.exists():
        return {"generated_at": None, "recommendations": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_recommendation_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    """原子写 JSON-only recommendation registry.

    单机兼容（DB 不可达）路径下没有事务保护；两个脚本并发做
    load → mutate → save 会让后写方静默吞掉前写方的变更。这里在落盘前读
    磁盘当前 version，与内存 registry 的 base version 比对：
      * 一致 → 正常 bump + 落盘
      * 不一致（base=N，磁盘=N+k，k>0）→ 抛 RuntimeError，把并发冲突显式
        抛给 caller，迫使它重跑 load → mutate 序列。

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
            log.warning(
                "recommendation_registry: CAS 读磁盘 version 失败 (%s)，"
                "跳过 CAS 按 base_version=%d 继续；磁盘可能被外部进程损坏",
                exc, expected_base_version,
            )
        else:
            disk_version = (
                int(on_disk.get("version", 0)) if isinstance(on_disk, dict) else 0
            )
            if disk_version != expected_base_version:
                raise RuntimeError(
                    f"recommendation_registry CAS 冲突：磁盘 version={disk_version}，"
                    f"内存 base_version={expected_base_version}；"
                    "另一操作已抢先写入，请重新 load→mutate→save"
                )

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    registry["version"] = expected_base_version + 1
    atomic_json_write(registry, path)
    log.info("保存 recommendation registry -> %s (v%d, %d items)",
             path, registry["version"], len(registry.get("recommendations", [])))


def create_recommendation(
    *,
    family: str,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str,
    recommendation_type: str,
    target_parameter_set_id: str | None = None,
    confidence: str,
    reason: str,
    evidence_bundle_ref: str | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    return {
        "recommendation_id": _make_recommendation_id(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "recommendation_type": recommendation_type,
        "target_parameter_set_id": target_parameter_set_id,
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
    new_family = rec.get("family")
    new_symbol = rec.get("symbol")
    new_tf = rec.get("timeframe")
    new_type = rec.get("recommendation_type")
    new_id = rec.get("recommendation_id")

    for existing in registry.get("recommendations", []):
        if (
            existing.get("status") == "draft"
            and existing.get("family") == new_family
            and existing.get("symbol") == new_symbol
            and existing.get("timeframe") == new_tf
            and existing.get("recommendation_type") == new_type
        ):
            # CAS 语义：只有 DB 里此 rec 仍处于 draft 时才推进成 superseded。
            # 如果另一个 operator 已经 approve/reject 掉了它，我们绝不能把
            # 已批准的 rec 覆盖成 superseded——保留 rollback 快照，在 DB
            # 返回 False（并发抢先）时回滚内存，让 JSON 与 DB 保持一致。
            prev_snapshot = {
                "status": existing["status"],
                "superseded_at": existing.get("superseded_at"),
                "superseded_by": existing.get("superseded_by"),
                "superseded_by_recommendation_id": existing.get(
                    "superseded_by_recommendation_id",
                ),
            }
            existing["status"] = "superseded"
            existing["superseded_at"] = rec.get("created_at")
            existing["superseded_by"] = "system"
            existing["superseded_by_recommendation_id"] = new_id
            try:
                db_result = _db_update_rec_status(
                    existing, expected_current_status="draft",
                )
            except DBUnavailableError:
                for key, value in prev_snapshot.items():
                    existing[key] = value
                raise
            if db_result is False:
                for key, value in prev_snapshot.items():
                    existing[key] = value
                log.warning(
                    "add_recommendation: existing rec %s 状态已被其他进程抢先"
                    "改写（非 draft），跳过自动 supersede 以避免覆盖 approved/rejected",
                    existing.get("recommendation_id"),
                )

    # A-0.3：DB 真源写入成功后才 append 到内存并落审计副本，否则 DB 不可达时
    # 内存里会残留一条 DB 无法回放的 recommendation，产生 split-brain。
    _db_sync_recommendation(rec)
    registry.setdefault("recommendations", []).append(rec)


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

    # 保留 rollback 快照，DB 返回 False（竞态）时回滚内存状态，保证 JSON 与
    # DB 在并发情况下仍然一致。
    prev_snapshot = {
        "status": rec["status"],
        "approved_by": rec.get("approved_by"),
        "approved_at": rec.get("approved_at"),
        "review_notes": rec.get("review_notes"),
    }
    rec["status"] = "approved"
    rec["approved_by"] = approved_by
    rec["approved_at"] = datetime.now(timezone.utc).isoformat()
    if notes:
        rec["review_notes"] = notes
    try:
        db_result = _db_update_rec_status(rec, expected_current_status="draft")
    except DBUnavailableError:
        for key, value in prev_snapshot.items():
            rec[key] = value
        raise
    if db_result is False:
        # 并发：另一 operator 已经抢先转移了状态，回滚本次修改，让 JSON 保持
        # 与 DB 一致；调用方收到 None 后应提示"状态已被他人改写"。
        for key, value in prev_snapshot.items():
            rec[key] = value
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

    prev_snapshot = {
        "status": rec["status"],
        "rejected_by": rec.get("rejected_by"),
        "rejected_at": rec.get("rejected_at"),
        "review_notes": rec.get("review_notes"),
    }
    rec["status"] = "rejected"
    rec["rejected_by"] = rejected_by
    rec["rejected_at"] = datetime.now(timezone.utc).isoformat()
    if notes:
        rec["review_notes"] = notes
    try:
        db_result = _db_update_rec_status(rec, expected_current_status="draft")
    except DBUnavailableError:
        for key, value in prev_snapshot.items():
            rec[key] = value
        raise
    if db_result is False:
        for key, value in prev_snapshot.items():
            rec[key] = value
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

    prev_snapshot = {
        "status": rec["status"],
        "superseded_at": rec.get("superseded_at"),
        "superseded_by": rec.get("superseded_by"),
        "superseded_by_recommendation_id": rec.get("superseded_by_recommendation_id"),
        "review_notes": rec.get("review_notes"),
    }
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
    except DBUnavailableError:
        for key, value in prev_snapshot.items():
            rec[key] = value
        raise
    if db_result is False:
        for key, value in prev_snapshot.items():
            rec[key] = value
        log.warning(
            "supersede: recommendation %s 状态被其他进程抢先改写，已回滚内存状态",
            recommendation_id,
        )
        return None
    return rec


# ── Active Decision Registry ────────────────────────────────────────


def load_active_decision_registry(path: pathlib.Path, *, skip_db: bool = False) -> dict[str, Any]:
    """加载 active decision registry.

    优先级: DB → 文件 → 空 registry。skip_db=True 跳过 DB 直接读文件。
    """
    if not skip_db:
        engine, ok = try_governance_db()
        if ok:
            try:
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.recommendations_db import db_load_active_decisions

                with Session(engine) as session:
                    registry = db_load_active_decisions(session)
                if registry.get("decisions"):
                    log.info("从数据库加载 active decision registry (%d decisions)",
                             len(registry["decisions"]))
                    return registry
                log.debug("active_decision_registry: DB 为空，fallback 到文件")
            except Exception as exc:
                log.warning("active_decision_registry: DB 读取失败 (%s)，fallback 到文件", exc)
            finally:
                if engine is not None:
                    engine.dispose()

    if not path.exists():
        return {"generated_at": None, "decisions": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_active_decision_registry(
    registry: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    registry["generated_at"] = datetime.now(timezone.utc).isoformat()
    registry["version"] = registry.get("version", 0) + 1
    atomic_json_write(registry, path)
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
    last_recommendation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """更新或插入 family/timeframe 的 active decision."""
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

    if existing:
        prev_snapshot = dict(existing)
        existing["current_status"] = current_status
        existing["active_parameter_set_id"] = active_parameter_set_id
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
            "active_parameter_set_id": active_parameter_set_id,
            "last_recommendation_id": last_recommendation_id,
            "last_updated_at": now,
            "notes": notes,
        })

    try:
        _db_sync_active_decision(
            family, timeframe_norm, current_status,
            active_parameter_set_id=active_parameter_set_id,
            last_recommendation_id=last_recommendation_id,
            notes=notes,
        )
    except DBUnavailableError:
        if existing is not None and prev_snapshot is not None:
            existing.clear()
            existing.update(prev_snapshot)
        elif appended_index is not None:
            # 防御性：并发 upsert 可能插入了新 entry，删除前检查索引仍指向我们 append
            # 的那一行（它会是最后一条，因为我们刚 append 完）。
            if appended_index < len(decisions):
                decisions.pop(appended_index)
        raise


# ── Evidence Bundle Index ────────────────────────────────────────────


def load_evidence_bundle_index(path: pathlib.Path) -> dict[str, Any]:
    engine, ok = try_governance_db()
    if ok:
        try:
            from sqlalchemy.orm import Session

            from aats.data_platform.governance.operational_state_db import (
                db_load_decision_evidence_bundle_index,
            )

            with Session(engine) as session:
                registry = db_load_decision_evidence_bundle_index(session)
            if registry.get("bundles"):
                return registry
        except Exception as exc:
            log.warning("evidence_bundle_index: DB read failed (%s), fallback to file", exc)
        finally:
            if engine is not None:
                engine.dispose()
    if not path.exists():
        return {"generated_at": None, "bundles": []}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_evidence_bundle_index(
    index: dict[str, Any], path: pathlib.Path,
) -> None:
    from aats.data_platform.governance._atomic_io import atomic_json_write

    index["generated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json_write(index, path)
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
        log.warning("evidence_bundle_index: DB sync failed (%s)", exc)
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


def _sync_registries_to_db_best_effort(
    rec_reg: dict[str, Any],
    dec_reg: dict[str, Any],
) -> None:
    """将最新 registry 状态批量同步到 DB。

    这一步是 Phase 5/6 在 daemon 容器内可见性的兜底收口：
    gateway/UI 读取 recommendation / active decision 时优先走 DB，
    因此这里需要确保最新 registry 至少在 DB 中是可见的。

    失败语义（函数名 ``_best_effort`` 的含义）：
      * governance DB 不可达（``try_governance_db`` 返回 not ok）——记 warning 后
        静默返回，不抛异常；JSON 已经 append 好，caller 按文件模式继续跑。
      * DB 可达但某条 upsert 抛 SQLAlchemy 错误——异常向上传播，caller 负责
        回滚 / 告警（通常是 daemon 里被捕获并标记这一轮 cycle 为 failed）。

    原函数名是 ``_or_raise`` 但只对第二种路径真正 raise，容易让人误以为 DB
    不可达也会炸，所以改成 ``_best_effort`` 以避免误导。
    """
    engine, ok = try_governance_db()
    if not ok:
        log.warning("recommendation_registry: governance DB 不可用，跳过强制 registry 同步")
        return
    try:
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.recommendations_db import (
            db_upsert_active_decision,
            db_upsert_recommendation,
        )

        with Session(engine) as session, session.begin():
            for rec in rec_reg.get("recommendations", []):
                db_upsert_recommendation(
                    session,
                    recommendation_id=rec["recommendation_id"],
                    family=rec["family"],
                    symbol=rec.get("symbol", "BTC-USDT-SWAP"),
                    timeframe=rec["timeframe"],
                    recommendation_type=rec.get("recommendation_type", "require_review"),
                    target_parameter_set_id=rec.get("target_parameter_set_id"),
                    confidence=rec.get("confidence", "low"),
                    reason=rec.get("reason", ""),
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
            for decision in dec_reg.get("decisions", []):
                db_upsert_active_decision(
                    session,
                    family=decision["family"],
                    symbol=decision.get("symbol", "BTC-USDT-SWAP"),
                    timeframe=decision["timeframe"],
                    current_status=decision.get("current_status", "require_review"),
                    active_parameter_set_id=decision.get("active_parameter_set_id"),
                    last_recommendation_id=decision.get("last_recommendation_id"),
                    notes=decision.get("notes"),
                )
    finally:
        if engine is not None:
            engine.dispose()


# ── 从 decision round 结果批量更新 ──────────────────────────────────


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
) -> dict[str, int]:
    """从 decision round 结果批量更新三个 registry.

    Returns
    -------
    dict  统计: recommendations_added, decisions_updated, bundles_registered
    """
    stats = {"recommendations_added": 0, "decisions_updated": 0, "bundles_registered": 0}

    # 1. Recommendation registry
    rec_reg = load_recommendation_registry(rec_registry_path)

    for uc in upgrade_candidates:
        # 只为有明确 decision 的参数创建 recommendation
        if uc.get("decision") in ("promote_candidate", "reject"):
            rec_type = "parameter_upgrade" if uc["decision"] == "promote_candidate" else "pause"
            rec = create_recommendation(
                family=uc["family"],
                timeframe=uc["timeframe"],
                recommendation_type=rec_type,
                target_parameter_set_id=uc.get("parameter_set_id"),
                confidence=uc.get("confidence", "low"),
                reason=uc.get("reason", ""),
                evidence_bundle_ref=round_id,
            )
            add_recommendation(rec_reg, rec)
            stats["recommendations_added"] += 1

    for ftd in ft_decisions:
        rec_type = ftd.get("decision", "require_review")
        if rec_type in RECOMMENDATION_TYPES:
            rec = create_recommendation(
                family=ftd["family"],
                timeframe=ftd["timeframe"],
                recommendation_type=rec_type,
                confidence=ftd.get("confidence", "low"),
                reason="; ".join(ftd.get("reasons", [])),
                evidence_bundle_ref=round_id,
            )
            add_recommendation(rec_reg, rec)
            stats["recommendations_added"] += 1

    save_recommendation_registry(rec_reg, rec_registry_path)

    # 2. Active decision registry
    dec_reg = load_active_decision_registry(decision_registry_path)

    # 参数升级建议 → 关联 parameter_set_id
    promoted_by_ft: dict[str, str] = {}
    for uc in upgrade_candidates:
        if uc.get("decision") == "promote_candidate":
            ft_key = f"{uc['family']}_{uc['timeframe'].lower()}"
            promoted_by_ft[ft_key] = uc.get("parameter_set_id", "")

    last_rec_ids: dict[str, str] = {}
    for rec in rec_reg.get("recommendations", []):
        ft_key = f"{rec['family']}_{rec['timeframe'].lower()}"
        last_rec_ids[ft_key] = rec["recommendation_id"]

    for ftd in ft_decisions:
        ft_key = ftd.get("combo_key", "")
        upsert_active_decision(
            dec_reg,
            family=ftd["family"],
            timeframe=ftd["timeframe"],
            current_status=ftd["decision"],
            active_parameter_set_id=promoted_by_ft.get(ft_key),
            last_recommendation_id=last_rec_ids.get(ft_key),
            notes=f"Decision round {round_id}",
        )
        stats["decisions_updated"] += 1

    save_active_decision_registry(dec_reg, decision_registry_path)

    # 3. Evidence bundle index
    bi = load_evidence_bundle_index(bundle_index_path)
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
    _sync_registries_to_db_best_effort(rec_reg, dec_reg)

    return stats
