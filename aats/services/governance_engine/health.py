from __future__ import annotations

from typing import Any, Protocol

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import ComponentHealth, SystemHealthSnapshot
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.governance_engine.mode import RuntimeModeController
from aats.storage.base import ReconciliationRepository


class MarketStatusProvider(Protocol):
    def status(self) -> dict[str, Any]:
        ...


class AccountStatusProvider(Protocol):
    def status(self) -> dict[str, Any]:
        ...


class ExecutionReadinessProvider(Protocol):
    def readiness(self) -> dict[str, Any]:
        ...


class SystemHealthService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        mode_controller: RuntimeModeController,
        kill_switch: KillSwitch,
        market_provider: MarketStatusProvider,
        account_provider: AccountStatusProvider,
        execution_provider: ExecutionReadinessProvider,
        reconciliation_repo: ReconciliationRepository,
    ) -> None:
        self.settings = settings
        self.mode_controller = mode_controller
        self.kill_switch = kill_switch
        self.market_provider = market_provider
        self.account_provider = account_provider
        self.execution_provider = execution_provider
        self.reconciliation_repo = reconciliation_repo

    def snapshot(self) -> SystemHealthSnapshot:
        base_components = self._base_components()
        execution_status = self.execution_provider.readiness()
        components = [
            *base_components,
            self._component_from_status("execution_adapter", execution_status),
        ]
        blockers = [blocker for component in components for blocker in component.blockers]

        status = "ok"
        if blockers:
            status = "blocked"
        elif any(component.status == "warn" for component in components):
            status = "warn"

        return SystemHealthSnapshot(
            mode=self.mode_controller.mode,
            operating_state=self.mode_controller.operating_state(),
            status=status,
            halted=self.kill_switch.halted,
            blockers=blockers,
            components=components,
        )

    def execution_blockers(self) -> list[str]:
        return list(dict.fromkeys(self._base_blockers()))

    def _base_components(self) -> list[ComponentHealth]:
        market_status = self.market_provider.status()
        account_status = self.account_provider.status()
        reconciliation_status = self._reconciliation_status(self.reconciliation_repo.latest())
        return [
            self._component_from_status("market_data", market_status),
            self._component_from_status("account_state", account_status),
            self._component_from_status("reconciliation", reconciliation_status),
        ]

    def _base_blockers(self) -> list[str]:
        blockers = [blocker for component in self._base_components() for blocker in component.blockers]
        if self.kill_switch.halted:
            blockers.append("kill_switch_active")
        return blockers

    @staticmethod
    def _component_from_status(component: str, status: dict[str, Any]) -> ComponentHealth:
        blockers: list[str] = list(status.get("blockers", []))
        detail = status.get("detail") or status.get("last_error")
        return ComponentHealth(
            component=component,
            status="ok" if status.get("ready", status.get("fresh", False)) and status.get("connected", True) else (
                "warn" if status.get("connected", False) else "blocked"
            ),
            connected=bool(status.get("connected", True)),
            fresh=bool(status.get("fresh", True)),
            last_update_ts=status.get("last_update_ts"),
            detail=str(detail) if detail is not None else None,
            blockers=blockers,
        )

    def _reconciliation_status(self, report: ReconciliationReport | None) -> dict[str, Any]:
        if report is None:
            return {
                "connected": True,
                "fresh": False,
                "last_update_ts": None,
                "blockers": ["reconciliation_missing"],
                "detail": "No reconciliation report available",
            }
        age_seconds = (utc_now() - report.as_of_ts).total_seconds()
        fresh = age_seconds <= self.settings.reconciliation_stale_after_seconds
        blockers: list[str] = []
        if not fresh:
            blockers.append("reconciliation_stale")
        if report.halt_required:
            blockers.append("reconciliation_halt_required")
        if report.review_required:
            blockers.append("operator_rebaseline_required")
        return {
            "connected": True,
            "fresh": fresh,
            "last_update_ts": report.as_of_ts,
            "blockers": blockers,
            "detail": report.severity,
            "ready": fresh and not report.halt_required and not report.review_required,
        }
