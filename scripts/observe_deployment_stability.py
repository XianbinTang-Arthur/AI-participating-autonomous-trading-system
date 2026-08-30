#!/usr/bin/env python3
"""Observe one simulation deployment health boundary inside a single WSL process.

The command emits only deterministic ``key=value`` facts.  It deliberately keeps
the 40 second clock and all Docker/HTTP probes on the WSL side so Windows-to-WSL
transport latency cannot be mistaken for observation time.
"""

from __future__ import annotations

import argparse
import http.client
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import write_deployment_evidence as evidence  # noqa: E402


_OBSERVATION_SECONDS = 40.0
_SAMPLE_INTERVAL_SECONDS = 2.0
_GATEWAY_TIMEOUT_SECONDS = 2.0
_NANOSECONDS_PER_SECOND = 1_000_000_000
_WALL_CLOCK_NS_RE = re.compile(r"^[1-9][0-9]{18}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_KEY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_OUTPUT_VALUE_RE = re.compile(
    r"^(?:[0-9]+(?:\.[0-9]{3})?|sha256:[0-9a-f]{64})$"
)


CommandRunner = Callable[[Sequence[str], Path | None], str]
ContainerProbe = Callable[[int | None], str]
NatsProbe = Callable[[int, str | None, bool], Mapping[str, object]]
GatewayProbe = Callable[[int], None]
HeartbeatReader = Callable[[str, str], int]
NanosecondClock = Callable[[], int]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class StabilityObservation:
    health_boundary_started_ns: int
    health_boundary_app_fingerprint: str
    collector_heartbeat_epochs: Mapping[str, int]
    observation_elapsed_ns: int
    sample_count: int


def _validated_required_containers(
    profile: str,
    required_containers: Sequence[str],
) -> tuple[str, ...]:
    try:
        expected = evidence._REQUIRED_CONTAINERS_BY_PROFILE[profile]
    except KeyError as exc:
        raise ValueError("unsupported_simulation_profile") from exc
    if len(required_containers) != len(expected) or set(required_containers) != set(
        expected
    ):
        raise ValueError("required_container_set_mismatch")
    return expected


def _validated_nanoseconds(value: object, *, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(error)
    return value


def _validated_wall_clock_ns(value: object) -> int:
    value_ns = _validated_nanoseconds(value, error="invalid_health_boundary_clock")
    if _WALL_CLOCK_NS_RE.fullmatch(str(value_ns)) is None:
        raise RuntimeError("invalid_health_boundary_clock")
    return value_ns


def _validated_heartbeat_epoch(value: object, *, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > 999_999_999_999
    ):
        raise RuntimeError(f"invalid_collector_heartbeat_epoch:{name}")
    return value


def _build_container_probe(
    *,
    names: tuple[str, ...],
    profile: str,
    runtime_readiness_generation: str,
    deployed_commit: str,
    nats_target_manifest_sha256: str,
    run: CommandRunner,
) -> ContainerProbe:
    image_id = run(
        ("docker", "image", "inspect", "aats-base:dev", "--format", "{{.Id}}"),
        None,
    ).strip()
    if evidence._IMAGE_RE.fullmatch(image_id) is None:
        raise RuntimeError("invalid_base_image_id")

    def probe(health_window_started_ns: int | None) -> str:
        container_facts, gateway_bindings = evidence._container_snapshot(
            names,
            expected_image_id=image_id,
            expected_generation=runtime_readiness_generation,
            expected_commit=deployed_commit,
            expected_profile=profile,
            expected_target_manifest_sha256=nats_target_manifest_sha256,
            run=run,
            health_window_started_ns=health_window_started_ns,
        )
        return evidence._app_runtime_snapshot_fingerprint(
            container_facts,
            gateway_bindings,
        )

    return probe


def _build_nats_probe(run: CommandRunner) -> NatsProbe:
    def probe(
        health_window_started_ns: int,
        expected_container_id: str | None,
        require_success_after_boundary: bool,
    ) -> Mapping[str, object]:
        return evidence.capture_shared_nats_health_snapshot(
            lambda args: run(args, None),
            health_window_started_ns=health_window_started_ns,
            expected_container_id=expected_container_id,
            require_success_after_boundary=require_success_after_boundary,
        )

    return probe


def _probe_loopback_gateway(port: int) -> None:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=_GATEWAY_TIMEOUT_SECONDS,
    )
    try:
        connection.request("GET", "/healthz", headers={"Host": "127.0.0.1"})
        response = connection.getresponse()
        if response.status != http.HTTPStatus.OK:
            raise RuntimeError("gateway_health_check_failed")
        response.read(1)
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError("gateway_health_check_failed") from exc
    finally:
        connection.close()


def _capture_collector_heartbeats(
    names: Sequence[str],
    *,
    heartbeat_reader: HeartbeatReader,
) -> dict[str, int]:
    heartbeats: dict[str, int] = {}
    for name in sorted(names):
        heartbeat_path = evidence._COLLECTOR_HEARTBEATS.get(name)
        if heartbeat_path is None:
            continue
        heartbeats[name] = _validated_heartbeat_epoch(
            heartbeat_reader(name, heartbeat_path),
            name=name,
        )
    return heartbeats


def _wait_until(
    target_ns: int,
    *,
    monotonic_clock_ns: NanosecondClock,
    sleep: Sleeper,
) -> int:
    now_ns = _validated_nanoseconds(
        monotonic_clock_ns(),
        error="invalid_monotonic_clock",
    )
    if now_ns < target_ns:
        sleep((target_ns - now_ns) / _NANOSECONDS_PER_SECOND)
        now_ns = _validated_nanoseconds(
            monotonic_clock_ns(),
            error="invalid_monotonic_clock",
        )
        if now_ns < target_ns:
            raise RuntimeError("monotonic_sleep_returned_early")
    return now_ns


def observe_deployment_stability(
    *,
    profile: str,
    required_containers: Sequence[str],
    runtime_readiness_generation: str,
    deployed_commit: str,
    nats_target_manifest_sha256: str,
    gateway_port: int,
    gateway_scheme: str = "http",
    run: CommandRunner = evidence._run_command,
    container_probe: ContainerProbe | None = None,
    nats_probe: NatsProbe | None = None,
    gateway_probe: GatewayProbe = _probe_loopback_gateway,
    heartbeat_reader: HeartbeatReader = evidence._copy_container_file_mtime,
    wall_clock_ns: NanosecondClock = time.time_ns,
    monotonic_clock_ns: NanosecondClock = time.monotonic_ns,
    sleep: Sleeper = time.sleep,
    observation_seconds: float = _OBSERVATION_SECONDS,
    sample_interval_seconds: float = _SAMPLE_INTERVAL_SECONDS,
) -> StabilityObservation:
    """Observe current health until a real monotonic deadline.

    ``observation_seconds`` and ``sample_interval_seconds`` are injectable for
    deterministic unit tests.  The CLI intentionally exposes no override and
    therefore always uses the production 40s/2s contract.
    """

    names = _validated_required_containers(profile, required_containers)
    if not evidence._READINESS_GENERATION_RE.fullmatch(runtime_readiness_generation):
        raise ValueError("invalid_runtime_readiness_generation")
    if not evidence._COMMIT_RE.fullmatch(deployed_commit):
        raise ValueError("invalid_deployed_commit")
    if not evidence._SHA256_RE.fullmatch(nats_target_manifest_sha256):
        raise ValueError("invalid_nats_target_manifest_sha256")
    if (
        gateway_scheme != "http"
        or
        isinstance(gateway_port, bool)
        or not isinstance(gateway_port, int)
        or not 1 <= gateway_port <= 65535
    ):
        raise ValueError("invalid_gateway_port")
    if (
        isinstance(observation_seconds, bool)
        or not isinstance(observation_seconds, (int, float))
        or observation_seconds <= 0
        or isinstance(sample_interval_seconds, bool)
        or not isinstance(sample_interval_seconds, (int, float))
        or sample_interval_seconds <= 0
        or sample_interval_seconds > observation_seconds
    ):
        raise ValueError("invalid_observation_timing")

    duration_ns = int(observation_seconds * _NANOSECONDS_PER_SECOND)
    interval_ns = int(sample_interval_seconds * _NANOSECONDS_PER_SECOND)
    if duration_ns <= 0 or interval_ns <= 0:
        raise ValueError("invalid_observation_timing")

    if container_probe is None:
        container_probe = _build_container_probe(
            names=names,
            profile=profile,
            runtime_readiness_generation=runtime_readiness_generation,
            deployed_commit=deployed_commit,
            nats_target_manifest_sha256=nats_target_manifest_sha256,
            run=run,
        )
    if nats_probe is None:
        nats_probe = _build_nats_probe(run)

    # Docker archive-path is observable by the lifecycle monitor.  Capture each
    # collector exactly once before fixing the authoritative health boundary.
    collector_heartbeats = _capture_collector_heartbeats(
        names,
        heartbeat_reader=heartbeat_reader,
    )
    boundary_started_ns = _validated_wall_clock_ns(wall_clock_ns())
    started_mono_ns = _validated_nanoseconds(
        monotonic_clock_ns(),
        error="invalid_monotonic_clock",
    )
    deadline_mono_ns = started_mono_ns + duration_ns

    initial_fingerprint: str | None = None
    expected_nats_container_id: str | None = None
    sample_count = 0

    def sample(*, final: bool) -> None:
        nonlocal initial_fingerprint, expected_nats_container_id, sample_count
        fingerprint = container_probe(boundary_started_ns if final else None)
        if not isinstance(fingerprint, str) or not evidence._SHA256_RE.fullmatch(
            fingerprint
        ):
            raise RuntimeError("invalid_app_runtime_fingerprint")
        if initial_fingerprint is None:
            initial_fingerprint = fingerprint
        elif fingerprint != initial_fingerprint:
            raise RuntimeError("app_runtime_changed_during_stability_observation")

        nats_health = nats_probe(
            boundary_started_ns,
            expected_nats_container_id,
            final,
        )
        nats_container_id = nats_health.get("container_id")
        if not isinstance(nats_container_id, str) or _CONTAINER_ID_RE.fullmatch(
            nats_container_id
        ) is None:
            raise RuntimeError("invalid_nats_health_container_id")
        if expected_nats_container_id is None:
            expected_nats_container_id = nats_container_id
        elif nats_container_id != expected_nats_container_id:
            raise RuntimeError("nats_runtime_changed_during_stability_observation")

        gateway_probe(gateway_port)
        sample_count += 1

    next_sample_ns = started_mono_ns
    while next_sample_ns < deadline_mono_ns:
        _wait_until(
            next_sample_ns,
            monotonic_clock_ns=monotonic_clock_ns,
            sleep=sleep,
        )
        sample(final=False)
        completed_ns = _validated_nanoseconds(
            monotonic_clock_ns(),
            error="invalid_monotonic_clock",
        )
        next_sample_ns += interval_ns
        if completed_ns > next_sample_ns:
            raise RuntimeError("stability_sample_cadence_exceeded")

    _wait_until(
        deadline_mono_ns,
        monotonic_clock_ns=monotonic_clock_ns,
        sleep=sleep,
    )
    # The final probes parse every retained app/NATS healthcheck since the wall
    # boundary and require at least one successful check in that window.
    sample(final=True)
    ended_mono_ns = _validated_nanoseconds(
        monotonic_clock_ns(),
        error="invalid_monotonic_clock",
    )
    if ended_mono_ns > deadline_mono_ns + interval_ns:
        raise RuntimeError("stability_sample_cadence_exceeded")
    if ended_mono_ns < deadline_mono_ns or initial_fingerprint is None:
        raise RuntimeError("stability_observation_window_too_short")

    return StabilityObservation(
        health_boundary_started_ns=boundary_started_ns,
        health_boundary_app_fingerprint=initial_fingerprint,
        collector_heartbeat_epochs=dict(collector_heartbeats),
        observation_elapsed_ns=ended_mono_ns - started_mono_ns,
        sample_count=sample_count,
    )


def observation_output_lines(observation: StabilityObservation) -> tuple[str, ...]:
    _validated_wall_clock_ns(observation.health_boundary_started_ns)
    if evidence._SHA256_RE.fullmatch(
        observation.health_boundary_app_fingerprint
    ) is None:
        raise RuntimeError("invalid_observation_output")
    if (
        isinstance(observation.observation_elapsed_ns, bool)
        or not isinstance(observation.observation_elapsed_ns, int)
        or observation.observation_elapsed_ns
        < int(_OBSERVATION_SECONDS * _NANOSECONDS_PER_SECOND)
        or isinstance(observation.sample_count, bool)
        or not isinstance(observation.sample_count, int)
        or observation.sample_count <= 0
    ):
        raise RuntimeError("invalid_observation_output")
    for name, epoch in observation.collector_heartbeat_epochs.items():
        if name not in evidence._COLLECTOR_HEARTBEATS:
            raise RuntimeError("invalid_observation_output")
        _validated_heartbeat_epoch(epoch, name=name)

    rows: list[tuple[str, str]] = [
        (
            "health_boundary_started_ns",
            str(observation.health_boundary_started_ns),
        ),
        (
            "health_boundary_app_fingerprint",
            observation.health_boundary_app_fingerprint,
        ),
        (
            "observed_stability_seconds",
            f"{observation.observation_elapsed_ns / _NANOSECONDS_PER_SECOND:.3f}",
        ),
        ("sample_count", str(observation.sample_count)),
    ]
    rows.extend(
        (
            f"collector_heartbeat_epoch:{name}",
            str(epoch),
        )
        for name, epoch in sorted(observation.collector_heartbeat_epochs.items())
    )
    lines: list[str] = []
    for key, value in rows:
        if (
            _OUTPUT_KEY_RE.fullmatch(key) is None
            or _OUTPUT_VALUE_RE.fullmatch(value) is None
        ):
            raise RuntimeError("invalid_observation_output")
        lines.append(f"{key}={value}")
    return tuple(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("spot", "derivatives"), required=True)
    parser.add_argument("--runtime-readiness-generation", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--nats-target-manifest-sha256", required=True)
    parser.add_argument("--gateway-scheme", choices=("http",), required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--stability-seconds", type=float, required=True)
    parser.add_argument("--required-container", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.stability_seconds != _OBSERVATION_SECONDS:
            raise ValueError("invalid_observation_timing")
        observation = observe_deployment_stability(
            profile=args.profile,
            required_containers=args.required_container,
            runtime_readiness_generation=args.runtime_readiness_generation,
            deployed_commit=args.deployed_commit,
            nats_target_manifest_sha256=args.nats_target_manifest_sha256,
            gateway_scheme=args.gateway_scheme,
            gateway_port=args.gateway_port,
            observation_seconds=args.stability_seconds,
        )
        for line in observation_output_lines(observation):
            print(line)
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        UnicodeError,
    ):
        # Stdout is a machine contract.  Keep stderr fixed and no-secret; unit
        # tests exercise the specific direct-call exceptions.
        print("deployment_stability_observation_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
