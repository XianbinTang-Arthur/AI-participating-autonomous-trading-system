#!/usr/bin/env python3
"""Canonical, no-secret NATS Docker runtime identity projection.

The standard deployment bootstrap, the JetStream cutover preflight, and the
final deployment evidence must compare the same facts.  This module owns the
only Docker inspect templates, parsers, and canonical SHA-256 projections for
the NATS container and its persistent volume.

Only explicitly selected runtime metadata is inspected.  In particular, the
container environment is never read, so credentials cannot enter fingerprints
or command output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


NATS_CONTAINER = "aats-nats"
NATS_VOLUME = "aats-dev_nats_data"
NATS_COMPOSE_PROJECT = "aats-dev"
NATS_COMPOSE_SERVICE = "nats"
NATS_COMPOSE_VOLUME = "nats_data"
NATS_EXPECTED_IMAGE = (
    "nats:2.10-alpine@"
    "sha256:b83efabe3e7def1e0a4a31ec6e078999bb17c80363f881df35edc70fcb6bb927"
)

# Newline-delimited JSON scalars avoid an ambiguous delimiter while selecting
# only non-secret fields.  Exactly one /data mount must produce exactly three
# final lines; zero or multiple matches therefore fail closed in the parser.
NATS_CONTAINER_INSPECT_TEMPLATE = "\n".join(
    (
        "{{json .Id}}",
        "{{json .Image}}",
        "{{json .Config.Image}}",
        "{{json .State.Status}}",
        (
            "{{if .State.Health}}{{json .State.Health.Status}}"
            '{{else}}"none"{{end}}'
        ),
        (
            "{{if .State.Health}}{{.State.Health.FailingStreak}}"
            "{{else}}-1{{end}}"
        ),
        "{{json .State.StartedAt}}",
        "{{.RestartCount}}",
        '{{json (index .Config.Labels "com.docker.compose.project")}}',
        '{{json (index .Config.Labels "com.docker.compose.service")}}',
        (
            '{{range .Mounts}}{{if eq .Destination "/data"}}'
            "{{json .Type}}\n{{json .Name}}\n{{json .RW}}{{end}}{{end}}"
        ),
    )
)

NATS_VOLUME_INSPECT_TEMPLATE = "\n".join(
    (
        "{{json .Name}}",
        "{{json .Driver}}",
        "{{json .Scope}}",
        "{{json .CreatedAt}}",
        "{{json .Options}}",
        '{{json (index .Labels "com.docker.compose.project")}}',
        '{{json (index .Labels "com.docker.compose.volume")}}',
        '{{json (index .Labels "com.aats.bootstrap_lock")}}',
    )
)

# Health.Log is intentionally kept out of NATS_CONTAINER_INSPECT_TEMPLATE and
# therefore out of the stable runtime fingerprint: Docker retains a rolling
# five-entry health log, so successful checks would otherwise create false
# identity drift.  This separate projection lets deployment stability checks
# reject transient failures without weakening immutable identity comparisons.
NATS_HEALTH_INSPECT_TEMPLATE = (
    '{"Id":{{json .Id}},"RestartCount":{{json .RestartCount}},'
    '"State":{"Status":{{json .State.Status}},'
    '"Health":{"Status":{{json .State.Health.Status}},'
    '"FailingStreak":{{json .State.Health.FailingStreak}},'
    '"Checks":[{{range $index, $entry := .State.Health.Log}}'
    '{{if $index}},{{end}}{"Start":{{json $entry.Start}},'
    '"End":{{json $entry.End}},"ExitCode":{{json $entry.ExitCode}}}'
    '{{end}}]}}}'
)

_CONTAINER_SCHEMA = "aats.nats_runtime_identity.container.v1"
_VOLUME_SCHEMA = "aats.nats_runtime_identity.volume.v1"
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DOCKER_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_RFC3339_NANO_RE = re.compile(
    r"^(?P<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})$"
)

CommandRunner = Callable[[Sequence[str]], str]


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
    return completed.stdout


def canonical_fingerprint(projection: Mapping[str, Any]) -> str:
    """Hash one schema-qualified projection with stable JSON serialization."""

    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_string(line: str, *, error: str) -> str:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(error) from exc
    if not isinstance(value, str):
        raise RuntimeError(error)
    return value


def _non_negative_integer(line: str, *, error: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", line):
        raise RuntimeError(error)
    return int(line)


def _required_non_negative_integer(value: object, *, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(error)
    return value


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


@dataclass(frozen=True, slots=True)
class NatsContainerIdentity:
    container_id: str
    image_id: str
    configured_image_reference: str
    status: str
    health_status: str
    health_failing_streak: int
    started_at: str
    restart_count: int
    compose_project: str
    compose_service: str
    data_mount_type: str
    data_mount_name: str
    data_mount_read_write: bool

    def projection(self) -> dict[str, object]:
        return {
            "schema": _CONTAINER_SCHEMA,
            "container": {
                "id": self.container_id,
                "image_id": self.image_id,
                "configured_image_reference": self.configured_image_reference,
                "status": self.status,
                "health": {
                    "status": self.health_status,
                    "failing_streak": self.health_failing_streak,
                },
                "started_at": self.started_at,
                "restart_count": self.restart_count,
                "compose": {
                    "project": self.compose_project,
                    "service": self.compose_service,
                },
                "data_mount": {
                    "destination": "/data",
                    "type": self.data_mount_type,
                    "name": self.data_mount_name,
                    "read_write": self.data_mount_read_write,
                },
            },
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.projection())

    def public_identity(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "container_id": self.container_id,
            "restart_count": self.restart_count,
        }


@dataclass(frozen=True, slots=True)
class NatsVolumeIdentity:
    name: str
    driver: str
    scope: str
    created_at: str
    options: dict[str, str]
    labels: dict[str, str]

    def projection(self) -> dict[str, object]:
        return {
            "schema": _VOLUME_SCHEMA,
            "volume": {
                "name": self.name,
                "driver": self.driver,
                "scope": self.scope,
                "created_at": self.created_at,
                "options": self.options,
                "labels": self.labels,
            },
        }

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.projection())


def parse_nats_container_identity(output: str) -> NatsContainerIdentity:
    """Validate one healthy standard NATS container projection."""

    error = "nats_runtime_invalid_container_identity"
    lines = output.splitlines()
    if len(lines) != 13:
        raise RuntimeError(f"{error}:line_count:{len(lines)}")
    (
        container_id_line,
        image_id_line,
        configured_image_reference_line,
        status_line,
        health_status_line,
        failing_streak_line,
        started_at_line,
        restart_count_line,
        compose_project_line,
        compose_service_line,
        mount_type_line,
        mount_name_line,
        mount_read_write_line,
    ) = lines
    container_id = _json_string(container_id_line, error=f"{error}:container_id")
    image_id = _json_string(image_id_line, error=f"{error}:image_id")
    configured_image_reference = _json_string(
        configured_image_reference_line,
        error=f"{error}:configured_image_reference",
    )
    status = _json_string(status_line, error=f"{error}:status")
    health_status = _json_string(
        health_status_line,
        error=f"{error}:health_status",
    )
    failing_streak = _non_negative_integer(
        failing_streak_line,
        error=f"{error}:health_failing_streak",
    )
    started_at = _json_string(started_at_line, error=f"{error}:started_at")
    restart_count = _non_negative_integer(
        restart_count_line,
        error=f"{error}:restart_count",
    )
    compose_project = _json_string(
        compose_project_line,
        error=f"{error}:compose_project",
    )
    compose_service = _json_string(
        compose_service_line,
        error=f"{error}:compose_service",
    )
    mount_type = _json_string(mount_type_line, error=f"{error}:mount_type")
    mount_name = _json_string(mount_name_line, error=f"{error}:mount_name")
    try:
        mount_read_write = json.loads(mount_read_write_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{error}:mount_read_write") from exc
    if not isinstance(mount_read_write, bool):
        raise RuntimeError(f"{error}:mount_read_write")

    checks = (
        (_CONTAINER_ID_RE.fullmatch(container_id) is not None, "container_id"),
        (_IMAGE_ID_RE.fullmatch(image_id) is not None, "image_id"),
        (
            configured_image_reference == NATS_EXPECTED_IMAGE,
            "configured_image_reference",
        ),
        (status == "running", "status"),
        (health_status == "healthy", "health_status"),
        (failing_streak == 0, "health_failing_streak"),
        (_DOCKER_TIMESTAMP_RE.fullmatch(started_at) is not None, "started_at"),
        (compose_project == NATS_COMPOSE_PROJECT, "compose_project"),
        (compose_service == NATS_COMPOSE_SERVICE, "compose_service"),
        (mount_type == "volume", "mount_type"),
        (mount_name == NATS_VOLUME, "mount_name"),
        (mount_read_write, "mount_read_write"),
    )
    for passed, field in checks:
        if not passed:
            raise RuntimeError(f"{error}:{field}")

    return NatsContainerIdentity(
        container_id=container_id,
        image_id=image_id,
        configured_image_reference=configured_image_reference,
        status=status,
        health_status=health_status,
        health_failing_streak=failing_streak,
        started_at=started_at,
        restart_count=restart_count,
        compose_project=compose_project,
        compose_service=compose_service,
        data_mount_type=mount_type,
        data_mount_name=mount_name,
        data_mount_read_write=mount_read_write,
    )


def parse_nats_health_snapshot(
    output: str,
    *,
    health_window_started_ns: int | None = None,
    expected_container_id: str | None = None,
    require_success_after_boundary: bool = False,
) -> dict[str, object]:
    """Validate current NATS health and retained checks after a time boundary.

    The returned mapping is deliberately not part of the immutable NATS
    fingerprint.  It is a bounded health observation whose rolling log may
    advance while the container identity remains stable.
    """

    error = "nats_runtime_invalid_health_snapshot"
    if health_window_started_ns is not None and (
        isinstance(health_window_started_ns, bool)
        or not isinstance(health_window_started_ns, int)
        or health_window_started_ns <= 0
    ):
        raise RuntimeError(f"{error}:health_window_started_ns")
    if require_success_after_boundary and health_window_started_ns is None:
        raise RuntimeError(f"{error}:health_window_required")
    if expected_container_id is not None and (
        not isinstance(expected_container_id, str)
        or _CONTAINER_ID_RE.fullmatch(expected_container_id) is None
    ):
        raise RuntimeError(f"{error}:expected_container_id")

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(error) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "Id",
        "RestartCount",
        "State",
    }:
        raise RuntimeError(error)
    container_id = payload.get("Id")
    restart_count = _required_non_negative_integer(
        payload.get("RestartCount"),
        error=f"{error}:restart_count",
    )
    state = payload.get("State")
    if (
        not isinstance(container_id, str)
        or _CONTAINER_ID_RE.fullmatch(container_id) is None
        or not isinstance(state, dict)
        or set(state) != {"Status", "Health"}
    ):
        raise RuntimeError(error)
    if expected_container_id is not None and container_id != expected_container_id:
        raise RuntimeError("nats_runtime_health_container_identity_mismatch")

    health = state.get("Health")
    if not isinstance(health, dict) or set(health) != {
        "Status",
        "FailingStreak",
        "Checks",
    }:
        raise RuntimeError(error)
    status = state.get("Status")
    health_status = health.get("Status")
    failing_streak = _required_non_negative_integer(
        health.get("FailingStreak"),
        error=f"{error}:failing_streak",
    )
    checks = health.get("Checks")
    if not isinstance(checks, list) or not checks:
        raise RuntimeError(f"{error}:health_log")

    previous_started_ns = 0
    previous_ended_ns = 0
    checks_after_boundary = 0
    last_exit_code = -1
    last_ended_ns = 0
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"Start", "End", "ExitCode"}:
            raise RuntimeError(f"{error}:health_log")
        started_ns = _rfc3339_nanoseconds(
            check.get("Start"),
            error=f"{error}:health_started_at",
        )
        ended_ns = _rfc3339_nanoseconds(
            check.get("End"),
            error=f"{error}:health_ended_at",
        )
        exit_code = _required_non_negative_integer(
            check.get("ExitCode"),
            error=f"{error}:health_exit_code",
        )
        if (
            ended_ns < started_ns
            or started_ns < previous_started_ns
            or ended_ns < previous_ended_ns
        ):
            raise RuntimeError(f"{error}:health_log_order")
        if health_window_started_ns is not None and ended_ns >= health_window_started_ns:
            checks_after_boundary += 1
            if exit_code != 0:
                raise RuntimeError("nats_runtime_health_failed_after_boundary")
        previous_started_ns = started_ns
        previous_ended_ns = ended_ns
        last_exit_code = exit_code
        last_ended_ns = ended_ns

    if (
        status != "running"
        or health_status != "healthy"
        or failing_streak != 0
        or last_exit_code != 0
        or restart_count != 0
    ):
        raise RuntimeError("nats_runtime_not_healthy")
    if require_success_after_boundary and checks_after_boundary == 0:
        raise RuntimeError("nats_runtime_health_not_observed_after_boundary")
    return {
        "container_id": container_id,
        "restart_count": restart_count,
        "status": status,
        "health_status": health_status,
        "health_failing_streak": failing_streak,
        "last_health_exit_code": last_exit_code,
        "last_health_check_ended_ns": last_ended_ns,
        "health_checks_observed_after_boundary": (
            checks_after_boundary if health_window_started_ns is not None else None
        ),
    }


def parse_nats_volume_identity(output: str) -> NatsVolumeIdentity:
    """Validate the default local Compose NATS volume using safe fields only."""

    error = "nats_runtime_invalid_volume_identity"
    lines = output.splitlines()
    if len(lines) != 8:
        raise RuntimeError(f"{error}:line_count:{len(lines)}")
    name = _json_string(lines[0], error=f"{error}:name")
    driver = _json_string(lines[1], error=f"{error}:driver")
    scope = _json_string(lines[2], error=f"{error}:scope")
    created_at = _json_string(lines[3], error=f"{error}:created_at")
    try:
        options_value = json.loads(lines[4])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{error}:options") from exc
    if options_value is None:
        options: dict[str, str] = {}
    elif isinstance(options_value, dict) and not options_value:
        options = {}
    else:
        raise RuntimeError(f"{error}:options")
    compose_project = _json_string(lines[5], error=f"{error}:compose_project")
    compose_volume = _json_string(lines[6], error=f"{error}:compose_volume")
    try:
        bootstrap_lock_value = json.loads(lines[7])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{error}:bootstrap_lock") from exc
    # Docker's Go template renders a missing value selected with
    # ``index .Labels`` as the JSON string ``""`` (not JSON ``null``).  Both
    # forms mean that this pre-existing Compose volume has no fresh-bootstrap
    # claim.  A non-empty claim remains strictly validated and is matched to
    # the current deployment token by the fresh-install path.
    if bootstrap_lock_value == "":
        bootstrap_lock_value = None
    if bootstrap_lock_value is not None and (
        not isinstance(bootstrap_lock_value, str)
        or _SAFE_LABEL_VALUE_RE.fullmatch(bootstrap_lock_value) is None
    ):
        raise RuntimeError(f"{error}:bootstrap_lock")
    labels = {
        "com.docker.compose.project": compose_project,
        "com.docker.compose.volume": compose_volume,
    }
    if bootstrap_lock_value is not None:
        labels["com.aats.bootstrap_lock"] = bootstrap_lock_value
    checks = (
        (name == NATS_VOLUME, "name"),
        (driver == "local", "driver"),
        (scope == "local", "scope"),
        (_DOCKER_TIMESTAMP_RE.fullmatch(created_at) is not None, "created_at"),
        (
            compose_project == NATS_COMPOSE_PROJECT,
            "compose_project",
        ),
        (
            compose_volume == NATS_COMPOSE_VOLUME,
            "compose_volume",
        ),
    )
    for passed, field in checks:
        if not passed:
            raise RuntimeError(f"{error}:{field}")
    return NatsVolumeIdentity(
        name=name,
        driver=driver,
        scope=scope,
        created_at=created_at,
        options=options,
        labels=labels,
    )


def capture_nats_identity(
    run: CommandRunner = _run_command,
) -> dict[str, object]:
    output = run(
        (
            "docker",
            "inspect",
            "--format",
            NATS_CONTAINER_INSPECT_TEMPLATE,
            NATS_CONTAINER,
        )
    )
    identity = parse_nats_container_identity(output)
    pinned_image_id = run(
        (
            "docker",
            "image",
            "inspect",
            NATS_EXPECTED_IMAGE,
            "--format",
            "{{.Id}}",
        )
    ).strip()
    if _IMAGE_ID_RE.fullmatch(pinned_image_id) is None:
        raise RuntimeError("nats_runtime_invalid_pinned_image_id")
    if identity.image_id != pinned_image_id:
        raise RuntimeError("nats_runtime_container_image_not_pinned")
    return identity.public_identity()


def capture_nats_health_snapshot(
    run: CommandRunner = _run_command,
    *,
    health_window_started_ns: int | None = None,
    expected_container_id: str | None = None,
    require_success_after_boundary: bool = False,
) -> dict[str, object]:
    output = run(
        (
            "docker",
            "inspect",
            "--format",
            NATS_HEALTH_INSPECT_TEMPLATE,
            NATS_CONTAINER,
        )
    )
    return parse_nats_health_snapshot(
        output,
        health_window_started_ns=health_window_started_ns,
        expected_container_id=expected_container_id,
        require_success_after_boundary=require_success_after_boundary,
    )


def capture_nats_volume_fingerprint(
    run: CommandRunner = _run_command,
) -> str:
    output = run(
        (
            "docker",
            "volume",
            "inspect",
            "--format",
            NATS_VOLUME_INSPECT_TEMPLATE,
            NATS_VOLUME,
        )
    )
    return parse_nats_volume_identity(output).fingerprint


def capture_nats_runtime_snapshot(
    run: CommandRunner = _run_command,
) -> dict[str, object]:
    container = capture_nats_identity(run)
    return {
        **container,
        "volume_fingerprint": capture_nats_volume_fingerprint(run),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture canonical no-secret NATS Docker identity facts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--format", choices=("tsv",), default="tsv")
    health = subparsers.add_parser("health-check")
    health.add_argument("--since-ns", type=int)
    health.add_argument("--require-success-after-boundary", action="store_true")
    subparsers.add_parser("volume-fingerprint")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            snapshot = capture_nats_runtime_snapshot()
            print(
                "\t".join(
                    (
                        str(snapshot["fingerprint"]),
                        str(snapshot["container_id"]),
                        str(snapshot["restart_count"]),
                        str(snapshot["volume_fingerprint"]),
                    )
                )
            )
            return 0
        if args.command == "volume-fingerprint":
            print(capture_nats_volume_fingerprint())
            return 0
        if args.command == "health-check":
            capture_nats_health_snapshot(
                health_window_started_ns=args.since_ns,
                require_success_after_boundary=args.require_success_after_boundary,
            )
            print("healthy")
            return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"NATS runtime identity capture failed: {exc}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
