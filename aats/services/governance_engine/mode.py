from __future__ import annotations

from aats.bootstrap.settings import AATSSettings, RuntimeMode
from aats.schemas.system import OperatingState
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.runtime_layers import (
    EnvironmentCapabilities,
    PolicyProfile,
    RecoveryPolicy,
    RuntimeLayering,
    RuntimeProfile,
    resolve_runtime_layering,
)


class RuntimeModeController:
    _SUPPORTED_SET = {"backtest", "paper_live", "guarded_live"}

    def __init__(
        self,
        *,
        settings: AATSSettings,
        kill_switch: KillSwitch,
        runtime_layering: RuntimeLayering | None = None,
    ) -> None:
        self.settings = settings
        self.kill_switch = kill_switch
        self._mode: RuntimeMode = settings.mode
        self.runtime_layering = runtime_layering or resolve_runtime_layering(settings)

    @property
    def mode(self) -> RuntimeMode:
        return self._mode

    def set_mode(self, mode: RuntimeMode) -> RuntimeMode:
        if mode == "autonomous_live":
            raise ValueError("autonomous_live is not supported in this prototype")
        if mode not in self._SUPPORTED_SET:
            raise ValueError(f"unsupported mode={mode}")
        self._mode = mode
        return self._mode

    @property
    def runtime_profile(self) -> RuntimeProfile:
        return self.runtime_layering.runtime_profile

    @property
    def environment_capabilities(self) -> EnvironmentCapabilities:
        return self.runtime_layering.environment_capabilities

    @property
    def policy_profile(self) -> PolicyProfile:
        return self.runtime_layering.policy_profile

    @property
    def recovery_policy(self) -> RecoveryPolicy:
        return self.runtime_layering.recovery_policy

    def operating_state(self) -> OperatingState:
        return self.runtime_layering.operating_state

    def snapshot(self) -> dict[str, object]:
        operating_state = self.operating_state()
        mode_behavior = self._mode_behavior()
        return {
            "mode": self._mode,
            "config_profile": self.settings.config_profile,
            "startup_profile": self.settings.startup_profile,
            "operating_state": operating_state,
            "runtime_profile": self.runtime_profile.to_dict(),
            "environment_capabilities": self.environment_capabilities.to_dict(),
            "policy_profile": self.policy_profile.to_dict(),
            "recovery_policy": self.recovery_policy.to_dict(),
            "market_data_source": mode_behavior["market_data_source"],
            "account_read_source": mode_behavior["account_read_source"],
            "market_data_backend": self.settings.market_data_backend,
            "account_backend": self.settings.account_backend,
            "execution_backend": self.settings.execution_backend,
            "ai_operating_mode": self.settings.ai_operating_mode,
            "execution_route": mode_behavior["execution_route"],
            "exchange_submit_target": mode_behavior["exchange_submit_target"],
            "exchange_submit_allowed": mode_behavior["exchange_submit_allowed"],
            "submit_blocked_reasons": mode_behavior["submit_blocked_reasons"],
            "execution_blocked": not mode_behavior["exchange_submit_allowed"],
            "blocked_reason": (
                mode_behavior["submit_blocked_reasons"][0]
                if mode_behavior["submit_blocked_reasons"]
                else None
            ),
            "live_submit_enabled": self.settings.live_submit_enabled,
            "guarded_execution_dry_run": self.settings.guarded_execution_dry_run,
            "okx_simulated_trading": self.settings.okx_simulated_trading,
            "trading_product_type": self.settings.trading_product_type,
            "margin_mode": self.settings.margin_mode,
            "max_target_leverage": self.settings.max_target_leverage,
            "halted": self.kill_switch.halted,
        }

    def _mode_behavior(self) -> dict[str, object]:
        market_data_source = "demo" if self.environment_capabilities.market_data_source_kind == "demo" else "okx"
        account_read_source = (
            "okx" if self.environment_capabilities.account_state_source_kind == "exchange" else "disabled"
        )
        # All mode variants derive their behavior from environment_capabilities
        # and policy_profile, which are already differentiated by runtime_layering.
        return {
            "market_data_source": market_data_source,
            "account_read_source": account_read_source,
            "execution_route": self.environment_capabilities.execution_route,
            "exchange_submit_target": self.environment_capabilities.exchange_submission_target,
            "exchange_submit_allowed": self.environment_capabilities.exchange_submission_enabled,
            "submit_blocked_reasons": list(self.runtime_layering.mode_submit_blocked_reasons),
        }
