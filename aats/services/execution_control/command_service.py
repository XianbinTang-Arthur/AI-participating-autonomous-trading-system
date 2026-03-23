from __future__ import annotations

from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Any

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderIntent, OrderState
from aats.storage.execution_command_repo import ExecutionCommandRepository

SubmitExecutor = Callable[[OrderIntent, str | None], Awaitable[OrderState]]
CancelExecutor = Callable[[str], Awaitable[OrderState]]


class ExecutionCommandProcessor:
    def __init__(
        self,
        *,
        execution_command_repo: ExecutionCommandRepository,
        submit_executor: SubmitExecutor,
        cancel_executor: CancelExecutor,
    ) -> None:
        self.execution_command_repo = execution_command_repo
        self.submit_executor = submit_executor
        self.cancel_executor = cancel_executor
        self.logger = get_logger("aats.execution_control.command_service")
        self._lock = Lock()
        self._attempt_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._last_processed_command_id = None
        self._last_processed_command_type = None
        self._last_processed_state = None
        self._last_processed_at = None
        self._last_error = None

    async def process_pending(self, *, limit: int = 20) -> int:
        commands = self.execution_command_repo.pending_commands(limit=limit)
        processed = 0
        for command in commands:
            await self._process_command(command)
            processed += 1
        return processed

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            status = "idle"
            if self._failure_count > 0 and self._last_processed_state == "FAILED":
                status = "degraded"
            elif self._success_count > 0:
                status = "healthy"
            return {
                "configured": True,
                "status": status,
                "attempt_count": self._attempt_count,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "last_processed_command_id": self._last_processed_command_id,
                "last_processed_command_type": self._last_processed_command_type,
                "last_processed_state": self._last_processed_state,
                "last_processed_at": self._last_processed_at,
                "last_error": self._last_error,
            }

    async def _process_command(self, command: dict[str, Any]) -> None:
        command_id = str(command["command_id"])
        command_type = str(command["command_type"])
        now = utc_now()
        self.execution_command_repo.mark_sent(command_id, updated_at=now)
        with self._lock:
            self._attempt_count += 1
        try:
            if command_type == "submit":
                payload = dict(command.get("command_payload") or {})
                intent = OrderIntent.model_validate(payload.get("intent") or {})
                result = await self.submit_executor(intent, payload.get("client_order_id"))
            elif command_type == "cancel":
                payload = dict(command.get("command_payload") or {})
                client_order_id = str(payload.get("client_order_id") or "")
                if not client_order_id:
                    raise ValueError("cancel_command_missing_client_order_id")
                result = await self.cancel_executor(client_order_id)
            else:
                raise ValueError(f"unsupported_execution_command_type:{command_type}")
            acked_at = utc_now()
            self.execution_command_repo.mark_acked(command_id, updated_at=acked_at)
            with self._lock:
                self._success_count += 1
                self._last_processed_command_id = command_id
                self._last_processed_command_type = command_type
                self._last_processed_state = "ACKED"
                self._last_processed_at = acked_at
                self._last_error = None
            log_event(
                self.logger,
                "execution_command_acked",
                command_id=command_id,
                command_type=command_type,
                order_id=command.get("order_id"),
                order_status=result.status,
            )
        except Exception as exc:
            failed_at = utc_now()
            self.execution_command_repo.mark_failed(command_id, error=str(exc), updated_at=failed_at)
            with self._lock:
                self._failure_count += 1
                self._last_processed_command_id = command_id
                self._last_processed_command_type = command_type
                self._last_processed_state = "FAILED"
                self._last_processed_at = failed_at
                self._last_error = str(exc)
            log_event(
                self.logger,
                "execution_command_failed",
                level="error",
                **correlation_fields(
                    order_id=command.get("order_id"),
                    command_id=command_id,
                    command_type=command_type,
                    error=str(exc),
                ),
            )
