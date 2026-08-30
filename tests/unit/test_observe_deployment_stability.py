from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from scripts import observe_deployment_stability as observer


BOUNDARY_NS = 1_777_777_777_000_000_000
DEPLOYED_COMMIT = "a" * 40
READINESS_GENERATION = "generation-1"
TARGET_MANIFEST_SHA256 = "sha256:" + "b" * 64
APP_FINGERPRINT = "sha256:" + "c" * 64
NATS_CONTAINER_ID = "d" * 64


class _FakeClock:
    def __init__(self) -> None:
        self.now_ns = 0
        self.sleep_calls: list[float] = []

    def monotonic_ns(self) -> int:
        return self.now_ns

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleep_calls.append(seconds)
        self.now_ns += round(seconds * 1_000_000_000)

    def advance(self, seconds: float) -> None:
        self.now_ns += round(seconds * 1_000_000_000)


def _successful_nats_probe(
    _boundary_ns: int,
    _expected_container_id: str | None,
    _require_success_after_boundary: bool,
) -> Mapping[str, object]:
    return {"container_id": NATS_CONTAINER_ID}


def _observe(
    *,
    clock: _FakeClock,
    container_probe: observer.ContainerProbe,
    nats_probe: observer.NatsProbe = _successful_nats_probe,
    gateway_probe: observer.GatewayProbe = lambda _port: None,
    heartbeat_reader: observer.HeartbeatReader = lambda _name, _path: 1_777_777_776,
    wall_clock_ns: observer.NanosecondClock = lambda: BOUNDARY_NS,
    observation_seconds: float = 4.0,
    sample_interval_seconds: float = 2.0,
) -> observer.StabilityObservation:
    names = observer.evidence._REQUIRED_CONTAINERS_BY_PROFILE["derivatives"]
    return observer.observe_deployment_stability(
        profile="derivatives",
        required_containers=names,
        runtime_readiness_generation=READINESS_GENERATION,
        deployed_commit=DEPLOYED_COMMIT,
        nats_target_manifest_sha256=TARGET_MANIFEST_SHA256,
        gateway_scheme="http",
        gateway_port=8000,
        container_probe=container_probe,
        nats_probe=nats_probe,
        gateway_probe=gateway_probe,
        heartbeat_reader=heartbeat_reader,
        wall_clock_ns=wall_clock_ns,
        monotonic_clock_ns=clock.monotonic_ns,
        sleep=clock.sleep,
        observation_seconds=observation_seconds,
        sample_interval_seconds=sample_interval_seconds,
    )


def test_observer_captures_heartbeats_before_boundary_and_verifies_final_window() -> (
    None
):
    clock = _FakeClock()
    events: list[str] = []
    container_boundaries: list[int | None] = []
    nats_calls: list[tuple[int, str | None, bool]] = []
    gateway_ports: list[int] = []

    def heartbeat_reader(name: str, _path: str) -> int:
        events.append(f"heartbeat:{name}")
        return 1_777_777_776

    def wall_clock_ns() -> int:
        events.append("boundary")
        return BOUNDARY_NS

    def container_probe(boundary_ns: int | None) -> str:
        container_boundaries.append(boundary_ns)
        return APP_FINGERPRINT

    def nats_probe(
        boundary_ns: int,
        expected_container_id: str | None,
        require_success_after_boundary: bool,
    ) -> Mapping[str, object]:
        nats_calls.append(
            (
                boundary_ns,
                expected_container_id,
                require_success_after_boundary,
            )
        )
        return {"container_id": NATS_CONTAINER_ID}

    result = _observe(
        clock=clock,
        container_probe=container_probe,
        nats_probe=nats_probe,
        gateway_probe=gateway_ports.append,
        heartbeat_reader=heartbeat_reader,
        wall_clock_ns=wall_clock_ns,
    )

    assert events[-1] == "boundary"
    assert events.count("boundary") == 1
    assert events[:2] == [
        "heartbeat:aats-liquidations-daemon",
        "heartbeat:aats-microstructure-collector",
    ]
    assert container_boundaries == [None, None, BOUNDARY_NS]
    assert nats_calls == [
        (BOUNDARY_NS, None, False),
        (BOUNDARY_NS, NATS_CONTAINER_ID, False),
        (BOUNDARY_NS, NATS_CONTAINER_ID, True),
    ]
    assert gateway_ports == [8000, 8000, 8000]
    assert result.health_boundary_started_ns == BOUNDARY_NS
    assert result.health_boundary_app_fingerprint == APP_FINGERPRINT
    assert result.observation_elapsed_ns == 4_000_000_000
    assert result.sample_count == 3
    assert result.collector_heartbeat_epochs == {
        "aats-liquidations-daemon": 1_777_777_776,
        "aats-microstructure-collector": 1_777_777_776,
    }
    assert clock.sleep_calls == [2.0, 2.0]


def test_observer_uses_real_monotonic_deadline_including_probe_time() -> None:
    clock = _FakeClock()
    boundaries: list[int | None] = []

    def slow_container_probe(boundary_ns: int | None) -> str:
        boundaries.append(boundary_ns)
        clock.advance(1.5)
        return APP_FINGERPRINT

    result = _observe(clock=clock, container_probe=slow_container_probe)

    assert boundaries == [None, None, BOUNDARY_NS]
    assert clock.sleep_calls == [0.5, 0.5]
    assert result.observation_elapsed_ns == 5_500_000_000
    assert result.sample_count == 3


def test_observer_fails_closed_when_a_probe_misses_two_second_cadence() -> None:
    clock = _FakeClock()

    def late_container_probe(_boundary_ns: int | None) -> str:
        clock.advance(2.1)
        return APP_FINGERPRINT

    with pytest.raises(RuntimeError, match="stability_sample_cadence_exceeded"):
        _observe(clock=clock, container_probe=late_container_probe)


def test_observer_fails_closed_when_only_final_probe_misses_cadence() -> None:
    clock = _FakeClock()

    def late_final_container_probe(boundary_ns: int | None) -> str:
        if boundary_ns is not None:
            clock.advance(2.1)
        return APP_FINGERPRINT

    with pytest.raises(RuntimeError, match="stability_sample_cadence_exceeded"):
        _observe(clock=clock, container_probe=late_final_container_probe)


def test_observer_rejects_app_fingerprint_drift() -> None:
    clock = _FakeClock()
    fingerprints = iter((APP_FINGERPRINT, "sha256:" + "e" * 64))

    with pytest.raises(
        RuntimeError,
        match="app_runtime_changed_during_stability_observation",
    ):
        _observe(clock=clock, container_probe=lambda _boundary: next(fingerprints))


def test_observer_fails_closed_on_gateway_or_nats_probe() -> None:
    gateway_clock = _FakeClock()

    def bad_gateway(_port: int) -> None:
        raise RuntimeError("gateway_health_check_failed")

    with pytest.raises(RuntimeError, match="gateway_health_check_failed"):
        _observe(
            clock=gateway_clock,
            container_probe=lambda _boundary: APP_FINGERPRINT,
            gateway_probe=bad_gateway,
        )

    nats_clock = _FakeClock()

    def bad_nats(
        _boundary: int,
        _expected: str | None,
        _require_success: bool,
    ) -> Mapping[str, object]:
        raise RuntimeError("nats_runtime_health_failed_after_boundary")

    with pytest.raises(
        RuntimeError,
        match="nats_runtime_health_failed_after_boundary",
    ):
        _observe(
            clock=nats_clock,
            container_probe=lambda _boundary: APP_FINGERPRINT,
            nats_probe=bad_nats,
        )


def test_observer_rejects_nats_identity_change() -> None:
    clock = _FakeClock()
    identities = iter((NATS_CONTAINER_ID, "e" * 64))

    def nats_probe(
        _boundary: int,
        _expected: str | None,
        _require_success: bool,
    ) -> Mapping[str, object]:
        return {"container_id": next(identities)}

    with pytest.raises(
        RuntimeError,
        match="nats_runtime_changed_during_stability_observation",
    ):
        _observe(
            clock=clock,
            container_probe=lambda _boundary: APP_FINGERPRINT,
            nats_probe=nats_probe,
        )


def test_default_container_probe_reuses_one_batch_snapshot_per_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = observer.evidence._REQUIRED_CONTAINERS_BY_PROFILE["derivatives"]
    calls: list[dict[str, object]] = []

    def run(args: Sequence[str], _cwd: Path | None = None) -> str:
        assert tuple(args) == (
            "docker",
            "image",
            "inspect",
            "aats-base:dev",
            "--format",
            "{{.Id}}",
        )
        return "sha256:" + "f" * 64

    def batch_snapshot(
        actual_names: Sequence[str],
        **kwargs: object,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        calls.append({"names": tuple(actual_names), **kwargs})
        return (
            [{"name": name} for name in actual_names],
            [
                {
                    "container_port": "8000/tcp",
                    "host_ip": "127.0.0.1",
                    "host_port": "8000",
                }
            ],
        )

    monkeypatch.setattr(observer.evidence, "_container_snapshot", batch_snapshot)
    probe = observer._build_container_probe(
        names=names,
        profile="derivatives",
        runtime_readiness_generation=READINESS_GENERATION,
        deployed_commit=DEPLOYED_COMMIT,
        nats_target_manifest_sha256=TARGET_MANIFEST_SHA256,
        run=run,
    )

    first = probe(None)
    final = probe(BOUNDARY_NS)

    assert first == final
    assert len(calls) == 2
    assert calls[0]["names"] == names
    assert calls[1]["names"] == names
    assert calls[0]["health_window_started_ns"] is None
    assert calls[1]["health_window_started_ns"] == BOUNDARY_NS
    assert calls[0]["run"] is run


def test_output_is_deterministic_strict_key_value_and_no_secret() -> None:
    observation = observer.StabilityObservation(
        health_boundary_started_ns=BOUNDARY_NS,
        health_boundary_app_fingerprint=APP_FINGERPRINT,
        collector_heartbeat_epochs={
            "aats-microstructure-collector": 1_777_777_776,
            "aats-liquidations-daemon": 1_777_777_775,
        },
        observation_elapsed_ns=40_100_000_000,
        sample_count=21,
    )

    assert observer.observation_output_lines(observation) == (
        f"health_boundary_started_ns={BOUNDARY_NS}",
        f"health_boundary_app_fingerprint={APP_FINGERPRINT}",
        "observed_stability_seconds=40.100",
        "sample_count=21",
        "collector_heartbeat_epoch:aats-liquidations-daemon=1777777775",
        "collector_heartbeat_epoch:aats-microstructure-collector=1777777776",
    )
    assert all(
        line.count("=") == 1 for line in observer.observation_output_lines(observation)
    )

    with pytest.raises(RuntimeError, match="invalid_observation_output"):
        observer.observation_output_lines(
            observer.StabilityObservation(
                health_boundary_started_ns=BOUNDARY_NS,
                health_boundary_app_fingerprint="not-a-fingerprint",
                collector_heartbeat_epochs={},
                observation_elapsed_ns=40_000_000_000,
                sample_count=21,
            )
        )


def test_cli_requires_explicit_production_observation_contract() -> None:
    parser = observer._build_parser()
    args = parser.parse_args(
        [
            "--profile",
            "spot",
            "--runtime-readiness-generation",
            READINESS_GENERATION,
            "--deployed-commit",
            DEPLOYED_COMMIT,
            "--nats-target-manifest-sha256",
            TARGET_MANIFEST_SHA256,
            "--gateway-scheme",
            "http",
            "--gateway-port",
            "8000",
            "--stability-seconds",
            "40",
            *(
                item
                for name in observer.evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
                for item in ("--required-container", name)
            ),
        ]
    )

    assert args.gateway_scheme == "http"
    assert args.stability_seconds == 40.0


def test_cli_rejects_non_production_stability_window_before_probing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        observer,
        "observe_deployment_stability",
        lambda **_kwargs: pytest.fail("observer must not run for a non-40s window"),
    )

    result = observer.main(
        [
            "--profile",
            "spot",
            "--runtime-readiness-generation",
            READINESS_GENERATION,
            "--deployed-commit",
            DEPLOYED_COMMIT,
            "--nats-target-manifest-sha256",
            TARGET_MANIFEST_SHA256,
            "--gateway-scheme",
            "http",
            "--gateway-port",
            "8000",
            "--stability-seconds",
            "39",
            *(
                item
                for name in observer.evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
                for item in ("--required-container", name)
            ),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "deployment_stability_observation_failed\n"
