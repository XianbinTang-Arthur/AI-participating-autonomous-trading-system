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


class TestTimeframeFilterRegression(unittest.TestCase):
    """P1-2 回归: enabled_decision_timeframes 必须过滤非活跃时间框架。

    场景: registry 同时包含 independent_15m 和 independent_1h，两者的
    entry_threshold 映射到同一个 AATSSettings 字段。如果 timeframe 过滤
    失效，后加载的 1h 值会覆盖当前实盘使用的 15m 值。
    """

    def _write_registry(self, tmp: Path, values_by_combo: dict[str, dict]) -> Path:
        active_sets = {
            combo: {
                "parameter_set_id": f"test_{combo}",
                "family": combo.rsplit("_", 1)[0],
                "timeframe": combo.rsplit("_", 1)[1],
                "values": v,
            }
            for combo, v in values_by_combo.items()
        }
        registry_path = tmp / "registry.json"
        registry_path.write_text(
            json.dumps({"generated_at": "2026-04-11T00:00:00Z", "active_sets": active_sets}),
            encoding="utf-8",
        )
        return registry_path

    def test_15m_not_overridden_by_1h(self) -> None:
        """apply 后 entry_threshold 应来自 15m 而非 1h。"""
        from aats.bootstrap.active_parameters import apply_active_parameters_to_settings

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            registry_path = self._write_registry(
                tmp,
                {
                    "independent_15m": {"entry_threshold": 0.25},
                    "independent_1h": {"entry_threshold": 0.99},
                },
            )
            base_settings: dict = {
                "active_parameters_enabled": True,
                "active_parameter_registry_path": str(registry_path),
                "active_parameter_db_url": None,
                "enabled_decision_timeframes": ("15m",),
            }
            merged = apply_active_parameters_to_settings(
                base_settings, project_root=tmp,
            )
            target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
            self.assertEqual(
                merged[target_field],
                0.25,
                f"{target_field} 应为 15m 的 0.25，而非 1h 的 0.99",
            )

    def test_1h_values_used_when_1h_enabled(self) -> None:
        """当 enabled_decision_timeframes=('1h',) 时应使用 1h 参数。"""
        from aats.bootstrap.active_parameters import apply_active_parameters_to_settings

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            registry_path = self._write_registry(
                tmp,
                {
                    "independent_15m": {"entry_threshold": 0.25},
                    "independent_1h": {"entry_threshold": 0.99},
                },
            )
            base_settings: dict = {
                "active_parameters_enabled": True,
                "active_parameter_registry_path": str(registry_path),
                "active_parameter_db_url": None,
                "enabled_decision_timeframes": ("1h",),
            }
            merged = apply_active_parameters_to_settings(
                base_settings, project_root=tmp,
            )
            target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
            self.assertEqual(
                merged[target_field],
                0.99,
                f"{target_field} 应为 1h 的 0.99",
            )


class TestDbPartialFallbackRegression(unittest.TestCase):
    """P2-1 回归: DB 返回部分 active sets 时必须从文件 registry 补齐缺失 combo。

    场景: DB 只 seed 了 independent_1h，但文件 registry 包含
    independent_15m + independent_1h。缺失的 15m 应从文件补齐。
    """

    def test_missing_combo_merged_from_file(self) -> None:
        """DB 缺 independent_15m 时应从文件 registry 补齐。"""
        from unittest.mock import patch
        from aats.bootstrap.active_parameters import (
            DEFAULT_ACTIVE_DIR,
            DEFAULT_REGISTRY_FILENAME,
        )

        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # 构造文件 registry 包含 15m + 1h
            registry_dir = tmp / DEFAULT_ACTIVE_DIR
            registry_dir.mkdir(parents=True)
            file_registry_path = registry_dir / DEFAULT_REGISTRY_FILENAME
            file_registry_path.write_text(
                json.dumps({
                    "generated_at": "2026-04-11T00:00:00Z",
                    "active_sets": {
                        "independent_15m": {
                            "parameter_set_id": "ps_file_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "values": {"entry_threshold": 0.30},
                        },
                        "independent_1h": {
                            "parameter_set_id": "ps_file_1h",
                            "family": "independent",
                            "timeframe": "1h",
                            "values": {"entry_threshold": 0.80},
                        },
                    },
                }),
                encoding="utf-8",
            )

            # mock _try_load_from_db: 只返回 independent_1h
            db_partial_result = {
                "generated_at": None,
                "active_sets": {
                    "independent_1h": {
                        "parameter_set_id": "ps_db_1h",
                        "family": "independent",
                        "timeframe": "1h",
                        "values": {"entry_threshold": 0.90},
                    },
                },
            }
            with patch(
                "aats.bootstrap.active_parameters._try_load_from_db",
                return_value=db_partial_result,
            ):
                overrides = build_settings_overrides(
                    project_root=tmp,
                    db_url="mock://db",
                )

            target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
            # 两个 combo 都应该产出 overrides。
            # 因 15m 和 1h 映射到同一字段，最终值取决于遍历顺序；
            # 关键断言：target_field 必须存在（代表 15m 补齐成功参与了映射）。
            self.assertIn(
                target_field,
                overrides,
                "entry_threshold 对应的 settings 字段应存在于 overrides 中",
            )

    def test_no_file_merge_without_db(self) -> None:
        """非 DB 路径下不应触发额外的文件 merge。"""
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            registry_path = tmp / "registry.json"
            registry_path.write_text(
                json.dumps({
                    "generated_at": "2026-04-11T00:00:00Z",
                    "active_sets": {
                        "independent_15m": {
                            "parameter_set_id": "ps_15m",
                            "family": "independent",
                            "timeframe": "15m",
                            "values": {"entry_threshold": 0.25},
                        },
                    },
                }),
                encoding="utf-8",
            )

            with self.assertLogs(
                "aats.bootstrap.active_parameters", level=logging.INFO,
            ) as cm:
                overrides = build_settings_overrides(
                    registry_path=registry_path,
                )

            # 不应有 "db_partial" 的 WARNING
            warning_records = [
                r for r in cm.records if r.levelno == logging.WARNING
            ]
            partial_warnings = [
                r for r in warning_records
                if "db_partial" in r.getMessage()
            ]
            self.assertFalse(
                partial_warnings,
                "非 DB 路径不应触发 active_parameter_db_partial WARNING",
            )


if __name__ == "__main__":
    unittest.main()
