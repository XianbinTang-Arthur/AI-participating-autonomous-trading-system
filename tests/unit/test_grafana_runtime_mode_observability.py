"""P0-b Task 2.2 + 2.3 regression guard.

验证 Grafana dashboard 和 alerting 规则里包含 P0-b governance observability 资源:
- dashboard ``aats_operations.json`` 包含 "Runtime Trading Mode" stat panel
- ``rules.yml`` 包含 2 条 runtime governance alerts

对应 spec: docs/governance/p0b_observability_implementation_spec_2026_04_20.md §2.2 + §2.3.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS_DIR = REPO_ROOT / "deploy" / "wsl2-dev" / "grafana" / "provisioning" / "dashboards" / "files" / "AATS"
ALERTING_DIR = REPO_ROOT / "deploy" / "wsl2-dev" / "grafana" / "provisioning" / "alerting"


class TestGrafanaRuntimeModePanel(unittest.TestCase):
    """Task 2.2: ``aats_operations.json`` 包含 Runtime Trading Mode stat panel."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = json.loads(
            (DASHBOARDS_DIR / "aats_operations.json").read_text(encoding="utf-8")
        )

    def test_dashboard_has_runtime_mode_panel(self) -> None:
        panels = self.dashboard["panels"]
        runtime_panels = [
            p for p in panels if "Runtime Trading Mode" in (p.get("title") or "")
        ]
        self.assertEqual(
            len(runtime_panels), 1,
            "dashboard 必须恰好有一个 Runtime Trading Mode stat panel",
        )
        panel = runtime_panels[0]
        self.assertEqual(panel["type"], "stat", "Runtime Trading Mode 必须是 Stat 类型 (大字报)")

    def test_runtime_mode_panel_queries_prometheus_and_postgres(self) -> None:
        panel = next(p for p in self.dashboard["panels"] if "Runtime Trading Mode" in (p.get("title") or ""))
        targets = panel.get("targets", [])
        self.assertGreaterEqual(len(targets), 2, "必须有 Prometheus + Postgres fallback 两个 query")
        prom_targets = [t for t in targets if t.get("datasource", {}).get("uid") == "prometheus"]
        pg_targets = [t for t in targets if t.get("datasource", {}).get("uid") == "postgres"]
        self.assertTrue(prom_targets, "必须有 Prometheus target")
        self.assertTrue(pg_targets, "必须有 Postgres fallback target")
        # Prometheus query 必须引用 aats_runtime_ai_operating_mode
        self.assertIn(
            "aats_runtime_ai_operating_mode",
            prom_targets[0]["expr"],
            "Prometheus query 要命中 Task 2.4 注册的 metric 名",
        )
        # Postgres fallback 必须查 strategy.decision_outcome 的 ai_operating_mode
        pg_sql = pg_targets[0].get("rawSql", "")
        self.assertIn("strategy.decision_outcome", pg_sql)
        self.assertIn("ai_operating_mode", pg_sql)

    def test_runtime_mode_panel_has_value_mappings(self) -> None:
        panel = next(p for p in self.dashboard["panels"] if "Runtime Trading Mode" in (p.get("title") or ""))
        mappings_list = (
            panel.get("fieldConfig", {}).get("defaults", {}).get("mappings", [])
        )
        self.assertTrue(mappings_list, "需要 value mappings 做 mode → 文案 的本地化")
        mapping_options = mappings_list[0].get("options", {})
        self.assertIn("baseline_only", mapping_options)
        self.assertIn("ai_assisted", mapping_options)
        self.assertIn("ai_decision_maker", mapping_options)
        # 三种 mode 的颜色语义
        self.assertEqual(mapping_options["baseline_only"]["color"], "blue")
        self.assertEqual(mapping_options["ai_assisted"]["color"], "orange")
        self.assertEqual(mapping_options["ai_decision_maker"]["color"], "red")


class TestGrafanaRuntimeModeAlerts(unittest.TestCase):
    """Task 2.3: rules.yml 包含 2 条 runtime governance 告警."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = yaml.safe_load(
            (ALERTING_DIR / "rules.yml").read_text(encoding="utf-8")
        )

    def _all_rule_uids(self) -> set[str]:
        uids: set[str] = set()
        for group in self.rules.get("groups", []):
            for rule in group.get("rules", []):
                uid = rule.get("uid")
                if uid:
                    uids.add(str(uid))
        return uids

    def test_alert_sev2_baseline_has_orders_defined(self) -> None:
        uids = self._all_rule_uids()
        self.assertIn(
            "sev2-runtime-baseline-has-orders",
            uids,
            "Task 2.3: 缺 SEV-2 baseline_only-has-orders 告警",
        )

    def test_alert_sev3_ai_decision_no_orders_defined(self) -> None:
        uids = self._all_rule_uids()
        self.assertIn(
            "sev3-runtime-ai-decision-no-orders",
            uids,
            "Task 2.3: 缺 SEV-3 ai_decision_maker-no-orders 告警",
        )

    def test_alerts_have_runtime_governance_component_label(self) -> None:
        runtime_rules = []
        for group in self.rules.get("groups", []):
            for rule in group.get("rules", []):
                if str(rule.get("uid", "")).startswith(("sev2-runtime-", "sev3-runtime-")):
                    runtime_rules.append(rule)
        self.assertGreaterEqual(len(runtime_rules), 2)
        for rule in runtime_rules:
            labels = rule.get("labels", {})
            self.assertEqual(
                labels.get("component"),
                "runtime_governance",
                f"rule {rule.get('uid')} 缺 component=runtime_governance label",
            )


if __name__ == "__main__":
    unittest.main()
