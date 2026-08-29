import base64
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import docker_event_monitor as monitor


START_NS = 1_725_000_000_123_456_789
ALLOWLIST = ("aats-gateway", "aats-nats")
DAEMON_ID = "AATS:LOCAL:DAEMON:01"
OTHER_DAEMON_ID = "AATS:LOCAL:DAEMON:02"


def test_daemon_binding_transport_envelope_is_ascii_and_integrity_bound() -> None:
    envelope = monitor.daemon_binding_transport_envelope(DAEMON_ID)
    digest, encoded = envelope.split("\t")
    raw = base64.b64decode(encoded, validate=True)

    assert raw == DAEMON_ID.encode("ascii")
    assert digest == f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _packet(
    *,
    start_ns: int = START_NS,
    cutoff_ns: int = START_NS + 123_456_789,
    end_ns: int = START_NS + 1_000_000_123,
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "format_version": "aats.live_docker_event_window.v1",
        "source": "docker_engine_live_stream",
        "complete": False,
        "coverage_status": "BOUNDED_OBSERVED",
        "trust_boundary": {
            "server_filter": (
                "type=container AND com.docker.compose.project=aats-dev"
            ),
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
        "allowlist": list(ALLOWLIST),
        "docker_daemon_id": DAEMON_ID,
        "coverage_started_ns": start_ns,
        "requested_cutoff_ns": cutoff_ns,
        "coverage_ended_ns": end_ns,
        "segments": [
            {
                "segment_id": 1,
                "requested_at_ns": start_ns - 1,
                "until_ns": end_ns,
                "ready_at_ns": start_ns,
                "completed_at_ns": end_ns + 1,
                "event_count": len(events or []),
                "clean_eof": True,
            }
        ],
        "pre_coverage_history_events": [],
        "events": events or [],
        "fatal_errors": [],
    }


def test_validate_live_packet_preserves_non_microsecond_boundaries() -> None:
    packet = _packet()

    assert monitor.validate_live_window_evidence(
        packet,
        expected_allowlist=ALLOWLIST,
        expected_start_ns=START_NS,
        expected_cutoff_ns=START_NS + 123_456_789,
    ) is packet


def test_validate_live_packet_rejects_daemon_identity_mismatch() -> None:
    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="live_docker_evidence_daemon_mismatch",
    ):
        monitor.validate_live_window_evidence(
            _packet(),
            expected_allowlist=ALLOWLIST,
            expected_daemon_id=OTHER_DAEMON_ID,
        )


def test_direct_info_reads_daemon_id_from_fixed_socket(monkeypatch) -> None:
    calls: list[object] = []

    class _Response:
        status = 200

        @staticmethod
        def read(_limit: int) -> bytes:
            return json.dumps({"ID": DAEMON_ID}).encode("utf-8")

    class _Connection:
        def __init__(self, socket_path: Path, *, timeout: float) -> None:
            calls.append((socket_path, timeout))

        def request(self, method: str, path: str, *, headers) -> None:
            calls.append((method, path, headers))

        @staticmethod
        def getresponse() -> _Response:
            return _Response()

        def close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr(monitor, "_UnixHTTPConnection", _Connection)

    assert monitor._read_direct_docker_daemon_id() == DAEMON_ID
    assert calls[0] == (Path("/var/run/docker.sock"), 10.0)
    assert calls[1] == (
        "GET",
        "/info",
        {"Accept": "application/json", "Host": "docker"},
    )
    assert calls[-1] == "closed"


def test_live_segment_request_always_bounds_daemon_history_with_since(
    monkeypatch,
) -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    class _Response:
        status = 200

        @staticmethod
        def readline(_limit: int) -> bytes:
            return b""

    class _Connection:
        def __init__(self, _socket_path: Path, *, timeout: float) -> None:
            assert timeout == 11.0

        def request(
            self,
            method: str,
            path: str,
            *,
            headers: dict[str, str],
        ) -> None:
            requests.append((method, path, headers))

        @staticmethod
        def getresponse() -> _Response:
            return _Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(monitor, "_UnixHTTPConnection", _Connection)
    monkeypatch.setattr(monitor.time, "time_ns", lambda: START_NS)
    monkeypatch.setattr(monitor.time, "monotonic_ns", lambda: START_NS)
    ready: list[tuple[int, int]] = []

    monitor._read_live_segment(
        START_NS + 1_000_000_000,
        lambda wall, monotonic: ready.append((wall, monotonic)),
        lambda _event: None,
    )

    assert ready == [(START_NS, START_NS)]
    assert len(requests) == 1
    method, path, headers = requests[0]
    assert method == "GET"
    assert "since=1725000000.123456789" in path
    assert "until=1725000001.123456789" in path
    assert "filters=" in path
    assert headers == {"Accept": "application/json", "Host": "docker"}


def test_packet_retains_pre_ready_history_outside_authoritative_window() -> None:
    subject = monitor.LiveDockerEventMonitor(
        ALLOWLIST,
        max_runtime_seconds=5.0,
        runtime_loader=lambda _names: {},
        daemon_identity_loader=lambda: DAEMON_ID,
    )
    segment = monitor._Segment(
        segment_id=1,
        requested_at_ns=START_NS - 10,
        requested_at_mono_ns=START_NS - 10,
        until_ns=START_NS + 1_000,
        ready_at_ns=START_NS,
        ready_at_mono_ns=START_NS,
        completed_at_ns=START_NS + 1_001,
        completed_at_mono_ns=START_NS + 1_001,
        event_count=2,
    )
    segment.done.set()
    subject._started_ns = START_NS
    subject._started_mono_ns = START_NS
    subject._docker_daemon_id = DAEMON_ID
    subject._segments = [segment]
    before_ready = {
        "container_id": "1" * 64,
        "name": "aats-gateway",
        "action": "start",
        "time_nano": START_NS - 1,
    }
    after_ready = {
        "container_id": "1" * 64,
        "name": "aats-gateway",
        "action": "health_status: healthy",
        "time_nano": START_NS + 1,
    }
    subject._record_event(before_ready)
    subject._record_event(after_ready)

    packet = subject._build_packet(START_NS + 500)

    assert packet["pre_coverage_history_events"] == [before_ready]
    assert packet["events"] == [after_ready]
    assert monitor.validate_live_window_evidence(
        packet,
        expected_allowlist=ALLOWLIST,
        expected_start_ns=START_NS,
        expected_cutoff_ns=START_NS + 500,
    ) is packet


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("DOCKER_HOST", "docker_host_environment_forbidden"),
        ("DOCKER_CONTEXT", "docker_context_environment_forbidden"),
    ],
)
def test_daemon_binding_rejects_docker_cli_routing_environment(
    name: str,
    error: str,
) -> None:
    with pytest.raises(monitor.DockerEventMonitorError, match=error):
        monitor.validate_local_docker_daemon_binding(environ={name: "override"})


def test_daemon_binding_requires_default_context(monkeypatch) -> None:
    monkeypatch.setattr(monitor, "_run_docker_cli", lambda *_args: "remote\n")

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="docker_default_context_required",
    ):
        monitor.validate_local_docker_daemon_binding(environ={})


def test_daemon_binding_rejects_cli_direct_id_mismatch(monkeypatch) -> None:
    def _cli(*arguments: str) -> str:
        if arguments == ("context", "show"):
            return "default\n"
        assert arguments == ("info", "--format", "{{json .ID}}")
        return json.dumps(OTHER_DAEMON_ID)

    monkeypatch.setattr(monitor, "_run_docker_cli", _cli)
    monkeypatch.setattr(
        monitor,
        "_read_direct_docker_daemon_id",
        lambda: DAEMON_ID,
    )

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="docker_cli_direct_daemon_id_mismatch",
    ):
        monitor.validate_local_docker_daemon_binding(environ={})


def test_daemon_binding_returns_exact_matching_local_id(monkeypatch) -> None:
    def _cli(*arguments: str) -> str:
        if arguments == ("context", "show"):
            return "default\n"
        assert arguments == ("info", "--format", "{{json .ID}}")
        return f"{json.dumps(DAEMON_ID)}\n"

    monkeypatch.setattr(monitor, "_run_docker_cli", _cli)
    monkeypatch.setattr(
        monitor,
        "_read_direct_docker_daemon_id",
        lambda: DAEMON_ID,
    )

    assert monitor.validate_local_docker_daemon_binding(environ={}) == DAEMON_ID
    assert (
        monitor.validate_local_docker_daemon_binding(
            environ={"DOCKER_HOST": "unix:///var/run/docker.sock"}
        )
        == DAEMON_ID
    )


def test_daemon_binding_cli_subcommand_outputs_only_validated_id(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        monitor,
        "validate_local_docker_daemon_binding",
        lambda: DAEMON_ID,
    )

    assert monitor.main(["daemon-binding"]) == 0
    assert capsys.readouterr().out == f"{DAEMON_ID}\n"


def test_validate_live_packet_rejects_segment_gap() -> None:
    packet = _packet(end_ns=START_NS + 3_000_000_000)
    packet["segments"] = [
        {
            "segment_id": 1,
            "requested_at_ns": START_NS - 1,
            "until_ns": START_NS + 1_000_000_000,
            "ready_at_ns": START_NS,
            "completed_at_ns": START_NS + 1_000_000_001,
            "event_count": 0,
            "clean_eof": True,
        },
        {
            "segment_id": 2,
            "requested_at_ns": START_NS + 1_000_000_001,
            "until_ns": START_NS + 3_000_000_000,
            "ready_at_ns": START_NS + 1_000_000_001,
            "completed_at_ns": START_NS + 3_000_000_001,
            "event_count": 0,
            "clean_eof": True,
        },
    ]

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="live_docker_evidence_segment_gap",
    ):
        monitor.validate_live_window_evidence(
            packet,
            expected_allowlist=ALLOWLIST,
        )


def test_validate_live_packet_rejects_early_eof_shape() -> None:
    packet = _packet()
    packet["segments"][0]["completed_at_ns"] = START_NS + 999_999_999

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="invalid_live_docker_evidence_segment",
    ):
        monitor.validate_live_window_evidence(
            packet,
            expected_allowlist=ALLOWLIST,
        )


def test_monitor_rejects_early_live_segment_eof() -> None:
    def _early_reader(_until_ns, on_ready, _on_event) -> None:
        on_ready(time.time_ns(), time.monotonic_ns())

    subject = monitor.LiveDockerEventMonitor(
        ALLOWLIST,
        max_runtime_seconds=5.0,
        segment_seconds=1.0,
        overlap_seconds=0.25,
        segment_reader=_early_reader,
        runtime_loader=lambda _names: {},
        daemon_identity_loader=lambda: DAEMON_ID,
    )
    try:
        with pytest.raises(
            monitor.DockerEventMonitorError,
            match="live_docker_segment_early_eof",
        ):
            subject.start()
    finally:
        subject.close()


def test_monitor_rejects_wall_monotonic_clock_drift() -> None:
    def _drift_reader(until_ns, on_ready, _on_event) -> None:
        on_ready(time.time_ns(), time.monotonic_ns() - 2_000_000_000)
        while time.time_ns() < until_ns:
            time.sleep(0.01)

    subject = monitor.LiveDockerEventMonitor(
        ALLOWLIST,
        max_runtime_seconds=5.0,
        segment_seconds=1.0,
        overlap_seconds=0.25,
        segment_reader=_drift_reader,
        runtime_loader=lambda _names: {},
        daemon_identity_loader=lambda: DAEMON_ID,
    )
    try:
        subject.start()
        with pytest.raises(
            monitor.DockerEventMonitorError,
            match="live_docker_segment_clock_drift",
        ):
            subject.seal(time.time_ns())
    finally:
        subject.close()


def test_monitor_rejects_daemon_identity_drift_before_seal() -> None:
    identities = iter((DAEMON_ID, DAEMON_ID, OTHER_DAEMON_ID))

    def _reader(until_ns, on_ready, _on_event) -> None:
        on_ready(time.time_ns(), time.monotonic_ns())
        while time.time_ns() < until_ns:
            time.sleep(0.01)

    subject = monitor.LiveDockerEventMonitor(
        ALLOWLIST,
        max_runtime_seconds=5.0,
        segment_seconds=1.0,
        overlap_seconds=0.25,
        segment_reader=_reader,
        runtime_loader=lambda _names: {},
        daemon_identity_loader=lambda: next(identities),
    )
    try:
        subject.start()
        with pytest.raises(
            monitor.DockerEventMonitorError,
            match="live_docker_daemon_identity_drift",
        ):
            subject.seal(time.time_ns())
    finally:
        subject.close()


def test_monitor_ignores_exec_but_fails_closed_on_project_rename() -> None:
    subject = monitor.LiveDockerEventMonitor(
        ALLOWLIST,
        max_runtime_seconds=5.0,
        runtime_loader=lambda _names: {},
        daemon_identity_loader=lambda: DAEMON_ID,
    )
    subject._record_event(
        {
            "container_id": "1" * 64,
            "name": "aats-gateway",
            "action": "exec_start",
            "time_nano": START_NS,
            "exec_id": "2" * 64,
        }
    )
    assert subject._events == []
    subject._record_event(
        {
            "container_id": "3" * 64,
            "name": "renamed-target",
            "action": "rename",
            "time_nano": START_NS + 1,
        }
    )
    assert subject._fatal_error == "project_container_rename_observed"


def test_runtime_loader_rejects_target_without_exact_project_label(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=f"{'1' * 64}|aats-gateway|another-project\n"
        ),
    )

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="docker_runtime_project_label_mismatch",
    ):
        monitor._load_target_runtime(("aats-gateway",))


def test_external_seal_surfaces_daemon_failure_without_waiting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_dir = tmp_path / "monitor"
    control_dir.mkdir()
    token = "monitor-token"
    metadata = {
        "deployment_lock_id": "lock-1",
        "runtime_readiness_generation": "generation-1",
        "deployed_commit": "a" * 40,
    }
    (control_dir / "failed.json").write_text(
        json.dumps(
            {
                "format_version": "aats.live_docker_event_monitor_failed.v1",
                "token": token,
                "error": "simulated_reader_crash",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "_validate_control_dir", lambda path: path)
    monkeypatch.setattr(
        monitor,
        "_atomic_json",
        lambda path, payload, **_kwargs: path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        ),
    )
    monkeypatch.setattr(
        monitor,
        "load_external_monitor_ready",
        lambda *_args, **_kwargs: {
            "coverage_started_ns": START_NS,
            "pid": os.getpid(),
        },
    )

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="external_live_monitor_failed:simulated_reader_crash",
    ):
        monitor.seal_external_monitor(
            control_dir,
            token=token,
            cutoff_ns=START_NS + 1,
            expected_allowlist=ALLOWLIST,
            expected_metadata=metadata,
            timeout_seconds=0.1,
        )


def test_external_ready_rejects_tampered_daemon_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_dir = tmp_path / "monitor"
    control_dir.mkdir()
    token = "monitor-token"
    metadata = {
        "deployment_lock_id": "lock-1",
        "runtime_readiness_generation": "generation-1",
        "deployed_commit": "a" * 40,
    }
    (control_dir / "ready.json").write_text(
        json.dumps(
            {
                "format_version": "aats.live_docker_event_monitor_ready.v1",
                "token": token,
                "pid": os.getpid(),
                "allowlist": list(ALLOWLIST),
                "coverage_started_ns": START_NS,
                "docker_daemon_id": OTHER_DAEMON_ID,
                "metadata": metadata,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "_validate_control_dir", lambda path: path)
    monkeypatch.setattr(
        monitor,
        "validate_local_docker_daemon_binding",
        lambda: DAEMON_ID,
    )

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="live_monitor_ready_daemon_mismatch",
    ):
        monitor.load_external_monitor_ready(
            control_dir,
            token=token,
            expected_allowlist=ALLOWLIST,
            expected_metadata=metadata,
        )


def test_external_seal_rejects_tampered_daemon_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_dir = tmp_path / "monitor"
    control_dir.mkdir()
    token = "monitor-token"
    metadata = {
        "deployment_lock_id": "lock-1",
        "runtime_readiness_generation": "generation-1",
        "deployed_commit": "a" * 40,
    }
    (control_dir / "sealed.json").write_text(
        json.dumps(
            {
                "format_version": "aats.live_docker_event_monitor_sealed.v1",
                "token": token,
                "pid": os.getpid(),
                "docker_daemon_id": OTHER_DAEMON_ID,
                "metadata": metadata,
                "evidence": _packet(cutoff_ns=START_NS + 1),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "_validate_control_dir", lambda path: path)
    monkeypatch.setattr(
        monitor,
        "_atomic_json",
        lambda path, payload, **_kwargs: path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        ),
    )
    monkeypatch.setattr(
        monitor,
        "load_external_monitor_ready",
        lambda *_args, **_kwargs: {
            "coverage_started_ns": START_NS,
            "pid": os.getpid(),
            "docker_daemon_id": DAEMON_ID,
        },
    )

    with pytest.raises(
        monitor.DockerEventMonitorError,
        match="invalid_live_monitor_sealed_packet",
    ):
        monitor.seal_external_monitor(
            control_dir,
            token=token,
            cutoff_ns=START_NS + 1,
            expected_allowlist=ALLOWLIST,
            expected_metadata=metadata,
            timeout_seconds=0.1,
        )
