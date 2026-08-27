"""P2 治理管道修复测试.

P2: rollback_triggered 不再死胡同 — enforce_pending_rollbacks 自动回滚。
（原 P1 "绕过 gate 的批量应用动作" 已在批次 A 物理删除，详见
 docs/task/rdp_hardening_batch_a_detailed_design.md §3.4。）
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class TestEnforcePendingRollbacks(unittest.TestCase):
    """P2: enforce_pending_rollbacks() 读取 rollback_triggered 评估并执行回滚."""

    def setUp(self) -> None:
        from aats.data_platform.production_workflow.post_apply_evidence import (
            POST_APPLY_EVIDENCE_CONTRACT_VERSION,
            make_source_provenance,
        )

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        now = datetime.now(timezone.utc)
        self.release_created_at = (now - timedelta(hours=3)).isoformat()
        self.release_applied_at = (now - timedelta(hours=2)).isoformat()
        self.evidence_evaluated_at = (now - timedelta(hours=1)).isoformat()

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
                        "created_at": self.release_created_at,
                        "applied_at": self.release_applied_at,
                        "apply_operation_id": "apply_rel_test_001",
                        "apply_result": "success",
                        "observation_status": "completed",
                    },
                ],
            },
        )
        rollback_source = make_source_provenance(
            source_kind="governance_snapshot",
            source_id="governance_snapshot_rel_test_001",
            source_timestamp=self.evidence_evaluated_at,
            source_payload={"status": "regression"},
        )
        self._write_json(
            self.root
            / "artifacts/production_workflow/rollback_recommendations"
            / "rel_test_001/rollback_recommendation.json",
            {
                "release_id": "rel_test_001",
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "evaluated_at": self.evidence_evaluated_at,
                "rollback_recommended": True,
                "severity": "high",
                "triggers": [
                    {
                        "trigger": "governance_regression",
                        "fired": True,
                        "evidence_status": "valid",
                        "severity": "high",
                        "source_provenance": rollback_source,
                    }
                ],
                "fired_trigger_count": 1,
                "evidence_contract_version": (
                    POST_APPLY_EVIDENCE_CONTRACT_VERSION
                ),
                "source_provenance": [rollback_source],
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
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_enforce_executes_rollback_for_rollback_triggered(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """rollback_triggered 评估 → 自动回滚到 previous_parameter_set_id."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        mock_rollback.return_value = {"ok": True, "message": "rollback success"}
        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {
                    "parameter_set_id": "ps_new",
                }
            }
        }

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
            expected_from_parameter_set_id="ps_new",
            expected_from_recommendation_id="rec_test",
            expected_previous_parameter_set_id="ps_old",
            trigger_release_id="rel_test_001",
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
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_enforce_cancels_if_active_changes_after_claim(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """人工切换插入 claim 窗口时，旧自动 intent 必须零写并收口取消."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            pending_rollback_combos,
            save_effectiveness_registry,
        )

        # The optimistic pre-check still sees the release-owned set.  The
        # transaction-level compare-and-set then observes the operator's newer
        # choice and rejects before any active-parameter write.
        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {"parameter_set_id": "ps_new"},
            }
        }
        mock_rollback.return_value = {
            "ok": False,
            "code": "ACTIVE_SET_CHANGED",
            "reason": "expected_current_parameter_set_mismatch",
            "message": "operator changed the active set",
            "from_parameter_set_id": "ps_manual_choice",
        }
        save_effectiveness_registry(
            self.root,
            {
                "evaluations": [
                    {
                        "evaluation_id": "eff_concurrent_manual_switch",
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                    }
                ]
            },
        )

        results = enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["cancelled_due_to_active_change"])
        mock_rollback.assert_called_once_with(
            self.root,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_old",
            expected_from_parameter_set_id="ps_new",
            expected_from_recommendation_id="rec_test",
            expected_previous_parameter_set_id="ps_old",
            trigger_release_id="rel_test_001",
            actor="release_effectiveness_auto_rollback",
            notes=unittest.mock.ANY,
        )
        evaluation = load_effectiveness_registry(self.root)["evaluations"][0]
        self.assertTrue(evaluation["rollback_cancelled"])
        self.assertEqual(evaluation["rollback_enforcement_status"], "cancelled")
        self.assertIn("ps_manual_choice", evaluation["rollback_cancelled_reason"])
        # 文件模式只能记录调用结果，不能提供 DB-owned immutable capital
        # proof；因此即使调用返回 active changed，也必须保持阻断，等待对账。
        self.assertEqual(
            pending_rollback_combos(self.root),
            {"independent_15m": "rel_test_001"},
        )

    def test_active_change_race_adopts_exact_operator_rollback(self) -> None:
        """Operator rollback committed after claim remains enforced rollback."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        save_effectiveness_registry(
            self.root,
            {
                "evaluations": [
                    {
                        "evaluation_id": "eff_operator_race",
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                    }
                ]
            },
        )
        operator_fact = {
            "operation_id": "op_operator_race",
            "target_parameter_set_id": "ps_old",
            "actor": "operator_alice",
            "fact_observed_at": datetime.now(timezone.utc) + timedelta(seconds=1),
        }
        with (
            patch(
                "aats.data_platform.metrics.release_effectiveness."
                "_load_completed_operator_rollback_fact",
                side_effect=[None, operator_fact],
            ),
            patch(
                "aats.data_platform.decision_system.active_parameter_apply."
                "rollback_active_parameter_set",
                return_value={
                    "ok": False,
                    "code": "ACTIVE_SET_CHANGED",
                    "from_parameter_set_id": "ps_old",
                },
            ),
        ):
            results = enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[0]["resolved_by_existing_rollback"])
        self.assertFalse(results[0]["cancelled_due_to_active_change"])
        evaluation = load_effectiveness_registry(self.root)["evaluations"][0]
        self.assertEqual(evaluation["rollback_enforcement_status"], "enforced")
        self.assertEqual(evaluation["rollback_capital_proof_kind"], "rollback")
        self.assertEqual(
            evaluation["rollback_capital_operation_id"],
            "op_operator_race",
        )
        self.assertNotIn("rollback_cancelled", evaluation)

    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_enforce_records_failure_without_marking_enforced(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """回滚入口返回失败后进入 reconciliation，绝不自动重试."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        mock_rollback.return_value = {
            "ok": False,
            "message": "target parameter set not found",
        }
        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {
                    "parameter_set_id": "ps_new",
                }
            }
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

        # 未标记 enforced，但已调用过资本入口 → 必须人工核验，不能重试。
        eff_reg = load_effectiveness_registry(self.root)
        ev = eff_reg["evaluations"][0]
        self.assertFalse(ev.get("rollback_enforced", False))
        self.assertEqual(ev["rollback_attempts"], 1)
        self.assertIn("not found", ev["last_rollback_error"])
        self.assertEqual(
            ev["rollback_enforcement_status"],
            "reconciliation_required",
        )

        # 第 2 次执行只报告待核验，不会再次调用 rollback。
        results2 = enforce_pending_rollbacks(self.root)
        self.assertEqual(len(results2), 1)
        self.assertFalse(results2[0]["ok"])
        self.assertTrue(results2[0]["reconciliation_required"])

        eff_reg2 = load_effectiveness_registry(self.root)
        ev2 = eff_reg2["evaluations"][0]
        self.assertFalse(ev2.get("rollback_enforced", False))
        self.assertEqual(ev2["rollback_attempts"], 1)
        mock_rollback.assert_called_once()

    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_success_with_final_state_write_failure_is_not_replayed(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """真实回滚成功但终态未落库时，in_progress anchor 阻止重放."""
        from aats.data_platform.governance._exceptions import DBUnavailableError
        from aats.data_platform.metrics import release_effectiveness as module

        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {"parameter_set_id": "ps_new"},
            }
        }
        mock_rollback.return_value = {"ok": True, "message": "rollback success"}
        persisted = {
            "evaluations": [
                {
                    "evaluation_id": "eff_uncertain_final_write",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                }
            ]
        }
        save_calls = 0

        def _load(_root: Path) -> dict:
            return deepcopy(persisted)

        def _save(_root: Path, evaluation: dict) -> dict:
            nonlocal save_calls, persisted
            save_calls += 1
            if save_calls == 1:
                persisted = {"evaluations": [deepcopy(evaluation)]}
                return deepcopy(evaluation)
            raise DBUnavailableError("synthetic final state write failure")

        with (
            patch.object(module, "load_effectiveness_registry", side_effect=_load),
            patch.object(module, "save_effectiveness_evaluation", side_effect=_save),
        ):
            with self.assertRaises(DBUnavailableError):
                module.enforce_pending_rollbacks(self.root)

            self.assertEqual(
                persisted["evaluations"][0]["rollback_enforcement_status"],
                "in_progress",
            )
            mock_rollback.assert_called_once()

            results = module.enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["reconciliation_required"])
        mock_rollback.assert_called_once()

    def test_legacy_enforced_requires_reconciliation_without_replay(self) -> None:
        """只有 legacy boolean 的终态保持阻断，且绝不重放资本动作."""
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

        with patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "rollback_active_parameter_set"
        ) as rollback:
            results = enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["skipped"])
        self.assertTrue(results[0]["reconciliation_required"])
        rollback.assert_not_called()

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

    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_enforce_handles_missing_previous_parameter_set_id(
        self,
        mock_load_active_registry,
    ) -> None:
        """release 没有 previous_parameter_set_id 时报错而非崩溃."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {
                    "parameter_set_id": "ps_new",
                }
            }
        }

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
                        "recommendation_id": "rec_test",
                        # 没有 previous_parameter_set_id
                        "created_at": self.release_created_at,
                        "applied_at": self.release_applied_at,
                        "apply_operation_id": "apply_rel_test_001",
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


    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_enforce_cancels_stale_rollback_when_later_release_exists(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """旧 release 的 rollback_triggered 不应回滚后续已成功生效的新 release."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

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
                        "created_at": self.release_created_at,
                        "applied_at": self.release_applied_at,
                        "apply_operation_id": "apply_rel_test_001",
                        "apply_result": "success",
                        "observation_status": "completed",
                    },
                    {
                        "release_id": "rel_test_002",
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "parameter_set_id": "ps_latest",
                        "previous_parameter_set_id": "ps_new",
                        "recommendation_id": "rec_latest",
                        "apply_result": "success",
                        "observation_status": "observing",
                    },
                ],
            },
        )
        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {
                    "parameter_set_id": "ps_latest",
                }
            }
        }
        mock_rollback.return_value = {
            "ok": False,
            "code": "ACTIVE_SET_CHANGED",
            "reason": "expected_current_recommendation_mismatch",
            "message": "later release owns the active combo",
            "from_parameter_set_id": "ps_latest",
        }

        save_effectiveness_registry(
            self.root,
            {
                "evaluations": [
                    {
                        "evaluation_id": "eff_old_release",
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                    },
                ],
            },
        )

        results = enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["cancelled_due_to_active_change"])
        self.assertEqual(
            results[0]["rollback_result"]["from_parameter_set_id"],
            "ps_latest",
        )
        mock_rollback.assert_called_once()

        ev = load_effectiveness_registry(self.root)["evaluations"][0]
        self.assertTrue(ev["rollback_cancelled"])
        self.assertIn("ps_latest", ev["rollback_cancelled_reason"])

    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_later_release_rolled_back_to_old_release_does_not_cancel_old_intent(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """R1→R2→rollback R2→R1 后，R1 的已知坏结论仍必须执行."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

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
                        "created_at": self.release_created_at,
                        "applied_at": self.release_applied_at,
                        "apply_operation_id": "apply_rel_test_001",
                        "apply_result": "success",
                    },
                    {
                        "release_id": "rel_test_002",
                        "family": "independent",
                        "timeframe": "15m",
                        "combo_key": "independent_15m",
                        "parameter_set_id": "ps_latest",
                        "previous_parameter_set_id": "ps_new",
                        "recommendation_id": "rec_latest",
                        "apply_result": "success",
                        "observation_status": "rolled_back",
                        "rollback_to_parameter_set_id": "ps_new",
                    },
                ]
            },
        )
        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {"parameter_set_id": "ps_new"},
            }
        }
        mock_rollback.return_value = {"ok": True, "message": "rollback success"}
        save_effectiveness_registry(
            self.root,
            {
                "evaluations": [
                    {
                        "evaluation_id": "eff_old_release_reactivated",
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                    }
                ]
            },
        )

        results = enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        mock_rollback.assert_called_once_with(
            self.root,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_old",
            expected_from_parameter_set_id="ps_new",
            expected_from_recommendation_id="rec_test",
            expected_previous_parameter_set_id="ps_old",
            trigger_release_id="rel_test_001",
            actor="release_effectiveness_auto_rollback",
            notes=unittest.mock.ANY,
        )

    @patch(
        "aats.data_platform.decision_system.active_parameter_apply"
        ".rollback_active_parameter_set"
    )
    @patch(
        "aats.bootstrap.active_parameters.load_active_parameter_registry"
    )
    def test_enforce_cancels_rollback_when_release_no_longer_active(
        self,
        mock_load_active_registry,
        mock_rollback,
    ) -> None:
        """release 已不再控制当前 active parameter set 时，不应继续自动 rollback."""
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        mock_load_active_registry.return_value = {
            "active_sets": {
                "independent_15m": {
                    "parameter_set_id": "ps_manual_override",
                }
            }
        }
        mock_rollback.return_value = {
            "ok": False,
            "code": "ACTIVE_SET_CHANGED",
            "reason": "expected_current_parameter_set_mismatch",
            "message": "manual override owns the active combo",
            "from_parameter_set_id": "ps_manual_override",
        }
        save_effectiveness_registry(
            self.root,
            {
                "evaluations": [
                    {
                        "evaluation_id": "eff_inactive_release",
                        "release_id": "rel_test_001",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                    },
                ],
            },
        )
        results = enforce_pending_rollbacks(self.root)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["cancelled_due_to_active_change"])
        self.assertEqual(
            results[0]["rollback_result"]["from_parameter_set_id"],
            "ps_manual_override",
        )
        mock_rollback.assert_called_once()

        ev = load_effectiveness_registry(self.root)["evaluations"][0]
        self.assertTrue(ev["rollback_cancelled"])
        self.assertIn("ps_manual_override", ev["rollback_cancelled_reason"])

    def test_permanent_no_target_requires_successful_pause_before_cancelling(
        self,
    ) -> None:
        """UPDATE 0 rows must remain a blocking reconciliation, not cancelled."""
        from aats.data_platform.metrics import release_effectiveness as module

        persisted = {
            "evaluations": [
                {
                    "evaluation_id": "eff_pause_missing",
                    "release_id": "rel_test_001",
                    "family": "independent",
                    "timeframe": "15m",
                    "conclusion": "rollback_triggered",
                }
            ]
        }

        def _load(_root: Path) -> dict:
            return deepcopy(persisted)

        def _save(_root: Path, evaluation: dict) -> dict:
            nonlocal persisted
            persisted = {"evaluations": [deepcopy(evaluation)]}
            return deepcopy(evaluation)

        class _Engine:
            def dispose(self) -> None:
                return None

        class _PauseSession:
            def __enter__(self) -> "_PauseSession":
                return self

            def __exit__(self, _exc_type, _exc, _tb) -> bool:
                return False

            def commit(self) -> None:
                return None

        with (
            patch.object(module, "load_effectiveness_registry", side_effect=_load),
            patch.object(module, "save_effectiveness_evaluation", side_effect=_save),
            patch(
                "aats.data_platform.production_workflow.release_registry."
                "load_release_history",
                return_value={
                    "releases": [
                        {
                            "release_id": "rel_test_001",
                            "family": "independent",
                            "timeframe": "15m",
                            "combo_key": "independent_15m",
                            "parameter_set_id": "ps_new",
                            "previous_parameter_set_id": "ps_old",
                            "recommendation_id": "rec_test",
                            "created_at": self.release_created_at,
                            "applied_at": self.release_applied_at,
                            "apply_operation_id": "apply_rel_test_001",
                            "apply_result": "success",
                        }
                    ]
                },
            ),
            patch(
                "aats.bootstrap.active_parameters.load_active_parameter_registry",
                return_value={
                    "active_sets": {
                        "independent_15m": {"parameter_set_id": "ps_new"}
                    }
                },
            ),
            patch(
                "aats.data_platform.decision_system.active_parameter_apply."
                "rollback_active_parameter_set",
                return_value={
                    "ok": False,
                    "reason": "no_apply_history_for_target",
                    "message": "target has no apply history",
                },
            ),
            patch.object(module, "try_governance_db", return_value=(_Engine(), True)),
            patch("sqlalchemy.orm.Session", return_value=_PauseSession()),
            patch(
                "aats.data_platform.governance.recommendations_db."
                "db_set_combo_pause",
                return_value=False,
            ),
            patch(
                "aats.data_platform.governance.active_params_db."
                "db_try_acquire_parameter_apply_lock",
                return_value=True,
            ),
        ):
            results = module.enforce_pending_rollbacks(self.root)
            pending = module.pending_rollback_combos(self.root)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertFalse(results[0]["soft_paused"])
        self.assertTrue(results[0]["reconciliation_required"])
        ev = persisted["evaluations"][0]
        self.assertFalse(ev.get("rollback_cancelled", False))
        self.assertEqual(
            ev["rollback_enforcement_status"],
            "reconciliation_required",
        )
        self.assertEqual(ev["rollback_reconciliation_reason"], "soft_pause_not_persisted")
        self.assertEqual(pending, {"independent_15m": "rel_test_001"})

    def test_failed_release_effectiveness_never_reaches_capital_action(self) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            load_effectiveness_registry,
            save_effectiveness_registry,
        )

        history_path = (
            self.root
            / "artifacts/production_workflow/parameter_release_history.json"
        )
        history = self._read_json(history_path)
        history["releases"][0]["apply_result"] = "failed"
        self._write_json(history_path, history)
        save_effectiveness_registry(self.root, {
            "evaluations": [{
                "evaluation_id": "eff_failed_release",
                "release_id": "rel_test_001",
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "conclusion": "rollback_triggered",
            }],
        })

        with patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "rollback_active_parameter_set"
        ) as rollback:
            results = enforce_pending_rollbacks(self.root)

        self.assertEqual(results[0]["error"], "release_not_applied")
        self.assertTrue(results[0]["reconciliation_required"])
        rollback.assert_not_called()
        evaluation = load_effectiveness_registry(self.root)["evaluations"][0]
        self.assertEqual(
            evaluation["rollback_enforcement_status"],
            "reconciliation_required",
        )

    def test_cross_identity_supporting_evidence_never_reaches_capital_action(
        self,
    ) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

        evidence_path = (
            self.root
            / "artifacts/production_workflow/rollback_recommendations"
            / "rel_test_001/rollback_recommendation.json"
        )
        evidence = self._read_json(evidence_path)
        evidence.update({
            "family": "directional",
            "timeframe": "1h",
            "combo_key": "directional_1h",
        })
        self._write_json(evidence_path, evidence)
        save_effectiveness_registry(self.root, {
            "evaluations": [{
                "evaluation_id": "eff_cross_identity",
                "release_id": "rel_test_001",
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "conclusion": "rollback_triggered",
            }],
        })

        with patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "rollback_active_parameter_set"
        ) as rollback:
            results = enforce_pending_rollbacks(self.root)

        self.assertEqual(
            results[0]["error"],
            "release_evidence_identity_mismatch",
        )
        self.assertTrue(results[0]["reconciliation_required"])
        rollback.assert_not_called()

    def test_summary_without_rollback_provenance_never_reaches_capital_action(
        self,
    ) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            save_effectiveness_registry,
        )

        evidence_path = (
            self.root
            / "artifacts/production_workflow/rollback_recommendations"
            / "rel_test_001/rollback_recommendation.json"
        )
        evidence = self._read_json(evidence_path)
        evidence["rollback_recommended"] = False
        self._write_json(evidence_path, evidence)
        save_effectiveness_registry(self.root, {
            "evaluations": [{
                "evaluation_id": "eff_without_provenance",
                "release_id": "rel_test_001",
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "conclusion": "rollback_triggered",
            }],
        })

        with patch(
            "aats.data_platform.decision_system.active_parameter_apply."
            "rollback_active_parameter_set"
        ) as rollback:
            results = enforce_pending_rollbacks(self.root)

        self.assertEqual(
            results[0]["error"],
            "rollback_supporting_provenance_missing",
        )
        self.assertTrue(results[0]["reconciliation_required"])
        rollback.assert_not_called()


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
                {
                    "release_id": "rel_4",
                    "family": "independent",
                    "timeframe": "1h",
                    "conclusion": "rollback_triggered",
                    "rollback_cancelled": True,
                },
            ],
            "generated_at": "2026-04-13T00:00:00+00:00",
        }
        (root / "artifacts/metrics/release_effectiveness_registry.json").write_text(
            json.dumps(eff_data), encoding="utf-8",
        )

        result = pending_rollback_combos(root)

        # rel_2/rel_4 只有 legacy terminal boolean，没有显式 attempt、时间链和
        # DB-owned capital proof；必须继续阻断，不能按兼容终态放行。
        self.assertEqual(
            result,
            {
                "independent_15m": "rel_1",
                "directional_1h": "rel_2",
                "independent_1h": "rel_4",
            },
        )

        tmp.cleanup()

    def test_malformed_terminal_flags_remain_pending_and_fail_closed(self) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            pending_rollback_combos,
        )

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        path = root / "artifacts/metrics/release_effectiveness_registry.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "evaluations": [
                    {
                        "release_id": "rel_both_true",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                        "rollback_enforced": True,
                        "rollback_cancelled": True,
                        "rollback_enforcement_status": "enforced",
                    },
                    {
                        "release_id": "rel_status_conflict",
                        "family": "directional",
                        "timeframe": "1h",
                        "conclusion": "rollback_triggered",
                        "rollback_enforced": True,
                        "rollback_enforcement_status": "pending",
                    },
                    {
                        "release_id": "rel_string_enforced",
                        "family": "independent",
                        "timeframe": "4h",
                        "conclusion": "rollback_triggered",
                        "rollback_enforced": "true",
                    },
                    {
                        "release_id": "rel_string_opposite",
                        "family": "directional",
                        "timeframe": "4h",
                        "conclusion": "rollback_triggered",
                        "rollback_enforced": True,
                        "rollback_cancelled": "true",
                        "rollback_enforcement_status": "enforced",
                    },
                ]
            }),
            encoding="utf-8",
        )

        result = pending_rollback_combos(root)

        self.assertEqual(result["independent_15m"], "rel_both_true")
        self.assertEqual(result["directional_1h"], "rel_status_conflict")
        self.assertEqual(result["independent_4h"], "rel_string_enforced")
        self.assertEqual(result["directional_4h"], "rel_string_opposite")
        tmp.cleanup()

    @patch(
        "aats.data_platform.production_workflow.release_registry."
        "load_release_history",
        return_value={"releases": []},
    )
    def test_non_boolean_rollback_flags_require_reconciliation_without_action(
        self,
        _mock_history,
    ) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
        )

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        path = root / "artifacts/metrics/release_effectiveness_registry.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "evaluations": [
                    {
                        "release_id": "rel_type_polluted",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                        "rollback_enforced": "true",
                    }
                ]
            }),
            encoding="utf-8",
        )

        results = enforce_pending_rollbacks(root)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["skipped"])
        self.assertTrue(results[0]["reconciliation_required"])
        self.assertIn("exact bool", results[0]["error"])
        tmp.cleanup()

    @patch(
        "aats.data_platform.production_workflow.release_registry."
        "load_release_history",
        return_value={"releases": []},
    )
    @patch(
        "aats.data_platform.decision_system.active_parameter_apply."
        "rollback_active_parameter_set",
    )
    def test_legacy_action_anchors_never_replay_rollback(
        self,
        mock_rollback,
        _mock_history,
    ) -> None:
        from aats.data_platform.metrics.release_effectiveness import (
            enforce_pending_rollbacks,
            pending_rollback_combos,
        )

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        path = root / "artifacts/metrics/release_effectiveness_registry.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "evaluations": [
                    {
                        "release_id": "rel_legacy_failed_attempt",
                        "family": "independent",
                        "timeframe": "15m",
                        "conclusion": "rollback_triggered",
                        "rollback_attempts": 1,
                        "last_rollback_error": "timeout",
                    },
                    {
                        "release_id": "rel_legacy_unpersisted_pause",
                        "family": "directional",
                        "timeframe": "1h",
                        "conclusion": "rollback_triggered",
                        "rollback_cancelled": True,
                        "rollback_soft_pause_applied": False,
                        "rollback_cancelled_reason": (
                            "soft_paused_no_valid_rollback_target: timeout"
                        ),
                    },
                ]
            }),
            encoding="utf-8",
        )

        results = enforce_pending_rollbacks(root)
        pending = pending_rollback_combos(root)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["skipped"] for item in results))
        self.assertTrue(
            all(item["reconciliation_required"] for item in results)
        )
        self.assertEqual(
            pending,
            {
                "independent_15m": "rel_legacy_failed_attempt",
                "directional_1h": "rel_legacy_unpersisted_pause",
            },
        )
        mock_rollback.assert_not_called()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
