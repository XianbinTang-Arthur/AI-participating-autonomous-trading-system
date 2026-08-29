#!/usr/bin/env python3
"""Fail-closed, read-only JetStream durable cutover preflight.

This command is intentionally limited to the loopback NATS endpoint used by
the WSL2 deployment.  It lists every stream and every consumer with explicit
pagination, then evaluates only existing four-role durables whose topic is a
stream-backed ``DEFAULT_CRITICAL_TOPICS`` event with expected
``DeliverPolicy.ALL``.  Persist-only critical topics have no JetStream stream
or live consumer and are deliberately excluded.

The command never acknowledges messages and never creates, updates, deletes,
or purges JetStream state.  Every outcome is written as a no-secret JSON
evidence packet under ``artifacts/deployments`` before the exit code is
returned to the deployment pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.docker_event_monitor import (  # noqa: E402
    LiveDockerEventMonitor,
    validate_live_window_evidence,
)
from scripts.nats_runtime_identity import (  # noqa: E402
    NATS_CONTAINER as _NATS_CONTAINER,
    capture_nats_identity,
    capture_nats_volume_fingerprint,
)

_NATS_LOOPBACK_SERVER = "nats://127.0.0.1:4222"
_MAIN_ROLES = ("gateway", "market", "decision", "execution")
_KNOWN_APP_CONTAINERS = (
    "aats-gateway",
    "aats-market",
    "aats-decision",
    "aats-execution",
    "aats-rdp-daemon",
    "aats-liquidations-daemon",
    "aats-microstructure-collector",
)
_TARGET_MAX_ACK_PENDING = 1
_GENERATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_DIR = Path("artifacts/deployments")
_SCHEMA_VERSION = "aats.nats_durable_cutover_preflight.v2"
_MAX_PAGINATED_ITEMS = 100_000
_MAX_PREVIOUS_EVIDENCE_BYTES = 5 * 1024 * 1024
_STAGE_PRE_FULL_DOWN = "pre_full_down"
_STAGE_POST_INFRA_PRE_APP_UP = "post_infra_pre_app_up"
_STAGES = (_STAGE_PRE_FULL_DOWN, _STAGE_POST_INFRA_PRE_APP_UP)
_NATS_BOOTSTRAP_MODES = (
    "existing_container_preserved",
    "proven_fresh_install",
)
_APP_QUIESCENCE_INSPECT_TEMPLATE = (
    '{"Name":{{json .Name}},"Id":{{json .Id}},'
    '"Status":{{json .State.Status}},'
    '"StartedAt":{{json .State.StartedAt}},'
    '"FinishedAt":{{json .State.FinishedAt}},'
    '"RestartCount":{{json .RestartCount}},'
    '"ComposeProject":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"ComposeService":{{json (index .Config.Labels "com.docker.compose.service")}}}'
)
_TARGET_ENV_FIELDS: dict[str, tuple[str, str, type[int] | type[float]]] = {
    "AATS_NATS_MARKET_MAX_BYTES": ("AATS_EVENTS_MARKET", "max_bytes", int),
    "AATS_NATS_MARKET_MAX_MSGS": ("AATS_EVENTS_MARKET", "max_msgs", int),
    "AATS_NATS_MARKET_MAX_MSG_SIZE": (
        "AATS_EVENTS_MARKET",
        "max_msg_size",
        int,
    ),
    "AATS_NATS_MARKET_MAX_AGE_SECONDS": (
        "AATS_EVENTS_MARKET",
        "max_age_seconds",
        float,
    ),
    "AATS_NATS_EVENTS_MAX_BYTES": ("AATS_EVENTS", "max_bytes", int),
    "AATS_NATS_EVENTS_MAX_MSGS": ("AATS_EVENTS", "max_msgs", int),
    "AATS_NATS_EVENTS_MAX_MSG_SIZE": ("AATS_EVENTS", "max_msg_size", int),
    "AATS_NATS_EVENTS_MAX_AGE_SECONDS": (
        "AATS_EVENTS",
        "max_age_seconds",
        float,
    ),
}

T = TypeVar("T")
CommandRunner = Callable[[Sequence[str]], str]


@dataclass(frozen=True, slots=True)
class ExpectedDurable:
    durable: str
    role: str
    topic: str
    stream: str
    filter_subject: str
    ack_policy: str = "explicit"
    deliver_policy: str = "all"


@dataclass(frozen=True, slots=True)
class ConsumerCursor:
    delivered_stream_seq: int
    delivered_consumer_seq: int
    ack_floor_stream_seq: int
    ack_floor_consumer_seq: int


@dataclass(frozen=True, slots=True)
class ConsumerState:
    stream: str
    durable: str
    created: str
    deliver_policy: str
    ack_policy: str
    filter_subject: str | None
    filter_subjects: tuple[str, ...]
    deliver_group: str | None
    max_ack_pending: int
    num_ack_pending: int
    cursor: ConsumerCursor


@dataclass(frozen=True, slots=True)
class CriticalStreamState:
    name: str
    created: str
    subjects: tuple[str, ...]
    retention: str
    storage: str
    discard: str
    max_age_seconds: float
    max_bytes: int
    max_msgs: int
    max_msg_size: int
    num_replicas: int
    duplicate_window_seconds: float
    deny_purge: bool
    messages: int
    bytes: int
    first_seq: int
    last_seq: int
    consumer_count: int
    deleted: tuple[int, ...]
    num_deleted: int


@dataclass(frozen=True, slots=True)
class QueryResult:
    stream_count: int
    consumer_count: int
    consumers: tuple[ConsumerState, ...]
    streams: tuple[CriticalStreamState, ...] = ()


def _run_command(args: Sequence[str]) -> str:
    completed = subprocess.run(
        list(args),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    return completed.stdout.strip()


def _validated_commit(raw: str) -> str:
    value = raw.strip()
    if not _COMMIT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid_deployed_commit")
    return value


def _validated_generation(raw: str) -> str:
    value = raw.strip()
    if not _GENERATION_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid_runtime_readiness_generation")
    return value


def _validated_sha256(raw: str) -> str:
    value = raw.strip()
    if not _SHA256_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid_nats_baseline_fingerprint")
    return value


def _required_nats_bootstrap(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "mode",
        "baseline_fingerprint",
        "volume_fingerprint",
    }:
        raise RuntimeError("nats_cutover_malformed_bootstrap_provenance")
    mode = value.get("mode")
    fingerprint = value.get("baseline_fingerprint")
    volume_fingerprint = value.get("volume_fingerprint")
    if (
        mode not in _NATS_BOOTSTRAP_MODES
        or not isinstance(fingerprint, str)
        or not isinstance(volume_fingerprint, str)
    ):
        raise RuntimeError("nats_cutover_malformed_bootstrap_provenance")
    if not _SHA256_RE.fullmatch(fingerprint) or not _SHA256_RE.fullmatch(
        volume_fingerprint
    ):
        raise RuntimeError("nats_cutover_malformed_bootstrap_provenance")
    return {
        "mode": mode,
        "baseline_fingerprint": fingerprint,
        "volume_fingerprint": volume_fingerprint,
    }


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_text_from_ns(value: int) -> str:
    return _utc_text(datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc))


def _parse_evidence_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError("nats_cutover_malformed_evidence_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("nats_cutover_malformed_evidence_time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("nats_cutover_malformed_evidence_time")
    return parsed


def _docker_timestamp(value: int) -> str:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    return f"{seconds}.{nanoseconds:09d}"


def capture_nats_baseline_fingerprint(
    run: CommandRunner = _run_command,
) -> str:
    """Hash the exact healthy NATS instance and its standard persistent mount."""
    return str(capture_nats_identity(run)["fingerprint"])


def capture_app_quiescence(
    run: CommandRunner = _run_command,
) -> tuple[dict[str, object], ...]:
    """Capture exact stopped/not-found lifecycle facts for every known app."""

    listed = run(("docker", "ps", "-a", "--format", "{{.Names}}"))
    names = [line.strip() for line in listed.splitlines() if line.strip()]
    if len(names) != len(set(names)):
        raise RuntimeError("app_quiescence_duplicate_container_name")
    existing = set(names)
    snapshot: list[dict[str, object]] = []
    for name in _KNOWN_APP_CONTAINERS:
        if name not in existing:
            snapshot.append(
                {
                    "name": name,
                    "existence": "not_found",
                    "container_id": None,
                    "status": None,
                    "started_at": None,
                    "finished_at": None,
                    "restart_count": None,
                }
            )
            continue
        raw = run(
            (
                "docker",
                "inspect",
                "--format",
                _APP_QUIESCENCE_INSPECT_TEMPLATE,
                name,
            )
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("app_quiescence_invalid_inspect_json") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("app_quiescence_invalid_inspect_json")
        raw_name = payload.get("Name")
        if raw_name != f"/{name}":
            raise RuntimeError("app_quiescence_container_name_mismatch")
        if payload.get("ComposeProject") != "aats-dev":
            raise RuntimeError("app_quiescence_compose_project_mismatch")
        if payload.get("ComposeService") != name:
            raise RuntimeError("app_quiescence_compose_service_mismatch")
        container_id = payload.get("Id")
        restart_count = payload.get("RestartCount")
        if not isinstance(container_id, str) or not _CONTAINER_ID_RE.fullmatch(
            container_id
        ):
            raise RuntimeError("app_quiescence_invalid_container_id")
        status = payload.get("Status")
        started_at = payload.get("StartedAt")
        finished_at = payload.get("FinishedAt")
        if status not in {"exited", "dead"}:
            raise RuntimeError(f"app_quiescence_container_not_stopped:{name}")
        if not isinstance(started_at, str) or not started_at:
            raise RuntimeError("app_quiescence_invalid_started_at")
        if not isinstance(finished_at, str) or not finished_at:
            raise RuntimeError("app_quiescence_invalid_finished_at")
        if (
            isinstance(restart_count, bool)
            or not isinstance(restart_count, int)
            or restart_count < 0
        ):
            raise RuntimeError("app_quiescence_invalid_restart_count")
        snapshot.append(
            {
                "name": name,
                "existence": "present",
                "container_id": container_id,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "restart_count": restart_count,
            }
        )
    return tuple(snapshot)


def query_app_lifecycle_events(
    *,
    since_ns: int,
    until_ns: int,
    run: CommandRunner = _run_command,
) -> tuple[dict[str, object], ...]:
    """Read Docker's lifecycle event history for the exact NATS query window."""

    if since_ns <= 0 or until_ns < since_ns:
        raise RuntimeError("app_quiescence_invalid_event_window")
    raw = run(
        (
            "docker",
            "events",
            "--since",
            _docker_timestamp(since_ns),
            "--until",
            _docker_timestamp(until_ns),
            "--filter",
            "type=container",
            "--format",
            "{{json .}}",
        )
    )
    relevant: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("app_quiescence_invalid_event_json") from exc
        if not isinstance(payload, dict) or payload.get("Type") != "container":
            raise RuntimeError("app_quiescence_invalid_event_json")
        actor = payload.get("Actor")
        if not isinstance(actor, dict):
            raise RuntimeError("app_quiescence_invalid_event_actor")
        attributes = actor.get("Attributes")
        if not isinstance(attributes, dict):
            raise RuntimeError("app_quiescence_invalid_event_attributes")
        name = attributes.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("app_quiescence_invalid_event_name")
        if name not in (*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER):
            continue
        action = payload.get("Action", payload.get("status"))
        container_id = actor.get("ID", payload.get("id"))
        time_nano = payload.get("timeNano")
        if not isinstance(action, str) or not action:
            raise RuntimeError("app_quiescence_invalid_event_action")
        if not isinstance(container_id, str) or not _CONTAINER_ID_RE.fullmatch(
            container_id
        ):
            raise RuntimeError("app_quiescence_invalid_event_container_id")
        if (
            isinstance(time_nano, bool)
            or not isinstance(time_nano, int)
            or not (since_ns <= time_nano <= until_ns)
        ):
            raise RuntimeError("app_quiescence_invalid_event_time")
        relevant.append(
            {
                "name": name,
                "container_id": container_id,
                "action": action,
                "time_nano": time_nano,
            }
        )
    relevant.sort(
        key=lambda event: (
            int(event["time_nano"]),
            str(event["name"]),
            str(event["action"]),
        )
    )
    return tuple(relevant)


def build_app_quiescence_evidence(
    *,
    since_ns: int,
    until_ns: int,
    before: Sequence[dict[str, object]],
    after: Sequence[dict[str, object]],
    events: Sequence[dict[str, object]],
    event_capture: dict[str, object],
) -> dict[str, object]:
    validate_live_window_evidence(
        event_capture,
        expected_allowlist=(*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER),
        expected_start_ns=since_ns,
        expected_cutoff_ns=until_ns,
    )
    fingerprint_match = list(before) == list(after)
    status = (
        "PASSED_WITH_TRUST_BOUNDARY"
        if fingerprint_match and not events
        else "INVALIDATED"
    )
    return {
        "status": status,
        "complete": False,
        "coverage_status": "BOUNDED_OBSERVED",
        "allowlist": list(_KNOWN_APP_CONTAINERS),
        "lifecycle_event_allowlist": [*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER],
        "window_started_ns": since_ns,
        "window_ended_ns": until_ns,
        "window_started_at_utc": _utc_text_from_ns(since_ns),
        "window_ended_at_utc": _utc_text_from_ns(until_ns),
        "fingerprint_match": fingerprint_match,
        "before": list(before),
        "after": list(after),
        "event_capture": event_capture,
        "lifecycle_events": list(events),
    }


def build_expected_durable_index() -> dict[str, ExpectedDurable]:
    """Derive the exact four-role critical ALL durable names from runtime truth."""

    from aats.bus.nats_bus import (
        DEFAULT_CRITICAL_TOPICS,
        DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS,
        NatsBusConfig,
        delivery_semantics_for,
    )

    config = NatsBusConfig()
    expected: dict[str, ExpectedDurable] = {}
    stream_backed_topics = (
        DEFAULT_CRITICAL_TOPICS - DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS
    )
    for role in _MAIN_ROLES:
        for topic in sorted(stream_backed_topics):
            if delivery_semantics_for(topic) != "event":
                continue
            durable = config.durable_name_for(role, topic)
            stream_spec = config.stream_spec_for_topic(topic)
            if stream_spec is None:
                raise RuntimeError("nats_cutover_expected_stream_missing")
            candidate = ExpectedDurable(
                durable=durable,
                role=role,
                topic=topic,
                stream=stream_spec.name,
                filter_subject=config.subject_for(topic),
            )
            previous = expected.setdefault(durable, candidate)
            if previous != candidate:
                raise RuntimeError("nats_cutover_expected_durable_collision")
    return expected


def evaluate_consumer(
    state: ConsumerState,
    expected: ExpectedDurable,
) -> dict[str, object]:
    """Classify one existing expected durable without mutating broker state."""

    blockers: list[str] = []
    if state.stream != expected.stream:
        blockers.append("stream_drift")
    if state.deliver_policy != expected.deliver_policy:
        blockers.append("deliver_policy_drift")
    if state.ack_policy != expected.ack_policy:
        blockers.append("ack_policy_drift")
    filter_matches = (
        state.filter_subject == expected.filter_subject
        and not state.filter_subjects
    ) or (
        state.filter_subject in {None, ""}
        and state.filter_subjects == (expected.filter_subject,)
    )
    if not filter_matches:
        blockers.append("filter_subject_drift")
    if state.deliver_group not in {None, ""}:
        blockers.append("deliver_group_drift")
    if state.num_ack_pending < 0:
        blockers.append("invalid_num_ack_pending")

    reducing_window = (
        state.max_ack_pending <= 0
        or state.max_ack_pending > _TARGET_MAX_ACK_PENDING
    )
    if reducing_window and state.num_ack_pending != 0:
        blockers.append("ack_window_migration_requires_drain")
    elif (
        state.max_ack_pending == _TARGET_MAX_ACK_PENDING
        and state.num_ack_pending > _TARGET_MAX_ACK_PENDING
    ):
        blockers.append("outstanding_exceeds_target")

    if blockers:
        status = "BLOCKED"
    elif reducing_window:
        status = "SAFE_TO_SHRINK"
    else:
        status = "SAFE_ALREADY_ONE"

    return {
        "identity": {
            "stream": state.stream,
            "durable": state.durable,
            "role": expected.role,
            "topic": expected.topic,
        },
        "created": state.created,
        "cursor": asdict(state.cursor),
        "immutable_config": {
            "actual": {
                "stream": state.stream,
                "deliver_policy": state.deliver_policy,
                "ack_policy": state.ack_policy,
                "filter_subject": state.filter_subject,
                "filter_subjects": list(state.filter_subjects),
                "deliver_group": state.deliver_group,
            },
            "expected": {
                "stream": expected.stream,
                "deliver_policy": expected.deliver_policy,
                "ack_policy": expected.ack_policy,
                "filter_subject": expected.filter_subject,
                "deliver_group": None,
            },
        },
        "window": {
            "current_max_ack_pending": state.max_ack_pending,
            "target_max_ack_pending": _TARGET_MAX_ACK_PENDING,
        },
        "outstanding": {"num_ack_pending": state.num_ack_pending},
        "status": status,
        "blockers": blockers,
    }


def evaluate_existing_consumers(
    consumers: Sequence[ConsumerState],
    expected_by_name: dict[str, ExpectedDurable],
) -> tuple[list[dict[str, object]], bool]:
    """Evaluate only existing consumers matching the current critical ALL map."""

    rows = [
        evaluate_consumer(state, expected_by_name[state.durable])
        for state in consumers
        if state.durable in expected_by_name
    ]
    rows.sort(
        key=lambda row: (
            str(row["identity"]["stream"]),  # type: ignore[index]
            str(row["identity"]["durable"]),  # type: ignore[index]
        )
    )
    blocked = any(row["status"] == "BLOCKED" for row in rows)
    return rows, blocked


def build_unexpected_durable_rows(
    consumers: Sequence[ConsumerState],
    expected_by_name: dict[str, ExpectedDurable],
) -> list[dict[str, object]]:
    """Expose every unowned durable instead of filtering it out of evidence."""

    rows = [
        {
            "identity": {
                "stream": state.stream,
                "durable": state.durable,
            },
            "created": state.created,
            "window": {"current_max_ack_pending": state.max_ack_pending},
            "outstanding": {"num_ack_pending": state.num_ack_pending},
        }
        for state in consumers
        if state.durable not in expected_by_name
    ]
    rows.sort(
        key=lambda row: (
            str(row["identity"]["stream"]),  # type: ignore[index]
            str(row["identity"]["durable"]),  # type: ignore[index]
        )
    )
    return rows


def _indexed_rows(
    rows: object,
    *,
    kind: str,
) -> dict[tuple[str, ...], dict[str, object]]:
    if not isinstance(rows, list):
        raise RuntimeError("nats_cutover_malformed_previous_preflight")
    result: dict[tuple[str, ...], dict[str, object]] = {}
    identity_fields = ("name",) if kind == "stream" else (
        "stream",
        "durable",
        "role",
        "topic",
    )
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("identity"), dict):
            raise RuntimeError("nats_cutover_malformed_previous_preflight")
        identity = row["identity"]
        key = tuple(_required_name(identity.get(field)) for field in identity_fields)
        if key in result:
            raise RuntimeError("nats_cutover_duplicate_previous_identity")
        result[key] = row
    return result


def _required_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("nats_cutover_malformed_previous_preflight")
    return value


def _row_integer(mapping: dict[str, object], field: str) -> int:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("nats_cutover_malformed_previous_preflight")
    return value


def evaluate_cutover_continuity(
    *,
    previous_streams: object,
    current_streams: Sequence[dict[str, object]],
    previous_durables: object,
    current_durables: Sequence[dict[str, object]],
    baseline_sha256: str,
) -> dict[str, object]:
    previous_stream_index = _indexed_rows(previous_streams, kind="stream")
    current_stream_index = _indexed_rows(list(current_streams), kind="stream")
    previous_durable_index = _indexed_rows(previous_durables, kind="durable")
    current_durable_index = _indexed_rows(list(current_durables), kind="durable")
    violations: list[str] = []

    if set(previous_stream_index) != set(current_stream_index):
        violations.append("critical_stream_identity_set_changed")
    if set(previous_durable_index) != set(current_durable_index):
        violations.append("critical_durable_identity_set_changed")

    for key in sorted(set(previous_stream_index) & set(current_stream_index)):
        previous = previous_stream_index[key]
        current = current_stream_index[key]
        name = key[0]
        if previous.get("created") != current.get("created"):
            violations.append(f"stream_created_changed:{name}")
        if previous.get("immutable_config") != current.get("immutable_config"):
            violations.append(f"stream_config_changed:{name}")
        previous_state = _required_mapping(previous.get("state"))
        current_state = _required_mapping(current.get("state"))
        # All application publishers/consumers are stopped across this window.
        # Any broker state change is therefore an unowned write, ACK, purge, or
        # retention mutation and must invalidate the cutover, not merely avoid
        # rollback. Exact equality also catches purge-then-repopulate attacks.
        for field in (
            "messages",
            "bytes",
            "first_seq",
            "last_seq",
            "consumer_count",
        ):
            if _row_integer(current_state, field) != _row_integer(
                previous_state,
                field,
            ):
                violations.append(f"stream_state_changed:{name}:{field}")
        if current_state.get("deleted") != previous_state.get("deleted"):
            violations.append(f"stream_deleted_state_changed:{name}")
        if _row_integer(current_state, "num_deleted") != _row_integer(
            previous_state,
            "num_deleted",
        ):
            violations.append(f"stream_deleted_state_changed:{name}:num_deleted")

    cursor_fields = (
        "delivered_stream_seq",
        "delivered_consumer_seq",
        "ack_floor_stream_seq",
        "ack_floor_consumer_seq",
    )
    for key in sorted(set(previous_durable_index) & set(current_durable_index)):
        previous = previous_durable_index[key]
        current = current_durable_index[key]
        identity = f"{key[0]}/{key[1]}"
        if previous.get("created") != current.get("created"):
            violations.append(f"durable_created_changed:{identity}")
        if previous.get("immutable_config") != current.get("immutable_config"):
            violations.append(f"durable_config_changed:{identity}")
        if previous.get("window") != current.get("window"):
            violations.append(f"durable_window_changed:{identity}")
        previous_cursor = _required_mapping(previous.get("cursor"))
        current_cursor = _required_mapping(current.get("cursor"))
        for field in cursor_fields:
            if _row_integer(current_cursor, field) != _row_integer(
                previous_cursor,
                field,
            ):
                violations.append(f"durable_cursor_changed:{identity}:{field}")
        previous_outstanding = _required_mapping(previous.get("outstanding"))
        current_outstanding = _required_mapping(current.get("outstanding"))
        if _row_integer(current_outstanding, "num_ack_pending") != _row_integer(
            previous_outstanding,
            "num_ack_pending",
        ):
            violations.append(f"durable_outstanding_changed:{identity}")

    return {
        "status": "PASSED" if not violations else "INVALIDATED",
        "complete": True,
        "baseline_sha256": baseline_sha256,
        "streams_checked": len(previous_stream_index),
        "durables_checked": len(previous_durable_index),
        "violations": violations,
    }


def evaluate_active_runtime_continuity(
    *,
    previous_streams: object,
    current_streams: Sequence[dict[str, object]],
    previous_durables: object,
    current_durables: Sequence[dict[str, object]],
    allow_new_identities: bool = False,
) -> dict[str, object]:
    """Validate monotonic JetStream continuity while applications are active.

    Publishers, consumers, retention and INTEREST deletion can legitimately
    change counts, outstanding work and first sequence.  Identity, creation
    time and immutable configuration must remain exact; stream last sequence
    and every durable cursor may only advance.  JetStream does not expose purge
    provenance, so purge-vs-retention remains an explicit trust boundary while
    ``deny_purge`` is false.
    """

    previous_stream_index = _indexed_rows(previous_streams, kind="stream")
    current_stream_index = _indexed_rows(list(current_streams), kind="stream")
    previous_durable_index = _indexed_rows(previous_durables, kind="durable")
    current_durable_index = _indexed_rows(list(current_durables), kind="durable")
    violations: list[str] = []
    if (
        not set(previous_stream_index).issubset(current_stream_index)
        or (
            not allow_new_identities
            and set(previous_stream_index) != set(current_stream_index)
        )
    ):
        violations.append("critical_stream_identity_set_changed")
    if (
        not set(previous_durable_index).issubset(current_durable_index)
        or (
            not allow_new_identities
            and set(previous_durable_index) != set(current_durable_index)
        )
    ):
        violations.append("critical_durable_identity_set_changed")

    for key in sorted(set(previous_stream_index) & set(current_stream_index)):
        previous = previous_stream_index[key]
        current = current_stream_index[key]
        name = key[0]
        if previous.get("created") != current.get("created"):
            violations.append(f"stream_created_changed:{name}")
        if previous.get("immutable_config") != current.get("immutable_config"):
            violations.append(f"stream_config_changed:{name}")
        previous_state = _required_mapping(previous.get("state"))
        current_state = _required_mapping(current.get("state"))
        if _row_integer(current_state, "last_seq") < _row_integer(
            previous_state,
            "last_seq",
        ):
            violations.append(f"stream_last_seq_regressed:{name}")

    cursor_fields = (
        "delivered_stream_seq",
        "delivered_consumer_seq",
        "ack_floor_stream_seq",
        "ack_floor_consumer_seq",
    )
    for key in sorted(set(previous_durable_index) & set(current_durable_index)):
        previous = previous_durable_index[key]
        current = current_durable_index[key]
        identity = f"{key[0]}/{key[1]}"
        if previous.get("created") != current.get("created"):
            violations.append(f"durable_created_changed:{identity}")
        if previous.get("immutable_config") != current.get("immutable_config"):
            violations.append(f"durable_config_changed:{identity}")
        if previous.get("window") != current.get("window"):
            violations.append(f"durable_window_changed:{identity}")
        previous_cursor = _required_mapping(previous.get("cursor"))
        current_cursor = _required_mapping(current.get("cursor"))
        for field in cursor_fields:
            if _row_integer(current_cursor, field) < _row_integer(
                previous_cursor,
                field,
            ):
                violations.append(f"durable_cursor_regressed:{identity}:{field}")

    return {
        "status": "PASSED_WITH_TRUST_BOUNDARY" if not violations else "INVALIDATED",
        "complete": False,
        "streams_checked": len(current_stream_index),
        "durables_checked": len(current_durable_index),
        "new_identities_allowed": allow_new_identities,
        "purge_exclusion_verified": False,
        "trust_boundary": "purge_vs_legitimate_retention_not_distinguishable",
        "violations": violations,
    }


def load_previous_preflight(
    path: Path,
    *,
    generation: str,
    deployment_lock_id: str,
    deployed_commit: str,
) -> tuple[dict[str, object], dict[str, object]]:
    allowed_root = _EVIDENCE_DIR.resolve()
    candidate = path if path.is_absolute() else Path.cwd() / path
    if candidate.is_symlink():
        raise RuntimeError("nats_cutover_previous_preflight_symlink_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(allowed_root)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise RuntimeError("nats_cutover_invalid_previous_preflight_path") from exc
    raw = resolved.read_bytes()
    if len(raw) > _MAX_PREVIOUS_EVIDENCE_BYTES:
        raise RuntimeError("nats_cutover_previous_preflight_too_large")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("nats_cutover_malformed_previous_preflight") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("nats_cutover_malformed_previous_preflight")
    expected = {
        "schema_version": _SCHEMA_VERSION,
        "stage": _STAGE_PRE_FULL_DOWN,
        "generation": generation,
        "deployment_lock_id": deployment_lock_id,
        "deployed_commit": deployed_commit,
        "operation": "READ_ONLY",
        "status": "PASSED_WITH_TRUST_BOUNDARY",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("nats_cutover_previous_preflight_binding_mismatch")
    if payload.get("mutations_performed") != []:
        raise RuntimeError("nats_cutover_previous_preflight_not_read_only")
    nats_bootstrap = _required_nats_bootstrap(payload.get("nats_bootstrap"))
    nats_query_fingerprint = payload.get("nats_query_fingerprint")
    if (
        not isinstance(nats_query_fingerprint, str)
        or not _SHA256_RE.fullmatch(nats_query_fingerprint)
        or nats_query_fingerprint != nats_bootstrap["baseline_fingerprint"]
    ):
        raise RuntimeError("nats_cutover_malformed_bootstrap_provenance")
    target_manifest = payload.get("target_stream_manifest")
    target_compliance = payload.get("stream_target_compliance")
    if (
        not isinstance(target_manifest, dict)
        or not isinstance(target_manifest.get("streams"), list)
        or not isinstance(target_manifest.get("sha256"), str)
        or not _SHA256_RE.fullmatch(str(target_manifest.get("sha256")))
        or not isinstance(target_compliance, dict)
        or target_compliance.get("status")
        not in {"MATCHED", "PROVISIONING_REQUIRED"}
        or target_compliance.get("target_sha256") != target_manifest.get("sha256")
        or target_compliance.get("unexpected_names") != []
        or target_compliance.get("drift") != []
    ):
        raise RuntimeError("nats_cutover_malformed_target_stream_manifest")
    query = _required_mapping(payload.get("query"))
    quiescence = _required_mapping(payload.get("app_quiescence"))
    continuity = _required_mapping(payload.get("continuity"))
    if (
        query.get("complete") is not True
        or quiescence.get("status") != "PASSED_WITH_TRUST_BOUNDARY"
        or quiescence.get("complete") is not False
        or quiescence.get("coverage_status") != "BOUNDED_OBSERVED"
        or quiescence.get("fingerprint_match") is not True
        or quiescence.get("lifecycle_events") != []
        or quiescence.get("allowlist") != list(_KNOWN_APP_CONTAINERS)
        or quiescence.get("lifecycle_event_allowlist")
        != [*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER]
        or not isinstance(quiescence.get("before"), list)
        or quiescence.get("before") != quiescence.get("after")
        or payload.get("unexpected_durables") != []
        or continuity.get("status") != "BASELINE_CAPTURED"
        or continuity.get("complete") is not True
        or continuity.get("violations") != []
        or payload.get("previous_preflight") is not None
    ):
        raise RuntimeError("nats_cutover_previous_preflight_not_passed")
    checked_at = payload.get("checked_at_utc")
    parsed_checked_at = _parse_evidence_timestamp(checked_at)
    window_started_at = quiescence.get("window_started_at_utc")
    window_ended_at = quiescence.get("window_ended_at_utc")
    window_started_ns = quiescence.get("window_started_ns")
    window_ended_ns = quiescence.get("window_ended_ns")
    if (
        isinstance(window_started_ns, bool)
        or not isinstance(window_started_ns, int)
        or isinstance(window_ended_ns, bool)
        or not isinstance(window_ended_ns, int)
        or not (0 < window_started_ns <= window_ended_ns)
    ):
        raise RuntimeError("nats_cutover_invalid_evidence_time_window")
    parsed_window_started_at = _parse_evidence_timestamp(window_started_at)
    parsed_window_ended_at = _parse_evidence_timestamp(window_ended_at)
    if not (
        parsed_checked_at <= parsed_window_started_at <= parsed_window_ended_at
    ):
        raise RuntimeError("nats_cutover_invalid_evidence_time_window")
    if (
        abs(
            int(parsed_window_started_at.timestamp() * 1_000_000_000)
            - window_started_ns
        )
        > 1_000
        or abs(
            int(parsed_window_ended_at.timestamp() * 1_000_000_000)
            - window_ended_ns
        )
        > 1_000
    ):
        raise RuntimeError("nats_cutover_invalid_evidence_time_window")
    validate_live_window_evidence(
        quiescence.get("event_capture"),
        expected_allowlist=(*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER),
        expected_start_ns=window_started_ns,
        expected_cutoff_ns=window_ended_ns,
    )
    sha256 = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    try:
        relative_path = resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        relative_path = resolved.as_posix()
    reference = {
        "path": relative_path,
        "sha256": sha256,
        "schema_version": _SCHEMA_VERSION,
        "stage": _STAGE_PRE_FULL_DOWN,
        "generation": generation,
        "deployment_lock_id": deployment_lock_id,
        "deployed_commit": deployed_commit,
        "checked_at_utc": checked_at,
        "window_started_at_utc": window_started_at,
        "window_ended_at_utc": window_ended_at,
        "window_started_ns": window_started_ns,
        "window_ended_ns": window_ended_ns,
        "nats_bootstrap": nats_bootstrap,
        "nats_query_fingerprint": nats_query_fingerprint,
        "target_stream_manifest_sha256": target_manifest["sha256"],
    }
    return payload, reference


def validate_preflight_window_order(
    *,
    previous_reference: dict[str, object],
    current_window_started_ns: int,
) -> None:
    """Order two preflight stages using their authoritative nanosecond bounds."""

    previous_window_started_ns = previous_reference.get("window_started_ns")
    previous_window_ended_ns = previous_reference.get("window_ended_ns")
    values = (
        previous_window_started_ns,
        previous_window_ended_ns,
        current_window_started_ns,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise RuntimeError("nats_cutover_invalid_evidence_time_window")
    if not (
        0 < previous_window_started_ns <= previous_window_ended_ns
        and current_window_started_ns > 0
    ):
        raise RuntimeError("nats_cutover_invalid_evidence_time_window")
    if current_window_started_ns < previous_window_started_ns:
        raise RuntimeError("nats_cutover_preflight_time_rollback")
    if current_window_started_ns < previous_window_ended_ns:
        raise RuntimeError("nats_cutover_preflight_time_window_overlap")


async def _fetch_all_pages(
    fetch: Callable[[int], Awaitable[Sequence[T]]],
    *,
    identity: Callable[[T], str],
) -> list[T]:
    """Fetch an offset-paginated NATS list completely and fail on stalls."""

    result: list[T] = []
    seen: set[str] = set()
    offset = 0
    while True:
        page = list(await fetch(offset))
        if not page:
            return result
        for item in page:
            item_identity = identity(item)
            if not item_identity or item_identity in seen:
                raise RuntimeError("nats_cutover_preflight_pagination_stalled")
            seen.add(item_identity)
            result.append(item)
        offset += len(page)
        if offset > _MAX_PAGINATED_ITEMS:
            raise RuntimeError("nats_cutover_preflight_pagination_limit")


def _policy_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).lower()


def _sequence_value(container: object, field: str) -> int:
    if container is None or not hasattr(container, field):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(container, field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _integer_value(
    container: object,
    field: str,
    *,
    non_negative: bool,
) -> int:
    if container is None or not hasattr(container, field):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(container, field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    if non_negative and value < 0:
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _required_name(value: object) -> str:
    """Return an exact broker identity or fail closed on malformed metadata."""

    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _broker_created_text(value: object) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RuntimeError("nats_cutover_malformed_created_timestamp")
    return _utc_text(value)


def _raw_broker_created_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("nats_cutover_malformed_created_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("nats_cutover_malformed_created_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("nats_cutover_malformed_created_timestamp")
    return _utc_text(parsed)


def _duration_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("nats_cutover_malformed_stream_state")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError("nats_cutover_malformed_stream_state") from exc
    if not math.isfinite(result) or result < 0:
        raise RuntimeError("nats_cutover_malformed_stream_state")
    if result > 1e10:
        result /= 1e9
    if not math.isfinite(result):
        raise RuntimeError("nats_cutover_malformed_stream_state")
    return result


def _stream_state(info: object, *, created: str) -> CriticalStreamState:
    config = getattr(info, "config", None)
    state = getattr(info, "state", None)
    if config is None or state is None:
        raise RuntimeError("nats_cutover_malformed_stream_state")
    subjects = getattr(config, "subjects", None)
    if not isinstance(subjects, (list, tuple)) or not subjects or not all(
        isinstance(subject, str) and subject for subject in subjects
    ):
        raise RuntimeError("nats_cutover_malformed_stream_state")
    deny_purge = getattr(config, "deny_purge", None)
    if not isinstance(deny_purge, bool):
        raise RuntimeError("nats_cutover_malformed_stream_state")
    deleted_raw = getattr(state, "deleted", None)
    if deleted_raw is None:
        deleted: tuple[int, ...] = ()
    elif isinstance(deleted_raw, list) and all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in deleted_raw
    ):
        deleted = tuple(sorted(deleted_raw))
    else:
        raise RuntimeError("nats_cutover_malformed_stream_state")
    num_deleted_raw = getattr(state, "num_deleted", None)
    if num_deleted_raw is None:
        num_deleted = len(deleted)
    elif (
        isinstance(num_deleted_raw, bool)
        or not isinstance(num_deleted_raw, int)
        or num_deleted_raw < 0
    ):
        raise RuntimeError("nats_cutover_malformed_stream_state")
    else:
        num_deleted = num_deleted_raw
    lost = getattr(state, "lost", None)
    if lost is not None:
        lost_messages = getattr(lost, "msgs", None)
        lost_bytes = getattr(lost, "bytes", None)
        if lost_messages or (isinstance(lost_bytes, int) and lost_bytes > 0):
            raise RuntimeError("nats_cutover_stream_reports_lost_data")
    return CriticalStreamState(
        name=_required_name(getattr(config, "name", None)),
        created=created,
        subjects=tuple(sorted(subjects)),
        retention=_policy_text(getattr(config, "retention", None)),
        storage=_policy_text(getattr(config, "storage", None)),
        discard=_policy_text(getattr(config, "discard", None)),
        max_age_seconds=_duration_seconds(getattr(config, "max_age", None)),
        max_bytes=_integer_value(config, "max_bytes", non_negative=False),
        max_msgs=_integer_value(config, "max_msgs", non_negative=False),
        max_msg_size=_integer_value(config, "max_msg_size", non_negative=False),
        num_replicas=_integer_value(config, "num_replicas", non_negative=True),
        duplicate_window_seconds=_duration_seconds(
            getattr(config, "duplicate_window", None)
        ),
        deny_purge=deny_purge,
        messages=_integer_value(state, "messages", non_negative=True),
        bytes=_integer_value(state, "bytes", non_negative=True),
        first_seq=_integer_value(state, "first_seq", non_negative=True),
        last_seq=_integer_value(state, "last_seq", non_negative=True),
        consumer_count=_integer_value(state, "consumer_count", non_negative=True),
        deleted=deleted,
        num_deleted=num_deleted,
    )


def _stream_spec_config(spec: object) -> dict[str, object]:
    return {
        "subjects": [f"aats.{topic}" for topic in sorted(spec.topics)],
        "retention": spec.retention,
        "storage": spec.storage,
        "discard": spec.discard,
        "max_age_seconds": float(spec.max_age_seconds),
        "max_bytes": spec.max_bytes,
        "max_msgs": spec.max_msgs,
        "max_msg_size": spec.max_msg_size,
        "num_replicas": spec.num_replicas,
        "duplicate_window_seconds": float(spec.duplicate_window_seconds),
        "deny_purge": spec.deny_purge,
    }


def load_target_stream_manifest(env_path: Path) -> dict[str, object]:
    """Resolve only the eight non-secret NATS capacity overrides from a profile.

    The profile also contains credentials.  This parser deliberately never
    stores, logs, serializes, or returns any non-allowlisted value.
    """

    from aats.bus.nats_bus import DEFAULT_STREAM_SPECS

    resolved = env_path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError("nats_cutover_target_env_not_regular_file")
    overrides_by_stream: dict[str, dict[str, int | float]] = {}
    seen: set[str] = set()
    with resolved.open("r", encoding="utf-8", errors="strict") as handle:
        for line in handle:
            candidate = line.lstrip()
            if candidate.startswith("export "):
                candidate = candidate[7:].lstrip()
            key, separator, raw_value = candidate.partition("=")
            key = key.strip()
            if separator != "=" or key not in _TARGET_ENV_FIELDS:
                continue
            if key in seen:
                raise RuntimeError("nats_cutover_duplicate_target_env_override")
            seen.add(key)
            value_text = raw_value.strip()
            if (
                len(value_text) >= 2
                and value_text[0] == value_text[-1]
                and value_text[0] in {'"', "'"}
            ):
                value_text = value_text[1:-1]
            stream_name, field, converter = _TARGET_ENV_FIELDS[key]
            try:
                value = converter(value_text)
            except (OverflowError, ValueError) as exc:
                raise RuntimeError("nats_cutover_invalid_target_env_override") from exc
            if (
                isinstance(value, bool)
                or (isinstance(value, float) and not math.isfinite(value))
                or value <= 0
            ):
                raise RuntimeError("nats_cutover_invalid_target_env_override")
            overrides_by_stream.setdefault(stream_name, {})[field] = value

    specs = tuple(
        replace(spec, **overrides_by_stream.get(spec.name, {}))
        for spec in DEFAULT_STREAM_SPECS
    )
    streams = [
        {
            "identity": {"name": spec.name},
            "immutable_config": _stream_spec_config(spec),
        }
        for spec in sorted(specs, key=lambda item: item.name)
    ]
    canonical = json.dumps(streams, sort_keys=True, separators=(",", ":"))
    return {
        "source": "profile_env_allowlist",
        "streams": streams,
        "sha256": f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
    }


def _default_target_stream_manifest() -> dict[str, object]:
    from aats.bus.nats_bus import DEFAULT_STREAM_SPECS

    streams = [
        {
            "identity": {"name": spec.name},
            "immutable_config": _stream_spec_config(spec),
        }
        for spec in sorted(DEFAULT_STREAM_SPECS, key=lambda item: item.name)
    ]
    canonical = json.dumps(streams, sort_keys=True, separators=(",", ":"))
    return {
        "source": "code_defaults",
        "streams": streams,
        "sha256": f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
    }


def build_critical_stream_rows(
    streams: Sequence[CriticalStreamState],
) -> list[dict[str, object]]:
    rows = [
        {
            "identity": {"name": state.name},
            "created": state.created,
            "immutable_config": {
                "subjects": list(state.subjects),
                "retention": state.retention,
                "storage": state.storage,
                "discard": state.discard,
                "max_age_seconds": state.max_age_seconds,
                "max_bytes": state.max_bytes,
                "max_msgs": state.max_msgs,
                "max_msg_size": state.max_msg_size,
                "num_replicas": state.num_replicas,
                "duplicate_window_seconds": state.duplicate_window_seconds,
                "deny_purge": state.deny_purge,
            },
            "state": {
                "messages": state.messages,
                "bytes": state.bytes,
                "first_seq": state.first_seq,
                "last_seq": state.last_seq,
                "consumer_count": state.consumer_count,
                "deleted": list(state.deleted),
                "num_deleted": state.num_deleted,
            },
        }
        for state in streams
    ]
    rows.sort(key=lambda row: str(row["identity"]["name"]))  # type: ignore[index]
    return rows


def evaluate_stream_target(
    *,
    actual_streams: Sequence[dict[str, object]],
    target_manifest: dict[str, object],
    bootstrap_mode: str,
    require_fresh_empty: bool = True,
) -> tuple[dict[str, object], bool]:
    target_rows = target_manifest.get("streams")
    if not isinstance(target_rows, list):
        raise RuntimeError("nats_cutover_malformed_target_stream_manifest")
    target_index = _indexed_rows(target_rows, kind="stream")
    actual_index = _indexed_rows(list(actual_streams), kind="stream")
    expected_names = {key[0] for key in target_index}
    actual_names = {key[0] for key in actual_index}
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    drift: list[dict[str, object]] = []
    for key in sorted(set(target_index) & set(actual_index)):
        expected_config = target_index[key].get("immutable_config")
        actual_config = actual_index[key].get("immutable_config")
        if expected_config != actual_config:
            expected_mapping = _required_mapping(expected_config)
            actual_mapping = _required_mapping(actual_config)
            fields = sorted(
                field
                for field in set(expected_mapping) | set(actual_mapping)
                if expected_mapping.get(field) != actual_mapping.get(field)
            )
            drift.append({"name": key[0], "fields": fields})

    # Missing target streams contain no legacy state and may be provisioned by
    # the exact target code.  Existing wrong or unexpected streams are never
    # silently updated/ignored across a cutover because they can change routing
    # or conceal broker state outside the evidence set.
    blocked = bool(unexpected or drift)
    # Before application startup, a volume proven fresh by the deployment
    # bootstrap must still be empty; any stream at that point means the
    # freshness proof was invalidated.  The final post-start qualification
    # deliberately disables this phase-specific rule because the application
    # is then expected to have provisioned the exact target streams.
    if require_fresh_empty and bootstrap_mode == "proven_fresh_install" and actual_names:
        blocked = True
    status = "BLOCKED" if blocked else (
        "PROVISIONING_REQUIRED" if missing else "MATCHED"
    )
    return (
        {
            "status": status,
            "target_sha256": target_manifest.get("sha256"),
            "expected_names": sorted(expected_names),
            "actual_names": sorted(actual_names),
            "missing_names": missing,
            "unexpected_names": unexpected,
            "drift": drift,
        },
        blocked,
    )


def _consumer_state(stream: str, info: object) -> ConsumerState:
    config = getattr(info, "config")
    created_text = _broker_created_text(getattr(info, "created", None))
    delivered = getattr(info, "delivered")
    ack_floor = getattr(info, "ack_floor")
    return ConsumerState(
        stream=_required_name(stream),
        durable=_required_name(getattr(info, "name")),
        created=created_text,
        deliver_policy=_policy_text(getattr(config, "deliver_policy")),
        ack_policy=_policy_text(getattr(config, "ack_policy")),
        filter_subject=getattr(config, "filter_subject", None),
        filter_subjects=tuple(getattr(config, "filter_subjects", None) or ()),
        deliver_group=getattr(config, "deliver_group", None),
        # Missing/None/malformed broker counters are UNKNOWN, never a proven
        # zero-drain state. max_ack_pending may legitimately be <=0 to denote
        # an unlimited/server-default window, so retain its signed integer.
        max_ack_pending=_integer_value(
            config,
            "max_ack_pending",
            non_negative=False,
        ),
        num_ack_pending=_integer_value(
            info,
            "num_ack_pending",
            non_negative=True,
        ),
        cursor=ConsumerCursor(
            delivered_stream_seq=_sequence_value(delivered, "stream_seq"),
            delivered_consumer_seq=_sequence_value(delivered, "consumer_seq"),
            ack_floor_stream_seq=_sequence_value(ack_floor, "stream_seq"),
            ack_floor_consumer_seq=_sequence_value(ack_floor, "consumer_seq"),
        ),
    )


async def query_consumer_states_from_js(js: Any) -> QueryResult:
    """Read every stream consumer through paginated management queries only."""

    streams = await _fetch_all_pages(
        lambda offset: js.streams_info(offset=offset),
        identity=lambda info: _required_name(info.config.name),
    )
    prefix = _required_name(getattr(js, "_prefix", None))
    timeout = getattr(js, "_timeout", None)
    api_request = getattr(js, "_api_request", None)
    if not callable(api_request):
        raise RuntimeError("nats_cutover_raw_stream_info_unavailable")
    stream_states_list: list[CriticalStreamState] = []
    for info in streams:
        stream_name = _required_name(info.config.name)
        raw_info = await api_request(
            f"{prefix}.STREAM.INFO.{stream_name}",
            b"",
            timeout=timeout,
        )
        if not isinstance(raw_info, dict):
            raise RuntimeError("nats_cutover_malformed_stream_state")
        stream_states_list.append(
            _stream_state(
                info,
                created=_raw_broker_created_text(raw_info.get("created")),
            )
        )
    stream_states = tuple(stream_states_list)
    consumers: list[ConsumerState] = []
    consumer_count = 0
    for stream_info in streams:
        stream_name = _required_name(stream_info.config.name)
        page = await _fetch_all_pages(
            lambda offset, name=stream_name: js.consumers_info(
                name,
                offset=offset,
            ),
            identity=lambda info: _required_name(info.name),
        )
        consumer_count += len(page)
        consumers.extend(_consumer_state(stream_name, info) for info in page)
    return QueryResult(
        stream_count=len(streams),
        consumer_count=consumer_count,
        consumers=tuple(consumers),
        streams=stream_states,
    )


async def query_loopback_nats() -> QueryResult:
    """Connect to the fixed WSL2 loopback endpoint and perform read-only queries."""

    import nats

    client = await nats.connect(
        servers=[_NATS_LOOPBACK_SERVER],
        connect_timeout=5.0,
        max_reconnect_attempts=0,
    )
    try:
        return await query_consumer_states_from_js(client.jetstream(timeout=5.0))
    finally:
        await client.close()


def build_evidence(
    *,
    generation: str,
    deployment_lock_id: str,
    deployed_commit: str,
    checked_at: datetime,
    query_result: QueryResult | None,
    rows: Sequence[dict[str, object]],
    status: str,
    app_quiescence: dict[str, object] | None,
    nats_bootstrap: dict[str, str] | None = None,
    nats_query_fingerprint: str | None = None,
    stage: str = _STAGE_PRE_FULL_DOWN,
    critical_streams: Sequence[dict[str, object]] = (),
    unexpected_durables: Sequence[dict[str, object]] = (),
    target_stream_manifest: dict[str, object] | None = None,
    stream_target_compliance: dict[str, object] | None = None,
    previous_preflight: dict[str, object] | None = None,
    continuity: dict[str, object] | None = None,
    error_code: str | None = None,
) -> dict[str, object]:
    if target_stream_manifest is None:
        target_stream_manifest = _default_target_stream_manifest()
    if stream_target_compliance is None and nats_bootstrap is not None:
        bootstrap_mode = nats_bootstrap.get("mode")
        if isinstance(bootstrap_mode, str):
            stream_target_compliance, target_blocked = evaluate_stream_target(
                actual_streams=critical_streams,
                target_manifest=target_stream_manifest,
                bootstrap_mode=bootstrap_mode,
            )
            if target_blocked and status in {
                "PASSED",
                "PASSED_WITH_TRUST_BOUNDARY",
            }:
                status = "BLOCKED"
    if unexpected_durables and status in {
        "PASSED",
        "PASSED_WITH_TRUST_BOUNDARY",
    }:
        status = "BLOCKED"
    evidence: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "stage": stage,
        "generation": generation,
        "deployment_lock_id": deployment_lock_id,
        "deployed_commit": deployed_commit,
        "checked_at_utc": _utc_text(checked_at),
        "operation": "READ_ONLY",
        "mutations_performed": [],
        "status": status,
        "query": {
            "complete": query_result is not None,
            "streams_scanned": (
                query_result.stream_count if query_result is not None else 0
            ),
            "consumers_scanned": (
                query_result.consumer_count if query_result is not None else 0
            ),
            "critical_all_durables_found": len(rows),
            "critical_streams_found": len(critical_streams),
        },
        "critical_streams": list(critical_streams),
        "target_stream_manifest": target_stream_manifest,
        "stream_target_compliance": stream_target_compliance,
        "durables": list(rows),
        "unexpected_durables": list(unexpected_durables),
        "app_quiescence": app_quiescence,
        "nats_bootstrap": nats_bootstrap,
        "nats_query_fingerprint": nats_query_fingerprint,
        "previous_preflight": previous_preflight,
        "continuity": continuity,
    }
    if status == "BLOCKED":
        evidence["recovery"] = {
            "instruction_code": (
                "nats_durable_cutover_requires_approved_legacy_drain_or_release_review"
            ),
            "instruction": (
                "Keep NATS and persistent state online. For outstanding-only "
                "blockers, use the matching legacy consumer under an approved "
                "change window to drain naturally to zero, then rerun this "
                "preflight. Immutable drift requires human release review. "
                "Never auto-ack, delete, update, recreate, reset, or purge."
            ),
        }
    if error_code is not None:
        evidence["error_code"] = error_code
    return evidence


def write_evidence(evidence: dict[str, object]) -> Path:
    """Atomically create one immutable-named evidence packet."""

    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = _EVIDENCE_DIR / (
        f"nats_durable_cutover_preflight_{stamp}_{uuid4().hex[:12]}.json"
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=_EVIDENCE_DIR,
            prefix=".nats_durable_cutover_preflight_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(evidence, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        _fsync_directory(_EVIDENCE_DIR)
    except Exception:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise
    return target


def _fsync_directory(path: Path) -> None:
    """Persist a newly linked evidence name on the Linux deployment host."""

    if os.name == "nt":
        # Windows does not expose portable directory descriptors.  Production
        # evidence generation runs inside the repository's WSL2 environment.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only critical durable ACK-window cutover preflight"
    )
    parser.add_argument(
        "--generation",
        required=True,
        type=_validated_generation,
        help="Non-secret runtime readiness generation recorded in evidence",
    )
    parser.add_argument(
        "--deployment-lock-id",
        required=True,
        type=_validated_generation,
        help="Non-secret ID of the long-lived WSL deployment lock holder",
    )
    parser.add_argument(
        "--deployed-commit",
        required=True,
        type=_validated_commit,
        help="Exact 40-hex commit being deployed",
    )
    parser.add_argument(
        "--target-env-file",
        required=True,
        type=Path,
        help=(
            "Profile env file; only the eight allowlisted non-secret NATS "
            "capacity overrides are read"
        ),
    )
    parser.add_argument("--stage", choices=_STAGES, required=True)
    parser.add_argument("--previous-preflight", type=Path)
    parser.add_argument(
        "--nats-bootstrap-mode",
        choices=_NATS_BOOTSTRAP_MODES,
    )
    parser.add_argument(
        "--nats-baseline-fingerprint",
        type=_validated_sha256,
    )
    parser.add_argument(
        "--nats-volume-fingerprint",
        type=_validated_sha256,
    )
    args = parser.parse_args(argv)
    if (
        args.stage == _STAGE_PRE_FULL_DOWN
        and args.previous_preflight is not None
    ):
        parser.error("pre_full_down_forbids_previous_preflight")
    if (
        args.stage == _STAGE_POST_INFRA_PRE_APP_UP
        and args.previous_preflight is None
    ):
        parser.error("post_infra_pre_app_up_requires_previous_preflight")
    bootstrap_values = (
        args.nats_bootstrap_mode,
        args.nats_baseline_fingerprint,
        args.nats_volume_fingerprint,
    )
    if args.stage == _STAGE_PRE_FULL_DOWN and any(
        value is None for value in bootstrap_values
    ):
        parser.error("pre_full_down_requires_nats_bootstrap_provenance")
    if args.stage == _STAGE_POST_INFRA_PRE_APP_UP and any(
        value is not None for value in bootstrap_values
    ):
        parser.error("post_infra_pre_app_up_forbids_new_bootstrap_provenance")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    checked_at = datetime.now(timezone.utc)
    app_quiescence: dict[str, object] | None = None
    critical_streams: list[dict[str, object]] = []
    previous_payload: dict[str, object] | None = None
    previous_reference: dict[str, object] | None = None
    continuity: dict[str, object] | None = None
    nats_bootstrap: dict[str, str] | None = None
    nats_query_fingerprint: str | None = None
    target_stream_manifest: dict[str, object] | None = None
    stream_target_compliance: dict[str, object] | None = None
    try:
        target_stream_manifest = load_target_stream_manifest(args.target_env_file)
        if args.stage == _STAGE_POST_INFRA_PRE_APP_UP:
            previous_payload, previous_reference = load_previous_preflight(
                args.previous_preflight,
                generation=args.generation,
                deployment_lock_id=args.deployment_lock_id,
                deployed_commit=args.deployed_commit,
            )
            nats_bootstrap = _required_nats_bootstrap(
                previous_payload.get("nats_bootstrap")
            )
            previous_target = previous_payload.get("target_stream_manifest")
            if not isinstance(previous_target, dict) or (
                previous_target.get("sha256")
                != target_stream_manifest.get("sha256")
                or previous_target.get("streams")
                != target_stream_manifest.get("streams")
            ):
                raise RuntimeError("nats_cutover_target_stream_manifest_changed")
        else:
            nats_bootstrap = _required_nats_bootstrap(
                {
                    "mode": args.nats_bootstrap_mode,
                    "baseline_fingerprint": args.nats_baseline_fingerprint,
                    "volume_fingerprint": args.nats_volume_fingerprint,
                }
            )
        with LiveDockerEventMonitor(
            (*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER),
            max_runtime_seconds=90.0,
        ) as event_monitor:
            window_started_ns = event_monitor.start()
            if previous_reference is not None:
                validate_preflight_window_order(
                    previous_reference=previous_reference,
                    current_window_started_ns=window_started_ns,
                )
            before = capture_app_quiescence()
            nats_identity_before = capture_nats_identity()
            nats_fingerprint_before = str(nats_identity_before["fingerprint"])
            nats_volume_fingerprint_before = capture_nats_volume_fingerprint()
            if (
                args.stage == _STAGE_PRE_FULL_DOWN
                and nats_fingerprint_before
                != nats_bootstrap["baseline_fingerprint"]
            ):
                raise RuntimeError("nats_cutover_bootstrap_fingerprint_mismatch")
            if (
                nats_volume_fingerprint_before
                != nats_bootstrap["volume_fingerprint"]
            ):
                raise RuntimeError("nats_cutover_volume_fingerprint_mismatch")
            if args.stage == _STAGE_POST_INFRA_PRE_APP_UP:
                if nats_fingerprint_before == nats_bootstrap["baseline_fingerprint"]:
                    raise RuntimeError("nats_cutover_post_container_not_recreated")
                if nats_identity_before["restart_count"] != 0:
                    raise RuntimeError("nats_cutover_post_container_restarted")
            query_result = asyncio.run(query_loopback_nats())
            nats_identity_after = capture_nats_identity()
            nats_fingerprint_after = str(nats_identity_after["fingerprint"])
            nats_volume_fingerprint_after = capture_nats_volume_fingerprint()
            if nats_fingerprint_after != nats_fingerprint_before:
                raise RuntimeError("nats_cutover_nats_identity_changed")
            if nats_identity_after != nats_identity_before:
                raise RuntimeError("nats_cutover_nats_identity_changed")
            if nats_volume_fingerprint_after != nats_volume_fingerprint_before:
                raise RuntimeError("nats_cutover_nats_volume_identity_changed")
            nats_query_fingerprint = nats_fingerprint_before
            after = capture_app_quiescence()
            requested_cutoff_ns = time.time_ns()
            event_capture = event_monitor.seal(requested_cutoff_ns)
        events = tuple(event_capture["events"])
        window_ended_ns = requested_cutoff_ns
        window_started_at = datetime.fromtimestamp(
            window_started_ns / 1_000_000_000,
            tz=timezone.utc,
        )
        window_ended_at = datetime.fromtimestamp(
            window_ended_ns / 1_000_000_000,
            tz=timezone.utc,
        )
        if not checked_at <= window_started_at <= window_ended_at:
            raise RuntimeError("nats_cutover_invalid_evidence_time_window")
        app_quiescence = build_app_quiescence_evidence(
            since_ns=window_started_ns,
            until_ns=window_ended_ns,
            before=before,
            after=after,
            events=events,
            event_capture=event_capture,
        )
        expected = build_expected_durable_index()
        rows, blocked = evaluate_existing_consumers(
            query_result.consumers,
            expected,
        )
        unexpected_durables = build_unexpected_durable_rows(
            query_result.consumers,
            expected,
        )
        blocked = blocked or bool(unexpected_durables)
        critical_streams = build_critical_stream_rows(query_result.streams)
        stream_target_compliance, stream_target_blocked = evaluate_stream_target(
            actual_streams=critical_streams,
            target_manifest=target_stream_manifest,
            bootstrap_mode=nats_bootstrap["mode"],
        )
        if args.stage == _STAGE_PRE_FULL_DOWN:
            continuity = {
                "status": "BASELINE_CAPTURED",
                "complete": True,
                "baseline_sha256": None,
                "streams_checked": len(critical_streams),
                "durables_checked": len(rows),
                "violations": [],
            }
        else:
            if previous_payload is None or previous_reference is None:
                raise RuntimeError("nats_cutover_previous_preflight_missing")
            continuity = evaluate_cutover_continuity(
                previous_streams=previous_payload.get("critical_streams"),
                current_streams=critical_streams,
                previous_durables=previous_payload.get("durables"),
                current_durables=rows,
                baseline_sha256=str(previous_reference["sha256"]),
            )
        quiescence_invalidated = (
            app_quiescence["status"] != "PASSED_WITH_TRUST_BOUNDARY"
        )
        continuity_invalidated = continuity["status"] == "INVALIDATED"
        invalidated = quiescence_invalidated or continuity_invalidated
        status = "INVALIDATED" if invalidated else (
            "BLOCKED"
            if blocked or stream_target_blocked
            else "PASSED_WITH_TRUST_BOUNDARY"
        )
        evidence = build_evidence(
            generation=args.generation,
            deployment_lock_id=args.deployment_lock_id,
            deployed_commit=args.deployed_commit,
            checked_at=checked_at,
            query_result=query_result,
            rows=rows,
            status=status,
            app_quiescence=app_quiescence,
            nats_bootstrap=nats_bootstrap,
            nats_query_fingerprint=nats_query_fingerprint,
            stage=args.stage,
            critical_streams=critical_streams,
            unexpected_durables=unexpected_durables,
            target_stream_manifest=target_stream_manifest,
            stream_target_compliance=stream_target_compliance,
            previous_preflight=previous_reference,
            continuity=continuity,
            error_code=(
                "nats_durable_cutover_app_quiescence_invalidated"
                if quiescence_invalidated
                else (
                    "nats_durable_cutover_continuity_invalidated"
                    if continuity_invalidated
                    else None
                )
            ),
        )
        exit_code = 5 if invalidated else (
            2 if blocked or stream_target_blocked else 0
        )
    except Exception:
        evidence = build_evidence(
            generation=args.generation,
            deployment_lock_id=args.deployment_lock_id,
            deployed_commit=args.deployed_commit,
            checked_at=checked_at,
            query_result=None,
            rows=(),
            status="QUERY_FAILED",
            app_quiescence=app_quiescence,
            nats_bootstrap=nats_bootstrap,
            nats_query_fingerprint=nats_query_fingerprint,
            stage=args.stage,
            critical_streams=critical_streams,
            target_stream_manifest=target_stream_manifest,
            stream_target_compliance=stream_target_compliance,
            previous_preflight=previous_reference,
            continuity=continuity,
            error_code="nats_durable_cutover_preflight_query_failed",
        )
        exit_code = 3

    try:
        evidence_path = write_evidence(evidence)
    except Exception:
        print(
            "ERROR: nats_durable_cutover_preflight_evidence_write_failed",
            file=sys.stderr,
        )
        return 4

    print(evidence_path.as_posix())
    if exit_code == 2:
        print(
            "ERROR: nats_durable_cutover_preflight_blocked",
            file=sys.stderr,
        )
    elif exit_code == 3:
        print(
            "ERROR: nats_durable_cutover_preflight_query_failed",
            file=sys.stderr,
        )
    elif exit_code == 5:
        print(f"ERROR: {evidence['error_code']}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
