"""Batch A migration — Python-level executor + structured report.

The orphan-report stage needs structured output (counts, illegal values, etc.),
which the raw .sql file cannot produce. This module keeps the check definitions
in one place so the Python runner and the human-readable .sql stay in sync.

Stages 4.4.2 / 4.4.3 / 4.4.4 / 99 simply stream the corresponding .sql file
through `engine.begin()`. Stage 4.4.1 (this module) is the only one with
programmatic structure.

See: docs/task/rdp_hardening_batch_a_detailed_design.md §4.5
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


MIGRATIONS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class OrphanCheck:
    """FK-gap check: SQL must return zero rows — any row blocks the migration."""

    check_id: str
    title: str
    sql: str


@dataclass(frozen=True)
class DistributionCheck:
    """Value-distribution check: returned values must fall inside `allowlist`.

    SQL must return rows shaped as (value, row_count). Any value outside the
    allowlist is flagged and blocks the migration.
    """

    check_id: str
    title: str
    sql: str
    allowlist: frozenset[str]


# ----------------------------------------------------------------------------
# Hard orphan checks — all must return 0 rows.
# ----------------------------------------------------------------------------
ORPHAN_CHECKS: tuple[OrphanCheck, ...] = (
    OrphanCheck(
        check_id="01",
        title="active_parameter_sets.parameter_set_id -> parameter_sets",
        sql="""
            SELECT a.family, a.timeframe, a.parameter_set_id
            FROM governance.active_parameter_sets a
            LEFT JOIN governance.parameter_sets p
              ON a.parameter_set_id = p.parameter_set_id
            WHERE p.parameter_set_id IS NULL
        """,
    ),
    OrphanCheck(
        check_id="02",
        title="parameter_apply_history.to_parameter_set_id -> parameter_sets",
        sql="""
            SELECT h.operation_id, h.family, h.timeframe, h.to_parameter_set_id
            FROM governance.parameter_apply_history h
            LEFT JOIN governance.parameter_sets p
              ON h.to_parameter_set_id = p.parameter_set_id
            WHERE h.to_parameter_set_id IS NOT NULL
              AND p.parameter_set_id IS NULL
        """,
    ),
    OrphanCheck(
        check_id="03",
        title="parameter_apply_history.from_parameter_set_id -> parameter_sets",
        sql="""
            SELECT h.operation_id, h.family, h.timeframe, h.from_parameter_set_id
            FROM governance.parameter_apply_history h
            LEFT JOIN governance.parameter_sets p
              ON h.from_parameter_set_id = p.parameter_set_id
            WHERE h.from_parameter_set_id IS NOT NULL
              AND p.parameter_set_id IS NULL
        """,
    ),
    OrphanCheck(
        check_id="04a",
        title="parameter_releases.parameter_set_id -> parameter_sets",
        sql="""
            SELECT r.release_id, r.parameter_set_id AS missing_parameter_set_id
            FROM governance.parameter_releases r
            LEFT JOIN governance.parameter_sets p
              ON r.parameter_set_id = p.parameter_set_id
            WHERE p.parameter_set_id IS NULL
        """,
    ),
    OrphanCheck(
        check_id="04b",
        title="parameter_releases.previous_parameter_set_id -> parameter_sets",
        sql="""
            SELECT r.release_id, r.previous_parameter_set_id AS missing_previous_parameter_set_id
            FROM governance.parameter_releases r
            LEFT JOIN governance.parameter_sets p
              ON r.previous_parameter_set_id = p.parameter_set_id
            WHERE r.previous_parameter_set_id IS NOT NULL
              AND p.parameter_set_id IS NULL
        """,
    ),
    OrphanCheck(
        check_id="05",
        title="rollback_recommendations.suggested_target_parameter_set_id -> parameter_sets",
        sql="""
            SELECT r.release_id, r.suggested_target_parameter_set_id
            FROM governance.rollback_recommendations r
            LEFT JOIN governance.parameter_sets p
              ON r.suggested_target_parameter_set_id = p.parameter_set_id
            WHERE r.suggested_target_parameter_set_id IS NOT NULL
              AND p.parameter_set_id IS NULL
        """,
    ),
    OrphanCheck(
        check_id="06",
        title="active_decisions.active_parameter_set_id -> parameter_sets",
        sql="""
            SELECT d.family, d.timeframe, d.active_parameter_set_id
            FROM governance.active_decisions d
            LEFT JOIN governance.parameter_sets p
              ON d.active_parameter_set_id = p.parameter_set_id
            WHERE d.active_parameter_set_id IS NOT NULL
              AND p.parameter_set_id IS NULL
        """,
    ),
)


# ----------------------------------------------------------------------------
# Soft distribution checks — rows must fall inside allowlist.
# ----------------------------------------------------------------------------
DISTRIBUTION_CHECKS: tuple[DistributionCheck, ...] = (
    DistributionCheck(
        check_id="7a",
        title="parameter_sets.status",
        sql="SELECT status, COUNT(*) AS rows FROM governance.parameter_sets GROUP BY status ORDER BY status",
        # Mirrors VALID_PS_STATUSES in aats/data_platform/governance/_db_util.py.
        allowlist=frozenset({"draft", "candidate", "frozen", "deprecated"}),
    ),
    DistributionCheck(
        check_id="7b",
        title="recommendations.status",
        sql="SELECT status, COUNT(*) AS rows FROM governance.recommendations GROUP BY status ORDER BY status",
        # Mirrors VALID_REC_STATUSES in aats/data_platform/governance/_db_util.py.
        allowlist=frozenset({"draft", "approved", "rejected", "superseded"}),
    ),
    DistributionCheck(
        check_id="7c",
        title="parameter_apply_history.operation_type",
        sql="SELECT operation_type, COUNT(*) AS rows FROM governance.parameter_apply_history GROUP BY operation_type ORDER BY operation_type",
        # `clear` is emitted by active_parameter_apply.py when wiping a combo.
        allowlist=frozenset({"apply", "rollback", "clear"}),
    ),
    DistributionCheck(
        check_id="7d",
        title="parameter_releases.apply_result",
        sql="SELECT apply_result, COUNT(*) AS rows FROM governance.parameter_releases GROUP BY apply_result ORDER BY apply_result",
        # Writers: release_registry.py — pending / blocked_by_gate / success / failed.
        allowlist=frozenset({"pending", "blocked_by_gate", "success", "failed"}),
    ),
    DistributionCheck(
        check_id="7e",
        title="parameter_releases.observation_status",
        sql="SELECT observation_status, COUNT(*) AS rows FROM governance.parameter_releases GROUP BY observation_status ORDER BY observation_status",
        # Writers: release_registry.py + observation_window.py.
        allowlist=frozenset({"pending", "observing", "completed", "rollback_recommended", "rolled_back"}),
    ),
    DistributionCheck(
        check_id="7f1",
        title="observation_results.status",
        sql="SELECT status, COUNT(*) AS rows FROM governance.observation_results GROUP BY status ORDER BY status",
        # Writers: observation_window.py lines 323/327/330/334/337.
        allowlist=frozenset({"observing", "completed", "rollback_recommended"}),
    ),
    DistributionCheck(
        check_id="7f2",
        title="observation_results.recommendation",
        sql="SELECT recommendation, COUNT(*) AS rows FROM governance.observation_results GROUP BY recommendation ORDER BY recommendation",
        # Writers: observation_window.py — keep / review / rollback_recommended.
        allowlist=frozenset({"keep", "review", "rollback_recommended"}),
    ),
    DistributionCheck(
        check_id="7g",
        title="rollback_recommendations.severity",
        sql="SELECT severity, COUNT(*) AS rows FROM governance.rollback_recommendations GROUP BY severity ORDER BY severity",
        # Writers: rollback_policy.py — none / medium / high. Column default 'none'.
        allowlist=frozenset({"none", "medium", "high"}),
    ),
    DistributionCheck(
        check_id="7h",
        title="release_effectiveness.conclusion",
        sql="SELECT conclusion, COUNT(*) AS rows FROM governance.release_effectiveness GROUP BY conclusion ORDER BY conclusion",
        # Writers: _derive_effectiveness in metrics/release_effectiveness.py.
        allowlist=frozenset({"rollback_triggered", "insufficient_evidence", "ineffective", "effective", "mixed"}),
    ),
)


BACKFILL_PROBE_SQL = """
    SELECT
      SUM(CASE WHEN p.source_round_id IS NOT NULL THEN 1 ELSE 0 END) AS will_populate,
      SUM(CASE WHEN p.source_round_id IS NULL THEN 1 ELSE 0 END) AS will_stay_null,
      COUNT(*) AS total_recommendations
    FROM governance.recommendations r
    LEFT JOIN governance.parameter_sets p
      ON r.target_parameter_set_id = p.parameter_set_id
"""


@dataclass
class OrphanReport:
    """Structured result of stage 4.4.1 orphan + distribution scan."""

    orphans: dict[str, dict[str, Any]] = field(default_factory=dict)
    distributions: dict[str, dict[str, Any]] = field(default_factory=dict)
    backfill_probe: dict[str, Any] = field(default_factory=dict)

    @property
    def orphan_row_total(self) -> int:
        return sum(v["row_count"] for v in self.orphans.values())

    @property
    def illegal_value_total(self) -> int:
        return sum(len(v["illegal_values"]) for v in self.distributions.values())

    @property
    def is_clean(self) -> bool:
        return self.orphan_row_total == 0 and self.illegal_value_total == 0

    def summary_dict(self) -> dict[str, Any]:
        return {
            "orphan_row_total": self.orphan_row_total,
            "illegal_value_total": self.illegal_value_total,
            "is_clean": self.is_clean,
            "orphans": self.orphans,
            "distributions": self.distributions,
            "backfill_probe": self.backfill_probe,
        }


def run_orphan_report(engine: Engine) -> OrphanReport:
    """Execute all stage 4.4.1 checks and return a structured report.

    Purely read-only — safe on production. The caller decides whether to
    continue to stage 4.4.2 based on `report.is_clean`.
    """
    report = OrphanReport()

    with engine.connect() as conn:
        for check in ORPHAN_CHECKS:
            rows = conn.execute(text(check.sql)).fetchall()
            report.orphans[check.check_id] = {
                "title": check.title,
                "row_count": len(rows),
                "rows": [dict(r._mapping) for r in rows],
            }

        for check in DISTRIBUTION_CHECKS:
            rows = conn.execute(text(check.sql)).fetchall()
            observed_values = [r[0] for r in rows]
            illegal_values = [v for v in observed_values if v not in check.allowlist]
            report.distributions[check.check_id] = {
                "title": check.title,
                "allowlist": sorted(check.allowlist),
                "observed": [
                    {"value": r[0], "rows": r[1]} for r in rows
                ],
                "illegal_values": illegal_values,
            }

        backfill_row = conn.execute(text(BACKFILL_PROBE_SQL)).first()
        if backfill_row is not None:
            report.backfill_probe = {
                "will_populate": int(backfill_row[0] or 0),
                "will_stay_null": int(backfill_row[1] or 0),
                "total_recommendations": int(backfill_row[2] or 0),
            }

    return report


def format_report_text(report: OrphanReport) -> str:
    """Render an OrphanReport as human-readable text for CLI output."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("RDP Batch A — Stage 4.4.1 Orphan / Distribution Report")
    lines.append("=" * 72)
    lines.append("")

    lines.append("## Orphan checks (must all return 0 rows)")
    for check_id, data in report.orphans.items():
        status = "OK  " if data["row_count"] == 0 else "FAIL"
        lines.append(f"  [{status}] {check_id}. {data['title']}: {data['row_count']} rows")
        for row in data["rows"][:5]:
            lines.append(f"          {row}")
        if len(data["rows"]) > 5:
            lines.append(f"          ... and {len(data['rows']) - 5} more rows")
    lines.append("")

    lines.append("## Distribution checks (values must fall inside allowlist)")
    for check_id, data in report.distributions.items():
        has_illegal = bool(data["illegal_values"])
        status = "FAIL" if has_illegal else "OK  "
        lines.append(f"  [{status}] {check_id}. {data['title']}")
        lines.append(f"          allowlist: {data['allowlist']}")
        for obs in data["observed"]:
            marker = "!!" if obs["value"] not in data["allowlist"] else "  "
            lines.append(f"          {marker} {obs['value']!r}: {obs['rows']} rows")
    lines.append("")

    lines.append("## Backfill probe (recommendations.source_round_id via join)")
    bp = report.backfill_probe
    if bp:
        lines.append(
            f"  total={bp['total_recommendations']}  "
            f"will_populate={bp['will_populate']}  "
            f"will_stay_null={bp['will_stay_null']}"
        )
    lines.append("")

    lines.append("-" * 72)
    if report.is_clean:
        lines.append("RESULT: CLEAN — stage 4.4.2 (add FKs) may proceed.")
    else:
        lines.append("RESULT: DIRTY — migration blocked until data is triaged.")
        lines.append(
            f"        orphan rows: {report.orphan_row_total}, "
            f"illegal distribution values: {report.illegal_value_total}"
        )
    lines.append("-" * 72)

    return "\n".join(lines)


def load_migration_sql(stage: str) -> str:
    """Load a .sql file by short stage name. Raises FileNotFoundError if absent."""
    filename_map = {
        "orphan_report": "batch_a_01_orphan_report.sql",
        "fks": "batch_a_02_add_fks.sql",
        "uqs": "batch_a_03_add_uqs.sql",
        "checks": "batch_a_04_add_checks.sql",
        "rollback": "batch_a_99_rollback.sql",
    }
    if stage not in filename_map:
        raise ValueError(f"unknown stage: {stage!r}; expected one of {sorted(filename_map)}")
    path = MIGRATIONS_DIR / filename_map[stage]
    if not path.exists():
        raise FileNotFoundError(f"migration SQL not found: {path} (stage {stage!r} not yet implemented)")
    return path.read_text(encoding="utf-8")
