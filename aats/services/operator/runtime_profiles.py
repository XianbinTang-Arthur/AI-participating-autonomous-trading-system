from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.execution import OrderState
from aats.schemas.operator import AuthSource, OperatorActionRecord, OperatorRole
from aats.schemas.runtime_profiles import (
    RUNTIME_PROFILE_MANAGED_FIELDS,
    RuntimeProfileActivationState,
    RuntimeProfileDiff,
    RuntimeProfilePreflightResult,
    RuntimeProfileResolution,
    RuntimeProfileRevision,
    apply_runtime_profile_payload,
    classify_runtime_profile_change,
    diff_runtime_profile_payload,
    runtime_profile_payload_from_settings,
    stage_activation_state,
    summarize_runtime_profile_payload,
)
from aats.storage.base import EventStore, ExecutionRepository, RuntimeProfileRepository


class RuntimeProfileError(ValueError):
    pass


def runtime_profile_resolution(
    *,
    settings: AATSSettings,
    repo: RuntimeProfileRepository | None,
) -> RuntimeProfileResolution:
    if repo is None:
        return RuntimeProfileResolution(
            profile_source="env_only",
            resolved_settings=settings.model_dump(mode="python"),
        )
    return RuntimeProfileResolution(
        profile_source="env_fallback",
        activation_state=RuntimeProfileActivationState(),
        resolved_settings=settings.model_dump(mode="python"),
    )


def _activate_pending_revision(
    *,
    settings: AATSSettings,
    repo: RuntimeProfileRepository,
    activation: RuntimeProfileActivationState,
) -> RuntimeProfileRevision | None:
    if not activation.pending_revision_id:
        return None

    activation = activation.model_copy(
        update={
            "last_activation_result": "activation_in_progress",
            "last_activation_at": utc_now(),
            "last_activation_error": None,
        }
    )
    repo.save_activation_state(activation)
    pending = repo.get_revision(activation.pending_revision_id)
    if pending is None:
        activation = activation.model_copy(
            update={
                "last_activation_result": "activation_failed",
                "last_activation_error": "pending_revision_missing",
            }
        )
        repo.save_activation_state(activation)
        raise RuntimeProfileError("pending_runtime_profile_missing")
    try:
        apply_runtime_profile_payload(settings, pending.payload)
    except Exception as exc:
        failed = pending.model_copy(update={"status": "activation_failed"})
        repo.save_revision(failed)
        activation = activation.model_copy(
            update={
                "last_activation_result": "activation_failed",
                "last_activation_error": f"pending_revision_invalid:{exc}",
            }
        )
        repo.save_activation_state(activation)
        raise RuntimeProfileError(f"pending_runtime_profile_invalid:{exc}") from exc

    prior_active = repo.get_revision(activation.active_revision_id) if activation.active_revision_id else None
    if prior_active is not None:
        repo.save_revision(prior_active.model_copy(update={"status": "superseded"}))
    pending = pending.model_copy(update={"status": "active"})
    repo.save_revision(pending)
    activation = activation.model_copy(
        update={
            "previous_active_revision_id": activation.active_revision_id,
            "active_revision_id": pending.revision_id,
            "active_profile_label": pending.profile_label,
            "pending_revision_id": None,
            "pending_profile_label": None,
            "restart_required": False,
            "last_activation_result": "activation_succeeded",
            "last_activation_at": utc_now(),
            "last_activation_error": None,
        }
    )
    repo.save_activation_state(activation)
    return pending


def sanitize_runtime_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(payload) - set(RUNTIME_PROFILE_MANAGED_FIELDS))
    if unknown:
        raise RuntimeProfileError(f"runtime_profile_fields_unsupported:{','.join(unknown)}")
    return {field: payload[field] for field in RUNTIME_PROFILE_MANAGED_FIELDS if field in payload}


def describe_runtime_profile_diff(diff: RuntimeProfileDiff) -> list[str]:
    lines: list[str] = []
    for field in diff.changed_fields:
        previous = diff.previous_values.get(field)
        next_value = diff.next_values.get(field)
        if field == "trading_product_type":
            lines.append(f"Switches product posture from {previous or '-'} to {next_value or '-'}.")
        elif field == "margin_mode":
            lines.append(f"Changes margin model from {previous or '-'} to {next_value or '-'}.")
        elif field == "default_symbol":
            lines.append(f"Changes default symbol from {previous or '-'} to {next_value or '-'}.")
        elif field == "allowed_symbols":
            lines.append(
                f"Changes allowed symbols from {', '.join(previous or []) or '-'} to {', '.join(next_value or []) or '-'}."
            )
        elif field == "max_target_leverage":
            lines.append(f"Raises leverage cap from {previous}x to {next_value}x.")
        elif field == "default_target_leverage":
            lines.append(f"Changes default leverage from {previous}x to {next_value}x.")
        elif field == "default_order_qty":
            lines.append(f"Changes default order quantity from {previous} to {next_value}.")
        elif field == "max_notional_per_symbol":
            lines.append(f"Changes max notional per symbol from {previous} to {next_value}.")
        elif field == "strategy_short_bias_enabled":
            lines.append(f"{'Enables' if next_value else 'Disables'} short-bias strategy behavior.")
        elif field == "strategy_dynamic_leverage_enabled":
            lines.append(f"{'Enables' if next_value else 'Disables'} dynamic leverage behavior.")
        else:
            lines.append(f"Changes {field.replace('_', ' ')} from {previous} to {next_value}.")
    return lines


def readonly_runtime_profile_snapshot(
    *,
    settings: AATSSettings,
    resolution: RuntimeProfileResolution,
) -> dict[str, Any]:
    current_payload = runtime_profile_payload_from_settings(settings)
    return {
        "profile_source": resolution.profile_source,
        "active_revision": None,
        "pending_revision": None,
        "activation": RuntimeProfileActivationState().model_dump(mode="json"),
        "current_runtime_payload": current_payload,
        "current_runtime_summary": summarize_runtime_profile_payload(current_payload),
        "revisions": [],
        "management_enabled": False,
        "control_plane_status": "deprecated_readonly",
        "control_plane_summary": "runtime_profile_control_disabled",
    }


def runtime_profile_action_payload(
    *,
    action: str,
    actor_role: OperatorRole,
    actor_identity: str | None,
    auth_source: AuthSource,
    status: str,
    previous_revision_id: str | None = None,
    new_revision_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> OperatorActionRecord:
    return OperatorActionRecord(
        action=action,  # type: ignore[arg-type]
        actor_role=actor_role,
        actor_identity=actor_identity,
        auth_source=auth_source,
        reason="runtime_profile_control",
        status=status,
        details={
            "previous_revision_id": previous_revision_id,
            "new_revision_id": new_revision_id,
            **(details or {}),
        },
    )


@dataclass(slots=True)
class RuntimeProfileControlService:
    settings: AATSSettings
    repo: RuntimeProfileRepository
    execution_repo: ExecutionRepository | None = None
    event_store: EventStore | None = None

    @staticmethod
    def _disabled() -> RuntimeProfileError:
        return RuntimeProfileError("runtime_profile_control_disabled")

    def snapshot(self) -> dict[str, Any]:
        current_payload = runtime_profile_payload_from_settings(self.settings)
        return {
            "profile_source": "env_fallback",
            "active_revision": None,
            "pending_revision": None,
            "activation": RuntimeProfileActivationState().model_dump(mode="json"),
            "current_runtime_payload": current_payload,
            "current_runtime_summary": summarize_runtime_profile_payload(current_payload),
            "revisions": [],
            "management_enabled": False,
        }

    def create_draft(
        self,
        *,
        profile_label: str,
        actor_identity: str | None,
    ) -> tuple[RuntimeProfileRevision, RuntimeProfileDiff]:
        raise self._disabled()

    def update_draft(
        self,
        *,
        revision_id: str,
        profile_label: str | None,
        payload: dict[str, Any],
        activation_note: str | None,
        actor_identity: str | None,
    ) -> tuple[RuntimeProfileRevision, RuntimeProfileDiff]:
        raise self._disabled()

    def stage_revision(
        self,
        *,
        revision_id: str,
        actor_identity: str | None,
    ) -> tuple[RuntimeProfileRevision, RuntimeProfileDiff, RuntimeProfilePreflightResult, RuntimeProfileActivationState]:
        raise self._disabled()

    def cancel_pending(self) -> RuntimeProfileActivationState:
        raise self._disabled()

    def request_restart(self, *, actor_identity: str | None) -> RuntimeProfileActivationState:
        raise self._disabled()

    def preflight(self, classification: str) -> RuntimeProfilePreflightResult:
        raise self._disabled()

    def active_revision(self) -> RuntimeProfileRevision | None:
        return None

    def audit_payload(
        self,
        *,
        action: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        status: str,
        previous_revision_id: str | None = None,
        new_revision_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> OperatorActionRecord:
        return OperatorActionRecord(
            action=action,  # type: ignore[arg-type]
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason="runtime_profile_control",
            status=status,
            details={
                "previous_revision_id": previous_revision_id,
                "new_revision_id": new_revision_id,
                **(details or {}),
            },
        )

    def _revision_view(
        self,
        revision: RuntimeProfileRevision,
        active: RuntimeProfileRevision | None,
        pending: RuntimeProfileRevision | None,
    ) -> dict[str, Any]:
        base_payload = active.payload if active is not None else runtime_profile_payload_from_settings(self.settings)
        diff = diff_runtime_profile_payload(base_payload, revision.payload)
        return {
            **revision.model_dump(mode="json"),
            "is_active": active is not None and active.revision_id == revision.revision_id,
            "is_pending": pending is not None and pending.revision_id == revision.revision_id,
            "diff": diff.model_dump(mode="json"),
            "diff_narrative": describe_runtime_profile_diff(diff),
        }

    @staticmethod
    def _open_order_blocker(order: OrderState) -> dict[str, Any]:
        return {
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "status": order.status,
            "submission_mode": order.submission_mode,
            "venue": order.venue,
            "requested_qty": order.requested_qty,
            "remaining_qty": order.remaining_qty,
        }
