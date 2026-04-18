"""Profile-level research job — grid search + gate 预检 + recommendation 产出.

设计来源: docs/task/rdp_scope_expansion_detailed_design_v3.md §1.2

职责:
  对每个 profile_id,按 profile_clamps 圈 3x3x3 = 27 点 grid(或 coordinate
  descent 只动一维 3 轮),对每个 point 做 90 天 OOS replay,算 3 指标
  (Sharpe/MaxDD/Activity),跑 Gate 校验,选最佳 candidate。

产出:
  1. governance.profile_research_runs 插一条 run 记录(成功/失败都插,失败写
     error_message)
  2. 如果 candidate 通过 Gate 且在 clamp 内 → 产出 recommendation_type
     ='parameter_upgrade',status='draft' 等操作员审批
  3. 如果最佳 candidate 被 clamp 拒绝(越界) → 调 increment_streak_atomic;
     streak 达到 3 触发 profile_type_review recommendation(status='draft')
  4. 如果最佳 candidate 在 clamp 内 → 调 reset_streak

Observability (R2-07):
  metric: rdp_profile_research_duration_seconds (Histogram)
  labels: profile_id / grid_method / grid_size

本文件只做骨架 + orchestration 逻辑,真实的 OOS replay 对接 Phase 1.5
(replay_engine.py,另行实现)。骨架里 replay 由 ``_run_oos_replay_stub``
返回 deterministic mock,让上层 orchestration 能在 CI 跑通。
"""

from __future__ import annotations

import itertools
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import text

from aats.data_platform.gates.profile_gate import (
    check_profile_gate,
    compute_metrics_from_replay,
)
from aats.data_platform.governance.profile_streak_db import (
    increment_streak_atomic,
    reset_streak,
)
from aats.data_platform.research.profile_clamps import (
    clamp_violation_direction,
    get_profile_clamps,
    is_in_clamp,
)

log = logging.getLogger(__name__)


# Grid 稀疏度 — 每个维度取 3 个 anchor(low / mid / high),共 3^N 组合。
# v3 §1.2 决定:coordinate descent 只动一维 3 轮 = 9 点;product 默认 27 点。
_GRID_METHODS = frozenset({"product", "coordinate_descent"})
_GRID_ANCHORS_PRODUCT = 3
_GRID_ANCHORS_COORDINATE = 3

# Prometheus histogram(R2-07)— 延迟 import 避免无 prometheus 环境失败
_PROM_METRIC = None


def _get_duration_metric():
    """延迟加载 prometheus Histogram,方便 CI 环境跳过。"""
    global _PROM_METRIC
    if _PROM_METRIC is not None:
        return _PROM_METRIC
    try:
        from prometheus_client import Histogram  # type: ignore
        _PROM_METRIC = Histogram(
            "rdp_profile_research_duration_seconds",
            "Profile research job runtime",
            ["profile_id", "grid_method", "grid_size"],
        )
    except Exception:  # pragma: no cover
        class _NoopHistogram:
            def labels(self, **_kw):
                return self
            def observe(self, _v):
                pass
        _PROM_METRIC = _NoopHistogram()
    return _PROM_METRIC


# =============================================================================
# Data models
# =============================================================================

@dataclass(frozen=True)
class GridPoint:
    """Grid 上的一个候选参数点。"""
    values: dict[str, float]


@dataclass(frozen=True)
class ReplayResult:
    """一次 OOS replay 的原始输出。"""
    sharpe: float
    maxdd: float
    trades_per_year: float


@dataclass
class CandidateEval:
    """grid point 在 gate 下的评估结果。"""
    point: GridPoint
    replay: ReplayResult
    gate_metrics: dict[str, float] = field(default_factory=dict)
    gate_allow: bool = False
    gate_failures: tuple[str, ...] = ()
    in_clamp: bool = True
    clamp_direction: str | None = None  # above_upper / below_lower / None


@dataclass
class ProfileResearchReport:
    run_id: str
    profile_id: str
    grid_method: str
    grid_size: int
    oos_window_days: int
    best_candidate: CandidateEval | None
    recommendation_id: str | None
    rejected_by_clamp: bool
    clamp_violation_direction: str | None
    duration_seconds: float
    error: str | None = None


# =============================================================================
# Grid generation
# =============================================================================

def _anchors_for_range(lo: float, hi: float, n: int) -> list[float]:
    """把 [lo, hi] 均匀切 n 份,返回 n 个 anchor(含两端)。"""
    if n < 2:
        raise ValueError(f"anchor count must be >= 2, got {n}")
    step = (hi - lo) / (n - 1)
    return [round(lo + i * step, 6) for i in range(n)]


def build_product_grid(
    profile_id: str,
    *,
    anchors_per_dim: int = _GRID_ANCHORS_PRODUCT,
) -> list[GridPoint]:
    """全乘积 grid: anchors^N 点。N=3 → 27 点。"""
    clamps = get_profile_clamps(profile_id)
    keys = sorted(clamps.keys())
    per_dim = [
        _anchors_for_range(clamps[k]["lo"], clamps[k]["hi"], anchors_per_dim)
        for k in keys
    ]
    points: list[GridPoint] = []
    for combo in itertools.product(*per_dim):
        points.append(GridPoint(values=dict(zip(keys, combo))))
    return points


def build_coordinate_descent_grid(
    profile_id: str,
    *,
    baseline: dict[str, float],
    anchors_per_dim: int = _GRID_ANCHORS_COORDINATE,
) -> list[GridPoint]:
    """Coordinate descent: 从 baseline 出发,每维轮换扫 anchors_per_dim 点。
    产生 N * anchors_per_dim 点(N=3 → 9 点),但 baseline 自身只含一次。
    """
    clamps = get_profile_clamps(profile_id)
    keys = sorted(clamps.keys())
    points: list[GridPoint] = []
    seen: set[tuple[tuple[str, float], ...]] = set()
    for dim in keys:
        anchors = _anchors_for_range(
            clamps[dim]["lo"], clamps[dim]["hi"], anchors_per_dim,
        )
        for val in anchors:
            vals = dict(baseline)
            vals[dim] = val
            key = tuple(sorted(vals.items()))
            if key in seen:
                continue
            seen.add(key)
            points.append(GridPoint(values=vals))
    return points


# =============================================================================
# OOS replay stub (真实实现见 Phase 1.5)
# =============================================================================

def _run_oos_replay_stub(
    point: GridPoint, *, profile_id: str, oos_window_days: int,
) -> ReplayResult:
    """Deterministic mock replay — 允许 orchestration CI 跑通。

    真实实现需要调用 execution_fills / execution_orders 的 OOS replay,
    见 aats.data_platform.replay.profile_replay(TBD)。

    暂时返回一个函数: 值越靠 clamp 中点,Sharpe 越高。用于 unit test。
    """
    clamps = get_profile_clamps(profile_id)
    # dist_score: 0(中点)→ 越大越差
    score = 0.0
    for k, v in point.values.items():
        lo = clamps[k]["lo"]
        hi = clamps[k]["hi"]
        mid = (lo + hi) / 2
        span = hi - lo if hi > lo else 1.0
        score += abs(v - mid) / span
    score /= max(1, len(point.values))

    return ReplayResult(
        sharpe=1.8 - score * 0.6,          # 1.8 最好,最差 ~1.2
        maxdd=-(0.10 + score * 0.05),      # 更差 = 更负
        trades_per_year=260 * (1.0 - score * 0.4),
    )


# =============================================================================
# Gate + evaluation
# =============================================================================

def evaluate_candidate(
    point: GridPoint,
    *,
    profile_id: str,
    current_baseline_stats: dict[str, float],
    replay_fn: Callable[..., ReplayResult],
    oos_window_days: int,
) -> CandidateEval:
    """对单个 grid point 跑 replay + gate。"""
    replay = replay_fn(
        point, profile_id=profile_id, oos_window_days=oos_window_days,
    )
    candidate_stats = {
        "sharpe": replay.sharpe,
        "maxdd": replay.maxdd,
        "trades_per_year": replay.trades_per_year,
    }
    gate_metrics = compute_metrics_from_replay(
        current_stats=current_baseline_stats,
        candidate_stats=candidate_stats,
    )
    gate = check_profile_gate(gate_metrics)

    # clamp direction — 任一维越界就算越界,方向取第一个越界键
    direction: str | None = None
    in_clamp = True
    for k, v in point.values.items():
        if not is_in_clamp(profile_id, k, v):
            in_clamp = False
            direction = clamp_violation_direction(profile_id, k, v)
            break

    return CandidateEval(
        point=point,
        replay=replay,
        gate_metrics=gate_metrics,
        gate_allow=gate.allow_apply,
        gate_failures=gate.failures,
        in_clamp=in_clamp,
        clamp_direction=direction,
    )


def select_best_candidate(
    evals: Iterable[CandidateEval],
) -> CandidateEval | None:
    """从通过 gate 的候选里挑 Sharpe 最高。全部不过返回 None。"""
    passing = [e for e in evals if e.gate_allow and e.in_clamp]
    if not passing:
        return None
    return max(passing, key=lambda e: e.replay.sharpe)


def select_best_violating(
    evals: Iterable[CandidateEval],
) -> CandidateEval | None:
    """全部被 clamp 拒绝时,挑 Sharpe 最高的越界候选。用于 streak 判断。"""
    evals_list = list(evals)
    violators = [e for e in evals_list if not e.in_clamp and e.gate_allow]
    if violators:
        return max(violators, key=lambda e: e.replay.sharpe)
    # 没有 clamp-outside 通过 gate 的 → 挑 sharpe 最高
    if evals_list:
        return max(evals_list, key=lambda e: e.replay.sharpe)
    return None


# =============================================================================
# DB writers
# =============================================================================

def _insert_research_run(
    research_session: Any,
    *,
    report: ProfileResearchReport,
) -> None:
    """写一条 profile_research_runs 记录。失败也要写(error_message)。"""
    metrics: dict[str, Any]
    if report.best_candidate is not None:
        bc = report.best_candidate
        metrics = {
            "values": bc.point.values,
            "replay": {
                "sharpe": bc.replay.sharpe,
                "maxdd": bc.replay.maxdd,
                "trades_per_year": bc.replay.trades_per_year,
            },
            "gate": bc.gate_metrics,
            "gate_failures": list(bc.gate_failures),
            "in_clamp": bc.in_clamp,
            "clamp_direction": bc.clamp_direction,
        }
    else:
        metrics = {}

    research_session.execute(text("""
        INSERT INTO governance.profile_research_runs
            (run_id, profile_id, oos_window_days, grid_size, grid_method,
             metrics, recommendation_id, rejected_by_clamp,
             clamp_violation_direction, started_at, finished_at, error_message)
        VALUES
            (:rid, :pid, :oos, :gsize, :gmethod,
             :metrics::jsonb, :rec, :rejected,
             :direction, NOW() - (:dur || ' seconds')::interval, NOW(), :err)
        ON CONFLICT (run_id) DO NOTHING
    """), {
        "rid": report.run_id,
        "pid": report.profile_id,
        "oos": report.oos_window_days,
        "gsize": report.grid_size,
        "gmethod": report.grid_method,
        "metrics": json.dumps(metrics, ensure_ascii=False),
        "rec": report.recommendation_id,
        "rejected": report.rejected_by_clamp,
        "direction": report.clamp_violation_direction,
        "dur": f"{report.duration_seconds:.3f}",
        "err": report.error,
    })


def _emit_upgrade_recommendation(
    research_session: Any,
    *,
    profile_id: str,
    candidate: CandidateEval,
    run_id: str,
    baseline_parameter_set_id: str | None,
) -> str:
    """产出 parameter_upgrade recommendation(status='draft')。返回 rec_id。

    同时 INSERT 一个新 parameter_set(scope='profile'),rec 指向它。
    """
    rec_id = f"rec-profile-{profile_id}-{uuid.uuid4().hex[:12]}"
    ps_id = f"ps-profile-{profile_id}-{uuid.uuid4().hex[:12]}"

    # 1. parameter_set
    research_session.execute(text("""
        INSERT INTO governance.parameter_sets
            (parameter_set_id, scope, scope_ref, family, timeframe,
             source_round_id, source_phase, dataset_version,
             values, confidence, status, created_at)
        VALUES
            (:psid, 'profile', :pid, NULL, NULL,
             :rid, 'profile_research', 'v1.0',
             :vals::jsonb, 'medium', 'draft', NOW())
    """), {
        "psid": ps_id,
        "pid": profile_id,
        "rid": run_id,
        "vals": json.dumps(candidate.point.values, ensure_ascii=False),
    })

    # 2. recommendation
    review_notes = {
        "source": "profile_research",
        "run_id": run_id,
        "gate_metrics": candidate.gate_metrics,
        "in_clamp": candidate.in_clamp,
    }
    reason = (
        f"profile {profile_id}: Sharpe ratio "
        f"{candidate.gate_metrics.get('sharpe_ratio', 0):.3f}, "
        f"activity {candidate.gate_metrics.get('activity_ratio', 0):.3f}"
    )

    research_session.execute(text("""
        INSERT INTO governance.recommendations
            (recommendation_id, scope, scope_ref, family, symbol, timeframe,
             recommendation_type, target_parameter_set_id,
             confidence, reason, status,
             review_notes, created_at)
        VALUES
            (:rid, 'profile', :pid, NULL, 'BTC-USDT-SWAP', NULL,
             'parameter_upgrade', :psid,
             'medium', :reason, 'draft',
             :notes, NOW())
    """), {
        "rid": rec_id,
        "pid": profile_id,
        "psid": ps_id,
        "reason": reason,
        "notes": json.dumps(review_notes, ensure_ascii=False),
    })

    return rec_id


def _emit_profile_type_review_recommendation(
    research_session: Any,
    *,
    profile_id: str,
    streak_count: int,
    direction: str,
    run_id: str,
) -> str:
    """连续 3 轮 clamp 越界 → 产 profile_type_review rec。"""
    rec_id = f"rec-profile-review-{profile_id}-{uuid.uuid4().hex[:12]}"
    reason = (
        f"profile {profile_id}: {streak_count} consecutive runs pushed "
        f"outside clamp ({direction}); operator review required"
    )
    review_notes = {
        "source": "profile_research",
        "source_type": "profile_type_review",
        "run_id": run_id,
        "streak_count": streak_count,
        "direction": direction,
    }

    research_session.execute(text("""
        INSERT INTO governance.recommendations
            (recommendation_id, scope, scope_ref, family, symbol, timeframe,
             recommendation_type, target_parameter_set_id,
             confidence, reason, status,
             review_notes, created_at)
        VALUES
            (:rid, 'profile', :pid, NULL, 'BTC-USDT-SWAP', NULL,
             'profile_type_review', NULL,
             'high', :reason, 'draft',
             :notes, NOW())
    """), {
        "rid": rec_id,
        "pid": profile_id,
        "reason": reason,
        "notes": json.dumps(review_notes, ensure_ascii=False),
    })

    # 更新 streak 表里的 review_recommendation_id(方便去重)
    research_session.execute(text("""
        UPDATE governance.profile_type_review_streak
        SET review_recommendation_id = :rid
        WHERE profile_id = :pid
    """), {"rid": rec_id, "pid": profile_id})

    return rec_id


# =============================================================================
# Main orchestrator
# =============================================================================

def run_profile_research(
    *,
    research_session: Any,
    profile_id: str,
    current_baseline_stats: dict[str, float],
    baseline_values: dict[str, float] | None = None,
    baseline_parameter_set_id: str | None = None,
    oos_window_days: int = 90,
    grid_method: str = "product",
    streak_threshold: int = 3,
    replay_fn: Callable[..., ReplayResult] | None = None,
) -> ProfileResearchReport:
    """跑一次 profile research。返回 report,已自动写 DB。

    参数:
      current_baseline_stats — {sharpe, maxdd, trades_per_year},用来算 ratio
      baseline_values        — coordinate_descent 的起点;product 模式可为 None
      replay_fn              — 注入实际 replay 实现,不传用 stub
    """
    if grid_method not in _GRID_METHODS:
        raise ValueError(
            f"grid_method must be one of {sorted(_GRID_METHODS)}, got {grid_method!r}"
        )
    if replay_fn is None:
        replay_fn = _run_oos_replay_stub

    run_id = f"run-profile-{profile_id}-{uuid.uuid4().hex[:12]}"
    t_start = time.perf_counter()

    try:
        # 1. Grid 生成
        if grid_method == "product":
            grid = build_product_grid(profile_id)
        else:
            if baseline_values is None:
                raise ValueError(
                    "coordinate_descent requires baseline_values"
                )
            grid = build_coordinate_descent_grid(
                profile_id, baseline=baseline_values,
            )
        grid_size = len(grid)

        # 2. 对每点跑 replay + gate
        evals = [
            evaluate_candidate(
                pt,
                profile_id=profile_id,
                current_baseline_stats=current_baseline_stats,
                replay_fn=replay_fn,
                oos_window_days=oos_window_days,
            )
            for pt in grid
        ]

        # 3. 选 best clamp-in candidate(优先),否则挑 clamp-violator
        best_in_clamp = select_best_candidate(evals)
        rec_id: str | None = None
        rejected_by_clamp = False
        direction: str | None = None

        if best_in_clamp is not None:
            # Clamp-in 且 gate 通过 → 产 upgrade rec + reset streak
            rec_id = _emit_upgrade_recommendation(
                research_session,
                profile_id=profile_id,
                candidate=best_in_clamp,
                run_id=run_id,
                baseline_parameter_set_id=baseline_parameter_set_id,
            )
            reset_streak(research_session, profile_id=profile_id)
            best_candidate = best_in_clamp
        else:
            # 没 clamp-in 通过 gate 的 → 挑 violator 判断 streak
            best_violator = select_best_violating(evals)
            best_candidate = best_violator
            if best_violator is not None and not best_violator.in_clamp:
                # 真正越界 — 走 streak 逻辑
                rejected_by_clamp = True
                direction = best_violator.clamp_direction
                if direction in ("above_upper", "below_lower"):
                    streak = increment_streak_atomic(
                        research_session,
                        profile_id=profile_id,
                        direction=direction,
                        run_id=run_id,
                    )
                    research_session.flush()
                    if streak.streak_count >= streak_threshold:
                        rec_id = _emit_profile_type_review_recommendation(
                            research_session,
                            profile_id=profile_id,
                            streak_count=streak.streak_count,
                            direction=streak.direction,
                            run_id=run_id,
                        )
            # else: grid 空或 gate 全挂 — 不动 streak

        duration = time.perf_counter() - t_start
        report = ProfileResearchReport(
            run_id=run_id,
            profile_id=profile_id,
            grid_method=grid_method,
            grid_size=grid_size,
            oos_window_days=oos_window_days,
            best_candidate=best_candidate,
            recommendation_id=rec_id,
            rejected_by_clamp=rejected_by_clamp,
            clamp_violation_direction=direction,
            duration_seconds=duration,
        )
        _insert_research_run(research_session, report=report)
        research_session.commit()

        _get_duration_metric().labels(
            profile_id=profile_id,
            grid_method=grid_method,
            grid_size=str(grid_size),
        ).observe(duration)

        log.info(
            "profile_research done: profile=%s run=%s grid=%d method=%s "
            "best_sharpe=%s rec=%s rejected=%s duration=%.2fs",
            profile_id, run_id, grid_size, grid_method,
            f"{best_candidate.replay.sharpe:.3f}" if best_candidate else "n/a",
            rec_id, rejected_by_clamp, duration,
        )
        return report

    except Exception as exc:
        duration = time.perf_counter() - t_start
        error_msg = f"{type(exc).__name__}: {exc}"
        log.exception("profile_research failed: profile=%s run=%s", profile_id, run_id)
        try:
            research_session.rollback()
        except Exception:
            pass
        fail_report = ProfileResearchReport(
            run_id=run_id,
            profile_id=profile_id,
            grid_method=grid_method,
            grid_size=0,
            oos_window_days=oos_window_days,
            best_candidate=None,
            recommendation_id=None,
            rejected_by_clamp=False,
            clamp_violation_direction=None,
            duration_seconds=duration,
            error=error_msg,
        )
        try:
            _insert_research_run(research_session, report=fail_report)
            research_session.commit()
        except Exception:
            log.exception("failed to persist failure run record")
        return fail_report


__all__ = [
    "CandidateEval",
    "GridPoint",
    "ProfileResearchReport",
    "ReplayResult",
    "build_coordinate_descent_grid",
    "build_product_grid",
    "evaluate_candidate",
    "run_profile_research",
    "select_best_candidate",
    "select_best_violating",
]
