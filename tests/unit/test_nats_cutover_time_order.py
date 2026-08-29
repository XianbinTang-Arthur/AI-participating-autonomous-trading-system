import pytest

from scripts import check_nats_durable_cutover as cutover


def _reference(*, started_ns: int, ended_ns: int) -> dict[str, object]:
    # Text timestamps are informational here.  Cross-stage ordering must use
    # the exact integer fields so sub-microsecond boundaries are not rounded.
    return {
        "window_started_ns": started_ns,
        "window_ended_ns": ended_ns,
        "window_started_at_utc": "2026-08-28T00:00:00Z",
        "window_ended_at_utc": "2026-08-28T00:00:00Z",
    }


def test_preflight_window_order_rejects_sub_microsecond_overlap() -> None:
    previous_start_ns = 1_777_777_777_000_000_123
    previous_end_ns = 1_777_777_777_000_000_987

    with pytest.raises(
        RuntimeError,
        match="nats_cutover_preflight_time_window_overlap",
    ):
        cutover.validate_preflight_window_order(
            previous_reference=_reference(
                started_ns=previous_start_ns,
                ended_ns=previous_end_ns,
            ),
            current_window_started_ns=previous_end_ns - 1,
        )


def test_preflight_window_order_accepts_exact_adjacent_nanosecond_boundary() -> None:
    previous_start_ns = 1_777_777_777_000_000_123
    previous_end_ns = 1_777_777_777_000_000_987

    cutover.validate_preflight_window_order(
        previous_reference=_reference(
            started_ns=previous_start_ns,
            ended_ns=previous_end_ns,
        ),
        current_window_started_ns=previous_end_ns,
    )


def test_preflight_window_order_distinguishes_clock_rollback() -> None:
    previous_start_ns = 1_777_777_777_000_000_123

    with pytest.raises(
        RuntimeError,
        match="nats_cutover_preflight_time_rollback",
    ):
        cutover.validate_preflight_window_order(
            previous_reference=_reference(
                started_ns=previous_start_ns,
                ended_ns=previous_start_ns + 1_000,
            ),
            current_window_started_ns=previous_start_ns - 1,
        )


@pytest.mark.parametrize(
    "reference",
    (
        _reference(started_ns=0, ended_ns=1),
        _reference(started_ns=2, ended_ns=1),
        {"window_started_ns": True, "window_ended_ns": 2},
        {"window_started_ns": 1, "window_ended_ns": "2"},
    ),
)
def test_preflight_window_order_rejects_malformed_authoritative_bounds(
    reference: dict[str, object],
) -> None:
    with pytest.raises(
        RuntimeError,
        match="nats_cutover_invalid_evidence_time_window",
    ):
        cutover.validate_preflight_window_order(
            previous_reference=reference,
            current_window_started_ns=3,
        )
