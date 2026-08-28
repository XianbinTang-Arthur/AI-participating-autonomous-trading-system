from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aats.data_platform.decision_system import evidence_bundle
from aats.data_platform.governance.snapshot_db import (
    SNAPSHOT_ACTIVE_ROUND_INDEX,
    SNAPSHOT_ARTIFACT_INDEX,
    SNAPSHOT_QUALITY_MONITOR,
)


def _producer_cost_summary() -> dict[str, Any]:
    return {
        "total_candidates": 7,
        "full_fill_ratio": 0.75,
        "positive_edge_ratio": 0.6,
        "slippage": {"mean": 1.25, "p95": 2.5},
        "total_execution_cost": {"mean": 3.5, "p95": 5.0},
        "cost_adjusted_edge": {"mean": 4.25, "p95": 8.0},
        "model_version": "v1_bar_proxy",
    }


def _assert_canonical_projection(cost_summary: dict[str, Any]) -> None:
    assert cost_summary["total_candidates"] == 7
    assert cost_summary["full_fill_ratio"] == 0.75
    assert cost_summary["positive_edge_ratio"] == 0.6
    assert cost_summary["slippage_mean"] == 1.25
    assert cost_summary["total_cost_mean"] == 3.5
    assert cost_summary["cost_adjusted_edge_mean"] == 4.25
    # The projection adds aliases without discarding producer detail.
    assert cost_summary["slippage"] == {"mean": 1.25, "p95": 2.5}
    assert cost_summary["cost_adjusted_edge"] == {"mean": 4.25, "p95": 8.0}


def test_phase4_db_snapshot_projects_nested_cost_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _load_snapshot(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "round_id": "20260828_000000_deadbeef",
            "phase": "phase4",
            "status": "succeeded",
            "started_at": "2026-08-28T00:00:00+00:00",
            "replay_only": False,
            "manifest": {},
            "summary": {
                "combos": {
                    "independent_15m": {
                        "status": "succeeded",
                        "cost_summary": _producer_cost_summary(),
                    }
                }
            },
            "data_source": "db",
        }

    monkeypatch.setattr(
        evidence_bundle,
        "load_research_round_snapshot",
        _load_snapshot,
    )

    enriched = evidence_bundle._enrich_round_from_manifest(
        {"round_id": "20260828_000000_deadbeef"},
        "phase4",
        tmp_path,
        require_managed_db_truth=True,
    )

    assert captured["require_managed_db_truth"] is True
    assert enriched["data_source"] == "db"
    _assert_canonical_projection(
        enriched["combos"]["independent_15m"]["cost_summary"]
    )


def test_phase4_file_fallback_uses_same_cost_summary_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    round_dir = tmp_path / "execution_round"
    combo_dir = round_dir / "independent_15m"
    combo_dir.mkdir(parents=True)
    (combo_dir / "execution_cost_summary.json").write_text(
        json.dumps(_producer_cost_summary()),
        encoding="utf-8",
    )
    (round_dir / "round_manifest.json").write_text(
        json.dumps(
            {
                "round_id": "20260828_000001_cafebabe",
                "overall_status": "succeeded",
                "combos": [
                    {
                        "key": "independent_15m",
                        "status": "succeeded",
                        "run_dir": str(combo_dir),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        evidence_bundle,
        "load_research_round_snapshot",
        lambda **_kwargs: None,
    )

    enriched = evidence_bundle._enrich_round_from_manifest(
        {
            "round_id": "20260828_000001_cafebabe",
            "path": str(round_dir),
        },
        "phase4",
        tmp_path,
    )

    assert enriched["data_source"] == "file"
    _assert_canonical_projection(
        enriched["combos"]["independent_15m"]["cost_summary"]
    )


@pytest.mark.parametrize(
    ("data_source", "canonical", "evidence_source"),
    [
        ("db", True, "governance_index"),
        ("db_bootstrap", False, "governance_index_db_bootstrap"),
        ("file_fallback", False, "governance_index_file_fallback"),
    ],
)
def test_build_evidence_bundle_only_marks_db_snapshots_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_source: str,
    canonical: bool,
    evidence_source: str,
) -> None:
    calls: list[tuple[str, bool]] = []

    def _load_governance_snapshot(
        _project_root: Path,
        *,
        snapshot_type: str,
        require_managed_db_truth: bool = False,
    ) -> dict[str, Any]:
        calls.append((snapshot_type, require_managed_db_truth))
        if snapshot_type == SNAPSHOT_ACTIVE_ROUND_INDEX:
            return {"data_source": data_source, "all_rounds": []}
        if snapshot_type == SNAPSHOT_ARTIFACT_INDEX:
            return {
                "data_source": data_source,
                "artifacts": [],
                "summary": {"total_artifacts": 0},
            }
        assert snapshot_type == SNAPSHOT_QUALITY_MONITOR
        return {"data_source": data_source, "summary": {}}

    monkeypatch.setattr(
        evidence_bundle,
        "load_governance_snapshot",
        _load_governance_snapshot,
    )
    monkeypatch.setattr(
        evidence_bundle,
        "_collect_latest_step2_round_diags",
        lambda *_args, **_kwargs: (False, [], None, None),
    )
    monkeypatch.setattr(
        evidence_bundle,
        "load_registry",
        lambda *_args, **_kwargs: {"parameter_sets": []},
    )

    bundle = evidence_bundle.build_evidence_bundle(tmp_path)

    assert calls == [
        (SNAPSHOT_ARTIFACT_INDEX, True),
        (SNAPSHOT_ACTIVE_ROUND_INDEX, True),
        (SNAPSHOT_QUALITY_MONITOR, True),
    ]
    assert bundle["governance_index_used"] == {
        "artifact_index": canonical,
        "active_round_index": canonical,
    }
    assert bundle["governance_index_data_source"] == {
        "artifact_index": data_source,
        "active_round_index": data_source,
    }
    assert bundle["phase3_evidence"]["evidence_source"] == evidence_source
    assert bundle["phase4_evidence"]["evidence_source"] == evidence_source


def test_formal_phase4_rejects_non_db_exact_round_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _load_round(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "round_id": "20260828_000002_feedface",
            "status": "succeeded",
            "started_at": "2026-08-28T00:00:00+00:00",
            "summary": {"combos": {}},
            "manifest": {},
            "data_source": "file_fallback",
        }

    monkeypatch.setattr(
        evidence_bundle,
        "load_research_round_snapshot",
        _load_round,
    )
    active_round_index = {
        "data_source": "db",
        "all_rounds": [
            {
                "round_id": "20260828_000002_feedface",
                "phase": "phase4",
                "status": "succeeded",
            }
        ],
    }

    evidence = evidence_bundle.collect_phase4_evidence(
        tmp_path,
        active_round_index=active_round_index,
        require_managed_db_truth=True,
        expected_round_id="20260828_000002_feedface",
    )

    assert captured["require_managed_db_truth"] is True
    assert evidence["round_snapshot_data_source"] == "file_fallback"
    assert evidence["evidence_source"] == (
        "governance_index_round_file_fallback"
    )
    assert evidence["round_selection_error"] == "round_snapshot_not_db_truth"
    assert evidence["trusted_round_count"] == 0
    assert evidence["latest_round"] is None


def test_formal_phase4_rejects_non_db_active_round_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_round_load(**_kwargs: Any) -> None:
        raise AssertionError("degraded active index must not resolve exact rounds")

    monkeypatch.setattr(
        evidence_bundle,
        "load_research_round_snapshot",
        _unexpected_round_load,
    )
    active_round_index = {
        "data_source": "db_bootstrap",
        "all_rounds": [
            {
                "round_id": "20260828_000003_baadf00d",
                "phase": "phase4",
                "status": "succeeded",
            }
        ],
    }

    evidence = evidence_bundle.collect_phase4_evidence(
        tmp_path,
        active_round_index=active_round_index,
        require_managed_db_truth=True,
    )

    assert evidence["evidence_source"] == "governance_index_db_bootstrap"
    assert evidence["round_count"] == 1
    assert evidence["trusted_round_count"] == 0
    assert evidence["skipped_untrusted"] == 1
    assert evidence["latest_round"] is None


def test_phase4_expected_round_selects_exact_indexed_db_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_round_id = "20260827_120000_deadbeef"
    loaded_round_ids: list[str] = []

    def _load_round(**kwargs: Any) -> dict[str, Any]:
        loaded_round_ids.append(kwargs["round_id"])
        return {
            "round_id": kwargs["round_id"],
            "phase": "phase4",
            "status": "succeeded",
            "started_at": "2026-08-27T12:00:00+00:00",
            "summary": {
                "combos": {
                    "independent_15m": {
                        "status": "succeeded",
                        "cost_summary": _producer_cost_summary(),
                    }
                }
            },
            "manifest": {},
            "data_source": "db",
        }

    monkeypatch.setattr(
        evidence_bundle,
        "load_research_round_snapshot",
        _load_round,
    )
    active_round_index = {
        "data_source": "db",
        "all_rounds": [
            {
                "round_id": "20260828_120000_cafebabe",
                "phase": "phase4",
                "status": "succeeded",
                "started_at": "2026-08-28T12:00:00+00:00",
            },
            {
                "round_id": expected_round_id,
                "phase": "phase4",
                "status": "succeeded",
                "started_at": "2026-08-27T12:00:00+00:00",
            },
        ],
    }

    evidence = evidence_bundle.collect_phase4_evidence(
        tmp_path,
        active_round_index=active_round_index,
        expected_round_id=expected_round_id,
    )

    assert loaded_round_ids == [expected_round_id]
    assert evidence["round_selection"] == "exact"
    assert evidence["round_selection_error"] is None
    assert evidence["latest_round"]["round_id"] == expected_round_id
    _assert_canonical_projection(
        evidence["latest_round"]["combos"]["independent_15m"][
            "cost_summary"
        ]
    )


def test_phase4_expected_round_must_be_in_canonical_active_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_round_load(**_kwargs: Any) -> None:
        raise AssertionError("a missing indexed round must not be loaded")

    monkeypatch.setattr(
        evidence_bundle,
        "load_research_round_snapshot",
        _unexpected_round_load,
    )
    evidence = evidence_bundle.collect_phase4_evidence(
        tmp_path,
        active_round_index={"data_source": "db", "all_rounds": []},
        expected_round_id="20260828_120000_deadbeef",
    )

    assert evidence["round_selection_error"] == "expected_round_not_indexed"
    assert evidence["trusted_round_count"] == 0
    assert evidence["latest_round"] is None
