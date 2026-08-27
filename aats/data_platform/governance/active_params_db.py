"""Active Parameters DB — governance.active_parameter_sets 读写层.

将 active parameter 的存储从 JSON 文件迁移到 Postgres governance schema。
提供 6 个函数，1:1 替换原有的文件操作。

依赖:
  - governance.active_parameter_sets 表  (migration 0013)
  - governance.parameter_apply_history 表 (migration 0013)
  - aats.data_platform.db 的连接管理
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ._db_util import ADVISORY_LOCK_KEYS

# Task P3-1：常量定义移到 imports 之后，消除 E402（之前 UTC 夹在两组 import 中间）。
UTC = timezone.utc

log = logging.getLogger(__name__)

_PARAMETER_APPLY_LOCK_NAMESPACE = ADVISORY_LOCK_KEYS["parameter_apply_combo"]


def _canonical_parameter_combo_key(*, family: str, timeframe: str) -> str:
    """Return the sole advisory-lock identity for a parameter combo.

    Database safety checks compare combo identity with ``lower(btrim(...))``.
    The transaction lock must use the identical equivalence relation or a
    producer using padded/mixed-case identity can run concurrently with an
    apply using the canonical spelling.
    """
    if not isinstance(family, str) or not isinstance(timeframe, str):
        raise ValueError("parameter combo family/timeframe must be strings")
    family_norm = family.strip().lower()
    timeframe_norm = timeframe.strip().lower()
    if not family_norm or not timeframe_norm:
        raise ValueError("parameter combo family/timeframe must be non-empty")
    return f"{family_norm}_{timeframe_norm}"


def db_try_acquire_parameter_apply_lock(
    session: Session,
    *,
    family: str,
    timeframe: str,
) -> bool:
    """Try to acquire the transaction-scoped mutation lock for one combo.

    Apply, rollback, clear and pending-rollback persistence must all use this
    lock.  Keeping every capital-state mutation in one lock namespace makes
    the apply-history ``from``/``to`` chain linearizable.
    """
    combo_key = _canonical_parameter_combo_key(
        family=family,
        timeframe=timeframe,
    )
    row = session.execute(
        text(
            "SELECT pg_try_advisory_xact_lock("
            ":namespace, hashtext(:combo_key)) AS acquired"
        ),
        {"namespace": _PARAMETER_APPLY_LOCK_NAMESPACE, "combo_key": combo_key},
    ).fetchone()
    return bool(row is not None and row.acquired is True)


_RELEASE_ROLLBACK_RESOLVED_SQL = """
(
    r.observation_status = 'rolled_back'
    AND r.payload ->> 'rollback_capital_proof_version'
        = 'rdp-release-rollback-capital-proof/v1'
    AND jsonb_typeof(r.payload -> 'rollback_capital_proof_verified') = 'boolean'
    AND r.payload -> 'rollback_capital_proof_verified' = 'true'::jsonb
    AND NULLIF(btrim(r.payload ->> 'rollback_to_parameter_set_id'), '') IS NOT NULL
    AND NULLIF(btrim(r.payload ->> 'rollback_operation_id'), '') IS NOT NULL
    AND EXISTS (
        SELECT 1
        FROM governance.parameter_apply_history AS rh
        WHERE rh.operation_id = r.payload ->> 'rollback_operation_id'
          AND rh.operation_type = 'rollback'
          AND lower(btrim(rh.family)) = lower(btrim(r.family))
          AND lower(btrim(rh.timeframe)) = lower(btrim(r.timeframe))
          AND rh.from_parameter_set_id = r.parameter_set_id
          AND rh.to_parameter_set_id
              = r.payload ->> 'rollback_to_parameter_set_id'
    )
)
"""


def _canonical_utc_json_timestamp_sql(value_sql: str) -> str:
    """Return a non-throwing canonical UTC/calendar validation expression.

    Direct ``::timestamptz`` casts are unsafe on legacy JSON because malformed
    dates can abort the whole apply-gate query.  Nested CASE first proves digit
    shape, then validates the Gregorian calendar with integer arithmetic only;
    no date constructor or cast is allowed on untrusted payload text.
    """

    regex = (
        "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
        "[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?[+]00:00$"
    )
    return f"""
    CASE
        WHEN {value_sql} ~ '{regex}' THEN
            CASE
                WHEN substring({value_sql} FROM 1 FOR 4)::integer
                         BETWEEN 1970 AND 9999
                 AND substring({value_sql} FROM 6 FOR 2)::integer
                         BETWEEN 1 AND 12
                 AND substring({value_sql} FROM 12 FOR 2)::integer
                         BETWEEN 0 AND 23
                 AND substring({value_sql} FROM 15 FOR 2)::integer
                         BETWEEN 0 AND 59
                 AND substring({value_sql} FROM 18 FOR 2)::integer
                         BETWEEN 0 AND 59
                THEN substring({value_sql} FROM 9 FOR 2)::integer BETWEEN 1 AND
                     CASE substring({value_sql} FROM 6 FOR 2)::integer
                         WHEN 1 THEN 31
                         WHEN 2 THEN
                             CASE
                                 WHEN mod(
                                          substring({value_sql} FROM 1 FOR 4)::integer,
                                          400
                                      ) = 0
                                   OR (
                                       mod(
                                           substring({value_sql} FROM 1 FOR 4)::integer,
                                           4
                                       ) = 0
                                       AND mod(
                                           substring({value_sql} FROM 1 FOR 4)::integer,
                                           100
                                       ) <> 0
                                   )
                                 THEN 29
                                 ELSE 28
                             END
                         WHEN 3 THEN 31
                         WHEN 4 THEN 30
                         WHEN 5 THEN 31
                         WHEN 6 THEN 30
                         WHEN 7 THEN 31
                         WHEN 8 THEN 31
                         WHEN 9 THEN 30
                         WHEN 10 THEN 31
                         WHEN 11 THEN 30
                         WHEN 12 THEN 31
                         ELSE 0
                     END
                ELSE FALSE
            END
        ELSE FALSE
    END
    """


_EFFECTIVENESS_STARTED_AT_SQL = (
    "e.payload ->> 'rollback_enforcement_started_at'"
)
_EFFECTIVENESS_FINISHED_AT_SQL = (
    "e.payload ->> 'rollback_enforcement_finished_at'"
)
_EFFECTIVENESS_STARTED_AT_VALID_SQL = _canonical_utc_json_timestamp_sql(
    _EFFECTIVENESS_STARTED_AT_SQL
)
_EFFECTIVENESS_FINISHED_AT_VALID_SQL = _canonical_utc_json_timestamp_sql(
    _EFFECTIVENESS_FINISHED_AT_SQL
)


_EFFECTIVENESS_ACTION_RESOLVED_SQL = f"""
(
    e.conclusion = 'rollback_triggered'
    AND e.payload ->> 'rollback_capital_proof_version'
        = 'rdp-rollback-capital-proof/v1'
    AND jsonb_typeof(e.payload -> 'rollback_capital_proof_verified') = 'boolean'
    AND e.payload -> 'rollback_capital_proof_verified' = 'true'::jsonb
    AND NULLIF(btrim(e.payload ->> 'rollback_enforcement_attempt_id'), '')
        IS NOT NULL
    AND NULLIF(btrim(e.payload ->> 'rollback_enforcement_started_at'), '')
        IS NOT NULL
    AND NULLIF(btrim(e.payload ->> 'rollback_enforcement_finished_at'), '')
        IS NOT NULL
    AND {_EFFECTIVENESS_STARTED_AT_VALID_SQL}
    AND {_EFFECTIVENESS_FINISHED_AT_VALID_SQL}
    -- Canonical UTC strings are fixed-width through seconds and use at most
    -- six fractional digits, so lexical order is chronological without a cast.
    AND {_EFFECTIVENESS_STARTED_AT_SQL} <= {_EFFECTIVENESS_FINISHED_AT_SQL}
    AND {_EFFECTIVENESS_STARTED_AT_SQL} <= (
        to_char(
            (now() + interval '5 minutes') AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US'
        ) || '+00:00'
    )
    AND {_EFFECTIVENESS_FINISHED_AT_SQL} <= (
        to_char(
            (now() + interval '5 minutes') AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US'
        ) || '+00:00'
    )
    AND EXISTS (
        SELECT 1
        FROM governance.release_effectiveness_action_proofs AS ep
        WHERE ep.release_id = e.release_id
          AND ep.attempt_id
              = e.payload ->> 'rollback_enforcement_attempt_id'
          AND ep.outcome = e.payload ->> 'rollback_enforcement_status'
          AND ep.proof_kind = e.payload ->> 'rollback_capital_proof_kind'
          AND ep.started_at_utc
              = e.payload ->> 'rollback_enforcement_started_at'
          AND ep.finished_at_utc
              = e.payload ->> 'rollback_enforcement_finished_at'
          AND ep.operation_id IS NOT DISTINCT FROM NULLIF(
              btrim(e.payload ->> 'rollback_capital_operation_id'), ''
          )
          AND ep.target_parameter_set_id IS NOT DISTINCT FROM NULLIF(
              btrim(e.payload ->> 'rollback_to_parameter_set_id'), ''
          )
          AND ep.observed_active_parameter_set_id IS NOT DISTINCT FROM NULLIF(
              btrim(
                  e.payload
                      ->> 'rollback_capital_proof_active_parameter_set_id'
              ), ''
          )
          AND ep.decision_status IS NOT DISTINCT FROM NULLIF(
              btrim(e.payload ->> 'rollback_capital_proof_decision_status'), ''
          )
    )
    AND (
        (
            e.payload ->> 'rollback_enforcement_status' = 'enforced'
            AND jsonb_typeof(e.payload -> 'rollback_enforced') = 'boolean'
            AND e.payload -> 'rollback_enforced' = 'true'::jsonb
            AND (
                NOT (e.payload ? 'rollback_cancelled')
                OR (
                    jsonb_typeof(e.payload -> 'rollback_cancelled') = 'boolean'
                    AND e.payload -> 'rollback_cancelled' = 'false'::jsonb
                )
            )
            AND jsonb_typeof(e.payload -> 'rollback_soft_pause_applied')
                = 'boolean'
            AND e.payload -> 'rollback_soft_pause_applied' = 'false'::jsonb
            AND e.payload ->> 'rollback_capital_proof_kind' = 'rollback'
            AND NULLIF(btrim(e.payload ->> 'rollback_capital_operation_id'), '')
                IS NOT NULL
            AND NULLIF(btrim(e.payload ->> 'rollback_to_parameter_set_id'), '')
                IS NOT NULL
            AND e.payload ->> 'rollback_enforced_at'
                = e.payload ->> 'rollback_enforcement_finished_at'
            AND {_RELEASE_ROLLBACK_RESOLVED_SQL}
            AND e.payload ->> 'rollback_capital_operation_id'
                = r.payload ->> 'rollback_operation_id'
            AND e.payload ->> 'rollback_to_parameter_set_id'
                = r.payload ->> 'rollback_to_parameter_set_id'
        )
        OR (
            e.payload ->> 'rollback_enforcement_status' = 'cancelled'
            AND jsonb_typeof(e.payload -> 'rollback_cancelled') = 'boolean'
            AND e.payload -> 'rollback_cancelled' = 'true'::jsonb
            AND (
                NOT (e.payload ? 'rollback_enforced')
                OR (
                    jsonb_typeof(e.payload -> 'rollback_enforced') = 'boolean'
                    AND e.payload -> 'rollback_enforced' = 'false'::jsonb
                )
            )
            AND e.payload ->> 'rollback_cancelled_at'
                = e.payload ->> 'rollback_enforcement_finished_at'
            AND (
                (
                    e.payload ->> 'rollback_capital_proof_kind'
                        = 'active_parameter_changed'
                    AND jsonb_typeof(
                        e.payload -> 'rollback_soft_pause_applied'
                    ) = 'boolean'
                    AND e.payload -> 'rollback_soft_pause_applied' = 'false'::jsonb
                    AND e.payload ->> 'rollback_cancelled_reason'
                        LIKE 'active_parameter_set_changed_before_rollback:%'
                    AND NULLIF(btrim(
                        e.payload ->> 'rollback_capital_proof_active_parameter_set_id'
                    ), '') IS NOT NULL
                    AND e.payload
                        ->> 'rollback_capital_proof_active_parameter_set_id'
                        <> r.parameter_set_id
                    AND r.observation_status IS DISTINCT FROM 'rolled_back'
                )
                OR (
                    e.payload ->> 'rollback_capital_proof_kind' = 'soft_pause'
                    AND jsonb_typeof(
                        e.payload -> 'rollback_soft_pause_applied'
                    ) = 'boolean'
                    AND e.payload -> 'rollback_soft_pause_applied' = 'true'::jsonb
                    AND e.payload ->> 'rollback_capital_proof_decision_status'
                        = 'pause'
                    AND e.payload ->> 'rollback_cancelled_reason'
                        LIKE 'soft_paused_no_valid_rollback_target:%'
                )
            )
        )
    )
)
"""


def db_get_pending_rollback_release_id(
    session: Session,
    *,
    family: str,
    timeframe: str,
) -> str | None:
    """Return the latest unresolved rollback-triggered release for a combo.

    Action state lives in the canonical JSONB payload.  Compare JSON text
    values instead of casting to boolean so malformed legacy payloads fail
    closed as unresolved instead of aborting the query.
    """
    # Evidence producers persist before the effectiveness materializer runs.
    # That interval is itself a durable rollback obligation: a new apply must
    # not slip through merely because the derived effectiveness row has not yet
    # been created. Producers take the same combo advisory lock, so this read
    # and subsequent capital write are linearizable with evidence commits.
    raw_evidence = session.execute(
        text(
            f"""
            WITH raw_obligations AS (
                SELECT o.release_id, o.family, o.timeframe,
                       o.combo_key, o.evaluated_at
                FROM governance.observation_results AS o
                WHERE o.status = 'rollback_recommended'
                UNION ALL
                SELECT rb.release_id, rb.family, rb.timeframe,
                       rb.combo_key, rb.evaluated_at
                FROM governance.rollback_recommendations AS rb
                WHERE rb.rollback_recommended IS TRUE
            )
            SELECT raw.release_id
            FROM raw_obligations AS raw
            LEFT JOIN governance.parameter_releases AS r
              ON r.release_id = raw.release_id
            LEFT JOIN governance.release_effectiveness AS e
              ON e.release_id = raw.release_id
            WHERE (
                    e.release_id IS NULL
                    OR (
                        e.evaluated_at >= raw.evaluated_at
                        AND {_EFFECTIVENESS_ACTION_RESOLVED_SQL}
                    ) IS NOT TRUE
              )
              AND (
                    r.release_id IS NULL
                    OR r.apply_result IS DISTINCT FROM 'success'
                    OR NULLIF(btrim(r.family), '') IS NULL
                    OR NULLIF(btrim(r.timeframe), '') IS NULL
                    OR NULLIF(btrim(r.combo_key), '') IS NULL
                    OR NULLIF(btrim(raw.family), '') IS NULL
                    OR NULLIF(btrim(raw.timeframe), '') IS NULL
                    OR NULLIF(btrim(raw.combo_key), '') IS NULL
                    OR lower(btrim(raw.family)) <> lower(btrim(r.family))
                    OR lower(btrim(raw.timeframe)) <> lower(btrim(r.timeframe))
                    OR lower(btrim(raw.combo_key)) <> lower(btrim(r.combo_key))
                    OR lower(btrim(r.combo_key))
                       <> lower(btrim(r.family)) || '_' || lower(btrim(r.timeframe))
                    OR (
                        lower(btrim(r.family)) = lower(btrim(:family))
                        AND lower(btrim(r.timeframe)) = lower(btrim(:timeframe))
                    )
              )
            ORDER BY raw.evaluated_at DESC NULLS FIRST, raw.release_id DESC
            LIMIT 1
            """
        ),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()
    if raw_evidence is not None:
        release_id = getattr(raw_evidence, "release_id", None)
        if release_id:
            return str(release_id)

    row = session.execute(
        text(
            f"""
            SELECT e.release_id
            FROM governance.release_effectiveness AS e
            LEFT JOIN governance.parameter_releases AS r
              ON r.release_id = e.release_id
            WHERE (
                    -- A valid historical evaluation is scoped exclusively by
                    -- its canonical successful release.  Any missing/malformed
                    -- or cross-identity lineage remains a *global* unresolved
                    -- veto: legacy polluted evidence must never authorize one
                    -- combo while accidentally blocking another.
                    r.release_id IS NULL
                    OR r.apply_result IS DISTINCT FROM 'success'
                    OR NULLIF(btrim(r.family), '') IS NULL
                    OR NULLIF(btrim(r.timeframe), '') IS NULL
                    OR NULLIF(btrim(r.combo_key), '') IS NULL
                    OR NULLIF(btrim(e.family), '') IS NULL
                    OR NULLIF(btrim(e.timeframe), '') IS NULL
                    OR NULLIF(btrim(e.payload ->> 'combo_key'), '') IS NULL
                    OR lower(btrim(e.family)) <> lower(btrim(r.family))
                    OR lower(btrim(e.timeframe)) <> lower(btrim(r.timeframe))
                    OR lower(btrim(e.payload ->> 'combo_key'))
                       <> lower(btrim(r.combo_key))
                    OR lower(btrim(r.combo_key))
                       <> lower(btrim(r.family)) || '_' || lower(btrim(r.timeframe))
                    OR (
                        lower(btrim(r.family)) = lower(btrim(:family))
                        AND lower(btrim(r.timeframe)) = lower(btrim(:timeframe))
                    )
              )
              AND e.conclusion = 'rollback_triggered'
              AND ({_EFFECTIVENESS_ACTION_RESOLVED_SQL}) IS NOT TRUE
            ORDER BY e.evaluated_at DESC, e.id DESC
            LIMIT 1
            """
        ),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()
    if row is None:
        return None
    release_id = getattr(row, "release_id", None)
    return str(release_id) if release_id else None


def db_get_known_bad_release_id_for_parameter_set(
    session: Session,
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
) -> str | None:
    """Return a rollback-triggered release that used this immutable target.

    Terminal enforcement/cancellation does not rehabilitate the parameter set;
    it only resolves that action attempt.  Callers hold the combo mutation lock
    so this query and the following apply/rollback write form one safety boundary.
    """
    row = session.execute(
        text("""
            WITH risk_evidence AS (
                SELECT e.release_id, e.evaluated_at, e.id AS evidence_id
                FROM governance.release_effectiveness AS e
                WHERE e.conclusion = 'rollback_triggered'
                UNION ALL
                SELECT o.release_id, o.evaluated_at, o.id AS evidence_id
                FROM governance.observation_results AS o
                WHERE o.status = 'rollback_recommended'
                UNION ALL
                SELECT rb.release_id, rb.evaluated_at, rb.id AS evidence_id
                FROM governance.rollback_recommendations AS rb
                WHERE rb.rollback_recommended IS TRUE
            )
            SELECT risk.release_id
            FROM risk_evidence AS risk
            JOIN governance.parameter_releases AS r
              ON r.release_id = risk.release_id
            WHERE r.parameter_set_id = :pid
              AND r.apply_result = 'success'
            ORDER BY risk.evaluated_at DESC, risk.evidence_id DESC
            LIMIT 1
        """),
        {
            "pid": parameter_set_id,
        },
    ).fetchone()
    release_id = getattr(row, "release_id", None) if row is not None else None
    if not isinstance(release_id, str) or not release_id.strip():
        return None
    return release_id.strip()


# ── UPSERT (写入 / 更新 active 参数) ────────────────────────────────

def db_upsert_active_set(
    session: Session,
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict[str, Any],
    source_round_id: str | None = None,
    approval_recommendation_id: str | None = None,
    applied_by: str = "operator",
) -> None:
    """UPSERT 一条 active parameter set 记录.

    INSERT ... ON CONFLICT (family, timeframe) DO UPDATE
    保证原子性。调用方应在同一个事务中配合 db_append_history() 使用。
    """
    session.execute(
        text("""
            INSERT INTO governance.active_parameter_sets
                (family, timeframe, parameter_set_id, values,
                 source_round_id, approval_recommendation_id,
                 applied_by, applied_at, updated_at)
            VALUES
                (:family, :timeframe, :ps_id, CAST(:vals AS jsonb),
                 :src_round, :approval_rec,
                 :applied_by, :now, :now)
            ON CONFLICT (family, timeframe) DO UPDATE SET
                parameter_set_id            = EXCLUDED.parameter_set_id,
                values                      = EXCLUDED.values,
                source_round_id             = EXCLUDED.source_round_id,
                approval_recommendation_id  = EXCLUDED.approval_recommendation_id,
                applied_by                  = EXCLUDED.applied_by,
                applied_at                  = EXCLUDED.applied_at,
                updated_at                  = EXCLUDED.updated_at
        """),
        {
            "family": family,
            "timeframe": timeframe.lower(),
            "ps_id": parameter_set_id,
            "vals": _json_dumps(values),
            "src_round": source_round_id,
            "approval_rec": approval_recommendation_id,
            "applied_by": applied_by,
            "now": datetime.now(timezone.utc),
        },
    )
    log.info("DB upsert active set: %s/%s -> %s", family, timeframe, parameter_set_id)


def db_get_parameter_set_for_update(
    session: Session,
    *,
    parameter_set_id: str,
    family: str,
    timeframe: str,
    symbol: str,
    source_round_id: str,
    expected_values: dict[str, Any],
) -> dict[str, Any] | None:
    """Lock one immutable parameter-set candidate in a caller-owned transaction."""
    row = session.execute(
        text(
            """
            SELECT parameter_set_id, family, symbol, timeframe,
                   source_round_id, values AS param_values, status
            FROM governance.parameter_sets
            WHERE parameter_set_id = :parameter_set_id
              AND family = :family
              AND lower(btrim(timeframe)) = lower(btrim(:timeframe))
              AND symbol = :symbol
              AND source_round_id = :source_round_id
            FOR UPDATE
            """
        ),
        {
            "parameter_set_id": parameter_set_id,
            "family": family,
            "timeframe": timeframe,
            "symbol": symbol,
            "source_round_id": source_round_id,
        },
    ).fetchone()
    if row is None:
        return None
    if type(row.param_values) is not dict or row.param_values != expected_values:
        return None
    return {
        "parameter_set_id": row.parameter_set_id,
        "family": row.family,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "source_round_id": row.source_round_id,
        "values": row.param_values,
        "status": row.status,
    }


# ── SELECT 全部 active 记录 ──────────────────────────────────────────

def db_load_active_registry(session: Session) -> dict[str, Any]:
    """SELECT 全部 active parameter sets，返回与文件 registry 相同的格式.

    Returns
    -------
    dict  格式:
        {
          "generated_at": "...",
          "active_sets": {
            "independent_15m": {
              "parameter_set_id": "ps_xxx",
              "family": "independent",
              "timeframe": "15m",
              "values": { ... }
            },
            ...
          }
        }
    """
    rows = session.execute(
        text("""
            SELECT family, timeframe, parameter_set_id,
                   values AS param_values,
                   source_round_id, approval_recommendation_id,
                   applied_by, applied_at
            FROM governance.active_parameter_sets
            ORDER BY family, timeframe
        """)
    ).fetchall()

    active_sets: dict[str, Any] = {}
    for row in rows:
        combo_key = f"{row.family}_{row.timeframe}"
        # JSONB 列正常情况下一定是 dict,但若 DB 被手工订正成标量/数组(historical
        # migration 事故或 ORM bypass 写入),下游 `_build_applied_recommendation_ids`
        # 等消费者会 AttributeError。在最接近 DB 的加载层做类型护栏,把坏数据
        # 降级为空 dict + log,避免整条读路径崩。
        param_values = row.param_values if isinstance(row.param_values, dict) else {}
        active_sets[combo_key] = {
            "parameter_set_id": row.parameter_set_id,
            "family": row.family,
            "timeframe": row.timeframe,
            "values": param_values,
            "source_round_id": row.source_round_id,
            "approval_recommendation_id": row.approval_recommendation_id,
            "applied_by": row.applied_by,
            "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_sets": active_sets,
    }


# ── SELECT 单条 ──────────────────────────────────────────────────────

def db_get_active_set(
    session: Session,
    family: str,
    timeframe: str,
) -> dict[str, Any]:
    """获取指定 family/timeframe 的 active 参数值.

    Returns
    -------
    dict  参数值字典，如果不存在返回空 dict。
    """
    row = session.execute(
        text("""
            SELECT values AS param_values FROM governance.active_parameter_sets
            WHERE family = :family AND timeframe = :timeframe
        """),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()

    if row is None:
        return {}
    return row.param_values


# ── DELETE 单条 ──────────────────────────────────────────────────────

def db_clear_active_set(
    session: Session,
    family: str,
    timeframe: str,
) -> bool:
    """删除指定 family/timeframe 的 active 记录.

    Returns
    -------
    bool  是否确实删除了一行。
    """
    result = session.execute(
        text("""
            DELETE FROM governance.active_parameter_sets
            WHERE family = :family AND timeframe = :timeframe
        """),
        {"family": family, "timeframe": timeframe.lower()},
    )
    deleted = result.rowcount > 0
    if deleted:
        log.info("DB cleared active set: %s/%s", family, timeframe)
    return deleted


# ── INSERT 审计日志 ──────────────────────────────────────────────────

def db_append_history(
    session: Session,
    *,
    operation_id: str,
    operation_type: str,
    family: str,
    timeframe: str,
    from_parameter_set_id: str | None = None,
    to_parameter_set_id: str | None = None,
    recommendation_id: str | None = None,
    actor: str = "operator",
    notes: str | None = None,
) -> None:
    """INSERT 一条审计日志（不可变，只追加）."""
    session.execute(
        text("""
            INSERT INTO governance.parameter_apply_history
                (operation_id, operation_type, family, timeframe,
                 from_parameter_set_id, to_parameter_set_id,
                 recommendation_id, actor, notes, created_at)
            VALUES
                (:op_id, :op_type, :family, :timeframe,
                 :from_ps, :to_ps,
                 :rec_id, :actor, :notes, :now)
        """),
        {
            "op_id": operation_id,
            "op_type": operation_type,
            "family": family,
            "timeframe": timeframe.lower(),
            "from_ps": from_parameter_set_id,
            "to_ps": to_parameter_set_id,
            "rec_id": recommendation_id,
            "actor": actor,
            "notes": notes,
            "now": datetime.now(timezone.utc),
        },
    )
    log.info("DB history: %s %s/%s (%s)", operation_type, family, timeframe, operation_id)


# ── 查询上一版本（用于 rollback） ─────────────────────────────────

def db_get_previous_set_id(
    session: Session,
    family: str,
    timeframe: str,
) -> str | None:
    """查找最近一条 apply/rollback 操作的 from_parameter_set_id.

    用于 rollback：当前 active 是最新一条 apply 的 to_parameter_set_id，
    那么上一版就是那条记录的 from_parameter_set_id。

    加 ``FOR UPDATE`` 是为了防止并发两个 rollback 请求同时从同一条 history
    行推导出同一个目标：第二个请求会在事务结束前被阻塞，避免双写竞态。

    Returns
    -------
    str | None  上一版的 parameter_set_id，如果没有历史则返回 None。
    """
    row = session.execute(
        text("""
            SELECT from_parameter_set_id
            FROM governance.parameter_apply_history
            WHERE family = :family
              AND timeframe = :timeframe
              AND operation_type IN ('apply', 'rollback')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            FOR UPDATE
        """),
        {"family": family, "timeframe": timeframe.lower()},
    ).fetchone()

    if row is None:
        return None
    return row.from_parameter_set_id


# ── Rollback 目标强校验 ─────────────────────────────────────────────


def validate_rollback_target(
    session: Session,
    family: str,
    timeframe: str,
    target_parameter_set_id: str,
) -> tuple[bool, str]:
    """校验 rollback 目标合法性。返回 ``(ok, reason_if_rejected)``.

    见 ``docs/task/rdp_hardening_batch_a_detailed_design.md §2.3``。6 条规则：

    1. parameter_sets 存在 + family/timeframe 匹配
    2. status ∈ {frozen, released}
    3. 归属正确（与 1 同查询）
    4. parameter_apply_history 有一条 ``operation_type='apply'``、
       ``to_parameter_set_id=target`` 且带 recommendation_id 的历史
       （证明 target 曾由该批准建议实际生效）
    5. active_parameter_sets 当前值 ≠ target（避免自回滚）
    6. apply history 所指 recommendation 的不可变 identity 必须仍匹配 target；
       当前状态允许 ``approved`` 或正常被后续发布淘汰的 ``superseded``。
    7. target 不得属于任何 ``rollback_triggered`` release。参数集是不可变
       快照；一旦效果评估确认其为已知坏版本，即使旧 action 因人工切换而
       cancelled，也不能再通过另一条 rollback 路径重新激活。

    全部通过才返回 ``(True, "")``；任何一条失败，立即短路并返回英文理由码。
    调用方负责映射到 HTTP 语义（422）与结构化日志审计。
    """
    tf = timeframe.lower()

    # 规则 1+2+3: parameter_sets 存在 + 状态合法 + 归属正确
    #
    # Bug 8 修复（2026-04-19）: 规则 2 放宽接受 deprecated target。
    #
    # 原规则仅接受 {frozen, released}，但：
    #   - frozen 是"计划未交付"状态（freeze API 未实现，DB 0 行）
    #   - Bug 9 让 apply 写 released；同 combo 下旧 released 自动降级为
    #     deprecated（保持"任一时刻最多 1 条 released"invariant）
    #   - 实际 rollback target 永远是"上一代曾 live 的 parameter_set"，在
    #     Bug 9 机制下必然是 deprecated → 规则 2 永久拒绝 auto-rollback
    #
    # 修复语义：deprecated 是生命周期正常终点，规则 4 已强制要求"曾在 apply
    # history 出现过"（= 曾经 live），规则 5 保证非 current，规则 6 保证审批链。
    # 这三条合起来等于 rollback 的业务定义，deprecated 不应该是拒绝理由。
    #
    # 时间门控：deprecated_at 超过 30 天的拒绝，避免回滚到"业务语境已变"的老
    # 参数。frozen/released 无时间门控（当前 state，不存在 "太老" 的问题）。
    row = session.execute(
        text("""
            SELECT status, deprecated_at FROM governance.parameter_sets
            WHERE parameter_set_id = :pid
              AND family = :family
              AND timeframe = :tf
        """),
        {"pid": target_parameter_set_id, "family": family, "tf": tf},
    ).fetchone()
    if row is None:
        return False, "target_not_found_or_wrong_combo"
    if row.status not in ("frozen", "released", "deprecated"):
        return False, f"target_status_illegal:{row.status}"
    if row.status == "deprecated":
        deprecated_at = row.deprecated_at
        if deprecated_at is None:
            # 没有时间戳的 deprecated 视为"太老"（保守），拒绝
            return False, "target_deprecated_without_timestamp"
        age_days = (datetime.now(UTC) - deprecated_at).days
        if age_days > 30:
            return False, f"target_deprecated_too_old:{age_days}d"

    # 规则 4: 历史凭证（必须在该 combo 下作为 apply 的 to 出现过）
    history_row = session.execute(
        text("""
            SELECT recommendation_id
            FROM governance.parameter_apply_history
            WHERE family = :family
              AND timeframe = :tf
              AND operation_type = 'apply'
              AND to_parameter_set_id = :pid
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """),
        {"family": family, "tf": tf, "pid": target_parameter_set_id},
    ).fetchone()
    if history_row is None:
        return False, "no_apply_history_for_target"
    lineage_recommendation_id = getattr(history_row, "recommendation_id", None)
    if not lineage_recommendation_id:
        return False, "apply_history_missing_recommendation_lineage"

    # 规则 5: 不是当前生效（同时锁住 active 行，防止校验后被改）
    current_row = session.execute(
        text("""
            SELECT parameter_set_id FROM governance.active_parameter_sets
            WHERE family = :family AND timeframe = :tf
            FOR UPDATE
        """),
        {"family": family, "tf": tf},
    ).fetchone()
    if (
        current_row is not None
        and current_row.parameter_set_id == target_parameter_set_id
    ):
        return False, "target_is_currently_active"

    # 规则 6: history 指向的确切批准链路。``superseded`` 是正常状态：第二次
    # apply 会把第一代 approved 建议淘汰，但不能因此让上一版永远不可回滚。
    rec_row = session.execute(
        text("""
            SELECT 1 FROM governance.recommendations
            WHERE recommendation_id = :recommendation_id
              AND target_parameter_set_id = :pid
              AND family = :family
              AND timeframe = :tf
              AND status IN ('approved', 'superseded')
            LIMIT 1
        """),
        {
            "recommendation_id": lineage_recommendation_id,
            "pid": target_parameter_set_id,
            "family": family,
            "tf": tf,
        },
    ).fetchone()
    if rec_row is None:
        return False, "no_approved_recommendation_lineage"

    # 规则 7: known-bad effectiveness veto。使用 parameter_releases 关联不可变
    # parameter_set_id；在 rollback caller 持有 combo advisory lock 和 active
    # row FOR UPDATE 的事务内执行，避免检查后被正常 effectiveness writer 抢写。
    bad_release_id = db_get_known_bad_release_id_for_parameter_set(
        session,
        family=family,
        timeframe=tf,
        parameter_set_id=target_parameter_set_id,
    )
    if bad_release_id is not None:
        return (
            False,
            "target_has_known_bad_effectiveness:"
            f"{bad_release_id}",
        )

    return True, ""


def db_get_parameter_set_values(
    session: Session,
    parameter_set_id: str,
    *,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any] | None:
    """从 DB 读 parameter_sets.values + source_round_id，并推导 lineage rec id.

    Rollback 强制从 DB 读目标 values，不再依赖 JSON registry —— 这是 A-0.1
    的核心收口：消除"写 JSON → 读 JSON → 写 DB"的注入通道。

    ``approval_recommendation_id`` 不是 ``parameter_sets`` 的列，而是挂在
    ``active_parameter_sets``/``parameter_apply_history`` 上。这里通过查询
    最近一次把该 target 作为 ``to_parameter_set_id`` 的 apply 历史来推导
    lineage（与 validator 规则 4 使用的是同一证据源）。
    """
    row = session.execute(
        text("""
            SELECT values AS param_values,
                   source_round_id
            FROM governance.parameter_sets
            WHERE parameter_set_id = :pid
        """),
        {"pid": parameter_set_id},
    ).fetchone()
    if row is None:
        return None

    lineage_rec_id: str | None = None
    params: dict[str, Any] = {"pid": parameter_set_id}
    where_extra = ""
    if family is not None and timeframe is not None:
        where_extra = " AND family = :family AND timeframe = :tf"
        params["family"] = family
        params["tf"] = timeframe.lower()
    lineage_row = session.execute(
        text(f"""
            SELECT recommendation_id
            FROM governance.parameter_apply_history
            WHERE operation_type = 'apply'
              AND to_parameter_set_id = :pid
              {where_extra}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """),
        params,
    ).fetchone()
    if lineage_row is not None:
        lineage_rec_id = lineage_row.recommendation_id

    return {
        "values": row.param_values,
        "source_round_id": row.source_round_id,
        "approval_recommendation_id": lineage_rec_id,
    }


# ── 工具函数 ──────────────────────────────────────────────────────────

def _json_dumps(obj: Any) -> str:
    """序列化为 JSON 字符串（给 JSONB 参数用）."""
    return json.dumps(obj, ensure_ascii=False, default=str)
