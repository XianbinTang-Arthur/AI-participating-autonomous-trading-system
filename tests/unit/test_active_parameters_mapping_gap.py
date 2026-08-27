"""测试 build_settings_overrides 的映射缺失检测机制。

Path 1 重构后语义:
- 每个 family 有各自的 required 子集 (_RDP_CORE_RESEARCH_PARAMS_BY_FAMILY)
- required 缺映射 → ERROR + skip combo (fail-close)
- 非 required 未映射 → INFO 记录 (dropped as research-only)
- 已 replay-only 白名单 (taker_fee_bps 等) → 静默忽略

数据来源已完全迁移到 PostgreSQL，所有测试通过 mock _try_load_from_db 注入数据。
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aats.bootstrap.active_parameters import (
    ActiveParameterSafetyError,
    _RDP_CORE_RESEARCH_PARAMS,
    _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY,
    _RDP_REPLAY_ONLY_PARAMS,
    PARAMETER_MAPPING_DIRECTIONAL,
    PARAMETER_MAPPING_INDEPENDENT,
    _try_load_from_db,
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
    """验证 per-family required 映射缺失检测。

    Path 1 新语义:
    - required 缺映射 → ERROR + skip combo (fail-close)
    - 非 required 未映射 → INFO "research-only keys"
    - replay-only 白名单 key → 静默忽略
    """

    def test_independent_mapping_gap_skips_combo(self) -> None:
        """INDEPENDENT 缺 required 映射时应 skip combo 并 ERROR。

        回归测试: 这是 Path 1 之前唯一生效的 fail-close 场景,
        独立出来确认 independent 侧行为不变。
        """
        # 构造 independent combo 故意缺 entry_threshold 的场景（通过临时删除映射）
        import aats.bootstrap.active_parameters as ap

        orig_mapping = dict(ap.PARAMETER_MAPPING_INDEPENDENT)
        broken_mapping = dict(orig_mapping)
        broken_mapping.pop("entry_threshold", None)

        db_result = _make_db_result({
            "independent_15m": {
                "entry_threshold": 0.25,  # required 但映射被移除
                "close_threshold": 0.10,
                "failed_thesis_net_edge_bps": -1.0,
                "catastrophic_failed_thesis_buffer_bps": 3.0,
                "de_risk_net_edge_bps": 2.0,
                "min_hold_seconds": 120,
                "expected_slippage_buffer_bps": 0.5,
                "expected_execution_buffer_bps": 0.5,
                "min_safe_net_edge_bps": 2.0,
            },
        })

        with (
            patch.object(ap, "PARAMETER_MAPPING_INDEPENDENT", broken_mapping),
            patch.dict(
                ap.FAMILY_PARAMETER_MAPPINGS,
                {"independent": broken_mapping},
            ),
            patch(
                "aats.bootstrap.active_parameters._try_load_from_db",
                return_value=db_result,
            ),
        ):
            with self.assertLogs(
                "aats.bootstrap.active_parameters",
                level=logging.ERROR,
            ) as cm:
                overrides = build_settings_overrides(db_url="mock://db")

        error_records = [r for r in cm.records if r.levelno == logging.ERROR]
        self.assertTrue(error_records, "应至少触发一条 ERROR")
        err_text = " ".join(r.getMessage() for r in error_records)
        self.assertIn("independent_15m", err_text)
        self.assertIn("entry_threshold", err_text)
        # combo 被 skip，不应产出 overrides
        self.assertEqual(overrides, {})

    def test_directional_happy_path_no_error(self) -> None:
        """DIRECTIONAL 提供 min_hold_seconds 映射时, 不应 ERROR, 其它研究 key 降级 INFO。

        Path 1 核心验证: directional combo 包含全套 21 研究 key,
        仅 min_hold_seconds 是 required, 映射存在 → 应当成功注入;
        其余 20 个研究 key 未映射但非 required → INFO "research-only"。
        """
        db_result = _make_db_result({
            "directional_15m": {
                # A 类: required, 必须映射
                "min_hold_seconds": 300.0,
                # 可选映射 (已在 PARAMETER_MAPPING_DIRECTIONAL)
                "directional_trend_weight": 0.7,
                # replay-only 白名单
                "taker_fee_bps": 5.0,
                "slippage_bps": 1.0,
                # 非 required 未映射: 应降级 INFO
                "entry_threshold": 0.45,
                "close_threshold": 0.20,
                "scale_in_threshold": 0.55,
                "failed_thesis_net_edge_bps": -1.0,
                "de_risk_net_edge_bps": 2.0,
                "catastrophic_failed_thesis_buffer_bps": 3.0,
                "expected_slippage_buffer_bps": 0.5,
                "expected_execution_buffer_bps": 0.5,
                "max_acceptable_cost_bps": 7.5,
                "rebalance_cooldown_seconds": 120.0,
                "max_thesis_age_seconds": 1800.0,
                "min_score_drawdown_bps": 6.0,
                "min_liquidity_quality": 0.55,
                "limit_offset_bps_entry": 1.5,
                "signal_edge_scale_bps": 12.0,
                "score_stability_threshold": 5.0,
                "min_confirm_ticks": 2,
                "min_safe_net_edge_bps": 2.0,
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
                overrides = build_settings_overrides(db_url="mock://db")

        # 不应有 ERROR
        error_records = [r for r in cm.records if r.levelno == logging.ERROR]
        self.assertFalse(
            error_records,
            f"directional happy path 不应触发 ERROR, 实际: "
            f"{[r.getMessage() for r in error_records]}",
        )

        # min_hold_seconds 应成功注入 strategy_min_hold_seconds (global)
        self.assertEqual(overrides.get("strategy_min_hold_seconds"), 300.0)
        # directional_trend_weight 已撤除 PLACEHOLDER 映射,应静默忽略
        # (在 _RDP_REPLAY_ONLY_PARAMS 白名单中)
        self.assertNotIn("strategy_entry_alpha_min", overrides)

        # 应有一条 INFO 级的 "research-only keys" 记录
        info_records = [r for r in cm.records if r.levelno == logging.INFO]
        info_text = " ".join(r.getMessage() for r in info_records)
        self.assertIn("research-only keys", info_text)
        self.assertIn("directional_15m", info_text)
        # 这些非 required 未映射的 key 应出现在 INFO 中
        self.assertIn("entry_threshold", info_text)
        self.assertIn("close_threshold", info_text)
        self.assertIn("rebalance_cooldown_seconds", info_text)

    def test_directional_missing_required_min_hold_errors(self) -> None:
        """如果 min_hold_seconds 映射被移除, directional combo 应 ERROR + skip。

        回归保护: 确保新的 per-family required 机制对 directional 也能
        fail-close, 不只是 independent。
        """
        import aats.bootstrap.active_parameters as ap

        orig_mapping = dict(ap.PARAMETER_MAPPING_DIRECTIONAL)
        broken_mapping = dict(orig_mapping)
        broken_mapping.pop("min_hold_seconds", None)

        db_result = _make_db_result({
            "directional_15m": {
                "min_hold_seconds": 300.0,  # 值存在但映射缺失
                "directional_trend_weight": 0.7,
            },
        })

        with (
            patch.object(ap, "PARAMETER_MAPPING_DIRECTIONAL", broken_mapping),
            patch.dict(
                ap.FAMILY_PARAMETER_MAPPINGS,
                {"directional": broken_mapping},
            ),
            patch(
                "aats.bootstrap.active_parameters._try_load_from_db",
                return_value=db_result,
            ),
        ):
            with self.assertLogs(
                "aats.bootstrap.active_parameters",
                level=logging.ERROR,
            ) as cm:
                overrides = build_settings_overrides(db_url="mock://db")

        error_records = [r for r in cm.records if r.levelno == logging.ERROR]
        self.assertTrue(error_records)
        err_text = " ".join(r.getMessage() for r in error_records)
        self.assertIn("directional_15m", err_text)
        self.assertIn("min_hold_seconds", err_text)
        self.assertEqual(overrides, {})

    def test_no_warning_for_independent_full_mapping(self) -> None:
        """INDEPENDENT 家族映射完整, 不应触发 WARNING/ERROR。"""
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

        bad_records = [
            r for r in cm.records
            if r.levelno in (logging.WARNING, logging.ERROR)
        ]
        self.assertFalse(
            bad_records,
            f"INDEPENDENT 映射完整, 不应触发 WARNING/ERROR, 实际触发: "
            f"{[(r.levelname, r.getMessage()) for r in bad_records]}",
        )

    def test_unknown_family_no_required_passes(self) -> None:
        """未纳入 required dict 的 family (smart_arbitrage 等) 不应 ERROR。

        Path 1 的 per-family required 机制对未登记的 family 返回空 required,
        这样新 family 接入 RDP 前,其研究输出不会引发 ERROR 风暴。
        """
        db_result = _make_db_result({
            "smart_arbitrage_1h": {
                "some_future_param_1": 1.0,
                "some_future_param_2": 2.0,
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
                overrides = build_settings_overrides(db_url="mock://db")

        error_records = [r for r in cm.records if r.levelno == logging.ERROR]
        self.assertFalse(
            error_records,
            f"未知 family 不应 ERROR, 实际: "
            f"{[r.getMessage() for r in error_records]}",
        )
        # 无映射 → 无 overrides
        self.assertEqual(overrides, {})

    def test_core_research_params_cover_replay_override_fields(self) -> None:
        """核心研究参数白名单应覆盖 ReplayParameterOverrides 的主要字段。"""
        # 结构性测试: 任何新增的 RDP 研究参数都应该更新 independent
        # required 集合, 否则映射缺失检测会漏报
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

    def test_per_family_required_structure(self) -> None:
        """per-family required dict 的结构性约束。"""
        # independent 必须被登记
        self.assertIn("independent", _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY)
        # directional 必须被登记
        self.assertIn("directional", _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY)
        # directional required 应包含 min_hold_seconds (生产端可消费的唯一 key)
        self.assertIn(
            "min_hold_seconds",
            _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY["directional"],
        )
        # 兼容别名 _RDP_CORE_RESEARCH_PARAMS 应等同 independent required
        self.assertEqual(
            _RDP_CORE_RESEARCH_PARAMS,
            _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY["independent"],
        )

    def test_directional_mapping_includes_min_hold_seconds(self) -> None:
        """Path 1 新增: directional 映射必须包含 min_hold_seconds。"""
        self.assertIn("min_hold_seconds", PARAMETER_MAPPING_DIRECTIONAL)
        self.assertEqual(
            PARAMETER_MAPPING_DIRECTIONAL["min_hold_seconds"],
            "strategy_min_hold_seconds",
        )

    def test_directional_trend_weight_not_mapped_to_alpha_min(self) -> None:
        """回归保护: directional_trend_weight → strategy_entry_alpha_min 的
        PLACEHOLDER 映射已撤除 (2026-04-18 实盘发现语义冲突).

        不能重新引入此映射, 否则 directional combo 的 trend_weight=1.0
        会被注入到全局 strategy_entry_alpha_min, 锁死所有 family 入场门控.
        """
        self.assertNotIn(
            "directional_trend_weight",
            PARAMETER_MAPPING_DIRECTIONAL,
            msg=(
                "directional_trend_weight 不应映射到 AATSSettings。"
                "此 PLACEHOLDER 已撤除, 见 active_parameters.py 内相应注释。"
            ),
        )
        # directional_trend_weight 必须在 replay-only 白名单中 (静默忽略)
        self.assertIn("directional_trend_weight", _RDP_REPLAY_ONLY_PARAMS)

    def test_directional_trend_weight_does_not_leak_to_alpha_min(self) -> None:
        """端到端回归: 即使 directional combo 包含 trend_weight=1.0,
        strategy_entry_alpha_min 也不应被 override。"""
        db_result = _make_db_result({
            "directional_15m": {
                "min_hold_seconds": 300.0,
                "directional_trend_weight": 1.0,  # 严重副作用的值
                "taker_fee_bps": 5.0,
                "slippage_bps": 2.0,
            },
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            overrides = build_settings_overrides(db_url="mock://db")

        # strategy_min_hold_seconds 应注入
        self.assertEqual(overrides.get("strategy_min_hold_seconds"), 300.0)
        # strategy_entry_alpha_min 绝对不能出现
        self.assertNotIn(
            "strategy_entry_alpha_min",
            overrides,
            msg="trend_weight=1.0 不应泄漏到全局 entry_alpha_min",
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

    def test_conflicting_enabled_timeframes_fail_closed(self) -> None:
        """Flat runtime settings must not silently choose one timeframe."""

        db_result = _make_db_result({
            "independent_15m": {"entry_threshold": 0.25},
            "independent_1h": {"entry_threshold": 0.99},
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            with self.assertRaisesRegex(
                ActiveParameterSafetyError,
                "settings collision",
            ):
                build_settings_overrides(db_url="mock://db")

    def test_equal_values_across_timeframes_preserve_both_lineages(self) -> None:
        """Identical flat values are deterministic and may share the field."""

        db_result = _make_db_result({
            "independent_15m": {"entry_threshold": 0.25},
            "independent_1h": {"entry_threshold": 0.25},
        })

        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=db_result,
        ):
            overrides = build_settings_overrides(db_url="mock://db")

        target_field = PARAMETER_MAPPING_INDEPENDENT["entry_threshold"]
        self.assertEqual(overrides[target_field], 0.25)
        self.assertEqual(
            overrides["active_parameter_set_ids"],
            {
                "independent_15m": "test_independent_15m",
                "independent_1h": "test_independent_1h",
            },
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
        """纯离线开发且未配置 managed DB 时可保留默认配置。"""
        with (
            patch(
                "aats.bootstrap.active_parameters._try_load_from_db",
                return_value=None,
            ),
            patch(
                "aats.bootstrap.active_parameters."
                "has_explicit_governance_db_configuration",
                return_value=False,
            ),
        ):
            overrides = build_settings_overrides()

        self.assertEqual(overrides, {})

    def test_active_decision_query_failure_discards_active_sets(self) -> None:
        """局部 decision 查询失败不得把 pause 误解释成未暂停。"""

        class _Rows:
            def __init__(self, rows: list[object]) -> None:
                self._rows = rows

            def fetchall(self) -> list[object]:
                return self._rows

        class _Connection:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self) -> "_Connection":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def begin(self) -> "_Connection":
                return self

            def execute(self, statement: object) -> _Rows:
                self.calls += 1
                sql = str(statement)
                if sql.startswith("SET TRANSACTION"):
                    return _Rows([])
                if "FROM governance.active_parameter_sets AS a" in sql:
                    row = type(
                        "ActiveRow",
                        (),
                        {
                            "family": "independent",
                            "timeframe": "15m",
                            "parameter_set_id": "ps_must_not_load",
                            "param_values": {"entry_threshold": 0.99},
                            "source_round_id": "round_1",
                            "approval_recommendation_id": "rec_1",
                            "applied_by": "operator",
                            "applied_at": None,
                        },
                    )()
                    return _Rows([row])
                if "FROM governance.active_decisions" in sql:
                    raise RuntimeError("active_decisions SELECT denied")
                return _Rows([])

        class _Engine:
            def __init__(self) -> None:
                self.connection = _Connection()

            def connect(self) -> _Connection:
                return self.connection

            def dispose(self) -> None:
                return None

        with patch("sqlalchemy.create_engine", return_value=_Engine()):
            registry = _try_load_from_db("postgresql://managed.invalid/aats")

        self.assertIsNone(registry)

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


def _complete_managed_independent_values() -> dict[str, float | int]:
    values: dict[str, float | int] = {
        key: 1.0
        for key in _RDP_CORE_RESEARCH_PARAMS_BY_FAMILY["independent"]
    }
    values["min_confirm_ticks"] = 2
    values["max_thesis_age_seconds"] = 1800
    return values


class TestManagedActiveParameterFailClosed(unittest.TestCase):
    def _managed_registry(
        self,
        *,
        combo: str = "independent_15m",
        values: object | None = None,
    ) -> dict:
        family, timeframe = combo.rsplit("_", 1)
        return {
            "generated_at": None,
            "governance_managed": True,
            "active_sets": {
                combo: {
                    "parameter_set_id": "ps_managed",
                    "family": family,
                    "timeframe": timeframe,
                    "values": (
                        _complete_managed_independent_values()
                        if values is None
                        else values
                    ),
                }
            },
        }

    def test_managed_missing_production_value_stops_startup(self) -> None:
        values = _complete_managed_independent_values()
        values.pop("entry_threshold")
        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=self._managed_registry(values=values),
        ):
            with self.assertRaisesRegex(
                ActiveParameterSafetyError, "contract incomplete"
            ):
                build_settings_overrides(db_url="mock://db")

    def test_managed_null_production_value_stops_startup(self) -> None:
        values = _complete_managed_independent_values()
        values["entry_threshold"] = None
        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=self._managed_registry(values=values),
        ):
            with self.assertRaisesRegex(ActiveParameterSafetyError, "null"):
                build_settings_overrides(db_url="mock://db")

    def test_managed_unknown_family_stops_startup(self) -> None:
        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=self._managed_registry(
                combo="future_family_15m", values={"future": 1.0}
            ),
        ):
            with self.assertRaisesRegex(
                ActiveParameterSafetyError, "no production contract"
            ):
                build_settings_overrides(db_url="mock://db")

    def test_managed_invalid_numeric_value_stops_startup(self) -> None:
        values = _complete_managed_independent_values()
        values["entry_threshold"] = "not-a-number"
        with patch(
            "aats.bootstrap.active_parameters._try_load_from_db",
            return_value=self._managed_registry(values=values),
        ):
            with self.assertRaisesRegex(
                ActiveParameterSafetyError, "strict settings validation"
            ):
                build_settings_overrides(db_url="mock://db")

    def test_rdp_database_url_outage_is_managed_failure(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "AATS_ACTIVE_PARAMETER_DB_URL": "",
                    "RDP_DATABASE_URL": "postgresql://managed.invalid/aats",
                },
                clear=False,
            ),
            patch(
                "aats.bootstrap.active_parameters._try_load_from_db",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(
                ActiveParameterSafetyError, "DB truth unavailable"
            ):
                build_settings_overrides()

    def test_step3_materializes_complete_independent_production_contract(self) -> None:
        import scripts.rdp_run_step3_research as step3

        merged = step3._merge_recommendations(
            {
                "candidates": {
                    "independent_15m": {
                        "entry_threshold": 0.30,
                        "close_threshold": 0.15,
                    }
                }
            },
            {},
        )["independent_15m"]
        emitted = {
            name: record["value"]
            for name, record in merged.items()
            if isinstance(record, dict) and record.get("value") is not None
        }
        production_contract = set(PARAMETER_MAPPING_INDEPENDENT).difference(
            _RDP_REPLAY_ONLY_PARAMS
        )
        self.assertEqual(production_contract.difference(emitted), set())
        self.assertEqual(emitted["short_entry_threshold"], 0.30)
        self.assertEqual(emitted["short_close_threshold"], 0.15)
        self.assertEqual(emitted["min_score_drawdown_bps"], 6.0)


class _DbRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def fetchall(self) -> list[object]:
        return self._rows


class _SnapshotConnection:
    def __init__(
        self,
        *,
        active_rows: list[object],
        decision_rows: list[object],
        known_bad_rows: list[object] | None = None,
    ) -> None:
        self.active_rows = active_rows
        self.decision_rows = decision_rows
        self.known_bad_rows = known_bad_rows or []
        self.statements: list[str] = []
        self.begin_count = 0

    def __enter__(self) -> "_SnapshotConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> "_SnapshotConnection":
        self.begin_count += 1
        return self

    def execute(self, statement: object) -> _DbRows:
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("SET TRANSACTION"):
            return _DbRows([])
        if "FROM governance.active_parameter_sets AS a" in sql:
            return _DbRows(self.active_rows)
        if "FROM governance.active_decisions" in sql:
            return _DbRows(self.decision_rows)
        if "FROM governance.release_effectiveness AS e" in sql:
            return _DbRows(self.known_bad_rows)
        raise AssertionError(f"unexpected SQL: {sql}")


class _SnapshotEngine:
    def __init__(self, connection: _SnapshotConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> _SnapshotConnection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


def _canonical_active_row(**overrides: object) -> SimpleNamespace:
    values = {"entry_threshold": 0.25}
    now = datetime.now(timezone.utc)
    fields: dict[str, object] = {
        "family": "independent",
        "timeframe": "15m",
        "parameter_set_id": "ps_1",
        "param_values": values,
        "source_round_id": "parameter_round_1",
        "approval_recommendation_id": "rec_1",
        "applied_by": "operator",
        "applied_at": now,
        "canonical_parameter_set_id": "ps_1",
        "parameter_set_family": "independent",
        "parameter_set_symbol": "BTC-USDT-SWAP",
        "parameter_set_timeframe": "15m",
        "canonical_param_values": values,
        "parameter_set_status": "released",
        "parameter_set_source_round_id": "parameter_round_1",
        "canonical_recommendation_id": "rec_1",
        "recommendation_family": "independent",
        "recommendation_symbol": "BTC-USDT-SWAP",
        "recommendation_timeframe": "15m",
        "target_parameter_set_id": "ps_1",
        "recommendation_source_round_id": "decision_round_9",
        "recommendation_type": "parameter_upgrade",
        "recommendation_status": "approved",
        "evidence_bundle_ref": "bundle_1",
        "approved_by": "reviewer",
        "approved_at": now,
        "canonical_release_id": "rel_1",
        "release_count": 1,
        "apply_operation_id": "op_1",
        "release_applied_at": (now - timedelta(seconds=30)).isoformat(),
        "release_created_at": now - timedelta(minutes=1),
        "lineage_count": 1,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestActiveParameterSnapshotLineage(unittest.TestCase):
    def _load(
        self,
        *,
        active_rows: list[object] | None = None,
        decision_rows: list[object] | None = None,
        known_bad_rows: list[object] | None = None,
    ) -> tuple[dict | None, _SnapshotConnection, _SnapshotEngine]:
        connection = _SnapshotConnection(
            active_rows=(
                [_canonical_active_row()] if active_rows is None else active_rows
            ),
            decision_rows=(
                [
                SimpleNamespace(
                    family="independent",
                    timeframe="15m",
                    combo_key="independent_15m",
                    current_status="keep_active",
                )
                ]
                if decision_rows is None
                else decision_rows
            ),
            known_bad_rows=known_bad_rows,
        )
        engine = _SnapshotEngine(connection)
        with patch("sqlalchemy.create_engine", return_value=engine):
            registry = _try_load_from_db("postgresql://managed.invalid/aats")
        return registry, connection, engine

    def test_uses_one_read_only_repeatable_read_snapshot(self) -> None:
        registry, connection, engine = self._load()
        self.assertIsNotNone(registry)
        self.assertEqual(connection.begin_count, 1)
        self.assertEqual(
            connection.statements[0],
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        )
        self.assertTrue(engine.disposed)
        self.assertIn("independent_15m", registry["active_sets"])

    def test_missing_approval_lineage_is_quarantined(self) -> None:
        registry, _, _ = self._load(
            active_rows=[_canonical_active_row(approval_recommendation_id=None)]
        )
        self.assertEqual(registry["active_sets"], {})
        self.assertEqual(
            registry["quarantined_combos"]["independent_15m"],
            "active_parameter_lineage_invalid",
        )

    def test_lineage_negative_matrix_is_fail_closed(self) -> None:
        cases = {
            "parameter_set_missing": {"canonical_parameter_set_id": None},
            "parameter_set_not_released": {"parameter_set_status": "candidate"},
            "parameter_source_mismatch": {
                "parameter_set_source_round_id": "other_round"
            },
            "recommendation_source_missing": {
                "recommendation_source_round_id": ""
            },
            "evidence_missing": {"evidence_bundle_ref": ""},
            "symbol_mismatch": {"recommendation_symbol": "ETH-USDT-SWAP"},
            "recommendation_type_invalid": {
                "recommendation_type": "keep_active"
            },
            "recommendation_status_invalid": {
                "recommendation_status": "draft"
            },
            "approver_missing": {"approved_by": ""},
            "approval_naive": {"approved_at": datetime.now()},
            "duplicate_apply_history": {"lineage_count": 2},
            "release_missing": {
                "canonical_release_id": None,
                "release_count": 0,
            },
            "duplicate_release": {"release_count": 2},
            "release_operation_missing": {"apply_operation_id": ""},
            "release_applied_at_missing": {"release_applied_at": None},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                registry, _, _ = self._load(
                    active_rows=[_canonical_active_row(**overrides)]
                )
                self.assertEqual(registry["active_sets"], {})
                self.assertEqual(
                    registry["quarantined_combos"]["independent_15m"],
                    "active_parameter_lineage_invalid",
                )

    def test_known_bad_parameter_set_is_quarantined(self) -> None:
        registry, _, _ = self._load(
            known_bad_rows=[
                SimpleNamespace(
                    release_id="rel_bad",
                    parameter_set_id="ps_1",
                    release_family="independent",
                    release_timeframe="15m",
                    release_combo_key="independent_15m",
                    risk_family="independent",
                    risk_timeframe="15m",
                    risk_combo_key="independent_15m",
                    apply_result="success",
                )
            ]
        )
        self.assertEqual(registry["active_sets"], {})
        self.assertEqual(
            registry["quarantined_combos"]["independent_15m"],
            "known_bad_parameter_set",
        )

    def test_malformed_risk_lineage_globally_quarantines(self) -> None:
        registry, _, _ = self._load(
            known_bad_rows=[
                SimpleNamespace(
                    release_id="rel_orphan",
                    parameter_set_id=None,
                    release_family=None,
                    release_timeframe=None,
                    release_combo_key=None,
                    risk_family="independent",
                    risk_timeframe="15m",
                    risk_combo_key="independent_15m",
                    apply_result=None,
                )
            ]
        )
        self.assertEqual(registry["active_sets"], {})
        self.assertEqual(
            registry["quarantined_combos"]["independent_15m"],
            "global_risk_evidence_lineage_invalid",
        )

    def test_non_object_values_are_quarantined(self) -> None:
        registry, _, _ = self._load(
            active_rows=[
                _canonical_active_row(
                    param_values=[1, 2], canonical_param_values=[1, 2]
                )
            ]
        )
        self.assertEqual(
            registry["quarantined_combos"]["independent_15m"],
            "active_parameter_values_not_object",
        )

    def test_decision_status_is_exact_and_not_trimmed(self) -> None:
        registry, _, _ = self._load(
            decision_rows=[
                SimpleNamespace(
                    family="independent",
                    timeframe="15m",
                    combo_key="independent_15m",
                    current_status=" keep_active",
                )
            ]
        )
        self.assertEqual(registry["active_sets"], {})
        self.assertIn(
            "decision_missing_or_not_apply_capable",
            registry["quarantined_combos"]["independent_15m"],
        )

    def test_duplicate_decisions_pause_and_quarantine(self) -> None:
        registry, _, _ = self._load(
            decision_rows=[
                SimpleNamespace(
                    family="independent",
                    timeframe="15m",
                    combo_key="independent_15m",
                    current_status="keep_active",
                ),
                SimpleNamespace(
                    family="INDEPENDENT",
                    timeframe="15M",
                    combo_key="legacy",
                    current_status="pause",
                ),
            ]
        )
        self.assertEqual(registry["active_sets"], {})
        self.assertIn("independent_15m", registry["paused_combos"])
        self.assertIn(
            "count=2", registry["quarantined_combos"]["independent_15m"]
        )

    def test_duplicate_canonical_active_rows_are_quarantined(self) -> None:
        registry, _, _ = self._load(
            active_rows=[
                _canonical_active_row(),
                _canonical_active_row(family="INDEPENDENT", timeframe="15M"),
            ]
        )
        self.assertEqual(registry["active_sets"], {})
        self.assertEqual(
            registry["quarantined_combos"]["independent_15m"],
            "duplicate_canonical_active_set",
        )

    def test_apply_capable_decision_without_active_row_is_quarantined(self) -> None:
        registry, _, _ = self._load(active_rows=[])

        self.assertEqual(registry["active_sets"], {})
        self.assertEqual(
            registry["quarantined_combos"]["independent_15m"],
            "apply_capable_decision_missing_active_set",
        )

    def test_explicit_pause_without_active_row_is_safe(self) -> None:
        registry, _, _ = self._load(
            active_rows=[],
            decision_rows=[
                SimpleNamespace(
                    family="independent",
                    timeframe="15m",
                    combo_key="independent_15m",
                    current_status="pause",
                )
            ],
        )

        self.assertEqual(registry["active_sets"], {})
        self.assertEqual(registry["quarantined_combos"], {})
        self.assertEqual(registry["paused_combos"], ["independent_15m"])

    def test_empty_managed_decision_state_is_quarantined(self) -> None:
        registry, _, _ = self._load(active_rows=[], decision_rows=[])

        self.assertEqual(
            registry["quarantined_combos"]["__governance__"],
            "managed_decision_state_empty",
        )


if __name__ == "__main__":
    unittest.main()
