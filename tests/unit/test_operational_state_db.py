"""Unit tests for operational_state_db scheduler state roundtrip.

回归保护 B1 修复：bootstrap_stage / bootstrap_completed_at 在 DB 保存时通过
sentinel workflow ``__scheduler_meta__`` 落库，load 时从 meta 行还原到根级，
不再丢失。任何把 meta 行当作普通 workflow 处理、或 save 路径遗漏 meta upsert
的回归，都会被这里捕捉到。

测试采用轻量 fake Session 模拟 Postgres 的 SELECT / INSERT ... ON CONFLICT，
覆盖"真源在 DB"的契约：即便文件 fallback 不可用，meta 字段也能 roundtrip。
"""

from __future__ import annotations

import json
from typing import Any

from aats.data_platform.governance.operational_state_db import (
    _SCHEDULER_META_WORKFLOW,
    db_load_scheduler_state,
    db_save_scheduler_state,
)


class _FakeRow:
    """模拟 SQLAlchemy Row 对象：支持属性访问。"""

    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            setattr(self, key, value)


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_FakeRow]:
        return list(self._rows)

    def fetchone(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """最小可用 fake Session：只覆盖 db_save_scheduler_state / db_load_scheduler_state 调用的语义。

    - INSERT ... ON CONFLICT (workflow) DO UPDATE 被解析成 upsert 到 ``self.rows``
    - SELECT ... FROM governance.workflow_scheduler_state ORDER BY workflow 返回全量
    不需要支持完整 SQL 语义，只要能回放 load/save 的契约即可。
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()
        if sql.startswith("INSERT INTO governance.workflow_scheduler_state"):
            assert params is not None, "upsert 必须带参数"
            workflow = params["workflow"]
            existing = self.rows.get(workflow, {})
            existing.update(params)
            # jsonb 列在真 DB 是 dict；fake 里保留 json 字符串原样即可，load 时解析
            self.rows[workflow] = existing
            return _FakeResult([])
        if sql.startswith("SELECT workflow, initialized_at"):
            # load 路径
            ordered = sorted(self.rows.values(), key=lambda item: item["workflow"])
            rows = [
                _FakeRow(
                    {
                        "workflow": item.get("workflow"),
                        "initialized_at": item.get("initialized_at"),
                        "last_processed_slot": item.get("last_processed_slot"),
                        "last_action": item.get("last_action"),
                        "last_checked_at": item.get("last_checked_at"),
                        "last_task_id": item.get("last_task_id"),
                        "last_reason": item.get("last_reason"),
                        "schedule": item.get("schedule"),
                        # 在真 DB 里 jsonb 反序列化为 dict；fake 里我们存的是 json_dumps 结果
                        # —— 用正则提取 state_payload 的 JSON 字符串再解码。
                        "state_payload": _decode_state_payload(item.get("state_payload")),
                    }
                )
                for item in ordered
            ]
            return _FakeResult(rows)
        raise AssertionError(f"Unexpected SQL in fake session: {sql[:80]}...")


def _decode_state_payload(raw: Any) -> Any:
    """db_save 时写入的是 json_dumps(dict)；还原为 dict 以模拟 jsonb 列的反序列化。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def test_scheduler_state_roundtrip_preserves_bootstrap_stage() -> None:
    """B1 回归：bootstrap_stage / bootstrap_completed_at 必须跨 save→load 保留。"""

    session = _FakeSession()

    original = {
        "initialized_at": "2026-04-16T12:00:00+00:00",
        "bootstrap_stage": "research_cycle",
        "bootstrap_completed_at": None,
        "workflows": {
            "data_maintenance": {
                "last_action": "bootstrap_completed",
                "last_processed_slot": "2026-04-16T04:00:00+00:00",
            },
            "research_cycle": {
                "last_action": "bootstrap_enqueued",
                "last_processed_slot": None,
            },
        },
    }

    db_save_scheduler_state(session, original)

    # meta 行必须存在，且持有根级 bootstrap 字段
    assert _SCHEDULER_META_WORKFLOW in session.rows, \
        "save 路径必须 upsert sentinel workflow 的 meta 行"

    reloaded = db_load_scheduler_state(session)

    assert reloaded["bootstrap_stage"] == "research_cycle", \
        "bootstrap_stage 必须从 meta 行还原"
    assert reloaded["bootstrap_completed_at"] is None, \
        "bootstrap_completed_at=None 必须忠实还原，不能被 workflows 行污染"
    # workflows 必须仍包含真实 workflow，不含 sentinel
    assert set(reloaded["workflows"].keys()) == {"data_maintenance", "research_cycle"}, \
        "load 结果的 workflows 不能包含 sentinel workflow"
    assert reloaded["workflows"]["data_maintenance"]["last_action"] == "bootstrap_completed"


def test_scheduler_state_roundtrip_preserves_bootstrap_completed_at() -> None:
    """bootstrap 完成后 bootstrap_stage=None、bootstrap_completed_at 有值，两者都必须 roundtrip。"""

    session = _FakeSession()

    original = {
        "initialized_at": "2026-04-16T12:00:00+00:00",
        "bootstrap_stage": None,
        "bootstrap_completed_at": "2026-04-16T13:30:00+00:00",
        "workflows": {
            "governance_cycle": {
                "last_action": "done",
                "last_processed_slot": "2026-04-16T07:00:00+00:00",
            },
        },
    }

    db_save_scheduler_state(session, original)
    reloaded = db_load_scheduler_state(session)

    assert reloaded["bootstrap_stage"] is None
    assert reloaded["bootstrap_completed_at"] == "2026-04-16T13:30:00+00:00", \
        "bootstrap 完成时间戳在 DB-backed 路径必须持久化到 meta 行"


def test_scheduler_state_load_ignores_meta_row_from_workflows() -> None:
    """sentinel 行不能泄漏到 workflows dict；历史残留 meta 行重新 load 仍然安全。"""

    session = _FakeSession()
    # 手动注入两条 meta 行和一条真实 workflow 行，模拟 partial 数据
    session.rows[_SCHEDULER_META_WORKFLOW] = {
        "workflow": _SCHEDULER_META_WORKFLOW,
        "initialized_at": None,
        "last_processed_slot": None,
        "last_action": None,
        "last_checked_at": None,
        "last_task_id": None,
        "last_reason": None,
        "schedule": None,
        "state_payload": json.dumps(
            {"bootstrap_stage": "data_maintenance", "bootstrap_completed_at": None}
        ),
    }
    session.rows["data_maintenance"] = {
        "workflow": "data_maintenance",
        "initialized_at": None,
        "last_processed_slot": None,
        "last_action": "bootstrap_enqueued",
        "last_checked_at": None,
        "last_task_id": None,
        "last_reason": None,
        "schedule": None,
        "state_payload": json.dumps({"last_action": "bootstrap_enqueued"}),
    }

    reloaded = db_load_scheduler_state(session)

    assert reloaded["bootstrap_stage"] == "data_maintenance"
    assert "data_maintenance" in reloaded["workflows"]
    assert _SCHEDULER_META_WORKFLOW not in reloaded["workflows"], \
        "sentinel workflow 绝不能出现在 workflows dict 里"
