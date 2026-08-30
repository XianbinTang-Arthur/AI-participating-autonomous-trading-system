from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from scripts import check_nats_durable_cutover as cutover
from scripts import nats_runtime_identity


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
EVIDENCE_SCRIPT = REPO_ROOT / "scripts" / "write_deployment_evidence.py"
GIT_BASH = Path(r"D:\Git\Git\bin\bash.exe")
READINESS_GENERATION = "aaaaaaaaaaaa-20260824T000000Z-123-456"
DEPLOYED_COMMIT = "a" * 40
DEPLOYMENT_LOCK_ID = "test-deployment-lock"
NATS_TARGET_MANIFEST_SHA256 = str(
    cutover._default_target_stream_manifest()["sha256"]
)
NATS_POST_IDENTITY_FACTS = "\n".join(
    (
        json.dumps("f" * 64),
        json.dumps("sha256:" + "b" * 64),
        json.dumps(nats_runtime_identity.NATS_EXPECTED_IMAGE),
        json.dumps("running"),
        json.dumps("healthy"),
        "0",
        json.dumps("2026-08-24T00:01:30Z"),
        "0",
        json.dumps("aats-dev"),
        json.dumps("nats"),
        json.dumps("volume"),
        json.dumps("aats-dev_nats_data"),
        "true",
    )
)
NATS_POST_FINGERPRINT = nats_runtime_identity.parse_nats_container_identity(
    NATS_POST_IDENTITY_FACTS
).fingerprint
NATS_HEALTH_FACTS = json.dumps(
    {
        "Id": "f" * 64,
        "RestartCount": 0,
        "State": {
            "Status": "running",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Checks": [
                    {
                        "Start": "2026-08-24T00:01:20Z",
                        "End": "2026-08-24T00:01:21Z",
                        "ExitCode": 0,
                    }
                ],
            },
        },
    },
    separators=(",", ":"),
)
NATS_VOLUME_FACTS = "\n".join(
    (
        json.dumps("aats-dev_nats_data"),
        json.dumps("local"),
        json.dumps("local"),
        json.dumps("2026-08-23T00:00:00Z"),
        json.dumps(None),
        json.dumps("aats-dev"),
        json.dumps("nats_data"),
        json.dumps(None),
    )
)
NATS_VOLUME_FINGERPRINT = nats_runtime_identity.parse_nats_volume_identity(
    NATS_VOLUME_FACTS
).fingerprint
NATS_BOOTSTRAP = {
    "mode": "existing_container_preserved",
    "baseline_fingerprint": "sha256:" + "b" * 64,
    "volume_fingerprint": NATS_VOLUME_FINGERPRINT,
}
HEALTH_BOUNDARY_NS = int(
    datetime(2026, 8, 24, 0, 1, 2, tzinfo=UTC).timestamp()
    * 1_000_000_000
)


def _matched_nats_stream_rows() -> cutover.QueryResult:
    expected = tuple(cutover.build_expected_durable_index().values())
    consumer_counts: dict[str, int] = {}
    consumers: list[cutover.ConsumerState] = []
    for durable in expected:
        consumer_counts[durable.stream] = consumer_counts.get(durable.stream, 0) + 1
        consumers.append(
            cutover.ConsumerState(
                stream=durable.stream,
                durable=durable.durable,
                created="2026-08-24T00:00:00Z",
                deliver_policy=durable.deliver_policy,
                ack_policy=durable.ack_policy,
                filter_subject=durable.filter_subject,
                filter_subjects=(),
                deliver_group=None,
                max_ack_pending=1,
                num_ack_pending=0,
                cursor=cutover.ConsumerCursor(0, 0, 0, 0),
                ack_wait_seconds=durable.ack_wait_seconds,
                max_deliver=durable.max_deliver,
            )
        )
    streams: list[cutover.CriticalStreamState] = []
    for row in cutover._default_target_stream_manifest()["streams"]:
        identity = row["identity"]
        config = row["immutable_config"]
        name = identity["name"]
        streams.append(
            cutover.CriticalStreamState(
                name=name,
                created="2026-08-24T00:00:00Z",
                subjects=tuple(config["subjects"]),
                retention=config["retention"],
                storage=config["storage"],
                discard=config["discard"],
                max_age_seconds=config["max_age_seconds"],
                max_bytes=config["max_bytes"],
                max_msgs=config["max_msgs"],
                max_msg_size=config["max_msg_size"],
                num_replicas=config["num_replicas"],
                duplicate_window_seconds=config["duplicate_window_seconds"],
                deny_purge=config["deny_purge"],
                messages=0,
                bytes=0,
                first_seq=0,
                last_seq=0,
                consumer_count=consumer_counts.get(name, 0),
                deleted=(),
                num_deleted=0,
            )
        )
    return cutover.QueryResult(
        stream_count=len(streams),
        consumer_count=len(consumers),
        consumers=tuple(consumers),
        streams=tuple(streams),
    )


def _qualified_preflight_snapshot() -> tuple[
    cutover.QueryResult,
    list[dict[str, object]],
    list[dict[str, object]],
]:
    query = _matched_nats_stream_rows()
    durables, blocked = cutover.evaluate_existing_consumers(
        query.consumers,
        cutover.build_expected_durable_index(),
    )
    assert blocked is False
    return query, durables, cutover.build_critical_stream_rows(query.streams)


def test_deployment_evidence_supports_wsl_system_python_310() -> None:
    source = EVIDENCE_SCRIPT.read_text(encoding="utf-8")

    assert "from datetime import UTC" not in source
    assert "timezone.utc" in source


def _load_evidence_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("write_deployment_evidence", EVIDENCE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_final_durable_projection_hash_binds_identity_and_configuration() -> None:
    module = _load_evidence_module()
    _, durables, _ = _qualified_preflight_snapshot()

    projection, fingerprint = module._durable_qualification_projection(durables)
    identity_changed = copy.deepcopy(projection)
    identity_changed[0]["identity"]["durable"] += "-forged"
    _, identity_fingerprint = module._durable_qualification_projection(
        identity_changed
    )
    config_changed = copy.deepcopy(projection)
    config_changed[0]["immutable_config"]["actual"]["ack_policy"] = "none"
    _, config_fingerprint = module._durable_qualification_projection(config_changed)

    assert fingerprint.startswith("sha256:")
    assert identity_fingerprint != fingerprint
    assert config_fingerprint != fingerprint


def _passed_app_quiescence(
    checked_at: datetime | None = None,
) -> dict[str, object]:
    snapshot = tuple(
        {
            "name": name,
            "existence": "not_found",
            "container_id": None,
            "status": None,
            "started_at": None,
            "finished_at": None,
            "restart_count": None,
        }
        for name in cutover._KNOWN_APP_CONTAINERS
    )
    start_ns = int(
        (checked_at or datetime(2026, 8, 24, tzinfo=UTC)).timestamp()
        * 1_000_000_000
    )
    return cutover.build_app_quiescence_evidence(
        since_ns=start_ns,
        until_ns=start_ns + 1_000_000_000,
        before=snapshot,
        after=snapshot,
        events=(),
        event_capture=_live_event_capture(
            start_ns,
            start_ns + 1_000_000_000,
        ),
    )


def _live_event_capture(
    start_ns: int,
    cutoff_ns: int,
    *,
    events=(),
) -> dict[str, object]:
    rows = list(events)
    coverage_end_ns = cutoff_ns + 100
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
        "allowlist": [*cutover._KNOWN_APP_CONTAINERS, cutover._NATS_CONTAINER],
        "docker_daemon_id": "AATS:LOCAL:DAEMON:TEST",
        "coverage_started_ns": start_ns,
        "requested_cutoff_ns": cutoff_ns,
        "coverage_ended_ns": coverage_end_ns,
        "segments": [
            {
                "segment_id": 1,
                "requested_at_ns": start_ns - 1,
                "until_ns": coverage_end_ns,
                "ready_at_ns": start_ns,
                "completed_at_ns": coverage_end_ns + 1,
                "event_count": len(rows),
                "clean_eof": True,
            }
        ],
        "pre_coverage_history_events": [],
        "events": rows,
        "fatal_errors": [],
    }


def _write_preflight_pair(tmp_path: Path, module: ModuleType) -> tuple[Path, Path]:
    evidence_dir = tmp_path / "artifacts" / "deployments"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    before_path = evidence_dir / "before.json"
    after_path = evidence_dir / "after.json"
    query, durables, streams = _qualified_preflight_snapshot()
    before_payload = cutover.build_evidence(
        generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 24, tzinfo=UTC),
        query_result=query,
        rows=durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(
            datetime(2026, 8, 24, tzinfo=UTC)
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BOOTSTRAP["baseline_fingerprint"],
        critical_streams=streams,
        continuity={
            "status": "BASELINE_CAPTURED",
            "complete": True,
            "baseline_sha256": None,
            "streams_checked": len(streams),
            "durables_checked": len(durables),
            "passive_retention_trims": [],
            "violations": [],
        },
    )
    before_path.write_text(
        json.dumps(before_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    before_reference, _ = module._nats_cutover_preflight_reference(
        repo_root=tmp_path,
        path=before_path,
        runtime_readiness_generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        expected_stage="pre_full_down",
    )
    after_payload = cutover.build_evidence(
        generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 24, 0, 1, tzinfo=UTC),
        query_result=query,
        rows=durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(
            datetime(2026, 8, 24, 0, 1, tzinfo=UTC)
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_POST_FINGERPRINT,
        stage="post_infra_pre_app_up",
        critical_streams=streams,
        previous_preflight=before_reference,
        continuity=cutover.evaluate_cutover_continuity(
            previous_streams=streams,
            current_streams=streams,
            previous_durables=durables,
            current_durables=durables,
            baseline_sha256=before_reference["sha256"],
        ),
    )
    after_path.write_text(
        json.dumps(after_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return before_path, after_path


def _load_preflight_chain(
    tmp_path: Path,
    module: ModuleType,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    before_reference, before_payload = module._nats_cutover_preflight_reference(
        repo_root=tmp_path,
        path=before_path,
        runtime_readiness_generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        expected_stage="pre_full_down",
    )
    _, after_payload = module._nats_cutover_preflight_reference(
        repo_root=tmp_path,
        path=after_path,
        runtime_readiness_generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        expected_stage="post_infra_pre_app_up",
    )
    return before_reference, before_payload, after_payload


@pytest.mark.parametrize(
    "mutation",
    (
        "query_count",
        "role",
        "semantics",
        "actual_config",
        "expected_config",
        "self_reported_status",
    ),
)
def test_preflight_reference_recomputes_row_and_count_projection(
    tmp_path: Path,
    mutation: str,
) -> None:
    module = _load_evidence_module()
    before_path, _ = _write_preflight_pair(tmp_path, module)
    payload = json.loads(before_path.read_text(encoding="utf-8"))
    row = payload["durables"][0]
    if mutation == "query_count":
        payload["query"]["consumers_scanned"] += 1
    elif mutation == "role":
        row["identity"]["role"] = "market"
    elif mutation == "semantics":
        row["identity"]["delivery_semantics"] = "forged"
    elif mutation == "actual_config":
        row["immutable_config"]["actual"]["headers_only"] = True
    elif mutation == "expected_config":
        row["mutable_config"]["expected"]["max_deliver"] += 1
    else:
        row["status"] = "SAFE_ALREADY_ONE"
        row["window"]["current_max_ack_pending"] = 256
    before_path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="nats_cutover_preflight_durable_projection_invalid",
    ):
        module._nats_cutover_preflight_reference(
            repo_root=tmp_path,
            path=before_path,
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            expected_stage="pre_full_down",
        )


def test_preflight_chain_recomputes_continuity_and_rejects_forged_cursor_rollback(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    before_reference, before_payload, after_payload = _load_preflight_chain(
        tmp_path,
        module,
    )
    forged_before = copy.deepcopy(before_payload)
    forged_before["durables"][0]["cursor"]["delivered_stream_seq"] = 1

    with pytest.raises(
        ValueError,
        match="nats_cutover_continuity_recomputed_not_passed",
    ):
        module._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=forged_before,
            after_payload=after_payload,
        )


def test_preflight_chain_recomputes_continuity_and_rejects_forged_stream_purge(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    before_reference, before_payload, after_payload = _load_preflight_chain(
        tmp_path,
        module,
    )
    forged_before = copy.deepcopy(before_payload)
    forged_state = forged_before["critical_streams"][0]["state"]
    forged_state.update(
        {
            "messages": 1,
            "bytes": 16,
            "first_seq": 1,
            "last_seq": 1,
        }
    )

    with pytest.raises(
        ValueError,
        match="nats_cutover_continuity_recomputed_not_passed",
    ):
        module._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=forged_before,
            after_payload=after_payload,
        )


def test_preflight_chain_rejects_forged_continuity_summary(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    before_reference, before_payload, after_payload = _load_preflight_chain(
        tmp_path,
        module,
    )
    forged_after = copy.deepcopy(after_payload)
    forged_after["continuity"]["streams_checked"] += 1

    with pytest.raises(
        ValueError,
        match="nats_cutover_continuity_artifact_mismatch:streams_checked",
    ):
        module._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=before_payload,
            after_payload=forged_after,
        )


def test_preflight_chain_rejects_omitted_passive_retention_trim_summary(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    before_reference, before_payload, after_payload = _load_preflight_chain(
        tmp_path,
        module,
    )
    forged_after = copy.deepcopy(after_payload)
    del forged_after["continuity"]["passive_retention_trims"]

    with pytest.raises(
        ValueError,
        match=(
            "nats_cutover_continuity_artifact_mismatch:"
            "passive_retention_trims"
        ),
    ):
        module._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=before_payload,
            after_payload=forged_after,
        )


def _container_inspect_payload(
    names: tuple[str, ...],
    *,
    image_id: str,
    profile: str,
    unhealthy_name: str | None = None,
) -> str:
    payload = []
    for index, name in enumerate(names, start=1):
        payload.append(
            {
                "Id": f"{index:064x}",
                "Name": f"/{name}",
                "Image": image_id,
                "RestartCount": 0,
                "State": {
                    "Status": "running",
                    "StartedAt": "2026-08-24T00:00:00Z",
                    "Health": {
                        "Status": "unhealthy" if name == unhealthy_name else "healthy",
                        "FailingStreak": 1 if name == unhealthy_name else 0,
                        "Checks": [
                            {
                                "Start": "2026-08-24T00:01:03.000000001Z",
                                "End": "2026-08-24T00:01:03.000000002Z",
                                "ExitCode": 1 if name == unhealthy_name else 0,
                            }
                        ],
                    },
                },
                "ComposeProject": "aats-dev",
                "ComposeService": name,
                "NatsTargetManifestSha256": NATS_TARGET_MANIFEST_SHA256,
                "SafeEnvironment": {
                    "AATS_RUNTIME_READINESS_GENERATION": READINESS_GENERATION,
                    "AATS_DEPLOYED_GIT_COMMIT": DEPLOYED_COMMIT,
                    "AATS_PROFILE": profile,
                },
                "Ports": (
                    {
                        "8000/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "8001"}
                        ]
                    }
                    if name == "aats-gateway"
                    else {}
                    )
            }
        )
    return "\n".join(json.dumps(row, sort_keys=True) for row in payload)


def _evidence_runner(
    module: ModuleType,
    *,
    profile: str,
    image_id: str,
    heartbeat_epoch: int | None = None,
    unhealthy_name: str | None = None,
    nats_health_facts: str = NATS_HEALTH_FACTS,
):
    names = module._REQUIRED_CONTAINERS_BY_PROFILE[profile]
    inspect_payload = _container_inspect_payload(
        names,
        image_id=image_id,
        profile=profile,
        unhealthy_name=unhealthy_name,
    )

    def _run(args: tuple[str, ...], _cwd: Path | None = None) -> str:
        if args == (
            "docker",
            "image",
            "inspect",
            nats_runtime_identity.NATS_EXPECTED_IMAGE,
            "--format",
            "{{.Id}}",
        ):
            return "sha256:" + "b" * 64
        if args == (
            "docker",
            "inspect",
            "--format",
            module._NATS_IDENTITY_INSPECT_TEMPLATE,
            "aats-nats",
        ):
            return NATS_POST_IDENTITY_FACTS
        if args == (
            "docker",
            "inspect",
            "--format",
            module._NATS_HEALTH_INSPECT_TEMPLATE,
            "aats-nats",
        ):
            return nats_health_facts
        if args == (
            "docker",
            "volume",
            "inspect",
            "--format",
            module._NATS_VOLUME_INSPECT_TEMPLATE,
            "aats-dev_nats_data",
        ):
            return NATS_VOLUME_FACTS
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return DEPLOYED_COMMIT
        if args[:4] == ("docker", "image", "inspect", "aats-base:dev"):
            return image_id
        if args[:2] == ("docker", "inspect"):
            return inspect_payload
        if args[:2] == ("docker", "events"):
            return ""
        raise AssertionError(args)

    boundary_payload = (
        _container_inspect_payload(
            names,
            image_id=image_id,
            profile=profile,
        )
        if unhealthy_name is not None
        else inspect_payload
    )
    facts, bindings = module._container_snapshot(
        names,
        expected_image_id=image_id,
        expected_generation=READINESS_GENERATION,
        expected_commit=DEPLOYED_COMMIT,
        expected_profile=profile,
        expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
        run=lambda _args, _cwd=None: boundary_payload,
    )
    boundary_fingerprint = module._app_runtime_snapshot_fingerprint(
        facts,
        bindings,
    )

    heartbeat_epochs = (
        {
            name: heartbeat_epoch
            for name in names
            if name in module._COLLECTOR_HEARTBEATS
        }
        if heartbeat_epoch is not None
        else {}
    )

    return names, _run, boundary_fingerprint, heartbeat_epochs


def _writer_monitor_kwargs(names: tuple[str, ...]) -> dict[str, object]:
    app_up_authorized_ns = HEALTH_BOUNDARY_NS - 800_000_000

    def _sealer(
        _control_dir: Path,
        *,
        cutoff_ns: int,
        **_kwargs,
    ) -> dict[str, object]:
        events: list[dict[str, object]] = []
        for index, name in enumerate(names, start=1):
            event_ns = app_up_authorized_ns + index * 10_000_000
            events.extend(
                (
                    {
                        "name": name,
                        "container_id": f"{index:064x}",
                        "action": "create",
                        "time_nano": event_ns,
                    },
                    {
                        "name": name,
                        "container_id": f"{index:064x}",
                        "action": "start",
                        "time_nano": event_ns + 1,
                    },
                    {
                        "name": name,
                        "container_id": f"{index:064x}",
                        "action": "health_status: healthy",
                        "time_nano": event_ns + 2,
                    },
                )
            )
            if name in cutover._KNOWN_APP_CONTAINERS[-2:]:
                events.append(
                    {
                        "name": name,
                        "container_id": f"{index:064x}",
                        "action": "archive-path",
                        "time_nano": event_ns + 3,
                    }
                )
        return _live_event_capture(
            HEALTH_BOUNDARY_NS - 3_000_000_000,
            cutoff_ns,
            events=sorted(events, key=lambda row: int(row["time_nano"])),
        )

    return {
        "lifecycle_monitor_control_dir": Path(
            "/tmp/aats-docker-event-monitor-fs007-test"
        ),
        "lifecycle_monitor_token": "monitor-token",
        "app_up_authorized_ns": app_up_authorized_ns,
        "lifecycle_monitor_sealer": _sealer,
    }


def _run_deploy_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GIT_BASH), "scripts/deploy.sh", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=15,
    )


def test_deploy_requires_explicit_profile_before_any_wsl_call() -> None:
    result = _run_deploy_gate()

    assert result.returncode == 2
    assert "必须显式指定 --profile" in result.stderr
    assert "找不到 wsl 命令" not in result.stderr


@pytest.mark.parametrize(
    "profile",
    ["spot-live", "derivatives-live", "derivatives-live-monolith"],
)
def test_deploy_rejects_every_live_profile_and_yes_cannot_override(profile: str) -> None:
    result = _run_deploy_gate("--profile", profile, "--yes")

    assert result.returncode == 5
    assert "REAL-MONEY PRODUCTION: NO-GO" in result.stderr
    assert "找不到 wsl 命令" not in result.stderr


@pytest.mark.parametrize(
    ("projected_health", "expected_status"),
    (
        ("running healthy 0 0 0", 0),
        ("running healthy 1 0 1", 1),
        ("running healthy 0 0 1", 1),
        ("running unhealthy 3 1 1", 1),
    ),
)
def test_active_stability_probe_requires_latest_healthcheck_success(
    projected_health: str,
    expected_status: int,
) -> None:
    command = f"""
set -euo pipefail
source scripts/deploy.sh
APP_CONTAINERS='aats-gateway'
wsl_run() {{ printf '%s\\n' '{projected_health}'; }}
if all_required_app_containers_healthy; then
    actual=0
else
    actual=1
fi
test \"$actual\" -eq {expected_status}
"""
    result = subprocess.run(
        [str(GIT_BASH), "-c", command],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=15,
    )

    assert result.returncode == 0, result.stderr


def test_deploy_order_and_failure_posture_are_fail_closed() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert source.index("require_explicit_non_live_profile") < source.index("resolve_profile \"$PROFILE\"")
    assert source.index("step_build\n") < source.index("step_down\n")
    assert source.index("step_schema_migrate\n") < source.index("step_app_up\n")
    assert "应用 docker compose up 返回非零；继续" not in source
    assert "docker compose $COMPOSE_CMD_ARGS down --timeout 5\" ||" not in source
    assert "模拟栈基础检查通过（不是 trading-ready 或生产放行）" in source


def test_derivatives_simulation_and_future_live_topologies_require_public_collectors() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    required_line = (
        'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon '
        'aats-liquidations-daemon aats-microstructure-collector"'
    )
    assert source.count(required_line) == 2

    simulation_overlay = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.derivatives.yml"
    ).read_text(encoding="utf-8")
    for service in ("aats-liquidations-daemon:", "aats-microstructure-collector:"):
        assert service in simulation_overlay
    assert ".env.derivatives.live" not in simulation_overlay


def test_entrypoint_wrappers_default_to_simulation_and_reject_live() -> None:
    paths = [
        REPO_ROOT / "scripts" / "keepalive_wsl2_aats.ps1",
        REPO_ROOT / "scripts" / "prewarm_wsl2_aats.ps1",
        REPO_ROOT / "scripts" / "register_wsl2_aats_startup_task.ps1",
        REPO_ROOT / ".codex" / "skills" / "wsl2-deploy" / "scripts" / "run-deploy.ps1",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "[string]$Profile = 'derivatives'" in source
        assert "REAL-MONEY PRODUCTION is NO-GO" in source


def test_deploy_wrapper_rejects_windows_bash_launchers() -> None:
    source = (
        REPO_ROOT / ".codex" / "skills" / "wsl2-deploy" / "scripts" / "run-deploy.ps1"
    ).read_text(encoding="utf-8")

    for rejected in (
        r"*\WindowsApps\bash.exe",
        r"*\Windows\System32\bash.exe",
        r"*\Windows\Sysnative\bash.exe",
        r"*\Windows\SysWOW64\bash.exe",
    ):
        assert f"'{rejected}'" in source
    assert r"D:\Git\Git\bin\bash.exe" in source


def test_lifecycle_helpers_keep_legacy_stop_and_remove_paths_available() -> None:
    keepalive = (REPO_ROOT / "scripts" / "keepalive_wsl2_aats.ps1").read_text(encoding="utf-8")
    startup_task = (REPO_ROOT / "scripts" / "register_wsl2_aats_startup_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "$Action -eq 'Start'" in keepalive
    assert "Stop and Status remain available for legacy cleanup" in keepalive
    assert "-not $Remove" in startup_task


def test_evidence_packet_contains_only_simulation_identity_and_explicit_unknowns(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(module, profile="spot", image_id=image_id)
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    final_clock_values = iter(
        (HEALTH_BOUNDARY_NS + 40_000_000_000, HEALTH_BOUNDARY_NS + 41_000_000_000)
    )

    payload = module.build_evidence(
        repo_root=tmp_path,
        profile="spot",
        overlay="docker-compose.aats.spot.yml",
        schema_job_status="passed",
        runtime_readiness_generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        required_containers=names,
        nats_stream_probe=_matched_nats_stream_rows,
        **_writer_monitor_kwargs(names),
        health_boundary_started_ns=HEALTH_BOUNDARY_NS,
        health_boundary_app_fingerprint=health_boundary_fingerprint,
        collector_heartbeat_epochs=heartbeat_epochs,
        nats_cutover_preflight_before_path=before_path,
        nats_cutover_preflight_after_path=after_path,
        run=fake_run,
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        nanosecond_clock=lambda: next(final_clock_values),
    )

    assert payload["status"] == "simulation_stack_healthy_bounded_observation"
    assert payload["production_ready"] is False
    assert payload["trading_ready"] is False
    assert payload["deployed_commit"] == DEPLOYED_COMMIT
    assert payload["base_image_id"] == image_id
    assert payload["runtime_readiness_generation"] == READINESS_GENERATION
    assert payload["schema_contract"] == {
        "job_status": "passed",
        "clone_manifest_verified": False,
        "consistent_rollback_verified": False,
    }
    assert len(payload["required_containers"]) == 5
    assert payload["gateway_published_bindings"] == [
        {"container_port": "8000/tcp", "host_ip": "127.0.0.1", "host_port": "8001"}
    ]
    assert payload["collector_freshness"] == []
    durable_qualification = payload["nats_durable_qualification"]
    assert durable_qualification["expected_count"] == len(
        cutover.build_expected_durable_index()
    )
    assert durable_qualification["observed_count"] == len(
        cutover.build_expected_durable_index()
    )
    assert durable_qualification["first_snapshot"] == durable_qualification[
        "second_snapshot"
    ]
    for snapshot in (
        durable_qualification["first_snapshot"],
        durable_qualification["second_snapshot"],
    ):
        canonical = json.dumps(
            snapshot["canonical_projection"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert snapshot["canonical_projection_sha256"] == (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
    encoded = json.dumps(payload).lower()
    for forbidden in ("password", "api_key", "token", "database_url", "dsn"):
        assert forbidden not in encoded


def test_final_evidence_rejects_recovered_nats_health_failure_after_boundary(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    recovered_health = json.dumps(
        {
            "Id": "f" * 64,
            "RestartCount": 0,
            "State": {
                "Status": "running",
                "Health": {
                    "Status": "healthy",
                    "FailingStreak": 0,
                    "Checks": [
                        {
                            "Start": "2026-08-24T00:01:10Z",
                            "End": "2026-08-24T00:01:11Z",
                            "ExitCode": 1,
                        },
                        {
                            "Start": "2026-08-24T00:01:20Z",
                            "End": "2026-08-24T00:01:21Z",
                            "ExitCode": 0,
                        },
                    ],
                },
            },
        },
        separators=(",", ":"),
    )
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(
            module,
            profile="spot",
            image_id=image_id,
            nats_health_facts=recovered_health,
        )
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)

    with pytest.raises(
        RuntimeError,
        match="nats_runtime_health_failed_after_boundary",
    ):
        module.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=fake_run,
            generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        )


def test_final_evidence_requires_nats_success_after_health_boundary(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    pre_boundary_health = json.dumps(
        {
            "Id": "f" * 64,
            "RestartCount": 0,
            "State": {
                "Status": "running",
                "Health": {
                    "Status": "healthy",
                    "FailingStreak": 0,
                    "Checks": [
                        {
                            "Start": "2026-08-24T00:00:50Z",
                            "End": "2026-08-24T00:00:51Z",
                            "ExitCode": 0,
                        }
                    ],
                },
            },
        },
        separators=(",", ":"),
    )
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(
            module,
            profile="spot",
            image_id=image_id,
            nats_health_facts=pre_boundary_health,
        )
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)

    with pytest.raises(
        RuntimeError,
        match="nats_runtime_health_not_observed_after_boundary",
    ):
        module.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=fake_run,
            generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        )


def test_final_evidence_requires_live_stream_target_to_be_fully_provisioned(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(module, profile="spot", image_id=image_id)
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)

    def _stream_target_drift_probe() -> cutover.QueryResult:
        query = _matched_nats_stream_rows()
        return replace(
            query,
            streams=(
                replace(query.streams[0], max_bytes=query.streams[0].max_bytes + 1),
                *query.streams[1:],
            ),
        )

    with pytest.raises(RuntimeError, match="final_nats_stream_target_not_matched"):
        module.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_stream_target_drift_probe,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=fake_run,
            generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        )


def test_final_evidence_rechecks_stream_target_at_end_of_capture_window(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(module, profile="spot", image_id=image_id)
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    probe_calls = 0

    def _drifting_probe() -> cutover.QueryResult:
        nonlocal probe_calls
        probe_calls += 1
        query = _matched_nats_stream_rows()
        if probe_calls == 2:
            query = replace(
                query,
                streams=(replace(query.streams[0], max_bytes=1), *query.streams[1:]),
            )
        return query

    with pytest.raises(RuntimeError, match="final_nats_stream_target_not_matched"):
        module.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_drifting_probe,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=fake_run,
            generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        )

    assert probe_calls == 2


def test_evidence_packet_requires_fresh_public_collector_heartbeats(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    generated_at = datetime(2026, 8, 24, 0, 1, 3, tzinfo=UTC)
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = _evidence_runner(
        module,
        profile="derivatives",
        image_id=image_id,
        heartbeat_epoch=int(generated_at.timestamp()) - 10,
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    final_clock_values = iter(
        (HEALTH_BOUNDARY_NS + 40_000_000_000, HEALTH_BOUNDARY_NS + 41_000_000_000)
    )

    payload = module.build_evidence(
        repo_root=tmp_path,
        profile="derivatives",
        overlay="docker-compose.aats.derivatives.yml",
        schema_job_status="passed",
        runtime_readiness_generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        required_containers=names,
        nats_stream_probe=_matched_nats_stream_rows,
        **_writer_monitor_kwargs(names),
        health_boundary_started_ns=HEALTH_BOUNDARY_NS,
        health_boundary_app_fingerprint=health_boundary_fingerprint,
        collector_heartbeat_epochs=heartbeat_epochs,
        nats_cutover_preflight_before_path=before_path,
        nats_cutover_preflight_after_path=after_path,
        run=fake_run,
        generated_at=generated_at,
        nanosecond_clock=lambda: next(final_clock_values),
    )

    assert payload["collector_freshness"] == [
        {
            "name": "aats-liquidations-daemon",
            "heartbeat_path": "/tmp/aats_liquidations_heartbeat",
            "heartbeat_at": "2026-08-24T00:00:53+00:00",
                "heartbeat_age_seconds": 50.0,
            "fresh": True,
        },
        {
            "name": "aats-microstructure-collector",
            "heartbeat_path": "/tmp/aats_microstructure_heartbeat",
            "heartbeat_at": "2026-08-24T00:00:53+00:00",
                "heartbeat_age_seconds": 50.0,
            "fresh": True,
        }
    ]


def test_evidence_rejects_stale_or_future_collector_heartbeat() -> None:
    module = _load_evidence_module()
    now = datetime(2026, 8, 24, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="collector_heartbeat_stale"):
        module._collector_heartbeat_fact(
            "aats-microstructure-collector",
            heartbeat_epoch=int(now.timestamp()) - 60,
            now=now,
        )
    with pytest.raises(RuntimeError, match="collector_heartbeat_in_future"):
        module._collector_heartbeat_fact(
            "aats-microstructure-collector",
            heartbeat_epoch=int(now.timestamp()) + 6,
            now=now,
        )


def test_evidence_observes_heartbeat_after_reading_it(tmp_path: Path) -> None:
    module = _load_evidence_module()
    started_at = datetime.fromtimestamp(
        HEALTH_BOUNDARY_NS // 1_000_000_000,
        tz=UTC,
    )
    heartbeat_at = started_at.replace(second=5)
    observed_at = started_at.replace(second=6)
    observed_later = started_at.replace(second=7)
    clock_values = iter((started_at, observed_at, observed_later))
    image_id = "sha256:" + "b" * 64

    def clock() -> datetime:
        return next(clock_values)

    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = _evidence_runner(
        module,
        profile="derivatives",
        image_id=image_id,
        heartbeat_epoch=int(heartbeat_at.timestamp()),
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    final_clock_values = iter(
        (HEALTH_BOUNDARY_NS + 40_000_000_000, HEALTH_BOUNDARY_NS + 41_000_000_000)
    )

    payload = module.build_evidence(
        repo_root=tmp_path,
        profile="derivatives",
        overlay="docker-compose.aats.derivatives.yml",
        schema_job_status="passed",
        runtime_readiness_generation=READINESS_GENERATION,
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        required_containers=names,
        nats_stream_probe=_matched_nats_stream_rows,
        **_writer_monitor_kwargs(names),
        health_boundary_started_ns=HEALTH_BOUNDARY_NS,
        health_boundary_app_fingerprint=health_boundary_fingerprint,
        collector_heartbeat_epochs=heartbeat_epochs,
        nats_cutover_preflight_before_path=before_path,
        nats_cutover_preflight_after_path=after_path,
        run=fake_run,
        clock=clock,
        nanosecond_clock=lambda: next(final_clock_values),
    )

    assert payload["generated_at"] == started_at.isoformat()
    assert payload["collector_freshness"][0]["heartbeat_age_seconds"] == 38.0
    assert payload["collector_freshness"][1]["heartbeat_age_seconds"] == 38.0


@pytest.mark.parametrize(
    ("final_end_offset_seconds", "expected_error"),
    (
        (2, "final_deployment_stability_window_too_short"),
        (61, "final_deployment_evidence_window_exceeded"),
    ),
)
def test_final_evidence_window_is_bounded_from_health_boundary(
    tmp_path: Path,
    final_end_offset_seconds: int,
    expected_error: str,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(module, profile="spot", image_id=image_id)
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    final_clock_values = iter(
        (
            HEALTH_BOUNDARY_NS + 1_000_000_000,
            HEALTH_BOUNDARY_NS + final_end_offset_seconds * 1_000_000_000,
        )
    )

    with pytest.raises(RuntimeError, match=expected_error):
        module.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=fake_run,
            nanosecond_clock=lambda: next(final_clock_values),
        )


def test_final_evidence_rechecks_collector_freshness_at_window_end(
    tmp_path: Path,
) -> None:
    module = _load_evidence_module()
    image_id = "sha256:" + "b" * 64
    boundary_epoch = HEALTH_BOUNDARY_NS // 1_000_000_000
    names, fake_run, health_boundary_fingerprint, heartbeat_epochs = (
        _evidence_runner(
            module,
            profile="derivatives",
            image_id=image_id,
            heartbeat_epoch=boundary_epoch - 50,
        )
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)
    final_clock_values = iter(
        (HEALTH_BOUNDARY_NS + 40_000_000_000, HEALTH_BOUNDARY_NS + 41_000_000_000)
    )

    with pytest.raises(RuntimeError, match="collector_heartbeat_stale"):
        module.build_evidence(
            repo_root=tmp_path,
            profile="derivatives",
            overlay="docker-compose.aats.derivatives.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=fake_run,
            generated_at=datetime.fromtimestamp(boundary_epoch, tz=UTC),
            nanosecond_clock=lambda: next(final_clock_values),
        )


def test_collector_heartbeat_cli_arguments_are_exact_and_injection_safe() -> None:
    module = _load_evidence_module()
    assert module._parse_collector_heartbeat_epochs(
        (
            "aats-liquidations-daemon=1787880000",
            "aats-microstructure-collector=1787880001",
        )
    ) == {
        "aats-liquidations-daemon": 1787880000,
        "aats-microstructure-collector": 1787880001,
    }
    for invalid in (
        ("aats-liquidations-daemon=1;touch /tmp/pwn",),
        ("unknown=1787880000",),
        ("aats-liquidations-daemon=1787880000",) * 2,
    ):
        with pytest.raises(ValueError, match="invalid_collector_heartbeat_epoch_argument"):
            module._parse_collector_heartbeat_epochs(invalid)


def test_evidence_writer_refuses_overwrite(tmp_path: Path) -> None:
    module = _load_evidence_module()
    payload = {
        "generated_at": "2026-08-24T00:00:00+00:00",
        "deployed_commit": "c" * 40,
        "profile": "derivatives",
    }

    target = module.write_evidence(repo_root=tmp_path, payload=payload)
    try:
        assert stat.S_IMODE(target.stat().st_mode) & stat.S_IWUSR == 0
        with pytest.raises(FileExistsError):
            module.write_evidence(repo_root=tmp_path, payload=payload)
    finally:
        target.chmod(0o644)


def test_evidence_builder_rejects_live_or_unhealthy_container(tmp_path: Path) -> None:
    module = _load_evidence_module()
    with pytest.raises(ValueError, match="simulation_profile"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives-live",
            overlay="live.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id="test-deployment-lock",
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=("aats-gateway",),
            nats_stream_probe=_matched_nats_stream_rows,
            lifecycle_monitor_control_dir=Path("/tmp/unused"),
            lifecycle_monitor_token="monitor-token",
            app_up_authorized_ns=HEALTH_BOUNDARY_NS - 1,
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint="sha256:" + "1" * 64,
            collector_heartbeat_epochs={},
        )

    image_id = "sha256:" + "e" * 64
    names, unhealthy_run, health_boundary_fingerprint, heartbeat_epochs = _evidence_runner(
        module,
        profile="spot",
        image_id=image_id,
        unhealthy_name="aats-gateway",
    )
    before_path, after_path = _write_preflight_pair(tmp_path, module)

    with pytest.raises(
        RuntimeError,
        match="container_health_failed_after_boundary:aats-gateway",
    ):
        module.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation=READINESS_GENERATION,
            deployment_lock_id="test-deployment-lock",
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=health_boundary_fingerprint,
            collector_heartbeat_epochs=heartbeat_epochs,
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=unhealthy_run,
        )


@pytest.mark.parametrize("generation", ["", "bad generation", "bad/generation", "x" * 129])
def test_evidence_builder_rejects_invalid_runtime_readiness_generation(generation: str) -> None:
    module = _load_evidence_module()

    with pytest.raises(ValueError, match="invalid_runtime_readiness_generation"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives",
            overlay="docker-compose.aats.derivatives.yml",
            schema_job_status="passed",
            runtime_readiness_generation=generation,
            deployment_lock_id="test-deployment-lock",
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=(
                "aats-gateway",
                "aats-market",
                "aats-decision",
                "aats-execution",
                "aats-rdp-daemon",
                "aats-liquidations-daemon",
                "aats-microstructure-collector",
            ),
            nats_stream_probe=_matched_nats_stream_rows,
            lifecycle_monitor_control_dir=Path("/tmp/unused"),
            lifecycle_monitor_token="monitor-token",
            app_up_authorized_ns=HEALTH_BOUNDARY_NS - 1,
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint="sha256:" + "1" * 64,
            collector_heartbeat_epochs={},
        )
