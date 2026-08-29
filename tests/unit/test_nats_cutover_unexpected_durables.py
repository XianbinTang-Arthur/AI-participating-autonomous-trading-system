from datetime import datetime, timezone

from scripts import check_nats_durable_cutover as cutover


def _state(durable: str) -> cutover.ConsumerState:
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
            "window": {"current_max_ack_pending": 1},
            "outstanding": {"num_ack_pending": 0},
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
    assert payload["recovery"]["instruction_code"].startswith(
        "nats_durable_cutover_requires_approved"
    )
