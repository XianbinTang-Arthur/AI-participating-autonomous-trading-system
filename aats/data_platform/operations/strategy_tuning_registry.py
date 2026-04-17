"""Registry helpers for automated strategy tuning proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aats.data_platform.governance._atomic_io import atomic_json_write
from aats.data_platform.governance._db_util import try_governance_db

_REGISTRY_PATH = Path("artifacts/governance/strategy_tuning_proposals.json")
_OVERRIDES_PATH = Path("artifacts/governance/strategy_tuning_overrides.json")
_OPEN_STATUSES = frozenset({"pending_review"})
_FINAL_STATUSES = frozenset({"approved", "rejected", "superseded"})


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
    if ok:
        try:
            from aats.data_platform.governance.strategy_tuning_db import (
                db_upsert_strategy_tuning_proposal,
            )

            with Session(engine) as session, session.begin():
                for proposal in registry.get("proposals", []):
                    if isinstance(proposal, dict):
                        db_upsert_strategy_tuning_proposal(session, proposal)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).exception(
                "strategy tuning registry DB 同步失败，保存未完成",
            )
            raise RuntimeError(
                f"strategy tuning registry DB 同步失败，状态未持久化到真源: {exc}"
            ) from exc
        finally:
            if engine is not None:
                engine.dispose()

    atomic_json_write(registry, path)
    return path


def load_strategy_tuning_overrides(project_root: Path) -> dict[str, Any]:
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.strategy_tuning_db import (
                db_load_strategy_tuning_overrides,
            )

            with Session(engine) as session:
                payload = db_load_strategy_tuning_overrides(session)
            # DB 是真源：空 overrides 也直接返回，避免把旧 strategy_tuning_overrides.json 重新污染运行参数
            payload.setdefault("combo_overrides", {})
            return payload
        except Exception:
            pass
        finally:
            if engine is not None:
                engine.dispose()

    path = overrides_path(project_root)
    if not path.exists():
        return {"generated_at": None, "combo_overrides": {}}
    try:
        import json

        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {"generated_at": None, "combo_overrides": {}}
    if not isinstance(payload, dict):
        return {"generated_at": None, "combo_overrides": {}}
    payload.setdefault("combo_overrides", {})
    return payload


def save_strategy_tuning_overrides(project_root: Path, payload: dict[str, Any]) -> Path:
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
    payload = _build_active_overrides(registry)
    path = save_strategy_tuning_overrides(project_root, payload)
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
