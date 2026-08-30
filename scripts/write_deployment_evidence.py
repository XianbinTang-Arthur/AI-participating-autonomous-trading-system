#!/usr/bin/env python3
"""Write a no-secret, non-overwriting simulation deployment evidence packet."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.docker_event_monitor import (  # noqa: E402
    seal_external_monitor,
    validate_live_window_evidence,
)
from scripts.nats_runtime_identity import (  # noqa: E402
    NATS_CONTAINER_INSPECT_TEMPLATE,
    NATS_HEALTH_INSPECT_TEMPLATE,
    NATS_VOLUME_INSPECT_TEMPLATE,
    capture_nats_health_snapshot as capture_shared_nats_health_snapshot,
    capture_nats_identity as capture_shared_nats_identity,
    capture_nats_volume_fingerprint as capture_shared_nats_volume_fingerprint,
)


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_RE = re.compile(r"^aats-[a-z0-9-]+$")
_CONTAINER_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}/(?:tcp|udp)$")
_RFC3339_NANO_RE = re.compile(
    r"^(?P<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})$"
)
_READINESS_GENERATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIMULATION_PROFILES = frozenset({"spot", "derivatives"})
_LOOPBACK_HOST_IPS = frozenset({"127.0.0.1", "::1"})
_COLLECTOR_HEARTBEATS = {
    "aats-liquidations-daemon": "/tmp/aats_liquidations_heartbeat",
    "aats-microstructure-collector": "/tmp/aats_microstructure_heartbeat",
}
_COLLECTOR_MAX_HEARTBEAT_AGE_SECONDS = 60.0
_COLLECTOR_MAX_FUTURE_SKEW_SECONDS = 5.0
_MIN_FINAL_EVIDENCE_WINDOW_SECONDS = 35.0
# Moby retains only five health log entries.  The shortest managed application
# health interval is 15s, so a window below 75s guarantees that any one-off
# failure after the boundary is still present in at least one final inspect.
_MAX_FINAL_EVIDENCE_WINDOW_SECONDS = 60.0
_COMMAND_TIMEOUT_SECONDS = 30.0
_NATS_CUTOVER_PREFLIGHT_SCHEMA = "aats.nats_durable_cutover_preflight.v3"
_MAX_NATS_CUTOVER_EVIDENCE_BYTES = 5 * 1024 * 1024
_NATS_BOOTSTRAP_MODES = frozenset(
    {"existing_container_preserved", "proven_fresh_install"}
)
_KNOWN_APP_CONTAINERS = (
    "aats-gateway",
    "aats-market",
    "aats-decision",
    "aats-execution",
    "aats-rdp-daemon",
    "aats-liquidations-daemon",
    "aats-microstructure-collector",
)
_NATS_CONTAINER = "aats-nats"
_NATS_IDENTITY_INSPECT_TEMPLATE = NATS_CONTAINER_INSPECT_TEMPLATE
_NATS_HEALTH_INSPECT_TEMPLATE = NATS_HEALTH_INSPECT_TEMPLATE
_NATS_VOLUME_INSPECT_TEMPLATE = NATS_VOLUME_INSPECT_TEMPLATE
_APP_CONTAINER_INSPECT_TEMPLATE = (
    '{"Name":{{json .Name}},"Id":{{json .Id}},"Image":{{json .Image}},'
    '"RestartCount":{{json .RestartCount}},'
    '"State":{"Status":{{json .State.Status}},'
    '"StartedAt":{{json .State.StartedAt}},'
    '"Health":{"Status":{{json .State.Health.Status}},'
    '"FailingStreak":{{json .State.Health.FailingStreak}},'
    '"Checks":[{{range $index, $entry := .State.Health.Log}}'
    '{{if $index}},{{end}}{"Start":{{json $entry.Start}},'
    '"End":{{json $entry.End}},"ExitCode":{{json $entry.ExitCode}}}'
    '{{end}}]}},'
    '"ComposeProject":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"ComposeService":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"NatsTargetManifestSha256":'
    '{{json (index .Config.Labels "com.aats.nats-target-manifest-sha256")}},'
    '"SafeEnvironment":{'
    '"AATS_RUNTIME_READINESS_GENERATION":'
    '{{json (index .Config.Labels "com.aats.runtime-readiness-generation")}},'
    '"AATS_DEPLOYED_GIT_COMMIT":'
    '{{json (index .Config.Labels "com.aats.deployed-git-commit")}},'
    '"AATS_PROFILE":{{json (index .Config.Labels "com.aats.profile")}}},'
    '"Ports":{{json .NetworkSettings.Ports}}}'
)
_REQUIRED_CONTAINERS_BY_PROFILE = {
    "spot": (
        "aats-gateway",
        "aats-market",
        "aats-decision",
        "aats-execution",
        "aats-rdp-daemon",
    ),
    "derivatives": _KNOWN_APP_CONTAINERS,
}


CommandRunner = Callable[[Sequence[str], Path | None], str]
Clock = Callable[[], datetime]
NanosecondClock = Callable[[], int]
NatsStreamProbe = Callable[[], object]
LifecycleMonitorSealer = Callable[..., dict[str, object]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_command(args: Sequence[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def _copy_container_file_mtime(container: str, path: str) -> int:
    """Read one file mtime through Docker's archive API without container exec."""

    try:
        completed = subprocess.run(
            ("docker", "cp", f"{container}:{path}", "-"),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"collector_heartbeat_copy_failed:{container}") from exc
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise RuntimeError(f"collector_heartbeat_copy_failed:{container}")
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"collector_heartbeat_archive_invalid:{container}") from exc
    if (
        len(members) != 1
        or Path(members[0].name).name != Path(path).name
        or isinstance(members[0].mtime, bool)
        or not isinstance(members[0].mtime, int)
        or members[0].mtime < 0
    ):
        raise RuntimeError(f"collector_heartbeat_archive_invalid:{container}")
    return members[0].mtime


def _required_text(value: object, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(error_code)
    return value


def _required_non_negative_int(value: object, error_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(error_code)
    return value


def _required_nats_bootstrap(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "mode",
        "baseline_fingerprint",
        "volume_fingerprint",
    }:
        raise ValueError("invalid_nats_cutover_bootstrap_provenance")
    mode = value.get("mode")
    fingerprint = value.get("baseline_fingerprint")
    volume_fingerprint = value.get("volume_fingerprint")
    if (
        mode not in _NATS_BOOTSTRAP_MODES
        or not isinstance(fingerprint, str)
        or not isinstance(volume_fingerprint, str)
    ):
        raise ValueError("invalid_nats_cutover_bootstrap_provenance")
    if not _SHA256_RE.fullmatch(fingerprint) or not _SHA256_RE.fullmatch(
        volume_fingerprint
    ):
        raise ValueError("invalid_nats_cutover_bootstrap_provenance")
    return {
        "mode": mode,
        "baseline_fingerprint": fingerprint,
        "volume_fingerprint": volume_fingerprint,
    }


def _required_target_stream_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"source", "streams", "sha256"}:
        raise ValueError("invalid_nats_target_stream_manifest")
    source = value.get("source")
    streams = value.get("streams")
    fingerprint = value.get("sha256")
    if (
        source not in {"profile_env_allowlist", "code_defaults"}
        or not isinstance(streams, list)
        or not streams
        or not isinstance(fingerprint, str)
        or not _SHA256_RE.fullmatch(fingerprint)
    ):
        raise ValueError("invalid_nats_target_stream_manifest")
    names: set[str] = set()
    for row in streams:
        if not isinstance(row, dict) or set(row) != {"identity", "immutable_config"}:
            raise ValueError("invalid_nats_target_stream_manifest")
        identity = row.get("identity")
        config = row.get("immutable_config")
        if not isinstance(identity, dict) or set(identity) != {"name"}:
            raise ValueError("invalid_nats_target_stream_manifest")
        name = identity.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("invalid_nats_target_stream_manifest")
        names.add(name)
        if not isinstance(config, dict) or set(config) != {
            "subjects",
            "retention",
            "storage",
            "discard",
            "max_age_seconds",
            "max_bytes",
            "max_msgs",
            "max_msg_size",
            "num_replicas",
            "duplicate_window_seconds",
            "deny_purge",
        }:
            raise ValueError("invalid_nats_target_stream_manifest")
    canonical = json.dumps(streams, sort_keys=True, separators=(",", ":"))
    expected = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    if fingerprint != expected:
        raise ValueError("invalid_nats_target_stream_manifest_hash")
    return {"source": source, "streams": streams, "sha256": fingerprint}


def _required_stream_target_compliance(
    value: object,
    *,
    target_sha256: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid_nats_stream_target_compliance")
    if (
        value.get("status") not in {"MATCHED", "PROVISIONING_REQUIRED"}
        or value.get("target_sha256") != target_sha256
        or value.get("unexpected_names") != []
        or value.get("drift") != []
        or not isinstance(value.get("expected_names"), list)
        or not isinstance(value.get("actual_names"), list)
        or not isinstance(value.get("missing_names"), list)
    ):
        raise ValueError("invalid_nats_stream_target_compliance")
    return value


def _stream_target_projection(
    rows: Sequence[dict[str, object]],
) -> tuple[list[dict[str, object]], str]:
    projection: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("invalid_final_nats_stream_snapshot")
        identity = row.get("identity")
        created = row.get("created")
        immutable_config = row.get("immutable_config")
        if (
            not isinstance(identity, dict)
            or not isinstance(created, str)
            or not created
            or not isinstance(immutable_config, dict)
        ):
            raise RuntimeError("invalid_final_nats_stream_snapshot")
        name = identity.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise RuntimeError("invalid_final_nats_stream_snapshot")
        seen.add(name)
        projection.append(
            {
                "identity": {"name": name},
                "created": created,
                "immutable_config": immutable_config,
            }
        )
    projection.sort(key=lambda row: str(row["identity"]["name"]))  # type: ignore[index]
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    fingerprint = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return projection, fingerprint


def _durable_qualification_projection(
    rows: Sequence[dict[str, object]],
) -> tuple[list[dict[str, object]], str]:
    """Build a canonical, no-secret durable snapshot for independent audit."""

    required_keys = {
        "identity",
        "created",
        "cursor",
        "immutable_config",
        "mutable_config",
        "window",
        "outstanding",
        "status",
        "blockers",
    }
    required_identity_keys = {
        "stream",
        "durable",
        "role",
        "topic",
        "delivery_semantics",
    }
    projection: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != required_keys:
            raise RuntimeError("invalid_final_nats_durable_snapshot")
        identity = row.get("identity")
        created = row.get("created")
        if (
            not isinstance(identity, dict)
            or set(identity) != required_identity_keys
            or not isinstance(created, str)
            or not created
            or row.get("status") != "SAFE_ALREADY_ONE"
            or row.get("blockers") != []
        ):
            raise RuntimeError("invalid_final_nats_durable_snapshot")
        stream = identity.get("stream")
        durable = identity.get("durable")
        if (
            not isinstance(stream, str)
            or not stream
            or not isinstance(durable, str)
            or not durable
            or (stream, durable) in seen
        ):
            raise RuntimeError("invalid_final_nats_durable_snapshot")
        if any(
            not isinstance(row.get(key), dict)
            for key in (
                "cursor",
                "immutable_config",
                "mutable_config",
                "window",
                "outstanding",
            )
        ):
            raise RuntimeError("invalid_final_nats_durable_snapshot")
        seen.add((stream, durable))
        projection.append(deepcopy(row))
    projection.sort(
        key=lambda row: (
            str(row["identity"]["stream"]),  # type: ignore[index]
            str(row["identity"]["durable"]),  # type: ignore[index]
        )
    )
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    fingerprint = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return projection, fingerprint


def _utc_text_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat()


def _parse_aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_nats_cutover_preflight_checked_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_nats_cutover_preflight_checked_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_nats_cutover_preflight_checked_at")
    return parsed


def _rfc3339_nanoseconds(value: object, *, error: str) -> int:
    if not isinstance(value, str):
        raise RuntimeError(error)
    match = _RFC3339_NANO_RE.fullmatch(value)
    if match is None:
        raise RuntimeError(error)
    zone = match.group("zone")
    try:
        whole_seconds = datetime.fromisoformat(
            f"{match.group('base')}{'+00:00' if zone == 'Z' else zone}"
        )
    except ValueError as exc:
        raise RuntimeError(error) from exc
    fraction = (match.group("fraction") or "").ljust(9, "0")
    return int(whole_seconds.timestamp()) * 1_000_000_000 + int(fraction or "0")


def _capture_nats_identity(*, run: CommandRunner) -> dict[str, object]:
    return capture_shared_nats_identity(lambda args: run(args, None))


def _capture_nats_health_snapshot(
    *,
    run: CommandRunner,
    health_window_started_ns: int,
    expected_container_id: str,
) -> dict[str, object]:
    return capture_shared_nats_health_snapshot(
        lambda args: run(args, None),
        health_window_started_ns=health_window_started_ns,
        expected_container_id=expected_container_id,
        require_success_after_boundary=True,
    )


def _capture_nats_volume_fingerprint(*, run: CommandRunner) -> str:
    return capture_shared_nats_volume_fingerprint(lambda args: run(args, None))


def _probe_final_nats_state() -> object:
    """Read live JetStream streams and durables without mutation."""

    from scripts import check_nats_durable_cutover as cutover

    return asyncio.run(cutover.query_loopback_nats())


def _read_final_nats_state(
    probe: NatsStreamProbe,
) -> tuple[object, list[dict[str, object]], list[dict[str, object]]]:
    """Normalize one complete read-only JetStream snapshot for final evidence."""

    from scripts import check_nats_durable_cutover as cutover

    result = probe()
    if not isinstance(result, cutover.QueryResult):
        raise RuntimeError("invalid_final_nats_query_result")
    if (
        isinstance(result.stream_count, bool)
        or result.stream_count < 0
        or result.stream_count != len(result.streams)
        or isinstance(result.consumer_count, bool)
        or result.consumer_count < 0
        or result.consumer_count != len(result.consumers)
    ):
        raise RuntimeError("invalid_final_nats_query_result")
    try:
        cutover.validate_query_result_consumer_projection(result)
    except RuntimeError as exc:
        raise RuntimeError("invalid_final_nats_query_result") from exc
    streams = cutover.build_critical_stream_rows(result.streams)
    expected = cutover.build_expected_durable_index()
    expected_pairs = {
        (expected_row.stream, expected_row.durable)
        for expected_row in expected.values()
    }
    observed_pairs = [
        (state.stream, state.durable) for state in result.consumers
    ]
    if (
        len(observed_pairs) != len(set(observed_pairs))
        or set(observed_pairs) != expected_pairs
    ):
        raise RuntimeError("final_nats_durable_set_not_qualified")
    durables, blocked = cutover.evaluate_existing_consumers(
        result.consumers,
        expected,
    )
    if blocked or len(durables) != len(expected_pairs):
        raise RuntimeError("final_nats_durable_set_not_qualified")
    if any(row.get("status") != "SAFE_ALREADY_ONE" for row in durables):
        raise RuntimeError("final_nats_durable_ack_window_not_qualified")
    return result, streams, durables


def _validate_external_lifecycle_capture(
    event_capture: dict[str, object],
    *,
    post_window_started_ns: int,
    post_window_ended_ns: int,
    app_up_authorized_ns: int,
    health_boundary_started_ns: int,
    requested_cutoff_ns: int,
    required_container_facts: Sequence[dict[str, object]],
) -> dict[str, object]:
    """Classify the live deployment lifecycle stream against exact phases."""

    allowlist = (*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER)
    coverage_started_ns = event_capture.get("coverage_started_ns")
    coverage_ended_ns = event_capture.get("coverage_ended_ns")
    validate_live_window_evidence(
        event_capture,
        expected_allowlist=allowlist,
        expected_start_ns=(
            coverage_started_ns
            if isinstance(coverage_started_ns, int)
            and not isinstance(coverage_started_ns, bool)
            else -1
        ),
        expected_cutoff_ns=requested_cutoff_ns,
    )
    if (
        isinstance(coverage_started_ns, bool)
        or not isinstance(coverage_started_ns, int)
        or isinstance(coverage_ended_ns, bool)
        or not isinstance(coverage_ended_ns, int)
        or not (
            0
            < coverage_started_ns
            <= post_window_started_ns
            <= post_window_ended_ns
            <= app_up_authorized_ns
            <= health_boundary_started_ns
            <= requested_cutoff_ns
            <= coverage_ended_ns
        )
    ):
        raise RuntimeError("deployment_lifecycle_monitor_boundary_invalid")

    final_ids: dict[str, str] = {}
    for row in required_container_facts:
        name = row.get("name")
        container_id = row.get("container_id")
        if (
            not isinstance(name, str)
            or name not in _KNOWN_APP_CONTAINERS
            or name in final_ids
            or not isinstance(container_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", container_id)
        ):
            raise RuntimeError("invalid_final_container_lifecycle_identity")
        final_ids[name] = container_id

    event_rows = event_capture.get("events")
    if not isinstance(event_rows, list):
        raise RuntimeError("invalid_deployment_lifecycle_capture")
    per_container: dict[str, list[dict[str, object]]] = {
        name: [] for name in final_ids
    }
    for event in event_rows:
        if not isinstance(event, dict):
            raise RuntimeError("invalid_deployment_lifecycle_capture")
        name = event.get("name")
        action = event.get("action")
        container_id = event.get("container_id")
        event_time = event.get("time_nano")
        if (
            not isinstance(name, str)
            or not isinstance(action, str)
            or not isinstance(container_id, str)
            or isinstance(event_time, bool)
            or not isinstance(event_time, int)
        ):
            raise RuntimeError("invalid_deployment_lifecycle_capture")
        if name == _NATS_CONTAINER:
            raise RuntimeError("nats_lifecycle_changed_since_post_preflight")
        if name not in final_ids:
            raise RuntimeError("non_profile_app_lifecycle_changed_during_deployment")
        if event_time <= post_window_ended_ns or event_time < app_up_authorized_ns:
            raise RuntimeError("app_lifecycle_changed_before_app_up_authorization")
        if event_time >= health_boundary_started_ns:
            raise RuntimeError("container_lifecycle_changed_after_health_boundary")
        if container_id != final_ids[name]:
            raise RuntimeError("container_identity_changed_during_app_startup")
        per_container[name].append(event)

    permitted_health_actions = {
        "health_status: starting",
        "health_status: healthy",
    }
    for name, events in per_container.items():
        actions = [str(event["action"]) for event in events]
        if actions.count("create") != 1 or actions.count("start") != 1:
            raise RuntimeError(f"incomplete_container_startup_lifecycle:{name}")
        create_index = actions.index("create")
        start_index = actions.index("start")
        if create_index >= start_index:
            raise RuntimeError(f"invalid_container_startup_lifecycle_order:{name}")
        health_actions = [action for action in actions if action.startswith("health_status")]
        if (
            any(action not in permitted_health_actions for action in health_actions)
            or health_actions.count("health_status: starting") > 1
            or health_actions.count("health_status: healthy") > 1
            or any(actions.index(action) <= start_index for action in health_actions)
        ):
            raise RuntimeError(f"invalid_container_health_lifecycle:{name}")
        archive_count = actions.count("archive-path")
        expected_archive_count = 1 if name in _COLLECTOR_HEARTBEATS else 0
        if archive_count != expected_archive_count:
            raise RuntimeError(f"invalid_container_archive_lifecycle:{name}")
        permitted = {"create", "start", "archive-path", *permitted_health_actions}
        if any(action not in permitted for action in actions):
            raise RuntimeError(f"unexpected_container_lifecycle_action:{name}")
        if any(
            actions.index(action) <= start_index
            for action in actions
            if action == "archive-path"
        ):
            raise RuntimeError(f"invalid_container_archive_lifecycle:{name}")

    return {
        "status": "PASSED_WITH_TRUST_BOUNDARY",
        "complete": False,
        "coverage_status": "BOUNDED_OBSERVED",
        "monitor_coverage_started_ns": coverage_started_ns,
        "post_preflight_window_started_ns": post_window_started_ns,
        "post_preflight_window_ended_ns": post_window_ended_ns,
        "app_up_authorized_ns": app_up_authorized_ns,
        "health_boundary_started_ns": health_boundary_started_ns,
        "requested_cutoff_ns": requested_cutoff_ns,
        "transport_coverage_ended_ns": coverage_ended_ns,
        "required_startup_sequences": {
            name: [str(event["action"]) for event in events]
            for name, events in per_container.items()
        },
        "event_capture": event_capture,
    }


def _safe_container_environment(
    raw_environment: object,
    *,
    name: str,
    expected_generation: str,
    expected_commit: str,
    expected_profile: str,
) -> dict[str, str]:
    if not isinstance(raw_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_environment.items()
    ):
        raise RuntimeError(f"invalid_container_environment:{name}")
    expected = {
        "AATS_RUNTIME_READINESS_GENERATION": expected_generation,
        "AATS_DEPLOYED_GIT_COMMIT": expected_commit,
        "AATS_PROFILE": expected_profile,
    }
    if set(raw_environment) != set(expected):
        raise RuntimeError(f"invalid_container_environment:{name}")
    observed = dict(raw_environment)
    for key, expected_value in expected.items():
        if observed.get(key) != expected_value:
            raise RuntimeError(f"container_environment_mismatch:{name}:{key}")
    return observed


def _gateway_published_bindings_from_inspect(
    published_ports: object,
) -> list[dict[str, str]]:
    if not isinstance(published_ports, dict):
        raise RuntimeError("invalid_gateway_port_mapping")
    result: list[dict[str, str]] = []
    for container_port, bindings in published_ports.items():
        if not isinstance(container_port, str) or not _CONTAINER_PORT_RE.fullmatch(
            container_port
        ):
            raise RuntimeError("invalid_gateway_container_port")
        if int(container_port.split("/", maxsplit=1)[0]) > 65535:
            raise RuntimeError("invalid_gateway_container_port")
        if not isinstance(bindings, list):
            raise RuntimeError("invalid_gateway_port_bindings")
        for binding in bindings:
            if not isinstance(binding, dict):
                raise RuntimeError("invalid_gateway_port_binding")
            host_ip = binding.get("HostIp")
            host_port = binding.get("HostPort")
            if host_ip not in _LOOPBACK_HOST_IPS:
                raise RuntimeError("gateway_binding_not_loopback")
            if (
                not isinstance(host_port, str)
                or not host_port.isdigit()
                or not (1 <= int(host_port) <= 65535)
            ):
                raise RuntimeError("invalid_gateway_host_port")
            result.append(
                {
                    "container_port": container_port,
                    "host_ip": host_ip,
                    "host_port": host_port,
                }
            )
    if not result:
        raise RuntimeError("gateway_has_no_published_loopback_binding")
    return sorted(
        result,
        key=lambda item: (item["container_port"], item["host_ip"], item["host_port"]),
    )


def _container_snapshot(
    names: Sequence[str],
    *,
    expected_image_id: str,
    expected_generation: str,
    expected_commit: str,
    expected_profile: str,
    expected_target_manifest_sha256: str,
    run: CommandRunner,
    health_window_started_ns: int | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    if not names or len(set(names)) != len(names):
        raise ValueError("invalid_required_container_list")
    if not _SHA256_RE.fullmatch(expected_target_manifest_sha256):
        raise ValueError("invalid_expected_nats_target_manifest_sha256")
    for name in names:
        if not _CONTAINER_RE.fullmatch(name):
            raise ValueError("invalid_required_container_name")
    raw = run(
        ("docker", "inspect", "--format", _APP_CONTAINER_INSPECT_TEMPLATE, *names),
        None,
    )
    try:
        payload = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid_container_inspect_json") from exc
    if len(payload) != len(names):
        raise RuntimeError("incomplete_container_inspect")

    by_name: dict[str, dict[str, object]] = {}
    gateway_bindings: list[dict[str, str]] | None = None
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("invalid_container_inspect_json")
        raw_name = item.get("Name")
        if not isinstance(raw_name, str):
            raise RuntimeError("invalid_container_inspect_name")
        name = raw_name.removeprefix("/")
        if name not in names or name in by_name:
            raise RuntimeError("unexpected_container_inspect_name")
        if item.get("ComposeProject") != "aats-dev":
            raise RuntimeError(f"container_compose_project_mismatch:{name}")
        if item.get("ComposeService") != name:
            raise RuntimeError(f"container_compose_service_mismatch:{name}")
        target_manifest_sha256 = item.get("NatsTargetManifestSha256")
        if (
            not isinstance(target_manifest_sha256, str)
            or not _SHA256_RE.fullmatch(target_manifest_sha256)
        ):
            raise RuntimeError(f"invalid_nats_target_manifest_sha256:{name}")
        if target_manifest_sha256 != expected_target_manifest_sha256:
            raise RuntimeError(f"nats_target_manifest_sha256_mismatch:{name}")
        container_id = _required_text(
            item.get("Id"), f"invalid_container_id:{name}"
        )
        image_id = _required_text(
            item.get("Image"), f"invalid_container_image_id:{name}"
        )
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise RuntimeError(f"invalid_container_id:{name}")
        if not _IMAGE_RE.fullmatch(image_id):
            raise RuntimeError(f"invalid_container_image_id:{name}")
        if image_id != expected_image_id:
            raise RuntimeError(f"container_image_not_current_build:{name}")

        state = item.get("State")
        if not isinstance(state, dict):
            raise RuntimeError(f"invalid_container_runtime_state:{name}")
        status = state.get("Status")
        health_state = state.get("Health")
        health = health_state.get("Status") if isinstance(health_state, dict) else None
        if not isinstance(health_state, dict):
            raise RuntimeError(f"invalid_container_health_state:{name}")
        failing_streak = _required_non_negative_int(
            health_state.get("FailingStreak"),
            f"invalid_container_health_failing_streak:{name}",
        )
        health_checks = health_state.get("Checks")
        if not isinstance(health_checks, list) or not health_checks:
            raise RuntimeError(f"invalid_container_health_log:{name}")
        health_exit_codes: list[int] = []
        health_success_observed_after_boundary = health_window_started_ns is None
        for check in health_checks:
            if not isinstance(check, dict):
                raise RuntimeError(f"invalid_container_health_log:{name}")
            exit_code = _required_non_negative_int(
                check.get("ExitCode"),
                f"invalid_container_health_exit_code:{name}",
            )
            check_started_ns = _rfc3339_nanoseconds(
                check.get("Start"),
                error=f"invalid_container_health_started_at:{name}",
            )
            check_ended_ns = _rfc3339_nanoseconds(
                check.get("End"),
                error=f"invalid_container_health_ended_at:{name}",
            )
            if check_ended_ns < check_started_ns:
                raise RuntimeError(f"invalid_container_health_window:{name}")
            if (
                health_window_started_ns is not None
                and check_ended_ns >= health_window_started_ns
            ):
                if exit_code != 0:
                    raise RuntimeError(
                        f"container_health_failed_after_boundary:{name}"
                    )
                health_success_observed_after_boundary = True
            health_exit_codes.append(exit_code)
        if not health_success_observed_after_boundary:
            raise RuntimeError(
                f"container_health_not_observed_after_boundary:{name}"
            )
        last_health_exit_code = _required_non_negative_int(
            health_exit_codes[-1],
            f"invalid_container_health_exit_code:{name}",
        )
        if (
            status != "running"
            or health != "healthy"
            or failing_streak != 0
            or last_health_exit_code != 0
        ):
            raise RuntimeError(f"required_container_not_healthy:{name}:{status}:{health}")
        started_at = _required_text(
            state.get("StartedAt"), f"invalid_container_started_at:{name}"
        )
        restart_count = _required_non_negative_int(
            item.get("RestartCount"), f"invalid_container_restart_count:{name}"
        )
        if restart_count != 0:
            raise RuntimeError(f"required_container_restarted_during_deployment:{name}")
        safe_environment = _safe_container_environment(
            item.get("SafeEnvironment"),
            name=name,
            expected_generation=expected_generation,
            expected_commit=expected_commit,
            expected_profile=expected_profile,
        )
        by_name[name] = {
            "name": name,
            "container_id": container_id,
            "image_id": image_id,
            "status": status,
            "health": health,
            "health_failing_streak": failing_streak,
            "last_health_exit_code": last_health_exit_code,
            "health_success_observed_after_boundary": (
                health_success_observed_after_boundary
                if health_window_started_ns is not None
                else None
            ),
            "started_at": started_at,
            "restart_count": restart_count,
            "nats_target_manifest_sha256": target_manifest_sha256,
            "safe_environment": safe_environment,
        }
        if name == "aats-gateway":
            gateway_bindings = _gateway_published_bindings_from_inspect(
                item.get("Ports")
            )

    if set(by_name) != set(names):
        raise RuntimeError("incomplete_container_inspect")
    if gateway_bindings is None:
        raise RuntimeError("required_gateway_missing")
    return [by_name[name] for name in names], gateway_bindings


def _app_runtime_snapshot_fingerprint(
    container_facts: Sequence[dict[str, object]],
    gateway_bindings: Sequence[dict[str, str]],
) -> str:
    stable_container_facts = []
    for row in container_facts:
        stable_row = dict(row)
        stable_row.pop("health_success_observed_after_boundary", None)
        stable_container_facts.append(stable_row)
    ordered_containers = sorted(
        stable_container_facts,
        key=lambda row: str(row.get("name")),
    )
    ordered_bindings = sorted(
        gateway_bindings,
        key=lambda row: (
            str(row.get("container_port")),
            str(row.get("host_ip")),
            str(row.get("host_port")),
        ),
    )
    canonical = json.dumps(
        {
            "containers": ordered_containers,
            "gateway_bindings": ordered_bindings,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _collector_heartbeat_fact(
    name: str,
    *,
    heartbeat_epoch: int,
    now: datetime | None = None,
    clock: Clock = _utc_now,
    observation_phase: str = "current",
    observation_method: str = "supplied_epoch",
) -> dict[str, object]:
    if observation_phase not in {"current", "pre_health_boundary"}:
        raise ValueError("invalid_collector_heartbeat_observation_phase")
    if observation_method not in {"supplied_epoch", "docker_archive_mtime"}:
        raise ValueError("invalid_collector_heartbeat_observation_method")
    heartbeat_path = _COLLECTOR_HEARTBEATS[name]
    try:
        if isinstance(heartbeat_epoch, bool) or not isinstance(heartbeat_epoch, int):
            raise ValueError
        heartbeat_at = datetime.fromtimestamp(heartbeat_epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError(f"invalid_collector_heartbeat_epoch:{name}") from exc
    observed_at = now or clock()
    raw_age_seconds = (observed_at - heartbeat_at).total_seconds()
    if raw_age_seconds < -_COLLECTOR_MAX_FUTURE_SKEW_SECONDS:
        raise RuntimeError(f"collector_heartbeat_in_future:{name}:{raw_age_seconds:.3f}")
    age_seconds = max(0.0, raw_age_seconds)
    if age_seconds >= _COLLECTOR_MAX_HEARTBEAT_AGE_SECONDS:
        raise RuntimeError(f"collector_heartbeat_stale:{name}:{age_seconds:.3f}")
    return {
        "name": name,
        "heartbeat_path": heartbeat_path,
        "heartbeat_at": heartbeat_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "observation_phase": observation_phase,
        "observation_method": observation_method,
        "heartbeat_age_seconds": round(age_seconds, 3),
        "fresh": True,
    }


def _recompute_preflight_durable_row(
    row: object,
    *,
    expected: object,
    cutover: object,
) -> dict[str, object]:
    """Rebuild one v3 row from its raw fields and reject self-reported safety."""

    error = "nats_cutover_preflight_durable_projection_invalid"
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
        raise ValueError(error)
    identity = row.get("identity")
    if not isinstance(identity, dict) or identity != {
        "stream": expected.stream,
        "durable": expected.durable,
        "role": expected.role,
        "topic": expected.topic,
        "delivery_semantics": expected.delivery_semantics,
    }:
        raise ValueError(error)
    try:
        _parse_aware_timestamp(row.get("created"))
    except ValueError as exc:
        raise ValueError(error) from exc

    immutable = row.get("immutable_config")
    mutable = row.get("mutable_config")
    if not isinstance(immutable, dict) or set(immutable) != {"actual", "expected"}:
        raise ValueError(error)
    if not isinstance(mutable, dict) or set(mutable) != {"actual", "expected"}:
        raise ValueError(error)
    actual = immutable.get("actual")
    expected_config = immutable.get("expected")
    canonical_expected = cutover._expected_consumer_config(expected)
    if expected_config != canonical_expected or not isinstance(actual, dict):
        raise ValueError(error)
    if set(actual) != set(canonical_expected) | {"filter_subjects"}:
        raise ValueError(error)
    actual_mutable = mutable.get("actual")
    if (
        mutable.get("expected")
        != cutover._expected_consumer_mutable_config(expected)
        or not isinstance(actual_mutable, dict)
        or set(actual_mutable) != {"ack_wait_seconds", "max_deliver"}
    ):
        raise ValueError(error)

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
        raise ValueError(error)
    for value in cursor.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(error)
    current_window = window.get("current_max_ack_pending")
    target_window = window.get("target_max_ack_pending")
    num_ack_pending = outstanding.get("num_ack_pending")
    if (
        isinstance(current_window, bool)
        or not isinstance(current_window, int)
        or isinstance(target_window, bool)
        or target_window != 1
        or isinstance(num_ack_pending, bool)
        or not isinstance(num_ack_pending, int)
        or num_ack_pending < 0
    ):
        raise ValueError(error)

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
        raise ValueError(error)

    state = cutover.ConsumerState(
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
        cursor=cutover.ConsumerCursor(**cursor),
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
    recomputed = cutover.evaluate_consumer(state, expected)
    if recomputed != row:
        raise ValueError(error)
    return recomputed


def _nats_cutover_preflight_reference(
    *,
    repo_root: Path,
    path: Path,
    runtime_readiness_generation: str,
    deployment_lock_id: str,
    deployed_commit: str,
    expected_stage: str,
) -> tuple[dict[str, object], dict[str, object]]:
    allowed_root = (repo_root / "artifacts" / "deployments").resolve()
    candidate = path if path.is_absolute() else repo_root / path
    if candidate.is_symlink():
        raise ValueError("nats_cutover_preflight_symlink_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
        relative_to_artifacts = resolved.relative_to(allowed_root)
        relative_to_repo = resolved.relative_to(repo_root.resolve())
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise ValueError("invalid_nats_cutover_preflight_path") from exc
    if not resolved.is_file():
        raise ValueError("invalid_nats_cutover_preflight_path")
    raw = resolved.read_bytes()
    if len(raw) > _MAX_NATS_CUTOVER_EVIDENCE_BYTES:
        raise ValueError("nats_cutover_preflight_too_large")
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_nats_cutover_preflight_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_nats_cutover_preflight_json")
    if payload.get("schema_version") != _NATS_CUTOVER_PREFLIGHT_SCHEMA:
        raise ValueError("invalid_nats_cutover_preflight_schema")
    if payload.get("stage") != expected_stage:
        raise ValueError("nats_cutover_preflight_stage_mismatch")
    if payload.get("generation") != runtime_readiness_generation:
        raise ValueError("nats_cutover_preflight_generation_mismatch")
    if payload.get("deployment_lock_id") != deployment_lock_id:
        raise ValueError("nats_cutover_preflight_lock_mismatch")
    if payload.get("deployed_commit") != deployed_commit:
        raise ValueError("nats_cutover_preflight_commit_mismatch")
    if (
        payload.get("status") != "PASSED_WITH_TRUST_BOUNDARY"
        or payload.get("operation") != "READ_ONLY"
        or payload.get("mutations_performed") != []
    ):
        raise ValueError("nats_cutover_preflight_not_passed")
    nats_bootstrap = _required_nats_bootstrap(payload.get("nats_bootstrap"))
    nats_query_fingerprint = payload.get("nats_query_fingerprint")
    if not isinstance(nats_query_fingerprint, str) or not _SHA256_RE.fullmatch(
        nats_query_fingerprint
    ):
        raise ValueError("invalid_nats_cutover_query_fingerprint")
    if (
        expected_stage == "pre_full_down"
        and nats_query_fingerprint != nats_bootstrap["baseline_fingerprint"]
    ):
        raise ValueError("nats_cutover_baseline_fingerprint_mismatch")
    query = payload.get("query")
    if not isinstance(query, dict) or query.get("complete") is not True:
        raise ValueError("nats_cutover_preflight_query_incomplete")
    durable_rows = payload.get("durables")
    critical_streams = payload.get("critical_streams")
    if not isinstance(critical_streams, list) or not isinstance(durable_rows, list):
        raise ValueError("nats_cutover_preflight_snapshot_incomplete")
    if payload.get("unexpected_durables") != []:
        raise ValueError("nats_cutover_preflight_unexpected_durables")
    if payload.get("missing_declared_durables") != []:
        raise ValueError("nats_cutover_preflight_missing_declared_durables")
    from scripts import check_nats_durable_cutover as cutover

    expected_durables = cutover.build_expected_durable_index()
    expected_by_pair = {
        (row.stream, row.durable): row for row in expected_durables.values()
    }
    expected_pairs = set(expected_by_pair)
    observed_pairs: set[tuple[str, str]] = set()
    event_count = 0
    non_event_count = 0
    for row in durable_rows:
        if not isinstance(row, dict):
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
        identity = row.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
        stream = identity.get("stream")
        durable = identity.get("durable")
        semantics = identity.get("delivery_semantics")
        if not isinstance(stream, str) or not isinstance(durable, str):
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
        pair = (stream, durable)
        if pair in observed_pairs:
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
        observed_pairs.add(pair)
        expected = expected_by_pair.get(pair)
        if expected is None:
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
        _recompute_preflight_durable_row(
            row,
            expected=expected,
            cutover=cutover,
        )
        if semantics == "event":
            event_count += 1
            allowed_statuses = {"SAFE_TO_SHRINK", "SAFE_ALREADY_ONE"}
        elif semantics in {"snapshot", "transient"}:
            non_event_count += 1
            allowed_statuses = {
                "SAFE_TO_SHRINK",
                "SAFE_TO_RECONCILE_IN_PLACE",
                "SAFE_REBUILDABLE_NON_EVENT",
                "SAFE_ALREADY_ONE",
            }
        else:
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
        if row.get("status") not in allowed_statuses or row.get("blockers") != []:
            raise ValueError("nats_cutover_preflight_durable_projection_invalid")
    if nats_bootstrap["mode"] == "existing_container_preserved":
        if observed_pairs != expected_pairs:
            raise ValueError("nats_cutover_preflight_durable_projection_incomplete")
    elif observed_pairs:
        raise ValueError("nats_cutover_fresh_install_has_existing_durables")
    expected_query = {
        "complete": True,
        "streams_scanned": len(critical_streams),
        "consumers_scanned": len(durable_rows),
        "declared_durables_found": len(durable_rows),
        "critical_all_durables_found": event_count,
        "known_non_event_durables_found": non_event_count,
        "critical_streams_found": len(critical_streams),
    }
    if (
        query != expected_query
        or query.get("critical_all_durables_found") != event_count
        or query.get("known_non_event_durables_found") != non_event_count
        or payload.get("blocker_classes") != []
    ):
        raise ValueError("nats_cutover_preflight_durable_projection_invalid")
    target_stream_manifest = _required_target_stream_manifest(
        payload.get("target_stream_manifest")
    )
    reported_stream_compliance = _required_stream_target_compliance(
        payload.get("stream_target_compliance"),
        target_sha256=str(target_stream_manifest["sha256"]),
    )
    try:
        recomputed_stream_compliance, stream_blocked = (
            cutover.evaluate_stream_target(
                actual_streams=critical_streams,
                target_manifest=target_stream_manifest,
                bootstrap_mode=nats_bootstrap["mode"],
            )
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("nats_cutover_stream_projection_invalid") from exc
    if stream_blocked or reported_stream_compliance != recomputed_stream_compliance:
        raise ValueError("nats_cutover_stream_projection_invalid")
    try:
        cutover.validate_preflight_snapshot_projection(
            payload,
            bootstrap_mode=nats_bootstrap["mode"],
        )
    except RuntimeError as exc:
        raise ValueError("nats_cutover_preflight_snapshot_invalid") from exc
    quiescence = payload.get("app_quiescence")
    if (
        not isinstance(quiescence, dict)
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
    ):
        raise ValueError("nats_cutover_preflight_quiescence_not_passed")
    continuity = payload.get("continuity")
    expected_continuity_status = (
        "BASELINE_CAPTURED" if expected_stage == "pre_full_down" else "PASSED"
    )
    passive_retention_trims = (
        continuity.get("passive_retention_trims")
        if isinstance(continuity, dict)
        else None
    )
    if (
        not isinstance(continuity, dict)
        or continuity.get("complete") is not True
        or continuity.get("status") != expected_continuity_status
        or not isinstance(passive_retention_trims, list)
        or (
            expected_stage == "pre_full_down"
            and passive_retention_trims != []
        )
        or any(
            not isinstance(trim, dict)
            or set(trim)
            != {
                "stream",
                "delta",
                "messages_removed",
                "bytes_removed",
                "first_seq_advanced",
                "trust_boundary",
            }
            or not isinstance(trim.get("stream"), str)
            or not trim.get("stream")
            or isinstance(trim.get("delta"), bool)
            or not isinstance(trim.get("delta"), int)
            or trim.get("delta", 0) <= 0
            or trim.get("messages_removed") != trim.get("delta")
            or trim.get("first_seq_advanced") != trim.get("delta")
            or isinstance(trim.get("bytes_removed"), bool)
            or not isinstance(trim.get("bytes_removed"), int)
            or trim.get("bytes_removed", 0) <= 0
            or trim.get("trust_boundary")
            != cutover._PASSIVE_RETENTION_TRIM_TRUST_BOUNDARY
            for trim in passive_retention_trims
        )
        or continuity.get("violations") != []
    ):
        raise ValueError("nats_cutover_preflight_continuity_not_passed")
    checked_at_utc = payload.get("checked_at_utc")
    checked_at = _parse_aware_timestamp(checked_at_utc)
    window_started_ns = quiescence.get("window_started_ns")
    window_ended_ns = quiescence.get("window_ended_ns")
    if (
        isinstance(window_started_ns, bool)
        or not isinstance(window_started_ns, int)
        or isinstance(window_ended_ns, bool)
        or not isinstance(window_ended_ns, int)
        or not (0 < window_started_ns <= window_ended_ns)
    ):
        raise ValueError("nats_cutover_preflight_time_window_invalid")
    try:
        window_started_at = _parse_aware_timestamp(
            quiescence.get("window_started_at_utc")
        )
        window_ended_at = _parse_aware_timestamp(
            quiescence.get("window_ended_at_utc")
        )
    except ValueError as exc:
        raise ValueError("nats_cutover_preflight_time_window_invalid") from exc
    if not checked_at <= window_started_at <= window_ended_at:
        raise ValueError("nats_cutover_preflight_time_window_invalid")
    if (
        abs(int(window_started_at.timestamp() * 1_000_000_000) - window_started_ns)
        > 1_000
        or abs(int(window_ended_at.timestamp() * 1_000_000_000) - window_ended_ns)
        > 1_000
    ):
        raise ValueError("nats_cutover_preflight_time_window_invalid")
    validate_live_window_evidence(
        quiescence.get("event_capture"),
        expected_allowlist=(*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER),
        expected_start_ns=window_started_ns,
        expected_cutoff_ns=window_ended_ns,
    )
    reference = {
        "path": relative_to_repo.as_posix(),
        "sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "schema_version": _NATS_CUTOVER_PREFLIGHT_SCHEMA,
        "stage": expected_stage,
        "status": "PASSED_WITH_TRUST_BOUNDARY",
        "checked_at_utc": checked_at_utc,
        "window_started_at_utc": quiescence.get("window_started_at_utc"),
        "window_ended_at_utc": quiescence.get("window_ended_at_utc"),
        "window_started_ns": window_started_ns,
        "window_ended_ns": window_ended_ns,
        "generation": runtime_readiness_generation,
        "deployment_lock_id": deployment_lock_id,
        "deployed_commit": deployed_commit,
        "app_quiescence_status": "PASSED_WITH_TRUST_BOUNDARY",
        "streams_scanned": query.get("streams_scanned"),
        "consumers_scanned": query.get("consumers_scanned"),
        "declared_durables_found": query.get("declared_durables_found"),
        "critical_all_durables_found": query.get(
            "critical_all_durables_found"
        ),
        "known_non_event_durables_found": query.get(
            "known_non_event_durables_found"
        ),
        "critical_streams_found": query.get("critical_streams_found"),
        "artifact_relative_path": relative_to_artifacts.as_posix(),
        "nats_bootstrap": nats_bootstrap,
        "nats_query_fingerprint": nats_query_fingerprint,
        "target_stream_manifest_sha256": target_stream_manifest["sha256"],
    }
    return reference, payload


def _validate_nats_cutover_preflight_chain(
    *,
    before_reference: dict[str, object],
    before_payload: dict[str, object],
    after_payload: dict[str, object],
) -> None:
    if before_payload.get("previous_preflight") is not None:
        raise ValueError("nats_cutover_baseline_has_previous_preflight")
    previous = after_payload.get("previous_preflight")
    if not isinstance(previous, dict):
        raise ValueError("nats_cutover_previous_preflight_missing")
    for key in (
        "path",
        "sha256",
        "schema_version",
        "stage",
        "generation",
        "deployment_lock_id",
        "deployed_commit",
        "checked_at_utc",
        "window_started_at_utc",
        "window_ended_at_utc",
        "window_started_ns",
        "window_ended_ns",
        "nats_bootstrap",
        "nats_query_fingerprint",
        "target_stream_manifest_sha256",
    ):
        if previous.get(key) != before_reference.get(key):
            raise ValueError(f"nats_cutover_previous_preflight_mismatch:{key}")
    continuity = after_payload.get("continuity")
    if (
        not isinstance(continuity, dict)
        or continuity.get("baseline_sha256") != before_reference.get("sha256")
    ):
        raise ValueError("nats_cutover_continuity_hash_mismatch")
    from scripts import check_nats_durable_cutover as cutover

    try:
        recomputed_continuity = cutover.evaluate_cutover_continuity(
            previous_streams=before_payload.get("critical_streams"),
            current_streams=after_payload.get("critical_streams"),
            previous_durables=before_payload.get("durables"),
            current_durables=after_payload.get("durables"),
            baseline_sha256=str(before_reference.get("sha256")),
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("nats_cutover_continuity_recomputation_failed") from exc
    if (
        recomputed_continuity.get("status") != "PASSED"
        or recomputed_continuity.get("complete") is not True
        or recomputed_continuity.get("violations") != []
    ):
        raise ValueError("nats_cutover_continuity_recomputed_not_passed")
    for key in (
        "status",
        "complete",
        "baseline_sha256",
        "streams_checked",
        "durables_checked",
        "passive_retention_trims",
        "violations",
    ):
        if continuity.get(key) != recomputed_continuity.get(key):
            raise ValueError(f"nats_cutover_continuity_artifact_mismatch:{key}")
    if after_payload.get("nats_bootstrap") != before_reference.get(
        "nats_bootstrap"
    ):
        raise ValueError("nats_cutover_bootstrap_provenance_mismatch")
    if after_payload.get("nats_query_fingerprint") == before_reference.get(
        "nats_query_fingerprint"
    ):
        raise ValueError("nats_cutover_post_container_not_recreated")
    before_target = _required_target_stream_manifest(
        before_payload.get("target_stream_manifest")
    )
    after_target = _required_target_stream_manifest(
        after_payload.get("target_stream_manifest")
    )
    if (
        before_target["sha256"] != after_target["sha256"]
        or before_target["streams"] != after_target["streams"]
    ):
        raise ValueError("nats_cutover_target_stream_manifest_mismatch")
    before_checked_at = _parse_aware_timestamp(
        before_reference.get("checked_at_utc")
    )
    after_checked_at = _parse_aware_timestamp(after_payload.get("checked_at_utc"))
    if after_checked_at < before_checked_at:
        raise ValueError("nats_cutover_preflight_time_rollback")
    after_quiescence = after_payload.get("app_quiescence")
    if not isinstance(after_quiescence, dict):
        raise ValueError("nats_cutover_preflight_quiescence_not_passed")
    before_window_ended_ns = before_reference.get("window_ended_ns")
    after_window_started_ns = after_quiescence.get("window_started_ns")
    after_window_ended_ns = after_quiescence.get("window_ended_ns")
    if (
        isinstance(before_window_ended_ns, bool)
        or not isinstance(before_window_ended_ns, int)
        or isinstance(after_window_started_ns, bool)
        or not isinstance(after_window_started_ns, int)
        or isinstance(after_window_ended_ns, bool)
        or not isinstance(after_window_ended_ns, int)
        or not (
            0
            < before_window_ended_ns
            <= after_window_started_ns
            <= after_window_ended_ns
        )
    ):
        raise ValueError("nats_cutover_preflight_time_window_overlap")
    after_window_started_at = _parse_aware_timestamp(
        after_quiescence.get("window_started_at_utc")
    )
    after_window_ended_at = _parse_aware_timestamp(
        after_quiescence.get("window_ended_at_utc")
    )
    if not (
        after_checked_at <= after_window_started_at <= after_window_ended_at
    ):
        raise ValueError("nats_cutover_preflight_time_window_overlap")


def build_evidence(
    *,
    repo_root: Path,
    profile: str,
    overlay: str,
    schema_job_status: str,
    runtime_readiness_generation: str,
    deployment_lock_id: str,
    deployed_commit: str,
    required_containers: Sequence[str],
    nats_stream_probe: NatsStreamProbe,
    lifecycle_monitor_control_dir: Path,
    lifecycle_monitor_token: str,
    app_up_authorized_ns: int,
    health_boundary_started_ns: int,
    health_boundary_app_fingerprint: str,
    collector_heartbeat_epochs: dict[str, int],
    nats_cutover_preflight_before_path: Path | None = None,
    nats_cutover_preflight_after_path: Path | None = None,
    run: CommandRunner = _run_command,
    generated_at: datetime | None = None,
    clock: Clock = _utc_now,
    nanosecond_clock: NanosecondClock = time.time_ns,
    lifecycle_monitor_sealer: LifecycleMonitorSealer = seal_external_monitor,
) -> dict[str, object]:
    if profile not in _SIMULATION_PROFILES:
        raise ValueError("deployment_evidence_requires_simulation_profile")
    if schema_job_status != "passed":
        raise ValueError("schema_job_not_passed")
    if not _READINESS_GENERATION_RE.fullmatch(runtime_readiness_generation):
        raise ValueError("invalid_runtime_readiness_generation")
    if not _READINESS_GENERATION_RE.fullmatch(deployment_lock_id):
        raise ValueError("invalid_deployment_lock_id")
    if not _COMMIT_RE.fullmatch(deployed_commit):
        raise ValueError("invalid_deployed_commit")
    if len(set(required_containers)) != len(required_containers):
        raise ValueError("duplicate_required_container")
    required_container_names = _REQUIRED_CONTAINERS_BY_PROFILE[profile]
    if set(required_containers) != set(required_container_names):
        raise ValueError("required_container_set_mismatch")
    if (
        isinstance(app_up_authorized_ns, bool)
        or not isinstance(app_up_authorized_ns, int)
        or app_up_authorized_ns <= 0
    ):
        raise ValueError("invalid_app_up_authorized_ns")
    if (
        isinstance(health_boundary_started_ns, bool)
        or not isinstance(health_boundary_started_ns, int)
        or health_boundary_started_ns <= 0
    ):
        raise ValueError("invalid_health_boundary_started_ns")
    if not isinstance(health_boundary_app_fingerprint, str) or not _SHA256_RE.fullmatch(
        health_boundary_app_fingerprint
    ):
        raise ValueError("invalid_health_boundary_app_fingerprint")
    if not isinstance(lifecycle_monitor_control_dir, Path):
        raise ValueError("invalid_lifecycle_monitor_control_dir")
    if not isinstance(lifecycle_monitor_token, str) or not _READINESS_GENERATION_RE.fullmatch(
        lifecycle_monitor_token
    ):
        raise ValueError("invalid_lifecycle_monitor_token")
    expected_collectors = {
        name for name in required_container_names if name in _COLLECTOR_HEARTBEATS
    }
    if (
        not isinstance(collector_heartbeat_epochs, dict)
        or set(collector_heartbeat_epochs) != expected_collectors
    ):
        raise ValueError("collector_heartbeat_epoch_set_mismatch")

    if (
        nats_cutover_preflight_before_path is None
        or nats_cutover_preflight_after_path is None
    ):
        raise ValueError("nats_cutover_preflight_pair_required")
    before_reference, before_payload = _nats_cutover_preflight_reference(
        repo_root=repo_root,
        path=nats_cutover_preflight_before_path,
        runtime_readiness_generation=runtime_readiness_generation,
        deployment_lock_id=deployment_lock_id,
        deployed_commit=deployed_commit,
        expected_stage="pre_full_down",
    )
    after_reference, after_payload = _nats_cutover_preflight_reference(
        repo_root=repo_root,
        path=nats_cutover_preflight_after_path,
        runtime_readiness_generation=runtime_readiness_generation,
        deployment_lock_id=deployment_lock_id,
        deployed_commit=deployed_commit,
        expected_stage="post_infra_pre_app_up",
    )
    _validate_nats_cutover_preflight_chain(
        before_reference=before_reference,
        before_payload=before_payload,
        after_payload=after_payload,
    )
    final_target_manifest = _required_target_stream_manifest(
        after_payload.get("target_stream_manifest")
    )
    after_quiescence = after_payload.get("app_quiescence")
    if not isinstance(after_quiescence, dict):
        raise ValueError("nats_cutover_preflight_quiescence_not_passed")
    post_preflight_window_started_ns = after_reference.get("window_started_ns")
    post_preflight_window_ended_ns = after_reference.get("window_ended_ns")
    if (
        isinstance(post_preflight_window_started_ns, bool)
        or not isinstance(post_preflight_window_started_ns, int)
        or isinstance(post_preflight_window_ended_ns, bool)
        or not isinstance(post_preflight_window_ended_ns, int)
    ):
        raise ValueError("nats_cutover_preflight_time_window_invalid")

    commit = run(("git", "rev-parse", "HEAD"), repo_root)
    base_image_id_before = run(
        ("docker", "image", "inspect", "aats-base:dev", "--format", "{{.Id}}"),
        None,
    )
    if not _COMMIT_RE.fullmatch(commit) or commit != deployed_commit:
        raise RuntimeError("invalid_deployed_commit")
    if not _IMAGE_RE.fullmatch(base_image_id_before):
        raise RuntimeError("invalid_base_image_id")

    now = generated_at or clock()
    heartbeat_observed_at = datetime.fromtimestamp(
        health_boundary_started_ns / 1_000_000_000,
        tz=timezone.utc,
    )
    # Each mtime was captured exactly once immediately before the authoritative
    # health boundary.  Evaluate that immutable sample against the boundary,
    # which is the conservative upper bound on its actual read-time age.  The
    # final writer must remain observation-event-free from this boundary onward;
    # post-boundary continuity is established separately by the bounded Docker
    # health/lifecycle evidence below.
    collector_freshness = [
        _collector_heartbeat_fact(
            name,
            heartbeat_epoch=collector_heartbeat_epochs[name],
            now=heartbeat_observed_at,
            observation_phase="pre_health_boundary",
            observation_method="docker_archive_mtime",
        )
        for name in required_container_names
        if name in _COLLECTOR_HEARTBEATS
    ]
    window_started_ns = nanosecond_clock()
    if not (
        post_preflight_window_ended_ns
        <= health_boundary_started_ns
        <= window_started_ns
    ):
        raise RuntimeError("nats_post_preflight_time_after_final_window")
    nats_identity_before = _capture_nats_identity(run=run)
    nats_fingerprint_before = str(nats_identity_before["fingerprint"])
    if nats_identity_before["restart_count"] != 0:
        raise RuntimeError("final_nats_container_restarted")
    _capture_nats_health_snapshot(
        run=run,
        health_window_started_ns=health_boundary_started_ns,
        expected_container_id=str(nats_identity_before["container_id"]),
    )
    nats_volume_fingerprint_before = _capture_nats_volume_fingerprint(run=run)
    from scripts import check_nats_durable_cutover as cutover

    final_query_before, final_stream_rows, final_durable_rows = (
        _read_final_nats_state(nats_stream_probe)
    )
    final_stream_compliance, final_stream_blocked = cutover.evaluate_stream_target(
        actual_streams=final_stream_rows,
        target_manifest=final_target_manifest,
        bootstrap_mode=str(before_reference["nats_bootstrap"]["mode"]),
        require_fresh_empty=False,
    )
    if final_stream_blocked or final_stream_compliance.get("status") != "MATCHED":
        raise RuntimeError("final_nats_stream_target_not_matched")
    final_stream_projection, final_stream_projection_sha256 = (
        _stream_target_projection(final_stream_rows)
    )
    final_durable_projection, final_durable_projection_sha256 = (
        _durable_qualification_projection(final_durable_rows)
    )
    container_facts_before, gateway_bindings_before = _container_snapshot(
        required_container_names,
        expected_image_id=base_image_id_before,
        expected_generation=runtime_readiness_generation,
        expected_commit=deployed_commit,
        expected_profile=profile,
        expected_target_manifest_sha256=str(final_target_manifest["sha256"]),
        run=run,
        health_window_started_ns=health_boundary_started_ns,
    )
    if (
        _app_runtime_snapshot_fingerprint(
            container_facts_before,
            gateway_bindings_before,
        )
        != health_boundary_app_fingerprint
    ):
        raise RuntimeError("app_runtime_changed_since_health_boundary")
    container_facts_after, gateway_bindings_after = _container_snapshot(
        required_container_names,
        expected_image_id=base_image_id_before,
        expected_generation=runtime_readiness_generation,
        expected_commit=deployed_commit,
        expected_profile=profile,
        expected_target_manifest_sha256=str(final_target_manifest["sha256"]),
        run=run,
        health_window_started_ns=health_boundary_started_ns,
    )
    final_query_after, final_stream_rows_after, final_durable_rows_after = (
        _read_final_nats_state(nats_stream_probe)
    )
    # Close the second JetStream walk with fresh immutable runtime identities.
    # Sampling these before the query would leave a query-duration tail in
    # which NATS or the base tag could change without a postcondition check.
    nats_identity_after = _capture_nats_identity(run=run)
    nats_fingerprint_after = str(nats_identity_after["fingerprint"])
    _capture_nats_health_snapshot(
        run=run,
        health_window_started_ns=health_boundary_started_ns,
        expected_container_id=str(nats_identity_after["container_id"]),
    )
    nats_volume_fingerprint_after = _capture_nats_volume_fingerprint(run=run)
    base_image_id_after = run(
        ("docker", "image", "inspect", "aats-base:dev", "--format", "{{.Id}}"),
        None,
    )
    # The final app snapshot is a postcondition sampled after this logical
    # cutoff.  Taking the cutoff first avoids claiming an unobserved interval
    # between snapshot return and cutoff acquisition.
    window_ended_ns = nanosecond_clock()
    # Re-sample app health after the potentially slow second JetStream walk.
    # Otherwise a failed-and-recovered healthcheck during that query could be
    # absent from both the earlier app snapshot and Docker's unhealthy events
    # (which fire only after the configured retry threshold).
    container_facts_final, gateway_bindings_final = _container_snapshot(
        required_container_names,
        expected_image_id=base_image_id_before,
        expected_generation=runtime_readiness_generation,
        expected_commit=deployed_commit,
        expected_profile=profile,
        expected_target_manifest_sha256=str(final_target_manifest["sha256"]),
        run=run,
        health_window_started_ns=health_boundary_started_ns,
    )
    final_stream_compliance_after, final_stream_blocked_after = (
        cutover.evaluate_stream_target(
            actual_streams=final_stream_rows_after,
            target_manifest=final_target_manifest,
            bootstrap_mode=str(before_reference["nats_bootstrap"]["mode"]),
            require_fresh_empty=False,
        )
    )
    final_stream_projection_after, final_stream_projection_sha256_after = (
        _stream_target_projection(final_stream_rows_after)
    )
    final_durable_projection_after, final_durable_projection_sha256_after = (
        _durable_qualification_projection(final_durable_rows_after)
    )
    if base_image_id_after != base_image_id_before:
        raise RuntimeError("base_image_changed_during_evidence_capture")
    if (
        final_stream_blocked_after
        or final_stream_compliance_after.get("status") != "MATCHED"
    ):
        raise RuntimeError("final_nats_stream_target_not_matched")
    if (
        final_stream_projection_after != final_stream_projection
        or final_stream_projection_sha256_after != final_stream_projection_sha256
    ):
        raise RuntimeError("nats_stream_target_changed_during_final_evidence_capture")
    post_to_final_continuity = cutover.evaluate_active_runtime_continuity(
        previous_streams=after_payload.get("critical_streams"),
        current_streams=final_stream_rows,
        previous_durables=after_payload.get("durables"),
        current_durables=final_durable_rows,
        # App startup is the authorized provisioning phase.  Stream target
        # matching and the exact expected-durable projection above constrain
        # every permitted new identity; all pre-existing identities must remain.
        allow_new_identities=True,
        allow_declared_cutover_migrations=True,
    )
    if post_to_final_continuity.get("status") != "PASSED_WITH_TRUST_BOUNDARY":
        raise RuntimeError("nats_state_invalidated_since_post_preflight")
    final_double_read_continuity = cutover.evaluate_active_runtime_continuity(
        previous_streams=final_stream_rows,
        current_streams=final_stream_rows_after,
        previous_durables=final_durable_rows,
        current_durables=final_durable_rows_after,
        allow_new_identities=False,
    )
    if final_double_read_continuity.get("status") != "PASSED_WITH_TRUST_BOUNDARY":
        raise RuntimeError("nats_state_invalidated_during_final_evidence_capture")
    if (
        container_facts_after != container_facts_before
        or container_facts_final != container_facts_before
        or gateway_bindings_after != gateway_bindings_before
        or gateway_bindings_final != gateway_bindings_before
    ):
        raise RuntimeError("container_runtime_changed_during_evidence_capture")
    if nats_identity_before != nats_identity_after:
        raise RuntimeError("nats_identity_changed_during_final_evidence_capture")
    if nats_fingerprint_before != after_reference.get("nats_query_fingerprint"):
        raise RuntimeError("nats_identity_changed_since_post_preflight")
    if nats_volume_fingerprint_before != nats_volume_fingerprint_after:
        raise RuntimeError("nats_volume_changed_during_final_evidence_capture")
    if nats_volume_fingerprint_before != before_reference["nats_bootstrap"].get(
        "volume_fingerprint"
    ):
        raise RuntimeError("nats_volume_changed_since_bootstrap")
    event_capture = lifecycle_monitor_sealer(
        lifecycle_monitor_control_dir,
        token=lifecycle_monitor_token,
        cutoff_ns=window_ended_ns,
        expected_allowlist=(*_KNOWN_APP_CONTAINERS, _NATS_CONTAINER),
        expected_metadata={
            "deployment_lock_id": deployment_lock_id,
            "runtime_readiness_generation": runtime_readiness_generation,
            "deployed_commit": deployed_commit,
        },
    )
    lifecycle_evidence = _validate_external_lifecycle_capture(
        event_capture,
        post_window_started_ns=post_preflight_window_started_ns,
        post_window_ended_ns=post_preflight_window_ended_ns,
        app_up_authorized_ns=app_up_authorized_ns,
        health_boundary_started_ns=health_boundary_started_ns,
        requested_cutoff_ns=window_ended_ns,
        required_container_facts=container_facts_after,
    )
    coverage_ended_ns = lifecycle_evidence.get("transport_coverage_ended_ns")
    if isinstance(coverage_ended_ns, bool) or not isinstance(coverage_ended_ns, int):
        raise RuntimeError("deployment_lifecycle_monitor_boundary_invalid")
    final_window_seconds = (
        window_ended_ns - health_boundary_started_ns
    ) / 1_000_000_000
    if final_window_seconds < _MIN_FINAL_EVIDENCE_WINDOW_SECONDS:
        raise RuntimeError("final_deployment_stability_window_too_short")
    if final_window_seconds > _MAX_FINAL_EVIDENCE_WINDOW_SECONDS:
        raise RuntimeError("final_deployment_evidence_window_exceeded")
    # The Engine segment can close after the requested logical cutoff.  Recheck
    # every runtime identity and app health after that transport tail so a
    # failed-but-not-yet-unhealthy healthcheck cannot hide between the earlier
    # app postcondition and clean EOF.
    container_facts_sealed, gateway_bindings_sealed = _container_snapshot(
        required_container_names,
        expected_image_id=base_image_id_before,
        expected_generation=runtime_readiness_generation,
        expected_commit=deployed_commit,
        expected_profile=profile,
        expected_target_manifest_sha256=str(final_target_manifest["sha256"]),
        run=run,
        health_window_started_ns=health_boundary_started_ns,
    )
    nats_identity_sealed = _capture_nats_identity(run=run)
    nats_health_sealed = _capture_nats_health_snapshot(
        run=run,
        health_window_started_ns=health_boundary_started_ns,
        expected_container_id=str(nats_identity_sealed["container_id"]),
    )
    nats_volume_fingerprint_sealed = _capture_nats_volume_fingerprint(run=run)
    base_image_id_sealed = run(
        ("docker", "image", "inspect", "aats-base:dev", "--format", "{{.Id}}"),
        None,
    )
    if (
        container_facts_sealed != container_facts_before
        or gateway_bindings_sealed != gateway_bindings_before
    ):
        raise RuntimeError("container_runtime_changed_after_monitor_seal")
    if nats_identity_sealed != nats_identity_before:
        raise RuntimeError("nats_identity_changed_after_monitor_seal")
    if nats_volume_fingerprint_sealed != nats_volume_fingerprint_before:
        raise RuntimeError("nats_volume_changed_after_monitor_seal")
    if base_image_id_sealed != base_image_id_before:
        raise RuntimeError("base_image_changed_after_monitor_seal")
    payload: dict[str, object] = {
        "format_version": 2,
        "generated_at": now.isoformat(),
        "status": "simulation_stack_healthy_bounded_observation",
        "production_ready": False,
        "trading_ready": False,
        "deployed_commit": deployed_commit,
        "base_image_id": base_image_id_before,
        "profile": profile,
        "compose_overlay": overlay,
        "runtime_readiness_generation": runtime_readiness_generation,
        "deployment_lock_id": deployment_lock_id,
        "schema_contract": {
            "job_status": schema_job_status,
            "clone_manifest_verified": False,
            "consistent_rollback_verified": False,
        },
        "required_containers": container_facts_after,
        "collector_freshness": collector_freshness,
        "gateway_published_bindings": gateway_bindings_after,
        "container_runtime_evidence_window": {
            **lifecycle_evidence,
            "minimum_stability_seconds": _MIN_FINAL_EVIDENCE_WINDOW_SECONDS,
            "observed_stability_seconds": final_window_seconds,
            "all_required_container_healthchecks_observed_after_boundary": True,
            "window_started_at_utc": _utc_text_from_ns(
                health_boundary_started_ns
            ),
            "logical_cutoff_at_utc": _utc_text_from_ns(window_ended_ns),
            "transport_coverage_ended_at_utc": _utc_text_from_ns(
                coverage_ended_ns
            ),
            "health_boundary_app_fingerprint": health_boundary_app_fingerprint,
            "final_capture_started_at_utc": _utc_text_from_ns(window_started_ns),
            "allowlist": list(required_container_names),
            "post_health_boundary_lifecycle_events": [],
        },
        "nats_runtime_continuity": {
            "status": "PASSED_WITH_TRUST_BOUNDARY",
            "complete": False,
            "post_preflight_window_ended_at_utc": _utc_text_from_ns(
                post_preflight_window_ended_ns
            ),
            "logical_cutoff_at_utc": _utc_text_from_ns(window_ended_ns),
            "transport_coverage_ended_at_utc": _utc_text_from_ns(
                coverage_ended_ns
            ),
            "query_fingerprint": nats_fingerprint_after,
            "volume_fingerprint": nats_volume_fingerprint_after,
            "health": nats_health_sealed,
            "health_samples_validated": 3,
            "post_preflight_to_final": post_to_final_continuity,
            "final_double_read": final_double_read_continuity,
            "first_query": {
                "streams_scanned": final_query_before.stream_count,
                "consumers_scanned": final_query_before.consumer_count,
            },
            "second_query": {
                "streams_scanned": final_query_after.stream_count,
                "consumers_scanned": final_query_after.consumer_count,
            },
            "observed_lifecycle_events": [],
        },
        "nats_stream_target_qualification": {
            "status": "PASSED_WITH_TRUST_BOUNDARY",
            "complete": False,
            "target_stream_manifest_sha256": final_target_manifest["sha256"],
            "compliance": final_stream_compliance,
            "stable_snapshots": 2,
            "immutable_projection_sha256": final_stream_projection_sha256,
            "continuity": final_double_read_continuity,
        },
        "nats_durable_qualification": {
            "status": "PASSED_WITH_TRUST_BOUNDARY",
            "complete": False,
            "expected_count": len(cutover.build_expected_durable_index()),
            "observed_count": len(final_durable_rows_after),
            "stable_snapshots": 2,
            "first_snapshot": {
                "canonical_projection_sha256": final_durable_projection_sha256,
                "canonical_projection": final_durable_projection,
            },
            "second_snapshot": {
                "canonical_projection_sha256": (
                    final_durable_projection_sha256_after
                ),
                "canonical_projection": final_durable_projection_after,
            },
            "continuity": final_double_read_continuity,
        },
        "runtime_unknowns": [
            "production_account_and_exchange_not_verified",
            "production_schema_manifest_not_verified",
            "app_schema_parameter_rollback_not_verified",
            "trading_readiness_packet_not_verified",
            "docker_socket_writer_exclusion_not_verified",
            "docker_daemon_event_delivery_loss_not_detectable",
            "docker_http_ready_precedes_broker_subscription_ack",
            "docker_daemon_event_clock_alignment_not_verified",
            "ordered_lossless_container_audit_source_not_verified",
            "container_exec_and_network_attachment_events_not_observed",
            "cross_container_volume_access_not_observed",
            "nats_purge_vs_legitimate_retention_not_distinguishable",
        ],
    }
    payload["nats_durable_cutover_preflights"] = {
        "status": "PASSED_WITH_TRUST_BOUNDARY",
        "pre_full_down": before_reference,
        "post_infra_pre_app_up": after_reference,
    }
    # Compatibility alias: older evidence readers consumed one final preflight.
    payload["nats_durable_cutover_preflight"] = after_reference
    return payload


def write_evidence(*, repo_root: Path, payload: dict[str, object]) -> Path:
    evidence_dir = repo_root / "deploy" / "wsl2-dev" / "runtime" / "deployment-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.fromisoformat(str(payload["generated_at"]))
    timestamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    commit = str(payload["deployed_commit"])
    profile = str(payload["profile"])
    target = evidence_dir / f"{timestamp}-{profile}-{commit[:12]}.json"
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    target.chmod(0o444)
    _fsync_directory(evidence_dir)
    return target


def _fsync_directory(path: Path) -> None:
    """Persist a newly created evidence directory entry on the WSL2 host."""

    if os.name == "nt":
        # Windows has no portable directory-fsync equivalent.  The production
        # writer is invoked inside the repository's WSL2 deployment runtime.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--overlay", required=True)
    parser.add_argument("--schema-job-status", required=True)
    parser.add_argument("--runtime-readiness-generation", required=True)
    parser.add_argument("--deployment-lock-id", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--lifecycle-monitor-control-dir", type=Path, required=True)
    parser.add_argument("--lifecycle-monitor-token", required=True)
    parser.add_argument("--app-up-authorized-ns", type=int, required=True)
    parser.add_argument("--health-boundary-started-ns", type=int, required=True)
    parser.add_argument("--health-boundary-app-fingerprint", required=True)
    parser.add_argument(
        "--collector-heartbeat-epoch",
        action="append",
        default=[],
        metavar="NAME=EPOCH",
    )
    parser.add_argument(
        "--nats-cutover-preflight-before",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--nats-cutover-preflight-after",
        type=Path,
        required=True,
    )
    parser.add_argument("--required-container", action="append", default=[])
    return parser.parse_args(argv)


def _parse_collector_heartbeat_epochs(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value:
            raise ValueError("invalid_collector_heartbeat_epoch_argument")
        name, epoch_text = value.split("=", 1)
        if (
            name not in _COLLECTOR_HEARTBEATS
            or name in result
            or not re.fullmatch(r"[1-9][0-9]{0,11}", epoch_text)
        ):
            raise ValueError("invalid_collector_heartbeat_epoch_argument")
        result[name] = int(epoch_text)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    payload = build_evidence(
        repo_root=repo_root,
        profile=args.profile,
        overlay=args.overlay,
        schema_job_status=args.schema_job_status,
        runtime_readiness_generation=args.runtime_readiness_generation,
        deployment_lock_id=args.deployment_lock_id,
        deployed_commit=args.deployed_commit,
        required_containers=args.required_container,
        lifecycle_monitor_control_dir=args.lifecycle_monitor_control_dir,
        lifecycle_monitor_token=args.lifecycle_monitor_token,
        app_up_authorized_ns=args.app_up_authorized_ns,
        health_boundary_started_ns=args.health_boundary_started_ns,
        health_boundary_app_fingerprint=args.health_boundary_app_fingerprint,
        collector_heartbeat_epochs=_parse_collector_heartbeat_epochs(
            args.collector_heartbeat_epoch
        ),
        nats_cutover_preflight_before_path=args.nats_cutover_preflight_before,
        nats_cutover_preflight_after_path=args.nats_cutover_preflight_after,
        nats_stream_probe=_probe_final_nats_state,
    )
    target = write_evidence(repo_root=repo_root, payload=payload)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
