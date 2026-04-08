from __future__ import annotations

import unittest
from types import SimpleNamespace

from aats.services.operator.runtime_queries import RuntimeQueryFacade


class _FakeOwner:
    def __init__(
        self,
        *,
        phase5_enabled: bool,
        financial_convergence_mode_enabled: bool,
        portfolio_ledger_truth_enabled: bool,
    ) -> None:
        self._phase5_enabled = phase5_enabled
        self.runtime = SimpleNamespace(
            settings=SimpleNamespace(
                financial_convergence_mode_enabled=financial_convergence_mode_enabled,
                portfolio_ledger_truth_enabled=portfolio_ledger_truth_enabled,
            )
        )

    def _phase5_control_plane_enabled(self) -> bool:
        return self._phase5_enabled


class _AIRuntimeFakeOwner:
    """Stage 7：模拟 process_role=gateway 时 ai_service 为 None 的 owner。

    构造一个最小 SimpleNamespace runtime，使 RuntimeQueryFacade.ai_runtime()
    能完整跑通而不依赖任何真实 service。strategy_profiles.snapshot() 不会被
    调到（stub 路径会在 ai_service is None 时 early return），所以这里只需
    桩 runtime.ai_service / runtime.settings 两个最小依赖。
    """

    def __init__(self, *, ai_service: object | None, process_role: str | None) -> None:
        self.runtime = SimpleNamespace(
            ai_service=ai_service,
            settings=SimpleNamespace(
                process_role=process_role,
                ai_manual_operating_mode_override_freeze_seconds=900,
            ),
        )


class TestRuntimeQueryFacade(unittest.TestCase):
    def test_control_plane_consistency_marks_phase5_without_financial_convergence_as_transitional(self) -> None:
        facade = RuntimeQueryFacade(
            _FakeOwner(
                phase5_enabled=True,
                financial_convergence_mode_enabled=False,
                portfolio_ledger_truth_enabled=True,
            )
        )

        snapshot = facade._control_plane_consistency()

        self.assertEqual(snapshot["status"], "transitional")
        self.assertIn(
            "phase5_control_plane_running_without_financial_convergence",
            snapshot["warning_codes"],
        )

    def test_control_plane_consistency_marks_ledger_truth_without_phase5_as_transitional(self) -> None:
        facade = RuntimeQueryFacade(
            _FakeOwner(
                phase5_enabled=False,
                financial_convergence_mode_enabled=False,
                portfolio_ledger_truth_enabled=True,
            )
        )

        snapshot = facade._control_plane_consistency()

        self.assertEqual(snapshot["status"], "transitional")
        self.assertIn(
            "portfolio_ledger_truth_enabled_without_phase5_control_plane",
            snapshot["warning_codes"],
        )


class TestAiRuntimeStubWhenServiceMissing(unittest.TestCase):
    """Stage 7 修复：gateway/market/execution role 下 runtime.ai_service is None。

    在原版实现里 ai_runtime() 直接 .status() 触发 AttributeError，向上传播到
    build_recovery_view → build_system_mode → build_system_health，让 /system/health
    /system/recovery /system/mode 三个 CORE_SPECS endpoint 全部 500，UI 整体崩。

    修复后 ai_runtime() 在 ai_service is None 时返回稳定 stub，下游消费者
    （recovery_view 内嵌 + UI ai-view）都用 .get() / `||` 安全访问。
    """

    def test_ai_runtime_returns_stub_when_ai_service_is_none(self) -> None:
        facade = RuntimeQueryFacade(
            _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        )

        result = facade.ai_runtime()

        # 关键标识：UI 一眼能看出本进程没装 AI 切片
        self.assertEqual(result["provider"], "not_loaded")
        self.assertEqual(result["provider_state"], "not_loaded")
        self.assertEqual(result["outcome_state"], "not_loaded")
        self.assertFalse(result["ai_service_loaded"])
        self.assertEqual(result["process_role"], "gateway")

        # 关键状态字段：必须是 falsy 但格式合法（UI 用 ?? 0 / || "unknown" 兜底）
        self.assertFalse(result["configured"])
        self.assertFalse(result["provider_ready"])
        self.assertFalse(result["degraded"])
        self.assertFalse(result["provider_degraded"])
        self.assertFalse(result["auto_downgrade_active"])
        self.assertFalse(result["manual_override_active"])
        self.assertFalse(result["shadow_mode_enabled"])

        # operating_mode 系列必须存在且为 None（UI 用 || "unknown" 兜底）
        self.assertIsNone(result["configured_operating_mode"])
        self.assertIsNone(result["effective_operating_mode"])
        self.assertIsNone(result["canonical_configured_operating_mode"])
        self.assertIsNone(result["canonical_effective_operating_mode"])

        # 计数字段必须是 0 而非 None（UI 直接 formatNumber，None 会渲染成 NaN）
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertEqual(result["consecutive_successes"], 0)
        self.assertEqual(result["recent_assessment_count"], 0)
        self.assertEqual(result["recent_fallback_ratio"], 0.0)

        # 嵌套结构 failure_budget / outcome_policy / legacy_modes 必须存在
        # （recovery_view 内嵌时 .get 链不会 KeyError）
        self.assertIn("failure_budget", result)
        self.assertIn("outcome_policy", result)
        self.assertIn("legacy_modes", result)
        self.assertEqual(result["failure_budget"]["remaining_failures_until_degrade"], 0)
        self.assertEqual(result["outcome_policy"]["bad_window_threshold"], 0)
        self.assertIsNone(result["legacy_modes"]["configured_operating_mode"])

        # strategy_profile 控制字段保持原 dict 形态，给 UI auto-control panel 兜底
        self.assertEqual(
            result["strategy_profile_auto_control_reason"], "ai_service_not_loaded"
        )
        self.assertEqual(
            result["strategy_profile_control_effective_mode"], "manual"
        )
        self.assertEqual(result["operating_mode_source"], "ai_service_not_loaded")

    def test_ai_runtime_stub_carries_process_role_label(self) -> None:
        """Stage 7：每个非 monolith role 下 stub 都应该带上自己的 process_role 标签
        便于 UI/审计区分是哪个进程提供的诊断（market vs decision vs execution）。"""
        for role in ("gateway", "market", "execution"):
            with self.subTest(role=role):
                facade = RuntimeQueryFacade(
                    _AIRuntimeFakeOwner(ai_service=None, process_role=role)
                )
                result = facade.ai_runtime()
                self.assertEqual(result["process_role"], role)
                self.assertFalse(result["ai_service_loaded"])

    def test_ai_runtime_stub_handles_missing_freeze_setting_gracefully(self) -> None:
        """settings 缺 ai_manual_operating_mode_override_freeze_seconds 字段时
        stub 不应崩，应返回 None。"""
        owner = _AIRuntimeFakeOwner(ai_service=None, process_role="gateway")
        # 删字段，模拟极端 settings 缺失
        del owner.runtime.settings.ai_manual_operating_mode_override_freeze_seconds
        facade = RuntimeQueryFacade(owner)

        result = facade.ai_runtime()

        self.assertIsNone(result["manual_override_default_freeze_seconds"])
