from __future__ import annotations

from aats.bootstrap.settings import AATSSettings, RuntimeMode
from aats.schemas.system import OperatingState
from aats.services.governance_engine.kill_switch import KillSwitch


class RuntimeModeController:
    _SUPPORTED_SET = {"backtest", "paper_live", "guarded_live"}

    def __init__(self, *, settings: AATSSettings, kill_switch: KillSwitch) -> None:
        self.settings = settings
        self.kill_switch = kill_switch
        self._mode: RuntimeMode = settings.mode

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

    def operating_state(self) -> OperatingState:
        if self.settings.market_data_backend == "demo":
            return "local_demo"
        if self._mode == "paper_live":
            return "real_market_paper"
        if self.settings.execution_backend == "okx" and self.settings.okx_simulated_trading:
            if self.settings.live_submit_enabled and not self.settings.guarded_execution_dry_run:
                return "guarded_simulated_submit_enabled"
            return "guarded_simulated_submit_dry_run"
        if self.settings.live_submit_enabled and not self.settings.guarded_execution_dry_run:
            return "guarded_live_enabled"
        return "guarded_live_blocked"

    def snapshot(self) -> dict[str, object]:
        operating_state = self.operating_state()
        mode_behavior = self._mode_behavior(operating_state)
        return {
            "mode": self._mode,
            "config_profile": self.settings.config_profile,
            "operating_state": operating_state,
            "market_data_source": mode_behavior["market_data_source"],
            "account_read_source": mode_behavior["account_read_source"],
            "execution_backend": self.settings.execution_backend,
            "execution_route": mode_behavior["execution_route"],
            "exchange_submit_target": mode_behavior["exchange_submit_target"],
            "exchange_submit_allowed": mode_behavior["exchange_submit_allowed"],
            "submit_blocked_reasons": mode_behavior["submit_blocked_reasons"],
            "live_submit_enabled": self.settings.live_submit_enabled,
            "guarded_execution_dry_run": self.settings.guarded_execution_dry_run,
            "okx_simulated_trading": self.settings.okx_simulated_trading,
            "halted": self.kill_switch.halted,
        }

    def _mode_behavior(self, operating_state: OperatingState) -> dict[str, object]:
        market_data_source = "demo" if self.settings.market_data_backend == "demo" else "okx"
        account_read_source = "okx" if self.settings.account_read_enabled and self.settings.account_backend == "okx" else "disabled"
        if operating_state == "local_demo":
            return {
                "market_data_source": "demo",
                "account_read_source": "disabled",
                "execution_route": "paper_local",
                "exchange_submit_target": "none",
                "exchange_submit_allowed": False,
                "submit_blocked_reasons": ["local_demo_no_exchange_submission"],
            }
        if operating_state == "real_market_paper":
            return {
                "market_data_source": market_data_source,
                "account_read_source": account_read_source,
                "execution_route": "paper_local",
                "exchange_submit_target": "none",
                "exchange_submit_allowed": False,
                "submit_blocked_reasons": ["real_market_paper_uses_local_paper_execution"],
            }
        if operating_state == "guarded_simulated_submit_dry_run":
            return {
                "market_data_source": market_data_source,
                "account_read_source": account_read_source,
                "execution_route": "okx_demo_guarded",
                "exchange_submit_target": "okx_demo",
                "exchange_submit_allowed": False,
                "submit_blocked_reasons": ["guarded_execution_dry_run"],
            }
        if operating_state == "guarded_simulated_submit_enabled":
            return {
                "market_data_source": market_data_source,
                "account_read_source": account_read_source,
                "execution_route": "okx_demo_guarded",
                "exchange_submit_target": "okx_demo",
                "exchange_submit_allowed": True,
                "submit_blocked_reasons": [],
            }
        if operating_state == "guarded_live_enabled":
            return {
                "market_data_source": market_data_source,
                "account_read_source": account_read_source,
                "execution_route": "reserved_future_live",
                "exchange_submit_target": "future_real_money_live",
                "exchange_submit_allowed": False,
                "submit_blocked_reasons": ["real_money_live_not_supported"],
            }
        return {
            "market_data_source": market_data_source,
            "account_read_source": account_read_source,
            "execution_route": "reserved_future_live",
            "exchange_submit_target": "future_real_money_live",
            "exchange_submit_allowed": False,
            "submit_blocked_reasons": ["guarded_live_blocked_by_default"],
        }
