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
                return "guarded_simulated_enabled"
            return "guarded_simulated_dry_run"
        if self.settings.live_submit_enabled and not self.settings.guarded_execution_dry_run:
            return "guarded_live_enabled"
        return "guarded_live_blocked"

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self._mode,
            "operating_state": self.operating_state(),
            "execution_backend": self.settings.execution_backend,
            "live_submit_enabled": self.settings.live_submit_enabled,
            "guarded_execution_dry_run": self.settings.guarded_execution_dry_run,
            "okx_simulated_trading": self.settings.okx_simulated_trading,
            "halted": self.kill_switch.halted,
        }
