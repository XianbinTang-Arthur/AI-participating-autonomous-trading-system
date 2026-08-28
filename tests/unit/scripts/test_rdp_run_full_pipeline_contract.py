from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from aats.data_platform.governance.auto_import_candidates import (
    AUTO_IMPORT_SUCCESS_STATUSES,
)
from scripts import rdp_run_full_pipeline


def test_exit_two_is_partial_only_for_research_batch_phases(monkeypatch) -> None:
    monkeypatch.setattr(
        rdp_run_full_pipeline.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2),
    )

    phase4 = rdp_run_full_pipeline._run_phase("phase4", ["python", "phase4.py"])
    decision = rdp_run_full_pipeline._run_phase("decision", ["python", "decision.py"])

    assert phase4["status"] == "partial_success"
    assert decision["status"] == "failed"


def test_candidate_import_recovery_statuses_are_pipeline_success_contract() -> None:
    assert AUTO_IMPORT_SUCCESS_STATUSES <= (
        rdp_run_full_pipeline._SUCCESSFUL_IMPORT_STATUSES
    )
    for status in (
        "recovered_partial_import",
        "reconciled_import",
        "concurrent_transition_preserved",
    ):
        assert status in rdp_run_full_pipeline._SUCCESSFUL_IMPORT_STATUSES
    assert "round_content_conflict" not in (
        rdp_run_full_pipeline._SUCCESSFUL_IMPORT_STATUSES
    )
    assert "supersession_deferred" not in (
        rdp_run_full_pipeline._SUCCESSFUL_IMPORT_STATUSES
    )
    assert "import_lock_busy" not in (
        rdp_run_full_pipeline._SUCCESSFUL_IMPORT_STATUSES
    )


def test_pipeline_marker_rejects_duplicate_json_keys_and_non_finite_values() -> None:
    prefix = rdp_run_full_pipeline._STEP3_RESULT_PREFIX

    assert rdp_run_full_pipeline._extract_json_marker(
        prefix + '{"status":"succeeded","status":"failed"}',
        prefix,
    ) is None
    assert rdp_run_full_pipeline._extract_json_marker(
        prefix + '{"status":"succeeded","score":NaN}',
        prefix,
    ) is None


def test_decision_phase_captures_structured_business_result(monkeypatch) -> None:
    payload = {
        "round_id": "round_1",
        "readiness": "not_ready_attribution_issue",
        "research_outcome": "blocked_by_attribution",
    }
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_streaming_with_marker",
        lambda *_args, **_kwargs: (0, payload),
    )

    result = rdp_run_full_pipeline._run_phase(
        "decision",
        ["python", "decision.py"],
        result_prefix=rdp_run_full_pipeline._DECISION_RESULT_PREFIX,
    )

    assert result["status"] == "success"
    assert result["structured_result"] == payload


def test_decision_phase_fails_closed_when_result_marker_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_streaming_with_marker",
        lambda *_args, **_kwargs: (0, None),
    )

    result = rdp_run_full_pipeline._run_phase(
        "decision",
        ["python", "decision.py"],
        result_prefix=rdp_run_full_pipeline._DECISION_RESULT_PREFIX,
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 0
    assert "missing structured result marker" in result["error"]


def test_partial_research_phase_retains_structured_result(monkeypatch) -> None:
    payload = {"status": "partial_success", "round_id": "round_1"}
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_streaming_with_marker",
        lambda *_args, **_kwargs: (2, payload),
    )

    result = rdp_run_full_pipeline._run_phase(
        "step3",
        ["python", "step3.py"],
        result_prefix=rdp_run_full_pipeline._STEP3_RESULT_PREFIX,
    )

    assert result["status"] == "partial_success"
    assert result["structured_result"] == payload


def test_streaming_marker_runner_forwards_logs_and_requires_one_marker(
    capsys,
) -> None:
    prefix = "TEST_RESULT_JSON="
    script = (
        "import json; "
        "print('live-progress', flush=True); "
        f"print({prefix!r} + json.dumps({{'status': 'ok'}}), flush=True)"
    )
    return_code, marker = rdp_run_full_pipeline._run_streaming_with_marker(
        [sys.executable, "-c", script],
        result_prefix=prefix,
        child_env=None,
        timeout_s=10,
    )

    assert return_code == 0
    assert marker == {"status": "ok"}
    assert "live-progress" in capsys.readouterr().out

    duplicate_script = (
        "import json; "
        f"print({prefix!r} + json.dumps({{'status': 'one'}}), flush=True); "
        f"print({prefix!r} + json.dumps({{'status': 'two'}}), flush=True)"
    )
    return_code, marker = rdp_run_full_pipeline._run_streaming_with_marker(
        [sys.executable, "-c", duplicate_script],
        result_prefix=prefix,
        child_env=None,
        timeout_s=10,
    )
    assert return_code == 0
    assert marker is None


def test_research_dates_fail_before_any_phase_starts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["rdp_run_full_pipeline.py", "--stop-after", "phase2"],
    )
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("phase must not start"),
    )

    assert rdp_run_full_pipeline.main() == 2
    assert "研究阶段需要有效且递增" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (" ", "2026-08-27"),
        ("2026/08/01", "2026-08-27"),
        ("2026-08-27", "2026-08-27"),
        ("2026-08-28", "2026-08-27"),
    ],
)
def test_invalid_research_window_fails_before_phase_start(
    monkeypatch,
    start: str,
    end: str,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            start,
            "--end",
            end,
            "--stop-after",
            "phase2",
        ],
    )
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("phase must not start"),
    )

    assert rdp_run_full_pipeline.main() == 2


def test_governance_and_decision_only_dry_run_needs_no_research_dates(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--skip-phase2",
            "--skip-step3",
            "--skip-import-candidates",
            "--skip-phase3",
            "--skip-phase4",
            "--dry-run",
        ],
    )

    assert rdp_run_full_pipeline.main() == 0


def _research_marker(
    project_root: Path,
    *,
    phase: str,
    round_id: str,
    candidate_bytes: bytes,
    step2_marker: dict | None = None,
) -> dict:
    phase_dir = "step2_rounds" if phase == "step2" else "step3_rounds"
    candidate_name = (
        "parameter_candidates.json"
        if phase == "step2"
        else "parameter_candidates_merged.json"
    )
    round_dir = project_root / "artifacts/research" / phase_dir / round_id
    round_dir.mkdir(parents=True)
    candidate_path = round_dir / candidate_name
    candidate_path.write_bytes(candidate_bytes)
    marker = {
        "schema_version": f"aats.{phase}_result.v1",
        "phase": phase,
        "round_id": round_id,
        "round_dir": str(round_dir.resolve()),
        "candidate_path": str(candidate_path.resolve()),
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "status": "succeeded",
        "symbol": "BTC-USDT-SWAP",
        "dataset_version": "v1.0",
        "window": {"start": "2026-08-01", "end": "2026-08-27"},
    }
    if phase == "step3" and step2_marker is not None:
        marker["step2_round_id"] = step2_marker["round_id"]
        marker["step2_candidate_sha256"] = step2_marker["candidate_sha256"]
    (round_dir / "round_result.json").write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    return marker


def _phase34_marker(
    project_root: Path,
    *,
    phase: str,
    round_id: str,
    step3_marker: dict,
    replay_only: bool = False,
) -> dict:
    phase_dir = "attribution_rounds" if phase == "phase3" else "execution_rounds"
    round_dir = project_root / "artifacts/research" / phase_dir / round_id
    round_dir.mkdir(parents=True)
    source_round_id = step3_marker["round_id"]
    source_sha256 = step3_marker["candidate_sha256"]
    window = dict(step3_marker["window"])
    manifest = {
        "round_id": round_id,
        "phase": phase,
        "status": "succeeded",
        "overall_status": "succeeded",
        "symbol": "BTC-USDT-SWAP",
        "window": window,
        "scope": {"symbol": "BTC-USDT-SWAP", "window": window},
        "parameter_input": {
            "source_step3_round_id": source_round_id,
            "source_step3_candidate_sha256": source_sha256,
        },
        "combos": [
            {
                "key": key,
                "family": key.split("_", 1)[0],
                "timeframe": "15m" if key.endswith("15m") else "1H",
                "status": "succeeded",
                "source_step3_round_id": source_round_id,
                "source_step3_candidate_sha256": source_sha256,
            }
            for key in (
                "independent_15m",
                "independent_1h",
                "directional_15m",
                "directional_1h",
            )
        ],
    }
    manifest_path = round_dir / "round_manifest.json"
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    marker = {
        "schema_version": f"aats.{phase}_result.v1",
        "phase": phase,
        "round_id": round_id,
        "round_dir": str(round_dir.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_size_bytes": len(manifest_bytes),
        "status": "succeeded",
        "exit_code": 0,
        "symbol": "BTC-USDT-SWAP",
        "dataset_version": "v1.0",
        "window": window,
        "source_step3_round_id": source_round_id,
        "source_step3_candidate_sha256": source_sha256,
    }
    if phase == "phase3":
        marker["replay_only"] = replay_only
    (round_dir / "round_result.json").write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    return marker


def test_phase34_result_ref_rejects_duplicate_combo_topology(
    tmp_path: Path,
) -> None:
    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )
    marker = _phase34_marker(
        tmp_path,
        phase="phase3",
        round_id="20260827_130000_b1c2d3e4",
        step3_marker=step3_marker,
        replay_only=True,
    )
    manifest_path = Path(marker["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["combos"][-1] = dict(manifest["combos"][0])
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    marker["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    marker["manifest_size_bytes"] = len(manifest_bytes)
    result_path = Path(marker["round_dir"]) / "round_result.json"
    result_path.write_text(json.dumps(marker), encoding="utf-8")

    with pytest.raises(ValueError, match="phase34_result_manifest_topology_invalid"):
        rdp_run_full_pipeline._load_phase34_result_ref(
            str(result_path),
            phase="phase3",
            expected_dataset_version="v1.0",
            expected_symbol="BTC-USDT-SWAP",
            expected_window=step3_marker["window"],
            expected_step3=step3_marker,
            expected_replay_only=True,
            project_root=tmp_path,
        )


def test_decision_resume_passes_exact_phase34_round_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )
    phase3_marker = _phase34_marker(
        tmp_path,
        phase="phase3",
        round_id="20260827_130000_b1c2d3e4",
        step3_marker=step3_marker,
    )
    phase4_marker = _phase34_marker(
        tmp_path,
        phase="phase4",
        round_id="20260827_140000_c1d2e3f4",
        step3_marker=step3_marker,
    )
    seen: dict[str, object] = {}

    def _fake_run_phase(name: str, cmd: list[str], **_kwargs) -> dict:
        seen["name"] = name
        seen["cmd"] = cmd
        return {
            "phase": name,
            "status": "success",
            "exit_code": 0,
            "structured_result": {"round_id": "decision_round"},
        }

    monkeypatch.setattr(rdp_run_full_pipeline, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rdp_run_full_pipeline, "_run_phase", _fake_run_phase)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start-from",
            "decision",
            "--stop-after",
            "decision",
            "--step3-result-ref",
            str(Path(step3_marker["round_dir"]) / "round_result.json"),
            "--phase3-result-ref",
            str(Path(phase3_marker["round_dir"]) / "round_result.json"),
            "--phase4-result-ref",
            str(Path(phase4_marker["round_dir"]) / "round_result.json"),
        ],
    )

    assert rdp_run_full_pipeline.main() == 0
    assert seen["name"] == "decision"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--expected-step2-round-id") + 1] == step2_marker[  # type: ignore[union-attr]
        "round_id"
    ]
    assert cmd[cmd.index("--expected-phase3-round-id") + 1] == phase3_marker[  # type: ignore[union-attr]
        "round_id"
    ]
    assert cmd[cmd.index("--expected-phase4-round-id") + 1] == phase4_marker[  # type: ignore[union-attr]
        "round_id"
    ]


def test_full_pipeline_binds_one_exact_step2_step3_import_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aats.data_platform.governance import auto_import_candidates

    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3,"pipeline":"A"}',
        step2_marker=step2_marker,
    )
    global_latest_b = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_130000_deadbeef",
        candidate_bytes=b'{"step":3,"pipeline":"B"}',
        step2_marker=step2_marker,
    )
    seen: dict[str, object] = {}

    def _fake_run_phase(name: str, cmd: list[str], **_kwargs) -> dict:
        if name == "phase2":
            return {
                "phase": name,
                "status": "success",
                "exit_code": 0,
                "structured_result": step2_marker,
            }
        assert name == "step3"
        seen["step3_cmd"] = cmd
        return {
            "phase": name,
            "status": "success",
            "exit_code": 0,
            "structured_result": step3_marker,
        }

    def _fake_import(project_root: Path, **kwargs) -> dict:
        seen["import_root"] = project_root
        seen["candidate"] = kwargs.get("candidates_file")
        seen["expected_round_id"] = kwargs.get("expected_round_id")
        seen["expected_candidate_sha256"] = kwargs.get(
            "expected_candidate_sha256"
        )
        return {
            "status": "imported",
            "imported_count": 4,
            "deprecated_count": 0,
            "source_file": step3_marker["candidate_path"],
            "source_round_id": step3_marker["round_id"],
            "source_candidate_sha256": step3_marker["candidate_sha256"],
        }

    monkeypatch.setattr(rdp_run_full_pipeline, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rdp_run_full_pipeline, "_run_phase", _fake_run_phase)
    monkeypatch.setattr(
        auto_import_candidates,
        "auto_import_latest_candidates",
        _fake_import,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--stop-after",
            "import_candidates",
        ],
    )

    assert rdp_run_full_pipeline.main() == 0
    step3_cmd = seen["step3_cmd"]
    step2_arg = step3_cmd[step3_cmd.index("--step2-round-dir") + 1]  # type: ignore[union-attr]
    assert step2_arg == step2_marker["round_dir"]
    assert seen["candidate"] == Path(step3_marker["candidate_path"])
    assert seen["candidate"] != Path(global_latest_b["candidate_path"])
    assert seen["expected_round_id"] == step3_marker["round_id"]
    assert seen["expected_candidate_sha256"] == step3_marker["candidate_sha256"]


def test_explicit_result_ref_loads_one_exact_step2_step3_chain(
    tmp_path: Path,
) -> None:
    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )
    window = {"start": "2026-08-01", "end": "2026-08-27"}

    bound_step2 = rdp_run_full_pipeline._load_research_result_ref(
        str(Path(step2_marker["round_dir"]) / "round_result.json"),
        phase="step2",
        expected_dataset_version="v1.0",
        expected_symbol="BTC-USDT-SWAP",
        expected_window=window,
        project_root=tmp_path,
    )
    bound_step3 = rdp_run_full_pipeline._load_research_result_ref(
        str(Path(step3_marker["round_dir"]) / "round_result.json"),
        phase="step3",
        expected_dataset_version="v1.0",
        expected_symbol="BTC-USDT-SWAP",
        expected_window=window,
        expected_step2=bound_step2,
        project_root=tmp_path,
    )

    assert bound_step2["candidate_sha256"] == step2_marker["candidate_sha256"]
    assert bound_step3["candidate_sha256"] == step3_marker["candidate_sha256"]
    assert bound_step3["step2_round_id"] == bound_step2["round_id"]


def test_step3_resume_requires_explicit_step2_result_ref_before_start(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--start-from",
            "step3",
            "--stop-after",
            "step3",
        ],
    )
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("phase must not start"),
    )

    assert rdp_run_full_pipeline.main() == 2
    assert "--step2-result-ref" in capsys.readouterr().out


def test_step3_resume_uses_explicit_step2_round_without_latest_lookup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )
    seen: dict[str, object] = {}

    def _fake_run_phase(name: str, cmd: list[str], **_kwargs) -> dict:
        seen["cmd"] = cmd
        return {
            "phase": name,
            "status": "success",
            "exit_code": 0,
            "structured_result": step3_marker,
        }

    monkeypatch.setattr(rdp_run_full_pipeline, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rdp_run_full_pipeline, "_run_phase", _fake_run_phase)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--start-from",
            "step3",
            "--stop-after",
            "step3",
            "--step2-result-ref",
            str(Path(step2_marker["round_dir"]) / "round_result.json"),
        ],
    )

    assert rdp_run_full_pipeline.main() == 0
    cmd = seen["cmd"]
    assert cmd[cmd.index("--step2-round-dir") + 1] == step2_marker["round_dir"]  # type: ignore[union-attr]


def test_downstream_resume_requires_explicit_step3_result_ref_before_start(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start-from",
            "import_candidates",
            "--stop-after",
            "import_candidates",
        ],
    )
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("phase must not start"),
    )

    assert rdp_run_full_pipeline.main() == 2
    assert "--step3-result-ref" in capsys.readouterr().out


def test_new_phase2_cannot_be_split_from_skipped_step3_downstream(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--skip-step3",
            "--stop-after",
            "phase3",
            "--replay-only",
            "--step3-result-ref",
            "unused-round-result.json",
        ],
    )
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("phase must not start"),
    )

    assert rdp_run_full_pipeline.main() == 2


def test_resume_phase3_consumes_exact_step3_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )
    phase3_marker = _phase34_marker(
        tmp_path,
        phase="phase3",
        round_id="20260827_130000_b1c2d3e4",
        step3_marker=step3_marker,
        replay_only=True,
    )
    seen: dict[str, object] = {}

    def _fake_run_phase(name: str, cmd: list[str], **_kwargs) -> dict:
        seen["name"] = name
        seen["cmd"] = cmd
        return {
            "phase": name,
            "status": "success",
            "exit_code": 0,
            "structured_result": phase3_marker,
        }

    monkeypatch.setattr(rdp_run_full_pipeline, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rdp_run_full_pipeline, "_run_phase", _fake_run_phase)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--start-from",
            "phase3",
            "--stop-after",
            "phase3",
            "--replay-only",
            "--step3-result-ref",
            str(Path(step3_marker["round_dir"]) / "round_result.json"),
        ],
    )

    assert rdp_run_full_pipeline.main() == 0
    assert seen["name"] == "phase3"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--params-json") + 1] == step3_marker["candidate_path"]  # type: ignore[union-attr]


def test_params_json_cannot_split_explicit_step3_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )
    other_params = tmp_path / "other-params.json"
    other_params.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rdp_run_full_pipeline, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("phase must not start"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--start-from",
            "phase3",
            "--stop-after",
            "phase3",
            "--replay-only",
            "--step3-result-ref",
            str(Path(step3_marker["round_dir"]) / "round_result.json"),
            "--params-json",
            str(other_params),
        ],
    )

    assert rdp_run_full_pipeline.main() == 2


def test_no_stop_blocks_all_artifact_dependents_after_phase2_failure(
    monkeypatch,
) -> None:
    emitted: dict[str, object] = {}
    calls: list[str] = []

    def _fake_run_phase(name: str, *_args, **_kwargs) -> dict:
        calls.append(name)
        assert name == "phase2"
        return {"phase": name, "status": "failed", "exit_code": 1}

    monkeypatch.setattr(rdp_run_full_pipeline, "_run_phase", _fake_run_phase)
    monkeypatch.setattr(
        rdp_run_full_pipeline,
        "_emit_pipeline_result",
        lambda **kwargs: emitted.update(kwargs),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--stop-after",
            "phase4",
            "--replay-only",
            "--no-stop-on-failure",
        ],
    )

    assert rdp_run_full_pipeline.main() == 1
    assert calls == ["phase2"]
    results = emitted["results"]
    assert [item["phase"] for item in results] == [  # type: ignore[index]
        "phase2",
        "step3",
        "import_candidates",
        "phase3",
        "phase4",
    ]
    assert all(
        item.get("error") == "blocked_upstream"
        for item in results[1:]  # type: ignore[index]
    )


def test_import_success_with_wrong_returned_identity_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aats.data_platform.governance import auto_import_candidates

    step2_marker = _research_marker(
        tmp_path,
        phase="step2",
        round_id="20260827_100000_1234abcd",
        candidate_bytes=b'{"step":2}',
    )
    step3_marker = _research_marker(
        tmp_path,
        phase="step3",
        round_id="20260827_120000_a1b2c3d4",
        candidate_bytes=b'{"step":3}',
        step2_marker=step2_marker,
    )

    def _fake_run_phase(name: str, *_args, **_kwargs) -> dict:
        marker = step2_marker if name == "phase2" else step3_marker
        return {
            "phase": name,
            "status": "success",
            "exit_code": 0,
            "structured_result": marker,
        }

    monkeypatch.setattr(rdp_run_full_pipeline, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rdp_run_full_pipeline, "_run_phase", _fake_run_phase)
    monkeypatch.setattr(
        auto_import_candidates,
        "auto_import_latest_candidates",
        lambda *_args, **_kwargs: {
            "status": "imported",
            "imported_count": 1,
            "deprecated_count": 0,
            "source_file": step3_marker["candidate_path"],
            "source_round_id": "20260827_120001_deadbeef",
            "source_candidate_sha256": step3_marker["candidate_sha256"],
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_run_full_pipeline.py",
            "--start",
            "2026-08-01",
            "--end",
            "2026-08-27",
            "--stop-after",
            "import_candidates",
        ],
    )

    assert rdp_run_full_pipeline.main() == 1


def test_pipeline_marker_propagates_research_outcome(capsys) -> None:
    decision_result = {
        "round_id": "round_1",
        "readiness": "not_ready_attribution_issue",
        "research_outcome": "blocked_by_attribution",
    }

    rdp_run_full_pipeline._emit_pipeline_result(
        pipeline_id="pipeline_1",
        status="succeeded",
        results=[
            {
                "phase": "decision",
                "status": "success",
                "structured_result": decision_result,
            }
        ],
    )

    marker = capsys.readouterr().out.strip()
    payload = json.loads(marker.removeprefix(rdp_run_full_pipeline._PIPELINE_RESULT_PREFIX))
    assert payload["research_outcome"] == "blocked_by_attribution"
    assert payload["decision_round_id"] == "round_1"
    assert payload["readiness"] == "not_ready_attribution_issue"
