"""测试 build_settings_overrides 的映射缺失 WARNING 机制。

P1-3 修复验证: 当 RDP JSON 中存在研究参数但 FAMILY_PARAMETER_MAPPINGS
没有相应映射时 (如 DIRECTIONAL 映射不完整), 应记录 WARNING 并列出
被丢弃的参数, 帮助快速定位 '研究输出但未注入生产' 的断链。

数据来源已完全迁移到 PostgreSQL，所有测试通过 mock _try_load_from_db 注入数据。
"""
from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from aats.bootstrap.active_parameters import (
    _RDP_CORE_RESEARCH_PARAMS,
    _RDP_REPLAY_ONLY_PARAMS,
    PARAMETER_MAPPING_INDEPENDENT,
    build_settings_overrides,
)


def _make_db_result(values_by_combo: dict[str, dict]) -> dict:
    """构造一个 _try_load_from_db 返回格式的 registry dict."""
    active_sets = {
        combo: {
            "parameter_set_id": f"test_{combo}",
            "family": combo.rsplit("_", 1)[0],
            "timeframe": combo.rsplit("_", 1)[1],
            "values": v,
        }
        for combo, v in values_by_combo.items()
    }
    return {"generated_at": None, "active_sets": active_sets}


class TestBuildSettingsOverridesMappingGap(unittest.TestCase):
    """验证映射缺失时 fail-close，而不是继续做部分注入。"""

    def test_directional_mapping_gap_skips_combo(self) -> None:
        """DIRECTIONAL 家族缺映射时应跳过整个 combo，避免部分注入。"""
        db_result = _make_db_result({
            # directional 家族包含 entry_threshold 等核心参数,
            # 但 PARAMETER_MAPPING_DIRECTIONAL 只映射 3 项, 这 5 项都会被丢弃
            "directional_15m": {
                "entry_threshold": 0.45,
                "close_threshold": 0.20,
                "failed_thesis_net_edge_bps": -1.0,
                "de_risk_net_edge_bps": 2.0,
                "min_hold_seconds": 300.0,
                # 这一项应被映射（不出现在 dropped 中）
                "directional_trend_weight": 0.7,
            },
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            with self.assertLogs(
                "aats.bootstrap.active_parameters",
                level=logging.WARNING,
            ) as cm:
                overrides = build_settings_overrides(
                    db_url="mock://db",
                )

        # 核心参数被丢弃，WARNING 必须被记录
        warning_records = [
            r for r in cm.records if r.levelno == logging.WARNING
        ]
        self.assertTrue(warning_records, "应至少触发一条 WARNING")

        warning_text = " ".join(r.getMessage() for r in warning_records)
        self.assertIn("directional_15m", warning_text)
        # 核心参数必须在 dropped 列表中
        self.assertIn("entry_threshold", warning_text)
        self.assertIn("close_threshold", warning_text)
        self.assertIn("failed_thesis_net_edge_bps", warning_text)
        self.assertIn("de_risk_net_edge_bps", warning_text)
        self.assertIn("min_hold_seconds", warning_text)
        # replay-only 参数不应被列为缺失
        self.assertNotIn("directional_trend_weight", warning_text)

        self.assertNotIn("strategy_entry_alpha_min", overrides)

    def test_no_warning_for_independent_full_mapping(self) -> None:
        """INDEPENDENT 家族映射完整, 不应触发 WARNING。"""
        db_result = _make_db_result({
            "independent_15m": {
                "entry_threshold": 0.25,
                "close_threshold": 0.10,
                "failed_thesis_net_edge_bps": -1.0,
                "catastrophic_failed_thesis_buffer_bps": 3.0,
                "de_risk_net_edge_bps": 2.0,
                "min_hold_seconds": 120,
                "expected_slippage_buffer_bps": 0.5,
                "expected_execution_buffer_bps": 0.5,
                "min_safe_net_edge_bps": 2.0,
                # cost_config 相关, 属 replay-only 白名单
                "taker_fee_bps": 5.0,
                "slippage_bps": 2.0,
            },
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            with self.assertLogs(
                "aats.bootstrap.active_parameters",
                level=logging.INFO,
            ) as cm:
                _ = build_settings_overrides(
                    db_url="mock://db",
                )

        warning_records = [
            r for r in cm.records if r.levelno == logging.WARNING
        ]
        self.assertFalse(
            warning_records,
            f"INDEPENDENT 映射完整, 不应触发 WARNING, 实际触发: "
            f"{[r.getMessage() for r in warning_records]}",
        )

    def test_core_research_params_cover_replay_override_fields(self) -> None:
        """核心研究参数白名单应覆盖 ReplayParameterOverrides 的主要字段。"""
        # 这是一个结构性测试: 任何新增的 RDP 研究参数都应该
        # 更新 _RDP_CORE_RESEARCH_PARAMS 集合, 否则映射缺失检测会漏报
        expected_core = {
            "entry_threshold",
            "close_threshold",
            "failed_thesis_net_edge_bps",
            "catastrophic_failed_thesis_buffer_bps",
            "de_risk_net_edge_bps",
            "min_hold_seconds",
            "expected_slippage_buffer_bps",
            "expected_execution_buffer_bps",
            "min_safe_net_edge_bps",
        }
        self.assertTrue(
            expected_core.issubset(_RDP_CORE_RESEARCH_PARAMS),
            f"核心研究参数白名单缺少: {expected_core - _RDP_CORE_RESEARCH_PARAMS}",
        )

    def test_replay_only_params_are_not_warned(self) -> None:
        """replay-only 参数白名单必须防止误报。"""
        self.assertIn("cost_config", _RDP_REPLAY_ONLY_PARAMS)
        self.assertIn("taker_fee_bps", _RDP_REPLAY_ONLY_PARAMS)
        self.assertIn("slippage_bps", _RDP_REPLAY_ONLY_PARAMS)

    def test_independent_mapping_includes_catastrophic_buffer(self) -> None:
        """确认新增的 catastrophic_failed_thesis_buffer_bps 已映射到 independent 家族。"""
        self.assertIn("catastrophic_failed_thesis_buffer_bps", PARAMETER_MAPPING_INDEPENDENT)
        self.assertEqual(
            PARAMETER_MAPPING_INDEPENDENT["catastrophic_failed_thesis_buffer_bps"],
            "strategy_hedge_independent_catastrophic_failed_thesis_buffer_bps",
        )


class TestTimeframeFilterRegression(unittest.TestCase):
    """P1-2 回归: enabled_decision_timeframes 必须过滤非活跃时间框架。

    场景: DB 同时包含 independent_15m 和 independent_1h，两者的
    entry_threshold 映射到同一个 AATSSettings 字段。如果 timeframe 过滤
    失效，后加载的 1h 值会覆盖当前实盘使用的 15m 值。
    """

    def test_15m_not_overridden_by_1h(self) -> None:
        """apply 后 entry_threshold 应来自 15m 而非 1h。"""
        from aats.bootstrap.active_parameters import apply_active_parameters_to_settings

        db_result = _make_db_result({
            "independent_15m": {"entry_threshold": 0.25},
            "independent_1h": {"entry_threshold": 0.99},
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            base_settings: dict = {
                "active_parameters_enabled": True,
                "active_parameter_registry_path": None,
                "active_parameter_db_url": "mock://db",
                "enabled_decision_timeframes": ("15m",),
            }
            merged = apply_active_parameters_to_settings(base_settings)

        target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
        self.assertEqual(
            merged[target_field],
            0.25,
            f"{target_field} 应为 15m 的 0.25，而非 1h 的 0.99",
        )

    def test_1h_values_used_when_1h_enabled(self) -> None:
        """当 enabled_decision_timeframes=('1h',) 时应使用 1h 参数。"""
        from aats.bootstrap.active_parameters import apply_active_parameters_to_settings

        db_result = _make_db_result({
            "independent_15m": {"entry_threshold": 0.25},
            "independent_1h": {"entry_threshold": 0.99},
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            base_settings: dict = {
                "active_parameters_enabled": True,
                "active_parameter_registry_path": None,
                "active_parameter_db_url": "mock://db",
                "enabled_decision_timeframes": ("1h",),
            }
            merged = apply_active_parameters_to_settings(base_settings)

        target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
        self.assertEqual(
            merged[target_field],
            0.99,
            f"{target_field} 应为 1h 的 0.99",
        )


class TestDbOnlyRegression(unittest.TestCase):
    """DB-only 模式回归测试。

    文件 fallback 已移除，所有参数必须来自 DB。
    DB 不可用时应 fail-soft 返回空 overrides。
    """

    def test_db_returns_data(self) -> None:
        """DB 有数据时应正确产出 overrides。"""
        db_result = _make_db_result({
            "independent_15m": {
                "parameter_set_id": "ps_db_15m",
                "entry_threshold": 0.30,
            },
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            overrides = build_settings_overrides(db_url="mock://db")

        target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
        self.assertIn(target_field, overrides)
        self.assertEqual(overrides[target_field], 0.30)

    def test_db_unavailable_returns_empty(self) -> None:
        """DB 不可用时应返回空 overrides（fail-soft）。"""
        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=None,
        ):
            overrides = build_settings_overrides()

        self.assertEqual(overrides, {})

    def test_try_load_called_only_once(self) -> None:
        """_try_load_from_db 应只被调用 1 次，不再有文件 fallback 再次调用。"""
        import os

        db_result = _make_db_result({
            "independent_1h": {"entry_threshold": 0.90},
        })

        call_count = 0
        original_env = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")

        def _mock_try_load(db_url=None):
            nonlocal call_count
            call_count += 1
            return db_result

        try:
            os.environ["AATS_ACTIVE_PARAMETER_DB_URL"] = "postgresql://mock:5432/test"
            with patch(
                "aats.bootstrap.active_parameters._try_load_from_db",
                side_effect=_mock_try_load,
            ):
                build_settings_overrides(
                    timeframes=["15m"],
                )
        finally:
            if original_env is None:
                os.environ.pop("AATS_ACTIVE_PARAMETER_DB_URL", None)
            else:
                os.environ["AATS_ACTIVE_PARAMETER_DB_URL"] = original_env

        # _try_load_from_db 应只被调用 1 次
        self.assertEqual(
            call_count, 1,
            f"_try_load_from_db 应只被调用 1 次，实际 {call_count} 次",
        )


class TestActiveParameterNoneHandling(unittest.TestCase):
    def test_none_values_are_skipped_in_overrides(self) -> None:
        db_result = _make_db_result({
            "independent_15m": {
                "entry_threshold": None,
                "close_threshold": 0.12,
            },
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            overrides = build_settings_overrides(db_url="mock://db")

        self.assertNotIn(
            PARAMETER_MAPPING_INDEPENDENT["entry_threshold"],
            overrides,
        )
        self.assertEqual(
            overrides[PARAMETER_MAPPING_INDEPENDENT["close_threshold"]],
            0.12,
        )

    def test_safe_edge_invariant_tolerates_none(self) -> None:
        from aats.bootstrap.active_parameters import _validate_safe_edge_invariant

        settings = {
            "strategy_hedge_independent_min_safe_net_edge_bps": None,
            "strategy_hedge_independent_expected_slippage_buffer_bps": None,
            "strategy_hedge_independent_expected_execution_buffer_bps": None,
            "strategy_hedge_independent_de_risk_net_edge_bps": None,
        }

        _validate_safe_edge_invariant(settings)


if __name__ == "__main__":
    unittest.main()
