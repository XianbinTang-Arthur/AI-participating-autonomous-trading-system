from __future__ import annotations

from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.services.execution_engine.okx_account import OKXAccountService


class ExchangeStateFetcher:
    def __init__(self, *, account_service: OKXAccountService | None = None) -> None:
        self.account_service = account_service

    def fetch_snapshot(self) -> ExchangeAccountSnapshot | None:
        if self.account_service is None:
            return None
        return self.account_service.latest_snapshot()
