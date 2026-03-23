from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aats.services.execution_control.monitor import Phase1ShadowMonitor
from aats.services.execution_control.shadow import Phase1ExecutionShadowService
from aats.services.ledger.posting import Phase1LedgerMirrorService
from aats.storage.command_outbox_repo import CommandOutboxRepositoryV2
from aats.storage.execution_command_repo import ExecutionCommandRepository
from aats.storage.execution_fill_repo_v2 import ExecutionFillRepositoryV2
from aats.storage.execution_order_repo import ExecutionOrderHistoryRepository, ExecutionOrderRepository
from aats.storage.inbox_repo import ExternalInboxRepository
from aats.storage.ledger_repo import (
    LedgerAccountRepository,
    LedgerEntryRepository,
    LedgerJournalRepository,
    SettlementRepository,
)
from aats.storage.reservation_repo import ReservationRepositoryV2


@dataclass(slots=True)
class Phase1ShadowSubsystem:
    execution_order_repo: ExecutionOrderRepository | None = None
    execution_order_history_repo: ExecutionOrderHistoryRepository | None = None
    execution_command_repo: ExecutionCommandRepository | None = None
    execution_fill_repo: ExecutionFillRepositoryV2 | None = None
    reservation_repo: ReservationRepositoryV2 | None = None
    ledger_account_repo: LedgerAccountRepository | None = None
    ledger_journal_repo: LedgerJournalRepository | None = None
    ledger_entry_repo: LedgerEntryRepository | None = None
    settlement_repo: SettlementRepository | None = None
    external_inbox_repo: ExternalInboxRepository | None = None
    command_outbox_repo: CommandOutboxRepositoryV2 | None = None
    execution_shadow_service: Phase1ExecutionShadowService | None = None
    ledger_mirror_service: Phase1LedgerMirrorService | None = None
    monitor: Phase1ShadowMonitor | None = None

    def configured(self) -> bool:
        return any(
            (
                self.execution_order_repo is not None,
                self.execution_fill_repo is not None,
                self.reservation_repo is not None,
                self.execution_shadow_service is not None,
                self.ledger_mirror_service is not None,
                self.monitor is not None,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        if self.monitor is not None:
            return self.monitor.snapshot()
        return {
            "configured": self.configured(),
            "status": "not_configured" if not self.configured() else "idle",
            "connected": self.configured(),
            "ready": not self.configured(),
            "fresh": not self.configured(),
            "detail": "Phase 1 shadow compatibility layer is not configured in this runtime.",
            "summary": "Phase 1 shadow compatibility layer is not configured in this runtime.",
            "blockers": [],
            "lag": {
                "order_backlog": None,
                "fill_backlog": None,
                "obligation_backlog": None,
            },
            "execution_shadow": (
                self.execution_shadow_service.snapshot()
                if self.execution_shadow_service is not None
                else {"configured": False, "status": "not_configured"}
            ),
            "ledger_shadow": (
                self.ledger_mirror_service.snapshot()
                if self.ledger_mirror_service is not None
                else {"configured": False, "status": "not_configured"}
            ),
        }
