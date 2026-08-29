import copy

import pytest

from scripts import check_nats_durable_cutover as cutover
from scripts import write_deployment_evidence as evidence


START_NS = 1_725_000_000_000_000_123
POST_START_NS = START_NS + 100
POST_END_NS = START_NS + 200
APP_AUTH_NS = START_NS + 300
HEALTH_BOUNDARY_NS = START_NS + 10_000
CUTOFF_NS = START_NS + 20_000
COVERAGE_END_NS = START_NS + 30_000


def _final_facts() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "container_id": f"{index:064x}",
        }
        for index, name in enumerate(
            evidence._REQUIRED_CONTAINERS_BY_PROFILE["spot"],
            start=1,
        )
    ]


def _valid_events() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, fact in enumerate(_final_facts(), start=1):
        event_time = APP_AUTH_NS + index * 10
        rows.extend(
            [
                {
                    "name": fact["name"],
                    "container_id": fact["container_id"],
                    "action": "create",
                    "time_nano": event_time,
                },
                {
                    "name": fact["name"],
                    "container_id": fact["container_id"],
                    "action": "start",
                    "time_nano": event_time + 1,
                },
                {
                    "name": fact["name"],
                    "container_id": fact["container_id"],
                    "action": "health_status: healthy",
                    "time_nano": event_time + 2,
                },
            ]
        )
    return sorted(rows, key=lambda row: int(row["time_nano"]))


def _capture(events: list[dict[str, object]] | None = None) -> dict[str, object]:
    rows = _valid_events() if events is None else events
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
        "allowlist": [*evidence._KNOWN_APP_CONTAINERS, evidence._NATS_CONTAINER],
        "docker_daemon_id": "AATS:LOCAL:DAEMON:01",
        "coverage_started_ns": START_NS,
        "requested_cutoff_ns": CUTOFF_NS,
        "coverage_ended_ns": COVERAGE_END_NS,
        "segments": [
            {
                "segment_id": 1,
                "requested_at_ns": START_NS - 1,
                "until_ns": COVERAGE_END_NS,
                "ready_at_ns": START_NS,
                "completed_at_ns": COVERAGE_END_NS + 1,
                "event_count": len(rows),
                "clean_eof": True,
            }
        ],
        "pre_coverage_history_events": [],
        "events": rows,
        "fatal_errors": [],
    }


def _validate(capture: dict[str, object]) -> dict[str, object]:
    return evidence._validate_external_lifecycle_capture(
        capture,
        post_window_started_ns=POST_START_NS,
        post_window_ended_ns=POST_END_NS,
        app_up_authorized_ns=APP_AUTH_NS,
        health_boundary_started_ns=HEALTH_BOUNDARY_NS,
        requested_cutoff_ns=CUTOFF_NS,
        required_container_facts=_final_facts(),
    )


def test_exact_startup_sequences_pass_only_as_bounded_observation() -> None:
    result = _validate(_capture())

    assert result["status"] == "PASSED_WITH_TRUST_BOUNDARY"
    assert result["complete"] is False
    assert result["coverage_status"] == "BOUNDED_OBSERVED"
    assert result["transport_coverage_ended_ns"] == COVERAGE_END_NS


def test_event_before_app_up_authorization_is_rejected() -> None:
    rows = _valid_events()
    rows[0]["time_nano"] = APP_AUTH_NS - 1
    rows.sort(key=lambda row: int(row["time_nano"]))

    with pytest.raises(
        RuntimeError,
        match="app_lifecycle_changed_before_app_up_authorization",
    ):
        _validate(_capture(rows))


def test_missing_required_create_is_rejected() -> None:
    rows = _valid_events()
    target = _final_facts()[0]
    rows = [
        row
        for row in rows
        if not (row["name"] == target["name"] and row["action"] == "create")
    ]

    with pytest.raises(RuntimeError, match="incomplete_container_startup_lifecycle"):
        _validate(_capture(rows))


def test_recreated_container_id_drift_is_rejected() -> None:
    rows = copy.deepcopy(_valid_events())
    rows[0]["container_id"] = "f" * 64

    with pytest.raises(
        RuntimeError,
        match="container_identity_changed_during_app_startup",
    ):
        _validate(_capture(rows))


def test_unhealthy_transition_is_rejected_even_before_boundary() -> None:
    rows = copy.deepcopy(_valid_events())
    rows[2]["action"] = "health_status: unhealthy"

    with pytest.raises(RuntimeError, match="invalid_container_health_lifecycle"):
        _validate(_capture(rows))


def test_nats_lifecycle_event_is_rejected_across_entire_capture() -> None:
    rows = _valid_events()
    rows.append(
        {
            "name": "aats-nats",
            "container_id": "e" * 64,
            "action": "restart",
            "time_nano": APP_AUTH_NS + 1,
        }
    )
    rows.sort(key=lambda row: int(row["time_nano"]))

    with pytest.raises(
        RuntimeError,
        match="nats_lifecycle_changed_since_post_preflight",
    ):
        _validate(_capture(rows))


def test_any_post_health_boundary_event_is_rejected() -> None:
    rows = _valid_events()
    rows.append(
        {
            "name": _final_facts()[0]["name"],
            "container_id": _final_facts()[0]["container_id"],
            "action": "health_status: healthy",
            "time_nano": HEALTH_BOUNDARY_NS,
        }
    )
    rows.sort(key=lambda row: int(row["time_nano"]))

    with pytest.raises(
        RuntimeError,
        match="container_lifecycle_changed_after_health_boundary",
    ):
        _validate(_capture(rows))


def _consumer(durable: str) -> cutover.ConsumerState:
    return cutover.ConsumerState(
        stream="AATS_EVENTS_COMMANDS",
        durable=durable,
        created="2026-08-28T00:00:00Z",
        deliver_policy="all",
        ack_policy="explicit",
        filter_subject="aats.execution.order_intents",
        filter_subjects=(),
        deliver_group=None,
        max_ack_pending=1,
        num_ack_pending=0,
        cursor=cutover.ConsumerCursor(0, 0, 0, 0),
    )


def test_final_nats_state_rejects_unexpected_legacy_consumer(
    monkeypatch,
) -> None:
    expected = cutover.ExpectedDurable(
        durable="aats-execution-execution_order_intents",
        role="execution",
        topic="execution.order_intents",
        stream="AATS_EVENTS_COMMANDS",
        filter_subject="aats.execution.order_intents",
    )
    monkeypatch.setattr(
        cutover,
        "build_expected_durable_index",
        lambda: {expected.durable: expected},
    )
    result = cutover.QueryResult(
        stream_count=0,
        consumer_count=2,
        consumers=(
            _consumer(expected.durable),
            _consumer("legacy-unowned-consumer"),
        ),
        streams=(),
    )

    with pytest.raises(
        RuntimeError,
        match="final_nats_durable_set_not_qualified",
    ):
        evidence._read_final_nats_state(lambda: result)
