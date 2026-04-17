"""Registry helpers for automated strategy tuning proposals."""

from __future__ import annotations

import logging
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aats.data_platform.governance._atomic_io import atomic_json_write
from aats.data_platform.governance._db_util import try_governance_db

log = logging.getLogger(__name__)

_REGISTRY_PATH = Path("artifacts/governance/strategy_tuning_proposals.json")
_OVERRIDES_PATH = Path("artifacts/governance/strategy_tuning_overrides.json")
_OPEN_STATUSES = frozenset({"pending_review"})
_FINAL_STATUSES = frozenset({"approved", "rejected", "superseded"})

# P0-3：进程内 last-known-good overrides 缓存。
# DB 读成功 → 刷新；DB 读失败且缓存存在 → 降级返回带 stale=True 标志的副本。
# 4 进程里各自持有一份，不跨进程共享——每个进程独立对齐自己的 DB 读时序。
# 不做磁盘持久化：重启即冷启动，避免"旧 JSON 又悄悄生效"的历史坑。
_LAST_OVERRIDES_CACHE: dict[str, Any] | None = None


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "on", "true", "yes"}


def _is_tuning_json_export_enabled() -> bool:
    return _env_flag_enabled("AATS_P0_TUNING_JSON_EXPORT")


def _is_tuning_fail_loud_enabled() -> bool:
    return _env_flag_enabled("AATS_P0_TUNING_FAIL_LOUD")


def _validate_overrides_shape(payload: Any) -> bool:
    """写 cache 前做一次基本 shape 校验；防止半成品数据污染后续降级路径。

    要求：payload 是 dict，且 combo_overrides 是 dict。其它字段允许缺失或异常，
    因为 load 路径本身会兜底。
    """
    if not isinstance(payload, dict):
        return False
    combo = payload.get("combo_overrides")
    return isinstance(combo, dict)


def _cache_overrides(payload: dict[str, Any]) -> None:
    global _LAST_OVERRIDES_CACHE
    if not _validate_overrides_shape(payload):
        log.warning("strategy tuning overrides cache: payload shape 不合法，跳过缓存刷新")
        return
    _LAST_OVERRIDES_CACHE = {
        "combo_overrides": dict(payload.get("combo_overrides") or {}),
        "generated_at": payload.get("generated_at"),
        "loaded_at": _utcnow().isoformat(),
        "source": "db",
    }


def _reset_overrides_cache_for_tests() -> None:
    """测试钩子：在 fail-loud / cold-start 场景用例之间清理进程内 cache。

    模块私有，生产代码不应调用。测试只需 `from ... import _reset_overrides_cache_for_tests`。
    """
    global _LAST_OVERRIDES_CACHE
    _LAST_OVERRIDES_CACHE = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_proposal_id() -> str:
    return f"tprop_{_utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def registry_path(project_root: Path) -> Path:
    return project_root / _REGISTRY_PATH


def overrides_path(project_root: Path) -> Path:
    return project_root / _OVERRIDES_PATH


def load_strategy_tuning_registry(project_root: Path) -> dict[str, Any]:
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.strategy_tuning_db import (
                db_load_strategy_tuning_registry,
            )

            with Session(engine) as session:
                payload = db_load_strategy_tuning_registry(session)
            # DB 是真源：空 proposals 也直接返回，避免把旧 strategy_tuning_proposals.json 重新注入审核链
            payload.setdefault("version", 0)
            payload.setdefault("proposals", [])
            return payload
        except Exception:
            pass
        finally:
            if engine is not None:
                engine.dispose()

    path = registry_path(project_root)
    if not path.exists():
        return {"generated_at": None, "version": 0, "proposals": []}
    try:
        import json

        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {"generated_at": None, "version": 0, "proposals": []}
    if not isinstance(payload, dict):
        return {"generated_at": None, "version": 0, "proposals": []}
    payload.setdefault("version", 0)
    payload.setdefault("proposals", [])
    return payload


def save_strategy_tuning_registry(project_root: Path, registry: dict[str, Any]) -> Path:
    path = registry_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    registry["generated_at"] = _utcnow().isoformat()
    registry["version"] = int(registry.get("version", 0)) + 1

    # 顺序：DB 先、文件后。DB 写失败则文件保持旧状态，不留下未入库的 ghost 提案。
    engine, ok = try_governance_db()
    if not ok:
        if _is_tuning_fail_loud_enabled():
            raise RuntimeError(
                "governance DB 不可达，strategy tuning registry 无法持久化到真源 "
                "（AATS_P0_TUNING_FAIL_LOUD=on 强制中断）"
            )
        log.warning(
            "strategy tuning registry: DB 不可达，进入单机兼容模式仅写 JSON "
            "（生产建议开启 AATS_P0_TUNING_FAIL_LOUD）"
        )
    else:
        try:
            from aats.data_platform.governance.strategy_tuning_db import (
                db_upsert_strategy_tuning_proposal,
            )

            with Session(engine) as session, session.begin():
                for proposal in registry.get("proposals", []):
                    if isinstance(proposal, dict):
                        db_upsert_strategy_tuning_proposal(session, proposal)
        except Exception as exc:
            log.exception("strategy tuning registry DB 同步失败，保存未完成")
            raise RuntimeError(
                f"strategy tuning registry DB 同步失败，状态未持久化到真源: {exc}"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()

    atomic_json_write(registry, path)
    return path


def load_strategy_tuning_overrides(project_root: Path) -> dict[str, Any]:
    """运行时读取 strategy tuning overrides。

    真源是 ``governance.strategy_tuning_proposals`` 表（按 approved 派生 combo_overrides）。
    读路径：
      1. DB 可达 + 读成功 → 刷 `_LAST_OVERRIDES_CACHE`，返回 `{..., "stale": False}`
      2. DB 失败（不可达 / 超时 / 异常）+ cache 命中 + `AATS_P0_TUNING_FAIL_LOUD` off
         → 打 warning，返回 `{**cached, "stale": True, "source": "cache"}`
      3. DB 失败 + (cache 为空 或 `AATS_P0_TUNING_FAIL_LOUD=on`) → `RuntimeError`

    旧的 `artifacts/governance/strategy_tuning_overrides.json` 已彻底退出读路径。
    `project_root` 参数保留用于签名兼容（测试 / 消费方传入），内部不再访问文件。
    """
    del project_root  # 已不再读文件副本

    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.strategy_tuning_db import (
                db_load_strategy_tuning_overrides,
            )

            with Session(engine) as session:
                payload = db_load_strategy_tuning_overrides(session)
        except Exception as exc:
            log.warning("strategy tuning overrides: DB 读取失败 (%s)", exc)
            payload = None
        finally:
            if engine is not None:
                engine.dispose()

        if payload is not None:
            if not isinstance(payload, dict):
                payload = {"generated_at": None, "combo_overrides": {}}
            payload.setdefault("combo_overrides", {})
            _cache_overrides(payload)
            return {**payload, "stale": False, "source": "db"}

    # 到这里：DB 不可达或读出异常
    if _is_tuning_fail_loud_enabled():
        raise RuntimeError(
            "governance DB 不可达或读取失败，AATS_P0_TUNING_FAIL_LOUD=on 强制中断"
        )

    cached = _LAST_OVERRIDES_CACHE
    if cached is None:
        raise RuntimeError(
            "governance DB 不可达且进程内无 last-known overrides（cold start），无法加载"
        )

    log.warning(
        "strategy tuning overrides: DB 抖动，回退到 %s 的 cached 副本",
        cached.get("loaded_at"),
    )
    return {
        "generated_at": cached.get("generated_at"),
        "combo_overrides": dict(cached.get("combo_overrides") or {}),
        "loaded_at": cached.get("loaded_at"),
        "stale": True,
        "source": "cache",
    }


def save_strategy_tuning_overrides(project_root: Path, payload: dict[str, Any]) -> Path | None:
    """Deprecated：历史 JSON 写盘入口。

    真源已搬到 DB（`strategy_tuning_proposals` 表的 approved 行派生 combo_overrides），
    `save_strategy_tuning_overrides` 不再需要。保留 shim 是为了旧代码路径不立刻炸；
    仅在 `AATS_P0_TUNING_JSON_EXPORT=on` 时真的写出 JSON 副本。
    """
    warnings.warn(
        "save_strategy_tuning_overrides 已废弃：overrides 真源在 DB，"
        "JSON 副本仅在 AATS_P0_TUNING_JSON_EXPORT=on 时导出。",
        DeprecationWarning,
        stacklevel=2,
    )
    if not _is_tuning_json_export_enabled():
        return None
    path = overrides_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["generated_at"] = _utcnow().isoformat()
    atomic_json_write(payload, path)
    return path


def _build_active_overrides(registry: dict[str, Any]) -> dict[str, Any]:
    approved: list[dict[str, Any]] = [
        item for item in registry.get("proposals", []) if item.get("status") == "approved"
    ]
    approved.sort(
        key=lambda item: (
            str(item.get("reviewed_at") or ""),
            str(item.get("created_at") or ""),
        )
    )
    combo_overrides: dict[str, dict[str, Any]] = {}
    for proposal in approved:
        combo_key = str(proposal.get("combo_key") or "")
        parameter = str(proposal.get("parameter") or "")
        if not combo_key or not parameter:
            continue
        combo_overrides.setdefault(combo_key, {})[parameter] = proposal.get("proposed_value")
    return {"combo_overrides": combo_overrides}


def refresh_strategy_tuning_overrides(project_root: Path, registry: dict[str, Any]) -> str:
    """从内存 registry 派生 overrides，刷新进程 cache，并按 flag 决定是否写 JSON。

    调用方通常刚做完 `save_strategy_tuning_registry`（DB 写入了 proposals），
    因此这里派生出的 overrides 就是下一次 DB 读应该得到的内容——可以直接作为
    cache 的起点，保证 apply 后立刻有可降级的 last-known 副本。
    """
    payload = _build_active_overrides(registry)
    payload["generated_at"] = _utcnow().isoformat()
    _cache_overrides(payload)

    if not _is_tuning_json_export_enabled():
        return ""

    path = overrides_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(payload, path)
    return str(path)


def get_combo_tuning_overrides(
    project_root: Path,
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    combo_key = f"{family}_{str(timeframe).lower()}"
    payload = load_strategy_tuning_overrides(project_root)
    combo_overrides = payload.get("combo_overrides", {})
    overrides = combo_overrides.get(combo_key, {})
    return dict(overrides) if isinstance(overrides, dict) else {}


def find_strategy_tuning_proposal(
    registry: dict[str, Any],
    proposal_id: str,
) -> dict[str, Any] | None:
    for proposal in registry.get("proposals", []):
        if proposal.get("proposal_id") == proposal_id:
            return proposal
    return None


def record_generated_proposals(
    project_root: Path,
    *,
    review_id: str,
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    registry = load_strategy_tuning_registry(project_root)
    now = _utcnow().isoformat()
    recorded: list[dict[str, Any]] = []

    for generated in proposals:
        combo_key = generated.get("combo_key")
        parameter = generated.get("parameter")
        proposed_value = generated.get("proposed_value")
        current_value = generated.get("current_value")

        reused = None
        for existing in registry.get("proposals", []):
            if (
                existing.get("combo_key") == combo_key
                and existing.get("parameter") == parameter
                and existing.get("status") == "pending_review"
                and existing.get("proposed_value") == proposed_value
                and existing.get("current_value") == current_value
            ):
                reused = existing
                break

        if reused is not None:
            reused["last_seen_at"] = now
            reused["last_review_id"] = review_id
            recorded.append(reused)
            continue

        for existing in registry.get("proposals", []):
            if (
                existing.get("combo_key") == combo_key
                and existing.get("status") in _OPEN_STATUSES
            ):
                existing["status"] = "superseded"
                existing["superseded_at"] = now
                existing["superseded_by_review_id"] = review_id

        proposal = {
            "proposal_id": _make_proposal_id(),
            "review_id": review_id,
            "last_review_id": review_id,
            "created_at": now,
            "last_seen_at": now,
            "status": "pending_review",
            "review_required": True,
            **generated,
        }
        registry.setdefault("proposals", []).append(proposal)
        recorded.append(proposal)

    path = save_strategy_tuning_registry(project_root, registry)
    overrides = refresh_strategy_tuning_overrides(project_root, registry)
    return {
        "registry": registry,
        "registry_path": str(path),
        "overrides_path": overrides,
        "recorded_proposals": recorded,
        "pending_review_count": sum(
            1 for item in registry.get("proposals", []) if item.get("status") == "pending_review"
        ),
    }


from aats.data_platform.governance.step2_integrity_guard import (
    step2_integrity_blocking_reason as _step2_snapshot_blocking_reason,
)
# 历史符号保留以兼容可能的外部调用；语义现在由共享模块保证，不再允许
# 本地分叉——任一修改都会同时影响 approve / supersede / tuning review 三条路径。


def review_strategy_tuning_proposal(
    project_root: Path,
    *,
    proposal_id: str,
    action: str,
    reviewer: str,
    notes: str | None = None,
) -> dict[str, Any]:
    if action not in {"approve", "reject"}:
        raise ValueError(f"unsupported review action: {action}")

    registry = load_strategy_tuning_registry(project_root)
    proposal = find_strategy_tuning_proposal(registry, proposal_id)
    if proposal is None:
        return {
            "ok": False,
            "message": f"未找到调优提案: {proposal_id}",
            "proposal": None,
        }

    if proposal.get("status") not in _OPEN_STATUSES:
        return {
            "ok": False,
            "message": f"提案当前状态为 {proposal.get('status')}，不能继续审核",
            "proposal": proposal,
        }

    # Step2 快照不完整时阻断 approve；reject 永远允许——运营者需要能清理队列
    # 里过期/脏的提案，这和 UI action 侧 reject 始终 enabled 的契约一致。
    if action == "approve":
        blocking_reason = _step2_snapshot_blocking_reason(project_root)
        if blocking_reason is not None:
            return {
                "ok": False,
                "message": blocking_reason,
                "proposal": proposal,
                "integrity_blocked": True,
            }

    now = _utcnow().isoformat()
    proposal["status"] = "approved" if action == "approve" else "rejected"
    proposal["reviewed_at"] = now
    proposal["reviewed_by"] = reviewer
    if notes:
        proposal["review_notes"] = notes

    path = save_strategy_tuning_registry(project_root, registry)
    active_overrides = refresh_strategy_tuning_overrides(project_root, registry)
    return {
        "ok": True,
        "message": "审核已记录",
        "proposal": proposal,
        "registry_path": str(path),
        "overrides_path": active_overrides,
    }


__all__ = [
    "find_strategy_tuning_proposal",
    "get_combo_tuning_overrides",
    "load_strategy_tuning_registry",
    "load_strategy_tuning_overrides",
    "record_generated_proposals",
    "refresh_strategy_tuning_overrides",
    "registry_path",
    "review_strategy_tuning_proposal",
    "save_strategy_tuning_registry",
    "save_strategy_tuning_overrides",
]
