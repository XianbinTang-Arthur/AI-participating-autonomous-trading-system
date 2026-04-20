"""Batch B migration runner — Phase 1-3 schema evolution (scope + feature flag + saga).

参考: docs/task/rdp_scope_expansion_detailed_design_v3.md §7

Batch B 做十件事:
  - batch_b_01_core_schema.sql: scope 列 + system_config + saga + heartbeat
  - batch_b_02_profile_research.sql: profile_research_runs + streak
  - batch_b_03_cost_calibration.sql: cost_calibration_runs
  - batch_b_04_sleeve_advice.sql: vw_sleeve_advice_recent 视图
  - batch_b_05_microstructure.sql: P1-D Phase 1A bronze + staging microstructure 表
  - batch_b_06_silver_microstructure.sql: P1-D Phase 1A Silver 15m 聚合表 (5 张)
  - batch_b_07_ingest_runs_domain_extension.sql: 扩 domain='microstructure' 白名单
  - batch_b_08_oi_history.sql: P1-D Stage 5 OKX REST OI history bronze 表
  - batch_b_09_mark_ls_history.sql: P1-D Stage 5 OKX REST mark + LS history bronze 表
  - batch_b_11_silver_numeric_widen.sql: P0-a Silver vol_weighted_tfi 精度扩展

每个 stage 对应一个 rollback SQL,逆序回滚。

使用模式(与 _batch_a 一致):
    >>> from aats.data_platform.migrations._batch_b import run_batch_b_migrations
    >>> report = run_batch_b_migrations(engine)
    >>> if not report.ok:
    ...     raise RuntimeError(report.error_message)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine


MIGRATIONS_DIR = Path(__file__).parent


BATCH_B_STAGES: tuple[str, ...] = (
    "batch_b_01_core_schema",
    "batch_b_02_profile_research",
    "batch_b_03_cost_calibration",
    "batch_b_04_sleeve_advice",
    "batch_b_05_microstructure",
    "batch_b_06_silver_microstructure",
    "batch_b_07_ingest_runs_domain_extension",
    "batch_b_08_oi_history",
    "batch_b_09_mark_ls_history",
    "batch_b_11_silver_numeric_widen",
)


@dataclass
class BatchBStageResult:
    stage: str
    ok: bool
    error_message: str | None = None


@dataclass
class BatchBReport:
    stages: list[BatchBStageResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stages)

    @property
    def error_message(self) -> str | None:
        for s in self.stages:
            if not s.ok:
                return f"{s.stage}: {s.error_message}"
        return None


def _load_sql(stage: str, *, rollback: bool = False) -> str:
    suffix = "_rollback.sql" if rollback else ".sql"
    filename = f"{stage}{suffix}"
    path = MIGRATIONS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"migration SQL not found: {path}")
    return path.read_text(encoding="utf-8")


def run_batch_b_migrations(
    engine: Engine,
    *,
    stages: Iterable[str] | None = None,
) -> BatchBReport:
    """按序执行 Batch B migrations。

    每个 stage 在独立 transaction(SQL 文件内部已 BEGIN/COMMIT),
    失败即停;已跑的 stage 保留,未跑的跳过。
    """
    target = tuple(stages) if stages is not None else BATCH_B_STAGES
    report = BatchBReport()

    for stage in target:
        try:
            sql = _load_sql(stage)
            with engine.begin() as conn:
                conn.execute(text(sql))
            report.stages.append(BatchBStageResult(stage=stage, ok=True))
        except Exception as exc:
            report.stages.append(BatchBStageResult(
                stage=stage, ok=False, error_message=str(exc)
            ))
            break

    return report


def run_batch_b_rollback(
    engine: Engine,
    *,
    stages: Iterable[str] | None = None,
) -> BatchBReport:
    """按逆序回滚 Batch B migrations。"""
    target = tuple(stages) if stages is not None else tuple(reversed(BATCH_B_STAGES))
    report = BatchBReport()

    for stage in target:
        try:
            sql = _load_sql(stage, rollback=True)
            with engine.begin() as conn:
                conn.execute(text(sql))
            report.stages.append(BatchBStageResult(stage=stage, ok=True))
        except Exception as exc:
            report.stages.append(BatchBStageResult(
                stage=stage, ok=False, error_message=str(exc)
            ))
            break

    return report
