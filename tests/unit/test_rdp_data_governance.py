from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from aats.api.rdp_data_governance import (
    _CONTRACT_ELIGIBILITY_SQL,
    _build_cached_snapshot,
    _eligibility,
    _monitoring,
    build_data_governance_snapshot,
)


class _MappingResult:
    def __init__(self, *, rows=None, row=None) -> None:
        self._rows = list(rows or [])
        self._row = row

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        assert self._row is not None
        return self._row


class _EligibilityConnection:
    def __init__(self, *, states, aggregate) -> None:
        self.states = states
        self.aggregate = aggregate
        self.aggregate_parameters = None

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if "GROUP BY eligibility_mode, status" in sql:
            return _MappingResult(rows=self.states)
        if "WITH bundle_contract_state AS" in sql:
            self.aggregate_parameters = parameters
            return _MappingResult(row=self.aggregate)
        raise AssertionError(f"unexpected SQL: {sql}")


class _MonitoringConnection:
    def execute(self, statement, parameters=None):
        assert parameters is None
        sql = str(statement)
        if "FROM meta.collector_continuity_events" in sql:
            return _MappingResult(rows=[])
        if "AS archive_backlog" in sql:
            return _MappingResult(
                row={
                    "open_gaps": 0,
                    "archive_backlog": 0,
                    "archive_failures": 0,
                    "rebuild_failures": 0,
                }
            )
        raise AssertionError(f"unexpected SQL: {sql}")


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


def test_eligibility_keeps_raw_truth_but_blocks_legacy_derivative_bundle() -> None:
    connection = _EligibilityConnection(
        states=[
            {
                "eligibility_mode": "historical_research",
                "status": "ELIGIBLE",
                "bundle_count": 2,
            }
        ],
        aggregate={
            "bundle_total": 2,
            "raw_source_eligible": 2,
            "contract_aware_eligible": 1,
            "legacy_unbound": 1,
            "legacy_derivative_unbound": 1,
            "contract_binding_failed": 0,
        },
    )

    eligibility = _eligibility(connection)

    assert eligibility["status"] == "blocked"
    assert eligibility["bundle_count"] == 2
    assert eligibility["states"][0]["status"] == "ELIGIBLE"
    assert eligibility["raw_source"] == {
        "status": "available",
        "eligible_bundle_count": 2,
        "ineligible_bundle_count": 0,
        "eligibility_ratio": 1.0,
        "supports_monetary_research": False,
        "meaning": "仅证明既有来源、覆盖、因果与校验和资格，不证明衍生品金额单位正确",
    }
    assert eligibility["research_usable"] is False
    assert eligibility["contract_aware"]["eligible_bundle_count"] == 1
    assert (
        eligibility["contract_aware"]["legacy_derivative_unbound_bundle_count"]
        == 1
    )
    assert eligibility["contract_aware"]["eligibility_ratio"] == 0.5
    assert eligibility["reason_codes"] == [
        "legacy_derivative_contract_metadata_unbound",
        "instrument_contract_binding_report_missing",
    ]
    assert connection.aggregate_parameters == {
        "binding_policy_version": "instrument-contract-binding-v1",
        "supported_spot_symbols": ["BTC-USDT", "ETH-USDT"],
        "supported_swap_symbols": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    }


def test_contract_eligibility_sql_uses_explicit_supported_symbol_sets() -> None:
    assert ":supported_spot_symbols" in _CONTRACT_ELIGIBILITY_SQL
    assert ":supported_swap_symbols" in _CONTRACT_ELIGIBILITY_SQL
    assert "UPPER(BTRIM(" in _CONTRACT_ELIGIBILITY_SQL
    assert "'^[A-Z0-9]+-[A-Z0-9]+$'" not in _CONTRACT_ELIGIBILITY_SQL
    assert "'^[A-Z0-9]+-[A-Z0-9]+-SWAP$'" not in _CONTRACT_ELIGIBILITY_SQL


def test_eligibility_reports_unproven_instrument_scope_explicitly() -> None:
    connection = _EligibilityConnection(
        states=[
            {
                "eligibility_mode": "historical_research",
                "status": "ELIGIBLE",
                "bundle_count": 1,
            }
        ],
        aggregate={
            "bundle_total": 1,
            "raw_source_eligible": 1,
            "contract_aware_eligible": 0,
            "legacy_unbound": 1,
            "legacy_derivative_unbound": 0,
            "unsupported_instrument_scope": 1,
            "contract_binding_failed": 0,
        },
    )

    eligibility = _eligibility(connection)

    assert eligibility["status"] == "blocked"
    assert "instrument_scope_unsupported_or_unproven" in eligibility["reason_codes"]
    assert (
        eligibility["contract_aware"][
            "unsupported_instrument_scope_bundle_count"
        ]
        == 1
    )


def test_eligibility_only_reports_available_when_every_raw_eligible_bundle_is_bound() -> None:
    connection = _EligibilityConnection(
        states=[
            {
                "eligibility_mode": "historical_research",
                "status": "ELIGIBLE",
                "bundle_count": 2,
            }
        ],
        aggregate={
            "bundle_total": 2,
            "raw_source_eligible": 2,
            "contract_aware_eligible": 2,
            "legacy_unbound": 0,
            "legacy_derivative_unbound": 0,
            "contract_binding_failed": 0,
        },
    )

    eligibility = _eligibility(connection)

    assert eligibility["status"] == "available"
    assert eligibility["research_usable"] is False
    assert eligibility["eligibility_scope"] == "dataset_bundle_only"
    assert eligibility["contract_aware"]["research_usable"] is False
    assert eligibility["contract_aware"]["contract_bound_bundle_available"] is True
    assert eligibility["contract_aware"]["supports_monetary_research"] is False
    assert eligibility["reason_codes"] == []
    assert eligibility["contract_aware"]["blocked_bundle_count"] == 0
    assert eligibility["contract_aware"]["eligibility_ratio"] == 1.0
    assert "contract-aware eligible" in eligibility["next_action"]


def test_eligibility_distinguishes_missing_evidence_from_valid_zero() -> None:
    connection = _EligibilityConnection(
        states=[],
        aggregate={
            "bundle_total": 0,
            "raw_source_eligible": 0,
            "contract_aware_eligible": 0,
            "legacy_unbound": 0,
            "legacy_derivative_unbound": 0,
            "contract_binding_failed": 0,
        },
    )

    eligibility = _eligibility(connection)

    assert eligibility["status"] == "unknown"
    assert eligibility["research_usable"] is False
    assert eligibility["raw_source"]["eligibility_ratio"] is None
    assert eligibility["contract_aware"]["eligibility_ratio"] is None
    assert eligibility["reason_codes"] == ["dataset_bundle_evidence_missing"]


def test_eligibility_surfaces_present_but_invalid_contract_binding() -> None:
    connection = _EligibilityConnection(
        states=[
            {
                "eligibility_mode": "historical_research",
                "status": "ELIGIBLE",
                "bundle_count": 1,
            }
        ],
        aggregate={
            "bundle_total": 1,
            "raw_source_eligible": 1,
            "contract_aware_eligible": 0,
            "legacy_unbound": 0,
            "legacy_derivative_unbound": 0,
            "contract_binding_failed": 1,
        },
    )

    eligibility = _eligibility(connection)

    assert eligibility["status"] == "blocked"
    assert eligibility["research_usable"] is False
    assert eligibility["reason_codes"] == ["instrument_contract_binding_failed"]
    assert eligibility["contract_aware"]["binding_failed_bundle_count"] == 1


def test_monitoring_promotes_legacy_derivative_contract_gap_to_critical() -> None:
    eligibility = {
        "bundle_count": 2,
        "raw_source": {
            "eligible_bundle_count": 2,
            "eligibility_ratio": 1.0,
        },
        "contract_aware": {
            "eligibility_ratio": 0.5,
            "raw_eligible_but_contract_blocked_count": 1,
            "legacy_derivative_unbound_bundle_count": 1,
        },
    }

    monitoring = _monitoring(
        _MonitoringConnection(),
        Path.cwd(),
        eligibility=eligibility,
    )

    alert_codes = {alert["code"] for alert in monitoring["alerts"]}
    assert monitoring["status"] == "critical"
    assert "derivative_bundle_contract_metadata_unbound" in alert_codes
    assert monitoring["eligibility_ratio"] == 1.0
    assert monitoring["contract_aware_eligibility_ratio"] == 0.5


def test_monitoring_promotes_invalid_contract_binding_to_critical() -> None:
    eligibility = {
        "bundle_count": 1,
        "raw_source": {
            "eligible_bundle_count": 1,
            "eligibility_ratio": 1.0,
        },
        "contract_aware": {
            "eligibility_ratio": 0.0,
            "raw_eligible_but_contract_blocked_count": 1,
            "legacy_derivative_unbound_bundle_count": 0,
            "binding_failed_bundle_count": 1,
        },
    }

    monitoring = _monitoring(
        _MonitoringConnection(),
        Path.cwd(),
        eligibility=eligibility,
    )

    alert_codes = {alert["code"] for alert in monitoring["alerts"]}
    assert monitoring["status"] == "critical"
    assert "bundle_contract_binding_failed" in alert_codes
