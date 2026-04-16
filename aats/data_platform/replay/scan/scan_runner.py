"""Parameter scan runner: batch experiment execution.

Phase 2 设计决策 §11：
- 给定参数网格，自动运行多组 experiment
- 生成结构化对比结果
- 每组参数都应形成 experiment entry + result artifact + diagnostics summary

不做：
- 复杂搜索算法
- 黑盒优化器
- 大规模分布式并行
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from aats.data_platform.replay.adapters.base_adapter import BaseReplayAdapter
from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
from aats.data_platform.replay.core.replay_result_writer import (
    write_decisions_csv,
    write_summary_json,
)
from aats.data_platform.replay.core.replay_runner import run_replay
from aats.data_platform.replay.diagnostics.replay_diagnostics import (
    compare_diagnostics,
    compute_diagnostics,
)
from aats.data_platform.replay.registry.experiment_registry import (
    create_experiment,
    mark_experiment_failed,
    mark_experiment_running,
    mark_experiment_succeeded,
    upsert_experiment_summary,
)
from aats.data_platform.replay.scan.parameter_grid import (
    build_grid,
    combo_label,
    grid_to_json,
)

log = logging.getLogger(__name__)

# 产物根目录
_ARTIFACT_ROOT = pathlib.Path("artifacts/research/experiments")


# ---------------------------------------------------------------------------
# Scan run CRUD
# ---------------------------------------------------------------------------

def create_scan_run(
    session: Session,
    *,
    family: str,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    parameter_grid: dict[str, list[Any]],
    total_combinations: int,
    notes: str | None = None,
) -> UUID:
    """创建 parameter_scan_runs 记录。"""
    row = session.execute(
        text("""
            INSERT INTO research.parameter_scan_runs
                (family, symbol, timeframe, dataset_version,
                 parameter_grid, total_combinations, status, notes)
            VALUES
                (:fam, :sym, :tf, :dv, :pg, :tc, 'pending', :notes)
            RETURNING scan_run_id
        """),
        {
            "fam": family,
            "sym": symbol,
            "tf": timeframe,
            "dv": dataset_version,
            "pg": json.dumps(parameter_grid, ensure_ascii=False),
            "tc": total_combinations,
            "notes": notes,
        },
    )
    scan_id = row.scalar_one()
    session.flush()
    log.info("Created scan run %s (%d combinations)", scan_id, total_combinations)
    return scan_id


def mark_scan_running(session: Session, scan_run_id: UUID) -> None:
    session.execute(
        text("""
            UPDATE research.parameter_scan_runs
            SET status = 'running', started_at = :now, updated_at = :now
            WHERE scan_run_id = :sid
        """),
        {"sid": str(scan_run_id), "now": datetime.now(timezone.utc)},
    )
    session.flush()


def mark_scan_finished(
    session: Session,
    scan_run_id: UUID,
    *,
    completed: int,
    failed: int,
    comparison_path: str | None = None,
) -> None:
    """更新 scan run 最终状态。

    状态逻辑（P1-2: 对"部分成功"诚实）：
    - failed == 0               -> succeeded     全部成功
    - completed == 0            -> failed         全部失败
    - completed > 0 and failed > 0 -> partial_success 部分成功
    """
    if failed == 0:
        status = "succeeded"
    elif completed == 0:
        status = "failed"
    else:
        status = "partial_success"
    session.execute(
        text("""
            UPDATE research.parameter_scan_runs
            SET status = :st,
                completed_count = :cc,
                failed_count = :fc,
                comparison_path = :cp,
                finished_at = :now,
                updated_at = :now
            WHERE scan_run_id = :sid
        """),
        {
            "sid": str(scan_run_id),
            "st": status,
            "cc": completed,
            "fc": failed,
            "cp": comparison_path,
            "now": datetime.now(timezone.utc),
        },
    )
    session.flush()


# ---------------------------------------------------------------------------
# 主扫描流程
# ---------------------------------------------------------------------------

def run_parameter_scan(
    session: Session,
    *,
    adapter: BaseReplayAdapter,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    start_ts: datetime,
    end_ts: datetime,
    parameter_grid: dict[str, list[Any]] | None = None,
    base_params: dict[str, Any] | None = None,
    artifact_root: pathlib.Path | None = None,
) -> UUID:
    """执行一次完整参数扫描。

    流程：
    1. 创建 scan_run 记录
    2. 展开参数网格
    3. 逐组合运行 replay + diagnostics
    4. 生成 comparison summary
    5. 更新 scan_run 状态

    返回 scan_run_id。
    """
    if artifact_root is None:
        artifact_root = _ARTIFACT_ROOT

    grid = grid_to_json(parameter_grid)
    combos = build_grid(parameter_grid, base_params=base_params)

    # 1. 创建 scan_run
    scan_run_id = create_scan_run(
        session,
        family=adapter.family_name,
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=dataset_version,
        parameter_grid=grid,
        total_combinations=len(combos),
    )
    mark_scan_running(session, scan_run_id)
    session.commit()

    log.info("Starting parameter scan %s: %d combinations", scan_run_id, len(combos))

    # 2. 逐组合运行
    completed = 0
    failed = 0
    all_diagnostics: list[dict[str, Any]] = []
    all_labels: list[str] = []
    failed_combos: list[dict[str, Any]] = []

    for i, params in enumerate(combos):
        label = combo_label(params)
        log.info("[%d/%d] Running combo: %s", i + 1, len(combos), label)

        try:
            diag = _run_single_experiment(
                session,
                adapter=adapter,
                symbol=symbol,
                timeframe=timeframe,
                dataset_version=dataset_version,
                start_ts=start_ts,
                end_ts=end_ts,
                params=params,
                scan_run_id=scan_run_id,
                artifact_root=artifact_root,
                label=label,
            )
            all_diagnostics.append(diag)
            all_labels.append(label)
            completed += 1
        except Exception as exc:
            log.exception("Combo %s failed", label)
            failed_combos.append({
                "label": label,
                "parameters": params.to_dict(),
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
            failed += 1

        session.commit()

    # 3. comparison summary + failed combos artifact
    scan_dir = artifact_root / str(scan_run_id)
    comparison_path: str | None = None
    if all_diagnostics:
        comparison = compare_diagnostics(all_diagnostics, all_labels)
        comp_file = scan_dir / "comparison_summary.json"
        write_summary_json(comparison, comp_file)
        comparison_path = str(comp_file)
        log.info("Wrote comparison summary to %s", comp_file)

    # 写 failed combos artifact（无论有无失败都写，方便查验）
    failed_file = scan_dir / "failed_combos.json"
    write_summary_json(
        {
            "scan_run_id": str(scan_run_id),
            "total_combinations": len(combos),
            "failed_count": failed,
            "completed_count": completed,
            "failed_combos": failed_combos,
        },
        failed_file,
    )
    if failed_combos:
        log.warning("Wrote %d failed combo(s) to %s", len(failed_combos), failed_file)

    # 4. 更新 scan_run
    mark_scan_finished(
        session, scan_run_id,
        completed=completed, failed=failed,
        comparison_path=comparison_path,
    )
    session.commit()

    final_status = "succeeded" if failed == 0 else ("failed" if completed == 0 else "partial_success")
    log.info(
        "Parameter scan %s finished [%s]: %d/%d succeeded, %d failed",
        scan_run_id, final_status, completed, len(combos), failed,
    )
    if failed > 0 and completed > 0:
        log.warning(
            "Scan %s has partial failures (%d/%d failed). "
            "Check individual experiment logs for details.",
            scan_run_id, failed, len(combos),
        )
    return scan_run_id


# ---------------------------------------------------------------------------
# 单次实验执行
# ---------------------------------------------------------------------------

def _run_single_experiment(
    session: Session,
    *,
    adapter: BaseReplayAdapter,
    symbol: str,
    timeframe: str,
    dataset_version: str,
    start_ts: datetime,
    end_ts: datetime,
    params: ReplayParameterOverrides,
    scan_run_id: UUID,
    artifact_root: pathlib.Path,
    label: str,
) -> dict[str, Any]:
    """运行单次实验并返回诊断结果。"""
    # 创建实验
    exp_id = create_experiment(
        session,
        family=adapter.family_name,
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=dataset_version,
        parameter_overrides=params.to_dict(),
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        scan_run_id=scan_run_id,
        notes=f"scan combo: {label}",
    )
    mark_experiment_running(session, exp_id)
    session.flush()

    try:
        # 运行 replay
        decisions = run_replay(
            session,
            adapter=adapter,
            symbol=symbol,
            timeframe=timeframe,
            dataset_version=dataset_version,
            start_ts=start_ts,
            end_ts=end_ts,
            params=params,
        )

        # 写 artifact
        exp_dir = artifact_root / str(scan_run_id) / str(exp_id)
        result_path = write_decisions_csv(decisions, exp_dir / "replay_decisions.csv")
        diag = compute_diagnostics(decisions)
        summary_path = write_summary_json(diag, exp_dir / "diagnostics.json")

        # 写 registry
        upsert_experiment_summary(session, exp_id, summary=diag)
        mark_experiment_succeeded(
            session, exp_id,
            bar_count=len(decisions),
            result_path=str(result_path),
            summary_path=str(summary_path),
        )
        return diag

    except Exception as exc:
        mark_experiment_failed(session, exp_id, error_message=str(exc))
        raise
