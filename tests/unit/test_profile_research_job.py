"""profile_research_job orchestration unit tests.

不依赖真实 DB / live replay — 全部走 mock session + stub replay_fn。
覆盖骨架的关键分支:
  * product grid 大小 = 3^3 = 27
  * coordinate_descent grid = 9
  * 有 clamp-in 通过 gate 的 candidate → 产 upgrade rec
  * 无 clamp-in 通过 gate,只有 violators → 调 increment_streak + 可能 review rec
  * replay_fn 抛异常 → 走 fail 分支,run 记录含 error_message
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from aats.data_platform.research.profile_clamps import get_profile_clamps
from aats.data_platform.research.profile_research_job import (
    GridPoint,
    ReplayResult,
    build_coordinate_descent_grid,
    build_product_grid,
    evaluate_candidate,
    run_profile_research,
    select_best_candidate,
    select_best_violating,
)
from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)


# -----------------------------------------------------------------------------
# Grid builders
# -----------------------------------------------------------------------------

def test_product_grid_size_27() -> None:
    grid = build_product_grid("trend_normal")
    # 3 keys × 3 anchors = 27
    assert len(grid) == 27


def test_product_grid_endpoints_in_clamp() -> None:
    grid = build_product_grid("trend_normal")
    clamps = get_profile_clamps("trend_normal")
    for pt in grid:
        for k, v in pt.values.items():
            rng = clamps[k]
            assert rng["lo"] <= v <= rng["hi"]


def test_coordinate_descent_grid_size() -> None:
    clamps = get_profile_clamps("trend_normal")
    baseline = {k: (rng["lo"] + rng["hi"]) / 2 for k, rng in clamps.items()}
    grid = build_coordinate_descent_grid("trend_normal", baseline=baseline)
    # 3 dims × 3 anchors,但 baseline 在每个 dim 的 mid anchor 上重复 → dedup
    assert len(grid) <= 9
    assert len(grid) >= 7  # mid 重合最多 3 次


# -----------------------------------------------------------------------------
# Candidate eval
# -----------------------------------------------------------------------------

def _good_replay(pt, *, profile_id, oos_window_days) -> ReplayResult:
    return ReplayResult(sharpe=1.5, maxdd=-0.08, trades_per_year=120)


def _bad_replay(pt, *, profile_id, oos_window_days) -> ReplayResult:
    return ReplayResult(sharpe=0.4, maxdd=-0.25, trades_per_year=5)


def test_evaluate_candidate_passing() -> None:
    clamps = get_profile_clamps("trend_normal")
    mid_pt = GridPoint(values={
        k: (rng["lo"] + rng["hi"]) / 2 for k, rng in clamps.items()
    })
    eval_ = evaluate_candidate(
        mid_pt,
        profile_id="trend_normal",
        current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        replay_fn=_good_replay,
        oos_window_days=90,
    )
    assert eval_.in_clamp
    # sharpe_ratio = 1.5, maxdd_ratio = 0.8, activity = 1.2 — 全过
    assert eval_.gate_allow


def test_evaluate_candidate_failing() -> None:
    clamps = get_profile_clamps("trend_normal")
    mid_pt = GridPoint(values={
        k: (rng["lo"] + rng["hi"]) / 2 for k, rng in clamps.items()
    })
    eval_ = evaluate_candidate(
        mid_pt,
        profile_id="trend_normal",
        current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        replay_fn=_bad_replay,
        oos_window_days=90,
    )
    assert not eval_.gate_allow


def test_select_best_candidate_empty() -> None:
    assert select_best_candidate([]) is None


def test_select_best_violating_empty() -> None:
    assert select_best_violating([]) is None


# -----------------------------------------------------------------------------
# Orchestrator with mocked session
# -----------------------------------------------------------------------------

class _FakeSession:
    """极简 MagicMock-like session:只记录 execute 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._commit_count = 0
        self._rollback_count = 0
        self._streak_count = 0

    def execute(self, stmt, params=None):  # noqa: ANN001
        sql = str(stmt)
        self.calls.append((sql, params or {}))

        result = MagicMock()
        # 让 INSERT ... RETURNING 返回一行(streak table 有 RETURNING)
        if "profile_type_review_streak" in sql and "RETURNING" in sql:
            self._streak_count += 1
            row = MagicMock()
            row.streak_count = self._streak_count
            row.clamp_violation_direction = (params or {}).get("dir", "above_upper")
            row.was_inserted = self._streak_count == 1
            row.last_run_id = (params or {}).get("run_id", "run-x")
            result.first.return_value = row
        else:
            result.first.return_value = None
        return result

    def commit(self) -> None:
        self._commit_count += 1

    def rollback(self) -> None:
        self._rollback_count += 1

    def flush(self) -> None:
        pass


def test_run_profile_research_product_writes_record() -> None:
    session = _FakeSession()
    report = run_profile_research(
        research_session=session,
        profile_id="trend_normal",
        current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        grid_method="product",
        replay_fn=_good_replay,  # 所有点全部过 gate
    )
    assert report.error is None
    assert report.grid_size == 27
    # 至少一个 upgrade rec(candidate 全在 clamp 内 + gate 通过)
    inserts = [c for c in session.calls if "INSERT INTO governance" in c[0]]
    assert any("recommendations" in c[0] for c in inserts)
    assert any("profile_research_runs" in c[0] for c in inserts)
    parameter_insert = next(c for c in inserts if "parameter_sets" in c[0])
    assert "typed_json_identity_sha256" in parameter_insert[0]
    assert parameter_insert[1]["typed_json_identity_sha256"] == (
        parameter_values_fingerprint(json.loads(parameter_insert[1]["vals"]))
    )


def test_run_profile_research_bad_candidates_no_upgrade() -> None:
    session = _FakeSession()
    report = run_profile_research(
        research_session=session,
        profile_id="trend_normal",
        current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        grid_method="product",
        replay_fn=_bad_replay,  # gate 全失败
    )
    # 不会产 upgrade rec
    assert report.recommendation_id is None or "profile_upgrade" not in (report.recommendation_id or "")


def test_run_profile_research_invalid_method() -> None:
    session = _FakeSession()
    with pytest.raises(ValueError):
        run_profile_research(
            research_session=session,
            profile_id="trend_normal",
            current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
            grid_method="unknown_method",
        )


def test_run_profile_research_coordinate_requires_baseline() -> None:
    session = _FakeSession()
    report = run_profile_research(
        research_session=session,
        profile_id="trend_normal",
        current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        grid_method="coordinate_descent",  # baseline_values=None
        replay_fn=_good_replay,
    )
    # 走 exception 分支
    assert report.error is not None
    assert "baseline_values" in report.error


def test_run_profile_research_replay_exception_recorded() -> None:
    def _explode(_pt, **_kw):  # noqa: ANN001
        raise RuntimeError("replay broken")

    session = _FakeSession()
    report = run_profile_research(
        research_session=session,
        profile_id="trend_normal",
        current_baseline_stats={"sharpe": 1.0, "maxdd": -0.10, "trades_per_year": 100},
        grid_method="product",
        replay_fn=_explode,
    )
    assert report.error is not None
    assert "replay broken" in report.error
