#!/usr/bin/env python3
"""Fail-closed, read-only JetStream durable cutover preflight.

This command is intentionally limited to the loopback NATS endpoint used by
the WSL2 deployment.  It lists every stream and every consumer with explicit
pagination, then evaluates every existing durable declared by the exact
four-role runtime ownership manifest.  Critical event cursors keep strict
``DeliverPolicy.ALL`` drain semantics; declared snapshot/transient consumers
are recorded with their ``LAST``/``NEW`` semantics and may only be classified
as safely rebuildable when their immutable identity and configuration are
exact.  Persist-only critical topics have no JetStream stream or live consumer
and are deliberately excluded.

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

from aats.bus.nats_bus import (  # noqa: E402
    consumer_mutable_config_migration_blockers,
)
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
_SCHEMA_VERSION = "aats.nats_durable_cutover_preflight.v3"
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
    delivery_semantics: str = "event"
    ack_policy: str = "explicit"
    deliver_policy: str = "all"
    ack_wait_seconds: float = 30.0
    max_deliver: int = 5


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
    deliver_subject_present: bool = True
    replay_policy: str = "instant"
    headers_only: bool = False
    pause_until: str | None = None
    backoff_seconds: tuple[float, ...] = ()
    rate_limit_bps: int = 0
    inactive_threshold_seconds: float | None = None
    mem_storage: bool = False
    ack_wait_seconds: float = 30.0
    max_deliver: int = 5
    durable_name_matches: bool = True
    opt_start_seq: int | None = None
    opt_start_time: str | None = None


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


def validate_query_result_consumer_projection(result: QueryResult) -> None:
    """Cross-check broker stream counts against the paginated consumer list."""

    if (
        isinstance(result.stream_count, bool)
        or result.stream_count < 0
        or result.stream_count != len(result.streams)
        or isinstance(result.consumer_count, bool)
        or result.consumer_count < 0
        or result.consumer_count != len(result.consumers)
    ):
        raise RuntimeError("nats_consumer_projection_count_invalid")
    enumerated_by_stream: dict[str, int] = {}
    for consumer in result.consumers:
        enumerated_by_stream[consumer.stream] = (
            enumerated_by_stream.get(consumer.stream, 0) + 1
        )
    broker_stream_names: set[str] = set()
    for stream in result.streams:
        if stream.name in broker_stream_names:
            raise RuntimeError("nats_consumer_projection_stream_duplicate")
        broker_stream_names.add(stream.name)
        if stream.consumer_count != enumerated_by_stream.get(stream.name, 0):
            raise RuntimeError("nats_consumer_projection_stream_count_mismatch")
    if set(enumerated_by_stream) - broker_stream_names:
        raise RuntimeError("nats_consumer_projection_stream_missing")


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
    """Derive the exact declared four-role durable topology from runtime truth."""

    from aats.bus.consumer_ownership import (
        SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE,
    )
    from aats.bus.nats_bus import (
        DEFAULT_CRITICAL_TOPICS,
        DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS,
        NatsBusConfig,
        delivery_semantics_for,
    )
    from aats.events import topics

    config = NatsBusConfig()
    expected: dict[str, ExpectedDurable] = {}
    stream_backed_topics = (
        DEFAULT_CRITICAL_TOPICS - DEFAULT_PERSIST_ONLY_CRITICAL_TOPICS
    )
    if tuple(SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE) != _MAIN_ROLES:
        raise RuntimeError("nats_cutover_consumer_role_manifest_drift")
    for role, owned_topics in SPLIT_RUNTIME_CONSUMER_TOPICS_BY_ROLE.items():
        if owned_topics - stream_backed_topics:
            raise RuntimeError("nats_cutover_non_stream_consumer_declared")
        for topic in sorted(owned_topics):
            semantics = delivery_semantics_for(topic)
            deliver_policy = {
                "event": "all",
                "snapshot": "last",
                "transient": "new",
            }.get(semantics)
            if deliver_policy is None:
                raise RuntimeError("nats_cutover_unknown_delivery_semantics")
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
                delivery_semantics=semantics,
                deliver_policy=deliver_policy,
                ack_wait_seconds=(
                    90.0
                    if topic
                    in {topics.MARKET_SNAPSHOTS, topics.FEATURE_SNAPSHOTS}
                    else config.ack_wait_seconds
                ),
                max_deliver=config.max_deliver,
            )
            previous = expected.setdefault(durable, candidate)
            if previous != candidate:
                raise RuntimeError("nats_cutover_expected_durable_collision")
    return expected


def _actual_consumer_config(state: ConsumerState) -> dict[str, object]:
    """Return immutable/no-reset consumer behavior needed for safe delivery."""

    return {
        "stream": state.stream,
        "deliver_policy": state.deliver_policy,
        "ack_policy": state.ack_policy,
        "filter_subject": state.filter_subject,
        "filter_subjects": list(state.filter_subjects),
        "deliver_group": state.deliver_group,
        # Only presence is evidence: the generated push inbox itself is volatile
        # and must never become an identity or continuity field.
        "deliver_subject_present": state.deliver_subject_present,
        "replay_policy": state.replay_policy,
        "headers_only": state.headers_only,
        "pause_until": state.pause_until,
        "backoff_seconds": list(state.backoff_seconds),
        "rate_limit_bps": state.rate_limit_bps,
        "inactive_threshold_seconds": state.inactive_threshold_seconds,
        "mem_storage": state.mem_storage,
        "durable_name_matches": state.durable_name_matches,
        "opt_start_seq": state.opt_start_seq,
        "opt_start_time": state.opt_start_time,
    }


def _expected_consumer_config(expected: ExpectedDurable) -> dict[str, object]:
    return {
        "stream": expected.stream,
        "deliver_policy": expected.deliver_policy,
        "ack_policy": expected.ack_policy,
        "filter_subject": expected.filter_subject,
        "deliver_group": None,
        "deliver_subject_present": True,
        "replay_policy": "instant",
        "headers_only": False,
        "pause_until": None,
        "backoff_seconds": [],
        "rate_limit_bps": 0,
        "inactive_threshold_seconds": None,
        "mem_storage": False,
        "durable_name_matches": True,
        "opt_start_seq": None,
        "opt_start_time": None,
    }


def _actual_consumer_mutable_config(state: ConsumerState) -> dict[str, object]:
    return {
        "ack_wait_seconds": state.ack_wait_seconds,
        "max_deliver": state.max_deliver,
    }


def _expected_consumer_mutable_config(
    expected: ExpectedDurable,
) -> dict[str, object]:
    return {
        "ack_wait_seconds": expected.ack_wait_seconds,
        "max_deliver": expected.max_deliver,
    }


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
    if not state.durable_name_matches:
        blockers.append("durable_name_drift")
    if not state.deliver_subject_present:
        blockers.append("push_delivery_subject_missing")
    if state.replay_policy != "instant":
        blockers.append("replay_policy_drift")
    if state.headers_only:
        blockers.append("headers_only_enabled")
    if state.pause_until is not None:
        blockers.append("consumer_paused")
    if state.backoff_seconds:
        blockers.append("backoff_policy_drift")
    if state.rate_limit_bps != 0:
        blockers.append("rate_limit_enabled")
    if state.inactive_threshold_seconds is not None:
        blockers.append("inactive_threshold_enabled")
    if state.mem_storage:
        blockers.append("memory_storage_enabled")
    mutable_migration_blockers = consumer_mutable_config_migration_blockers(
        delivery_semantics=expected.delivery_semantics,
        current_ack_wait_seconds=state.ack_wait_seconds,
        target_ack_wait_seconds=expected.ack_wait_seconds,
        current_max_deliver=state.max_deliver,
        target_max_deliver=expected.max_deliver,
    )
    blockers.extend(mutable_migration_blockers)
    ack_wait_drift = state.ack_wait_seconds != expected.ack_wait_seconds
    ack_wait_reconcile_allowed = (
        ack_wait_drift and "ack_wait_drift" not in mutable_migration_blockers
    )
    if state.opt_start_seq is not None:
        blockers.append("start_sequence_enabled")
    if state.opt_start_time is not None:
        blockers.append("start_time_enabled")
    if state.num_ack_pending < 0:
        blockers.append("invalid_num_ack_pending")

    reducing_window = (
        state.max_ack_pending <= 0
        or state.max_ack_pending > _TARGET_MAX_ACK_PENDING
    )
    outstanding_exceeds_target = (
        state.max_ack_pending == _TARGET_MAX_ACK_PENDING
        and state.num_ack_pending > _TARGET_MAX_ACK_PENDING
    )
    mutable_config_drift = ack_wait_drift and ack_wait_reconcile_allowed
    if expected.delivery_semantics == "event":
        if reducing_window and state.num_ack_pending != 0:
            blockers.append("ack_window_migration_requires_drain")
        elif outstanding_exceeds_target:
            blockers.append("outstanding_exceeds_target")

    if blockers:
        status = "BLOCKED"
    elif expected.delivery_semantics != "event" and (
        (reducing_window and state.num_ack_pending != 0)
        or outstanding_exceeds_target
    ):
        status = "SAFE_REBUILDABLE_NON_EVENT"
    elif mutable_config_drift:
        status = "SAFE_TO_RECONCILE_IN_PLACE"
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
            "delivery_semantics": expected.delivery_semantics,
        },
        "created": state.created,
        "cursor": asdict(state.cursor),
        "immutable_config": {
            "actual": _actual_consumer_config(state),
            "expected": _expected_consumer_config(expected),
        },
        "mutable_config": {
            "actual": _actual_consumer_mutable_config(state),
            "expected": _expected_consumer_mutable_config(expected),
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
    """Evaluate existing consumers matching the declared runtime ownership map."""

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
    """Expose every truly unowned durable with complete read-only facts."""

    rows = [
        {
            "identity": {
                "stream": state.stream,
                "durable": state.durable,
            },
            "created": state.created,
            "cursor": asdict(state.cursor),
            "immutable_config": {
                "actual": _actual_consumer_config(state),
            },
            "mutable_config": {
                "actual": _actual_consumer_mutable_config(state),
            },
            "window": {"current_max_ack_pending": state.max_ack_pending},
            "outstanding": {"num_ack_pending": state.num_ack_pending},
            "status": "BLOCKED",
            "blockers": ["consumer_owner_not_declared"],
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


def build_missing_declared_durable_rows(
    consumers: Sequence[ConsumerState],
    expected_by_name: dict[str, ExpectedDurable],
) -> list[dict[str, object]]:
    """Expose declared consumers absent from a preserved NATS installation."""

    observed_names = {state.durable for state in consumers}
    rows = [
        {
            "identity": {
                "stream": expected.stream,
                "durable": expected.durable,
                "role": expected.role,
                "topic": expected.topic,
                "delivery_semantics": expected.delivery_semantics,
            },
            "immutable_config": {
                "expected": _expected_consumer_config(expected),
            },
            "mutable_config": {
                "expected": _expected_consumer_mutable_config(expected),
            },
            "status": "BLOCKED",
            "blockers": ["declared_consumer_missing"],
        }
        for expected in expected_by_name.values()
        if expected.durable not in observed_names
    ]
    rows.sort(
        key=lambda row: (
            str(row["identity"]["stream"]),  # type: ignore[index]
            str(row["identity"]["durable"]),  # type: ignore[index]
        )
    )
    return rows


def _recompute_preflight_durable_row(
    row: object,
    *,
    expected: ExpectedDurable,
) -> dict[str, object]:
    """Recompute a persisted row so its safety status is never self-attested."""

    error = "nats_cutover_malformed_previous_preflight"
    if not isinstance(row, dict) or set(row) != {
        "identity",
        "created",
        "cursor",
        "immutable_config",
        "mutable_config",
        "window",
        "outstanding",
        "status",
        "blockers",
    }:
        raise RuntimeError(error)
    if row.get("identity") != {
        "stream": expected.stream,
        "durable": expected.durable,
        "role": expected.role,
        "topic": expected.topic,
        "delivery_semantics": expected.delivery_semantics,
    }:
        raise RuntimeError(error)
    _parse_evidence_timestamp(row.get("created"))
    immutable = row.get("immutable_config")
    mutable = row.get("mutable_config")
    if not isinstance(immutable, dict) or set(immutable) != {"actual", "expected"}:
        raise RuntimeError(error)
    if not isinstance(mutable, dict) or set(mutable) != {"actual", "expected"}:
        raise RuntimeError(error)
    actual = immutable.get("actual")
    canonical_expected = _expected_consumer_config(expected)
    if (
        immutable.get("expected") != canonical_expected
        or not isinstance(actual, dict)
        or set(actual) != set(canonical_expected) | {"filter_subjects"}
    ):
        raise RuntimeError(error)
    actual_mutable = mutable.get("actual")
    if (
        mutable.get("expected") != _expected_consumer_mutable_config(expected)
        or not isinstance(actual_mutable, dict)
        or set(actual_mutable) != {"ack_wait_seconds", "max_deliver"}
    ):
        raise RuntimeError(error)

    cursor = row.get("cursor")
    window = row.get("window")
    outstanding = row.get("outstanding")
    if (
        not isinstance(cursor, dict)
        or set(cursor)
        != {
            "delivered_stream_seq",
            "delivered_consumer_seq",
            "ack_floor_stream_seq",
            "ack_floor_consumer_seq",
        }
        or not isinstance(window, dict)
        or set(window)
        != {"current_max_ack_pending", "target_max_ack_pending"}
        or not isinstance(outstanding, dict)
        or set(outstanding) != {"num_ack_pending"}
    ):
        raise RuntimeError(error)
    cursor_values = tuple(_row_integer(cursor, field) for field in cursor)
    del cursor_values
    current_window = window.get("current_max_ack_pending")
    target_window = window.get("target_max_ack_pending")
    num_ack_pending = outstanding.get("num_ack_pending")
    if (
        isinstance(current_window, bool)
        or not isinstance(current_window, int)
        or isinstance(target_window, bool)
        or target_window != _TARGET_MAX_ACK_PENDING
        or isinstance(num_ack_pending, bool)
        or not isinstance(num_ack_pending, int)
        or num_ack_pending < 0
    ):
        raise RuntimeError(error)

    filter_subject = actual.get("filter_subject")
    filter_subjects = actual.get("filter_subjects")
    deliver_group = actual.get("deliver_group")
    backoff = actual.get("backoff_seconds")
    pause_until = actual.get("pause_until")
    inactive_threshold = actual.get("inactive_threshold_seconds")
    ack_wait = actual_mutable.get("ack_wait_seconds")
    max_deliver = actual_mutable.get("max_deliver")
    rate_limit = actual.get("rate_limit_bps")
    opt_start_seq = actual.get("opt_start_seq")
    opt_start_time = actual.get("opt_start_time")
    if (
        filter_subject is not None
        and not isinstance(filter_subject, str)
        or not isinstance(filter_subjects, list)
        or not all(isinstance(value, str) for value in filter_subjects)
        or deliver_group is not None
        and not isinstance(deliver_group, str)
        or not isinstance(backoff, list)
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in backoff
        )
        or pause_until is not None
        and not isinstance(pause_until, str)
        or inactive_threshold is not None
        and (
            isinstance(inactive_threshold, bool)
            or not isinstance(inactive_threshold, (int, float))
        )
        or isinstance(ack_wait, bool)
        or not isinstance(ack_wait, (int, float))
        or isinstance(max_deliver, bool)
        or not isinstance(max_deliver, int)
        or isinstance(rate_limit, bool)
        or not isinstance(rate_limit, int)
        or not isinstance(actual.get("deliver_subject_present"), bool)
        or not isinstance(actual.get("replay_policy"), str)
        or not isinstance(actual.get("headers_only"), bool)
        or not isinstance(actual.get("mem_storage"), bool)
        or not isinstance(actual.get("durable_name_matches"), bool)
        or opt_start_seq is not None
        and (
            isinstance(opt_start_seq, bool)
            or not isinstance(opt_start_seq, int)
            or opt_start_seq < 0
        )
        or opt_start_time is not None
        and not isinstance(opt_start_time, str)
    ):
        raise RuntimeError(error)
    state = ConsumerState(
        stream=actual.get("stream"),
        durable=expected.durable,
        created=row["created"],
        deliver_policy=actual.get("deliver_policy"),
        ack_policy=actual.get("ack_policy"),
        filter_subject=filter_subject,
        filter_subjects=tuple(filter_subjects),
        deliver_group=deliver_group,
        max_ack_pending=current_window,
        num_ack_pending=num_ack_pending,
        cursor=ConsumerCursor(**cursor),
        deliver_subject_present=actual.get("deliver_subject_present"),
        replay_policy=actual.get("replay_policy"),
        headers_only=actual.get("headers_only"),
        pause_until=pause_until,
        backoff_seconds=tuple(float(value) for value in backoff),
        rate_limit_bps=rate_limit,
        inactive_threshold_seconds=(
            None if inactive_threshold is None else float(inactive_threshold)
        ),
        mem_storage=actual.get("mem_storage"),
        ack_wait_seconds=float(ack_wait),
        max_deliver=max_deliver,
        durable_name_matches=actual.get("durable_name_matches"),
        opt_start_seq=opt_start_seq,
        opt_start_time=opt_start_time,
    )
    recomputed = evaluate_consumer(state, expected)
    if recomputed != row:
        raise RuntimeError(error)
    return recomputed


def validate_preflight_snapshot_projection(
    payload: dict[str, object],
    *,
    bootstrap_mode: str,
) -> None:
    """Validate v3 broker rows, counts and target claims from raw evidence."""

    query = _required_mapping(payload.get("query"))
    durables = payload.get("durables")
    streams = payload.get("critical_streams")
    if not isinstance(durables, list) or not isinstance(streams, list):
        raise RuntimeError("nats_cutover_malformed_previous_preflight")
    if (
        payload.get("unexpected_durables") != []
        or payload.get("missing_declared_durables") != []
        or payload.get("blocker_classes") != []
    ):
        raise RuntimeError("nats_cutover_previous_preflight_not_passed")

    expected = build_expected_durable_index()
    expected_by_pair = {
        (item.stream, item.durable): item for item in expected.values()
    }
    observed_pairs: set[tuple[str, str]] = set()
    event_count = 0
    non_event_count = 0
    consumers_by_stream: dict[str, int] = {}
    for row in durables:
        if not isinstance(row, dict) or not isinstance(row.get("identity"), dict):
            raise RuntimeError("nats_cutover_malformed_previous_preflight")
        identity = row["identity"]
        pair = (identity.get("stream"), identity.get("durable"))
        if (
            not all(isinstance(value, str) for value in pair)
            or pair in observed_pairs
            or pair not in expected_by_pair
        ):
            raise RuntimeError("nats_cutover_malformed_previous_preflight")
        observed_pairs.add(pair)
        canonical = _recompute_preflight_durable_row(
            row,
            expected=expected_by_pair[pair],
        )
        if canonical["status"] == "BLOCKED" or canonical["blockers"] != []:
            raise RuntimeError("nats_cutover_previous_preflight_not_passed")
        semantics = identity.get("delivery_semantics")
        if semantics == "event":
            event_count += 1
        else:
            non_event_count += 1
        stream_name = pair[0]
        consumers_by_stream[stream_name] = consumers_by_stream.get(stream_name, 0) + 1

    if bootstrap_mode == "existing_container_preserved":
        if observed_pairs != set(expected_by_pair):
            raise RuntimeError("nats_cutover_malformed_previous_preflight")
    elif bootstrap_mode == "proven_fresh_install":
        if observed_pairs:
            raise RuntimeError("nats_cutover_malformed_previous_preflight")
    else:
        raise RuntimeError("nats_cutover_malformed_bootstrap_provenance")

    expected_query = {
        "complete": True,
        "streams_scanned": len(streams),
        "consumers_scanned": len(durables),
        "declared_durables_found": len(durables),
        "critical_all_durables_found": event_count,
        "known_non_event_durables_found": non_event_count,
        "critical_streams_found": len(streams),
    }
    if query != expected_query:
        raise RuntimeError("nats_cutover_malformed_previous_preflight")

    stream_index = _indexed_rows(streams, kind="stream")
    for key, row in stream_index.items():
        state = _required_mapping(row.get("state"))
        if _row_integer(state, "consumer_count") != consumers_by_stream.get(key[0], 0):
            raise RuntimeError("nats_cutover_malformed_previous_preflight")
    target_manifest = payload.get("target_stream_manifest")
    target_compliance = payload.get("stream_target_compliance")
    if not isinstance(target_manifest, dict) or not isinstance(target_compliance, dict):
        raise RuntimeError("nats_cutover_malformed_target_stream_manifest")
    target_rows = target_manifest.get("streams")
    target_sha256 = target_manifest.get("sha256")
    if not isinstance(target_rows, list) or not isinstance(target_sha256, str):
        raise RuntimeError("nats_cutover_malformed_target_stream_manifest")
    canonical_target = json.dumps(
        target_rows,
        sort_keys=True,
        separators=(",", ":"),
    )
    computed_sha256 = (
        f"sha256:{hashlib.sha256(canonical_target.encode('utf-8')).hexdigest()}"
    )
    if target_sha256 != computed_sha256:
        raise RuntimeError("nats_cutover_malformed_target_stream_manifest")
    recomputed_compliance, blocked = evaluate_stream_target(
        actual_streams=streams,
        target_manifest=target_manifest,
        bootstrap_mode=bootstrap_mode,
    )
    if blocked or recomputed_compliance != target_compliance:
        raise RuntimeError("nats_cutover_malformed_target_stream_manifest")


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
        if previous.get("mutable_config") != current.get("mutable_config"):
            violations.append(f"durable_mutable_config_changed:{identity}")
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
    allow_declared_cutover_migrations: bool = False,
) -> dict[str, object]:
    """Validate monotonic JetStream continuity while applications are active.

    Publishers, consumers, retention and INTEREST deletion can legitimately
    change counts, outstanding work and first sequence.  Identity, creation
    time and immutable configuration must remain exact; stream last sequence
    and every durable cursor may only advance.  The one bounded exception is
    the explicitly requested app-start cutover migration: an in-place
    ``SAFE_TO_SHRINK`` window update may change only the window; a narrowly
    allowed ``SAFE_TO_RECONCILE_IN_PLACE`` update may also raise a non-event
    consumer's ack wait to its declared target.  A
    ``SAFE_REBUILDABLE_NON_EVENT`` LAST/NEW consumer may also receive a new
    creation time and reset cursor.  Both must end as exact
    ``SAFE_ALREADY_ONE`` rows.  JetStream does not expose purge provenance, so
    purge-vs-retention remains an explicit trust boundary while ``deny_purge``
    is false.
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
    authorized_migrations: list[dict[str, str]] = []
    for key in sorted(set(previous_durable_index) & set(current_durable_index)):
        row_violation_count = len(violations)
        previous = previous_durable_index[key]
        current = current_durable_index[key]
        identity = f"{key[0]}/{key[1]}"
        previous_identity = _required_mapping(previous.get("identity"))
        current_identity = _required_mapping(current.get("identity"))
        previous_window = _required_mapping(previous.get("window"))
        current_window = _required_mapping(current.get("window"))
        previous_outstanding = _required_mapping(previous.get("outstanding"))
        previous_mutable = _required_mapping(previous.get("mutable_config"))
        current_mutable = _required_mapping(current.get("mutable_config"))
        previous_mutable_actual = _required_mapping(previous_mutable.get("actual"))
        previous_mutable_expected = _required_mapping(
            previous_mutable.get("expected")
        )
        current_mutable_actual = _required_mapping(current_mutable.get("actual"))
        current_mutable_expected = _required_mapping(current_mutable.get("expected"))
        previous_current_window = _row_integer(
            previous_window,
            "current_max_ack_pending",
        )
        previous_target_window = _row_integer(
            previous_window,
            "target_max_ack_pending",
        )
        current_current_window = _row_integer(
            current_window,
            "current_max_ack_pending",
        )
        current_target_window = _row_integer(
            current_window,
            "target_max_ack_pending",
        )
        previous_num_ack_pending = _row_integer(
            previous_outstanding,
            "num_ack_pending",
        )
        previous_ack_wait = previous_mutable_actual.get("ack_wait_seconds")
        target_ack_wait = previous_mutable_expected.get("ack_wait_seconds")
        current_ack_wait = current_mutable_actual.get("ack_wait_seconds")
        previous_delivery_semantics = previous_identity.get(
            "delivery_semantics"
        )
        previous_migration_blockers = (
            consumer_mutable_config_migration_blockers(
                delivery_semantics=(
                    previous_delivery_semantics
                    if isinstance(previous_delivery_semantics, str)
                    else "invalid"
                ),
                current_ack_wait_seconds=previous_ack_wait,
                target_ack_wait_seconds=target_ack_wait,
                current_max_deliver=previous_mutable_actual.get(
                    "max_deliver"
                ),
                target_max_deliver=previous_mutable_expected.get(
                    "max_deliver"
                ),
            )
        )
        current_migration_blockers = (
            consumer_mutable_config_migration_blockers(
                delivery_semantics=(
                    previous_delivery_semantics
                    if isinstance(previous_delivery_semantics, str)
                    else "invalid"
                ),
                current_ack_wait_seconds=current_ack_wait,
                target_ack_wait_seconds=current_mutable_expected.get(
                    "ack_wait_seconds"
                ),
                current_max_deliver=current_mutable_actual.get(
                    "max_deliver"
                ),
                target_max_deliver=current_mutable_expected.get(
                    "max_deliver"
                ),
            )
        )
        safe_non_event_ack_wait_raise = (
            previous_delivery_semantics in {"snapshot", "transient"}
            and current_identity.get("delivery_semantics")
            == previous_delivery_semantics
            and not previous_migration_blockers
            and not current_migration_blockers
            and current_ack_wait == target_ack_wait
            and current_mutable_actual.get("max_deliver")
            == current_mutable_expected.get("max_deliver")
        )
        reaches_exact_target = (
            previous_target_window == _TARGET_MAX_ACK_PENDING
            and current_target_window == _TARGET_MAX_ACK_PENDING
            and current_current_window == _TARGET_MAX_ACK_PENDING
            and (
                previous_current_window <= 0
                or previous_current_window > _TARGET_MAX_ACK_PENDING
            )
        )
        in_place_window_shrink = (
            allow_declared_cutover_migrations
            and previous.get("status") == "SAFE_TO_SHRINK"
            and current.get("status") == "SAFE_ALREADY_ONE"
            and reaches_exact_target
            and previous.get("created") == current.get("created")
            and previous.get("immutable_config") == current.get("immutable_config")
            and previous.get("mutable_config") == current.get("mutable_config")
        )
        in_place_mutable_reconcile = (
            allow_declared_cutover_migrations
            and previous.get("status") == "SAFE_TO_RECONCILE_IN_PLACE"
            and current.get("status") == "SAFE_ALREADY_ONE"
            and previous.get("created") == current.get("created")
            and previous.get("immutable_config") == current.get("immutable_config")
            and safe_non_event_ack_wait_raise
            and previous_mutable_actual != previous_mutable_expected
            and previous_mutable_expected == current_mutable_expected
            and current_mutable_actual == current_mutable_expected
            and previous_target_window == _TARGET_MAX_ACK_PENDING
            and current_target_window == _TARGET_MAX_ACK_PENDING
            and current_current_window == _TARGET_MAX_ACK_PENDING
            and (
                previous_current_window == _TARGET_MAX_ACK_PENDING
                or previous_current_window <= 0
                or previous_current_window > _TARGET_MAX_ACK_PENDING
            )
        )
        requires_non_event_rebuild = (
            (
                (
                    previous_current_window <= 0
                    or previous_current_window > _TARGET_MAX_ACK_PENDING
                )
                and previous_num_ack_pending != 0
            )
            or (
                previous_current_window == _TARGET_MAX_ACK_PENDING
                and previous_num_ack_pending > _TARGET_MAX_ACK_PENDING
            )
        )
        non_event_rebuild = (
            allow_declared_cutover_migrations
            and previous.get("status") == "SAFE_REBUILDABLE_NON_EVENT"
            and current.get("status") == "SAFE_ALREADY_ONE"
            and previous_identity.get("delivery_semantics")
            in {"snapshot", "transient"}
            and current_identity.get("delivery_semantics")
            == previous_identity.get("delivery_semantics")
            and previous.get("created") != current.get("created")
            and current_target_window == _TARGET_MAX_ACK_PENDING
            and current_current_window == _TARGET_MAX_ACK_PENDING
            and requires_non_event_rebuild
            and previous.get("immutable_config") == current.get("immutable_config")
            and previous_mutable_expected == current_mutable_expected
            and current_mutable_actual == current_mutable_expected
        )
        if (
            previous.get("created") != current.get("created")
            and not non_event_rebuild
        ):
            violations.append(f"durable_created_changed:{identity}")
        if previous.get("immutable_config") != current.get("immutable_config"):
            violations.append(f"durable_config_changed:{identity}")
        if (
            previous.get("mutable_config") != current.get("mutable_config")
            and not in_place_mutable_reconcile
            and not non_event_rebuild
        ):
            violations.append(f"durable_mutable_config_changed:{identity}")
        if (
            previous.get("window") != current.get("window")
            and not in_place_window_shrink
            and not in_place_mutable_reconcile
            and not non_event_rebuild
        ):
            violations.append(f"durable_window_changed:{identity}")
        previous_cursor = _required_mapping(previous.get("cursor"))
        current_cursor = _required_mapping(current.get("cursor"))
        for field in cursor_fields:
            if (
                _row_integer(current_cursor, field)
                < _row_integer(previous_cursor, field)
                and not non_event_rebuild
            ):
                violations.append(f"durable_cursor_regressed:{identity}:{field}")
        if len(violations) == row_violation_count:
            if in_place_window_shrink:
                authorized_migrations.append(
                    {"identity": identity, "kind": "IN_PLACE_ACK_WINDOW_SHRINK"}
                )
            elif non_event_rebuild:
                authorized_migrations.append(
                    {"identity": identity, "kind": "NON_EVENT_DURABLE_REBUILD"}
                )
            elif in_place_mutable_reconcile:
                authorized_migrations.append(
                    {"identity": identity, "kind": "IN_PLACE_MUTABLE_RECONCILE"}
                )

    return {
        "status": "PASSED_WITH_TRUST_BOUNDARY" if not violations else "INVALIDATED",
        "complete": False,
        "streams_checked": len(current_stream_index),
        "durables_checked": len(current_durable_index),
        "new_identities_allowed": allow_new_identities,
        "declared_cutover_migrations_allowed": (
            allow_declared_cutover_migrations
        ),
        "authorized_durable_migrations": authorized_migrations,
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
    validate_preflight_snapshot_projection(
        payload,
        bootstrap_mode=nats_bootstrap["mode"],
    )
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
        or payload.get("missing_declared_durables") != []
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


def _consumer_duration_seconds(
    value: object,
    *,
    optional: bool = False,
) -> float | None:
    """Normalize nats-py consumer durations without guessing malformed state."""

    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise RuntimeError("nats_cutover_malformed_consumer_state") from exc
    if not math.isfinite(result) or result < 0:
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    # Older nats-py response paths exposed nanoseconds; current versions expose
    # seconds.  The same explicit conversion used for stream durations keeps the
    # evidence stable across those wire decoders.
    if result > 1e10:
        result /= 1e9
    if not math.isfinite(result):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return result


def _consumer_optional_bool(config: object, field: str) -> bool:
    if not hasattr(config, field):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(config, field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _consumer_optional_integer(config: object, field: str) -> int:
    if not hasattr(config, field):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(config, field)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _consumer_deliver_subject_present(config: object) -> bool:
    if not hasattr(config, "deliver_subject"):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(config, "deliver_subject")
    if value is None:
        return False
    if not isinstance(value, str):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return bool(value.strip())


def _consumer_pause_until(config: object) -> str | None:
    if not hasattr(config, "pause_until"):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(config, "pause_until")
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _broker_created_text(value)
    if not isinstance(value, str):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _consumer_optional_timestamp(config: object, field: str) -> str | None:
    if not hasattr(config, field):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(config, field)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _broker_created_text(value)
    if not isinstance(value, str):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _consumer_optional_sequence(config: object, field: str) -> int | None:
    if not hasattr(config, field):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    value = getattr(config, field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return value


def _consumer_backoff_seconds(config: object) -> tuple[float, ...]:
    if not hasattr(config, "backoff"):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    raw = getattr(config, "backoff")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    values = tuple(_consumer_duration_seconds(value) for value in raw)
    if any(value is None for value in values):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    return tuple(float(value) for value in values)


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
    durable = _required_name(getattr(info, "name"))
    config_durable = getattr(config, "durable_name")
    if config_durable is not None and not isinstance(config_durable, str):
        raise RuntimeError("nats_cutover_malformed_consumer_state")
    created_text = _broker_created_text(getattr(info, "created", None))
    delivered = getattr(info, "delivered")
    ack_floor = getattr(info, "ack_floor")
    return ConsumerState(
        stream=_required_name(stream),
        durable=durable,
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
        deliver_subject_present=_consumer_deliver_subject_present(config),
        replay_policy=_policy_text(getattr(config, "replay_policy")),
        headers_only=_consumer_optional_bool(config, "headers_only"),
        pause_until=_consumer_pause_until(config),
        backoff_seconds=_consumer_backoff_seconds(config),
        rate_limit_bps=_consumer_optional_integer(config, "rate_limit_bps"),
        inactive_threshold_seconds=_consumer_duration_seconds(
            getattr(config, "inactive_threshold"),
            optional=True,
        ),
        mem_storage=_consumer_optional_bool(config, "mem_storage"),
        ack_wait_seconds=float(
            _consumer_duration_seconds(getattr(config, "ack_wait"))
        ),
        max_deliver=_integer_value(config, "max_deliver", non_negative=False),
        durable_name_matches=config_durable == durable,
        opt_start_seq=_consumer_optional_sequence(config, "opt_start_seq"),
        opt_start_time=_consumer_optional_timestamp(config, "opt_start_time"),
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
    result = QueryResult(
        stream_count=len(streams),
        consumer_count=consumer_count,
        consumers=tuple(consumers),
        streams=stream_states,
    )
    validate_query_result_consumer_projection(result)
    return result


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
    missing_declared_durables: Sequence[dict[str, object]] = (),
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
    if missing_declared_durables and status in {
        "PASSED",
        "PASSED_WITH_TRUST_BOUNDARY",
    }:
        status = "BLOCKED"
    event_row_count = 0
    non_event_row_count = 0
    row_blockers: set[str] = set()
    for row in rows:
        identity = row.get("identity")
        semantics = (
            identity.get("delivery_semantics")
            if isinstance(identity, dict)
            else None
        )
        if semantics == "event":
            event_row_count += 1
        elif semantics in {"snapshot", "transient"}:
            non_event_row_count += 1
        blockers = row.get("blockers")
        if isinstance(blockers, list):
            row_blockers.update(str(blocker) for blocker in blockers)
    if unexpected_durables:
        row_blockers.add("consumer_owner_not_declared")
    if missing_declared_durables:
        row_blockers.add("declared_consumer_missing")
    blocker_classes = sorted(row_blockers)
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
            "declared_durables_found": len(rows),
            "critical_all_durables_found": event_row_count,
            "known_non_event_durables_found": non_event_row_count,
            "critical_streams_found": len(critical_streams),
        },
        "critical_streams": list(critical_streams),
        "target_stream_manifest": target_stream_manifest,
        "stream_target_compliance": stream_target_compliance,
        "durables": list(rows),
        "unexpected_durables": list(unexpected_durables),
        "missing_declared_durables": list(missing_declared_durables),
        "app_quiescence": app_quiescence,
        "nats_bootstrap": nats_bootstrap,
        "nats_query_fingerprint": nats_query_fingerprint,
        "previous_preflight": previous_preflight,
        "continuity": continuity,
        "blocker_classes": blocker_classes,
    }
    if status == "BLOCKED":
        if "consumer_owner_not_declared" in blocker_classes:
            instruction_code = (
                "nats_durable_cutover_requires_approved_unknown_consumer_release_review"
            )
            instruction = (
                "Keep NATS and persistent state online. A durable is not owned "
                "by the current split-runtime manifest. Establish its human "
                "owner and retention requirement before any approved removal; "
                "never auto-ack, delete, recreate, reset, or purge it."
            )
        elif any(
            blocker in blocker_classes
            for blocker in (
                "ack_window_migration_requires_drain",
                "outstanding_exceeds_target",
            )
        ):
            instruction_code = (
                "nats_durable_cutover_requires_approved_all_cursor_drain"
            )
            instruction = (
                "Keep NATS and persistent state online. Drain the matching "
                "critical ALL consumer naturally to zero under an approved "
                "change window, then rerun this preflight. Never auto-ack, "
                "delete, recreate, reset, or purge its financial event cursor."
            )
        elif "declared_consumer_missing" in blocker_classes:
            instruction_code = (
                "nats_durable_cutover_requires_approved_missing_consumer_review"
            )
            instruction = (
                "Keep NATS and persistent state online. A consumer declared by "
                "the current release is absent from the preserved broker. "
                "Review cursor/replay consequences before provisioning it; "
                "never create, reset, or replace financial state implicitly."
            )
        else:
            instruction_code = (
                "nats_durable_cutover_requires_approved_config_release_review"
            )
            instruction = (
                "Keep NATS and persistent state online. Declared consumer or "
                "stream configuration differs from the release contract and "
                "requires human review. Never auto-ack, delete, recreate, "
                "reset, or purge critical state."
            )
        evidence["recovery"] = {
            "instruction_code": instruction_code,
            "instruction": instruction,
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
        missing_declared_durables = (
            build_missing_declared_durable_rows(
                query_result.consumers,
                expected,
            )
            if nats_bootstrap["mode"] == "existing_container_preserved"
            else []
        )
        blocked = (
            blocked
            or bool(unexpected_durables)
            or bool(missing_declared_durables)
        )
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
            missing_declared_durables=missing_declared_durables,
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
