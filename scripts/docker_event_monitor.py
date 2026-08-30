#!/usr/bin/env python3
"""Bounded live Docker lifecycle observation for deployment evidence.

The Docker daemon retains only a small global history buffer.  Consequently,
an after-the-fact ``docker events --since`` query cannot prove that a quiet
deployment window was complete.  This module instead keeps overlapping live
Engine API subscriptions open.  Every segment has a server-side ``until``
deadline and is authoritative only after HTTP 200; a segment is complete only
after the daemon closes it at that deadline.

Only a small, no-secret projection of events for an explicit container
allowlist is retained.  The daemon mode is used to span the post-cutover,
application-start and final-evidence phases of the standard deployment.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode


_FORMAT_VERSION = "aats.live_docker_event_window.v1"
_READY_FORMAT_VERSION = "aats.live_docker_event_monitor_ready.v1"
_SEALED_FORMAT_VERSION = "aats.live_docker_event_monitor_sealed.v1"
_FAILED_FORMAT_VERSION = "aats.live_docker_event_monitor_failed.v1"
_DOCKER_SOCKET = Path("/var/run/docker.sock")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_EXEC_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_TARGET_EVENTS = 10_000
_MAX_EVENT_LINE_BYTES = 1024 * 1024
_DEFAULT_SEGMENT_SECONDS = 5.0
_DEFAULT_OVERLAP_SECONDS = 2.0
_READY_TIMEOUT_SECONDS = 15.0
_SEAL_TIMEOUT_SECONDS = 20.0
_MAX_CLOCK_DRIFT_TOLERANCE_NS = 1_500_000_000
_MIN_CLOCK_DRIFT_TOLERANCE_NS = 50_000_000
_CLOCK_DRIFT_OVERLAP_MARGIN_NS = 250_000_000
_MAX_DOCKER_INFO_BYTES = 4 * 1024 * 1024
_DAEMON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class DockerEventMonitorError(RuntimeError):
    """A fail-closed live-event observation failure."""


def _clock_drift_tolerance_for_overlap(overlap_ns: int) -> int:
    """Bound accepted wall-clock steps below the configured segment overlap."""

    if isinstance(overlap_ns, bool) or not isinstance(overlap_ns, int) or overlap_ns <= 0:
        raise DockerEventMonitorError("invalid_live_monitor_overlap_ns")
    return min(
        _MAX_CLOCK_DRIFT_TOLERANCE_NS,
        max(
            _MIN_CLOCK_DRIFT_TOLERANCE_NS,
            overlap_ns - _CLOCK_DRIFT_OVERLAP_MARGIN_NS,
        ),
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, *, timeout: float) -> None:
        super().__init__("docker", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self._socket_path))
        self.sock = connection


def _validated_daemon_id(value: object) -> str:
    if not isinstance(value, str) or not _DAEMON_ID_RE.fullmatch(value):
        raise DockerEventMonitorError("invalid_docker_daemon_id")
    return value


def _read_direct_docker_daemon_id(
    *,
    socket_path: Path = _DOCKER_SOCKET,
) -> str:
    """Read the daemon ID directly from the same socket used for events."""

    connection = _UnixHTTPConnection(socket_path, timeout=10.0)
    try:
        connection.request(
            "GET",
            "/info",
            headers={"Accept": "application/json", "Host": "docker"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise DockerEventMonitorError(
                f"direct_docker_info_http_status:{response.status}"
            )
        raw = response.read(_MAX_DOCKER_INFO_BYTES + 1)
        if len(raw) > _MAX_DOCKER_INFO_BYTES:
            raise DockerEventMonitorError("direct_docker_info_too_large")
        try:
            payload = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DockerEventMonitorError("invalid_direct_docker_info") from exc
        if not isinstance(payload, dict):
            raise DockerEventMonitorError("invalid_direct_docker_info")
        return _validated_daemon_id(payload.get("ID"))
    finally:
        connection.close()


def _run_docker_cli(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("docker", *arguments),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerEventMonitorError("docker_cli_binding_query_failed") from exc
    return completed.stdout


def validate_local_docker_daemon_binding(
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Fail closed unless CLI and the fixed local socket identify one daemon."""

    active_environ = os.environ if environ is None else environ
    docker_host = active_environ.get("DOCKER_HOST", "")
    if docker_host not in {"", "unix:///var/run/docker.sock"}:
        raise DockerEventMonitorError("docker_host_environment_forbidden")
    if active_environ.get("DOCKER_CONTEXT", ""):
        raise DockerEventMonitorError("docker_context_environment_forbidden")

    if _run_docker_cli("context", "show").strip() != "default":
        raise DockerEventMonitorError("docker_default_context_required")
    raw_cli_id = _run_docker_cli("info", "--format", "{{json .ID}}")
    try:
        cli_id = _validated_daemon_id(json.loads(raw_cli_id))
    except json.JSONDecodeError as exc:
        raise DockerEventMonitorError("invalid_docker_cli_daemon_id") from exc
    direct_id = _read_direct_docker_daemon_id()
    if cli_id != direct_id:
        raise DockerEventMonitorError("docker_cli_direct_daemon_id_mismatch")
    return direct_id


def daemon_binding_transport_envelope(daemon_id: object) -> str:
    """Return an integrity-bound ASCII envelope for the wsl.exe boundary."""

    validated = _validated_daemon_id(daemon_id)
    raw = validated.encode("ascii", errors="strict")
    digest = hashlib.sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"sha256:{digest}\t{encoded}"


@dataclass(slots=True)
class _Segment:
    segment_id: int
    requested_at_ns: int
    requested_at_mono_ns: int
    until_ns: int
    ready_at_ns: int | None = None
    ready_at_mono_ns: int | None = None
    completed_at_ns: int | None = None
    completed_at_mono_ns: int | None = None
    event_count: int = 0
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)


SanitizedEvent = dict[str, object]
SegmentReady = Callable[[int, int], None]
SegmentEvent = Callable[[SanitizedEvent], None]
SegmentReader = Callable[[int, SegmentReady, SegmentEvent], None]
RuntimeLoader = Callable[[Sequence[str]], dict[str, str]]
DaemonIdentityLoader = Callable[[], str]


def _docker_timestamp(value_ns: int) -> str:
    if isinstance(value_ns, bool) or not isinstance(value_ns, int) or value_ns <= 0:
        raise DockerEventMonitorError("invalid_docker_event_timestamp")
    return f"{value_ns // 1_000_000_000}.{value_ns % 1_000_000_000:09d}"


def _required_allowlist(containers: Sequence[str]) -> tuple[str, ...]:
    names = tuple(containers)
    if not names or len(names) != len(set(names)):
        raise DockerEventMonitorError("invalid_docker_event_container_allowlist")
    if any(not isinstance(name, str) or not _CONTAINER_RE.fullmatch(name) for name in names):
        raise DockerEventMonitorError("invalid_docker_event_container_allowlist")
    return names


def sanitize_docker_event(payload: object) -> SanitizedEvent:
    """Return the only event fields permitted to enter deployment evidence."""

    if not isinstance(payload, dict) or payload.get("Type") != "container":
        raise DockerEventMonitorError("invalid_live_docker_event")
    actor = payload.get("Actor")
    if not isinstance(actor, dict):
        raise DockerEventMonitorError("invalid_live_docker_event_actor")
    attributes = actor.get("Attributes")
    if not isinstance(attributes, dict):
        raise DockerEventMonitorError("invalid_live_docker_event_attributes")
    container_id = actor.get("ID", payload.get("id"))
    name = attributes.get("name")
    action = payload.get("Action", payload.get("status"))
    time_nano = payload.get("timeNano")
    if not isinstance(container_id, str) or not _CONTAINER_ID_RE.fullmatch(container_id):
        raise DockerEventMonitorError("invalid_live_docker_event_container_id")
    if not isinstance(name, str) or not _CONTAINER_RE.fullmatch(name):
        raise DockerEventMonitorError("invalid_live_docker_event_container_name")
    if not isinstance(action, str) or not action or len(action) > 4096:
        raise DockerEventMonitorError("invalid_live_docker_event_action")
    if isinstance(time_nano, bool) or not isinstance(time_nano, int) or time_nano <= 0:
        raise DockerEventMonitorError("invalid_live_docker_event_time")

    result: SanitizedEvent = {
        "container_id": container_id,
        "name": name,
        "action": action,
        "time_nano": time_nano,
    }
    if action.startswith("exec_"):
        exec_id = attributes.get("execID")
        if not isinstance(exec_id, str) or not _EXEC_ID_RE.fullmatch(exec_id):
            raise DockerEventMonitorError("invalid_live_docker_event_exec_id")
        result["exec_id"] = exec_id
        if action == "exec_die":
            exit_code = attributes.get("exitCode")
            if not isinstance(exit_code, str) or not re.fullmatch(r"-?[0-9]+", exit_code):
                raise DockerEventMonitorError("invalid_live_docker_event_exit_code")
            result["exit_code"] = int(exit_code)
    return result


def _read_live_segment(
    until_ns: int,
    on_ready: SegmentReady,
    on_event: SegmentEvent,
    *,
    socket_path: Path = _DOCKER_SOCKET,
) -> None:
    """Read one live-only Engine API segment through daemon clean EOF."""

    # The Moby event broker has no sequence number or loss notification and can
    # skip a slow subscriber.  Restrict the server-side topic to this fixed
    # Compose project instead of consuming global container traffic; the final
    # packet still records this daemon trust boundary explicitly.  Do not add
    # an action filter here: Docker reports health transitions as qualified
    # actions (for example ``health_status: healthy``), and an incomplete
    # server-side action list would silently turn lifecycle evidence into a
    # false quiet window.  Automatic and manual exec events are intentionally
    # discarded client-side because Moby does not expose their provenance.
    filters = json.dumps(
        {
            "type": ["container"],
            "label": ["com.docker.compose.project=aats-dev"],
        },
        separators=(",", ":"),
    )
    # Docker replays its global in-memory history when ``since`` is omitted.
    # Bound that replay to the request instant so events from infrastructure
    # bootstrap before this monitor cannot poison an otherwise valid window.
    # Events delivered from this small request-to-HTTP-ready interval are
    # retained separately in the evidence packet; they are never discarded or
    # represented as part of the authoritative post-ready window.
    history_since_ns = time.time_ns()
    query = urlencode(
        {
            "since": _docker_timestamp(history_since_ns),
            "until": _docker_timestamp(until_ns),
            "filters": filters,
        }
    )
    timeout = max(10.0, (until_ns - time.time_ns()) / 1_000_000_000 + 10.0)
    connection = _UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        connection.request(
            "GET",
            f"/events?{query}",
            headers={"Accept": "application/json", "Host": "docker"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise DockerEventMonitorError(
                f"live_docker_event_http_status:{response.status}"
            )
        # HTTPResponse retains body bytes already received with the headers.
        # This callback establishes the client HTTP-ready boundary; Moby may
        # attach the broker subscription just after flushing those headers, so
        # the packet explicitly reports that residual trust gap.
        on_ready(time.time_ns(), time.monotonic_ns())
        while True:
            raw = response.readline(_MAX_EVENT_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > _MAX_EVENT_LINE_BYTES:
                raise DockerEventMonitorError("live_docker_event_line_too_large")
            try:
                payload = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DockerEventMonitorError("invalid_live_docker_event_json") from exc
            on_event(sanitize_docker_event(payload))
    finally:
        connection.close()


def _load_target_runtime(
    containers: Sequence[str],
) -> dict[str, str]:
    """Load current target IDs after the first live segment is ready."""

    completed = subprocess.run(
        (
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--format",
            '{{.ID}}|{{.Names}}|{{.Label "com.docker.compose.project"}}',
        ),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=10,
    )
    allowlist = set(containers)
    ids: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split("|")
        if len(parts) != 3:
            raise DockerEventMonitorError("invalid_docker_runtime_identity")
        container_id, name, compose_project = parts
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            raise DockerEventMonitorError("invalid_docker_runtime_identity")
        if name in allowlist:
            if compose_project != "aats-dev":
                raise DockerEventMonitorError("docker_runtime_project_label_mismatch")
            if name in ids:
                raise DockerEventMonitorError("duplicate_docker_runtime_identity")
            ids[name] = container_id
    return ids


class LiveDockerEventMonitor:
    """Continuously cover a dynamic window with overlapping live segments."""

    def __init__(
        self,
        containers: Sequence[str],
        *,
        max_runtime_seconds: float,
        segment_seconds: float = _DEFAULT_SEGMENT_SECONDS,
        overlap_seconds: float = _DEFAULT_OVERLAP_SECONDS,
        segment_reader: SegmentReader | None = None,
        runtime_loader: RuntimeLoader = _load_target_runtime,
        daemon_identity_loader: DaemonIdentityLoader = validate_local_docker_daemon_binding,
    ) -> None:
        self._containers = _required_allowlist(containers)
        if not (5.0 <= max_runtime_seconds <= 3600.0):
            raise DockerEventMonitorError("invalid_live_monitor_max_runtime")
        if not (1.0 <= segment_seconds <= 30.0):
            raise DockerEventMonitorError("invalid_live_monitor_segment_seconds")
        if not (0.25 <= overlap_seconds < segment_seconds / 2):
            raise DockerEventMonitorError("invalid_live_monitor_overlap_seconds")
        self._max_runtime_ns = int(max_runtime_seconds * 1_000_000_000)
        self._segment_ns = int(segment_seconds * 1_000_000_000)
        self._overlap_ns = int(overlap_seconds * 1_000_000_000)
        # WSL can apply a bounded host-clock correction while monotonic time
        # remains continuous.  Accept only a correction that still leaves a
        # configured overlap margin; larger or ambiguous steps remain fatal.
        self._clock_drift_tolerance_ns = _clock_drift_tolerance_for_overlap(
            self._overlap_ns
        )
        self._segment_reader = segment_reader or (
            lambda until_ns, ready, event: _read_live_segment(
                until_ns, ready, event
            )
        )
        self._runtime_loader = runtime_loader
        self._daemon_identity_loader = daemon_identity_loader
        self._condition = threading.Condition()
        self._segments: list[_Segment] = []
        self._events: list[SanitizedEvent] = []
        self._event_keys: set[tuple[object, ...]] = set()
        self._tracked_ids: set[str] = set()
        self._fatal_error: str | None = None
        self._abort = False
        self._seal_cutoff_ns: int | None = None
        self._started_ns: int | None = None
        self._started_mono_ns: int | None = None
        self._docker_daemon_id: str | None = None
        self._coordinator: threading.Thread | None = None

    @property
    def containers(self) -> tuple[str, ...]:
        return self._containers

    @property
    def docker_daemon_id(self) -> str:
        with self._condition:
            if self._docker_daemon_id is None:
                raise DockerEventMonitorError("live_docker_monitor_not_ready")
            return self._docker_daemon_id

    def __enter__(self) -> LiveDockerEventMonitor:
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _set_error(self, code: str) -> None:
        with self._condition:
            if self._fatal_error is None:
                self._fatal_error = code
            self._condition.notify_all()

    def _load_daemon_identity(self) -> str:
        try:
            return _validated_daemon_id(self._daemon_identity_loader())
        except DockerEventMonitorError:
            raise
        except Exception as exc:
            raise DockerEventMonitorError(
                "live_docker_daemon_binding_load_failed"
            ) from exc

    def _require_same_daemon(self) -> str:
        current = self._load_daemon_identity()
        with self._condition:
            if self._docker_daemon_id is None:
                raise DockerEventMonitorError("live_docker_monitor_not_ready")
            if current != self._docker_daemon_id:
                self._set_error("live_docker_daemon_identity_drift")
                raise DockerEventMonitorError("live_docker_daemon_identity_drift")
        return current

    def _record_event(self, event: SanitizedEvent) -> None:
        with self._condition:
            container_id = str(event["container_id"])
            name = str(event["name"])
            action = str(event["action"])
            if action.startswith("exec_"):
                # Docker does not distinguish its automatic healthcheck exec
                # from an operator issuing the same command.  This evidence is
                # therefore scoped to lifecycle/state events and advertises
                # the exec blind spot in its trust boundary.
                return
            if action == "rename":
                # The first live subscription becomes authoritative before the
                # runtime-ID snapshot.  During that small initialization
                # interval a target rename may be reported only under its new
                # name, before its ID is known locally.  Any rename in this
                # fixed Compose project is therefore a fail-closed condition;
                # silently ignoring an unrelated-looking name could hide the
                # target that moved out of the allowlist.
                self._set_error("project_container_rename_observed")
                return
            if name in self._containers:
                self._tracked_ids.add(container_id)
            if name not in self._containers and container_id not in self._tracked_ids:
                return
            key = (
                container_id,
                name,
                event["action"],
                event["time_nano"],
                event.get("exec_id"),
                event.get("exit_code"),
            )
            if key in self._event_keys:
                return
            if len(self._events) >= _MAX_TARGET_EVENTS:
                self._set_error("live_docker_target_event_limit_exceeded")
                return
            self._event_keys.add(key)
            self._events.append(dict(event))
            self._condition.notify_all()

    def _launch_segment(self) -> None:
        now_ns = time.time_ns()
        now_mono_ns = time.monotonic_ns()
        segment = _Segment(
            segment_id=len(self._segments) + 1,
            requested_at_ns=now_ns,
            requested_at_mono_ns=now_mono_ns,
            until_ns=now_ns + self._segment_ns,
        )
        self._segments.append(segment)

        def on_ready(ready_ns: int, ready_mono_ns: int) -> None:
            with self._condition:
                if segment.ready_at_ns is not None:
                    self._set_error("duplicate_live_docker_segment_ready")
                    return
                segment.ready_at_ns = ready_ns
                segment.ready_at_mono_ns = ready_mono_ns
                self._condition.notify_all()

        def on_event(event: SanitizedEvent) -> None:
            self._record_event(event)
            with self._condition:
                segment.event_count += 1

        def worker() -> None:
            try:
                self._segment_reader(segment.until_ns, on_ready, on_event)
                segment.completed_at_ns = time.time_ns()
                segment.completed_at_mono_ns = time.monotonic_ns()
                if segment.ready_at_ns is None:
                    raise DockerEventMonitorError("live_docker_segment_missing_ready")
                if segment.completed_at_ns < segment.until_ns:
                    raise DockerEventMonitorError("live_docker_segment_early_eof")
                wall_elapsed = segment.completed_at_ns - int(segment.ready_at_ns)
                mono_elapsed = segment.completed_at_mono_ns - int(
                    segment.ready_at_mono_ns
                )
                if abs(wall_elapsed - mono_elapsed) > self._clock_drift_tolerance_ns:
                    raise DockerEventMonitorError("live_docker_segment_clock_drift")
            except Exception as exc:  # fail closed at the thread boundary
                segment.error = str(exc) or type(exc).__name__
                self._set_error(segment.error)
            finally:
                segment.done.set()
                with self._condition:
                    self._condition.notify_all()

        threading.Thread(
            target=worker,
            name=f"aats-docker-events-{segment.segment_id}",
            daemon=True,
        ).start()

    def _coordinator_loop(self) -> None:
        deadline_mono_ns = time.monotonic_ns() + self._max_runtime_ns
        try:
            with self._condition:
                self._launch_segment()
            while True:
                with self._condition:
                    if self._abort or self._fatal_error is not None:
                        return
                    if time.monotonic_ns() >= deadline_mono_ns:
                        self._set_error("live_docker_monitor_runtime_exceeded")
                        return
                    latest = self._segments[-1]
                    seal_cutoff = self._seal_cutoff_ns
                    latest_covers_seal = (
                        seal_cutoff is not None
                        and latest.ready_at_ns is not None
                        and latest.until_ns >= seal_cutoff
                    )
                    if latest_covers_seal:
                        if all(segment.done.is_set() for segment in self._segments):
                            return
                    elif time.time_ns() >= latest.until_ns - self._overlap_ns:
                        self._launch_segment()
                    self._condition.wait(timeout=0.02)
        except Exception as exc:  # pragma: no cover - defensive coordinator boundary
            self._set_error(str(exc) or type(exc).__name__)

    def _initialize_start(self) -> tuple[int, int]:
        with self._condition:
            first = self._segments[0]
            if first.ready_at_ns is None or first.ready_at_mono_ns is None:
                raise DockerEventMonitorError("live_docker_monitor_not_ready")
            # The live subscription was already authoritative before the
            # runtime identity snapshot.  Retain every event from HTTP 200
            # onward so a rename/recreate during that snapshot cannot fall
            # between the two boundaries.
            started_ns = first.ready_at_ns
            started_mono_ns = first.ready_at_mono_ns
        try:
            ids = self._runtime_loader(self._containers)
        except Exception as exc:
            raise DockerEventMonitorError("live_docker_runtime_load_failed") from exc
        self._require_same_daemon()
        with self._condition:
            self._tracked_ids.update(ids.values())
            if self._fatal_error is not None:
                raise DockerEventMonitorError(self._fatal_error)
        return started_ns, started_mono_ns

    def start(self) -> int:
        daemon_id = self._load_daemon_identity()
        with self._condition:
            if self._coordinator is not None:
                if self._started_ns is None:
                    raise DockerEventMonitorError("live_docker_monitor_not_ready")
                if daemon_id != self._docker_daemon_id:
                    self._set_error("live_docker_daemon_identity_drift")
                    raise DockerEventMonitorError("live_docker_daemon_identity_drift")
                return self._started_ns
            self._docker_daemon_id = daemon_id
            self._coordinator = threading.Thread(
                target=self._coordinator_loop,
                name="aats-docker-events-coordinator",
                daemon=True,
            )
            self._coordinator.start()
            deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
            while not self._segments or self._segments[0].ready_at_ns is None:
                if self._fatal_error is not None:
                    raise DockerEventMonitorError(self._fatal_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerEventMonitorError("live_docker_monitor_ready_timeout")
                self._condition.wait(timeout=min(0.05, remaining))
            if not self._segments or self._segments[0].ready_at_ns is None:
                raise DockerEventMonitorError("live_docker_monitor_not_ready")
        started_ns, started_mono_ns = self._initialize_start()
        with self._condition:
            if self._fatal_error is not None:
                raise DockerEventMonitorError(self._fatal_error)
            self._started_ns = started_ns
            self._started_mono_ns = started_mono_ns
            self._condition.notify_all()
            return started_ns

    def health_packet(self) -> dict[str, object]:
        with self._condition:
            if self._started_ns is None or self._fatal_error is not None:
                raise DockerEventMonitorError(
                    self._fatal_error or "live_docker_monitor_not_ready"
                )
            active = [
                segment
                for segment in self._segments
                if segment.ready_at_ns is not None and not segment.done.is_set()
            ]
            if not active:
                raise DockerEventMonitorError("live_docker_monitor_has_no_live_segment")
            return {
                "format_version": _READY_FORMAT_VERSION,
                "coverage_started_ns": self._started_ns,
                "active_segment_until_ns": max(segment.until_ns for segment in active),
                "allowlist": list(self._containers),
                "docker_daemon_id": self._docker_daemon_id,
                "fatal_errors": [],
            }

    def seal(self, cutoff_ns: int, *, timeout: float = _SEAL_TIMEOUT_SECONDS) -> dict[str, object]:
        self._require_same_daemon()
        with self._condition:
            if self._started_ns is None:
                raise DockerEventMonitorError("live_docker_monitor_not_ready")
            if (
                isinstance(cutoff_ns, bool)
                or not isinstance(cutoff_ns, int)
                or cutoff_ns < self._started_ns
            ):
                raise DockerEventMonitorError("invalid_live_docker_seal_cutoff")
            if self._seal_cutoff_ns is not None and self._seal_cutoff_ns != cutoff_ns:
                raise DockerEventMonitorError("live_docker_monitor_already_sealing")
            self._seal_cutoff_ns = cutoff_ns
            self._condition.notify_all()
            deadline = time.monotonic() + timeout
            while self._coordinator is not None and self._coordinator.is_alive():
                if self._fatal_error is not None:
                    raise DockerEventMonitorError(self._fatal_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DockerEventMonitorError("live_docker_monitor_seal_timeout")
                self._condition.wait(timeout=min(0.05, remaining))
            if self._fatal_error is not None:
                raise DockerEventMonitorError(self._fatal_error)
        self._require_same_daemon()
        with self._condition:
            if self._fatal_error is not None:
                raise DockerEventMonitorError(self._fatal_error)
            packet = self._build_packet(cutoff_ns)
        validate_live_window_evidence(
            packet,
            expected_allowlist=self._containers,
            expected_start_ns=self._started_ns,
            expected_cutoff_ns=cutoff_ns,
            expected_daemon_id=self._docker_daemon_id,
        )
        return packet

    def _build_packet(self, cutoff_ns: int) -> dict[str, object]:
        if self._started_ns is None:
            raise DockerEventMonitorError("live_docker_monitor_not_ready")
        if self._docker_daemon_id is None:
            raise DockerEventMonitorError("live_docker_monitor_not_ready")
        segments = [segment for segment in self._segments if segment.ready_at_ns]
        relevant = [
            segment
            for segment in segments
            if int(segment.ready_at_ns or 0) <= cutoff_ns
            and segment.until_ns >= self._started_ns
        ]
        if not relevant or max(segment.until_ns for segment in relevant) < cutoff_ns:
            raise DockerEventMonitorError("live_docker_monitor_incomplete_coverage")
        relevant.sort(key=lambda segment: int(segment.ready_at_ns or 0))
        coverage_end_ns = max(segment.until_ns for segment in relevant)
        # The first Engine request is bounded with ``since`` sampled immediately
        # before the request but can still deliver a short history slice before
        # HTTP-ready.  Preserve
        # those records explicitly rather than allowing them to invalidate the
        # later bounded window or silently dropping them.
        delivered_events = sorted(
            (dict(event) for event in self._events),
            key=lambda event: (
                int(event["time_nano"]),
                str(event["name"]),
                str(event["action"]),
                str(event.get("exec_id", "")),
            ),
        )
        pre_coverage_history_events = [
            event
            for event in delivered_events
            if int(event["time_nano"]) < self._started_ns
        ]
        events = [
            event
            for event in delivered_events
            if int(event["time_nano"]) >= self._started_ns
        ]
        return {
            "format_version": _FORMAT_VERSION,
            "source": "docker_engine_live_stream",
            # Moby's broker has no event sequence or loss marker.  Clean live
            # EOF proves continuous observation of the delivered, project-
            # filtered stream, not adversarial completeness against another
            # Docker-socket writer or a saturated daemon subscriber.
            "complete": False,
            "coverage_status": "BOUNDED_OBSERVED",
            "trust_boundary": {
                "server_filter": "type=container AND com.docker.compose.project=aats-dev",
                "docker_socket_writer_exclusion_verified": False,
                "daemon_event_delivery_loss_detectable": False,
                "daemon_history_capacity_events": 256,
                "http_ready_precedes_broker_subscription_ack": True,
                "daemon_event_clock_alignment_verified": False,
                "ordered_lossless_audit_source_verified": False,
                "healthcheck_origin_distinguishable": False,
                "container_exec_events_observed": False,
                "network_attachment_events_observed": False,
                "cross_container_volume_access_observed": False,
                "project_container_rename_events_fail_closed": True,
                "pre_coverage_history_retained": True,
            },
            "allowlist": list(self._containers),
            "docker_daemon_id": self._docker_daemon_id,
            "coverage_started_ns": self._started_ns,
            "requested_cutoff_ns": cutoff_ns,
            "coverage_ended_ns": coverage_end_ns,
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "requested_at_ns": segment.requested_at_ns,
                    "until_ns": segment.until_ns,
                    "ready_at_ns": segment.ready_at_ns,
                    "completed_at_ns": segment.completed_at_ns,
                    "event_count": segment.event_count,
                    "clean_eof": segment.error is None and segment.done.is_set(),
                }
                for segment in relevant
            ],
            "pre_coverage_history_events": pre_coverage_history_events,
            "events": events,
            "fatal_errors": [],
        }

    def close(self) -> None:
        with self._condition:
            if self._coordinator is None:
                return
            if self._coordinator.is_alive() and self._seal_cutoff_ns is None:
                self._abort = True
                self._condition.notify_all()
            coordinator = self._coordinator
        coordinator.join(timeout=_DEFAULT_SEGMENT_SECONDS + 2.0)


def validate_live_window_evidence(
    packet: object,
    *,
    expected_allowlist: Sequence[str],
    expected_start_ns: int | None = None,
    expected_cutoff_ns: int | None = None,
    expected_daemon_id: str | None = None,
) -> dict[str, object]:
    """Validate clean EOF, overlap continuity and the no-secret event schema."""

    allowlist = _required_allowlist(expected_allowlist)
    if not isinstance(packet, dict):
        raise DockerEventMonitorError("invalid_live_docker_evidence")
    if (
        packet.get("format_version") != _FORMAT_VERSION
        or packet.get("source") != "docker_engine_live_stream"
        or packet.get("complete") is not False
        or packet.get("coverage_status") != "BOUNDED_OBSERVED"
        or packet.get("allowlist") != list(allowlist)
        or packet.get("fatal_errors") != []
    ):
        raise DockerEventMonitorError("invalid_live_docker_evidence")
    if packet.get("trust_boundary") != {
        "server_filter": "type=container AND com.docker.compose.project=aats-dev",
        "docker_socket_writer_exclusion_verified": False,
        "daemon_event_delivery_loss_detectable": False,
        "daemon_history_capacity_events": 256,
        "http_ready_precedes_broker_subscription_ack": True,
        "daemon_event_clock_alignment_verified": False,
        "ordered_lossless_audit_source_verified": False,
        "healthcheck_origin_distinguishable": False,
        "container_exec_events_observed": False,
        "network_attachment_events_observed": False,
        "cross_container_volume_access_observed": False,
        "project_container_rename_events_fail_closed": True,
        "pre_coverage_history_retained": True,
    }:
        raise DockerEventMonitorError("invalid_live_docker_evidence_trust_boundary")
    daemon_id = _validated_daemon_id(packet.get("docker_daemon_id"))
    if expected_daemon_id is not None and daemon_id != _validated_daemon_id(
        expected_daemon_id
    ):
        raise DockerEventMonitorError("live_docker_evidence_daemon_mismatch")
    start_ns = packet.get("coverage_started_ns")
    cutoff_ns = packet.get("requested_cutoff_ns")
    end_ns = packet.get("coverage_ended_ns")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start_ns, cutoff_ns, end_ns)):
        raise DockerEventMonitorError("invalid_live_docker_evidence_window")
    assert isinstance(start_ns, int) and isinstance(cutoff_ns, int) and isinstance(end_ns, int)
    if not (0 < start_ns <= cutoff_ns <= end_ns):
        raise DockerEventMonitorError("invalid_live_docker_evidence_window")
    if expected_start_ns is not None and start_ns != expected_start_ns:
        raise DockerEventMonitorError("live_docker_evidence_start_mismatch")
    if expected_cutoff_ns is not None and cutoff_ns != expected_cutoff_ns:
        raise DockerEventMonitorError("live_docker_evidence_cutoff_mismatch")
    segments = packet.get("segments")
    if not isinstance(segments, list) or not segments:
        raise DockerEventMonitorError("invalid_live_docker_evidence_segments")
    previous_until: int | None = None
    covers_start = False
    covers_cutoff = False
    for index, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict) or segment.get("clean_eof") is not True:
            raise DockerEventMonitorError("invalid_live_docker_evidence_segment")
        segment_id = segment.get("segment_id")
        requested_at = segment.get("requested_at_ns")
        until = segment.get("until_ns")
        ready = segment.get("ready_at_ns")
        completed = segment.get("completed_at_ns")
        event_count = segment.get("event_count")
        if (
            isinstance(segment_id, bool)
            or not isinstance(segment_id, int)
            or segment_id <= 0
            or isinstance(requested_at, bool)
            or not isinstance(requested_at, int)
            or isinstance(until, bool)
            or not isinstance(until, int)
            or isinstance(ready, bool)
            or not isinstance(ready, int)
            or isinstance(completed, bool)
            or not isinstance(completed, int)
            or isinstance(event_count, bool)
            or not isinstance(event_count, int)
            or event_count < 0
            or not (0 < requested_at <= ready <= until <= completed)
        ):
            raise DockerEventMonitorError("invalid_live_docker_evidence_segment")
        if index > 1 and previous_until is not None and ready > previous_until:
            raise DockerEventMonitorError("live_docker_evidence_segment_gap")
        previous_until = until
        covers_start = covers_start or ready <= start_ns <= until
        covers_cutoff = covers_cutoff or ready <= cutoff_ns <= until
    if not covers_start or not covers_cutoff or previous_until != end_ns:
        raise DockerEventMonitorError("live_docker_monitor_incomplete_coverage")
    events = packet.get("events")
    pre_coverage_events = packet.get("pre_coverage_history_events")
    if (
        not isinstance(events, list)
        or not isinstance(pre_coverage_events, list)
        or len(events) + len(pre_coverage_events) > _MAX_TARGET_EVENTS
    ):
        raise DockerEventMonitorError("invalid_live_docker_evidence_events")
    seen: set[tuple[object, ...]] = set()
    for event_set, minimum_time, maximum_time in (
        (pre_coverage_events, 1, start_ns - 1),
        (events, start_ns, end_ns),
    ):
        last_time = 0
        for event in event_set:
            sanitized = sanitize_docker_event(
                {
                    "Type": "container",
                    "Action": event.get("action") if isinstance(event, dict) else None,
                    "Actor": {
                        "ID": event.get("container_id") if isinstance(event, dict) else None,
                        "Attributes": {
                            "name": event.get("name") if isinstance(event, dict) else None,
                            **(
                                {"execID": event.get("exec_id")}
                                if isinstance(event, dict) and "exec_id" in event
                                else {}
                            ),
                            **(
                                {"exitCode": str(event.get("exit_code"))}
                                if isinstance(event, dict) and "exit_code" in event
                                else {}
                            ),
                        },
                    },
                    "timeNano": event.get("time_nano") if isinstance(event, dict) else None,
                }
            )
            if sanitized != event:
                raise DockerEventMonitorError("invalid_live_docker_evidence_event")
            if str(event["name"]) not in allowlist:
                raise DockerEventMonitorError(
                    "live_docker_evidence_event_outside_allowlist"
                )
            event_time = int(event["time_nano"])
            if not (minimum_time <= event_time <= maximum_time) or event_time < last_time:
                raise DockerEventMonitorError("invalid_live_docker_evidence_event_time")
            key = tuple(sorted(event.items()))
            if key in seen:
                raise DockerEventMonitorError("duplicate_live_docker_evidence_event")
            seen.add(key)
            last_time = event_time
    return packet


def _atomic_json(path: Path, payload: Mapping[str, object], *, mode: int = 0o600) -> None:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _load_json(path: Path, *, max_bytes: int = 10 * 1024 * 1024) -> dict[str, object]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
        raise DockerEventMonitorError("invalid_live_monitor_packet")
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockerEventMonitorError("invalid_live_monitor_packet") from exc
    if not isinstance(payload, dict):
        raise DockerEventMonitorError("invalid_live_monitor_packet")
    return payload


def _validated_metadata(metadata: Mapping[str, object]) -> dict[str, str]:
    expected_keys = {
        "deployment_lock_id",
        "runtime_readiness_generation",
        "deployed_commit",
    }
    if set(metadata) != expected_keys:
        raise DockerEventMonitorError("invalid_live_monitor_metadata")
    lock_id = metadata.get("deployment_lock_id")
    generation = metadata.get("runtime_readiness_generation")
    commit = metadata.get("deployed_commit")
    if (
        not isinstance(lock_id, str)
        or not _TOKEN_RE.fullmatch(lock_id)
        or not isinstance(generation, str)
        or not _TOKEN_RE.fullmatch(generation)
        or not isinstance(commit, str)
        or not _COMMIT_RE.fullmatch(commit)
    ):
        raise DockerEventMonitorError("invalid_live_monitor_metadata")
    return {
        "deployment_lock_id": lock_id,
        "runtime_readiness_generation": generation,
        "deployed_commit": commit,
    }


def _validate_control_dir(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if resolved.parent != Path("/tmp") or not re.fullmatch(
        r"aats-docker-event-monitor-[A-Za-z0-9._-]{1,160}", resolved.name
    ):
        raise DockerEventMonitorError("invalid_live_monitor_control_dir")
    return resolved


def run_monitor_daemon(
    *,
    control_dir: Path,
    token: str,
    containers: Sequence[str],
    metadata: Mapping[str, object],
    max_runtime_seconds: float,
) -> int:
    directory = _validate_control_dir(control_dir)
    if not _TOKEN_RE.fullmatch(token):
        raise DockerEventMonitorError("invalid_live_monitor_token")
    allowlist = _required_allowlist(containers)
    bound_metadata = _validated_metadata(metadata)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    monitor = LiveDockerEventMonitor(
        allowlist,
        max_runtime_seconds=max_runtime_seconds,
    )
    try:
        started_ns = monitor.start()
        ready = {
            "format_version": _READY_FORMAT_VERSION,
            "token": token,
            "pid": os.getpid(),
            "allowlist": list(allowlist),
            "coverage_started_ns": started_ns,
            "docker_daemon_id": monitor.docker_daemon_id,
            "metadata": bound_metadata,
        }
        _atomic_json(directory / "ready.json", ready, mode=0o400)
        deadline = time.monotonic() + max_runtime_seconds
        while time.monotonic() < deadline:
            if (directory / "cancel").is_file():
                monitor.close()
                _atomic_json(
                    directory / "failed.json",
                    {
                        "format_version": _FAILED_FORMAT_VERSION,
                        "token": token,
                        "error": "live_monitor_cancelled",
                    },
                    mode=0o400,
                )
                return 2
            request_path = directory / "seal-request.json"
            if request_path.is_file():
                request = _load_json(request_path)
                if request.get("token") != token:
                    raise DockerEventMonitorError("live_monitor_seal_token_mismatch")
                cutoff_ns = request.get("cutoff_ns")
                if isinstance(cutoff_ns, bool) or not isinstance(cutoff_ns, int):
                    raise DockerEventMonitorError("invalid_live_monitor_seal_cutoff")
                evidence = monitor.seal(cutoff_ns)
                sealed = {
                    "format_version": _SEALED_FORMAT_VERSION,
                    "token": token,
                    "pid": os.getpid(),
                    "docker_daemon_id": monitor.docker_daemon_id,
                    "metadata": bound_metadata,
                    "evidence": evidence,
                }
                _atomic_json(directory / "sealed.json", sealed, mode=0o400)
                return 0
            monitor.health_packet()
            time.sleep(0.05)
        raise DockerEventMonitorError("live_monitor_control_timeout")
    except Exception as exc:
        monitor.close()
        try:
            _atomic_json(
                directory / "failed.json",
                {
                    "format_version": _FAILED_FORMAT_VERSION,
                    "token": token,
                    "error": str(exc) or type(exc).__name__,
                },
                mode=0o400,
            )
        except Exception:
            pass
        return 1


def load_external_monitor_ready(
    control_dir: Path,
    *,
    token: str,
    expected_allowlist: Sequence[str],
    expected_metadata: Mapping[str, object],
) -> dict[str, object]:
    directory = _validate_control_dir(control_dir)
    ready = _load_json(directory / "ready.json")
    metadata = _validated_metadata(expected_metadata)
    if (
        ready.get("format_version") != _READY_FORMAT_VERSION
        or ready.get("token") != token
        or ready.get("allowlist") != list(_required_allowlist(expected_allowlist))
        or ready.get("metadata") != metadata
    ):
        raise DockerEventMonitorError("invalid_live_monitor_ready_packet")
    pid = ready.get("pid")
    started_ns = ready.get("coverage_started_ns")
    ready_daemon_id = ready.get("docker_daemon_id")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(started_ns, bool)
        or not isinstance(started_ns, int)
        or started_ns <= 0
        or not isinstance(ready_daemon_id, str)
    ):
        raise DockerEventMonitorError("invalid_live_monitor_ready_packet")
    current_daemon_id = validate_local_docker_daemon_binding()
    if _validated_daemon_id(ready_daemon_id) != current_daemon_id:
        raise DockerEventMonitorError("live_monitor_ready_daemon_mismatch")
    try:
        os.kill(pid, 0)
    except OSError as exc:
        raise DockerEventMonitorError("live_monitor_process_not_running") from exc
    return ready


def seal_external_monitor(
    control_dir: Path,
    *,
    token: str,
    cutoff_ns: int,
    expected_allowlist: Sequence[str],
    expected_metadata: Mapping[str, object],
    timeout_seconds: float = _SEAL_TIMEOUT_SECONDS,
) -> dict[str, object]:
    directory = _validate_control_dir(control_dir)
    ready = load_external_monitor_ready(
        directory,
        token=token,
        expected_allowlist=expected_allowlist,
        expected_metadata=expected_metadata,
    )
    request_path = directory / "seal-request.json"
    _atomic_json(
        request_path,
        {"token": token, "cutoff_ns": cutoff_ns},
        mode=0o400,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        failed_path = directory / "failed.json"
        if failed_path.is_file():
            failed = _load_json(failed_path)
            raise DockerEventMonitorError(
                f"external_live_monitor_failed:{failed.get('error', 'unknown')}"
            )
        sealed_path = directory / "sealed.json"
        if sealed_path.is_file():
            sealed = _load_json(sealed_path)
            if (
                sealed.get("format_version") != _SEALED_FORMAT_VERSION
                or sealed.get("token") != token
                or sealed.get("metadata") != _validated_metadata(expected_metadata)
                or sealed.get("docker_daemon_id") != ready.get("docker_daemon_id")
            ):
                raise DockerEventMonitorError("invalid_live_monitor_sealed_packet")
            evidence = sealed.get("evidence")
            validated = validate_live_window_evidence(
                evidence,
                expected_allowlist=expected_allowlist,
                expected_start_ns=int(ready["coverage_started_ns"]),
                expected_cutoff_ns=cutoff_ns,
                expected_daemon_id=str(ready["docker_daemon_id"]),
            )
            if validate_local_docker_daemon_binding() != ready["docker_daemon_id"]:
                raise DockerEventMonitorError("live_monitor_daemon_drift")
            return validated
        time.sleep(0.05)
    raise DockerEventMonitorError("external_live_monitor_seal_timeout")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon-binding")
    subparsers.add_parser("daemon-binding-envelope")
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--control-dir", type=Path, required=True)
    daemon.add_argument("--token", required=True)
    daemon.add_argument("--container", action="append", default=[])
    daemon.add_argument("--deployment-lock-id", required=True)
    daemon.add_argument("--runtime-readiness-generation", required=True)
    daemon.add_argument("--deployed-commit", required=True)
    daemon.add_argument("--max-runtime-seconds", type=float, required=True)
    ready = subparsers.add_parser("ready")
    ready.add_argument("--control-dir", type=Path, required=True)
    ready.add_argument("--token", required=True)
    ready.add_argument("--container", action="append", default=[])
    ready.add_argument("--deployment-lock-id", required=True)
    ready.add_argument("--runtime-readiness-generation", required=True)
    ready.add_argument("--deployed-commit", required=True)
    ready.add_argument(
        "--output",
        choices=("coverage-started-ns", "pid-and-coverage"),
        default="coverage-started-ns",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "daemon-binding":
        print(validate_local_docker_daemon_binding())
        return 0
    if args.command == "daemon-binding-envelope":
        print(
            daemon_binding_transport_envelope(
                validate_local_docker_daemon_binding()
            )
        )
        return 0
    metadata = {
        "deployment_lock_id": args.deployment_lock_id,
        "runtime_readiness_generation": args.runtime_readiness_generation,
        "deployed_commit": args.deployed_commit,
    }
    if args.command == "daemon":
        return run_monitor_daemon(
            control_dir=args.control_dir,
            token=args.token,
            containers=args.container,
            metadata=metadata,
            max_runtime_seconds=args.max_runtime_seconds,
        )
    ready = load_external_monitor_ready(
        args.control_dir,
        token=args.token,
        expected_allowlist=args.container,
        expected_metadata=metadata,
    )
    if args.output == "pid-and-coverage":
        print(f"{ready['pid']} {ready['coverage_started_ns']}")
    else:
        print(ready["coverage_started_ns"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
