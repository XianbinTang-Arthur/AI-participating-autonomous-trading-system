"""P1/P2 治理管道修复测试.

P1: apply-frozen 不再绕过治理 — 检查 rollback_triggered 结论 + 创建 release 审计
P2: rollback_triggered 不再死胡同 — enforce_pending_rollbacks 自动回滚
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class TestEnforcePendingRollbacks(unittest.TestCase):
    """P2: enforce_pending_rollbacks() 读取 rollback_triggered 评估并执行回滚."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        # 创建目录结构
        (self.root / "artifacts/metrics").mkdir(parents=True)
        (self.root / "artifacts/production_workflow").mkdir(parents=True)
        (self.root / "artifacts/governance").mkdir(parents=True)
        (self.root / "artifacts/decision_system").mkdir(parents=True)
        (self.root / "configs/active_parameter_sets").mkdir(parents=True)

        # 写入 parameter registry（含 2 个 frozen parameter sets）
        self._write_json(
            self.root / "artifacts/governance/current_parameter_registry.json",
            {
                "version": 1,
                "parameter_sets": [
                    {
                        "parameter_set_id": "ps_old",
                        "family": "independent",
                        "timeframe": "15m",
                        "status": "frozen",
                        "values": {"signal_edge_scale_bps": 15},
                    },
                    {
                        "parameter_set_id": "ps_new",
                        "family": "independent",
                        "timeframe": "15m",
                        "status": "frozen",
                        "values": {"signal_edge_scale_bps": 20},
                    },
                ],
            },
        )

        # 当前 active parameter set = ps_new
        self._write_json(
            self.root / "configs/active_parameter_sets/active_parameter_registry.json",
            {
                "generated_at": "2026-04-13T00:00:00+00:00",
                "active_sets": {
                    "independent_15m": {
                        "parameter_set_id": "ps_new",
                        "family": "independent",
                        "timeframe": "15m",
                        "values": {"signal_edge_scale_bps": 20},
                        "applied_by": "test",
                        "applied_at": "2026-04-12T00:00:00+00:00",
                    },
                },
            },
        )

        # release history — ps_new was released from ps_old
        self._write_json(
            self.root / "artifacts/production_workflow/parameter_release_history.json",
            {
                "releases": [
                    {
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "parameter_set_id": "ps_new",
                        "previous_parameter_set_id": "ps_old",
                        "recommendation_id": "rec_test",
                        "apply_result": "success",
                        "observation_status": "completed",
                    },
                ],
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    def test_enforce_executes_rollback_for_rollback_triggered(
        self, mock_rollback,
    ) -> None:
        """rollback_triggered 评估 → 自动回滚到 previous_parameter_set_id."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        mock_rollback.return_value = {"ok": True, "message": "rollback success"}

        # 写入 effectiveness registry（rollback_triggered 结论）
        eff_data = {
            "evaluations": [
                {
                    "evaluation_id": "eff_test",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                },
            ],
        }
        save_effectiveness_registry(self.root, eff_data)

        # 执行
        results = enforce_pending_rollbacks(self.root)

        # 验证回滚被调用
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["release_id"], "rel_test_001")

        # 验证 mock 调用参数正确
        mock_rollback.assert_called_once_with(
            self.root,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_old",
            actor="release_effectiveness_auto_rollback",
            notes=unittest.mock.ANY,
        )

        # 验证 effectiveness registry 已标记 rollback_enforced
        eff_reg = load_effectiveness_registry(self.root)
        ev = eff_reg["evaluations"][0]
        self.assertTrue(ev["rollback_enforced"])
        self.assertEqual(ev["rollback_to_parameter_set_id"], "ps_old")

    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    def test_enforce_records_failure_without_marking_enforced(
        self, mock_rollback,
    ) -> None:
        """回滚失败时不标记 enforced，记录 attempts 和 error，可重试."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        mock_rollback.return_value = {
            "ok": False,
            "message": "target parameter set not found",
        }

        eff_data = {
            "evaluations": [
                {
                    "evaluation_id": "eff_fail",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                },
            ],
        }
        save_effectiveness_registry(self.root, eff_data)

        # 第 1 次执行 — 失败
        results = enforce_pending_rollbacks(self.root)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])

        # 未标记 enforced → 下次可重试
        eff_reg = load_effectiveness_registry(self.root)
        ev = eff_reg["evaluations"][0]
        self.assertFalse(ev.get("rollback_enforced", False))
        self.assertEqual(ev["rollback_attempts"], 1)
        self.assertIn("not found", ev["last_rollback_error"])

        # 第 2 次执行 — 再次失败
        results2 = enforce_pending_rollbacks(self.root)
        self.assertEqual(len(results2), 1)
        self.assertFalse(results2[0]["ok"])

        eff_reg2 = load_effectiveness_registry(self.root)
        ev2 = eff_reg2["evaluations"][0]
        self.assertFalse(ev2.get("rollback_enforced", False))
        self.assertEqual(ev2["rollback_attempts"], 2)

    def test_enforce_skips_already_enforced(self) -> None:
        """已标记 rollback_enforced 的评估不重复执行."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

        eff_data = {
            "evaluations": [
                {
                    "evaluation_id": "eff_old",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                    "rollback_enforced": True,
                    "rollback_enforced_at": "2026-04-12T12:00:00+00:00",
                },
            ],
        }
        save_effectiveness_registry(self.root, eff_data)

        results = enforce_pending_rollbacks(self.root)

        # 已 enforced → 跳过，不应有任何结果
        self.assertEqual(results, [])

    def test_enforce_skips_non_rollback_conclusions(self) -> None:
        """effective / mixed 等结论不触发回滚."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

        eff_data = {
            "evaluations": [
                {
                    "evaluation_id": "eff_ok",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "effective",
                },
            ],
        }
        save_effectiveness_registry(self.root, eff_data)

        results = enforce_pending_rollbacks(self.root)
        self.assertEqual(results, [])

    def test_enforce_handles_missing_previous_parameter_set_id(self) -> None:
        """release 没有 previous_parameter_set_id 时报错而非崩溃."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

        # 修改 release history — 移除 previous_parameter_set_id
        self._write_json(
            self.root / "artifacts/production_workflow/parameter_release_history.json",
            {
                "releases": [
                    {
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "parameter_set_id": "ps_new",
                        # 没有 previous_parameter_set_id
                        "apply_result": "success",
                    },
                ],
            },
        )

        eff_data = {
            "evaluations": [
                {
                    "evaluation_id": "eff_no_prev",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                },
            ],
        }
        save_effectiveness_registry(self.root, eff_data)

        results = enforce_pending_rollbacks(self.root)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("no previous_parameter_set_id", results[0]["error"])


class TestPendingRollbackCombos(unittest.TestCase):
    """P1/P2 辅助：pending_rollback_combos() 返回待回滚的 combo 列表."""

    def test_returns_pending_rollback_combos(self) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            pending_rollback_combos,
        )

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "artifacts/metrics").mkdir(parents=True)

        eff_data = {
            "evaluations": [
                {
                    "release_id": "rel_1",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                },
                {
                    "release_id": "rel_2",
                    "family": "directional",
                    "timeframe": "1h",
                    "conclusion": "rollback_triggered",
                    "rollback_enforced": True,
                },
                {
                    "release_id": "rel_3",
                    "family": "directional",
                    "timeframe": "15m",
                    "conclusion": "effective",
                },
            ],
            "generated_at": "2026-04-13T00:00:00+00:00",
        }
        (root / "artifacts/metrics/release_effectiveness_registry.json").write_text(
            json.dumps(eff_data), encoding="utf-8",
        )

        result = pending_rollback_combos(root)

        # 只有 rel_1 是 pending（rel_2 已 enforced, rel_3 非 rollback）
        self.assertEqual(result, {"independent_15m": "rel_1"})

        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
