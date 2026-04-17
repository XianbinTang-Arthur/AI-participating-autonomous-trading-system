"""Unit tests for the Batch A migration helper module.

Purely structural checks — verifies the ORPHAN_CHECKS / DISTRIBUTION_CHECKS
lists are well-formed, the `format_report_text` helper handles edge cases,
and `load_migration_sql` dispatches to the correct filename. DB-bound
behaviour (running the orphan report against a real schema) is covered by
`tests/integration/test_rdp_batch_a_*` which use testcontainers.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from aats.data_platform.migrations import _batch_a
from aats.data_platform.migrations._batch_a import (
    DISTRIBUTION_CHECKS,
    ORPHAN_CHECKS,
    DistributionCheck,
    OrphanCheck,
    OrphanReport,
    format_report_text,
    load_migration_sql,
)


class TestOrphanCheckRegistry(unittest.TestCase):
    def test_orphan_check_ids_are_unique(self) -> None:
        ids = [c.check_id for c in ORPHAN_CHECKS]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate check ids: {ids}")

    def test_orphan_check_sql_targets_governance_schema(self) -> None:
        for check in ORPHAN_CHECKS:
            self.assertIn(
                "governance.",
                check.sql,
                f"check {check.check_id} missing governance schema prefix",
            )
            self.assertIn(
                "LEFT JOIN governance.parameter_sets",
                check.sql,
                f"check {check.check_id} must LEFT JOIN parameter_sets to detect orphans",
            )
            self.assertIn(
                "IS NULL",
                check.sql,
                f"check {check.check_id} must filter for missing (IS NULL) rows",
            )


class TestDistributionCheckRegistry(unittest.TestCase):
    def test_distribution_ids_are_unique(self) -> None:
        ids = [c.check_id for c in DISTRIBUTION_CHECKS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_distribution_allowlists_are_nonempty(self) -> None:
        for check in DISTRIBUTION_CHECKS:
            self.assertGreater(
                len(check.allowlist),
                0,
                f"check {check.check_id} has empty allowlist",
            )

    def test_distribution_sql_returns_value_and_rows_columns(self) -> None:
        for check in DISTRIBUTION_CHECKS:
            self.assertIn("GROUP BY", check.sql)
            self.assertIn("COUNT(*)", check.sql)


class TestExpectedCheckCoverage(unittest.TestCase):
    """Guard against accidental removal of critical checks during future edits."""

    def test_all_seven_orphan_categories_present(self) -> None:
        titles = {c.check_id: c.title for c in ORPHAN_CHECKS}
        for prefix in ("01", "02", "03", "04a", "04b", "05", "06"):
            self.assertIn(prefix, titles, f"missing orphan check {prefix}")

    def test_status_allowlist_for_parameter_sets_matches_check_constraint(self) -> None:
        target = next(c for c in DISTRIBUTION_CHECKS if c.check_id == "7a")
        self.assertEqual(
            target.allowlist,
            frozenset({"draft", "candidate", "frozen", "released", "deprecated"}),
        )

    def test_status_allowlist_for_recommendations_matches_check_constraint(self) -> None:
        target = next(c for c in DISTRIBUTION_CHECKS if c.check_id == "7b")
        self.assertEqual(
            target.allowlist,
            frozenset({"draft", "approved", "rejected", "superseded", "applied", "rolled_back"}),
        )


class TestOrphanReportProperties(unittest.TestCase):
    def test_empty_report_is_clean(self) -> None:
        report = OrphanReport()
        self.assertTrue(report.is_clean)
        self.assertEqual(report.orphan_row_total, 0)
        self.assertEqual(report.illegal_value_total, 0)

    def test_report_with_orphans_is_dirty(self) -> None:
        report = OrphanReport()
        report.orphans["01"] = {"title": "x", "row_count": 3, "rows": [{"a": 1}, {"a": 2}, {"a": 3}]}
        self.assertEqual(report.orphan_row_total, 3)
        self.assertFalse(report.is_clean)

    def test_report_with_illegal_values_is_dirty(self) -> None:
        report = OrphanReport()
        report.distributions["7a"] = {
            "title": "x",
            "allowlist": ["ok"],
            "observed": [{"value": "bad", "rows": 1}],
            "illegal_values": ["bad"],
        }
        self.assertEqual(report.illegal_value_total, 1)
        self.assertFalse(report.is_clean)


class TestFormatReportText(unittest.TestCase):
    def test_empty_report_renders_clean(self) -> None:
        report = OrphanReport()
        text = format_report_text(report)
        self.assertIn("RESULT: CLEAN", text)
        self.assertNotIn("RESULT: DIRTY", text)

    def test_report_with_orphans_renders_dirty(self) -> None:
        report = OrphanReport()
        report.orphans["01"] = {
            "title": "active_parameter_sets",
            "row_count": 2,
            "rows": [{"family": "trend", "timeframe": "1h", "parameter_set_id": "ps_xyz"}] * 2,
        }
        text = format_report_text(report)
        self.assertIn("RESULT: DIRTY", text)
        self.assertIn("[FAIL] 01", text)

    def test_report_truncates_long_row_lists(self) -> None:
        report = OrphanReport()
        report.orphans["01"] = {
            "title": "t",
            "row_count": 9,
            "rows": [{"x": i} for i in range(9)],
        }
        text = format_report_text(report)
        self.assertIn("and 4 more rows", text)

    def test_report_flags_illegal_distribution_value(self) -> None:
        report = OrphanReport()
        report.distributions["7a"] = {
            "title": "parameter_sets.status",
            "allowlist": ["draft", "frozen"],
            "observed": [
                {"value": "draft", "rows": 5},
                {"value": "mystery", "rows": 1},
            ],
            "illegal_values": ["mystery"],
        }
        text = format_report_text(report)
        self.assertIn("!!", text)
        self.assertIn("mystery", text)


class TestLoadMigrationSql(unittest.TestCase):
    def test_orphan_report_sql_exists_and_contains_expected_markers(self) -> None:
        sql = load_migration_sql("orphan_report")
        self.assertIn("governance.active_parameter_sets", sql)
        self.assertIn("governance.parameter_apply_history", sql)
        self.assertIn("governance.parameter_sets", sql)
        self.assertIn("READ-ONLY", sql)

    def test_unknown_stage_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            load_migration_sql("bogus")

    def test_unimplemented_stage_raises_file_not_found(self) -> None:
        for stage in ("fks", "uqs", "checks", "rollback"):
            path = _batch_a.MIGRATIONS_DIR / {
                "fks": "batch_a_02_add_fks.sql",
                "uqs": "batch_a_03_add_uqs.sql",
                "checks": "batch_a_04_add_checks.sql",
                "rollback": "batch_a_99_rollback.sql",
            }[stage]
            if path.exists():
                self.skipTest(f"{stage} SQL already exists — expected only after D3")
            with self.assertRaises(FileNotFoundError):
                load_migration_sql(stage)


if __name__ == "__main__":
    unittest.main()
