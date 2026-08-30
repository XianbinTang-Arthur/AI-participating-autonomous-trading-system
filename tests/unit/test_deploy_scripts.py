import asyncio
import hashlib
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import check_nats_durable_cutover as cutover
from scripts import nats_runtime_identity
from scripts import write_deployment_evidence as deployment_evidence


REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_ENV_FILE = REPO_ROOT / "configs" / "templates" / ".env.derivatives.example"
DEPLOYED_COMMIT = "a" * 40
DEPLOYMENT_LOCK_ID = "lock-20260828-1"
NATS_TARGET_MANIFEST_SHA256 = str(
    cutover._default_target_stream_manifest()["sha256"]
)
NATS_BASELINE_FINGERPRINT = "sha256:" + "b" * 64
NATS_POST_IDENTITY_FACTS = "\n".join(
    (
        json.dumps("f" * 64),
        json.dumps("sha256:" + "1" * 64),
        json.dumps(nats_runtime_identity.NATS_EXPECTED_IMAGE),
        json.dumps("running"),
        json.dumps("healthy"),
        "0",
        json.dumps("2026-08-28T00:01:30Z"),
        "0",
        json.dumps("aats-dev"),
        json.dumps("nats"),
        json.dumps("volume"),
        json.dumps("aats-dev_nats_data"),
        "true",
    )
)
NATS_POST_RECREATE_FINGERPRINT = (
    nats_runtime_identity.parse_nats_container_identity(
        NATS_POST_IDENTITY_FACTS
    ).fingerprint
)
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
                        "Start": "2026-08-28T00:01:20Z",
                        "End": "2026-08-28T00:01:21Z",
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
        json.dumps("2026-08-27T00:00:00Z"),
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
    "baseline_fingerprint": NATS_BASELINE_FINGERPRINT,
    "volume_fingerprint": NATS_VOLUME_FINGERPRINT,
}
HEALTH_BOUNDARY_NS = int(
    datetime(2026, 8, 28, 0, 1, 2, tzinfo=timezone.utc).timestamp()
    * 1_000_000_000
)


def _nats_identity(
    fingerprint: str,
    *,
    restart_count: int = 0,
) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "container_id": fingerprint.removeprefix("sha256:"),
        "restart_count": restart_count,
    }


def _critical_stream_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for target in cutover._default_target_stream_manifest()["streams"]:
        config = target["immutable_config"]
        rows.append(
            {
                "identity": dict(target["identity"]),
                "created": "2026-08-28T00:00:00Z",
                "immutable_config": dict(config),
                "state": {
                    "messages": 0,
                    "bytes": 0,
                    "first_seq": 0,
                    "last_seq": 0,
                    "consumer_count": 0,
                    "deleted": [],
                    "num_deleted": 0,
                },
            }
        )
    return rows


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
                created="2026-08-28T00:01:30Z",
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
    for row in _critical_stream_rows():
        config = row["immutable_config"]
        state = row["state"]
        name = row["identity"]["name"]
        streams.append(
            cutover.CriticalStreamState(
                name=name,
                created=row["created"],
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
                messages=state["messages"],
                bytes=state["bytes"],
                first_seq=state["first_seq"],
                last_seq=state["last_seq"],
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


def _app_quiescence_snapshot() -> tuple[dict[str, object], ...]:
    return tuple(
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


def _passed_app_quiescence(
    checked_at: datetime | None = None,
) -> dict[str, object]:
    snapshot = _app_quiescence_snapshot()
    start_ns = int(
        (
            checked_at
            or datetime(2026, 8, 28, tzinfo=timezone.utc)
        ).timestamp()
        * 1_000_000_000
    )
    return cutover.build_app_quiescence_evidence(
        since_ns=start_ns,
        until_ns=start_ns + 1_000_000_000,
        before=snapshot,
        after=snapshot,
        events=(),
        event_capture=_live_event_capture(start_ns, start_ns + 1_000_000_000),
    )


def _baseline_continuity() -> dict[str, object]:
    return {
        "status": "BASELINE_CAPTURED",
        "complete": True,
        "baseline_sha256": None,
        "streams_checked": 0,
        "durables_checked": 0,
        "passive_retention_trims": [],
        "violations": [],
    }


def _critical_stream_row(
    *,
    name: str = "AATS_EVENTS_COMMANDS",
    created: str = "2026-08-28T00:00:00Z",
    messages: int = 5,
    byte_count: int = 500,
    first_seq: int = 1,
    last_seq: int = 5,
) -> dict[str, object]:
    target_stream = next(
        row
        for row in cutover._default_target_stream_manifest()["streams"]
        if row["identity"]["name"] == name
    )
    return {
        "identity": {"name": name},
        "created": created,
        "immutable_config": json.loads(
            json.dumps(target_stream["immutable_config"])
        ),
        "state": {
            "messages": messages,
            "bytes": byte_count,
            "first_seq": first_seq,
            "last_seq": last_seq,
            "consumer_count": 1,
            "deleted": [],
            "num_deleted": 0,
        },
    }


def _container_inspect_payload(
    names: tuple[str, ...],
    *,
    image_id: str,
    generation: str = "generation-1",
    commit: str = DEPLOYED_COMMIT,
    profile: str = "spot",
    restart_count: int = 0,
) -> str:
    payload = []
    for index, name in enumerate(names, start=1):
        payload.append(
            {
                "Id": f"{index:064x}",
                "Name": f"/{name}",
                "Image": image_id,
                "RestartCount": restart_count,
                "State": {
                    "Status": "running",
                    "StartedAt": "2026-08-28T00:00:00Z",
                    "Health": {
                        "Status": "healthy",
                        "FailingStreak": 0,
                        "Checks": [
                            {
                                "Start": "2026-08-28T00:01:03.000000001Z",
                                "End": "2026-08-28T00:01:03.000000002Z",
                                "ExitCode": 0,
                            }
                        ],
                    },
                },
                "ComposeProject": "aats-dev",
                "ComposeService": name,
                "NatsTargetManifestSha256": NATS_TARGET_MANIFEST_SHA256,
                "SafeEnvironment": {
                    "AATS_RUNTIME_READINESS_GENERATION": generation,
                    "AATS_DEPLOYED_GIT_COMMIT": commit,
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
                ),
            }
        )
    return "\n".join(json.dumps(row, sort_keys=True) for row in payload)


def _decode_container_inspect_payload(raw: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in raw.splitlines() if line]


def _encode_container_inspect_payload(rows: list[dict[str, object]]) -> str:
    return "\n".join(json.dumps(row, sort_keys=True) for row in rows)


def _health_boundary_fingerprint(
    names: tuple[str, ...],
    *,
    image_id: str,
    profile: str = "spot",
    inspect_payload: str | None = None,
) -> str:
    payload = inspect_payload or _container_inspect_payload(
        names,
        image_id=image_id,
        profile=profile,
    )
    facts, bindings = deployment_evidence._container_snapshot(
        names,
        expected_image_id=image_id,
        expected_generation="generation-1",
        expected_commit=DEPLOYED_COMMIT,
        expected_profile=profile,
        expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
        run=lambda _args, _cwd=None: payload,
    )
    return deployment_evidence._app_runtime_snapshot_fingerprint(
        facts,
        bindings,
    )


def _writer_monitor_kwargs(
    names: tuple[str, ...],
) -> dict[str, object]:
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
            if name in deployment_evidence._COLLECTOR_HEARTBEATS:
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
            "/tmp/aats-docker-event-monitor-test"
        ),
        "lifecycle_monitor_token": "monitor-token",
        "app_up_authorized_ns": app_up_authorized_ns,
        "lifecycle_monitor_sealer": _sealer,
    }


def _nats_runtime_inspect_result(command: tuple[str, ...]) -> str | None:
    if command == (
        "docker",
        "inspect",
        "--format",
        deployment_evidence._NATS_IDENTITY_INSPECT_TEMPLATE,
        "aats-nats",
    ):
        return NATS_POST_IDENTITY_FACTS
    if command == (
        "docker",
        "inspect",
        "--format",
        deployment_evidence._NATS_HEALTH_INSPECT_TEMPLATE,
        "aats-nats",
    ):
        return NATS_HEALTH_FACTS
    if command == (
        "docker",
        "image",
        "inspect",
        nats_runtime_identity.NATS_EXPECTED_IMAGE,
        "--format",
        "{{.Id}}",
    ):
        return "sha256:" + "1" * 64
    if command == (
        "docker",
        "volume",
        "inspect",
        "--format",
        deployment_evidence._NATS_VOLUME_INSPECT_TEMPLATE,
        "aats-dev_nats_data",
    ):
        return NATS_VOLUME_FACTS
    return None


def _write_preflight_pair(tmp_path: Path) -> tuple[Path, Path]:
    preflight_dir = tmp_path / "artifacts" / "deployments"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    before_path = preflight_dir / "before.json"
    after_path = preflight_dir / "after.json"
    query, durables, streams = _qualified_preflight_snapshot()
    before_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=query,
        rows=durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(
            datetime(2026, 8, 28, tzinfo=timezone.utc)
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        critical_streams=streams,
        continuity={
            **_baseline_continuity(),
            "streams_checked": len(streams),
            "durables_checked": len(durables),
        },
    )
    before_path.write_text(
        json.dumps(before_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    before_reference, _ = deployment_evidence._nats_cutover_preflight_reference(
        repo_root=tmp_path,
        path=before_path,
        runtime_readiness_generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        expected_stage="pre_full_down",
    )
    after_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, 0, 1, tzinfo=timezone.utc),
        query_result=query,
        rows=durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(
            datetime(2026, 8, 28, 0, 1, tzinfo=timezone.utc)
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_POST_RECREATE_FINGERPRINT,
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


def _cutover_cli_args() -> list[str]:
    return [
        "--generation",
        "generation-1",
        "--deployment-lock-id",
        DEPLOYMENT_LOCK_ID,
        "--deployed-commit",
        DEPLOYED_COMMIT,
        "--target-env-file",
        str(TARGET_ENV_FILE),
        "--stage",
        "pre_full_down",
        "--nats-bootstrap-mode",
        "existing_container_preserved",
        "--nats-baseline-fingerprint",
        NATS_BASELINE_FINGERPRINT,
        "--nats-volume-fingerprint",
        NATS_VOLUME_FINGERPRINT,
    ]


def _patch_app_quiescence(monkeypatch) -> None:
    class _FakeEventMonitor:
        def __init__(self, *_args, **_kwargs) -> None:
            self._start_ns: int | None = None

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, *_args) -> None:
            return None

        def start(self) -> int:
            if self._start_ns is None:
                self._start_ns = time.time_ns()
            return self._start_ns

        def seal(self, cutoff_ns: int) -> dict[str, object]:
            assert self._start_ns is not None
            events = cutover.query_app_lifecycle_events(
                since_ns=self._start_ns,
                until_ns=cutoff_ns,
            )
            return _live_event_capture(
                self._start_ns,
                cutoff_ns,
                events=events,
            )

    monkeypatch.setattr(cutover, "LiveDockerEventMonitor", _FakeEventMonitor)
    monkeypatch.setattr(
        cutover,
        "capture_app_quiescence",
        _app_quiescence_snapshot,
    )
    monkeypatch.setattr(
        cutover,
        "query_app_lifecycle_events",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        cutover,
        "capture_nats_identity",
        lambda: _nats_identity(NATS_BASELINE_FINGERPRINT),
    )
    monkeypatch.setattr(
        cutover,
        "capture_nats_volume_fingerprint",
        lambda: NATS_VOLUME_FINGERPRINT,
    )


def test_sync_to_wsl2_pull_tracks_current_source_head() -> None:
    text = (REPO_ROOT / "scripts" / "sync_to_wsl2.sh").read_text(encoding="utf-8")

    assert "git fetch '$WIN_PROJECT_WSL' main" not in text
    assert "git symbolic-ref --quiet --short HEAD" in text
    assert "git rev-parse HEAD" in text


def test_sync_to_wsl2_branch_drift_repair_does_not_swallow_checkout_failures() -> None:
    text = (REPO_ROOT / "scripts" / "sync_to_wsl2.sh").read_text(encoding="utf-8")

    assert "checkout -b '$source_branch' FETCH_HEAD 2>/dev/null || true" not in text
    assert "git -C $WSL_PROJECT fetch '$WIN_PROJECT_WSL' '$source_branch'" in text


def test_standard_deploy_sync_is_one_acknowledged_wsl_git_transaction() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    sync = text.split("step_sync() {", 1)[1].split("\n}", 1)[0]
    builder = text.split("build_wsl_checkout_sync_command() {", 1)[1].split(
        "\n}", 1
    )[0]

    assert "sync_to_wsl2.sh" not in sync
    assert 'wsl_run "$sync_command"' in sync
    assert "build_wsl_checkout_sync_command" in sync
    assert "exit 22" in builder
    assert "exit 23" in builder
    assert "merge --ff-only FETCH_HEAD" in builder
    assert '[[ "${wsl_head_after,,}" != "$source_head" ]]' in builder


def test_deploy_script_rejects_dirty_synced_deploys() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "当前同步机制只会部署已提交的 Git HEAD" in text
    assert "继续部署 WSL2 侧现有代码？[y/N]" in text


def test_deploy_script_commit_only_uses_precisely_staged_files() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "\n            git add -A" not in text
    assert "--commit 只提交已精确暂存的文件" in text
    assert "repo_has_staged_changes()" in text
    assert "repo_has_unstaged_or_untracked_changes()" in text


def test_runtime_evidence_directories_are_gitignored() -> None:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "deploy/wsl2-dev/runtime/deployment-evidence/" in text
    assert "deploy/wsl2-dev/runtime/execution-funnel-evidence/" in text


def test_deployment_evidence_batch_inspect_binds_safe_runtime_facts() -> None:
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    image_id = "sha256:" + "1" * 64
    calls: list[tuple[str, ...]] = []

    def _run(args, _cwd=None) -> str:
        calls.append(tuple(args))
        return _container_inspect_payload(names, image_id=image_id)

    facts, bindings = deployment_evidence._container_snapshot(
        names,
        expected_image_id=image_id,
        expected_generation="generation-1",
        expected_commit=DEPLOYED_COMMIT,
        expected_profile="spot",
        expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
        run=_run,
    )

    assert calls == [
        (
            "docker",
            "inspect",
            "--format",
            deployment_evidence._APP_CONTAINER_INSPECT_TEMPLATE,
            *names,
        )
    ]
    assert bindings == [
        {
            "container_port": "8000/tcp",
            "host_ip": "127.0.0.1",
            "host_port": "8001",
        }
    ]
    assert facts[0]["container_id"] == f"{1:064x}"
    assert facts[0]["image_id"] == image_id
    assert facts[0]["restart_count"] == 0
    assert facts[0]["safe_environment"] == {
        "AATS_RUNTIME_READINESS_GENERATION": "generation-1",
        "AATS_DEPLOYED_GIT_COMMIT": DEPLOYED_COMMIT,
        "AATS_PROFILE": "spot",
    }
    assert "POSTGRES_PASSWORD" not in json.dumps(facts)
    assert ".Config.Env" not in deployment_evidence._APP_CONTAINER_INSPECT_TEMPLATE
    assert "{{json .}}" not in deployment_evidence._APP_CONTAINER_INSPECT_TEMPLATE


def test_deployment_evidence_rejects_image_or_safe_env_mismatch() -> None:
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    expected_image_id = "sha256:" + "1" * 64
    other_image_id = "sha256:" + "2" * 64

    with pytest.raises(RuntimeError, match="container_image_not_current_build"):
        deployment_evidence._container_snapshot(
            names,
            expected_image_id=expected_image_id,
            expected_generation="generation-1",
            expected_commit=DEPLOYED_COMMIT,
            expected_profile="spot",
            expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
            run=lambda _args, _cwd=None: _container_inspect_payload(
                names,
                image_id=other_image_id,
            ),
        )

    with pytest.raises(RuntimeError, match="container_environment_mismatch"):
        deployment_evidence._container_snapshot(
            names,
            expected_image_id=expected_image_id,
            expected_generation="different-generation",
            expected_commit=DEPLOYED_COMMIT,
            expected_profile="spot",
            expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
            run=lambda _args, _cwd=None: _container_inspect_payload(
                names,
                image_id=expected_image_id,
            ),
        )

    incomplete_payload = _decode_container_inspect_payload(
        _container_inspect_payload(names, image_id=expected_image_id)
    )
    del incomplete_payload[0]["SafeEnvironment"]["AATS_PROFILE"]
    with pytest.raises(RuntimeError, match="invalid_container_environment"):
        deployment_evidence._container_snapshot(
            names,
            expected_image_id=expected_image_id,
            expected_generation="generation-1",
            expected_commit=DEPLOYED_COMMIT,
            expected_profile="spot",
            expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
            run=lambda _args, _cwd=None: _encode_container_inspect_payload(
                incomplete_payload
            ),
        )

    target_drift_payload = _decode_container_inspect_payload(
        _container_inspect_payload(names, image_id=expected_image_id)
    )
    target_drift_payload[0]["NatsTargetManifestSha256"] = "sha256:" + "0" * 64
    with pytest.raises(
        RuntimeError,
        match="nats_target_manifest_sha256_mismatch:aats-gateway",
    ):
        deployment_evidence._container_snapshot(
            names,
            expected_image_id=expected_image_id,
            expected_generation="generation-1",
            expected_commit=DEPLOYED_COMMIT,
            expected_profile="spot",
            expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
            run=lambda _args, _cwd=None: _encode_container_inspect_payload(
                target_drift_payload
            ),
        )


def test_deployment_evidence_requires_exact_profile_container_set() -> None:
    with pytest.raises(ValueError, match="required_container_set_mismatch"):
        deployment_evidence.build_evidence(
            repo_root=REPO_ROOT,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=["aats-gateway"],
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(
                deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
            ),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint="sha256:" + "1" * 64,
            collector_heartbeat_epochs={},
            run=lambda _args, _cwd=None: "unused",
        )


def test_full_deployment_evidence_binds_stable_runtime_and_preflight_chain(
    tmp_path: Path,
) -> None:
    before_path, after_path = _write_preflight_pair(tmp_path)
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    image_id = "sha256:" + "1" * 64
    inspect_payload = _container_inspect_payload(names, image_id=image_id)
    calls: list[tuple[str, ...]] = []

    def _run(args, _cwd=None) -> str:
        command = tuple(args)
        calls.append(command)
        nats_result = _nats_runtime_inspect_result(command)
        if nats_result is not None:
            return nats_result
        if command == ("git", "rev-parse", "HEAD"):
            return DEPLOYED_COMMIT
        if command[:3] == ("docker", "image", "inspect"):
            return image_id
        if command[:2] == ("docker", "inspect"):
            return inspect_payload
        if command[:2] == ("docker", "events"):
            return ""
        raise AssertionError(command)

    final_clock_values = iter(
        (HEALTH_BOUNDARY_NS + 40_000_000_000, HEALTH_BOUNDARY_NS + 41_000_000_000)
    )
    payload = deployment_evidence.build_evidence(
        repo_root=tmp_path,
        profile="spot",
        overlay="docker-compose.aats.spot.yml",
        schema_job_status="passed",
        runtime_readiness_generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        required_containers=names,
        nats_stream_probe=_matched_nats_stream_rows,
        **_writer_monitor_kwargs(names),
        health_boundary_started_ns=HEALTH_BOUNDARY_NS,
        health_boundary_app_fingerprint=_health_boundary_fingerprint(
            names,
            image_id=image_id,
            inspect_payload=inspect_payload,
        ),
        collector_heartbeat_epochs={},
        nats_cutover_preflight_before_path=before_path,
        nats_cutover_preflight_after_path=after_path,
        run=_run,
        generated_at=datetime(2026, 8, 28, 0, 2, tzinfo=timezone.utc),
        nanosecond_clock=lambda: next(final_clock_values),
    )

    assert payload["status"] == "simulation_stack_healthy_bounded_observation"
    assert payload["deployed_commit"] == DEPLOYED_COMMIT
    assert payload["base_image_id"] == image_id
    assert (
        payload["nats_durable_cutover_preflights"]["status"]
        == "PASSED_WITH_TRUST_BOUNDARY"
    )
    assert len(payload["required_containers"]) == len(names)
    assert (
        payload["container_runtime_evidence_window"]["status"]
        == "PASSED_WITH_TRUST_BOUNDARY"
    )
    assert (
        payload["container_runtime_evidence_window"]
        ["post_health_boundary_lifecycle_events"]
        == []
    )
    app_inspect = (
        "docker",
        "inspect",
        "--format",
        deployment_evidence._APP_CONTAINER_INSPECT_TEMPLATE,
        *names,
    )
    assert sum(call == app_inspect for call in calls) == 4
    assert sum(
        call[:4] == ("docker", "image", "inspect", "aats-base:dev")
        for call in calls
    ) == 3
    assert sum(
        call[:4]
        == (
            "docker",
            "image",
            "inspect",
            nats_runtime_identity.NATS_EXPECTED_IMAGE,
        )
        for call in calls
    ) == 3


def test_standard_deploy_observes_minimum_stability_before_evidence() -> None:
    source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    stability = source.split("observe_app_stability_window() {", 1)[1].split(
        "\n}",
        1,
    )[0]
    main = source.split("main() {", 1)[1]

    assert "APP_STABILITY_WINDOW_SECONDS=40" in source
    assert deployment_evidence._MIN_FINAL_EVIDENCE_WINDOW_SECONDS == 35.0
    assert deployment_evidence._MAX_FINAL_EVIDENCE_WINDOW_SECONDS == 60.0
    health_probe = source.split(
        "all_required_app_containers_healthy() {", 1
    )[1].split("\n}", 1)[0]
    nats_health_probe = source.split(
        "nats_container_health_ok_since() {", 1
    )[1].split("\n}", 1)[0]
    assert ".State.Health.FailingStreak" in health_probe
    assert ".State.Health.Log" in health_probe
    assert ".ExitCode" in health_probe
    assert "health-check --since-ns" in nats_health_probe
    assert "--require-success-after-boundary" not in nats_health_probe
    assert main.index('run_locked_step "健康检查" step_health') < main.index(
        'run_locked_step "稳定性观察" observe_app_stability_window'
    ) < main.index('run_locked_step "部署证据" write_deployment_evidence')
    assert 'sleep "$interval"' in stability
    assert stability.count("all_required_app_containers_healthy") == 2
    assert stability.count("gateway_health_ok") == 2
    assert stability.count("nats_container_health_ok_since") == 2
    assert "return 17" in stability


def test_standard_deploy_freezes_and_revalidates_nats_target_snapshot() -> None:
    source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    main = source.split("main() {", 1)[1]
    preflight = source.split("run_nats_durable_cutover_preflight() {", 1)[1].split(
        "\n}", 1
    )[0]
    app_up = source.split("step_app_up() {", 1)[1].split("\n}", 1)[0]

    assert main.index('run_locked_step "代码同步" step_sync') < main.index(
        'run_locked_step "NATS 目标参数冻结" prepare_nats_target_env_snapshot'
    ) < main.index('run_locked_step "镜像构建" step_build')
    assert "--target-env-file '$NATS_TARGET_ENV_SNAPSHOT_PATH'" in preflight
    assert "--target-env-file '$ENV_PROFILE_PATH'" not in preflight
    assert preflight.index("assert_nats_target_env_snapshot") < preflight.index(
        "check_nats_durable_cutover.py"
    )
    assert app_up.count("assert_nats_target_env_snapshot") == 2
    assert "stat -Lc '%u:%g:%a:%F'" in source
    assert "0:0:444:regular file" in source
    assert "NATS_TARGET_MANIFEST_SHA256" in source
    assert "NATS_TARGET_ENV_SNAPSHOT_PATH" in source
    assert 'render --source \\"$ENV_PROFILE_PATH\\"' in source
    assert "render --source '$ENV_PROFILE_PATH'" not in source


def test_nats_target_root_scripts_use_exact_base64_transport() -> None:
    source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    helper = source.split("wsl_root_run_script() {", 1)[1].split("\n}", 1)[0]
    prepare = source.split("prepare_nats_target_env_snapshot() {", 1)[1].split(
        "\n}", 1
    )[0]
    cleanup = source.split("cleanup_nats_target_env_snapshot() {", 1)[1].split(
        "\n}", 1
    )[0]

    assert "base64 --decode | bash" in helper
    assert 'wsl_root_run_script "$root_script"' in prepare
    assert 'wsl_root_run_script "$root_script"' in cleanup
    assert 'wsl_root_run "set -euo pipefail' not in prepare
    assert 'wsl_root_run "set -euo pipefail' not in cleanup


def test_marker_cleanup_requires_local_completion_or_remote_acknowledgement() -> None:
    source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    helper = source.split("remove_proven_completed_active_marker() {", 1)[1].split(
        "\n}", 1
    )[0]
    ack_builder = source.split("build_wsl_completion_wrapped_command() {", 1)[
        1
    ].split("\n}", 1)[0]
    ack_finalizer = source.split("finalize_proven_wsl_completion() {", 1)[1].split(
        "\n}", 1
    )[0]
    transport = source.split("run_wsl_completion_transport() {", 1)[1].split(
        "\n}", 1
    )[0]
    guard = source.split("run_supervised_command_guard() {", 1)[1].split(
        "\n}", 1
    )[0]
    status_zero = guard.split("status-zero)", 1)[1].split(";;", 1)[0]
    parent_cleanup = source.split("remove_active_supervision_artifacts() {", 1)[
        1
    ].split("\n}", 1)[0]

    assert "for attempt in {1..10}" in helper
    assert "rm -f --" in helper
    assert source.count("remove_proven_completed_active_marker") == 4
    assert guard.count('remove_proven_completed_active_marker "$marker_file"') == 2
    assert 'if [[ "$status" -ne 0 ]]' in guard
    assert "finalize_proven_wsl_completion" in guard
    assert status_zero.index('if [[ "$status" -ne 0 ]]') < status_zero.index(
        'remove_proven_completed_active_marker "$marker_file"'
    )
    assert ack_builder.index('bash -c "$remote_command"') < ack_builder.index(
        "printf '%s\\\\t%s\\\\t%s"
    )
    assert '[[ -e "$completion_file" ]]' in ack_builder
    assert "stdout_sha256" in ack_builder
    assert "stderr_sha256" in ack_builder
    assert 'ln -- "$completion_tmp" "$completion_file"' in ack_builder
    assert 'pending_signal_status=0' in ack_builder
    assert 'completion_phase=preflight' in ack_builder
    assert 'completion_phase=run_remote_command' in ack_builder
    assert 'completion_phase=hash_output' in ack_builder
    assert 'completion_phase=publish_completion' in ack_builder
    assert "WSL completion wrapper failed: phase=%s status=%s" in ack_builder
    assert 'record_pending_signal 129' in ack_builder
    assert 'record_pending_signal 130' in ack_builder
    assert 'record_pending_signal 143' in ack_builder
    assert "EXIT HUP INT TERM" not in ack_builder
    assert ack_builder.index('bash -c "$remote_command"') < ack_builder.index(
        'trap - HUP INT TERM'
    )
    assert ack_builder.index('ln -- "$completion_tmp" "$completion_file"') < (
        ack_builder.index('if [[ "$transport_status" -ne 0 ]]')
    )
    assert "expected_marker_uid" in ack_builder
    assert 'MSYS_NO_PATHCONV=1 "$@"' in guard
    assert "MSYS2_ARG_CONV_EXCL='*'" in guard
    assert "script=sys.stdin.read()" in transport
    assert "stdin=subprocess.DEVNULL" in transport
    assert "128-status if status<0 else status" in transport
    assert "MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1" in transport
    assert 'supervised_command=(run_wsl_completion_transport' in source
    assert "printf '%s' '$wrapped_encoded' | base64 --decode | bash" not in source
    assert "os.O_EXCL" in source
    assert '[[ "$ack_marker" == "$marker_file"' in ack_finalizer
    assert '[[ "$ack_io_mode" == "$expected_io_mode" ]]' in ack_finalizer
    assert "ack_stdout_sha256" in ack_finalizer
    assert 'if [[ -e "$marker_file" ]]' in ack_finalizer
    assert 'rm -f -- "$marker_file"' in ack_finalizer
    assert "rm -f -- $completion_q" in ack_finalizer
    assert "remove_proven_completed_active_marker" not in parent_cleanup
    assert 'run_lock_supervised_wsl "WSL 命令" default "$io_mode"' in source
    assert 'run_lock_supervised_wsl "WSL root 命令" root "$io_mode"' in source
    assert 'if [[ "$status" -ne 0 ]]' in guard.split("wsl-ack)", 1)[1]
    wsl_ack = guard.split("wsl-ack)", 1)[1]
    assert wsl_ack.count("finalize_proven_wsl_completion") == 2
    assert wsl_ack.index('if [[ "$status" -ne 0 ]]') < wsl_ack.index(
        "finalize_proven_wsl_completion"
    )
    assert wsl_ack.index("sha256sum -- \"$stdout_file\"") < wsl_ack.index(
        'if [[ "$status" -ne 0 ]]'
    )
    assert 'cat -- "$stdout_file"' in guard
    assert 'if ! cat -- "$stdout_file"' in guard
    assert "sha256sum -- \"$stdout_file\"" in guard
    assert guard.count("WSL completion acknowledgement 缺失或校验失败") == 2
    assert "assert_no_owned_active_markers" in source


@pytest.mark.parametrize(
    ("profile", "overlay_name"),
    (
        ("spot", "docker-compose.aats.spot.yml"),
        ("derivatives", "docker-compose.aats.derivatives.yml"),
    ),
)
def test_every_simulation_app_binds_the_same_frozen_nats_target(
    profile: str,
    overlay_name: str,
) -> None:
    deploy_dir = REPO_ROOT / "deploy" / "wsl2-dev"
    base = yaml.safe_load(
        (deploy_dir / "docker-compose.aats.yml").read_text(encoding="utf-8")
    )
    overlay = yaml.safe_load(
        (deploy_dir / overlay_name).read_text(encoding="utf-8")
    )
    snapshot_path = (
        "${AATS_NATS_TARGET_ENV_SNAPSHOT_PATH:?NATS target snapshot required}"
    )
    manifest_value = (
        "${AATS_NATS_TARGET_MANIFEST_SHA256:?NATS target manifest sha256 required}"
    )
    label = "com.aats.nats-target-manifest-sha256"

    for name in deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE[profile]:
        service = overlay["services"][name]
        assert service["env_file"][-1] == snapshot_path
        label_source = base["services"].get(name, service)
        assert label_source["labels"][label] == manifest_value


def test_final_nats_second_query_is_closed_by_runtime_identity_postcondition() -> None:
    source = (REPO_ROOT / "scripts" / "write_deployment_evidence.py").read_text(
        encoding="utf-8"
    )
    second_query = source.index(
        "final_query_after, final_stream_rows_after, final_durable_rows_after"
    )
    nats_postcondition = source.index(
        "nats_identity_after = _capture_nats_identity",
        second_query,
    )
    cutoff = source.index("window_ended_ns = nanosecond_clock()", second_query)

    assert second_query < nats_postcondition < cutoff


def test_full_deployment_evidence_rejects_transient_container_lifecycle_event(
    tmp_path: Path,
) -> None:
    before_path, after_path = _write_preflight_pair(tmp_path)
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    image_id = "sha256:" + "1" * 64
    inspect_payload = _container_inspect_payload(names, image_id=image_id)
    since_ns = HEALTH_BOUNDARY_NS
    until_ns = since_ns + 1_000_000_000
    def _run(args, _cwd=None) -> str:
        command = tuple(args)
        nats_result = _nats_runtime_inspect_result(command)
        if nats_result is not None:
            return nats_result
        if command == ("git", "rev-parse", "HEAD"):
            return DEPLOYED_COMMIT
        if command[:3] == ("docker", "image", "inspect"):
            return image_id
        if command[:2] == ("docker", "inspect"):
            return inspect_payload
        raise AssertionError(command)

    monitor_kwargs = _writer_monitor_kwargs(names)
    normal_sealer = monitor_kwargs["lifecycle_monitor_sealer"]

    def _bad_sealer(*args, cutoff_ns: int, **kwargs) -> dict[str, object]:
        packet = normal_sealer(*args, cutoff_ns=cutoff_ns, **kwargs)
        packet["events"].append(
            {
                "name": "aats-gateway",
                "container_id": f"{1:064x}",
                "action": "health_status: unhealthy",
                "time_nano": cutoff_ns - 1,
            }
        )
        packet["events"].sort(key=lambda row: int(row["time_nano"]))
        packet["segments"][0]["event_count"] += 1
        return packet

    monitor_kwargs["lifecycle_monitor_sealer"] = _bad_sealer
    clock_values = iter((since_ns, until_ns))
    with pytest.raises(
        RuntimeError,
        match="container_lifecycle_changed_after_health_boundary",
    ):
        deployment_evidence.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **monitor_kwargs,
            health_boundary_started_ns=since_ns,
            health_boundary_app_fingerprint=_health_boundary_fingerprint(
                names,
                image_id=image_id,
                inspect_payload=inspect_payload,
            ),
            collector_heartbeat_epochs={},
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=_run,
            nanosecond_clock=lambda: next(clock_values),
        )


def test_container_snapshot_rejects_recovered_health_failure_after_boundary() -> None:
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    image_id = "sha256:" + "1" * 64
    rows = _decode_container_inspect_payload(
        _container_inspect_payload(names, image_id=image_id)
    )
    rows[0]["State"]["Health"]["Checks"] = [
        {
            "Start": "2026-08-28T00:01:03.000000001Z",
            "End": "2026-08-28T00:01:03.000000002Z",
            "ExitCode": 1,
        },
        {
            "Start": "2026-08-28T00:01:04.000000001Z",
            "End": "2026-08-28T00:01:04.000000002Z",
            "ExitCode": 0,
        },
    ]

    with pytest.raises(
        RuntimeError,
        match="container_health_failed_after_boundary:aats-gateway",
    ):
        deployment_evidence._container_snapshot(
            names,
            expected_image_id=image_id,
            expected_generation="generation-1",
            expected_commit=DEPLOYED_COMMIT,
            expected_profile="spot",
            expected_target_manifest_sha256=NATS_TARGET_MANIFEST_SHA256,
            run=lambda _args, _cwd=None: _encode_container_inspect_payload(rows),
            health_window_started_ns=HEALTH_BOUNDARY_NS,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        (
            "missing_collector_archive",
            "invalid_container_archive_lifecycle:aats-liquidations-daemon",
        ),
        (
            "duplicate_collector_archive",
            "invalid_container_archive_lifecycle:aats-liquidations-daemon",
        ),
        (
            "archive_on_non_collector",
            "invalid_container_archive_lifecycle:aats-gateway",
        ),
        (
            "collector_archive_after_boundary",
            "container_lifecycle_changed_after_health_boundary",
        ),
    ),
)
def test_deployment_lifecycle_requires_exact_collector_archive_observation(
    mutation: str,
    expected_error: str,
) -> None:
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["derivatives"]
    monitor_kwargs = _writer_monitor_kwargs(names)
    app_up_authorized_ns = int(monitor_kwargs["app_up_authorized_ns"])
    cutoff_ns = HEALTH_BOUNDARY_NS + 1_000_000_000
    sealer = monitor_kwargs["lifecycle_monitor_sealer"]
    packet = sealer(Path("/tmp/unused"), cutoff_ns=cutoff_ns)
    events = packet["events"]
    collector = "aats-liquidations-daemon"
    collector_id = f"{names.index(collector) + 1:064x}"
    if mutation == "missing_collector_archive":
        packet["events"] = [
            event
            for event in events
            if not (
                event["name"] == collector
                and event["action"] == "archive-path"
            )
        ]
    elif mutation == "duplicate_collector_archive":
        duplicate = next(
            dict(event)
            for event in events
            if event["name"] == collector and event["action"] == "archive-path"
        )
        duplicate["time_nano"] = int(duplicate["time_nano"]) + 1
        packet["events"] = sorted(
            [*events, duplicate],
            key=lambda row: int(row["time_nano"]),
        )
    elif mutation == "archive_on_non_collector":
        packet["events"] = sorted(
            [
                *events,
                {
                    "name": "aats-gateway",
                    "container_id": f"{1:064x}",
                    "action": "archive-path",
                    "time_nano": app_up_authorized_ns + 70_000_000,
                },
            ],
            key=lambda row: int(row["time_nano"]),
        )
    else:
        for event in events:
            if event["name"] == collector and event["action"] == "archive-path":
                event["time_nano"] = HEALTH_BOUNDARY_NS
                event["container_id"] = collector_id
                break
    packet["events"] = sorted(
        packet["events"],
        key=lambda row: int(row["time_nano"]),
    )
    packet["segments"][0]["event_count"] = len(packet["events"])
    required_facts = [
        {"name": name, "container_id": f"{index:064x}"}
        for index, name in enumerate(names, start=1)
    ]

    with pytest.raises(RuntimeError, match=expected_error):
        deployment_evidence._validate_external_lifecycle_capture(
            event_capture=packet,
            post_window_started_ns=HEALTH_BOUNDARY_NS - 2_900_000_000,
            post_window_ended_ns=app_up_authorized_ns - 1,
            app_up_authorized_ns=app_up_authorized_ns,
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            requested_cutoff_ns=cutoff_ns,
            required_container_facts=required_facts,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ("container_id", "container_runtime_changed_during_evidence_capture"),
        ("restart_count", "required_container_restarted_during_deployment"),
        ("safe_environment", "container_environment_mismatch"),
    ),
)
def test_full_deployment_evidence_rejects_second_snapshot_drift(
    tmp_path: Path,
    mutation: str,
    expected_error: str,
) -> None:
    before_path, after_path = _write_preflight_pair(tmp_path)
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    image_id = "sha256:" + "1" * 64
    first = _container_inspect_payload(names, image_id=image_id)
    second_payload = _decode_container_inspect_payload(first)
    if mutation == "container_id":
        second_payload[0]["Id"] = "f" * 64
    elif mutation == "restart_count":
        second_payload[0]["RestartCount"] = 1
    else:
        second_payload[0]["SafeEnvironment"][
            "AATS_RUNTIME_READINESS_GENERATION"
        ] = "other-generation"
    second = _encode_container_inspect_payload(second_payload)
    snapshots = iter((first, second, second))

    def _run(args, _cwd=None) -> str:
        command = tuple(args)
        nats_result = _nats_runtime_inspect_result(command)
        if nats_result is not None:
            return nats_result
        if command == ("git", "rev-parse", "HEAD"):
            return DEPLOYED_COMMIT
        if command[:3] == ("docker", "image", "inspect"):
            return image_id
        if command[:2] == ("docker", "inspect"):
            return next(snapshots)
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match=expected_error):
        deployment_evidence.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=_health_boundary_fingerprint(
                names,
                image_id=image_id,
                inspect_payload=first,
            ),
            collector_heartbeat_epochs={},
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=_run,
        )


def test_full_deployment_evidence_rejects_base_image_tag_drift(
    tmp_path: Path,
) -> None:
    before_path, after_path = _write_preflight_pair(tmp_path)
    names = deployment_evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"]
    image_id = "sha256:" + "1" * 64
    changed_image_id = "sha256:" + "2" * 64
    image_ids = iter((image_id, changed_image_id))

    def _run(args, _cwd=None) -> str:
        command = tuple(args)
        nats_result = _nats_runtime_inspect_result(command)
        if nats_result is not None:
            return nats_result
        if command == ("git", "rev-parse", "HEAD"):
            return DEPLOYED_COMMIT
        if command[:3] == ("docker", "image", "inspect"):
            return next(image_ids)
        if command[:2] == ("docker", "inspect"):
            return _container_inspect_payload(names, image_id=image_id)
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="base_image_changed_during_evidence_capture"):
        deployment_evidence.build_evidence(
            repo_root=tmp_path,
            profile="spot",
            overlay="docker-compose.aats.spot.yml",
            schema_job_status="passed",
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            required_containers=names,
            nats_stream_probe=_matched_nats_stream_rows,
            **_writer_monitor_kwargs(names),
            health_boundary_started_ns=HEALTH_BOUNDARY_NS,
            health_boundary_app_fingerprint=_health_boundary_fingerprint(
                names,
                image_id=image_id,
            ),
            collector_heartbeat_epochs={},
            nats_cutover_preflight_before_path=before_path,
            nats_cutover_preflight_after_path=after_path,
            run=_run,
        )


def _consumer_state(
    *,
    window: int,
    outstanding: int,
    policy: str = "all",
    stream: str = "AATS_EVENTS_COMMANDS",
    ack_policy: str = "explicit",
    filter_subject: str | None = "aats.execution.order_intents",
    filter_subjects: tuple[str, ...] = (),
    deliver_group: str | None = None,
) -> cutover.ConsumerState:
    return cutover.ConsumerState(
        stream=stream,
        durable="aats-execution-execution_order_intents",
        created="2026-08-28T00:00:00Z",
        deliver_policy=policy,
        ack_policy=ack_policy,
        filter_subject=filter_subject,
        filter_subjects=filter_subjects,
        deliver_group=deliver_group,
        max_ack_pending=window,
        num_ack_pending=outstanding,
        cursor=cutover.ConsumerCursor(
            delivered_stream_seq=13,
            delivered_consumer_seq=11,
            ack_floor_stream_seq=10,
            ack_floor_consumer_seq=10,
        ),
    )


def _expected_durable() -> cutover.ExpectedDurable:
    return cutover.ExpectedDurable(
        durable="aats-execution-execution_order_intents",
        role="execution",
        topic="execution.order_intents",
        stream="AATS_EVENTS_COMMANDS",
        filter_subject="aats.execution.order_intents",
    )


def test_nats_cutover_evaluator_requires_drain_before_window_shrink() -> None:
    safe = cutover.evaluate_consumer(
        _consumer_state(window=256, outstanding=0),
        _expected_durable(),
    )
    blocked = cutover.evaluate_consumer(
        _consumer_state(window=256, outstanding=3),
        _expected_durable(),
    )

    assert safe["status"] == "SAFE_TO_SHRINK"
    assert safe["blockers"] == []
    assert blocked["status"] == "BLOCKED"
    assert blocked["blockers"] == ["ack_window_migration_requires_drain"]


def test_nats_cutover_evaluator_rejects_legacy_outstanding_after_shrink() -> None:
    blocked = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=3),
        _expected_durable(),
    )
    one_legal_inflight = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=1),
        _expected_durable(),
    )

    assert blocked["status"] == "BLOCKED"
    assert blocked["blockers"] == ["outstanding_exceeds_target"]
    assert one_legal_inflight["status"] == "SAFE_ALREADY_ONE"
    assert one_legal_inflight["blockers"] == []


def test_nats_cutover_evaluator_rejects_deliver_policy_drift() -> None:
    row = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=0, policy="last"),
        _expected_durable(),
    )

    assert row["status"] == "BLOCKED"
    assert row["blockers"] == ["deliver_policy_drift"]


def test_nats_cutover_evaluator_rejects_immutable_config_drift() -> None:
    cases = (
        (
            _consumer_state(
                window=1,
                outstanding=0,
                stream="WRONG_STREAM",
            ),
            "stream_drift",
        ),
        (
            _consumer_state(
                window=1,
                outstanding=0,
                ack_policy="none",
            ),
            "ack_policy_drift",
        ),
        (
            _consumer_state(
                window=1,
                outstanding=0,
                filter_subject="aats.wrong",
            ),
            "filter_subject_drift",
        ),
        (
            _consumer_state(
                window=1,
                outstanding=0,
                deliver_group="legacy-queue",
            ),
            "deliver_group_drift",
        ),
    )

    for state, expected_blocker in cases:
        row = cutover.evaluate_consumer(state, _expected_durable())
        assert row["status"] == "BLOCKED"
        assert row["blockers"] == [expected_blocker]


def test_nats_cutover_expected_map_uses_exact_declared_runtime_topology() -> None:
    expected = cutover.build_expected_durable_index()

    assert "aats-execution-execution_order_intents" in expected
    assert expected["aats-decision-market_snapshots"].delivery_semantics == "snapshot"
    assert expected["aats-decision-market_snapshots"].deliver_policy == "last"
    assert expected["aats-decision-market_snapshots"].ack_wait_seconds == 90.0
    assert expected["aats-decision-features_snapshots"].ack_wait_seconds == 90.0
    assert expected["aats-execution-execution_order_intents"].ack_wait_seconds == 30.0
    assert expected["aats-decision-system_ai_command_requests"].delivery_semantics == (
        "transient"
    )
    assert expected["aats-decision-system_ai_command_requests"].deliver_policy == "new"
    assert all(item.topic != "system.audit_records" for item in expected.values())
    assert len(expected) == 77
    assert sum(item.delivery_semantics == "event" for item in expected.values()) == 49
    assert sum(item.delivery_semantics == "snapshot" for item in expected.values()) == 24
    assert sum(item.delivery_semantics == "transient" for item in expected.values()) == 4
    assert {item.role for item in expected.values()} == {
        "gateway",
        "market",
        "decision",
        "execution",
    }


def test_nats_cutover_query_paginates_all_streams_and_consumers_read_only() -> None:
    def _info(name: str, *, window: int = 1) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            created=datetime(2026, 8, 28, tzinfo=timezone.utc),
            config=SimpleNamespace(
                deliver_policy=SimpleNamespace(value="all"),
                ack_policy=SimpleNamespace(value="explicit"),
                filter_subject="aats.execution.order_intents",
                filter_subjects=None,
                deliver_group=None,
                max_ack_pending=window,
                deliver_subject="_INBOX.test",
                replay_policy=SimpleNamespace(value="instant"),
                headers_only=None,
                pause_until=None,
                backoff=None,
                rate_limit_bps=None,
                inactive_threshold=None,
                mem_storage=None,
                ack_wait=30.0,
                max_deliver=5,
                durable_name=name,
                opt_start_seq=None,
                opt_start_time=None,
            ),
            num_ack_pending=0,
            delivered=SimpleNamespace(stream_seq=4, consumer_seq=3),
            ack_floor=SimpleNamespace(stream_seq=3, consumer_seq=3),
        )

    def _stream_info(name: str, *, consumer_count: int) -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(
                name=name,
                subjects=[f"aats.{name.lower()}"],
                retention=SimpleNamespace(value="limits"),
                storage=SimpleNamespace(value="file"),
                discard=SimpleNamespace(value="old"),
                max_age=86_400.0,
                max_bytes=1_000_000,
                max_msgs=10_000,
                max_msg_size=1_000,
                num_replicas=1,
                duplicate_window=120.0,
                deny_purge=False,
            ),
            state=SimpleNamespace(
                messages=3,
                bytes=300,
                first_seq=1,
                last_seq=3,
                consumer_count=consumer_count,
                deleted=None,
                num_deleted=None,
                lost=None,
            ),
        )

    class _ReadOnlyJS:
        def __init__(self) -> None:
            self.stream_offsets: list[int] = []
            self.consumer_offsets: list[tuple[str, int]] = []
            self.raw_info_subjects: list[str] = []
            self._prefix = "$JS.API"
            self._timeout = 5.0

        async def streams_info(self, *, offset: int) -> list[SimpleNamespace]:
            self.stream_offsets.append(offset)
            return {
                    0: [_stream_info("S1", consumer_count=2)],
                    1: [_stream_info("S2", consumer_count=1)],
                2: [],
            }[offset]

        async def _api_request(
            self,
            subject: str,
            payload: bytes,
            *,
            timeout: float,
        ) -> dict[str, object]:
            assert payload == b""
            assert timeout == 5.0
            self.raw_info_subjects.append(subject)
            return {"created": "2026-08-28T00:00:00Z"}

        async def consumers_info(
            self,
            stream: str,
            *,
            offset: int,
        ) -> list[SimpleNamespace]:
            self.consumer_offsets.append((stream, offset))
            return {
                ("S1", 0): [_info("d1"), _info("d2")],
                ("S1", 2): [],
                ("S2", 0): [_info("d3")],
                ("S2", 1): [],
            }[(stream, offset)]

    js = _ReadOnlyJS()
    result = asyncio.run(cutover.query_consumer_states_from_js(js))

    assert js.stream_offsets == [0, 1, 2]
    assert js.consumer_offsets == [
        ("S1", 0),
        ("S1", 2),
        ("S2", 0),
        ("S2", 1),
    ]
    assert js.raw_info_subjects == [
        "$JS.API.STREAM.INFO.S1",
        "$JS.API.STREAM.INFO.S2",
    ]
    assert result.stream_count == 2
    assert result.consumer_count == 3
    assert [consumer.durable for consumer in result.consumers] == [
        "d1",
        "d2",
        "d3",
    ]
    assert [stream.created for stream in result.streams] == [
        "2026-08-28T00:00:00Z",
        "2026-08-28T00:00:00Z",
    ]


def test_nats_consumer_projection_rejects_broker_enumeration_mismatch() -> None:
    matched = _matched_nats_stream_rows()
    drifted_streams = list(matched.streams)
    drifted_streams[0] = replace(
        drifted_streams[0],
        consumer_count=drifted_streams[0].consumer_count + 1,
    )
    mismatched = replace(matched, streams=tuple(drifted_streams))

    with pytest.raises(
        RuntimeError,
        match="nats_consumer_projection_stream_count_mismatch",
    ):
        cutover.validate_query_result_consumer_projection(mismatched)
    with pytest.raises(RuntimeError, match="invalid_final_nats_query_result"):
        deployment_evidence._read_final_nats_state(lambda: mismatched)


def test_nats_cutover_stream_created_uses_raw_api_with_real_nats_type() -> None:
    from nats.js.api import (
        DiscardPolicy,
        RetentionPolicy,
        StorageType,
        StreamConfig,
        StreamInfo,
        StreamState,
    )

    info = StreamInfo(
        config=StreamConfig(
            name="AATS_EVENTS_COMMANDS",
            subjects=["aats.execution.order_intents"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            discard=DiscardPolicy.OLD,
            max_age=86_400.0,
            max_bytes=1_000_000,
            max_msgs=10_000,
            max_msg_size=1_000,
            num_replicas=1,
            duplicate_window=120.0,
            deny_purge=False,
        ),
        state=StreamState(
            messages=5,
            bytes=500,
            first_seq=1,
            last_seq=5,
            consumer_count=1,
        ),
    )

    state = cutover._stream_state(
        info,
        created=cutover._raw_broker_created_text("2026-08-28T00:00:00Z"),
    )

    assert not hasattr(info, "created")
    assert state.created == "2026-08-28T00:00:00Z"
    with pytest.raises(RuntimeError, match="malformed_created_timestamp"):
        cutover._raw_broker_created_text("2026-08-28T00:00:00")


def test_nats_cutover_continuity_rejects_stream_purge_and_cursor_rollback() -> None:
    durable = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=0),
        _expected_durable(),
    )
    purged_stream = _critical_stream_row(
        messages=0,
        byte_count=0,
        first_seq=6,
        last_seq=5,
    )
    previous_stream = _critical_stream_row()
    # A purge on a stream with max-age expiry disabled cannot qualify for the
    # bounded passive-retention trust boundary.
    previous_stream["immutable_config"]["max_age_seconds"] = 0.0
    purged_stream["immutable_config"]["max_age_seconds"] = 0.0
    rolled_back_durable = json.loads(json.dumps(durable))
    rolled_back_durable["cursor"]["ack_floor_stream_seq"] = 9

    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[previous_stream],
        current_streams=[purged_stream],
        previous_durables=[durable],
        current_durables=[rolled_back_durable],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity["status"] == "INVALIDATED"
    assert "stream_state_changed:AATS_EVENTS_COMMANDS:messages" in continuity[
        "violations"
    ]
    assert "stream_state_changed:AATS_EVENTS_COMMANDS:bytes" in continuity[
        "violations"
    ]
    assert any(
        str(item).startswith("durable_cursor_changed:")
        for item in continuity["violations"]
    )


def test_nats_cutover_continuity_rejects_purge_then_repopulation() -> None:
    previous = _critical_stream_row(
        messages=5,
        byte_count=500,
        first_seq=1,
        last_seq=5,
    )
    repopulated = _critical_stream_row(
        messages=6,
        byte_count=600,
        first_seq=6,
        last_seq=11,
    )

    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[previous],
        current_streams=[repopulated],
        previous_durables=[],
        current_durables=[],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity["status"] == "INVALIDATED"
    assert "stream_state_changed:AATS_EVENTS_COMMANDS:first_seq" in continuity[
        "violations"
    ]


def test_nats_cutover_continuity_rejects_unowned_publish_and_ack_progress() -> None:
    durable = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=0),
        _expected_durable(),
    )
    advanced_durable = json.loads(json.dumps(durable))
    advanced_durable["cursor"]["delivered_stream_seq"] += 1
    advanced_stream = _critical_stream_row(
        messages=6,
        byte_count=600,
        first_seq=1,
        last_seq=6,
    )

    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[_critical_stream_row()],
        current_streams=[advanced_stream],
        previous_durables=[durable],
        current_durables=[advanced_durable],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity["status"] == "INVALIDATED"
    assert "stream_state_changed:AATS_EVENTS_COMMANDS:messages" in continuity[
        "violations"
    ]
    assert any(
        str(item).startswith("durable_cursor_changed:")
        for item in continuity["violations"]
    )


def test_nats_cutover_continuity_accepts_identical_baseline() -> None:
    durable = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=0),
        _expected_durable(),
    )
    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[_critical_stream_row()],
        current_streams=[_critical_stream_row()],
        previous_durables=[durable],
        current_durables=[durable],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity == {
        "status": "PASSED",
        "complete": True,
        "baseline_sha256": "sha256:" + "1" * 64,
        "streams_checked": 1,
        "durables_checked": 1,
        "passive_retention_trims": [],
        "violations": [],
    }


def test_nats_cutover_continuity_accepts_exact_passive_retention_trim() -> None:
    durable = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=0),
        _expected_durable(),
    )
    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[_critical_stream_row()],
        current_streams=[
            _critical_stream_row(
                messages=3,
                byte_count=300,
                first_seq=3,
                last_seq=5,
            )
        ],
        previous_durables=[durable],
        current_durables=[durable],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity["status"] == "PASSED"
    assert continuity["violations"] == []
    assert continuity["passive_retention_trims"] == [
        {
            "stream": "AATS_EVENTS_COMMANDS",
            "delta": 2,
            "messages_removed": 2,
            "bytes_removed": 200,
            "first_seq_advanced": 2,
            "trust_boundary": "purge_vs_expiry_not_distinguishable",
        }
    ]


@pytest.mark.parametrize(
    "mutation",
    (
        "delta_mismatch",
        "bytes_unchanged",
        "bytes_increase",
        "non_limits",
        "max_age_zero",
        "discard_new",
        "deleted_nonempty",
    ),
)
def test_nats_cutover_continuity_rejects_inexact_or_ineligible_head_trim(
    mutation: str,
) -> None:
    previous = _critical_stream_row()
    current = _critical_stream_row(
        messages=3,
        byte_count=300,
        first_seq=3,
        last_seq=5,
    )
    if mutation == "delta_mismatch":
        current["state"]["first_seq"] = 2
    elif mutation == "bytes_unchanged":
        current["state"]["bytes"] = 500
    elif mutation == "bytes_increase":
        current["state"]["bytes"] = 501
    elif mutation == "non_limits":
        previous["immutable_config"]["retention"] = "interest"
        current["immutable_config"]["retention"] = "interest"
    elif mutation == "max_age_zero":
        previous["immutable_config"]["max_age_seconds"] = 0.0
        current["immutable_config"]["max_age_seconds"] = 0.0
    elif mutation == "discard_new":
        previous["immutable_config"]["discard"] = "new"
        current["immutable_config"]["discard"] = "new"
    else:
        previous["state"]["deleted"] = [2]
        previous["state"]["num_deleted"] = 1
        current["state"]["deleted"] = [2]
        current["state"]["num_deleted"] = 1

    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[previous],
        current_streams=[current],
        previous_durables=[],
        current_durables=[],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity["status"] == "INVALIDATED"
    assert continuity["passive_retention_trims"] == []


def test_nats_cutover_continuity_rejects_unprojected_durable_row_drift() -> None:
    durable = cutover.evaluate_consumer(
        _consumer_state(window=1, outstanding=0),
        _expected_durable(),
    )
    drifted = json.loads(json.dumps(durable))
    drifted["unprojected_state"] = "changed"

    continuity = cutover.evaluate_cutover_continuity(
        previous_streams=[_critical_stream_row()],
        current_streams=[_critical_stream_row()],
        previous_durables=[durable],
        current_durables=[drifted],
        baseline_sha256="sha256:" + "1" * 64,
    )

    assert continuity["status"] == "INVALIDATED"
    assert any(
        str(item).startswith("durable_row_changed:")
        for item in continuity["violations"]
    )


def test_nats_cutover_profile_target_manifest_applies_only_allowlisted_overrides(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.derivatives"
    env_file.write_text(
        "POSTGRES_PASSWORD=must-never-enter-evidence\n"
        "AATS_NATS_MARKET_MAX_BYTES=123456789\n"
        'AATS_NATS_EVENTS_MAX_AGE_SECONDS="43200"\n',
        encoding="utf-8",
    )

    manifest = cutover.load_target_stream_manifest(env_file)
    encoded = json.dumps(manifest)
    by_name = {
        row["identity"]["name"]: row["immutable_config"]
        for row in manifest["streams"]
    }

    assert by_name["AATS_EVENTS_MARKET"]["max_bytes"] == 123456789
    assert by_name["AATS_EVENTS"]["max_age_seconds"] == 43200.0
    assert "must-never-enter-evidence" not in encoded
    assert "POSTGRES_PASSWORD" not in encoded


def test_nats_cutover_blocks_existing_stream_config_drift_before_app_up() -> None:
    target = cutover._default_target_stream_manifest()
    actual = json.loads(json.dumps(target["streams"]))
    actual[0]["immutable_config"]["max_bytes"] = 1
    actual[0]["immutable_config"]["subjects"] = ["aats.wrong.subject"]

    compliance, blocked = cutover.evaluate_stream_target(
        actual_streams=actual,
        target_manifest=target,
        bootstrap_mode="existing_container_preserved",
    )

    assert blocked is True
    assert compliance["status"] == "BLOCKED"
    assert compliance["unexpected_names"] == []
    assert compliance["drift"] == [
        {
            "name": "AATS_EVENTS",
            "fields": ["max_bytes", "subjects"],
        }
    ]


def test_nats_cutover_allows_only_missing_target_streams_to_be_provisioned() -> None:
    target = cutover._default_target_stream_manifest()

    compliance, blocked = cutover.evaluate_stream_target(
        actual_streams=(),
        target_manifest=target,
        bootstrap_mode="existing_container_preserved",
    )

    assert blocked is False
    assert compliance["status"] == "PROVISIONING_REQUIRED"
    assert compliance["actual_names"] == []


def test_nats_cutover_final_phase_accepts_exact_target_after_fresh_bootstrap() -> None:
    target = cutover._default_target_stream_manifest()

    compliance, blocked = cutover.evaluate_stream_target(
        actual_streams=json.loads(json.dumps(target["streams"])),
        target_manifest=target,
        bootstrap_mode="proven_fresh_install",
        require_fresh_empty=False,
    )

    assert blocked is False
    assert compliance["status"] == "MATCHED"


def test_nats_cutover_pre_app_phase_rejects_streams_after_fresh_bootstrap() -> None:
    target = cutover._default_target_stream_manifest()

    compliance, blocked = cutover.evaluate_stream_target(
        actual_streams=json.loads(json.dumps(target["streams"])),
        target_manifest=target,
        bootstrap_mode="proven_fresh_install",
    )

    assert blocked is True
    assert compliance["status"] == "BLOCKED"


def test_nats_cutover_rejects_unknown_or_malformed_drain_counters() -> None:
    def _info(
        *,
        name: object = "aats-execution-execution_order_intents",
        max_ack_pending: object = 1,
        num_ack_pending: object = 0,
        delivered_stream_seq: object = 1,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            created=datetime(2026, 8, 28, tzinfo=timezone.utc),
            config=SimpleNamespace(
                deliver_policy=SimpleNamespace(value="all"),
                ack_policy=SimpleNamespace(value="explicit"),
                filter_subject="aats.execution.order_intents",
                filter_subjects=None,
                deliver_group=None,
                max_ack_pending=max_ack_pending,
                deliver_subject="_INBOX.test",
                replay_policy=SimpleNamespace(value="instant"),
                headers_only=None,
                pause_until=None,
                backoff=None,
                rate_limit_bps=None,
                inactive_threshold=None,
                mem_storage=None,
                ack_wait=30.0,
                max_deliver=5,
                durable_name=name,
                opt_start_seq=None,
                opt_start_time=None,
            ),
            num_ack_pending=num_ack_pending,
            delivered=SimpleNamespace(
                stream_seq=delivered_stream_seq,
                consumer_seq=1,
            ),
            ack_floor=SimpleNamespace(stream_seq=0, consumer_seq=0),
        )

    malformed = (
        _info(max_ack_pending=None),
        _info(num_ack_pending=None),
        _info(num_ack_pending="0"),
        _info(num_ack_pending=-1),
        _info(delivered_stream_seq=None),
        _info(name=None),
        _info(name=7),
        _info(name=""),
        _info(name="   "),
    )
    for info in malformed:
        try:
            cutover._consumer_state("AATS_EVENTS_COMMANDS", info)
        except RuntimeError as exc:
            assert str(exc) == "nats_cutover_malformed_consumer_state"
        else:
            raise AssertionError("UNKNOWN broker state must fail closed")

    for stream in (None, 7, "", "   "):
        try:
            cutover._consumer_state(stream, _info())  # type: ignore[arg-type]
        except RuntimeError as exc:
            assert str(exc) == "nats_cutover_malformed_consumer_state"
        else:
            raise AssertionError("UNKNOWN stream identity must fail closed")


def test_nats_cutover_quiescence_captures_full_lifecycle_fingerprint() -> None:
    container_name = "aats-execution"
    payload = {
        "Name": f"/{container_name}",
        "Id": "b" * 64,
        "RestartCount": 7,
        "Status": "exited",
        "StartedAt": "2026-08-28T00:00:00Z",
        "FinishedAt": "2026-08-28T00:01:00Z",
        "ComposeProject": "aats-dev",
        "ComposeService": container_name,
    }

    def _run(args) -> str:
        if tuple(args) == ("docker", "ps", "-a", "--format", "{{.Names}}"):
            return container_name
        assert tuple(args) == (
            "docker",
            "inspect",
            "--format",
            cutover._APP_QUIESCENCE_INSPECT_TEMPLATE,
            container_name,
        )
        return json.dumps(payload)

    snapshot = cutover.capture_app_quiescence(_run)
    execution = next(item for item in snapshot if item["name"] == container_name)

    assert execution == {
        "name": container_name,
        "existence": "present",
        "container_id": "b" * 64,
        "status": "exited",
        "started_at": "2026-08-28T00:00:00Z",
        "finished_at": "2026-08-28T00:01:00Z",
        "restart_count": 7,
    }
    assert len(snapshot) == len(cutover._KNOWN_APP_CONTAINERS)


def test_nats_cutover_quiescence_event_bracket_invalidates_transient_restart() -> None:
    event = {
        "Type": "container",
        "Action": "start",
        "Actor": {
            "ID": "c" * 64,
            "Attributes": {"name": "aats-execution"},
        },
        "timeNano": 1_700_000_000_500_000_000,
    }
    events = cutover.query_app_lifecycle_events(
        since_ns=1_700_000_000_000_000_000,
        until_ns=1_700_000_001_000_000_000,
        run=lambda _args: json.dumps(event),
    )
    snapshot = _app_quiescence_snapshot()
    evidence = cutover.build_app_quiescence_evidence(
        since_ns=1_700_000_000_000_000_000,
        until_ns=1_700_000_001_000_000_000,
        before=snapshot,
        after=snapshot,
        events=events,
        event_capture=_live_event_capture(
            1_700_000_000_000_000_000,
            1_700_000_001_000_000_000,
            events=events,
        ),
    )

    assert evidence["fingerprint_match"] is True
    assert evidence["status"] == "INVALIDATED"
    assert evidence["lifecycle_events"] == [
        {
            "name": "aats-execution",
            "container_id": "c" * 64,
            "action": "start",
            "time_nano": 1_700_000_000_500_000_000,
        }
    ]


def test_nats_cutover_evidence_is_read_only_and_omits_endpoint() -> None:
    query = cutover.QueryResult(
        stream_count=1,
        consumer_count=1,
        consumers=(_consumer_state(window=256, outstanding=0),),
    )
    rows, blocked = cutover.evaluate_existing_consumers(
        query.consumers,
        {_expected_durable().durable: _expected_durable()},
    )
    evidence = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=query,
        rows=rows,
        status="BLOCKED" if blocked else "PASSED",
        app_quiescence=_passed_app_quiescence(
            datetime(2026, 8, 28, 0, 1, tzinfo=timezone.utc)
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
    )
    encoded = json.dumps(evidence)

    assert evidence["operation"] == "READ_ONLY"
    assert evidence["mutations_performed"] == []
    assert evidence["generation"] == "generation-1"
    assert "nats://" not in encoded
    assert "127.0.0.1" not in encoded


def test_nats_cutover_block_writes_evidence_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _blocked_query() -> cutover.QueryResult:
        state = _consumer_state(window=256, outstanding=2)
        return cutover.QueryResult(
            stream_count=1,
            consumer_count=1,
            consumers=(state,),
        )

    monkeypatch.setattr(cutover, "query_loopback_nats", _blocked_query)
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", tmp_path / "deployments")
    _patch_app_quiescence(monkeypatch)

    assert cutover.main(_cutover_cli_args()) == 2
    evidence_paths = list((tmp_path / "deployments").glob("*.json"))
    assert len(evidence_paths) == 1
    evidence = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    assert evidence["status"] == "BLOCKED"
    assert evidence["durables"][0]["blockers"] == [
        "ack_window_migration_requires_drain"
    ]
    assert evidence["recovery"]["instruction_code"] == (
        "nats_durable_cutover_requires_approved_all_cursor_drain"
    )


def test_nats_cutover_query_failure_writes_sanitized_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _failed_query() -> cutover.QueryResult:
        raise RuntimeError("nats://operator:secret@127.0.0.1:4222")

    monkeypatch.setattr(cutover, "query_loopback_nats", _failed_query)
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", tmp_path / "deployments")
    _patch_app_quiescence(monkeypatch)

    assert cutover.main(_cutover_cli_args()) == 3
    evidence_paths = list((tmp_path / "deployments").glob("*.json"))
    assert len(evidence_paths) == 1
    evidence_text = evidence_paths[0].read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    assert evidence["status"] == "QUERY_FAILED"
    assert evidence["error_code"] == (
        "nats_durable_cutover_preflight_query_failed"
    )
    assert "secret" not in evidence_text
    assert "nats://" not in evidence_text


def test_nats_cutover_lifecycle_event_invalidates_pass_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _safe_query() -> cutover.QueryResult:
        return cutover.QueryResult(
            stream_count=0,
            consumer_count=0,
            consumers=(),
        )

    monkeypatch.setattr(cutover, "query_loopback_nats", _safe_query)
    _patch_app_quiescence(monkeypatch)
    monkeypatch.setattr(
        cutover,
        "query_app_lifecycle_events",
        lambda *, since_ns, **_kwargs: (
            {
                "name": "aats-market",
                "container_id": "d" * 64,
                "action": "start",
                "time_nano": since_ns + 1,
            },
        ),
    )
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", tmp_path / "deployments")

    assert cutover.main(_cutover_cli_args()) == 5
    evidence_path = next((tmp_path / "deployments").glob("*.json"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "INVALIDATED"
    assert evidence["app_quiescence"]["status"] == "INVALIDATED"
    assert evidence["error_code"] == (
        "nats_durable_cutover_app_quiescence_invalidated"
    )


def test_nats_cutover_baseline_cli_requires_complete_bootstrap_provenance() -> None:
    base = [
        "--generation",
        "generation-1",
        "--deployment-lock-id",
        DEPLOYMENT_LOCK_ID,
        "--deployed-commit",
        DEPLOYED_COMMIT,
        "--target-env-file",
        str(TARGET_ENV_FILE),
        "--stage",
        "pre_full_down",
    ]
    with pytest.raises(SystemExit):
        cutover._parse_args(base)
    with pytest.raises(SystemExit):
        cutover._parse_args(
            [*base, "--nats-bootstrap-mode", "existing_container_preserved"]
        )

    post = [
        *base[:-1],
        "post_infra_pre_app_up",
        "--previous-preflight",
        "before.json",
        "--nats-bootstrap-mode",
        "existing_container_preserved",
        "--nats-baseline-fingerprint",
        NATS_BASELINE_FINGERPRINT,
    ]
    with pytest.raises(SystemExit):
        cutover._parse_args(post)


def test_nats_cutover_post_blocks_previous_window_overlap_before_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence_dir = tmp_path / "deployments"
    evidence_dir.mkdir()
    before_path = evidence_dir / "before.json"
    now = datetime.now(timezone.utc)
    checked_at = now - timedelta(minutes=1)
    window_started_ns = int((now - timedelta(seconds=30)).timestamp() * 1e9)
    window_ended_ns = int((now + timedelta(minutes=1)).timestamp() * 1e9)
    snapshot = _app_quiescence_snapshot()
    before_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=checked_at,
        query_result=cutover.QueryResult(len(_critical_stream_rows()), 0, (), ()),
        rows=(),
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=cutover.build_app_quiescence_evidence(
            since_ns=window_started_ns,
            until_ns=window_ended_ns,
            before=snapshot,
            after=snapshot,
            events=(),
            event_capture=_live_event_capture(
                window_started_ns,
                window_ended_ns,
            ),
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        continuity=_baseline_continuity(),
        critical_streams=_critical_stream_rows(),
    )
    before_path.write_text(json.dumps(before_payload), encoding="utf-8")
    query_called = False

    async def _must_not_query() -> cutover.QueryResult:
        nonlocal query_called
        query_called = True
        return cutover.QueryResult(0, 0, (), ())

    monkeypatch.setattr(cutover, "query_loopback_nats", _must_not_query)
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", evidence_dir)

    assert cutover.main(
        [
            "--generation",
            "generation-1",
            "--deployment-lock-id",
            DEPLOYMENT_LOCK_ID,
            "--deployed-commit",
            DEPLOYED_COMMIT,
            "--target-env-file",
            str(TARGET_ENV_FILE),
            "--stage",
            "post_infra_pre_app_up",
            "--previous-preflight",
            str(before_path),
        ]
    ) == 3
    assert query_called is False


def test_nats_cutover_rejects_local_clock_rollback_before_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _safe_query() -> cutover.QueryResult:
        return cutover.QueryResult(0, 0, (), ())

    now = datetime.now(timezone.utc)
    times = iter(
        (
            int((now - timedelta(seconds=10)).timestamp() * 1e9),
            int((now - timedelta(seconds=9)).timestamp() * 1e9),
        )
    )
    monkeypatch.setattr(cutover.time, "time_ns", lambda: next(times))
    monkeypatch.setattr(cutover, "query_loopback_nats", _safe_query)
    _patch_app_quiescence(monkeypatch)
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", tmp_path / "deployments")

    assert cutover.main(_cutover_cli_args()) == 3
    evidence_path = next((tmp_path / "deployments").glob("*.json"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "QUERY_FAILED"


def test_nats_cutover_rejects_nats_identity_drift_inside_query_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _safe_query() -> cutover.QueryResult:
        return cutover.QueryResult(0, 0, (), ())

    fingerprints = iter(
        (NATS_BASELINE_FINGERPRINT, "sha256:" + "c" * 64)
    )
    monkeypatch.setattr(cutover, "query_loopback_nats", _safe_query)
    monkeypatch.setattr(
        cutover,
        "capture_nats_identity",
        lambda: _nats_identity(next(fingerprints)),
    )
    monkeypatch.setattr(
        cutover,
        "capture_nats_volume_fingerprint",
        lambda: NATS_VOLUME_FINGERPRINT,
    )
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", tmp_path / "deployments")
    monkeypatch.setattr(cutover, "capture_app_quiescence", _app_quiescence_snapshot)
    monkeypatch.setattr(
        cutover,
        "query_app_lifecycle_events",
        lambda **_kwargs: (),
    )

    assert cutover.main(_cutover_cli_args()) == 3
    evidence_path = next((tmp_path / "deployments").glob("*.json"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "QUERY_FAILED"
    assert evidence["nats_bootstrap"] == NATS_BOOTSTRAP


def test_nats_cutover_post_recreate_accepts_new_stable_nats_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence_dir = tmp_path / "deployments"
    evidence_dir.mkdir()
    before_path = evidence_dir / "before.json"
    qualified_query, qualified_durables, qualified_streams = (
        _qualified_preflight_snapshot()
    )
    before_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=qualified_query,
        rows=qualified_durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        critical_streams=qualified_streams,
        continuity={
            **_baseline_continuity(),
            "streams_checked": len(qualified_streams),
            "durables_checked": len(qualified_durables),
        },
    )
    before_path.write_text(
        json.dumps(before_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    async def _safe_query() -> cutover.QueryResult:
        return qualified_query

    monkeypatch.setattr(cutover, "query_loopback_nats", _safe_query)
    _patch_app_quiescence(monkeypatch)
    monkeypatch.setattr(
        cutover,
        "capture_nats_identity",
        lambda: _nats_identity(NATS_POST_RECREATE_FINGERPRINT),
    )
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", evidence_dir)

    args = [
        "--generation",
        "generation-1",
        "--deployment-lock-id",
        DEPLOYMENT_LOCK_ID,
        "--deployed-commit",
        DEPLOYED_COMMIT,
        "--target-env-file",
        str(TARGET_ENV_FILE),
        "--stage",
        "post_infra_pre_app_up",
        "--previous-preflight",
        str(before_path),
    ]
    assert cutover.main(args) == 0
    after_path = next(path for path in evidence_dir.glob("*.json") if path != before_path)
    after = json.loads(after_path.read_text(encoding="utf-8"))
    assert after["nats_bootstrap"] == NATS_BOOTSTRAP
    assert after["nats_query_fingerprint"] == NATS_POST_RECREATE_FINGERPRINT
    assert after["continuity"]["status"] == "PASSED"


def test_nats_cutover_post_stage_revalidates_previous_snapshot_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evidence_dir = tmp_path / "deployments"
    evidence_dir.mkdir()
    before_path = evidence_dir / "before.json"
    query, durables, streams = _qualified_preflight_snapshot()
    payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=query,
        rows=durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        critical_streams=streams,
        continuity={
            **_baseline_continuity(),
            "streams_checked": len(streams),
            "durables_checked": len(durables),
        },
    )
    payload["query"]["consumers_scanned"] += 1
    before_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", evidence_dir)

    with pytest.raises(
        RuntimeError,
        match="nats_cutover_malformed_previous_preflight",
    ):
        cutover.load_previous_preflight(
            before_path,
            generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
        )


@pytest.mark.parametrize(
    ("post_fingerprint", "restart_count"),
    (
        (NATS_BASELINE_FINGERPRINT, 0),
        (NATS_POST_RECREATE_FINGERPRINT, 7),
    ),
)
def test_nats_cutover_post_recreate_rejects_missing_recreate_or_restart_history(
    tmp_path: Path,
    monkeypatch,
    post_fingerprint: str,
    restart_count: int,
) -> None:
    evidence_dir = tmp_path / "deployments"
    evidence_dir.mkdir()
    before_path = evidence_dir / "before.json"
    before_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=cutover.QueryResult(0, 0, (), ()),
        rows=(),
        status="PASSED",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        continuity=_baseline_continuity(),
    )
    before_path.write_text(json.dumps(before_payload), encoding="utf-8")

    async def _safe_query() -> cutover.QueryResult:
        return cutover.QueryResult(0, 0, (), ())

    monkeypatch.setattr(cutover, "query_loopback_nats", _safe_query)
    _patch_app_quiescence(monkeypatch)
    monkeypatch.setattr(
        cutover,
        "capture_nats_identity",
        lambda: _nats_identity(post_fingerprint, restart_count=restart_count),
    )
    monkeypatch.setattr(cutover, "_EVIDENCE_DIR", evidence_dir)

    assert cutover.main(
        [
            "--generation",
            "generation-1",
            "--deployment-lock-id",
            DEPLOYMENT_LOCK_ID,
            "--deployed-commit",
            DEPLOYED_COMMIT,
            "--target-env-file",
            str(TARGET_ENV_FILE),
            "--stage",
            "post_infra_pre_app_up",
            "--previous-preflight",
            str(before_path),
        ]
    ) == 3
    after_path = next(
        path for path in evidence_dir.glob("*.json") if path != before_path
    )
    after = json.loads(after_path.read_text(encoding="utf-8"))
    assert after["status"] == "QUERY_FAILED"


def test_final_deployment_evidence_links_verified_cutover_path_and_hash(
    tmp_path: Path,
) -> None:
    preflight_dir = tmp_path / "artifacts" / "deployments"
    preflight_dir.mkdir(parents=True)
    preflight_path = preflight_dir / "preflight.json"
    qualified_query, qualified_durables, qualified_streams = (
        _qualified_preflight_snapshot()
    )
    preflight_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=qualified_query,
        rows=qualified_durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        critical_streams=qualified_streams,
        continuity={
            **_baseline_continuity(),
            "streams_checked": len(qualified_streams),
            "durables_checked": len(qualified_durables),
        },
    )
    encoded = (
        json.dumps(preflight_payload, sort_keys=True, indent=2) + "\n"
    ).encode()
    preflight_path.write_bytes(encoded)

    reference, _ = deployment_evidence._nats_cutover_preflight_reference(
        repo_root=tmp_path,
        path=Path("artifacts/deployments/preflight.json"),
        runtime_readiness_generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        expected_stage="pre_full_down",
    )

    assert reference["path"] == "artifacts/deployments/preflight.json"
    assert reference["sha256"] == (
        f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    )
    assert reference["status"] == "PASSED_WITH_TRUST_BOUNDARY"
    assert reference["streams_scanned"] == qualified_query.stream_count
    assert reference["consumers_scanned"] == qualified_query.consumer_count


def test_final_deployment_evidence_validates_two_preflight_hash_chain(
    tmp_path: Path,
) -> None:
    preflight_dir = tmp_path / "artifacts" / "deployments"
    preflight_dir.mkdir(parents=True)
    before_path = preflight_dir / "before.json"
    after_path = preflight_dir / "after.json"
    qualified_query, qualified_durables, qualified_streams = (
        _qualified_preflight_snapshot()
    )
    before_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=qualified_query,
        rows=qualified_durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        critical_streams=qualified_streams,
        continuity={
            **_baseline_continuity(),
            "streams_checked": len(qualified_streams),
            "durables_checked": len(qualified_durables),
        },
    )
    before_path.write_text(
        json.dumps(before_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    before_reference, loaded_before = (
        deployment_evidence._nats_cutover_preflight_reference(
            repo_root=tmp_path,
            path=before_path,
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            expected_stage="pre_full_down",
        )
    )
    after_payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, 0, 1, tzinfo=timezone.utc),
        query_result=qualified_query,
        rows=qualified_durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(
            datetime(2026, 8, 28, 0, 1, tzinfo=timezone.utc)
        ),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_POST_RECREATE_FINGERPRINT,
        stage="post_infra_pre_app_up",
        critical_streams=qualified_streams,
        previous_preflight=before_reference,
        continuity=cutover.evaluate_cutover_continuity(
            previous_streams=qualified_streams,
            current_streams=qualified_streams,
            previous_durables=qualified_durables,
            current_durables=qualified_durables,
            baseline_sha256=before_reference["sha256"],
        ),
    )
    after_path.write_text(
        json.dumps(after_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _, loaded_after = deployment_evidence._nats_cutover_preflight_reference(
        repo_root=tmp_path,
        path=after_path,
        runtime_readiness_generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        expected_stage="post_infra_pre_app_up",
    )

    deployment_evidence._validate_nats_cutover_preflight_chain(
        before_reference=before_reference,
        before_payload=loaded_before,
        after_payload=loaded_after,
    )

    bootstrap_drift = json.loads(json.dumps(loaded_after))
    bootstrap_drift["nats_bootstrap"] = {
        "mode": "proven_fresh_install",
        "baseline_fingerprint": NATS_BASELINE_FINGERPRINT,
    }
    with pytest.raises(ValueError, match="bootstrap_provenance_mismatch"):
        deployment_evidence._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=loaded_before,
            after_payload=bootstrap_drift,
        )

    timestamp_rollback = json.loads(json.dumps(loaded_after))
    timestamp_rollback["checked_at_utc"] = "2026-08-27T23:59:59Z"
    with pytest.raises(ValueError, match="preflight_time_rollback"):
        deployment_evidence._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=loaded_before,
            after_payload=timestamp_rollback,
        )

    overlapping_windows = json.loads(json.dumps(loaded_after))
    overlapping_windows["app_quiescence"]["window_started_ns"] = (
        before_reference["window_ended_ns"] - 1
    )
    with pytest.raises(ValueError, match="preflight_time_window_overlap"):
        deployment_evidence._validate_nats_cutover_preflight_chain(
            before_reference=before_reference,
            before_payload=loaded_before,
            after_payload=overlapping_windows,
        )

    before_path.write_text(
        before_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    changed_reference, changed_before = (
        deployment_evidence._nats_cutover_preflight_reference(
            repo_root=tmp_path,
            path=before_path,
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
            deployed_commit=DEPLOYED_COMMIT,
            expected_stage="pre_full_down",
        )
    )
    with pytest.raises(ValueError, match="previous_preflight_mismatch:sha256"):
        deployment_evidence._validate_nats_cutover_preflight_chain(
            before_reference=changed_reference,
            before_payload=changed_before,
            after_payload=loaded_after,
        )


def test_final_deployment_evidence_rejects_blocked_cutover(
    tmp_path: Path,
) -> None:
    preflight_dir = tmp_path / "artifacts" / "deployments"
    preflight_dir.mkdir(parents=True)
    preflight_path = preflight_dir / "preflight.json"
    payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=cutover.QueryResult(
            stream_count=1,
            consumer_count=1,
            consumers=(),
        ),
        rows=(),
        status="BLOCKED",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        continuity=_baseline_continuity(),
    )
    preflight_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        deployment_evidence._nats_cutover_preflight_reference(
            repo_root=tmp_path,
            path=preflight_path,
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
                deployed_commit=DEPLOYED_COMMIT,
                expected_stage="pre_full_down",
        )
    except ValueError as exc:
        assert str(exc) == "nats_cutover_preflight_not_passed"
    else:
        raise AssertionError("blocked preflight must not enter final evidence")


def test_final_deployment_evidence_rejects_unbound_or_invalid_quiescence(
    tmp_path: Path,
) -> None:
    preflight_dir = tmp_path / "artifacts" / "deployments"
    preflight_dir.mkdir(parents=True)
    preflight_path = preflight_dir / "preflight.json"
    qualified_query, qualified_durables, qualified_streams = (
        _qualified_preflight_snapshot()
    )
    payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id=DEPLOYMENT_LOCK_ID,
        deployed_commit=DEPLOYED_COMMIT,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=qualified_query,
        rows=qualified_durables,
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=_passed_app_quiescence(),
        nats_bootstrap=NATS_BOOTSTRAP,
        nats_query_fingerprint=NATS_BASELINE_FINGERPRINT,
        critical_streams=qualified_streams,
        continuity={
            **_baseline_continuity(),
            "streams_checked": len(qualified_streams),
            "durables_checked": len(qualified_durables),
        },
    )
    preflight_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        deployment_evidence._nats_cutover_preflight_reference(
            repo_root=tmp_path,
            path=preflight_path,
            runtime_readiness_generation="generation-1",
            deployment_lock_id="different-lock",
                deployed_commit=DEPLOYED_COMMIT,
                expected_stage="pre_full_down",
        )
    except ValueError as exc:
        assert str(exc) == "nats_cutover_preflight_lock_mismatch"
    else:
        raise AssertionError("preflight from another lock holder must be rejected")

    payload["app_quiescence"]["lifecycle_events"] = [  # type: ignore[index]
        {"action": "start"}
    ]
    preflight_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        deployment_evidence._nats_cutover_preflight_reference(
            repo_root=tmp_path,
            path=preflight_path,
            runtime_readiness_generation="generation-1",
            deployment_lock_id=DEPLOYMENT_LOCK_ID,
                deployed_commit=DEPLOYED_COMMIT,
                expected_stage="pre_full_down",
        )
    except ValueError as exc:
        assert str(exc) == "nats_cutover_preflight_quiescence_not_passed"
    else:
        raise AssertionError("lifecycle drift must not enter final deployment evidence")


def test_nats_cutover_script_contains_no_jetstream_mutation_calls() -> None:
    text = (
        REPO_ROOT / "scripts" / "check_nats_durable_cutover.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        ".ack(",
        ".add_consumer(",
        ".delete_consumer(",
        ".purge_stream(",
        ".update_stream(",
    ):
        assert forbidden not in text


def test_deploy_script_health_check_covers_current_topology() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'echo "aats-gateway aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"' in text
    assert 'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon"' in text
    assert 'echo "aats-gateway aats-market aats-decision aats-execution aats-rdp-daemon aats-liquidations-daemon aats-microstructure-collector"' in text
    assert "all_required_app_containers_healthy" in text


def test_deploy_health_boundary_is_fixed_after_collector_observation() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    health = text.split("step_health() {", 1)[1].split("\n}", 1)[0]
    writer = text.split("write_deployment_evidence() {", 1)[1].split("\n}", 1)[0]

    assert health.index("capture_deployment_collector_heartbeats.py") < health.index(
        "date +%s%N"
    )
    assert health.index("date +%s%N") < health.index(
        "capture_deployment_health_boundary.py"
    )
    assert "APP_COLLECTOR_HEARTBEAT_ARGS" in health
    assert "--health-boundary-started-ns" in writer
    assert "--health-boundary-app-fingerprint" in writer
    assert "--collector-heartbeat-epoch" in text
    evidence_source = (
        REPO_ROOT / "scripts" / "write_deployment_evidence.py"
    ).read_text(encoding="utf-8")
    build_body = evidence_source.split("def build_evidence(", 1)[1]
    assert '("docker", "exec"' not in build_body
    assert '("docker", "cp"' not in build_body


def test_deploy_script_health_helpers_check_each_container_safely() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'state="$(wsl_run "docker inspect --format' in text
    assert '"${fields[0]}" != "running"' in text
    assert '"${fields[1]}" != "healthy"' in text
    assert '"${fields[2]}" != "0"' in text
    assert '"${fields[$last_index]}" == "0"' in text
    assert "printf '%s %s\\n' \"$c\" \"$state\"" in text
    assert "printf '%s missing\\n' \"$c\"" in text


def test_deploy_script_health_check_logs_progress_and_gateway_state() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "gateway_health_ok()" in text
    assert "required_app_container_states_compact()" in text
    assert "gateway_state=\"未就绪\"" in text
    assert "gateway_state=\"已就绪\"" in text
    assert "健康检查进度" in text
    assert "容器=${container_states}" in text


def test_deploy_script_compose_failures_are_not_swallowed() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "基础设施 docker compose up 返回非零；继续检查实际容器状态" not in text
    assert "应用 docker compose up 返回非零；继续进入健康检查确认实际容器状态" not in text
    assert "docker compose $COMPOSE_CMD_ARGS down --timeout 5\" ||" not in text
    assert "up -d --wait --wait-timeout 90" in text
    assert "应用服务启动命令已返回，等待健康检查确认" in text
    assert "step_app_up" in text
    assert "step_health" in text


def test_deploy_stops_apps_before_coordination_infra_and_budgets_readiness_v2() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    app_stop = "docker stop --time 15 $container_ids_to_stop"
    cutover_preflight = (
        'if ! run_nats_durable_cutover_preflight "$stage" "$previous_path"; then'
    )
    full_down = "docker compose $COMPOSE_CMD_ARGS down --timeout 5"
    assert "HEALTH_TIMEOUT=210" in text
    assert app_stop in text
    assert "应用容器未处于 exited/dead；拒绝继续" in text
    assert "~/aats-venv/bin/python scripts/check_nats_durable_cutover.py" in text
    assert "保持 NATS/Redis/Postgres 在线并终止部署" in text
    assert "--nats-cutover-preflight-before '$NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH'" in text
    assert "--nats-cutover-preflight-after '$NATS_CUTOVER_PREFLIGHT_AFTER_EVIDENCE_PATH'" in text
    assert "exit 9" in text
    step_down = text.split("step_down() {", 1)[1].split("\n}", 1)[0]
    required_preflight = text.split(
        "require_nats_durable_cutover_preflight() {", 1
    )[1].split("\n}", 1)[0]
    infra_only_up = "ensure_nats_cutover_preflight_infra_up"
    assert step_down.index(app_stop) < step_down.index(infra_only_up)
    assert step_down.index(infra_only_up) < step_down.index(
        "require_nats_durable_cutover_preflight"
    )
    assert cutover_preflight in required_preflight
    assert step_down.index("require_nats_durable_cutover_preflight") < step_down.index(
        full_down
    )


def test_deploy_stop_scope_is_all_profile_apps_not_only_target_profile() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    expected = {
        "aats-gateway",
        "aats-market",
        "aats-decision",
        "aats-execution",
        "aats-rdp-daemon",
        "aats-liquidations-daemon",
        "aats-microstructure-collector",
    }
    assignment = re.search(
        r'^ALL_KNOWN_APP_CONTAINERS="([^"]+)"$',
        text,
        flags=re.MULTILINE,
    )
    assert assignment is not None
    assert set(assignment.group(1).split()) == expected

    step_down = text.split("step_down() {", 1)[1].split("\n}", 1)[0]
    capture = text.split("capture_app_quiescence_snapshot() {", 1)[1].split(
        "\n}", 1
    )[0]
    assert "for container in $ALL_KNOWN_APP_CONTAINERS; do" in step_down
    assert "for container in $ALL_KNOWN_APP_CONTAINERS; do" in capture
    assert "docker stop --time 15 $container_ids_to_stop" in step_down
    assert 'container_id="$(owned_app_container_id "$container")"' in step_down
    assert "for container in $APP_CONTAINERS; do" not in step_down
    assert 'local env_prefix' in step_down
    assert 'env_prefix="$(compose_env_prefix)"' in step_down

    harness = (
        REPO_ROOT / "tests" / "smoke" / "test_deploy_step_down_contract.sh"
    ).read_text(encoding="utf-8")
    assert "source \"$PROJECT_ROOT/scripts/deploy.sh\"" in harness
    assert "aats-liquidations-daemon" in harness
    assert "aats-microstructure-collector" in harness
    assert "AATS_RUNTIME_READINESS_GENERATION" in harness
    assert "AATS_DEPLOYED_GIT_COMMIT" in harness


def test_derivatives_collectors_receive_safe_deployment_identity() -> None:
    text = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.derivatives.yml"
    ).read_text(encoding="utf-8")

    for service in ("aats-liquidations-daemon:", "aats-microstructure-collector:"):
        section = text.split(service, 1)[1].split("\n  aats-", 1)[0]
        assert "<<: *aats-derivatives-env" in section
        assert "AATS_RUNTIME_READINESS_GENERATION" in section
        assert "AATS_DEPLOYED_GIT_COMMIT" in section


def test_deploy_holds_one_long_lived_wsl_lock_across_all_mutations() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    main = text.split("main() {", 1)[1].split("\n}", 1)[0]
    acquire = text.split("acquire_deploy_lock() {", 1)[1].split("\n}", 1)[0]
    release = text.split("release_deploy_lock() {", 1)[1].split("\n}", 1)[0]
    supervisor = text.split("run_lock_supervised_external() {", 1)[1].split(
        "\n}", 1
    )[0]

    assert "coproc AATS_DEPLOY_LOCK_KEEPER" in acquire
    assert "exec 9>>$lock_file_q" in acquire
    assert "flock -n 9" in acquire
    assert 'DEPLOY_LOCK_FILE="/tmp/aats-standard-deploy.lock"' in text
    assert 'AATS_DEPLOY_TEST_MODE:-false' in text
    assert '"${BASH_SOURCE[0]}" != "$0"' in text
    assert "DEPLOY_LOCK_OVERRIDE_REJECTED" in acquire
    assert 'lease_glob_q' in acquire
    assert 'active_glob_q' in acquire
    assert 'glob.glob(active_glob)' in acquire
    assert "while python3 -c" in acquire
    assert "DEPLOY_LOCK_HEARTBEAT_PID" in acquire
    assert "rm " not in release
    assert "rmdir " not in release
    assert main.index("acquire_deploy_lock") < main.index(
        'run_locked_step "部署预检"'
    )
    assert main.index('run_locked_step "代码提交"') < main.index(
        'run_locked_step "镜像构建"'
    )
    assert main.index('run_locked_step "部署证据"') < main.index(
        'run_locked_step "部署报告"'
    )
    assert "assert_deploy_lock_held" in text
    assert "trap release_deploy_lock EXIT" in acquire
    assert "terminate_active_supervised_process" in release
    assert release.index("terminate_active_supervised_process") < release.index(
        "remove_deploy_lock_lease"
    )
    assert release.index("assert_no_owned_active_markers") < release.index(
        "remove_deploy_lock_lease"
    )
    assert release.count('abort_deploy_lock_release "$original_status"') == 4
    final_marker_check = release.rindex('assert_no_owned_active_markers')
    snapshot_cleanup = release.index("cleanup_nats_target_env_snapshot")
    lease_cleanup = release.index("remove_deploy_lock_lease")
    assert snapshot_cleanup < final_marker_check < lease_cleanup
    assert release.index(
        'assert_deploy_lock_held "释放部署锁前最终确认"'
    ) < lease_cleanup
    abort_release = text.split("abort_deploy_lock_release() {", 1)[1].split(
        "\n}", 1
    )[0]
    assert "trap - EXIT" in abort_release
    assert 'exit "$original_status"' in abort_release
    assert "exit 16" in abort_release
    assert "DEPLOY_LOCK_HELD=false" in release
    assert release.rindex("trap - EXIT") > release.index("DEPLOY_LOCK_HELD=false")
    assert "DEPLOY_ACTIVE_PROCESS_PID" in supervisor
    assert supervisor.index("assert_no_owned_active_markers") < supervisor.index(
        "mktemp -d"
    )
    assert supervisor.index("assert_deploy_lock_held") < supervisor.index(
        '"${supervised_command[@]}" &'
    )
    assert "run_supervised_command_guard \\\n" in text
    assert '"$marker_file" "$completion_mode" "$completion_file" "$user_mode"' in text
    assert '"$io_mode" "$gate_dir" "$@" &' in text
    assert '"test ! -e $marker_q"' in text

    harness = (
        REPO_ROOT / "tests" / "smoke" / "test_deploy_lock_wsl_contract.sh"
    ).read_text(encoding="utf-8")
    assert "requires Windows Git Bash -> WSL" in harness
    assert "second WSL process acquired" in harness
    assert "deployment lock remained held after release" in harness
    assert "mid-step keeper loss did not fail closed" in harness
    assert "retain exclusion until the supervised WSL child finished" in harness
    assert "production lock-path override was not rejected" in harness
    assert "keeper loss allowed a side effect or false-success release" in harness
    assert "fresh predecessor lease did not quarantine takeover" in harness
    assert "TERM deployment shell did not exit 143" in harness
    assert "retain exclusion until the supervised WSL child finished" in harness
    assert "successor crossed stale lease while prior SIGKILL mutation was active" in harness
    assert "wrapper hard-crash allowed successor overlap" in harness
    assert "wrapper hard-crash poison allowed a second command to start" in harness
    assert "wsl-ack-semantic-nonzero-smoke" in harness
    assert "WSL completion ack did not preserve output/status" in harness


def test_deploy_requires_clean_wsl_checkout_even_when_sync_is_skipped() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    checker = text.split("assert_wsl_checkout_clean() {", 1)[1].split("\n}", 1)[0]
    build = text.split("step_build() {", 1)[1].split("\n}", 1)[0]
    evidence = text.split("write_deployment_evidence() {", 1)[1].split("\n}", 1)[0]

    assert "git -C $WSL_PROJECT diff --quiet --ignore-submodules=none" in checker
    assert "git -C $WSL_PROJECT diff --cached --quiet --ignore-submodules=none" in checker
    assert "~/aats-venv/bin/python scripts/write_deployment_evidence.py" in evidence
    assert "/usr/bin/python3 scripts/write_deployment_evidence.py" not in evidence
    writer_source = (
        REPO_ROOT / "scripts" / "write_deployment_evidence.py"
    ).read_text(encoding="utf-8")
    assert "sys.path.insert(0, str(_PROJECT_ROOT))" in writer_source
    assert "git -C $WSL_PROJECT ls-files --others --exclude-standard" in checker
    assert "return 19" in checker
    assert build.count("assert_wsl_checkout_clean") == 2
    assert build.index('assert_wsl_checkout_clean "镜像构建前"') < build.index(
        "docker compose $COMPOSE_CMD_ARGS build"
    )
    assert build.index("docker compose $COMPOSE_CMD_ARGS build") < build.index(
        'assert_wsl_checkout_clean "镜像构建后"'
    )
    assert 'assert_wsl_checkout_clean "最终部署证据生成前"' in evidence

    harness = (
        REPO_ROOT / "tests" / "smoke" / "test_deploy_wsl_checkout_clean_contract.sh"
    ).read_text(encoding="utf-8")
    assert "SKIP_SYNC=true" in harness
    assert "dirty WSL checkout was accepted after --skip-sync" in harness


def test_deploy_postgres_password_sync_is_lock_supervised() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    infra = text.split("step_infra_up() {", 1)[1].split("\n}", 1)[0]

    assert infra.count('    wsl_run "') == 2
    assert 'wsl -d "$DISTRO"' not in infra
    assert "docker exec -i aats-postgres psql" in infra
    assert "PG_USER=\\$(grep '^POSTGRES_USER=' \\\"$WSL2_ENV_FILE\\\"" in infra
    assert "PG_PW=\\$(grep '^POSTGRES_PASSWORD=' \\\"$WSL2_ENV_FILE\\\"" in infra
    assert "grep '^POSTGRES_USER=' '$WSL2_ENV_FILE'" not in infra
    assert "grep '^POSTGRES_PASSWORD=' '$WSL2_ENV_FILE'" not in infra
    assert 'pg_password=\\"\\$PG_PW\\"' in infra


def test_deploy_revalidates_quiescence_around_each_cutover_query() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    require = text.split(
        "require_nats_durable_cutover_preflight() {", 1
    )[1].split("\n}", 1)[0]
    main = text.split("main() {", 1)[1].split("\n}", 1)[0]

    before = 'assert_app_quiescence_unchanged "$context preflight 前"'
    query = "run_nats_durable_cutover_preflight"
    after = 'assert_app_quiescence_unchanged "$context preflight 后"'
    assert require.index(before) < require.index(query) < require.index(after)
    assert "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|{{.RestartCount}}" in text
    assert 'run_locked_step "最终 NATS cutover"' in main
    assert main.index('run_locked_step "最终 NATS cutover"') < main.index(
        'run_locked_step "应用启动"'
    )
    assert '"full-down 前" "pre_full_down"' in text
    assert '"最终 app-up 前" "post_infra_pre_app_up"' in main
    assert '"$NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH"' in main
    assert "NATS_CUTOVER_PREFLIGHT_BEFORE_EVIDENCE_PATH" in text
    assert "NATS_CUTOVER_PREFLIGHT_AFTER_EVIDENCE_PATH" in text


def test_deploy_app_stop_state_matrix_is_fail_closed() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    classifier = text.split(
        "app_container_state_is_stopped() {", 1
    )[1].split("\n}", 1)[0]
    accepted_match = re.search(
        r"\n\s*([^\n)]+)\)\s*\n\s*return 0",
        classifier,
    )
    assert accepted_match is not None
    accepted = set(accepted_match.group(1).strip().split("|"))

    expected = {
        "exited": True,
        "dead": True,
        "paused": False,
        "restarting": False,
        "removing": False,
        "running": False,
        "created": False,
        "unknown": False,
        "": False,
    }
    assert {state: state in accepted for state in expected} == expected
    assert "docker ps -a --format '{{.Names}}'" in text
    assert 'snapshot+="$container|not-found|-|-|-|-"' in text
    assert "应用容器 inspect 失败；拒绝继续" in text
    assert "exit 12" in text


def test_deploy_preflight_bootstraps_infra_only_and_preserves_it_on_failure() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    infra_function = text.split(
        "ensure_nats_cutover_preflight_infra_up() {", 1
    )[1].split("\n}", 1)[0]
    step_down = text.split("step_down() {", 1)[1].split("\n}", 1)[0]
    required_preflight = text.split(
        "require_nats_durable_cutover_preflight() {", 1
    )[1].split("\n}", 1)[0]

    assert "docker start" not in infra_function
    assert "首次只读 baseline 前禁止自动启动任何已停止容器" in infra_function
    assert "{{.Id}}|{{.State.Status}}|{{.Config.Image}}" in infra_function
    assert "{{.Type}}|{{.Name}}|{{.RW}}" in infra_function
    assert "com.docker.compose.project" in infra_function
    assert "com.docker.compose.service" in infra_function
    assert "com.aats.bootstrap_lock='$DEPLOY_LOCK_TOKEN'" in infra_function
    assert 'fresh_claim" != "$DEPLOY_LOCK_TOKEN' in infra_function
    assert "fresh NATS 持久卷声明 token 不匹配" in infra_function
    assert "容器缺失但候选 NATS 持久卷仍存在" in infra_function
    assert "up -d --wait --wait-timeout 90 --no-deps nats" in infra_function
    assert "$COMPOSE_OVERLAY" not in infra_function
    assert "$APP_CONTAINERS" not in infra_function
    assert (
        'if ! run_nats_durable_cutover_preflight "$stage" "$previous_path"; then'
        in required_preflight
    )
    assert "exit 9" in required_preflight
    assert step_down.index("require_nats_durable_cutover_preflight") < step_down.index(
        "docker compose $COMPOSE_CMD_ARGS down --timeout 5"
    )
    assert "唯一恢复路径" in required_preflight

    harness = (
        REPO_ROOT / "tests" / "smoke" / "test_nats_preflight_bootstrap_contract.sh"
    ).read_text(encoding="utf-8")
    assert "existing_stopped" in harness
    assert "existing_created" in harness
    assert "existing_foreign_project" in harness
    assert "existing_foreign_service" in harness
    assert "existing_wrong_image" in harness
    assert "existing_wrong_volume" in harness
    assert "existing_read_only_volume" in harness
    assert "existing_duplicate_data_mount" in harness
    assert '[[ -z "$captured_start" ]]' in harness
    assert '[[ -z "$captured_compose" ]]' in harness
    assert "missing_with_volume" in harness
    assert "fresh_install" in harness
    assert "fresh_claim_race" in harness
    assert "fresh_post_claim_replace" in harness


def test_deploy_script_accepts_root_and_legacy_wsl2_env_file_locations() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert 'test -f $WSL_PROJECT/.env.wsl2' in text
    assert 'test -f $WSL_PROJECT/$DEPLOY_DIR/.env.wsl2' in text


def test_postgres_backup_and_restore_are_database_scoped_and_verified() -> None:
    backup = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "scripts" / "backup_postgres.sh"
    ).read_text(encoding="utf-8")
    restore = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "scripts" / "restore_postgres.sh"
    ).read_text(encoding="utf-8")
    health = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "scripts" / "cron_healthcheck.sh"
    ).read_text(encoding="utf-8")

    assert '${DB_NAME}_${timestamp}.dump' in backup
    assert 'pg_restore --list < "${tmp_file}"' in backup
    assert '"${out_file}.sha256"' in backup
    assert 'name "${DB_NAME}_*.dump"' in restore
    assert 'basename}" != "${DB_NAME}_"*.dump' in restore
    assert 'sha256sum -c "${basename}.sha256"' in restore
    assert 'pg_restore --list < "${backup_file}"' in restore
    assert '--exit-on-error' in restore
    assert 'backup_database="${POSTGRES_DB:-aats}"' in health


def test_deploy_script_reports_actual_wsl_deployed_head() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "Windows HEAD:" in text
    assert "WSL HEAD:" in text
    assert "实际部署版本" in text
    assert "git -C $WSL_PROJECT rev-parse HEAD" in text


def test_deploy_script_syncs_postgres_password_with_psql_variables() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "-v pg_user=" in text
    assert "-v pg_password=" in text
    assert 'ALTER USER :\\"pg_user\\" PASSWORD :\'pg_password\';' in text


def test_postgres_probes_use_an_existing_database_and_redis_host_is_prepared() -> None:
    deploy_text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    compose_text = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    assert "docker exec aats-postgres sh -lc 'pg_isready" not in deploy_text
    assert "up -d --wait --wait-timeout 90" in deploy_text
    assert 'pg_isready -U \\"$$POSTGRES_USER\\" -d \\"$$POSTGRES_DB\\"' in compose_text
    assert "ensure_wsl_runtime_prerequisites" in deploy_text
    assert "vm.overcommit_memory=1" in deploy_text
    assert 'wsl -d "$DISTRO" -u root bash -c' in deploy_text


def test_deploy_script_provisions_tls_for_live_profiles_and_uses_https_health_checks() -> None:
    text = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    compose_text = (REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml").read_text(encoding="utf-8")
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "is_live_profile()" in text
    assert "ensure_operator_tls_assets()" in text
    assert "AATS_OPERATOR_TLS_CERT_FILE" in text
    assert "AATS_OPERATOR_TLS_KEY_FILE" in text
    assert "curl -kfs https://127.0.0.1:$port/healthz" in text
    assert "runtime/operator-tls" in gitignore_text
    assert "AATS_OPERATOR_TLS_CERT_FILE" in compose_text
    assert "AATS_OPERATOR_TLS_KEY_FILE" in compose_text
    assert "runtime/operator-tls:/app/deploy/wsl2-dev/runtime/operator-tls:ro" in compose_text
    assert "curl -kfs https://localhost:${AATS_API_PORT:-8000}/healthz" in compose_text


def test_derivatives_live_overlay_enables_execution_command_flow() -> None:
    compose_text = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.derivatives-live.yml"
    ).read_text(encoding="utf-8")

    assert 'AATS_EXECUTION_COMMAND_FLOW_ENABLED: "true"' in compose_text


def test_rdp_artifacts_are_shared_and_persistent_across_container_rebuilds() -> None:
    deploy = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    services = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml"
    ).read_text(encoding="utf-8")

    assert services.count("../../artifacts:/app/artifacts") == 2
    assert "ensure_rdp_artifact_directory" in deploy
    assert "chown -R 1000:1000 '$WSL_PROJECT/artifacts'" in deploy


def test_deploy_runbook_no_longer_points_to_stale_sync_or_bootstrap_paths() -> None:
    runbook = (REPO_ROOT / "deploy" / "wsl2-dev" / "RUNBOOK.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "deploy" / "wsl2-dev" / "README.md").read_text(encoding="utf-8")
    sync_workflow = (REPO_ROOT / "docs" / "operations" / "wsl2_sync_workflow.md").read_text(
        encoding="utf-8"
    )

    assert "单独 rsync 到 `~/aats-deploy/`" not in runbook
    assert "python3 -m aats.scripts.bootstrap_database" not in runbook
    assert "python3 -m aats.api.main" not in runbook
    assert "docker compose --env-file deploy/wsl2-dev/.env.wsl2" not in runbook
    assert "docker compose down -v" not in readme
    assert "envs/.env.wsl2-dev" not in readme
    assert "docker compose --env-file .env.wsl2 up -d" not in sync_workflow
    assert "bash scripts/deploy.sh --profile derivatives --skip-commit" in sync_workflow
