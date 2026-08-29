from pathlib import Path

import pytest

from scripts import check_nats_durable_cutover as cutover


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_broker_duration_rejects_non_finite_values(value: float) -> None:
    with pytest.raises(RuntimeError, match="nats_cutover_malformed_stream_state"):
        cutover._duration_seconds(value)


def test_broker_duration_rejects_integer_that_cannot_be_finitely_projected() -> None:
    with pytest.raises(RuntimeError, match="nats_cutover_malformed_stream_state"):
        cutover._duration_seconds(10**10_000)


@pytest.mark.parametrize("value", ("nan", "NaN", "inf", "+inf", "-inf", "1e999"))
def test_target_float_override_rejects_non_finite_values(
    tmp_path: Path,
    value: str,
) -> None:
    env_file = tmp_path / ".env.derivatives"
    env_file.write_text(
        f"AATS_NATS_EVENTS_MAX_AGE_SECONDS={value}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match="nats_cutover_invalid_target_env_override",
    ):
        cutover.load_target_stream_manifest(env_file)
