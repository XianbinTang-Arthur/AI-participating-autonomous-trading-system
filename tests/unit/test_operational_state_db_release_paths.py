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
from datetime import datetime, timezone
from typing import Any

import pytest


from aats.data_platform.governance.operational_state_db import (
    _merge_release_effectiveness_state,
    db_get_completed_operator_rollback_fact,
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
        self.effectiveness_rows: dict[str, dict[str, Any]] = {}
        self.apply_proof: dict[str, Any] | None = None
        self.rollback_proof: dict[str, Any] | None = None
        self.effectiveness_capital_proof: dict[str, Any] | None = None
        self.effectiveness_action_proofs: list[dict[str, Any]] = []

    # ------------------------------------------------------------------

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()
        self.statements.append((sql, dict(params or {})))

        if "AS lifecycle_matches" in sql and "AS history_matches" in sql:
            return _FakeResult(
                [_FakeRow(self.apply_proof)] if self.apply_proof is not None else []
            )

        if (
            "AS active_matches" in sql
            and "AS history_matches" in sql
            and "AS lifecycle_matches" not in sql
        ):
            return _FakeResult(
                [_FakeRow(self.rollback_proof)]
                if self.rollback_proof is not None
                else []
            )

        if "release_apply_result" in sql and "history_actor" in sql:
            return _FakeResult(
                [_FakeRow(self.effectiveness_capital_proof)]
                if self.effectiveness_capital_proof is not None
                else []
            )

        if sql.startswith(
            "INSERT INTO governance.release_effectiveness_action_proofs"
        ):
            proof = dict(params or {})
            self.effectiveness_action_proofs.append(proof)
            return _FakeResult([_FakeRow({"release_id": proof["release_id"]})])

        if (
            "FROM governance.parameter_releases" in sql
            and "FOR UPDATE" in sql
        ):
            release_id = (params or {}).get("release_id")
            stored = self.release_rows.get(release_id)
            if stored is None:
                return _FakeResult([])
            payload = stored.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            return _FakeResult([_FakeRow({
                **stored,
                "payload": payload or {},
                "created_at": stored.get("created_at"),
                "updated_at": stored.get("updated_at"),
            })])

        if (
            "FROM governance.release_effectiveness" in sql
            and "FOR UPDATE" in sql
        ):
            release_id = (params or {}).get("release_id")
            stored = self.effectiveness_rows.get(release_id)
            if stored is None:
                return _FakeResult([])
            payload = stored.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            return _FakeResult([_FakeRow({
                **stored,
                "payload": payload or {},
                "updated_at": stored.get("updated_at"),
            })])

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
            release_id = (params or {}).get("release_id")
            self.effectiveness_rows[release_id] = dict(params or {})
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
            if "payload = CAST(:payload AS jsonb)" in sql:
                row.update(dict(params or {}))
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

        if sql.startswith("UPDATE governance.release_effectiveness"):
            release_id = (params or {}).get("release_id")
            row = self.effectiveness_rows.get(release_id)
            if row is None:
                return _FakeResult([])
            row.update(dict(params or {}))
            return _FakeResult([])

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


def _seed_successful_release(session: _FakeSession, release_id: str) -> None:
    created_at = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    payload = {
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "recommendation_id": "rec_release",
        "parameter_set_id": "ps_release",
        "previous_parameter_set_id": "ps_previous",
        "actor": "operator",
        "gate_result_ref": "gate_release",
        "gate_status": "pass",
        "apply_result": "success",
        "apply_operation_id": "op_apply_release",
        "applied_at": "2026-08-27T09:01:00+00:00",
        "observation_status": "observing",
        "observation_window_hours": 24,
        "created_at": created_at.isoformat(),
    }
    session.release_rows[release_id] = {
        **payload,
        "payload": payload,
        "notes": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_release_rollback_attestation_is_owned_by_proof_writer() -> None:
    session = _FakeSession()
    release_id = "rel_release_proven"
    _seed_successful_release(session, release_id)
    session.rollback_proof = {"active_matches": True, "history_matches": True}

    merged = db_upsert_parameter_release(
        session,
        {
            "release_id": release_id,
            "family": "independent",
            "timeframe": "15m",
            "combo_key": "independent_15m",
            "recommendation_id": "rec_release",
            "parameter_set_id": "ps_release",
            "previous_parameter_set_id": "ps_previous",
            "apply_result": "success",
            "observation_status": "rolled_back",
            "rolled_back_at": "2026-08-27T10:03:00+00:00",
            "rollback_to_parameter_set_id": "ps_previous",
            "rollback_operation_id": "op_rollback_release",
            # Caller claims are stripped; only the successful DB proof below
            # may add these exact values.
            "rollback_capital_proof_version": "forged/v9",
            "rollback_capital_proof_verified": True,
        },
        allow_rollback_transition=True,
    )

    assert merged["rollback_capital_proof_version"] == (
        "rdp-release-rollback-capital-proof/v1"
    )
    assert merged["rollback_capital_proof_verified"] is True


def test_release_rollback_forged_attestation_cannot_bypass_capital_proof() -> None:
    session = _FakeSession()
    release_id = "rel_release_forged"
    _seed_successful_release(session, release_id)
    session.rollback_proof = {"active_matches": False, "history_matches": False}

    with pytest.raises(ValueError, match="exact canonical capital lineage"):
        db_upsert_parameter_release(
            session,
            {
                "release_id": release_id,
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "recommendation_id": "rec_release",
                "parameter_set_id": "ps_release",
                "previous_parameter_set_id": "ps_previous",
                "apply_result": "success",
                "observation_status": "rolled_back",
                "rolled_back_at": "2026-08-27T10:03:00+00:00",
                "rollback_to_parameter_set_id": "ps_previous",
                "rollback_operation_id": "op_rollback_release",
                "rollback_capital_proof_version": (
                    "rdp-release-rollback-capital-proof/v1"
                ),
                "rollback_capital_proof_verified": True,
            },
            allow_rollback_transition=True,
        )


# =====================================================================
# M6：legacy partial status writer is read-only
# =====================================================================


def test_m6_partial_status_mutation_is_rejected_without_sql() -> None:
    """Capital lifecycle state cannot bypass proof-bearing writers."""
    session = _FakeSession()
    session.release_rows["rel_1"] = {
        "release_id": "rel_1",
        "apply_result": "pending",
        "observation_status": "pending",
    }

    with pytest.raises(ValueError, match="partial parameter release"):
        db_update_parameter_release_status(
            session,
            "rel_1",
            apply_result="success",
            observation_status="rollback_recommended",
        )
    assert session.statements == []


def test_m6_partial_status_mutation_missing_row_is_still_rejected() -> None:
    """Absence of a row does not turn the compatibility API into a writer."""
    session = _FakeSession()

    with pytest.raises(ValueError, match="partial parameter release"):
        db_update_parameter_release_status(
            session, "rel_missing", apply_result="success",
        )
    assert session.statements == []


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
            "status": "observing",
            "recommendation": "review",
            "observation_window_hours": 24,
            "window_active": True,
            "started_at": "2026-08-27T10:00:00+00:00",
            "evaluated_at": "2026-08-27T10:05:00+00:00",
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
            "status": "observing",
            "recommendation": "review",
            "observation_window_hours": 24,
            "window_active": True,
            "started_at": "2026-08-27T10:00:00+00:00",
            "evaluated_at": "2026-08-27T10:05:00+00:00",
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
            "evaluated_at": "2026-08-27T10:05:00+00:00",
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
            "evaluated_at": "2026-08-27T10:05:00+00:00",
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


def test_effectiveness_in_progress_same_attempt_cannot_regress_to_pending() -> None:
    existing = {
        "evaluation_id": "eval_in_progress",
        "release_id": "rel_in_progress",
        "family": "independent",
        "timeframe": "15m",
        "conclusion": "rollback_triggered",
        "rollback_enforcement_status": "in_progress",
        "rollback_enforcement_attempt_id": "attempt_a",
        "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
    }
    incoming = {
        **existing,
        "rollback_enforcement_status": "pending",
    }

    merged = _merge_release_effectiveness_state(existing, incoming)

    assert merged["rollback_enforcement_status"] == "in_progress"
    assert merged["rollback_enforcement_attempt_id"] == "attempt_a"
    assert (
        merged["rollback_enforcement_started_at"]
        == "2026-08-27T10:00:00+00:00"
    )


def test_effectiveness_legacy_null_identity_is_backfilled_by_canonical_update() -> None:
    existing = {
        "evaluation_id": "eval_legacy",
        "release_id": "rel_legacy",
        "family": None,
        "timeframe": None,
        "conclusion": "rollback_triggered",
        "rollback_enforcement_status": "pending",
    }
    incoming = {
        **existing,
        "family": "independent",
        "timeframe": "15m",
    }

    merged = _merge_release_effectiveness_state(existing, incoming)

    assert merged["family"] == "independent"
    assert merged["timeframe"] == "15m"
    assert merged["conclusion"] == "rollback_triggered"
    assert merged["rollback_enforcement_status"] == "pending"


def test_effectiveness_unscoped_rollback_obligation_requires_reconciliation() -> None:
    merged = _merge_release_effectiveness_state(
        {},
        {
            "evaluation_id": "eval_orphan",
            "release_id": "rel_orphan",
            "conclusion": "rollback_triggered",
        },
    )

    assert merged["rollback_enforcement_status"] == "reconciliation_required"
    assert merged["rollback_reconciliation_reason"] == "rollback_identity_missing"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("rollback_enforced", "true"),
        ("rollback_cancelled", 1),
        ("rollback_enforced", None),
    ],
)
def test_effectiveness_rejects_non_boolean_rollback_flags(
    flag: str,
    value: object,
) -> None:
    incoming = {
        "evaluation_id": "eval_bad_flag",
        "release_id": "rel_bad_flag",
        "family": "independent",
        "timeframe": "15m",
        "conclusion": "rollback_triggered",
        flag: value,
    }

    with pytest.raises(ValueError, match="exact bool"):
        _merge_release_effectiveness_state({}, incoming)


def test_effectiveness_legacy_failed_attempt_is_quarantined() -> None:
    merged = _merge_release_effectiveness_state(
        {},
        {
            "evaluation_id": "eval_legacy_failed",
            "release_id": "rel_legacy_failed",
            "family": "independent",
            "timeframe": "15m",
            "conclusion": "rollback_triggered",
            "rollback_attempts": 1,
            "last_rollback_error": "timeout",
        },
    )

    assert merged["rollback_enforcement_status"] == "reconciliation_required"


def test_effectiveness_rejects_cancelled_with_unpersisted_soft_pause() -> None:
    with pytest.raises(ValueError, match="unproven soft-pause"):
        _merge_release_effectiveness_state(
            {},
            {
                "evaluation_id": "eval_unpersisted_pause",
                "release_id": "rel_unpersisted_pause",
                "family": "independent",
                "timeframe": "15m",
                "conclusion": "rollback_triggered",
                "rollback_cancelled": True,
                "rollback_soft_pause_applied": False,
                "rollback_cancelled_reason": (
                    "soft_paused_no_valid_rollback_target: timeout"
                ),
            },
        )


@pytest.mark.parametrize("status", ["enforced", "cancelled"])
def test_effectiveness_new_row_cannot_forge_terminal_action(status: str) -> None:
    finished_at = "2026-08-27T10:05:00+00:00"
    incoming = {
        "evaluation_id": "eval_forged_terminal",
        "release_id": "rel_forged_terminal",
        "family": "independent",
        "timeframe": "15m",
        "conclusion": "rollback_triggered",
        "rollback_enforcement_status": status,
        "rollback_enforcement_attempt_id": "attempt_forged",
        "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
        "rollback_enforcement_finished_at": finished_at,
    }
    if status == "enforced":
        incoming.update({
            "rollback_enforced": True,
            "rollback_enforced_at": finished_at,
            "rollback_to_parameter_set_id": "ps_previous",
        })
    else:
        incoming.update({
            "rollback_cancelled": True,
            "rollback_cancelled_at": finished_at,
            "rollback_cancelled_reason": "active parameter changed",
        })

    with pytest.raises(ValueError, match="cannot start with an action outcome"):
        _merge_release_effectiveness_state({}, incoming)


def test_effectiveness_pending_cannot_skip_claim_to_terminal() -> None:
    pending = _merge_release_effectiveness_state(
        {},
        {
            "evaluation_id": "eval_pending",
            "release_id": "rel_pending",
            "family": "independent",
            "timeframe": "15m",
            "conclusion": "rollback_triggered",
        },
    )
    forged = {
        **pending,
        "rollback_enforcement_status": "enforced",
        "rollback_enforcement_attempt_id": "attempt_forged",
        "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
        "rollback_enforcement_finished_at": "2026-08-27T10:05:00+00:00",
        "rollback_enforced": True,
        "rollback_enforced_at": "2026-08-27T10:05:00+00:00",
        "rollback_to_parameter_set_id": "ps_previous",
    }

    with pytest.raises(ValueError, match="directly to terminal"):
        _merge_release_effectiveness_state(pending, forged)


def test_effectiveness_claim_then_same_attempt_can_resolve_enforced() -> None:
    pending = _merge_release_effectiveness_state(
        {},
        {
            "evaluation_id": "eval_valid_action",
            "release_id": "rel_valid_action",
            "family": "independent",
            "timeframe": "15m",
            "conclusion": "rollback_triggered",
        },
    )
    claim = _merge_release_effectiveness_state(
        pending,
        {
            **pending,
            "rollback_enforcement_status": "in_progress",
            "rollback_enforcement_attempt_id": "attempt_valid",
            "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
        },
    )
    resolved = _merge_release_effectiveness_state(
        claim,
        {
            **claim,
            "rollback_enforcement_status": "enforced",
            "rollback_enforcement_finished_at": "2026-08-27T10:05:00+00:00",
            "rollback_enforced": True,
            "rollback_enforced_at": "2026-08-27T10:05:00+00:00",
            "rollback_to_parameter_set_id": "ps_previous",
            "rollback_soft_pause_applied": False,
            "rollback_capital_proof_version": "rdp-rollback-capital-proof/v1",
            "rollback_capital_proof_kind": "rollback",
            "rollback_capital_operation_id": "op_rollback_valid",
        },
    )

    assert resolved["rollback_enforcement_status"] == "enforced"
    assert resolved["rollback_enforcement_attempt_id"] == "attempt_valid"
    assert "rollback_capital_proof_verified" not in resolved


def _seed_in_progress_effectiveness(session: _FakeSession, release_id: str) -> None:
    evaluated_at = datetime(2026, 8, 27, 9, 30, tzinfo=timezone.utc)
    payload = {
        "evaluation_id": f"eval_{release_id}",
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "conclusion": "rollback_triggered",
        "evaluated_at": evaluated_at.isoformat(),
        "rollback_enforcement_status": "in_progress",
        "rollback_enforcement_attempt_id": "attempt_capital_truth",
        "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
    }
    session.effectiveness_rows[release_id] = {
        "evaluation_id": payload["evaluation_id"],
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "conclusion": "rollback_triggered",
        "evaluated_at": evaluated_at,
        "payload": payload,
        "updated_at": evaluated_at,
    }


def _enforced_terminal_candidate(release_id: str) -> dict[str, Any]:
    finished_at = "2026-08-27T10:05:00+00:00"
    return {
        "evaluation_id": f"eval_{release_id}",
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "conclusion": "rollback_triggered",
        "evaluated_at": "2026-08-27T09:30:00+00:00",
        "rollback_enforcement_status": "enforced",
        "rollback_enforcement_attempt_id": "attempt_capital_truth",
        "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
        "rollback_enforcement_finished_at": finished_at,
        "rollback_enforced": True,
        "rollback_enforced_at": finished_at,
        "rollback_to_parameter_set_id": "ps_previous",
        "rollback_soft_pause_applied": False,
        "rollback_capital_proof_version": "rdp-rollback-capital-proof/v1",
        "rollback_capital_proof_kind": "rollback",
        "rollback_capital_operation_id": "op_rollback_exact",
        # A caller must not be able to self-attest this flag.
        "rollback_capital_proof_verified": True,
    }


def _exact_enforced_capital_truth() -> dict[str, Any]:
    return {
        "release_apply_result": "success",
        "release_observation_status": "rolled_back",
        "release_parameter_set_id": "ps_release",
        "release_rollback_target": "ps_previous",
        "release_rollback_operation_id": "op_rollback_exact",
        "release_combo_key": "independent_15m",
        "active_parameter_set_id": "ps_previous",
        "history_operation_type": "rollback",
        "history_family": "independent",
        "history_timeframe": "15m",
        "history_from_parameter_set_id": "ps_release",
        "history_to_parameter_set_id": "ps_previous",
        "history_actor": "release_effectiveness_auto_rollback",
        "history_created_at": datetime(
            2026, 8, 27, 10, 3, tzinfo=timezone.utc
        ),
        "decision_status": None,
        "decision_updated_at": None,
        "decision_notes": None,
    }


def test_effectiveness_terminal_writer_adds_attestation_after_exact_db_proof() -> None:
    session = _FakeSession()
    _seed_in_progress_effectiveness(session, "rel_proven")
    session.effectiveness_capital_proof = _exact_enforced_capital_truth()

    merged = db_upsert_release_effectiveness(
        session,
        _enforced_terminal_candidate("rel_proven"),
    )

    assert merged["rollback_enforcement_status"] == "enforced"
    assert merged["rollback_capital_proof_verified"] is True
    assert merged["rollback_enforcement_finished_at"] == (
        "2026-08-27T10:03:00+00:00"
    )
    assert session.effectiveness_action_proofs[0]["attempt_id"] == (
        "attempt_capital_truth"
    )
    proof_queries = [
        sql for sql, _params in session.statements if "release_apply_result" in sql
    ]
    assert len(proof_queries) == 1


def test_effectiveness_terminal_writer_accepts_exact_operator_rollback() -> None:
    """Actor changes proof provenance, not the rollback terminal outcome."""
    session = _FakeSession()
    release_id = "rel_operator_rollback"
    _seed_in_progress_effectiveness(session, release_id)
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "history_actor": "operator_alice",
    }

    merged = db_upsert_release_effectiveness(
        session,
        _enforced_terminal_candidate(release_id),
    )

    assert merged["rollback_enforcement_status"] == "enforced"
    assert merged["rollback_capital_proof_kind"] == "rollback"
    assert merged["rollback_capital_proof_verified"] is True


def test_completed_operator_rollback_fact_requires_exact_capital_lineage() -> None:
    session = _FakeSession()
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "history_actor": "operator_alice",
    }

    fact = db_get_completed_operator_rollback_fact(
        session,
        release_id="rel_operator_rollback",
        family="independent",
        timeframe="15m",
    )

    assert fact is not None
    assert fact["operation_id"] == "op_rollback_exact"
    assert fact["target_parameter_set_id"] == "ps_previous"
    assert fact["fact_observed_at"] == datetime(
        2026, 8, 27, 10, 3, tzinfo=timezone.utc
    )

    session.effectiveness_capital_proof = {
        **session.effectiveness_capital_proof,
        "active_parameter_set_id": "ps_unrelated",
    }
    assert db_get_completed_operator_rollback_fact(
        session,
        release_id="rel_operator_rollback",
        family="independent",
        timeframe="15m",
    ) is None


def test_effectiveness_terminal_writer_rejects_forged_verified_flag() -> None:
    session = _FakeSession()
    _seed_in_progress_effectiveness(session, "rel_forged_verified")
    # No canonical proof row: the incoming verified=true must be stripped and
    # cannot authorize the terminal transition.
    session.effectiveness_capital_proof = None

    with pytest.raises(ValueError, match="no canonical release"):
        db_upsert_release_effectiveness(
            session,
            _enforced_terminal_candidate("rel_forged_verified"),
        )

    stored_payload = session.effectiveness_rows["rel_forged_verified"]["payload"]
    assert stored_payload["rollback_enforcement_status"] == "in_progress"


def test_effectiveness_enforced_rejects_wrong_current_active_target() -> None:
    session = _FakeSession()
    _seed_in_progress_effectiveness(session, "rel_wrong_active")
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "active_parameter_set_id": "ps_release",
    }

    with pytest.raises(ValueError, match="exact canonical capital lineage"):
        db_upsert_release_effectiveness(
            session,
            _enforced_terminal_candidate("rel_wrong_active"),
        )


def _cancelled_terminal_candidate(
    release_id: str,
    *,
    proof_kind: str,
) -> dict[str, Any]:
    finished_at = "2026-08-27T10:05:00+00:00"
    candidate = {
        "evaluation_id": f"eval_{release_id}",
        "release_id": release_id,
        "family": "independent",
        "timeframe": "15m",
        "combo_key": "independent_15m",
        "conclusion": "rollback_triggered",
        "evaluated_at": "2026-08-27T09:30:00+00:00",
        "rollback_enforcement_status": "cancelled",
        "rollback_enforcement_attempt_id": "attempt_capital_truth",
        "rollback_enforcement_started_at": "2026-08-27T10:00:00+00:00",
        "rollback_enforcement_finished_at": finished_at,
        "rollback_cancelled": True,
        "rollback_cancelled_at": finished_at,
        "rollback_capital_proof_version": "rdp-rollback-capital-proof/v1",
        "rollback_capital_proof_kind": proof_kind,
    }
    if proof_kind == "active_parameter_changed":
        candidate.update({
            "rollback_cancelled_reason": (
                "active_parameter_set_changed_before_rollback: proven"
            ),
            "rollback_soft_pause_applied": False,
            "rollback_capital_proof_active_parameter_set_id": "ps_other",
        })
    else:
        candidate.update({
            "rollback_cancelled_reason": (
                "soft_paused_no_valid_rollback_target: no target"
            ),
            "rollback_soft_pause_applied": True,
            "rollback_capital_proof_decision_status": "pause",
        })
    return candidate


def test_effectiveness_active_change_writes_immutable_observation_proof() -> None:
    session = _FakeSession()
    release_id = "rel_active_changed"
    _seed_in_progress_effectiveness(session, release_id)
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "release_observation_status": "observing",
        "active_parameter_set_id": "ps_other",
    }

    merged = db_upsert_release_effectiveness(
        session,
        _cancelled_terminal_candidate(
            release_id,
            proof_kind="active_parameter_changed",
        ),
    )

    assert merged["rollback_capital_proof_verified"] is True
    assert session.effectiveness_action_proofs[0][
        "observed_active_parameter_set_id"
    ] == "ps_other"
    assert session.effectiveness_action_proofs[0]["operation_id"] is None


def test_effectiveness_active_change_rejects_release_still_active() -> None:
    session = _FakeSession()
    release_id = "rel_still_active"
    _seed_in_progress_effectiveness(session, release_id)
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "release_observation_status": "observing",
        "active_parameter_set_id": "ps_release",
    }

    with pytest.raises(ValueError, match="active-change cancellation lacks"):
        db_upsert_release_effectiveness(
            session,
            _cancelled_terminal_candidate(
                release_id,
                proof_kind="active_parameter_changed",
            ),
        )


def test_effectiveness_soft_pause_binds_decision_time_to_attempt() -> None:
    session = _FakeSession()
    release_id = "rel_soft_pause"
    _seed_in_progress_effectiveness(session, release_id)
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "release_observation_status": "observing",
        "active_parameter_set_id": "ps_release",
        "decision_status": "pause",
        "decision_updated_at": datetime(
            2026, 8, 27, 10, 3, tzinfo=timezone.utc
        ),
        "decision_notes": (
            "soft_pause_auto_rollback_no_valid_target: "
            f"release={release_id} reason=no target"
        ),
    }

    merged = db_upsert_release_effectiveness(
        session,
        _cancelled_terminal_candidate(release_id, proof_kind="soft_pause"),
    )

    assert merged["rollback_enforcement_finished_at"] == (
        "2026-08-27T10:03:00+00:00"
    )
    assert session.effectiveness_action_proofs[0]["decision_status"] == "pause"


def test_effectiveness_rejects_fact_timestamp_after_claimed_finish() -> None:
    session = _FakeSession()
    release_id = "rel_late_history"
    _seed_in_progress_effectiveness(session, release_id)
    session.effectiveness_capital_proof = {
        **_exact_enforced_capital_truth(),
        "history_created_at": datetime(
            2026, 8, 27, 10, 6, tzinfo=timezone.utc
        ),
    }

    with pytest.raises(ValueError, match="outside the action attempt"):
        db_upsert_release_effectiveness(
            session,
            _enforced_terminal_candidate(release_id),
        )
