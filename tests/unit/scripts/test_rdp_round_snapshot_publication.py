from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from aats.data_platform.attribution import report_builder as attribution_report_builder
from aats.data_platform.execution_realism import aggregation as execution_aggregation
from aats.data_platform.execution_realism import report_builder as execution_report_builder
from scripts import (
    rdp_run_phase3_round as phase3,
    rdp_run_phase4_round as phase4,
    rdp_run_step2_research as step2,
)


def _write_report(*args: Any, **kwargs: Any) -> None:
    output_path = kwargs.get("output_path")
    if output_path is None:
        output_path = args[-1]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test report\n", encoding="utf-8")


def _single_phase3_result(
    family: str,
    timeframe: str,
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "family": family,
        "timeframe": timeframe,
        "status": "succeeded",
        "run_dir": "/test/phase3",
        "child_result_ref": {},
        "attribution_summary": [],
        "top_failure_modes": {},
        "alignment_stats": {},
        "live_query_succeeded": False,
        "_alignment_rows": [],
    }


def _single_phase4_result(
    family: str,
    timeframe: str,
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "family": family,
        "timeframe": timeframe,
        "status": "succeeded",
        "run_dir": "/test/phase4",
        "child_result_ref": {},
        "cost_summary": {},
        "slippage_rows": [],
        "alignment_stats": {},
    }


def _only_round_dir(artifact_root: Path) -> Path:
    round_dirs = [path for path in artifact_root.iterdir() if path.is_dir()]
    assert len(round_dirs) == 1
    return round_dirs[0]


@pytest.mark.parametrize(
    ("snapshot_saved", "managed", "expected_exit", "marker_published"),
    [
        (False, True, 3, False),
        (False, False, 0, True),
        (True, True, 0, True),
    ],
)
def test_step2_snapshot_precedes_result_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot_saved: bool,
    managed: bool,
    expected_exit: int,
    marker_published: bool,
) -> None:
    artifact_root = tmp_path / "step2"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_step2_research.py",
            "--artifact-root",
            str(artifact_root),
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--skip-calibration",
            "--skip-scan",
            "--no-print-summary",
        ],
    )
    monkeypatch.setattr(step2, "_build_conclusion_report", _write_report)
    monkeypatch.setattr(
        step2,
        "_determine_step2_round_status",
        lambda **_kwargs: "succeeded",
    )
    monkeypatch.setattr(
        step2,
        "save_research_round_snapshot",
        lambda **_kwargs: snapshot_saved,
    )
    monkeypatch.setattr(
        step2,
        "has_explicit_governance_db_configuration",
        lambda _root: managed,
    )

    if expected_exit:
        with pytest.raises(SystemExit) as exc_info:
            step2.main()
        assert exc_info.value.code == expected_exit
    else:
        step2.main()

    round_dir = _only_round_dir(artifact_root)
    stdout = capsys.readouterr().out
    assert (step2._STEP2_RESULT_PREFIX in stdout) is marker_published
    assert (round_dir / "round_result.json").exists() is marker_published


@pytest.mark.parametrize(
    ("snapshot_saved", "managed", "expected_exit", "marker_published"),
    [
        (False, True, 3, False),
        (False, False, 0, True),
        (True, True, 0, True),
    ],
)
def test_phase3_snapshot_precedes_result_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot_saved: bool,
    managed: bool,
    expected_exit: int,
    marker_published: bool,
) -> None:
    artifact_root = tmp_path / "phase3"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_phase3_round.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--artifact-root",
            str(artifact_root),
            "--replay-only",
            "--no-print-summary",
        ],
    )
    monkeypatch.setattr(phase3, "load_parameter_candidate_lineage", lambda _path: {})
    monkeypatch.setattr(phase3, "_run_single_attribution", _single_phase3_result)
    monkeypatch.setattr(
        attribution_report_builder,
        "build_phase3_conclusion",
        _write_report,
    )
    monkeypatch.setattr(
        phase3,
        "save_research_round_snapshot",
        lambda **_kwargs: snapshot_saved,
    )
    monkeypatch.setattr(
        phase3,
        "has_explicit_governance_db_configuration",
        lambda _root: managed,
    )

    assert phase3.main() == expected_exit

    round_dir = _only_round_dir(artifact_root)
    stdout = capsys.readouterr().out
    assert (phase3._ROUND_RESULT_MARKER in stdout) is marker_published
    assert (round_dir / "round_result.json").exists() is marker_published


@pytest.mark.parametrize(
    ("snapshot_saved", "managed", "expected_exit", "marker_published"),
    [
        (False, True, 3, False),
        (False, False, 0, True),
        (True, True, 0, True),
    ],
)
def test_phase4_snapshot_precedes_result_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot_saved: bool,
    managed: bool,
    expected_exit: int,
    marker_published: bool,
) -> None:
    artifact_root = tmp_path / "phase4"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_phase4_round.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--artifact-root",
            str(artifact_root),
            "--no-print-summary",
        ],
    )
    monkeypatch.setattr(phase4, "load_parameter_candidate_lineage", lambda _path: {})
    monkeypatch.setattr(
        phase4,
        "_run_single_execution_realism",
        _single_phase4_result,
    )
    monkeypatch.setattr(
        execution_aggregation,
        "build_execution_realism_comparison",
        lambda _payload: [],
    )
    monkeypatch.setattr(
        execution_aggregation,
        "generate_cross_comparison_findings",
        lambda _rows: [],
    )
    monkeypatch.setattr(
        execution_report_builder,
        "build_phase4_conclusion",
        _write_report,
    )
    monkeypatch.setattr(
        phase4,
        "save_research_round_snapshot",
        lambda **_kwargs: snapshot_saved,
    )
    monkeypatch.setattr(
        phase4,
        "has_explicit_governance_db_configuration",
        lambda _root: managed,
    )

    assert phase4.main() == expected_exit

    round_dir = _only_round_dir(artifact_root)
    stdout = capsys.readouterr().out
    assert (phase4._ROUND_RESULT_MARKER in stdout) is marker_published
    assert (round_dir / "round_result.json").exists() is marker_published
