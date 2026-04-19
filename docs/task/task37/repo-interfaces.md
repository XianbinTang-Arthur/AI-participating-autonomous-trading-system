# Task37 新 Repo 接口定义

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../../../docs/project_positioning.md)。


## 1. 目的

本文件定义 Phase 1 到 Phase 3 期间需要新增或替换的存储 repo 接口。目标是：

- 为新的执行状态机提供持久化边界。
- 为新的账本 posting 提供持久化边界。
- 为外部事件幂等消费提供 inbox / outbox 边界。
- 降低当前 `execution_repo / obligation_repo / portfolio_repo` 的职责耦合。

## 2. 推荐文件布局

- `aats/storage/execution_order_repo.py`
- `aats/storage/execution_order_repo_postgres.py`
- `aats/storage/execution_command_repo.py`
- `aats/storage/execution_command_repo_postgres.py`
- `aats/storage/execution_fill_repo_v2.py`
- `aats/storage/execution_fill_repo_v2_postgres.py`
- `aats/storage/reservation_repo.py`
- `aats/storage/reservation_repo_postgres.py`
- `aats/storage/ledger_repo.py`
- `aats/storage/ledger_repo_postgres.py`
- `aats/storage/inbox_repo.py`
- `aats/storage/inbox_repo_postgres.py`
- `aats/storage/command_outbox_repo.py`
- `aats/storage/command_outbox_repo_postgres.py`

## 3. 接口定义建议

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aats.schemas.common import EventEnvelope
from aats.schemas.execution import FillEvent, OrderIntent


class ExecutionOrderRepository(Protocol):
    def create_order(
        self,
        *,
        order_id: str,
        intent: OrderIntent,
        initial_state: str,
        created_at: datetime,
        raw_payload: dict,
    ) -> None: ...

    def get_order(self, order_id: str) -> dict | None: ...
    def get_order_by_intent(self, intent_id: str) -> dict | None: ...
    def get_order_by_client_order_id(self, client_order_id: str) -> dict | None: ...

    def update_order_state(
        self,
        *,
        order_id: str,
        expected_state_version: int,
        next_state: str,
        venue_order_id: str | None,
        last_exchange_ts: datetime | None,
        updated_at: datetime,
        raw_payload: dict,
    ) -> None: ...

    def open_orders(self) -> list[dict]: ...


class ExecutionOrderHistoryRepository(Protocol):
    def append_transition(
        self,
        *,
        order_id: str,
        from_state: str | None,
        to_state: str,
        reason_code: str | None,
        source: str,
        source_message_id: str | None,
        payload: dict,
        created_at: datetime,
    ) -> None: ...

    def history_for_order(self, order_id: str) -> list[dict]: ...


class ExecutionCommandRepository(Protocol):
    def enqueue_command(
        self,
        *,
        command_id: str,
        order_id: str,
        command_type: str,
        idempotency_key: str,
        payload: dict,
        created_at: datetime,
    ) -> None: ...

    def get_command(self, command_id: str) -> dict | None: ...
    def get_by_idempotency_key(self, idempotency_key: str) -> dict | None: ...
    def pending_commands(self, *, limit: int) -> list[dict]: ...
    def mark_sent(self, command_id: str, updated_at: datetime) -> None: ...
    def mark_acked(self, command_id: str, updated_at: datetime) -> None: ...
    def mark_failed(self, command_id: str, error: str, updated_at: datetime) -> None: ...


class ExecutionFillRepositoryV2(Protocol):
    def save_fill(
        self,
        *,
        fill: FillEvent,
        order_id: str,
        source: str,
        raw_payload: dict,
    ) -> bool: ...

    def get_fill(self, fill_id: str) -> dict | None: ...
    def get_fill_by_dedupe_key(self, source: str, venue_fill_id: str | None) -> dict | None: ...
    def fills_for_order(self, order_id: str) -> list[dict]: ...
    def fills_since(self, *, since: datetime | None = None, limit: int | None = None) -> list[dict]: ...


class ReservationRepositoryV2(Protocol):
    def create_reservation(
        self,
        *,
        reservation_id: str,
        order_id: str,
        reserve_account_id: str,
        reserved_amount: Decimal,
        state: str,
        created_at: datetime,
    ) -> None: ...

    def get_by_order_id(self, order_id: str) -> dict | None: ...
    def consume(self, *, reservation_id: str, amount: Decimal, updated_at: datetime) -> None: ...
    def release(self, *, reservation_id: str, amount: Decimal, next_state: str, updated_at: datetime) -> None: ...


class LedgerAccountRepository(Protocol):
    def get_or_create_account(
        self,
        *,
        account_type: str,
        currency: str,
        product_type: str,
        margin_mode: str,
        symbol: str | None,
        created_at: datetime,
    ) -> str: ...

    def get_account(self, account_id: str) -> dict | None: ...


class LedgerJournalRepository(Protocol):
    def create_journal(
        self,
        *,
        journal_id: str,
        journal_type: str,
        source_type: str,
        source_id: str,
        status: str,
        created_at: datetime,
        metadata: dict,
    ) -> None: ...

    def mark_posted(self, journal_id: str, posted_at: datetime) -> None: ...
    def get_by_source(self, source_type: str, source_id: str) -> dict | None: ...


class LedgerEntryRepository(Protocol):
    def append_entries(self, *, entries: list[dict]) -> None: ...
    def entries_for_journal(self, journal_id: str) -> list[dict]: ...
    def balance_by_account(self, account_id: str) -> Decimal: ...


class SettlementRepository(Protocol):
    def create_settlement(
        self,
        *,
        settlement_id: str,
        fill_id: str,
        order_id: str,
        state: str,
        created_at: datetime,
    ) -> None: ...

    def attach_journal(self, *, settlement_id: str, journal_id: str, posted_at: datetime) -> None: ...
    def get_by_fill_id(self, fill_id: str) -> dict | None: ...


class ExternalInboxRepository(Protocol):
    def save_incoming(
        self,
        *,
        inbox_id: str,
        source_system: str,
        dedupe_key: str,
        payload: dict,
        received_at: datetime,
    ) -> bool: ...

    def mark_processed(
        self,
        *,
        inbox_id: str,
        processing_result: str,
        processed_at: datetime,
        last_error: str | None = None,
    ) -> None: ...

    def unprocessed(self, *, limit: int) -> list[dict]: ...


class CommandOutboxRepositoryV2(Protocol):
    def enqueue(self, *, envelope: EventEnvelope, aggregate_type: str, aggregate_id: str) -> None: ...
    def pending(self, *, limit: int) -> list[dict]: ...
    def mark_published(self, event_id: str, published_at: datetime) -> None: ...
    def mark_failed(self, event_id: str, error: str) -> None: ...
```

## 4. 与现有 repo 的关系

现有 repo 不应立即删除，而应进入兼容期：

- `ExecutionRepository` 继续服务旧链路。
- `ExecutionOrderRepository` 服务新的执行状态机。
- `PortfolioRepository` 继续服务旧投影。
- `Ledger*Repository` 服务新的账本真相。

Phase 1 允许：

- 一次下单同时写入旧 `ExecutionRepository`。
- 同时写入新 `ExecutionOrderRepository`。

Phase 3 之后不再允许：

- 直接通过 `PortfolioRepository` 改写资金真相。
- 直接通过旧 obligation repo 维护保留金真相。

## 5. 接口设计约束

1. 所有 repo 输入输出都必须使用精确 `Decimal`，禁止在 repo 层使用 `float` 表达财务数值。
2. 所有幂等操作必须显式依赖唯一键：
   - `intent_id`
   - `client_order_id`
   - `venue_order_id`
   - `venue_fill_id`
   - `idempotency_key`
3. 所有状态推进接口都必须支持 optimistic concurrency，例如 `expected_state_version`。
4. 所有 inbox / outbox repo 都必须支持失败重试和状态机可见性。
