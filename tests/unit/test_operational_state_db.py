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
    def __init__(self, rows: list[_FakeRow], rowcount: int | None = None) -> None:
        self._rows = rows
        # rowcount 仿 SQLAlchemy CursorResult：UPDATE / DELETE 返回受影响行数；
        # SELECT / INSERT 默认不依赖 rowcount，保留 len(rows) 的老语义做兜底。
        self.rowcount = len(rows) if rowcount is None else rowcount

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


# =====================================================================
# P0-2 阶段 A：pre_apply_gate_results 业务查询 API
# =====================================================================
# 覆盖 db_record_gate_result / db_get_gate_result_by_run_id /
# db_get_latest_gate_result / db_list_gate_results_for_recommendation /
# db_list_gate_results_for_release。这些 API 是"gate 真源从 JSON 迁到 DB"
# 的读路径契约——任何 SQL 语义回归（查不到最新、JOIN 方向错、payload 丢字段）
# 都会让 apply 链路在 DB-only 模式下炸掉，这里先用 fake session 锁住语义。


from datetime import datetime, timezone

from aats.data_platform.governance.operational_state_db import (
    db_get_gate_result_by_run_id,
    db_get_latest_gate_result,
    db_list_gate_results_for_recommendation,
    db_list_gate_results_for_release,
    db_record_gate_result,
    db_set_gate_result_release_id,
)


class _FakeGateSession:
    """Fake session 覆盖 pre_apply_gate_results + parameter_releases 的最小 SQL 方言。

    支持的 SQL 模式：
      - INSERT INTO governance.pre_apply_gate_results ... ON CONFLICT (gate_run_id)
      - INSERT INTO governance.parameter_releases ...（仅用于测试 setup 数据）
      - SELECT payload, created_at FROM governance.pre_apply_gate_results ...
        WHERE gate_run_id / recommendation_id / release_id / LIMIT

    模拟策略：不做 SQL parsing，而是按 SQL 片段关键字路由；保留 gate_run_id
    的幂等写（覆盖 payload）以便测试 record 的 upsert 语义。
    """

    def __init__(self) -> None:
        self.gates: dict[str, dict[str, Any]] = {}
        self.releases: dict[str, dict[str, Any]] = {}

    def _store_gate(self, params: dict[str, Any]) -> None:
        gate_run_id = params["gate_run_id"]
        payload = params.get("payload")
        if isinstance(payload, str):
            try:
                payload_decoded = json.loads(payload)
            except (TypeError, ValueError):
                payload_decoded = {}
        else:
            payload_decoded = payload or {}

        existing = self.gates.get(gate_run_id)
        new_release_id = params.get("release_id")
        # COALESCE(EXCLUDED.release_id, current) 语义：新参数里 release_id 为 None
        # 时，保留已有回填值，避免 record 重放把 apply 流程回填的 release_id 清掉。
        if new_release_id is None and existing is not None:
            release_id = existing.get("release_id")
        else:
            release_id = new_release_id

        self.gates[gate_run_id] = {
            "gate_run_id": gate_run_id,
            "recommendation_id": params.get("recommendation_id"),
            "release_id": release_id,
            "payload": payload_decoded,
            "created_at": params.get("created_at"),
        }

    def _store_release(self, *, release_id: str, gate_result_ref: str | None = None) -> None:
        self.releases[release_id] = {
            "release_id": release_id,
            "gate_result_ref": gate_result_ref,
        }

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()

        if sql.startswith("INSERT INTO governance.pre_apply_gate_results"):
            assert params is not None
            self._store_gate(params)
            return _FakeResult([])

        if sql.startswith("UPDATE governance.pre_apply_gate_results"):
            # db_set_gate_result_release_id 的回填 UPDATE —— 匹配上的行才改写，
            # 没有对应 gate_run_id 时返回 rowcount=0，让上层按"未命中"处理。
            assert params is not None
            gate_run_id = params["gate_run_id"]
            existing = self.gates.get(gate_run_id)
            if existing is None:
                return _FakeResult([], rowcount=0)
            existing["release_id"] = params.get("release_id")
            return _FakeResult([], rowcount=1)

        if sql.startswith("INSERT INTO governance.parameter_releases"):
            # 测试 H1 端到端时 save_release_history 会触发此路径；记录 release
            # 行以便后续 JOIN 查询走真实数据而非 _store_release 手搭的测试桩。
            assert params is not None
            release_id = params["release_id"]
            self.releases[release_id] = {
                "release_id": release_id,
                "gate_result_ref": params.get("gate_result_ref"),
            }
            return _FakeResult([])

        if (
            "FROM governance.pre_apply_gate_results" in sql
            and "WHERE gate_run_id = :gate_run_id" in sql
            and "JOIN" not in sql
        ):
            assert params is not None
            row = self.gates.get(params["gate_run_id"])
            return _FakeResult([_FakeRow(row)] if row else [])

        if (
            "FROM governance.pre_apply_gate_results" in sql
            and "WHERE release_id = :release_id" in sql
            and "JOIN" not in sql
        ):
            # 阶段 B：按索引列 release_id 直接查，不再走 parameter_releases JOIN。
            assert params is not None
            matched: list[_FakeRow] = [
                _FakeRow(g)
                for g in self.gates.values()
                if g.get("release_id") == params["release_id"]
            ]
            matched.sort(
                key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            limit = int(params.get("limit") or 20)
            return _FakeResult(matched[:limit])

        if (
            "FROM governance.pre_apply_gate_results" in sql
            and "WHERE recommendation_id = :rec_id" in sql
        ):
            assert params is not None
            matched = [
                _FakeRow(g)
                for g in self.gates.values()
                if g["recommendation_id"] == params["rec_id"]
            ]
            matched.sort(
                key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            if "LIMIT 1" in sql:
                return _FakeResult(matched[:1])
            limit = int(params.get("limit") or 20)
            return _FakeResult(matched[:limit])

        raise AssertionError(f"Unexpected SQL in fake gate session: {sql[:80]}...")


def _make_gate_result(
    *,
    gate_run_id: str,
    recommendation_id: str,
    gate_status: str = "pass",
    allow_apply: bool = True,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        "gate_run_id": gate_run_id,
        "recommendation_id": recommendation_id,
        "allow_apply": allow_apply,
        "gate_status": gate_status,
        "total_checks": 3,
        "passed_checks": 3 if allow_apply else 2,
        "checks": [],
        "blocking_reasons": [] if allow_apply else ["rule_x"],
        "warnings": [],
        "created_at": created_at or "2026-04-16T12:00:00+00:00",
    }


def test_record_gate_result_is_upsert_by_run_id() -> None:
    """db_record_gate_result 用 gate_run_id 做幂等键；重复写覆盖 payload。"""
    session = _FakeGateSession()

    first = _make_gate_result(gate_run_id="gate_001", recommendation_id="rec_a", gate_status="pass")
    db_record_gate_result(session, first)
    assert session.gates["gate_001"]["payload"]["gate_status"] == "pass"

    second = _make_gate_result(
        gate_run_id="gate_001",
        recommendation_id="rec_a",
        gate_status="block",
        allow_apply=False,
    )
    db_record_gate_result(session, second)
    assert len(session.gates) == 1, "同一 gate_run_id 不能在表里出现两行"
    assert session.gates["gate_001"]["payload"]["gate_status"] == "block", \
        "后写入的 payload 必须覆盖前一次"


def test_get_gate_result_by_run_id_hit_and_miss() -> None:
    session = _FakeGateSession()
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_042", recommendation_id="rec_x"),
    )

    hit = db_get_gate_result_by_run_id(session, "gate_042")
    assert hit is not None
    assert hit["gate_run_id"] == "gate_042"
    assert hit["recommendation_id"] == "rec_x"

    miss = db_get_gate_result_by_run_id(session, "gate_999")
    assert miss is None


def test_get_latest_gate_result_returns_most_recent_for_recommendation() -> None:
    """同一 rec 跑多轮 gate，latest 必须按 created_at 降序取第一条。"""
    session = _FakeGateSession()
    db_record_gate_result(
        session,
        _make_gate_result(
            gate_run_id="gate_early",
            recommendation_id="rec_k",
            created_at="2026-04-15T08:00:00+00:00",
        ),
    )
    db_record_gate_result(
        session,
        _make_gate_result(
            gate_run_id="gate_mid",
            recommendation_id="rec_k",
            created_at="2026-04-15T12:00:00+00:00",
        ),
    )
    db_record_gate_result(
        session,
        _make_gate_result(
            gate_run_id="gate_late",
            recommendation_id="rec_k",
            gate_status="block",
            allow_apply=False,
            created_at="2026-04-15T16:00:00+00:00",
        ),
    )
    # 噪声：别的 rec 也有数据，不应该被返回
    db_record_gate_result(
        session,
        _make_gate_result(
            gate_run_id="gate_other",
            recommendation_id="rec_other",
            created_at="2026-04-15T23:00:00+00:00",
        ),
    )

    latest = db_get_latest_gate_result(session, recommendation_id="rec_k")
    assert latest is not None
    assert latest["gate_run_id"] == "gate_late", \
        "latest 必须忠实于 rec_k 的最新 created_at，不能被其它 rec 污染"
    assert latest["allow_apply"] is False


def test_get_latest_gate_result_returns_none_when_no_history() -> None:
    session = _FakeGateSession()
    assert db_get_latest_gate_result(session, recommendation_id="rec_empty") is None


def test_list_gate_results_for_recommendation_orders_desc_and_limits() -> None:
    session = _FakeGateSession()
    for idx, ts in enumerate(
        ["2026-04-15T08:00:00+00:00", "2026-04-15T09:00:00+00:00", "2026-04-15T10:00:00+00:00"]
    ):
        db_record_gate_result(
            session,
            _make_gate_result(
                gate_run_id=f"gate_{idx}",
                recommendation_id="rec_z",
                created_at=ts,
            ),
        )

    rows = db_list_gate_results_for_recommendation(
        session, recommendation_id="rec_z", limit=2
    )
    assert [r["gate_run_id"] for r in rows] == ["gate_2", "gate_1"], \
        "list 必须按 created_at 降序，最新在前；limit 必须生效"


def test_list_gate_results_for_release_uses_release_id_column() -> None:
    """阶段 B：直接按 pre_apply_gate_results.release_id 索引列查询。

    回填由 save_release_history 在 release upsert 同事务里通过
    db_set_gate_result_release_id 完成。没被回填的 gate 行不会混进结果。
    """
    session = _FakeGateSession()
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_for_release", recommendation_id="rec_r"),
    )
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_unrelated", recommendation_id="rec_r"),
    )
    # 模拟 save_release_history 的回填步骤
    db_set_gate_result_release_id(
        session, gate_run_id="gate_for_release", release_id="rel_abc",
    )

    rows = db_list_gate_results_for_release(session, release_id="rel_abc", limit=10)
    assert len(rows) == 1
    assert rows[0]["gate_run_id"] == "gate_for_release", \
        "只返回 release_id 匹配的 gate，不能把同 recommendation 的其它 gate 也带出来"


def test_list_gate_results_for_release_skips_unbackfilled_rows() -> None:
    """legacy gate 行没被回填 release_id → 不出现在结果里。

    这是阶段 B 切换到索引列后的预期行为：调用方需要在 legacy 场景下
    自行回落到 db_get_latest_gate_result(recommendation_id=...)。
    """
    session = _FakeGateSession()
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_only", recommendation_id="rec_q"),
    )
    # 从未回填 release_id → release 维度查不到
    assert db_list_gate_results_for_release(session, release_id="rel_missing") == []


# =====================================================================
# P0-2 阶段 E：release_id 回填 + COALESCE 保护
# =====================================================================
# H2：db_set_gate_result_release_id 的直接合同（hit / miss）。
# M2：db_upsert_pre_apply_gate_result 重放时必须用 COALESCE 保住已有 release_id。
# H1：release_registry.save_release_history 必须在同一事务里触发回填。


def test_set_gate_result_release_id_hit_returns_true() -> None:
    """有对应 gate_run_id 时 UPDATE 生效，并把 release_id 写进去。"""
    session = _FakeGateSession()
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_hit", recommendation_id="rec_h"),
    )
    assert session.gates["gate_hit"]["release_id"] is None, \
        "初次 record 的 gate 不应该带 release_id（apply 还没发生）"

    updated = db_set_gate_result_release_id(
        session, gate_run_id="gate_hit", release_id="rel_xyz",
    )
    assert updated is True, "命中必须返回 True"
    assert session.gates["gate_hit"]["release_id"] == "rel_xyz"


def test_set_gate_result_release_id_miss_returns_false() -> None:
    """gate_run_id 不存在时返回 False，不创建孤立行。"""
    session = _FakeGateSession()
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_only", recommendation_id="rec_o"),
    )

    updated = db_set_gate_result_release_id(
        session, gate_run_id="gate_absent", release_id="rel_abc",
    )
    assert updated is False, "未命中必须返回 False"
    assert "gate_absent" not in session.gates, \
        "UPDATE 不命中时绝不能副作用地创建新行"
    assert session.gates["gate_only"]["release_id"] is None, \
        "未命中的 UPDATE 不能溅到其它 gate 行"


def test_record_gate_result_replay_preserves_backfilled_release_id() -> None:
    """M2：record 重放时 payload 可以换（gate 重跑），但 release_id
    一旦被 apply 流程回填过，就不能被一次不带 release_id 的 record 清掉。

    COALESCE(EXCLUDED.release_id, current) 就是这条不变量的 SQL 化身。
    """
    session = _FakeGateSession()
    # 第一次 record：apply 还没发生，release_id 为 None
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_coalesce", recommendation_id="rec_c"),
    )
    # apply 成功后回填 release_id
    db_set_gate_result_release_id(
        session, gate_run_id="gate_coalesce", release_id="rel_locked",
    )
    assert session.gates["gate_coalesce"]["release_id"] == "rel_locked"

    # gate 重跑 / payload 重放（常见于手动补跑或 schema 迁移后 backfill）——
    # payload 会覆盖，但 release_id 必须仍然是 rel_locked
    db_record_gate_result(
        session,
        _make_gate_result(
            gate_run_id="gate_coalesce",
            recommendation_id="rec_c",
            gate_status="block",
            allow_apply=False,
        ),
    )
    assert session.gates["gate_coalesce"]["payload"]["gate_status"] == "block", \
        "重放必须更新 payload 的 gate_status"
    assert session.gates["gate_coalesce"]["release_id"] == "rel_locked", \
        "COALESCE 保护：重放时 release_id 为 None，不能把已有回填清掉"


class _SessionContextAdapter:
    """把 _FakeGateSession 适配成 `with Session(engine) as s, s.begin():` 协议。"""

    def __init__(self, inner: _FakeGateSession) -> None:
        self._inner = inner

    def __enter__(self) -> _FakeGateSession:
        return self._inner

    def __exit__(self, *exc: Any) -> bool:
        return False


class _TxCtx:
    def __enter__(self) -> _TxCtx:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeEngine:
    def dispose(self) -> None:
        return None


def test_save_release_history_backfills_gate_release_id_in_same_transaction(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """H1：save_release_history 把 release upsert 到 DB 后，必须把 release_id
    回填到对应 gate row。回填和 release upsert 共用同一事务：DB 失败时两者一起
    回滚，不会留下"release 已 commit 但 gate row 未回填"的 half-state。
    """
    from aats.data_platform.production_workflow import release_registry as rr

    session = _FakeGateSession()
    # 场景：两条 gate 行
    #   gate_live    — 有 release 配对，应被回填
    #   gate_orphan  — 没有 release 配对，不应被污染
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_live", recommendation_id="rec_live"),
    )
    db_record_gate_result(
        session,
        _make_gate_result(gate_run_id="gate_orphan", recommendation_id="rec_other"),
    )
    session.begin = lambda: _TxCtx()  # type: ignore[method-assign]

    monkeypatch.setattr(rr, "try_governance_db", lambda: (_FakeEngine(), True))
    monkeypatch.setattr(
        rr, "Session", lambda _engine: _SessionContextAdapter(session),
    )

    history = {
        "releases": [
            {
                "release_id": "rel_live",
                "family": "fam",
                "timeframe": "1h",
                "combo_key": "fam_1h",
                "recommendation_id": "rec_live",
                "parameter_set_id": "ps_1",
                "gate_result_ref": "gate_live",
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
            {
                # 手工 release（没跑 gate）：不应该触发回填
                "release_id": "rel_no_gate",
                "family": "fam",
                "timeframe": "1h",
                "combo_key": "fam_1h",
                "recommendation_id": "rec_manual",
                "parameter_set_id": "ps_2",
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
        ],
    }
    rr.save_release_history(history, tmp_path)

    assert session.gates["gate_live"]["release_id"] == "rel_live", \
        "save_release_history 必须在同一事务里把 release_id 回填到 gate row"
    assert session.gates["gate_orphan"]["release_id"] is None, \
        "没有 release 配对的 gate 行不能被污染"
    # parameter_releases INSERT 也确实落到 fake session 了（两条）
    assert set(session.releases.keys()) == {"rel_live", "rel_no_gate"}


def test_save_release_history_logs_warning_when_gate_row_missing(
    monkeypatch: Any, tmp_path: Any, caplog: Any
) -> None:
    """gate_run_id 在 DB 里没命中时，save_release_history 不应炸，只打 warning。

    动机：单机 / 历史 release 场景下 gate 表可能没写入；release 流程本身不能
    因为"找不到 gate 行"就阻塞后续所有 release 落库。
    """
    import logging

    from aats.data_platform.production_workflow import release_registry as rr

    session = _FakeGateSession()
    session.begin = lambda: _TxCtx()  # type: ignore[method-assign]

    monkeypatch.setattr(rr, "try_governance_db", lambda: (_FakeEngine(), True))
    monkeypatch.setattr(
        rr, "Session", lambda _engine: _SessionContextAdapter(session),
    )

    history = {
        "releases": [
            {
                "release_id": "rel_x",
                "family": "fam",
                "timeframe": "1h",
                "combo_key": "fam_1h",
                "recommendation_id": "rec_x",
                "parameter_set_id": "ps_x",
                "gate_result_ref": "gate_missing",  # 没 record 过 → UPDATE miss
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
        ],
    }

    with caplog.at_level(logging.WARNING, logger=rr.__name__):
        rr.save_release_history(history, tmp_path)

    assert "rel_x" in session.releases, "release 本身必须 upsert 成功"
    assert any(
        "gate_missing" in rec.getMessage() for rec in caplog.records
    ), "gate 行缺失时应当有 warning 可见"


# H-R1 回归：A-0.3 DB 降级清扫后，save_release_history 在 DB 不可达时必须抛
# DBUnavailableError；原 AATS_P0_RELEASE_FAIL_LOUD 环境开关已废除，所有调用方
# 走统一 fail-loud 路径，不再允许"仅写 JSON 假装成功"。


def test_save_release_history_raises_when_db_unavailable(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """A-0.3：DB 不可达必须抛 DBUnavailableError，且绝不写 JSON 副本。

    动机：JSON 副本只是审计视图，真源永远是 DB；若 DB 未 commit 就写 JSON，
    下一次 loader fallback 会把"从未入库的 ghost release"重新注入系统（上一次
    split-brain 事故的根因）。历史上用 AATS_P0_RELEASE_FAIL_LOUD env-var 让这
    条硬纪律变成可选开关，结果线下/测试环境长期走降级路径、production gap 藏到
    事故发生才暴露；A-0.3 把开关拿掉，强制所有调用方统一行为。
    """
    from aats.data_platform.governance._exceptions import DBUnavailableError
    from aats.data_platform.production_workflow import release_registry as rr

    monkeypatch.setattr(rr, "try_governance_db", lambda: (None, False))

    history = {
        "releases": [
            {
                "release_id": "rel_unavail",
                "family": "fam",
                "timeframe": "1h",
                "combo_key": "fam_1h",
                "recommendation_id": "rec_unavail",
                "parameter_set_id": "ps_unavail",
                "apply_result": "success",
                "observation_status": "observing",
                "observation_window_hours": 24,
            },
        ],
    }

    import pytest

    with pytest.raises(DBUnavailableError):
        rr.save_release_history(history, tmp_path)

    path = tmp_path / "artifacts/production_workflow/parameter_release_history.json"
    assert not path.exists(), "DB 不可达时不得写 JSON 副本，避免产生 ghost release"


# M-R2 回归：load_release_history 必须在返回体上打 source / stale 标记。


def test_load_release_history_marks_db_source_when_db_ok(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """DB 读成功时 source=db, stale=False。"""
    from aats.data_platform.production_workflow import release_registry as rr

    fake_history = {"generated_at": "t", "releases": [{"release_id": "r1"}]}

    def _fake_db_load(_session: Any) -> dict[str, Any]:
        return fake_history

    import aats.data_platform.governance.operational_state_db as osd

    monkeypatch.setattr(rr, "try_governance_db", lambda: (_FakeEngine(), True))
    monkeypatch.setattr(
        rr, "Session", lambda _engine: _SessionContextAdapter(_FakeGateSession()),
    )
    monkeypatch.setattr(osd, "db_load_release_history", _fake_db_load)

    result = rr.load_release_history(tmp_path)
    assert result["source"] == "db"
    assert result["stale"] is False
    assert result["releases"] == [{"release_id": "r1"}]


def test_load_release_history_marks_json_source_when_db_unreachable(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """DB 不可达 + JSON 存在 → source=json, stale=True, 附 stale_reason。

    M-R2 核心契约：消费方（rdp_control_summary / UI）必须能看出这次数据
    来自副本文件，不能被当成实时真源处理。
    """
    from aats.data_platform.production_workflow import release_registry as rr

    # 先写一个文件副本模拟"上次 DB 可达时保存的 JSON"
    hist_path = tmp_path / "artifacts/production_workflow/parameter_release_history.json"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist_path.write_text(
        json.dumps({"generated_at": "old", "releases": [{"release_id": "rel_old"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(rr, "try_governance_db", lambda: (None, False))

    result = rr.load_release_history(tmp_path)
    assert result["source"] == "json", "DB 不可达必须把 source 标成 json"
    assert result["stale"] is True, "JSON 副本必须显式 stale=True"
    assert result.get("stale_reason") == "db_unreachable"
    assert result["releases"] == [{"release_id": "rel_old"}]


def test_load_release_history_marks_empty_source_when_db_unreachable_and_no_json(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """DB 不可达 + 无 JSON 文件 → source=empty, stale=False（冷启动，没有 stale 数据可污染）。"""
    from aats.data_platform.production_workflow import release_registry as rr

    monkeypatch.setattr(rr, "try_governance_db", lambda: (None, False))

    result = rr.load_release_history(tmp_path)
    assert result["source"] == "empty"
    assert result["stale"] is False
    assert result["releases"] == []
