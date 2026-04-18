"""profile_type_review_streak 的原子 CAS 访问器。

背景(R1-08 + R2-02):
  research job 可能并发(手动触发 + schedule 重合),streak 自增必须用 CAS
  防重入。同时 R2-02 要求:方向变化不重置而是记作 'mixed',让 3 轮内震荡
  型 profile 照样能触发 review。

语义:
  - 同方向 + 新 run_id              → streak ++
  - 不同方向 + 新 run_id            → streak ++ 且 direction='mixed'
  - 同一 run_id 重放(dedup)         → no-op
  - 首次插入                         → streak=1 with 原始 direction
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


@dataclass(frozen=True)
class StreakResult:
    profile_id: str
    streak_count: int
    direction: str
    was_incremented: bool  # True 表示本次 call 实际触发了 ++ (非 dedup 重放)


def increment_streak_atomic(
    session: Any,
    *,
    profile_id: str,
    direction: str,
    run_id: str,
) -> StreakResult:
    """原子 CAS:只在 last_run_id != run_id 时才 ++ streak。

    返回 StreakResult,其中 was_incremented 表明本次是否真正 ++(false = 重放跳过)。

    方向变化语义(R2-02):
      若 stored direction != EXCLUDED direction 且 run_id 不同:
        - streak ++(不重置)
        - direction → 'mixed'
    """
    if direction not in ("above_upper", "below_lower"):
        raise ValueError(
            f"direction must be 'above_upper' or 'below_lower', got {direction!r}"
        )

    sql = text("""
        INSERT INTO governance.profile_type_review_streak
            (profile_id, clamp_violation_direction, streak_count,
             last_run_id, last_updated)
        VALUES
            (:pid, :dir, 1, :run_id, NOW())
        ON CONFLICT (profile_id) DO UPDATE SET
            streak_count = CASE
                WHEN governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
                THEN governance.profile_type_review_streak.streak_count + 1
                ELSE governance.profile_type_review_streak.streak_count
            END,
            clamp_violation_direction = CASE
                WHEN governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
                 AND governance.profile_type_review_streak.clamp_violation_direction != EXCLUDED.clamp_violation_direction
                THEN 'mixed'
                WHEN governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
                THEN EXCLUDED.clamp_violation_direction
                ELSE governance.profile_type_review_streak.clamp_violation_direction
            END,
            last_run_id = CASE
                WHEN governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
                THEN EXCLUDED.last_run_id
                ELSE governance.profile_type_review_streak.last_run_id
            END,
            last_updated = CASE
                WHEN governance.profile_type_review_streak.last_run_id != EXCLUDED.last_run_id
                THEN NOW()
                ELSE governance.profile_type_review_streak.last_updated
            END
        RETURNING
            streak_count,
            clamp_violation_direction,
            (xmax = 0) AS was_inserted,
            last_run_id
    """)

    row = session.execute(sql, {
        "pid": profile_id,
        "dir": direction,
        "run_id": run_id,
    }).first()

    was_incremented = bool(row.last_run_id == run_id)

    return StreakResult(
        profile_id=profile_id,
        streak_count=int(row.streak_count),
        direction=str(row.clamp_violation_direction),
        was_incremented=was_incremented,
    )


def reset_streak(
    session: Any,
    *,
    profile_id: str,
) -> None:
    """Clamp 内的 candidate 出现时,操作员 resolve 了 review,或切 profile 后重置。"""
    session.execute(
        text("DELETE FROM governance.profile_type_review_streak WHERE profile_id = :pid"),
        {"pid": profile_id},
    )


def get_streak(session: Any, *, profile_id: str) -> StreakResult | None:
    """读当前 streak。找不到返回 None。"""
    row = session.execute(text("""
        SELECT profile_id, clamp_violation_direction, streak_count, last_run_id
        FROM governance.profile_type_review_streak
        WHERE profile_id = :pid
    """), {"pid": profile_id}).first()

    if row is None:
        return None

    return StreakResult(
        profile_id=row.profile_id,
        streak_count=int(row.streak_count),
        direction=str(row.clamp_violation_direction),
        was_incremented=False,
    )
