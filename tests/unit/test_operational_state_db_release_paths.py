"""M4/M5/M6 回归：parameter_releases 路径的 SQL 契约。

不依赖 testcontainers；用一个只解析 SQL 前缀 + 记录参数的 fake Session 锁定：

- M4 db_set_gate_result_release_id：
    * 行不存在 → 返回 False，不发 UPDATE
    * 行存在但 release_id 已经等于目标值 → 返回 True，不发 UPDATE（避免 WAL 追加）
    * 行存在且 release_id 不同 → 发 UPDATE，返回 True

- M5 db_upsert_parameter_release：
    * ``timeframe`` 列与 ``payload.timeframe`` 归一到小写
    * 未传 ``combo_key`` 时按 ``family_timeframe`` 自动生成并小写

- M6 db_update_parameter_release_status：
    * 同时给 apply_result + observation_status → 发一条 UPDATE ... RETURNING，命中 → True
    * 行不存在 → UPDATE 不 RETURNING → False
    * caller 两个参数都不给 → 不发 UPDATE，只发 SELECT 1 存在性校验

真实 Postgres 的 jsonb_set 行为由 WSL2 testcontainers 层兜底；这里锁定的是
Python 侧的 SQL 构造路径 / 归一化逻辑 / 返回值语义。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aats.data_platform.governance.operational_state_db import (
    db_set_gate_result_release_id,
    db_update_parameter_release_status,
    db_upsert_observation_result,
    db_upsert_parameter_release,
    db_upsert_release_effectiveness,
    db_upsert_rollback_recommendation,
)


# =====================================================================
# Fake Session
# =====================================================================


class _FakeRow:
    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            setattr(self, key, value)


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[_FakeRow]:
        return list(self._rows)


class _FakeSession:
    """只覆盖本文件触达的三条 SQL 形态：
      * SELECT release_id FROM governance.pre_apply_gate_results WHERE gate_run_id = ...
      * INSERT INTO governance.parameter_releases ... ON CONFLICT ...
      * UPDATE governance.pre_apply_gate_results / parameter_releases ...
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        # gate_run_id -> row
        self.gate_rows: dict[str, dict[str, Any]] = {}
        # release_id -> row
        self.release_rows: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()
        self.statements.append((sql, dict(params or {})))

        if sql.startswith("SELECT release_id"):
            gate_run_id = (params or {}).get("gate_run_id")
            row = self.gate_rows.get(gate_run_id)
            if row is None:
                return _FakeResult([])
            return _FakeResult([_FakeRow({"release_id": row.get("release_id")})])

        if sql.startswith("UPDATE governance.pre_apply_gate_results"):
            gate_run_id = (params or {}).get("gate_run_id")
            row = self.gate_rows.setdefault(gate_run_id, {})
            row["release_id"] = (params or {}).get("release_id")
            row["updated_at"] = (params or {}).get("updated_at")
            return _FakeResult([])

        if sql.startswith("INSERT INTO governance.parameter_releases"):
            release_id = (params or {}).get("release_id")
            self.release_rows[release_id] = dict(params or {})
            return _FakeResult([])

        if sql.startswith("INSERT INTO governance.observation_results"):
            return _FakeResult([])

        if sql.startswith("INSERT INTO governance.rollback_recommendations"):
            return _FakeResult([])

        if sql.startswith("INSERT INTO governance.release_effectiveness"):
            return _FakeResult([])

        if sql.startswith("SELECT 1 AS present"):
            release_id = (params or {}).get("release_id")
            if release_id in self.release_rows:
                return _FakeResult([_FakeRow({"present": 1})])
            return _FakeResult([])

        if sql.startswith("UPDATE governance.parameter_releases"):
            release_id = (params or {}).get("release_id")
            row = self.release_rows.get(release_id)
            if row is None:
                return _FakeResult([])
            # 模拟 jsonb_set：把 apply_result / observation_status 就地更新
            apply_result = (params or {}).get("apply_result")
            obs_status = (params or {}).get("observation_status")
            if apply_result is not None:
                row["apply_result"] = apply_result
            if obs_status is not None:
                row["observation_status"] = obs_status
            row["updated_at"] = (params or {}).get("updated_at")
            return _FakeResult([_FakeRow({"release_id": release_id})])

        raise AssertionError(f"Unexpected SQL: {sql[:80]}...")


# =====================================================================
# M4：db_set_gate_result_release_id
# =====================================================================


def test_m4_gate_row_missing_returns_false_without_update() -> None:
    """行不存在 → caller 收到 False，不应发出 UPDATE。"""
    session = _FakeSession()

    ok = db_set_gate_result_release_id(
        session, gate_run_id="gate_nonexistent", release_id="rel_x",
    )

    assert ok is False
    # 只发了一条 SELECT，没有 UPDATE
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["SELECT"], (
        f"行不存在时不应发 UPDATE，实际 statements: {sql_types}"
    )


def test_m4_gate_row_already_matches_skips_update() -> None:
    """行存在且 release_id 已经是目标值 → 返回 True，但不发 UPDATE。

    动机：save_release_history 会在每个 release 行回写 release_id，若已经是
    目标值再 UPDATE 会污染 updated_at 并追加 WAL，毫无价值。
    """
    session = _FakeSession()
    session.gate_rows["gate_1"] = {"release_id": "rel_target"}

    ok = db_set_gate_result_release_id(
        session, gate_run_id="gate_1", release_id="rel_target",
    )

    assert ok is True
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["SELECT"], (
        "release_id 已是目标值时应跳过 UPDATE，只发 SELECT"
    )
    # 行的 release_id 没有被改写（证明没跑 UPDATE）
    assert session.gate_rows["gate_1"].get("updated_at") is None


def test_m4_gate_row_different_release_triggers_update() -> None:
    """行存在但 release_id 与目标不同 → 正常发 UPDATE 并返回 True。"""
    session = _FakeSession()
    session.gate_rows["gate_1"] = {"release_id": None}  # 历史上没有回填过

    ok = db_set_gate_result_release_id(
        session, gate_run_id="gate_1", release_id="rel_new",
    )

    assert ok is True
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["SELECT", "UPDATE"]
    assert session.gate_rows["gate_1"]["release_id"] == "rel_new"


# =====================================================================
# M5：db_upsert_parameter_release payload 归一
# =====================================================================


def _find_upsert_params(session: _FakeSession) -> dict[str, Any]:
    for sql, params in session.statements:
        if sql.startswith("INSERT INTO governance.parameter_releases"):
            return params
    raise AssertionError("未找到 parameter_releases 的 INSERT 语句")


def test_m5_timeframe_normalized_in_both_column_and_payload() -> None:
    """caller 传 timeframe='1H' → column 与 payload.timeframe 都必须落成 '1h'。"""
    session = _FakeSession()

    db_upsert_parameter_release(
        session,
        {
            "release_id": "rel_a",
            "family": "directional",
            "timeframe": "1H",
            "combo_key": "Directional_1H",
            "recommendation_id": "rec_a",
            "parameter_set_id": "ps_1",
            "previous_parameter_set_id": None,
            "actor": "operator",
            "gate_result_ref": None,
            "gate_status": "pass",
            "apply_result": "pending",
            "observation_status": "pending",
            "observation_window_hours": 24,
            "notes": None,
            "created_at": None,
        },
    )

    params = _find_upsert_params(session)
    assert params["timeframe"] == "1h", "column 必须归一到小写"
    assert params["combo_key"] == "directional_1h", "combo_key 必须归一到小写"

    payload = json.loads(params["payload"])
    assert payload["timeframe"] == "1h", (
        "payload JSON 中的 timeframe 也必须归一；否则从 payload 反序列化的读者"
        "与列值拿到的会不一致"
    )
    assert payload["combo_key"] == "directional_1h"


def test_m5_combo_key_inferred_from_family_timeframe_when_missing() -> None:
    """未传 combo_key → 用 family_timeframe 自动合成并小写。"""
    session = _FakeSession()

    db_upsert_parameter_release(
        session,
        {
            "release_id": "rel_b",
            "family": "Independent",
            "timeframe": "15M",
            # 故意省略 combo_key
            "recommendation_id": "rec_b",
            "parameter_set_id": "ps_2",
            "previous_parameter_set_id": None,
            "actor": "operator",
            "gate_result_ref": None,
            "gate_status": "pass",
            "apply_result": "pending",
            "observation_status": "pending",
            "observation_window_hours": 48,
            "notes": None,
            "created_at": None,
        },
    )

    params = _find_upsert_params(session)
    assert params["combo_key"] == "independent_15m"
    payload = json.loads(params["payload"])
    assert payload["combo_key"] == "independent_15m"


# =====================================================================
# M6：db_update_parameter_release_status → UPDATE RETURNING
# =====================================================================


def test_m6_update_with_both_fields_emits_single_update_returning() -> None:
    """apply_result + observation_status 都给 → 一条 UPDATE ... RETURNING 就够。

    旧实现是 SELECT → 整行重写（INSERT ... ON CONFLICT）两条 SQL + 全列覆盖。
    新实现必须只发一条 UPDATE；确保 statements 里只有这一条、且命中返回 True。
    """
    session = _FakeSession()
    session.release_rows["rel_1"] = {
        "release_id": "rel_1",
        "apply_result": "pending",
        "observation_status": "pending",
    }

    ok = db_update_parameter_release_status(
        session, "rel_1",
        apply_result="success",
        observation_status="rollback_recommended",
    )

    assert ok is True
    # 整个调用只应发一条 UPDATE，无 SELECT、无 INSERT
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["UPDATE"], (
        f"命中路径必须只发一条 UPDATE ... RETURNING，实际: {sql_types}"
    )
    # 行被 in-place 更新，而不是整行覆盖
    assert session.release_rows["rel_1"]["apply_result"] == "success"
    assert session.release_rows["rel_1"]["observation_status"] == "rollback_recommended"


def test_m6_update_not_found_returns_false() -> None:
    """release_id 不存在 → UPDATE 没有 RETURNING → False。"""
    session = _FakeSession()

    ok = db_update_parameter_release_status(
        session, "rel_missing", apply_result="success",
    )

    assert ok is False


def test_m6_noop_call_does_not_emit_update() -> None:
    """caller 两个参数都不给 → 只跑存在性校验（SELECT 1），不发 UPDATE。

    动机：允许"单纯确认行还在"的 caller 而不污染 updated_at。
    """
    session = _FakeSession()
    session.release_rows["rel_1"] = {"release_id": "rel_1"}

    ok = db_update_parameter_release_status(session, "rel_1")

    assert ok is True
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["SELECT"], (
        f"无变更时不应发 UPDATE，实际: {sql_types}"
    )


def test_m6_noop_call_missing_row_returns_false() -> None:
    """无参数 + 行不存在 → 存在性检查返回 False。"""
    session = _FakeSession()

    ok = db_update_parameter_release_status(session, "rel_missing")

    assert ok is False


# =====================================================================
# M5 扩展：observation_results / rollback_recommendations /
# release_effectiveness 三张表同样要做 column↔payload 归一
# =====================================================================
#
# 历史 bug：旧实现只 lower() 了 column，payload 仍直接 json_dumps(result)，
# caller 传 "1H" 时 column="1h" 但 payload.timeframe="1H"；下游读 payload
# 反序列化就会和列值对不上。


def _find_insert_params(session: _FakeSession, table: str) -> dict[str, Any]:
    prefix = f"INSERT INTO governance.{table}"
    for sql, params in session.statements:
        if sql.startswith(prefix):
            return params
    raise AssertionError(f"未找到 {table} 的 INSERT 语句")


def test_m5_observation_result_timeframe_and_combo_key_normalized() -> None:
    """db_upsert_observation_result：column 和 payload 的 timeframe/combo_key 都必须归一。"""
    session = _FakeSession()

    db_upsert_observation_result(
        session,
        {
            "release_id": "rel_o1",
            "family": "Directional",
            "timeframe": "1H",
            "combo_key": "Directional_1H",
            "status": "active",
            "recommendation": "review",
            "observation_window_hours": 24,
            "window_active": True,
            "started_at": None,
            "evaluated_at": None,
        },
    )

    params = _find_insert_params(session, "observation_results")
    assert params["timeframe"] == "1h"
    assert params["combo_key"] == "directional_1h"
    payload = json.loads(params["payload"])
    assert payload["timeframe"] == "1h", (
        "observation_results.payload.timeframe 必须与列归一一致，"
        "否则读者从 JSON 反序列化会拿到 '1H' 与列 '1h' 冲突"
    )
    assert payload["combo_key"] == "directional_1h"


def test_m5_observation_result_combo_key_inferred_when_missing() -> None:
    """未传 combo_key → 由 family_timeframe 合成。"""
    session = _FakeSession()

    db_upsert_observation_result(
        session,
        {
            "release_id": "rel_o2",
            "family": "Independent",
            "timeframe": "15M",
            "status": "active",
            "recommendation": "review",
        },
    )

    params = _find_insert_params(session, "observation_results")
    assert params["combo_key"] == "independent_15m"
    payload = json.loads(params["payload"])
    assert payload["combo_key"] == "independent_15m"


def test_m5_rollback_recommendation_timeframe_and_combo_key_normalized() -> None:
    """db_upsert_rollback_recommendation：column + payload 都归一。"""
    session = _FakeSession()

    db_upsert_rollback_recommendation(
        session,
        {
            "release_id": "rel_r1",
            "family": "Directional",
            "timeframe": "4H",
            "combo_key": "Directional_4H",
            "rollback_recommended": True,
            "severity": "high",
            "suggested_target_parameter_set_id": "ps_prev",
        },
    )

    params = _find_insert_params(session, "rollback_recommendations")
    assert params["timeframe"] == "4h"
    assert params["combo_key"] == "directional_4h"
    payload = json.loads(params["payload"])
    assert payload["timeframe"] == "4h"
    assert payload["combo_key"] == "directional_4h"


def test_m5_rollback_recommendation_combo_key_inferred_when_missing() -> None:
    session = _FakeSession()

    db_upsert_rollback_recommendation(
        session,
        {
            "release_id": "rel_r2",
            "family": "Contrarian",
            "timeframe": "30M",
            "rollback_recommended": False,
            "severity": "none",
        },
    )

    params = _find_insert_params(session, "rollback_recommendations")
    assert params["combo_key"] == "contrarian_30m"
    payload = json.loads(params["payload"])
    assert payload["combo_key"] == "contrarian_30m"


def test_m5_release_effectiveness_timeframe_normalized_in_column_and_payload() -> None:
    """db_upsert_release_effectiveness：列里没 combo_key，但 payload 的
    timeframe 必须归一；否则读 JSON 的 metrics 层会与 parameter_releases
    的 combo_key 拼不起来。
    """
    session = _FakeSession()

    db_upsert_release_effectiveness(
        session,
        {
            "evaluation_id": "eval_1",
            "release_id": "rel_e1",
            "family": "Directional",
            "timeframe": "1H",
            "conclusion": "positive",
            "evaluated_at": None,
        },
    )

    params = _find_insert_params(session, "release_effectiveness")
    assert params["timeframe"] == "1h"
    payload = json.loads(params["payload"])
    assert payload["timeframe"] == "1h", (
        "release_effectiveness.payload.timeframe 必须与列归一一致"
    )


def test_m5_release_effectiveness_blank_timeframe_becomes_null_column() -> None:
    """caller 没传 timeframe → 列落 NULL，payload 里保留 '' 以免破坏 JSON 结构。
    （该表历史上允许 timeframe 为 NULL，防止 bind 层报错。）
    """
    session = _FakeSession()

    db_upsert_release_effectiveness(
        session,
        {
            "evaluation_id": "eval_2",
            "release_id": "rel_e2",
            "family": "Directional",
            "conclusion": "negative",
        },
    )

    params = _find_insert_params(session, "release_effectiveness")
    assert params["timeframe"] is None, (
        "caller 没传 timeframe 时列应保持 NULL，不要改成 '' "
        "否则 UNIQUE index 语义和历史兼容被破坏"
    )
