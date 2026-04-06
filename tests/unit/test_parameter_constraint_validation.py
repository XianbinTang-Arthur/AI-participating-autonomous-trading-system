"""参数约束校验的单元测试。

覆盖:
  1. ReplayParameterOverrides.__post_init__ 中 safe_edge > de_risk 校验
  2. ReplayParameterOverrides.__post_init__ 中 min_hold <= max_thesis_age 校验
  3. _validate_safe_edge_invariant 运行时 fail-soft 校验
  4. Step 3 _auto_fix_constraint 对新规则的自动修复
  5. Step 3 _validate_constraints 对新规则的检测
"""
from __future__ import annotations

import logging
import unittest
from typing import Any

import pytest

from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
from aats.bootstrap.active_parameters import _validate_safe_edge_invariant


class TestReplayParameterOverridesConstraints(unittest.TestCase):
    """ReplayParameterOverrides.__post_init__ 约束校验。"""

    # ── safe_edge > de_risk ──────────────────────────────────────

    def test_safe_edge_gt_de_risk_passes(self) -> None:
        """safe_edge(2.0+0.5+0.5=3.0) > de_risk(2.0) 正常通过。"""
        p = ReplayParameterOverrides(
            min_safe_net_edge_bps=2.0,
            expected_slippage_buffer_bps=0.5,
            expected_execution_buffer_bps=0.5,
            de_risk_net_edge_bps=2.0,
        )
        self.assertEqual(p.min_safe_net_edge_bps, 2.0)

    def test_safe_edge_eq_de_risk_raises(self) -> None:
        """safe_edge == de_risk 应当拒绝（严格大于）。"""
        with pytest.raises(ValueError, match="safe_edge"):
            ReplayParameterOverrides(
                min_safe_net_edge_bps=1.0,
                expected_slippage_buffer_bps=0.5,
                expected_execution_buffer_bps=0.5,
                de_risk_net_edge_bps=2.0,
            )

    def test_safe_edge_lt_de_risk_raises(self) -> None:
        """safe_edge < de_risk 应当拒绝。"""
        with pytest.raises(ValueError, match="safe_edge"):
            ReplayParameterOverrides(
                min_safe_net_edge_bps=0.0,
                expected_slippage_buffer_bps=0.0,
                expected_execution_buffer_bps=0.0,
                de_risk_net_edge_bps=2.0,
            )

    # ── min_hold <= max_thesis_age ────────────────────────────────

    def test_min_hold_le_max_thesis_passes(self) -> None:
        """min_hold(300) <= max_thesis_age(1800) 正常通过。"""
        p = ReplayParameterOverrides(
            min_hold_seconds=300.0,
            max_thesis_age_seconds=1800.0,
        )
        self.assertEqual(p.min_hold_seconds, 300.0)

    def test_min_hold_eq_max_thesis_passes(self) -> None:
        """min_hold == max_thesis_age 允许（非严格）。"""
        p = ReplayParameterOverrides(
            min_hold_seconds=1800.0,
            max_thesis_age_seconds=1800.0,
        )
        self.assertEqual(p.min_hold_seconds, 1800.0)

    def test_min_hold_gt_max_thesis_raises(self) -> None:
        """min_hold > max_thesis_age 应当拒绝。"""
        with pytest.raises(ValueError, match="min_hold"):
            ReplayParameterOverrides(
                min_hold_seconds=7200.0,
                max_thesis_age_seconds=3600.0,
            )

    # ── 默认值组合通过 ────────────────────────────────────────────

    def test_default_values_pass_all_constraints(self) -> None:
        """默认构造不抛异常。"""
        p = ReplayParameterOverrides()
        self.assertIsNotNone(p)


class TestValidateSafeEdgeInvariant(unittest.TestCase):
    """运行时 _validate_safe_edge_invariant fail-soft 校验。"""

    @staticmethod
    def _make_settings(
        min_safe: float = 2.0,
        slippage: float = 0.5,
        exec_buf: float = 0.5,
        de_risk: float = 2.0,
    ) -> dict[str, Any]:
        return {
            "strategy_hedge_independent_min_safe_net_edge_bps": min_safe,
            "strategy_hedge_independent_expected_slippage_buffer_bps": slippage,
            "strategy_hedge_independent_expected_execution_buffer_bps": exec_buf,
            "strategy_hedge_independent_de_risk_net_edge_bps": de_risk,
        }

    def test_valid_settings_no_error_log(self, caplog: Any = None) -> None:
        """safe_edge(3.0) > de_risk(2.0) 不应产生 ERROR 日志。"""
        with self.assertLogs("aats.bootstrap.active_parameters", level="ERROR") as cm:
            logging.getLogger("aats.bootstrap.active_parameters").error("sentinel")
            _validate_safe_edge_invariant(self._make_settings(
                min_safe=2.0, slippage=0.5, exec_buf=0.5, de_risk=2.0,
            ))
        # 只有 sentinel，没有其他 ERROR
        self.assertEqual(len(cm.output), 1)
        self.assertIn("sentinel", cm.output[0])

    def test_violation_logs_error(self) -> None:
        """safe_edge(1.0) <= de_risk(2.0) 应产生 ERROR 日志。"""
        with self.assertLogs("aats.bootstrap.active_parameters", level="ERROR") as cm:
            _validate_safe_edge_invariant(self._make_settings(
                min_safe=0.0, slippage=0.5, exec_buf=0.5, de_risk=2.0,
            ))
        error_msgs = [m for m in cm.output if "配置倒挂" in m]
        self.assertEqual(len(error_msgs), 1)

    def test_missing_keys_uses_safe_defaults(self) -> None:
        """key 缺失时使用 YAML 对齐的默认值 (2.0+0.5+0.5=3.0 > 2.0)，不触发错误。"""
        with self.assertLogs("aats.bootstrap.active_parameters", level="ERROR") as cm:
            logging.getLogger("aats.bootstrap.active_parameters").error("sentinel")
            _validate_safe_edge_invariant({})  # 全部走 fallback
        self.assertEqual(len(cm.output), 1)
        self.assertIn("sentinel", cm.output[0])


class TestStep3ConstraintDetectionAndAutoFix(unittest.TestCase):
    """Step 3 _validate_constraints + _auto_fix_constraint 对新规则的覆盖。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 延迟导入 Step 3 脚本模块
        import importlib
        cls._s3 = importlib.import_module("scripts.rdp_run_step3_research")

    @staticmethod
    def _param(value: Any, confidence: str = "medium") -> dict[str, Any]:
        return {"value": value, "confidence": confidence}

    def _make_merged(self, overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """构造一个 merged dict，基线使用合法默认值 + overrides 覆盖。"""
        base = {
            "entry_threshold": self._param(0.40),
            "close_threshold": self._param(0.15),
            "scale_in_threshold": self._param(0.60),
            "de_risk_net_edge_bps": self._param(2.0),
            "failed_thesis_net_edge_bps": self._param(-1.0),
            "min_safe_net_edge_bps": self._param(2.0),
            "expected_slippage_buffer_bps": self._param(0.5),
            "expected_execution_buffer_bps": self._param(0.5),
            "min_hold_seconds": self._param(300.0),
            "max_thesis_age_seconds": self._param(1800.0),
        }
        base.update({k: self._param(v) if not isinstance(v, dict) else v
                      for k, v in overrides.items()})
        return {"test_ft": base}

    # ── safe_edge > de_risk: 检测 ─────────────────────────────────

    def test_safe_edge_violation_detected(self) -> None:
        merged = self._make_merged({
            "min_safe_net_edge_bps": 0.0,
            "expected_slippage_buffer_bps": 0.5,
            "expected_execution_buffer_bps": 0.5,
            "de_risk_net_edge_bps": 2.0,
        })
        cr = self._s3._validate_constraints(merged)
        self.assertFalse(cr["all_passed"])
        rules = [v["rule"] for v in cr["violations"]]
        self.assertIn("safe_edge > de_risk", rules)

    def test_safe_edge_passes_when_valid(self) -> None:
        merged = self._make_merged({
            "min_safe_net_edge_bps": 2.0,
            "expected_slippage_buffer_bps": 0.5,
            "expected_execution_buffer_bps": 0.5,
            "de_risk_net_edge_bps": 2.0,
        })
        cr = self._s3._validate_constraints(merged)
        rules = [v["rule"] for v in cr["violations"]]
        self.assertNotIn("safe_edge > de_risk", rules)

    # ── safe_edge > de_risk: auto-fix ─────────────────────────────

    def test_safe_edge_auto_fix_bumps_min_safe(self) -> None:
        merged = self._make_merged({
            "min_safe_net_edge_bps": 0.0,
            "expected_slippage_buffer_bps": 0.5,
            "expected_execution_buffer_bps": 0.5,
            "de_risk_net_edge_bps": 2.0,
        })
        self._s3._validate_constraints(merged)
        fixed_min_safe = merged["test_ft"]["min_safe_net_edge_bps"]["value"]
        fixed_safe_edge = fixed_min_safe + 0.5 + 0.5
        # safe_edge 修复后应当 > de_risk
        self.assertGreater(fixed_safe_edge, 2.0)
        self.assertEqual(merged["test_ft"]["min_safe_net_edge_bps"]["source"], "auto_fix")

    # ── min_hold <= max_thesis_age: 检测 ──────────────────────────

    def test_min_hold_violation_detected(self) -> None:
        merged = self._make_merged({
            "min_hold_seconds": 7200.0,
            "max_thesis_age_seconds": 3600.0,
        })
        cr = self._s3._validate_constraints(merged)
        self.assertFalse(cr["all_passed"])
        rules = [v["rule"] for v in cr["violations"]]
        self.assertIn("min_hold <= max_thesis_age", rules)

    def test_min_hold_passes_when_equal(self) -> None:
        merged = self._make_merged({
            "min_hold_seconds": 1800.0,
            "max_thesis_age_seconds": 1800.0,
        })
        cr = self._s3._validate_constraints(merged)
        rules = [v["rule"] for v in cr["violations"]]
        self.assertNotIn("min_hold <= max_thesis_age", rules)

    # ── min_hold <= max_thesis_age: auto-fix ──────────────────────

    def test_min_hold_auto_fix_reduces_to_max_thesis(self) -> None:
        merged = self._make_merged({
            "min_hold_seconds": 7200.0,
            "max_thesis_age_seconds": 3600.0,
        })
        self._s3._validate_constraints(merged)
        fixed_hold = merged["test_ft"]["min_hold_seconds"]["value"]
        self.assertEqual(fixed_hold, 3600.0)
        self.assertEqual(merged["test_ft"]["min_hold_seconds"]["source"], "auto_fix")

    # ── short_close <= short_entry: auto-fix ──────────────────────

    def test_short_close_auto_fix(self) -> None:
        merged = self._make_merged({
            "short_entry_threshold": 0.20,
            "short_close_threshold": 0.40,
        })
        self._s3._validate_constraints(merged)
        fixed = merged["test_ft"]["short_close_threshold"]["value"]
        self.assertLessEqual(fixed, 0.20)
        self.assertEqual(merged["test_ft"]["short_close_threshold"]["source"], "auto_fix")


if __name__ == "__main__":
    unittest.main()
