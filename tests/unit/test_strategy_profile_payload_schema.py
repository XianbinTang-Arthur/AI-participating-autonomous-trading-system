from __future__ import annotations

import unittest

from aats.bootstrap.settings import AATSSettings
from aats.schemas.strategy_profiles import (
    STRATEGY_PROFILE_MANAGED_FIELDS,
    StrategyProfilePayload,
    apply_strategy_profile_payload,
    strategy_profile_axes_from_payload,
    strategy_profile_payload_from_settings,
    summarize_strategy_profile_payload,
)


class StrategyProfilePayloadSchemaTests(unittest.TestCase):
    @staticmethod
    def _base_payload() -> dict[str, object]:
        settings = AATSSettings.model_validate({})
        return strategy_profile_payload_from_settings(settings).model_dump(mode="python")

    def test_payload_backfills_missing_short_thresholds_from_legacy_long_fields(self) -> None:
        raw = self._base_payload()
        for field in (
            "strategy_short_entry_allowed_regimes",
            "strategy_short_entry_min_signal_edge_bps",
            "strategy_short_entry_alpha_min",
            "strategy_short_entry_confidence_min",
            "strategy_short_scale_in_min_signal_edge_bps",
            "strategy_short_scale_in_alpha_min",
            "strategy_short_scale_in_confidence_min",
            "strategy_short_reversal_min_signal_edge_bps",
            "strategy_short_reversal_alpha_min",
            "strategy_short_reversal_confidence_min",
        ):
            raw.pop(field)

        payload = StrategyProfilePayload.model_validate(raw)

        self.assertEqual(payload.strategy_short_entry_allowed_regimes, payload.strategy_entry_allowed_regimes)
        self.assertEqual(payload.strategy_short_entry_min_signal_edge_bps, payload.strategy_entry_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_entry_alpha_min, payload.strategy_entry_alpha_min)
        self.assertEqual(payload.strategy_short_entry_confidence_min, payload.strategy_entry_confidence_min)
        self.assertEqual(payload.strategy_short_scale_in_min_signal_edge_bps, payload.strategy_scale_in_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_scale_in_alpha_min, payload.strategy_scale_in_alpha_min)
        self.assertEqual(payload.strategy_short_scale_in_confidence_min, payload.strategy_scale_in_confidence_min)
        self.assertEqual(payload.strategy_short_reversal_min_signal_edge_bps, payload.strategy_reversal_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_reversal_alpha_min, payload.strategy_reversal_alpha_min)
        self.assertEqual(payload.strategy_short_reversal_confidence_min, payload.strategy_reversal_confidence_min)

    def test_profile_summary_includes_short_confidence_thresholds(self) -> None:
        summary = summarize_strategy_profile_payload(self._base_payload())

        self.assertIn("strategy_entry_confidence_min", summary)
        self.assertIn("strategy_short_entry_confidence_min", summary)
        self.assertIn("strategy_scale_in_confidence_min", summary)
        self.assertIn("strategy_short_scale_in_confidence_min", summary)
        self.assertIn("strategy_reversal_confidence_min", summary)
        self.assertIn("strategy_short_reversal_confidence_min", summary)

    def test_axes_consider_stricter_short_side_alpha_thresholds(self) -> None:
        raw = self._base_payload()
        raw.update(
            {
                "strategy_entry_alpha_min": 0.16,
                "strategy_short_entry_alpha_min": 0.28,
                "strategy_scale_in_alpha_min": 0.20,
                "strategy_short_scale_in_alpha_min": 0.34,
                "strategy_reversal_alpha_min": 0.26,
                "strategy_short_reversal_alpha_min": 0.40,
            }
        )

        axes = strategy_profile_axes_from_payload(raw)

        self.assertEqual(axes.entry_threshold, "strict")
        self.assertEqual(axes.scale_in_threshold, "strict")
        self.assertEqual(axes.reversal_threshold, "strict")

    def test_spot_payload_from_settings_normalizes_short_fields_back_to_shared_thresholds(self) -> None:
        settings = AATSSettings.model_validate(
            {
                "trading_product_type": "spot",
                "margin_mode": "cash",
                "strategy_entry_allowed_regimes": ("trend", "breakout"),
                "strategy_entry_min_signal_edge_bps": 14.0,
                "strategy_entry_alpha_min": 0.18,
                "strategy_entry_confidence_min": 0.63,
                "strategy_scale_in_min_signal_edge_bps": 18.0,
                "strategy_scale_in_alpha_min": 0.24,
                "strategy_scale_in_confidence_min": 0.71,
                "strategy_reversal_min_signal_edge_bps": 24.0,
                "strategy_reversal_alpha_min": 0.34,
                "strategy_reversal_confidence_min": 0.79,
                "strategy_short_entry_allowed_regimes": ("uncertain",),
                "strategy_short_entry_min_signal_edge_bps": 11.0,
                "strategy_short_entry_alpha_min": 0.15,
                "strategy_short_entry_confidence_min": 0.55,
                "strategy_short_scale_in_min_signal_edge_bps": 16.0,
                "strategy_short_scale_in_alpha_min": 0.20,
                "strategy_short_scale_in_confidence_min": 0.64,
                "strategy_short_reversal_min_signal_edge_bps": 14.0,
                "strategy_short_reversal_alpha_min": 0.18,
                "strategy_short_reversal_confidence_min": 0.55,
            }
        )

        payload = strategy_profile_payload_from_settings(settings)

        self.assertEqual(payload.strategy_short_entry_allowed_regimes, payload.strategy_entry_allowed_regimes)
        self.assertEqual(payload.strategy_short_entry_min_signal_edge_bps, payload.strategy_entry_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_entry_alpha_min, payload.strategy_entry_alpha_min)
        self.assertEqual(payload.strategy_short_entry_confidence_min, payload.strategy_entry_confidence_min)
        self.assertEqual(payload.strategy_short_scale_in_min_signal_edge_bps, payload.strategy_scale_in_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_scale_in_alpha_min, payload.strategy_scale_in_alpha_min)
        self.assertEqual(payload.strategy_short_scale_in_confidence_min, payload.strategy_scale_in_confidence_min)
        self.assertEqual(payload.strategy_short_reversal_min_signal_edge_bps, payload.strategy_reversal_min_signal_edge_bps)
        self.assertEqual(payload.strategy_short_reversal_alpha_min, payload.strategy_reversal_alpha_min)
        self.assertEqual(payload.strategy_short_reversal_confidence_min, payload.strategy_reversal_confidence_min)

    def test_spot_profile_summary_omits_derivatives_only_short_fields(self) -> None:
        settings = AATSSettings.model_validate({"trading_product_type": "spot", "margin_mode": "cash"})

        payload = strategy_profile_payload_from_settings(settings)
        summary = summarize_strategy_profile_payload(payload, product_type="spot")

        self.assertIn("strategy_entry_confidence_min", summary)
        self.assertNotIn("strategy_short_entry_confidence_min", summary)
        self.assertNotIn("strategy_short_scale_in_confidence_min", summary)
        self.assertNotIn("strategy_short_reversal_confidence_min", summary)

    def test_spot_axes_ignore_stricter_short_side_alpha_thresholds(self) -> None:
        raw = self._base_payload()
        raw.update(
            {
                "strategy_entry_alpha_min": 0.16,
                "strategy_short_entry_alpha_min": 0.32,
                "strategy_scale_in_alpha_min": 0.20,
                "strategy_short_scale_in_alpha_min": 0.36,
                "strategy_reversal_alpha_min": 0.26,
                "strategy_short_reversal_alpha_min": 0.42,
            }
        )

        axes = strategy_profile_axes_from_payload(raw, product_type="spot")

        self.assertEqual(axes.entry_threshold, "relaxed")
        self.assertEqual(axes.scale_in_threshold, "relaxed")
        self.assertEqual(axes.reversal_threshold, "relaxed")


_INDEPENDENT_MANAGED_FIELDS = (
    "strategy_hedge_independent_long_entry_threshold",
    "strategy_hedge_independent_short_entry_threshold",
    "strategy_hedge_independent_long_close_threshold",
    "strategy_hedge_independent_short_close_threshold",
    "strategy_hedge_independent_long_scale_in_threshold",
    "strategy_hedge_independent_short_scale_in_threshold",
    "strategy_hedge_independent_long_min_hold_seconds",
    "strategy_hedge_independent_min_confirm_ticks",
    "strategy_hedge_independent_min_score_stability_bps",
)


class StrategyProfileIndependentExtensionTests(unittest.TestCase):
    """Profile 管辖面扩展到 independent family (2026-04-19).

    验证 9 个 independent 字段:
      1. 全部进入 STRATEGY_PROFILE_MANAGED_FIELDS
      2. settings → payload → settings 往返保留值
      3. apply_strategy_profile_payload 改写 settings
      4. 历史 payload (缺这 9 字段) 能无损反序列化 (default fill)

    注意: 这**只扩展机制 B (profile 切档管辖面)**, 不触碰机制 A
    (ai_operating_mode) —— 两者严格分离。
    """

    def test_managed_fields_includes_nine_independent_keys(self) -> None:
        for field in _INDEPENDENT_MANAGED_FIELDS:
            self.assertIn(
                field, STRATEGY_PROFILE_MANAGED_FIELDS,
                f"{field} must be in STRATEGY_PROFILE_MANAGED_FIELDS so切档 动作能联动改它",
            )

    def test_managed_fields_does_not_include_ai_operating_mode(self) -> None:
        """机制 A (ai_operating_mode) 与机制 B (profile 切档) 严格分离。

        ai_operating_mode 有独立的 Mode A/B/C 三档评分逻辑, 不应被 profile
        切档动作修改。如果将来要让它随 profile 联动, 需要单独的设计评审, 不能
        悄悄加进 MANAGED_FIELDS。
        """
        self.assertNotIn(
            "ai_operating_mode", STRATEGY_PROFILE_MANAGED_FIELDS,
            "ai_operating_mode 不归机制 B 管, 切档不应动它",
        )

    def test_payload_from_settings_captures_independent_values(self) -> None:
        settings = AATSSettings.model_validate({
            "strategy_hedge_independent_long_entry_threshold": 0.31,
            "strategy_hedge_independent_short_entry_threshold": 0.33,
            "strategy_hedge_independent_long_close_threshold": 0.15,
            "strategy_hedge_independent_short_close_threshold": 0.17,
            "strategy_hedge_independent_long_scale_in_threshold": 0.40,
            "strategy_hedge_independent_short_scale_in_threshold": 0.42,
            "strategy_hedge_independent_long_min_hold_seconds": 150.0,
            "strategy_hedge_independent_min_confirm_ticks": 4,
            "strategy_hedge_independent_min_score_stability_bps": 6.5,
        })

        payload = strategy_profile_payload_from_settings(settings)

        self.assertEqual(payload.strategy_hedge_independent_long_entry_threshold, 0.31)
        self.assertEqual(payload.strategy_hedge_independent_short_entry_threshold, 0.33)
        self.assertEqual(payload.strategy_hedge_independent_long_close_threshold, 0.15)
        self.assertEqual(payload.strategy_hedge_independent_short_close_threshold, 0.17)
        self.assertEqual(payload.strategy_hedge_independent_long_scale_in_threshold, 0.40)
        self.assertEqual(payload.strategy_hedge_independent_short_scale_in_threshold, 0.42)
        self.assertEqual(payload.strategy_hedge_independent_long_min_hold_seconds, 150.0)
        self.assertEqual(payload.strategy_hedge_independent_min_confirm_ticks, 4)
        self.assertEqual(payload.strategy_hedge_independent_min_score_stability_bps, 6.5)

    def test_apply_strategy_profile_payload_writes_independent_fields(self) -> None:
        """切档核心: apply_strategy_profile_payload 把 9 字段写入 settings。

        这是 'AI 自动切档 / operator 手动切档' 对 independent 家族真正生效的
        关键链路。之前 MANAGED_FIELDS 不含这些字段, 切档 independent 参数
        完全不变 —— 本次扩展就是修这个。
        """
        settings = AATSSettings.model_validate({
            "strategy_hedge_independent_long_entry_threshold": 0.66,
            "strategy_hedge_independent_min_confirm_ticks": 2,
        })
        # 构造一个 "range_defensive" 风格 payload (收紧入场, 放慢节奏)
        base = strategy_profile_payload_from_settings(settings).model_dump(mode="python")
        base.update({
            "strategy_hedge_independent_long_entry_threshold": 0.45,
            "strategy_hedge_independent_short_entry_threshold": 0.48,
            "strategy_hedge_independent_long_close_threshold": 0.20,
            "strategy_hedge_independent_short_close_threshold": 0.22,
            "strategy_hedge_independent_long_scale_in_threshold": 0.55,
            "strategy_hedge_independent_short_scale_in_threshold": 0.58,
            "strategy_hedge_independent_long_min_hold_seconds": 600.0,
            "strategy_hedge_independent_min_confirm_ticks": 5,
            "strategy_hedge_independent_min_score_stability_bps": 10.0,
        })
        payload = StrategyProfilePayload.model_validate(base)

        apply_strategy_profile_payload(settings, payload)

        # 9 个字段全部被改写
        self.assertEqual(settings.strategy_hedge_independent_long_entry_threshold, 0.45)
        self.assertEqual(settings.strategy_hedge_independent_short_entry_threshold, 0.48)
        self.assertEqual(settings.strategy_hedge_independent_long_close_threshold, 0.20)
        self.assertEqual(settings.strategy_hedge_independent_short_close_threshold, 0.22)
        self.assertEqual(settings.strategy_hedge_independent_long_scale_in_threshold, 0.55)
        self.assertEqual(settings.strategy_hedge_independent_short_scale_in_threshold, 0.58)
        self.assertEqual(settings.strategy_hedge_independent_long_min_hold_seconds, 600.0)
        self.assertEqual(settings.strategy_hedge_independent_min_confirm_ticks, 5)
        self.assertEqual(settings.strategy_hedge_independent_min_score_stability_bps, 10.0)

    def test_legacy_payload_without_independent_fields_deserializes_with_defaults(self) -> None:
        """历史 profile_revision DB 记录里缺这 9 字段时能无损 load。

        pydantic 默认值 = AATSSettings 对应字段默认值, load 出来的 payload 对
        应一个 "independent 默认档位" (不会随便给出 0 或 None 导致运行时
        NaN / divide-by-zero)。
        """
        settings = AATSSettings.model_validate({})
        raw = strategy_profile_payload_from_settings(settings).model_dump(mode="python")
        for field in _INDEPENDENT_MANAGED_FIELDS:
            raw.pop(field)

        payload = StrategyProfilePayload.model_validate(raw)

        # 默认值来自 AATSSettings 对应字段
        self.assertEqual(payload.strategy_hedge_independent_long_entry_threshold, 0.66)
        self.assertEqual(payload.strategy_hedge_independent_short_entry_threshold, 0.66)
        self.assertEqual(payload.strategy_hedge_independent_long_close_threshold, 0.66)
        self.assertEqual(payload.strategy_hedge_independent_short_close_threshold, 0.66)
        self.assertEqual(payload.strategy_hedge_independent_long_scale_in_threshold, 0.70)
        self.assertEqual(payload.strategy_hedge_independent_short_scale_in_threshold, 0.70)
        self.assertEqual(payload.strategy_hedge_independent_long_min_hold_seconds, 300.0)
        self.assertEqual(payload.strategy_hedge_independent_min_confirm_ticks, 2)
        self.assertEqual(payload.strategy_hedge_independent_min_score_stability_bps, 2.0)


if __name__ == "__main__":
    unittest.main()
