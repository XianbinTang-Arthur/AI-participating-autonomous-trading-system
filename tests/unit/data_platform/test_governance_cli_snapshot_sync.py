from __future__ import annotations

import json

import pytest

from aats.data_platform.governance import artifact_indexer, quality_monitor, round_manager
from aats.data_platform.governance import snapshot_db


def _index_payload() -> dict:
    return {
        "generated_at": "2026-08-26T15:00:00+00:00",
        "summary": {
            "total_artifacts": 0,
            "rounds": 0,
            "experiments": 0,
            "valid_manifests": 0,
            "total_rounds": 0,
            "phases_with_rounds": [],
        },
    }


@pytest.mark.parametrize(
    ("module", "argv", "builder_module", "builder_name", "snapshot_type", "output_name"),
    [
        (
            artifact_indexer,
            ["--validate"],
            "aats.data_platform.governance.artifact_index",
            "build_artifact_index",
            snapshot_db.SNAPSHOT_ARTIFACT_INDEX,
            "artifact_index.json",
        ),
        (
            round_manager,
            ["--refresh-index"],
            "aats.data_platform.governance.round_status",
            "build_active_round_index",
            snapshot_db.SNAPSHOT_ACTIVE_ROUND_INDEX,
            "active_round_index.json",
        ),
    ],
)
def test_governance_index_cli_writes_file_and_db_snapshot(
    monkeypatch,
    tmp_path,
    module,
    argv,
    builder_module,
    builder_name,
    snapshot_type,
    output_name,
) -> None:
    payload = _index_payload()
    persisted: list[tuple[str, dict]] = []
    source_module = __import__(builder_module, fromlist=[builder_name])

    monkeypatch.setattr(module, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(source_module, builder_name, lambda _root: payload)
    monkeypatch.setattr(
        snapshot_db,
        "save_governance_snapshot",
        lambda *, snapshot_type, payload: persisted.append(
            (snapshot_type, payload)
        )
        is None,
    )

    assert module.main(argv) == 0
    assert json.loads(
        (tmp_path / "artifacts/governance" / output_name).read_text(encoding="utf-8")
    ) == payload
    assert persisted == [(snapshot_type, payload)]


def test_quality_monitor_cli_writes_file_and_db_snapshot(monkeypatch, tmp_path) -> None:
    payload = {
        "generated_at": "2026-08-26T15:00:00+00:00",
        "summary": {
            "health": "degraded",
            "passed": 1,
            "total_checks": 2,
        },
    }
    persisted: list[tuple[str, dict]] = []

    monkeypatch.setattr(quality_monitor, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(quality_monitor, "run_quality_monitor", lambda _root: payload)
    monkeypatch.setattr(
        snapshot_db,
        "save_governance_snapshot",
        lambda *, snapshot_type, payload: persisted.append(
            (snapshot_type, payload)
        )
        is None,
    )

    assert quality_monitor.main(["--run"]) == 0
    assert json.loads(
        (
            tmp_path / "artifacts/governance/quality_monitor_summary.json"
        ).read_text(encoding="utf-8")
    ) == payload
    assert persisted == [(snapshot_db.SNAPSHOT_QUALITY_MONITOR, payload)]


def test_quality_monitor_cli_fails_closed_when_db_snapshot_write_fails(
    monkeypatch,
    tmp_path,
) -> None:
    payload = {
        "generated_at": "2026-08-26T15:00:00+00:00",
        "summary": {"health": "degraded", "passed": 1, "total_checks": 2},
    }
    monkeypatch.setattr(quality_monitor, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(quality_monitor, "run_quality_monitor", lambda _root: payload)
    monkeypatch.setattr(snapshot_db, "save_governance_snapshot", lambda **_kwargs: False)

    assert quality_monitor.main(["--run"]) == 1


@pytest.mark.parametrize(
    ("module", "argv", "builder_module", "builder_name"),
    [
        (
            artifact_indexer,
            ["--validate"],
            "aats.data_platform.governance.artifact_index",
            "build_artifact_index",
        ),
        (
            round_manager,
            ["--refresh-index"],
            "aats.data_platform.governance.round_status",
            "build_active_round_index",
        ),
    ],
)
def test_governance_index_cli_fails_closed_when_db_snapshot_write_fails(
    monkeypatch,
    tmp_path,
    module,
    argv,
    builder_module,
    builder_name,
) -> None:
    source_module = __import__(builder_module, fromlist=[builder_name])
    monkeypatch.setattr(module, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(source_module, builder_name, lambda _root: _index_payload())
    monkeypatch.setattr(snapshot_db, "save_governance_snapshot", lambda **_kwargs: False)

    assert module.main(argv) == 1
