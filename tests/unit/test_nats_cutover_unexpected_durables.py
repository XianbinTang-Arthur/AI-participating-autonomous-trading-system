import copy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from scripts import check_nats_durable_cutover as cutover


def _state(
    durable: str,
    *,
    stream: str = "AATS_EVENTS_COMMANDS",
    deliver_policy: str = "all",
    filter_subject: str = "aats.execution.order_intents",
    max_ack_pending: int = 1,
    num_ack_pending: int = 0,
) -> cutover.ConsumerState:
    return cutover.ConsumerState(
        stream=stream,
        durable=durable,
        created="2026-08-28T00:00:00Z",
        deliver_policy=deliver_policy,
        ack_policy="explicit",
        filter_subject=filter_subject,
        filter_subjects=(),
        deliver_group=None,
        max_ack_pending=max_ack_pending,
        num_ack_pending=num_ack_pending,
        cursor=cutover.ConsumerCursor(0, 0, 0, 0),
    )


def test_unexpected_durable_is_retained_as_no_secret_blocker() -> None:
    expected = cutover.ExpectedDurable(
        durable="aats-execution-execution_order_intents",
        role="execution",
        topic="execution.order_intents",
        stream="AATS_EVENTS_COMMANDS",
        filter_subject="aats.execution.order_intents",
    )

    rows = cutover.build_unexpected_durable_rows(
        (_state(expected.durable), _state("legacy-unowned-consumer")),
        {expected.durable: expected},
    )

    assert rows == [
        {
            "identity": {
                "stream": "AATS_EVENTS_COMMANDS",
                "durable": "legacy-unowned-consumer",
            },
            "created": "2026-08-28T00:00:00Z",
            "cursor": {
                "delivered_stream_seq": 0,
                "delivered_consumer_seq": 0,
                "ack_floor_stream_seq": 0,
                "ack_floor_consumer_seq": 0,
            },
            "immutable_config": {
                "actual": {
                    "stream": "AATS_EVENTS_COMMANDS",
                    "deliver_policy": "all",
                    "ack_policy": "explicit",
                    "filter_subject": "aats.execution.order_intents",
                    "filter_subjects": [],
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
            },
            "mutable_config": {
                "actual": {
                    "ack_wait_seconds": 30.0,
                    "max_deliver": 5,
                }
            },
            "window": {"current_max_ack_pending": 1},
            "outstanding": {"num_ack_pending": 0},
            "status": "BLOCKED",
            "blockers": ["consumer_owner_not_declared"],
        }
    ]


def test_unexpected_durable_forces_blocked_evidence() -> None:
    unexpected = cutover.build_unexpected_durable_rows(
        (_state("legacy-unowned-consumer"),),
        {},
    )

    payload = cutover.build_evidence(
        generation="generation-1",
        deployment_lock_id="lock-1",
        deployed_commit="a" * 40,
        checked_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
        query_result=cutover.QueryResult(0, 1, (_state("legacy-unowned-consumer"),)),
        rows=(),
        status="PASSED_WITH_TRUST_BOUNDARY",
        app_quiescence=None,
        unexpected_durables=unexpected,
    )

    assert payload["status"] == "BLOCKED"
    assert payload["unexpected_durables"] == unexpected
    assert payload["blocker_classes"] == ["consumer_owner_not_declared"]
    assert payload["recovery"]["instruction_code"] == (
        "nats_durable_cutover_requires_approved_unknown_consumer_release_review"
    )


def test_declared_non_event_with_outstanding_delivery_is_safe_rebuildable() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-system_guard_signal_updates"]
    state = _state(
        durable.durable,
        stream=durable.stream,
        deliver_policy=durable.deliver_policy,
        filter_subject=durable.filter_subject,
        max_ack_pending=256,
        num_ack_pending=1,
    )

    rows, blocked = cutover.evaluate_existing_consumers((state,), expected)
    unexpected = cutover.build_unexpected_durable_rows((state,), expected)

    assert blocked is False
    assert unexpected == []
    assert rows[0]["identity"]["delivery_semantics"] == "snapshot"
    assert rows[0]["status"] == "SAFE_REBUILDABLE_NON_EVENT"
    assert rows[0]["blockers"] == []


def test_mixed_declared_and_unknown_consumers_blocks_only_unknown_owner() -> None:
    expected = cutover.build_expected_durable_index()
    known = expected["aats-decision-system_ai_command_requests"]
    known_state = _state(
        known.durable,
        stream=known.stream,
        deliver_policy=known.deliver_policy,
        filter_subject=known.filter_subject,
        max_ack_pending=256,
    )
    unknown_state = replace(
        known_state,
        durable="aats-codex_manual_resume-system_operator_command_responses",
        filter_subject="aats.system.operator_command_responses",
    )

    rows, blocked = cutover.evaluate_existing_consumers(
        (known_state, unknown_state),
        expected,
    )
    unexpected = cutover.build_unexpected_durable_rows(
        (known_state, unknown_state),
        expected,
    )

    assert blocked is False
    assert [row["identity"]["durable"] for row in rows] == [known.durable]
    assert [row["identity"]["durable"] for row in unexpected] == [
        unknown_state.durable
    ]


def test_declared_non_event_name_with_config_drift_remains_blocked() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-system_ai_command_requests"]
    state = _state(
        durable.durable,
        stream=durable.stream,
        deliver_policy="last",
        filter_subject=durable.filter_subject,
        max_ack_pending=256,
    )

    rows, blocked = cutover.evaluate_existing_consumers((state,), expected)

    assert blocked is True
    assert rows[0]["status"] == "BLOCKED"
    assert rows[0]["blockers"] == ["deliver_policy_drift"]


def test_declared_snapshot_may_raise_ack_wait_in_place() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-market_snapshots"]
    state = replace(
        _state(
            durable.durable,
            stream=durable.stream,
            deliver_policy=durable.deliver_policy,
            filter_subject=durable.filter_subject,
            max_ack_pending=256,
        ),
        ack_wait_seconds=30.0,
    )

    row = cutover.evaluate_consumer(state, durable)

    assert row["status"] == "SAFE_TO_RECONCILE_IN_PLACE"
    assert row["blockers"] == []
    assert row["mutable_config"] == {
        "actual": {"ack_wait_seconds": 30.0, "max_deliver": 5},
        "expected": {"ack_wait_seconds": 90.0, "max_deliver": 5},
    }


@pytest.mark.parametrize("current_ack_wait", (0.0, -1.0, 120.0))
def test_declared_snapshot_rejects_nonpositive_or_decreasing_ack_wait(
    current_ack_wait: float,
) -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-market_snapshots"]
    state = replace(
        _state(
            durable.durable,
            stream=durable.stream,
            deliver_policy=durable.deliver_policy,
            filter_subject=durable.filter_subject,
        ),
        ack_wait_seconds=current_ack_wait,
    )

    row = cutover.evaluate_consumer(state, durable)

    assert row["status"] == "BLOCKED"
    assert row["blockers"] == ["ack_wait_drift"]


@pytest.mark.parametrize(
    ("field", "value", "blocker"),
    (
        ("deliver_subject_present", False, "push_delivery_subject_missing"),
        ("replay_policy", "original", "replay_policy_drift"),
        ("headers_only", True, "headers_only_enabled"),
        ("pause_until", "2026-08-29T01:00:00Z", "consumer_paused"),
        ("backoff_seconds", (1.0,), "backoff_policy_drift"),
        ("rate_limit_bps", 1, "rate_limit_enabled"),
        ("inactive_threshold_seconds", 60.0, "inactive_threshold_enabled"),
        ("mem_storage", True, "memory_storage_enabled"),
        ("ack_wait_seconds", 31.0, "ack_wait_drift"),
        ("max_deliver", 6, "max_deliver_drift"),
        ("durable_name_matches", False, "durable_name_drift"),
        ("opt_start_seq", 2, "start_sequence_enabled"),
        ("opt_start_time", "2099-01-01T00:00:00Z", "start_time_enabled"),
    ),
)
def test_declared_consumer_dangerous_behavior_is_fail_closed(
    field: str,
    value: object,
    blocker: str,
) -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-execution-execution_order_intents"]
    state = _state(
        durable.durable,
        stream=durable.stream,
        deliver_policy=durable.deliver_policy,
        filter_subject=durable.filter_subject,
    )

    row = cutover.evaluate_consumer(replace(state, **{field: value}), durable)

    assert row["status"] == "BLOCKED"
    assert row["blockers"] == [blocker]


def test_active_continuity_allows_only_declared_in_place_window_shrink() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-execution-execution_order_intents"]
    previous_state = replace(
        _state(
            durable.durable,
            stream=durable.stream,
            deliver_policy=durable.deliver_policy,
            filter_subject=durable.filter_subject,
            max_ack_pending=256,
        ),
        cursor=cutover.ConsumerCursor(10, 9, 8, 8),
    )
    current_state = replace(
        previous_state,
        max_ack_pending=1,
        cursor=cutover.ConsumerCursor(11, 10, 9, 9),
    )
    previous = cutover.evaluate_consumer(previous_state, durable)
    current = cutover.evaluate_consumer(current_state, durable)

    allowed = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[current],
        allow_declared_cutover_migrations=True,
    )
    strict = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[current],
    )
    assert previous["status"] == "SAFE_TO_SHRINK"
    assert current["status"] == "SAFE_ALREADY_ONE"
    assert allowed["status"] == "PASSED_WITH_TRUST_BOUNDARY"
    assert allowed["authorized_durable_migrations"] == [
        {
            "identity": f"{durable.stream}/{durable.durable}",
            "kind": "IN_PLACE_ACK_WINDOW_SHRINK",
        }
    ]
    assert strict["status"] == "INVALIDATED"
    assert strict["violations"] == [
        f"durable_window_changed:{durable.stream}/{durable.durable}"
    ]


@pytest.mark.parametrize(
    "previous_window,previous_outstanding",
    ((256, 1), (1, 2)),
)
def test_active_continuity_allows_only_declared_non_event_rebuild(
    previous_window: int,
    previous_outstanding: int,
) -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-system_guard_signal_updates"]
    previous_state = replace(
        _state(
            durable.durable,
            stream=durable.stream,
            deliver_policy=durable.deliver_policy,
            filter_subject=durable.filter_subject,
            max_ack_pending=previous_window,
            num_ack_pending=previous_outstanding,
        ),
        cursor=cutover.ConsumerCursor(100, 90, 80, 80),
    )
    current_state = replace(
        previous_state,
        created="2026-08-29T00:00:00Z",
        max_ack_pending=1,
        num_ack_pending=0,
        cursor=cutover.ConsumerCursor(1, 1, 0, 0),
        ack_wait_seconds=durable.ack_wait_seconds,
    )
    previous = cutover.evaluate_consumer(previous_state, durable)
    current = cutover.evaluate_consumer(current_state, durable)

    allowed = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[current],
        allow_declared_cutover_migrations=True,
    )
    strict = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[current],
    )
    forged_same_creation = cutover.evaluate_consumer(
        replace(current_state, created=previous_state.created),
        durable,
    )
    forged = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[forged_same_creation],
        allow_declared_cutover_migrations=True,
    )

    assert previous["status"] == "SAFE_REBUILDABLE_NON_EVENT"
    assert current["status"] == "SAFE_ALREADY_ONE"
    assert allowed["status"] == "PASSED_WITH_TRUST_BOUNDARY"
    assert allowed["authorized_durable_migrations"] == [
        {
            "identity": f"{durable.stream}/{durable.durable}",
            "kind": "NON_EVENT_DURABLE_REBUILD",
        }
    ]
    assert strict["status"] == "INVALIDATED"
    assert any(
        violation.startswith("durable_created_changed:")
        for violation in strict["violations"]
    )
    assert forged["status"] == "INVALIDATED"
    assert forged["authorized_durable_migrations"] == []
    assert any(
        violation.startswith("durable_cursor_regressed:")
        for violation in forged["violations"]
    )


def test_active_continuity_never_allows_event_cursor_recreation() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-execution-execution_order_intents"]
    previous_state = replace(
        _state(
            durable.durable,
            stream=durable.stream,
            deliver_policy=durable.deliver_policy,
            filter_subject=durable.filter_subject,
            max_ack_pending=256,
        ),
        cursor=cutover.ConsumerCursor(100, 90, 80, 80),
    )
    current_state = replace(
        previous_state,
        created="2026-08-29T00:00:00Z",
        max_ack_pending=1,
        cursor=cutover.ConsumerCursor(1, 1, 0, 0),
    )

    continuity = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[cutover.evaluate_consumer(previous_state, durable)],
        current_durables=[cutover.evaluate_consumer(current_state, durable)],
        allow_declared_cutover_migrations=True,
    )

    assert continuity["status"] == "INVALIDATED"
    assert continuity["authorized_durable_migrations"] == []
    assert any(
        violation.startswith("durable_created_changed:")
        for violation in continuity["violations"]
    )
    assert any(
        violation.startswith("durable_cursor_regressed:")
        for violation in continuity["violations"]
    )


def test_active_continuity_allows_declared_snapshot_ack_wait_raise() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-market_snapshots"]
    previous_state = replace(
        _state(
            durable.durable,
            stream=durable.stream,
            deliver_policy=durable.deliver_policy,
            filter_subject=durable.filter_subject,
            max_ack_pending=256,
        ),
        cursor=cutover.ConsumerCursor(10, 9, 8, 8),
        ack_wait_seconds=30.0,
    )
    current_state = replace(
        previous_state,
        max_ack_pending=1,
        ack_wait_seconds=90.0,
        cursor=cutover.ConsumerCursor(11, 10, 9, 9),
    )
    previous = cutover.evaluate_consumer(previous_state, durable)
    current = cutover.evaluate_consumer(current_state, durable)

    allowed = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[current],
        allow_declared_cutover_migrations=True,
    )
    strict = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[current],
    )

    assert previous["status"] == "SAFE_TO_RECONCILE_IN_PLACE"
    assert current["status"] == "SAFE_ALREADY_ONE"
    assert allowed["status"] == "PASSED_WITH_TRUST_BOUNDARY"
    assert allowed["authorized_durable_migrations"] == [
        {
            "identity": f"{durable.stream}/{durable.durable}",
            "kind": "IN_PLACE_MUTABLE_RECONCILE",
        }
    ]
    assert strict["status"] == "INVALIDATED"
    assert any(
        violation.startswith("durable_mutable_config_changed:")
        for violation in strict["violations"]
    )


def test_active_continuity_rejects_forged_mutable_reconcile() -> None:
    expected = cutover.build_expected_durable_index()
    durable = expected["aats-decision-market_snapshots"]
    previous = cutover.evaluate_consumer(
        replace(
            _state(
                durable.durable,
                stream=durable.stream,
                deliver_policy=durable.deliver_policy,
                filter_subject=durable.filter_subject,
                max_ack_pending=256,
            ),
            ack_wait_seconds=30.0,
            cursor=cutover.ConsumerCursor(10, 10, 9, 9),
        ),
        durable,
    )
    current = cutover.evaluate_consumer(
        replace(
            _state(
                durable.durable,
                stream=durable.stream,
                deliver_policy=durable.deliver_policy,
                filter_subject=durable.filter_subject,
            ),
            ack_wait_seconds=90.0,
            cursor=cutover.ConsumerCursor(11, 11, 10, 10),
        ),
        durable,
    )
    forged_max_deliver = copy.deepcopy(previous)
    forged_max_deliver["mutable_config"]["actual"]["max_deliver"] = 6
    forged_not_at_target = copy.deepcopy(current)
    forged_not_at_target["mutable_config"]["actual"]["ack_wait_seconds"] = 60.0
    forged_recreated = copy.deepcopy(current)
    forged_recreated["created"] = "2026-08-29T00:00:00Z"
    forged_cursor = copy.deepcopy(current)
    forged_cursor["cursor"]["delivered_stream_seq"] = 1

    cases = (
        (forged_max_deliver, current),
        (previous, forged_not_at_target),
        (previous, forged_recreated),
        (previous, forged_cursor),
    )
    for previous_row, current_row in cases:
        continuity = cutover.evaluate_active_runtime_continuity(
            previous_streams=[],
            current_streams=[],
            previous_durables=[previous_row],
            current_durables=[current_row],
            allow_declared_cutover_migrations=True,
        )
        assert continuity["status"] == "INVALIDATED"
        assert continuity["authorized_durable_migrations"] == []


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("ack_wait_seconds", True),
        ("max_deliver", True),
    ),
)
def test_active_continuity_rejects_boolean_numeric_evidence_forgery(
    field: str,
    forged_value: object,
) -> None:
    expected = cutover.build_expected_durable_index()
    durable = replace(
        expected["aats-decision-market_snapshots"],
        ack_wait_seconds=1.0,
        max_deliver=1,
    )
    previous = cutover.evaluate_consumer(
        replace(
            _state(
                durable.durable,
                stream=durable.stream,
                deliver_policy=durable.deliver_policy,
                filter_subject=durable.filter_subject,
            ),
            ack_wait_seconds=0.5,
            max_deliver=1,
            cursor=cutover.ConsumerCursor(10, 10, 9, 9),
        ),
        durable,
    )
    current = cutover.evaluate_consumer(
        replace(
            _state(
                durable.durable,
                stream=durable.stream,
                deliver_policy=durable.deliver_policy,
                filter_subject=durable.filter_subject,
            ),
            ack_wait_seconds=1.0,
            max_deliver=1,
            cursor=cutover.ConsumerCursor(11, 11, 10, 10),
        ),
        durable,
    )
    forged_current = copy.deepcopy(current)
    forged_current["mutable_config"]["actual"][field] = forged_value

    continuity = cutover.evaluate_active_runtime_continuity(
        previous_streams=[],
        current_streams=[],
        previous_durables=[previous],
        current_durables=[forged_current],
        allow_declared_cutover_migrations=True,
    )

    assert previous["status"] == "SAFE_TO_RECONCILE_IN_PLACE"
    assert current["status"] == "SAFE_ALREADY_ONE"
    assert continuity["status"] == "INVALIDATED"
    assert continuity["authorized_durable_migrations"] == []
