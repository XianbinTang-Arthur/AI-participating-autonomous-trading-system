"""测试 build_settings_overrides 的映射缺失 WARNING 机制。

P1-3 修复验证: 当 RDP JSON 中存在研究参数但 FAMILY_PARAMETER_MAPPINGS
没有相应映射时 (如 DIRECTIONAL 映射不完整), 应记录 WARNING 并列出
被丢弃的参数, 帮助快速定位 '研究输出但未注入生产' 的断链。
"""
from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aats.bootstrap.active_parameters import (
    _RDP_CORE_RESEARCH_PARAMS,
    _RDP_REPLAY_ONLY_PARAMS,
    FAMILY_PARAMETER_MAPPINGS,
    PARAMETER_MAPPING_DIRECTIONAL,
    PARAMETER_MAPPING_INDEPENDENT,
    build_settings_overrides,
)


class TestBuildSettingsOverridesMappingGap(unittest.TestCase):
    """验证 DIRECTIONAL 家族映射不完整时的运行时诊断能力。"""

    def _write_registry(self, tmp: Path, values_by_combo: dict[str, dict]) -> Path:
        active_sets = {
            combo: {
                "parameter_set_id": f"test_{combo}",
                "family": combo.split("_")[0],
                "timeframe": combo.split("_")[1],
                "values": v,
            }
            for combo, v in values_by_combo.items()
        }
        registry_path = tmp / "registry.json"
        registry_path.write_text(
            json.dumps({"generated_at": "2026-04-06T00:00:00Z", "active_sets": active_sets}),
            encoding="utf-8",
        )
        return registry_path

    def test_warning_emitted_for_directional_mapping_gap(self) -> None:
        """DIRECTIONAL 家族的 JSON 含核心研究参数但缺映射时应 WARN。"""
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            registry_path = self._write_registry(
                tmp,
                {
                    # directional 家族 JSON 包含 entry_threshold 等核心参数,
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
                },
            )

            with self.assertLogs(
                "aats.bootstrap.active_parameters",
                level=logging.WARNING,
            ) as cm:
                overrides = build_settings_overrides(
                    registry_path=registry_path,
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

            # overrides 应当包含 directional_trend_weight 的映射结果
            self.assertIn("strategy_entry_alpha_min", overrides)

    def test_no_warning_for_independent_full_mapping(self) -> None:
        """INDEPENDENT 家族映射完整, 不应触发 WARNING。"""
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            registry_path = self._write_registry(
                tmp,
                {
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
                },
            )

            with self.assertLogs(
                "aats.bootstrap.active_parameters",
                level=logging.INFO,
            ) as cm:
                _ = build_settings_overrides(
                    registry_path=registry_path,
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


if __name__ == "__main__":
    unittest.main()
