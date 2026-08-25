"""P0-b Task 2.2 + 2.3 regression guard.

验证 Grafana dashboard 和 alerting 规则里包含 P0-b governance observability 资源:
- dashboard ``aats_operations.json`` 包含 "Runtime Trading Mode" stat panel
- ``rules.yml`` 包含 runtime governance alert (2026-04-23 勘误后仅保留 sev3)

对应 spec: docs/governance/p0b_observability_implementation_spec_2026_04_20.md §2.2 + §2.3.

2026-04-23 勘误: 原 sev2-runtime-baseline-has-orders 告警基于错误假设
("baseline_only = 不下单") 已删除, 对应 `test_alert_sev2_baseline_has_orders_defined`
测试及 `test_alerts_have_runtime_governance_component_label` 的 `>=2` 断言已相应修订.
见 docs/governance/runtime_trading_mode_semantics.md §8.
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
    """Task 2.3: rules.yml 包含 runtime governance 告警.

    2026-04-23 勘误: sev2-runtime-baseline-has-orders 已废弃删除 (见 docstring).
    """

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

    def test_alert_sev2_baseline_has_orders_absent(self) -> None:
        """2026-04-23 勘误: 该告警基于错误假设 (baseline_only = 不下单) 已删除,
        确保不会被误恢复."""
        uids = self._all_rule_uids()
        self.assertNotIn(
            "sev2-runtime-baseline-has-orders",
            uids,
            "sev2-runtime-baseline-has-orders 已废弃 (见 runtime_trading_mode_semantics.md §8), "
            "不应重新出现在 rules.yml",
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
        # 2026-04-23 勘误后仅剩 sev3-runtime-ai-decision-no-orders.
        self.assertGreaterEqual(len(runtime_rules), 1)
        for rule in runtime_rules:
            labels = rule.get("labels", {})
            self.assertEqual(
                labels.get("component"),
                "runtime_governance",
                f"rule {rule.get('uid')} 缺 component=runtime_governance label",
            )


class TestProfileAwareObservabilityProvisioning(unittest.TestCase):
    """Runtime profiles must not alert on components they do not deploy."""

    def test_prometheus_targets_follow_compose_profile(self) -> None:
        deploy_dir = REPO_ROOT / "deploy" / "wsl2-dev"
        base_compose = (deploy_dir / "docker-compose.yml").read_text(encoding="utf-8")
        live_overlay = (deploy_dir / "docker-compose.aats.derivatives-live.yml").read_text(
            encoding="utf-8"
        )
        monolith_overlay = (
            deploy_dir / "docker-compose.aats.derivatives-live-monolith.yml"
        ).read_text(encoding="utf-8")
        sliced_targets = yaml.safe_load(
            (deploy_dir / "prometheus" / "targets" / "aats-sliced.yml").read_text(
                encoding="utf-8"
            )
        )
        empty_targets = yaml.safe_load(
            (deploy_dir / "prometheus" / "targets" / "empty.yml").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(len(sliced_targets[0]["targets"]), 4)
        self.assertEqual(empty_targets, [])
        self.assertIn("targets/empty.yml:/etc/prometheus/targets/microstructure.yml", base_compose)
        self.assertIn(
            "targets/microstructure.yml:/etc/prometheus/targets/microstructure.yml",
            live_overlay,
        )
        self.assertIn("targets/aats-monolith.yml:/etc/prometheus/targets/aats.yml", monolith_overlay)

    def test_ui_only_policy_is_muted_for_the_full_day(self) -> None:
        policies = yaml.safe_load(
            (ALERTING_DIR / "policies.yml").read_text(encoding="utf-8")
        )

        root_policy = policies["policies"][0]
        self.assertNotIn("mute_time_intervals", root_policy)
        catch_all_route = root_policy["routes"][0]
        self.assertEqual(catch_all_route["object_matchers"], [["alertname", "=~", ".+"]])
        self.assertEqual(catch_all_route["mute_time_intervals"], ["aats-ui-only"])
        mute_timing = policies["muteTimes"][0]
        self.assertEqual(mute_timing["name"], "aats-ui-only")
        full_day = mute_timing["time_intervals"][0]["times"][0]
        self.assertEqual(full_day, {"start_time": "00:00", "end_time": "24:00"})

    def test_microstructure_stale_rule_is_gated_by_deployed_target(self) -> None:
        rules = yaml.safe_load(
            (ALERTING_DIR / "rules.yml").read_text(encoding="utf-8")
        )
        micro_rule = next(
            rule
            for group in rules["groups"]
            for rule in group["rules"]
            if rule.get("uid") == "sev2-micro-ws-stale"
        )
        by_ref = {item["refId"]: item for item in micro_rule["data"]}

        self.assertEqual(by_ref["C"]["model"]["expression"], "($B < 1) && ($E > 0)")
        self.assertIn('up{job="aats-microstructure"}', by_ref["D"]["model"]["expr"])
        self.assertEqual(by_ref["E"]["model"]["expression"], "D")


if __name__ == "__main__":
    unittest.main()
