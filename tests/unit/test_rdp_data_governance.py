from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from aats.api.rdp_data_governance import (
    _build_cached_snapshot,
    build_data_governance_snapshot,
)


def test_snapshot_reads_latest_bounded_coverage_and_never_exposes_locator(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "artifacts/data_governance/coverage"
    directory.mkdir(parents=True)
    (directory / "coverage_20260826T010000Z.json").write_text(
        json.dumps(
            {
                "schema_version": "rdp-coverage-report-v1",
                "queried_at": "2026-08-26T01:00:00+00:00",
                "window": {
                    "start": "2026-08-25T00:00:00+00:00",
                    "end": "2026-08-26T00:00:00+00:00",
                },
                "algorithm_version": "rdp-coverage-v3",
                "result_fingerprint_sha256": "a" * 64,
                "summary": {"observed": 4, "missing": 2},
                "tables": [{}, {}],
                "database_url": "postgresql://must-not-leak",
            }
        ),
        encoding="utf-8",
    )
    database = {
        "status": "available",
        "historical_imports": {"status": "available", "recent": []},
        "live_collection": {"status": "available", "channels": []},
        "archives": {"status": "available"},
        "eligibility": {"status": "available"},
        "rebuilds": {"status": "available"},
        "sources": {"status": "available"},
        "gaps": {"status": "available"},
        "monitoring": {"status": "healthy", "alert_count": 0},
    }

    with patch("aats.api.rdp_data_governance._database_projection", return_value=database):
        snapshot = build_data_governance_snapshot(tmp_path)

    encoded = json.dumps(snapshot)
    assert snapshot["status"] == "ready"
    assert snapshot["coverage"]["algorithm_version"] == "rdp-coverage-v3"
    assert snapshot["coverage"]["table_count"] == 2
    assert "postgresql://" not in encoded
    assert "source_locator" not in encoded
    assert snapshot["safety"]["live_actions_available"] is False
    assert snapshot["monitoring"]["status"] == "healthy"


def test_snapshot_fails_closed_when_coverage_and_database_are_unavailable(
    tmp_path: Path,
) -> None:
    with patch(
        "aats.api.rdp_data_governance._database_projection",
        return_value={"status": "unknown", "reason_code": "OperationalError"},
    ):
        snapshot = build_data_governance_snapshot(tmp_path)

    assert snapshot["status"] == "unknown"
    assert snapshot["reason_codes"] == [
        "coverage_snapshot_unavailable",
        "data_governance_database_unavailable",
    ]
    assert snapshot["coverage"]["reason_code"] == "coverage_snapshot_missing"
    assert snapshot["historical_imports"]["status"] == "unknown"


def test_snapshot_cache_bounds_active_run_workspace_polling(tmp_path: Path) -> None:
    _build_cached_snapshot.cache_clear()
    database = {
        "status": "available",
        "monitoring": {"status": "healthy"},
    }
    with patch(
        "aats.api.rdp_data_governance._database_projection",
        return_value=database,
    ) as projection:
        first = build_data_governance_snapshot(tmp_path)
        second = build_data_governance_snapshot(tmp_path)

    assert projection.call_count == 1
    assert first == second
    assert first is not second
    assert first["cache_ttl_seconds"] == 30
