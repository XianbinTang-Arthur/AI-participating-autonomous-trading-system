from __future__ import annotations

from aats.bootstrap.settings import AATSSettings
from aats.services.strategy_engines.sleeve_reason_codes import (
    APPROVED_FOR_NON_PROTECTIVE_EXECUTION,
    AUTO_EXECUTION_DISABLED_BY_PROFILE,
    CANDIDATE_DISABLED,
    CANDIDATE_EXECUTION_INCOMPATIBLE,
    PROTECTIVE_INTENT_OVERRIDE,
    RUNTIME_NOT_SUPPORTED,
    unique_reason_codes,
)
from aats.services.strategy_engines.sleeve_routing_models import (
    ExecutionPermissionDecision,
    RawSleeveCandidateInputs,
)

NON_PROTECTIVE_ENTRY_EXECUTION_ADVISORY_ONLY_WARNING_CODE = (
    "non_protective_entry_execution_advisory_only"
)


def effective_non_protective_auto_execution_enabled(settings: AATSSettings) -> bool:
    return bool(settings.effective_strategy_sleeve_auto_execution_enabled)


def non_protective_entry_execution_guard(settings: AATSSettings) -> dict[str, object]:
    automatic_entry_enabled = effective_non_protective_auto_execution_enabled(settings)
    config_key = settings.strategy_sleeve_auto_execution_config_source
    if automatic_entry_enabled:
        return {
            "active": False,
            "status": "ready",
            "warning_code": None,
            "headline": "当前允许非保护性开仓自动进入执行链。",
            "summary": "当前非保护性开仓与加仓的自动执行已开启；新的 opening intent 可以继续进入 allocator 和执行链。",
            "operator_summary": "当前允许非保护性开仓与加仓自动执行，系统不会再把 opening intent 统一降级成仅参考。",
            "protective_execution_preserved": True,
            "configured_auto_execution_enabled": True,
            "effective_config_key": config_key,
            "using_deprecated_key": settings.strategy_sleeve_auto_execution_uses_deprecated_key,
        }
    return {
        "active": True,
        "status": "warning",
        "warning_code": NON_PROTECTIVE_ENTRY_EXECUTION_ADVISORY_ONLY_WARNING_CODE,
        "headline": "当前非保护性开仓自动执行已降级为仅参考。",
        "summary": "当前非保护性开仓与加仓只做参考（advisory-only），不会自动下单；保护性收缩与退出仍可继续执行。",
        "operator_summary": "当前非保护性开仓与加仓自动执行已关闭；opening intent 会在 allocator 前被降级成 advisory_only 或 hold_current。",
        "protective_execution_preserved": True,
        "configured_auto_execution_enabled": False,
        "effective_config_key": config_key,
        "using_deprecated_key": settings.strategy_sleeve_auto_execution_uses_deprecated_key,
    }


class SleeveExecutionPermissionPolicy:
    def __init__(self, settings: AATSSettings) -> None:
        self.settings = settings

    def evaluate(self, *, raw: RawSleeveCandidateInputs) -> ExecutionPermissionDecision:
        configured_auto_execution_enabled = effective_non_protective_auto_execution_enabled(self.settings)
        candidate_enabled = bool(raw.candidate_enabled)
        candidate_execution_compatible = bool(raw.candidate_execution_compatible)
        state_runtime_supported = bool(raw.state_runtime_supported)
        protective_intent = bool(raw.protective_intent)

        reason_codes: tuple[str, ...]
        permission_mode: str
        approved_for_execution: bool
        blocks_non_protective_execution = False

        if not state_runtime_supported:
            approved_for_execution = False
            permission_mode = "unsupported"
            blocks_non_protective_execution = True
            reason_codes = unique_reason_codes([RUNTIME_NOT_SUPPORTED])
        elif not candidate_execution_compatible:
            approved_for_execution = False
            permission_mode = "unsupported"
            blocks_non_protective_execution = True
            reason_codes = unique_reason_codes([CANDIDATE_EXECUTION_INCOMPATIBLE])
        elif protective_intent and (not configured_auto_execution_enabled or not candidate_enabled):
            approved_for_execution = True
            permission_mode = "protective_override"
            reason_codes = unique_reason_codes(
                [PROTECTIVE_INTENT_OVERRIDE],
                [AUTO_EXECUTION_DISABLED_BY_PROFILE] if not configured_auto_execution_enabled else [],
                [CANDIDATE_DISABLED] if not candidate_enabled else [],
            )
        elif not candidate_enabled:
            approved_for_execution = False
            permission_mode = "hold_current" if raw.active_inventory else "advisory_only"
            blocks_non_protective_execution = True
            reason_codes = unique_reason_codes([CANDIDATE_DISABLED])
        elif not configured_auto_execution_enabled:
            approved_for_execution = False
            permission_mode = "hold_current" if raw.active_inventory else "advisory_only"
            blocks_non_protective_execution = True
            reason_codes = unique_reason_codes([AUTO_EXECUTION_DISABLED_BY_PROFILE])
        else:
            approved_for_execution = True
            permission_mode = "approved"
            reason_codes = unique_reason_codes([APPROVED_FOR_NON_PROTECTIVE_EXECUTION])

        return ExecutionPermissionDecision(
            configured_auto_execution_enabled=configured_auto_execution_enabled,
            state_runtime_supported=state_runtime_supported,
            candidate_enabled=candidate_enabled,
            candidate_execution_compatible=candidate_execution_compatible,
            protective_intent=protective_intent,
            approved_for_execution=approved_for_execution,
            blocks_non_protective_execution=blocks_non_protective_execution,
            permission_mode=permission_mode,
            reason_codes=reason_codes,
            human_summary=self._human_summary(
                permission_mode=permission_mode,
                active_inventory=raw.active_inventory,
                state_runtime_supported=state_runtime_supported,
                candidate_execution_compatible=candidate_execution_compatible,
            ),
        )

    @staticmethod
    def _human_summary(
        *,
        permission_mode: str,
        active_inventory: bool,
        state_runtime_supported: bool,
        candidate_execution_compatible: bool,
    ) -> str:
        if permission_mode == "approved":
            return "当前允许非保护性自动执行。"
        if permission_mode == "protective_override":
            return "当前虽然关闭了普通自动执行，但保护性意图仍允许继续执行。"
        if permission_mode == "unsupported":
            if not state_runtime_supported:
                return "当前运行环境不支持这条 sleeve 自动进入执行链。"
            if not candidate_execution_compatible:
                return "当前 sleeve 候选不满足执行兼容性要求，因此不会自动进入执行链。"
            return "当前 sleeve 暂不满足自动执行前置条件。"
        if permission_mode == "hold_current":
            if active_inventory:
                return "当前不允许继续扩大风险，但系统会保持现有仓位。"
            return "当前不允许自动执行。"
        return "当前不允许自动执行，系统只保留参考信号。"
