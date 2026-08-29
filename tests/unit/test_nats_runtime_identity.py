import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts import check_nats_durable_cutover as cutover
from scripts import nats_runtime_identity as identity


def _container_output(
    *,
    failing_streak: str = "0",
    restart_count: str = "0",
    compose_project: str = identity.NATS_COMPOSE_PROJECT,
    compose_service: str = identity.NATS_COMPOSE_SERVICE,
    configured_image_reference: str = identity.NATS_EXPECTED_IMAGE,
    mount_lines: tuple[str, ...] | None = None,
) -> str:
    lines = [
        json.dumps("a" * 64),
        json.dumps("sha256:" + "b" * 64),
        json.dumps(configured_image_reference),
        json.dumps("running"),
        json.dumps("healthy"),
        failing_streak,
        json.dumps("2026-08-28T00:00:00.123456789Z"),
        restart_count,
        json.dumps(compose_project),
        json.dumps(compose_service),
    ]
    lines.extend(
        mount_lines
        if mount_lines is not None
        else (
            json.dumps("volume"),
            json.dumps(identity.NATS_VOLUME),
            json.dumps(True),
        )
    )
    return "\n".join(lines) + "\n"


def _volume_output(
    *,
    labels: dict[str, str] | None = None,
    driver: str = "local",
    scope: str = "local",
    options: dict[str, str] | None = None,
) -> str:
    label_facts = (
        labels
        if labels is not None
        else {
            "com.docker.compose.project": identity.NATS_COMPOSE_PROJECT,
            "com.docker.compose.volume": identity.NATS_COMPOSE_VOLUME,
        }
    )
    return "\n".join(
        (
            json.dumps(identity.NATS_VOLUME),
            json.dumps(driver),
            json.dumps(scope),
            json.dumps("2026-08-28T00:00:00Z"),
            json.dumps(options),
            json.dumps(label_facts.get("com.docker.compose.project")),
            json.dumps(label_facts.get("com.docker.compose.volume")),
            json.dumps(label_facts.get("com.aats.bootstrap_lock")),
        )
    )


def _health_output(
    *,
    checks: tuple[tuple[str, str, int], ...] = (
        (
            "2026-08-28T00:01:20Z",
            "2026-08-28T00:01:21Z",
            0,
        ),
    ),
    failing_streak: int = 0,
    restart_count: int = 0,
) -> str:
    return json.dumps(
        {
            "Id": "a" * 64,
            "RestartCount": restart_count,
            "State": {
                "Status": "running",
                "Health": {
                    "Status": "healthy",
                    "FailingStreak": failing_streak,
                    "Checks": [
                        {"Start": start, "End": end, "ExitCode": exit_code}
                        for start, end, exit_code in checks
                    ],
                },
            },
        },
        separators=(",", ":"),
    )


def test_canonical_fingerprint_is_mapping_order_independent() -> None:
    left = {
        "schema": "example.v1",
        "nested": {"z": 2, "a": 1},
    }
    right = {
        "nested": {"a": 1, "z": 2},
        "schema": "example.v1",
    }

    assert identity.canonical_fingerprint(left) == identity.canonical_fingerprint(
        right
    )


def test_capture_snapshot_uses_one_shared_container_and_volume_projection() -> None:
    calls: list[tuple[str, ...]] = []

    def run(command) -> str:
        command = tuple(command)
        calls.append(command)
        if command == (
            "docker",
            "image",
            "inspect",
            identity.NATS_EXPECTED_IMAGE,
            "--format",
            "{{.Id}}",
        ):
            return "sha256:" + "b" * 64
        if command == (
            "docker",
            "inspect",
            "--format",
            identity.NATS_CONTAINER_INSPECT_TEMPLATE,
            identity.NATS_CONTAINER,
        ):
            return _container_output(restart_count="7")
        if command == (
            "docker",
            "volume",
            "inspect",
            "--format",
            identity.NATS_VOLUME_INSPECT_TEMPLATE,
            identity.NATS_VOLUME,
        ):
            return _volume_output()
        raise AssertionError(command)

    snapshot = identity.capture_nats_runtime_snapshot(run)

    assert snapshot["container_id"] == "a" * 64
    # Historical restart count is an identity fact.  Stage callers, not the
    # shared parser, decide whether a particular boundary requires zero.
    assert snapshot["restart_count"] == 7
    assert str(snapshot["fingerprint"]).startswith("sha256:")
    assert str(snapshot["volume_fingerprint"]).startswith("sha256:")
    assert len(calls) == 3


def test_cutover_and_deploy_reference_the_shared_identity_contract() -> None:
    assert cutover.capture_nats_identity is identity.capture_nats_identity
    assert (
        cutover.capture_nats_volume_fingerprint
        is identity.capture_nats_volume_fingerprint
    )
    deploy_source = (
        Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh"
    ).read_text(encoding="utf-8")
    compose_source = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "wsl2-dev"
        / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    assert identity.NATS_EXPECTED_IMAGE in deploy_source
    assert f"image: {identity.NATS_EXPECTED_IMAGE}" in compose_source
    bootstrap_body = deploy_source.split(
        "capture_nats_cutover_bootstrap_fingerprint() {", 1
    )[1].split("\n}", 1)[0]
    assert (
        "~/aats-venv/bin/python scripts/nats_runtime_identity.py "
        "snapshot --format tsv"
    ) in bootstrap_body
    assert "{{.Image}}" not in bootstrap_body
    assert "sha256sum" not in bootstrap_body


@pytest.mark.parametrize(
    ("output", "case"),
    (
        (_container_output(failing_streak="1"), "failing streak"),
        (_container_output(compose_project="other"), "wrong project"),
        (_container_output(compose_service="other"), "wrong service"),
        (
            _container_output(configured_image_reference="nats:latest"),
            "wrong configured image",
        ),
        (_container_output(mount_lines=()), "missing data mount"),
        (
            _container_output(
                mount_lines=(
                    json.dumps("volume"),
                    json.dumps(identity.NATS_VOLUME),
                    json.dumps(True),
                    json.dumps("volume"),
                    json.dumps(identity.NATS_VOLUME),
                    json.dumps(True),
                )
            ),
            "multiple data mounts",
        ),
        (
            _container_output(
                mount_lines=(
                    json.dumps("bind"),
                    json.dumps(identity.NATS_VOLUME),
                    json.dumps(True),
                )
            ),
            "non-volume data mount",
        ),
        (
            _container_output(
                mount_lines=(
                    json.dumps("volume"),
                    json.dumps("wrong_volume"),
                    json.dumps(True),
                )
            ),
            "wrong volume",
        ),
        (
            _container_output(
                mount_lines=(
                    json.dumps("volume"),
                    json.dumps(identity.NATS_VOLUME),
                    json.dumps(False),
                )
            ),
            "read-only data mount",
        ),
        (_container_output() + json.dumps("extra") + "\n", "extra field"),
        (_container_output(restart_count="01"), "malformed integer"),
    ),
)
def test_container_identity_fails_closed(output: str, case: str) -> None:
    with pytest.raises(
        RuntimeError,
        match="nats_runtime_invalid_container_identity",
    ):
        identity.parse_nats_container_identity(output)


def test_capture_identity_rejects_actual_image_id_not_resolved_from_pin() -> None:
    def run(command) -> str:
        command = tuple(command)
        if command[:2] == ("docker", "inspect"):
            return _container_output()
        if command[:3] == ("docker", "image", "inspect"):
            return "sha256:" + "c" * 64
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="nats_runtime_container_image_not_pinned"):
        identity.capture_nats_identity(run)


def test_nats_health_rejects_failure_after_boundary_even_after_recovery() -> None:
    boundary_ns = int(datetime.fromisoformat("2026-08-28T00:01:02+00:00").timestamp() * 1_000_000_000)
    recovered = _health_output(
        checks=(
            (
                "2026-08-28T00:01:10Z",
                "2026-08-28T00:01:11Z",
                1,
            ),
            (
                "2026-08-28T00:01:20Z",
                "2026-08-28T00:01:21Z",
                0,
            ),
        )
    )

    with pytest.raises(RuntimeError, match="nats_runtime_health_failed_after_boundary"):
        identity.parse_nats_health_snapshot(
            recovered,
            health_window_started_ns=boundary_ns,
            require_success_after_boundary=True,
        )


def test_nats_health_log_is_separate_from_stable_identity_fingerprint() -> None:
    assert ".State.Health.Log" not in identity.NATS_CONTAINER_INSPECT_TEMPLATE
    assert ".State.Health.Log" in identity.NATS_HEALTH_INSPECT_TEMPLATE

    parsed = identity.parse_nats_health_snapshot(
        _health_output(),
        health_window_started_ns=int(
            datetime.fromisoformat("2026-08-28T00:01:02+00:00").timestamp()
            * 1_000_000_000
        ),
        expected_container_id="a" * 64,
        require_success_after_boundary=True,
    )

    assert parsed["health_checks_observed_after_boundary"] == 1
    assert parsed["last_health_exit_code"] == 0


def test_volume_fingerprint_canonicalizes_label_order() -> None:
    first = {
        "com.docker.compose.project": identity.NATS_COMPOSE_PROJECT,
        "com.docker.compose.volume": identity.NATS_COMPOSE_VOLUME,
        "com.aats.bootstrap_lock": "lock-1",
    }
    second = dict(reversed(tuple(first.items())))

    assert identity.parse_nats_volume_identity(
        _volume_output(labels=first)
    ).fingerprint == identity.parse_nats_volume_identity(
        _volume_output(labels=second)
    ).fingerprint


def test_volume_identity_normalizes_docker_missing_label_empty_string() -> None:
    lines = _volume_output().splitlines()
    lines[7] = json.dumps("")

    parsed = identity.parse_nats_volume_identity("\n".join(lines))

    assert "com.aats.bootstrap_lock" not in parsed.labels
    assert parsed.fingerprint == identity.parse_nats_volume_identity(
        _volume_output()
    ).fingerprint


@pytest.mark.parametrize(
    "labels",
    (
        {},
        {"com.docker.compose.project": identity.NATS_COMPOSE_PROJECT},
        {
            "com.docker.compose.project": "other",
            "com.docker.compose.volume": identity.NATS_COMPOSE_VOLUME,
        },
    ),
)
def test_volume_identity_requires_standard_compose_labels(
    labels: dict[str, str],
) -> None:
    with pytest.raises(RuntimeError, match="nats_runtime_invalid_volume_identity"):
        identity.parse_nats_volume_identity(_volume_output(labels=labels))


@pytest.mark.parametrize(
    ("driver", "scope", "field"),
    (
        ("nfs", "local", "driver"),
        ("local", "global", "scope"),
    ),
)
def test_volume_identity_requires_standard_local_driver_and_scope(
    driver: str,
    scope: str,
    field: str,
) -> None:
    with pytest.raises(
        RuntimeError,
        match=rf"nats_runtime_invalid_volume_identity:{field}",
    ):
        identity.parse_nats_volume_identity(
            _volume_output(driver=driver, scope=scope)
        )


@pytest.mark.parametrize(
    "options",
    (
        {"type": "none", "device": "/host/path", "o": "bind"},
        {"device": "server:/export", "type": "nfs"},
    ),
)
def test_volume_identity_rejects_non_default_local_driver_options(
    options: dict[str, str],
) -> None:
    with pytest.raises(
        RuntimeError,
        match="nats_runtime_invalid_volume_identity:options",
    ):
        identity.parse_nats_volume_identity(_volume_output(options=options))


def test_volume_inspect_template_selects_only_safe_labels() -> None:
    assert "{{json .Labels}}" not in identity.NATS_VOLUME_INSPECT_TEMPLATE
    assert "com.docker.compose.project" in identity.NATS_VOLUME_INSPECT_TEMPLATE
    assert "com.docker.compose.volume" in identity.NATS_VOLUME_INSPECT_TEMPLATE
    assert "com.aats.bootstrap_lock" in identity.NATS_VOLUME_INSPECT_TEMPLATE
