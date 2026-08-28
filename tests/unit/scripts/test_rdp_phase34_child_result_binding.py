from __future__ import annotations

import csv
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aats.data_platform.governance.parameter_identity import (
    parameter_values_fingerprint,
)
from scripts import (
    rdp_run_execution_realism,
    rdp_run_live_attribution,
    rdp_run_phase3_round,
    rdp_run_phase4_round,
)


def _csv_bytes(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _phase3_business_outputs(
    *,
    family: str,
    symbol: str,
    timeframe: str,
    replay_only: bool = False,
) -> dict[str, bytes]:
    row = {field: "" for field in rdp_run_phase3_round._ATTRIBUTION_ALIGNMENT_FIELDS}
    if replay_only:
        row.update(
            {
                "family": family,
                "symbol": symbol,
                "timeframe": timeframe,
                "replay_ts": "2026-08-25T00:00:00+00:00",
                "alignment_status": "replay_only",
                "replay_opening": "False",
                "live_opening": "False",
                "final_attribution_category": "not_applicable",
                "final_attribution_reason": "replay_not_selectable",
                "replay_action": "hold",
                "replay_selectable": "False",
                "replay_execution_compatible": "False",
                "replay_expected_net_edge_bps": "-5.0",
            }
        )
    else:
        row.update({
            "family": family,
            "symbol": symbol,
            "timeframe": timeframe,
            "replay_ts": "2026-08-25T00:00:00+00:00",
            "live_ts": "2026-08-25T00:00:00+00:00",
            "alignment_status": "aligned",
            "replay_opening": "True",
            "live_opening": "True",
            "strategy_reason": "passed",
            "permission_reason": "passed",
            "allocator_reason": "passed",
            "budget_reason": "passed",
            "risk_reason": "passed",
            "execution_reason": "passed",
            "order_status": "SUBMITTED",
            "fill_status": "filled",
            "replay_action": "open",
            "replay_selectable": "True",
            "replay_execution_compatible": "True",
            "replay_expected_net_edge_bps": "13.1",
            "live_state": "active",
            "live_route_action": "override_target",
            "live_automatic_enabled": "True",
            "final_attribution_category": "live_traded",
            "final_attribution_reason": "all_layers_passed",
            "live_intent_id": "intent-1",
            "live_decision_id": "decision-1",
            "live_allocation_id": "allocation-1",
            "live_parameter_set_id": "parameter-set-1",
            "live_runtime_generation": "generation-1",
            "live_code_version": "commit-1",
            "live_market_snapshot_ref": "market-1",
            "live_feature_snapshot_ref": "feature-1",
        })
    return _phase3_outputs_for_rows(
        [row],
        family=family,
        timeframe=timeframe,
    )


def _phase3_outputs_for_rows(
    rows: list[dict[str, Any]],
    *,
    family: str,
    timeframe: str,
) -> dict[str, bytes]:
    from aats.data_platform.attribution.aggregation import (
        build_attribution_summary,
        build_top_failure_modes,
    )

    return {
        "replay_live_alignment": _csv_bytes(
            rows,
            rdp_run_phase3_round._ATTRIBUTION_ALIGNMENT_FIELDS,
        ),
        "attribution_summary": json.dumps(
            build_attribution_summary(rows, family=family, timeframe=timeframe),
            sort_keys=True,
        ).encode(),
        "top_failure_modes": json.dumps(
            build_top_failure_modes(rows),
            sort_keys=True,
        ).encode(),
    }


def _phase4_business_outputs(
    *,
    run_id: str,
    family: str,
    symbol: str,
    timeframe: str,
    taker_fee_bps: float,
    matched: bool = True,
) -> dict[str, bytes]:
    alignment = {
        "family": family,
        "symbol": symbol,
        "timeframe": timeframe,
        "candidate_ts": "2026-08-25T00:00:00+00:00",
        "candidate_source": "replay",
        "candidate_side": "buy",
        "candidate_qty": 1.0,
        "candidate_notional_usd": 500.5 if matched else 0,
        "candidate_action": "open",
        "snapshot_ts": "2026-08-25T00:00:00+00:00" if matched else None,
        "trades_window_start": "2026-08-25T00:00:00+00:00" if matched else None,
        "trades_window_end": "2026-08-25T00:15:00+00:00" if matched else None,
        "alignment_status": "matched" if matched else "no_bar_data",
        "bar_open": 50000.0 if matched else None,
        "bar_high": 50100.0 if matched else None,
        "bar_low": 49900.0 if matched else None,
        "bar_close": 50050.0 if matched else None,
        "bar_volume": 1000.0 if matched else None,
        "bar_quote_volume": 50050000.0 if matched else None,
        "bar_range_bps": 39.96 if matched else None,
        "aligned_funding_rate": 0.0001 if matched else None,
        "signal_edge_proxy_bps": 20.0,
        "funding_adjustment_bps": 0.1,
        "cost_bps": 5.0,
        "expected_net_edge_bps": 13.1,
    }
    from aats.data_platform.execution_realism.execution_cost_model import (
        build_execution_cost_summary,
    )
    from aats.data_platform.execution_realism.fill_feasibility import (
        evaluate_fill_feasibility,
    )
    from aats.data_platform.execution_realism.slippage_estimator import (
        estimate_slippage,
    )

    feasibility = evaluate_fill_feasibility([alignment])
    slippage = estimate_slippage(feasibility, taker_fee_bps=taker_fee_bps)
    summary = build_execution_cost_summary(slippage)
    summary.update(
        {
            "schema_version": "execution_cost_summary_v1",
            "source_run_id": run_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "window_start": "2026-08-25T00:00:00+00:00",
            "window_end": "2026-08-26T00:00:00+00:00",
        }
    )
    return {
        "execution_alignment": _csv_bytes(
            [alignment],
            rdp_run_phase4_round._EXECUTION_ALIGNMENT_FIELDS,
        ),
        "fill_feasibility_summary": _csv_bytes(
            feasibility,
            rdp_run_phase4_round._FILL_FEASIBILITY_FIELDS,
        ),
        "slippage_summary": _csv_bytes(
            slippage,
            rdp_run_phase4_round._SLIPPAGE_FIELDS,
        ),
        "execution_cost_summary": json.dumps(summary, sort_keys=True).encode(),
    }


def _argument(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def _write_outputs(
    run_dir: Path,
    output_files: dict[str, str],
    *,
    overrides: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    payloads = {
        "replay_live_alignment": (
            b"alignment_status,final_attribution_category\nreplay_only,not_applicable\n"
        ),
        "attribution_summary": b"[]",
        "top_failure_modes": b'{"total_failures":0}',
        "execution_alignment": b"alignment_status\nmatched\n",
        "fill_feasibility_summary": b"candidate_id\n1\n",
        "slippage_summary": b"estimated_slippage_bps\n1.5\n",
        "execution_cost_summary": b'{"total_candidates":1}',
        "replay_params_used": b'{"min_confirm_ticks":2,"noise_buffer_bps":2.0}',
        "live_attribution_report": b"# attribution\n",
        "live_execution_realism_report": b"# execution\n",
    }
    payloads.update(overrides or {})
    run_dir.mkdir(parents=True)
    written: dict[str, bytes] = {}
    for key, filename in output_files.items():
        content = payloads[key]
        (run_dir / filename).write_bytes(content)
        written[key] = content
    return written


def _output_evidence(
    run_dir: Path,
    output_files: dict[str, str],
    contents: dict[str, bytes],
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "path": str((run_dir / filename).resolve()),
            "sha256": hashlib.sha256(contents[key]).hexdigest(),
            "size_bytes": len(contents[key]),
        }
        for key, filename in output_files.items()
    }


def _write_phase3_result(
    command: list[str],
    *,
    output_overrides: dict[str, bytes] | None = None,
    result_overrides: dict[str, Any] | None = None,
) -> SimpleNamespace:
    combo_root = Path(_argument(command, "--artifact-root"))
    result_path = Path(_argument(command, "--result-json"))
    run_id = f"child_{result_path.stem}"
    run_dir = combo_root / run_id
    business_outputs = _phase3_business_outputs(
        family=_argument(command, "--family"),
        symbol=_argument(command, "--symbol"),
        timeframe=_argument(command, "--timeframe"),
        replay_only="--replay-only" in command,
    )
    business_outputs.update(output_overrides or {})
    contents = _write_outputs(
        run_dir,
        rdp_run_phase3_round._CHILD_OUTPUT_FILES,
        overrides=business_outputs,
    )
    replay_params = json.loads(contents["replay_params_used"])
    result = {
        "schema_version": rdp_run_phase3_round._CHILD_RESULT_SCHEMA,
        "status": "succeeded",
        "exit_code": 0,
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "family": _argument(command, "--family"),
        "symbol": _argument(command, "--symbol"),
        "timeframe": _argument(command, "--timeframe"),
        "dataset_version": _argument(command, "--dataset-version"),
        "window": {
            "start": _argument(command, "--start"),
            "end": _argument(command, "--end"),
        },
        "replay_only": "--replay-only" in command,
        "resolved_parameter_values_fingerprint": parameter_values_fingerprint(
            replay_params
        ),
        "finished_at": "2026-08-27T12:00:00+00:00",
        "outputs": _output_evidence(
            run_dir,
            rdp_run_phase3_round._CHILD_OUTPUT_FILES,
            contents,
        ),
    }
    result.update(result_overrides or {})
    result_path.write_text(json.dumps(result), encoding="utf-8")
    marker = rdp_run_phase3_round._CHILD_RESULT_MARKER + json.dumps(result)
    return SimpleNamespace(returncode=0, stdout=marker.encode(), stderr=b"")


def _write_phase4_result(
    command: list[str],
    *,
    corrupt_digest: bool = False,
    cost_summary_mismatch: bool = False,
    matched: bool = True,
    output_overrides: dict[str, bytes] | None = None,
    result_overrides: dict[str, Any] | None = None,
) -> SimpleNamespace:
    combo_root = Path(_argument(command, "--artifact-root"))
    result_path = Path(_argument(command, "--result-json"))
    run_id = f"child_{result_path.stem}"
    run_dir = combo_root / run_id
    business_outputs = _phase4_business_outputs(
        run_id=run_id,
        family=_argument(command, "--family"),
        symbol=_argument(command, "--symbol"),
        timeframe=_argument(command, "--timeframe"),
        taker_fee_bps=float(_argument(command, "--taker-fee-bps")),
        matched=matched,
    )
    business_outputs.update(output_overrides or {})
    if cost_summary_mismatch:
        mismatched_summary = json.loads(business_outputs["execution_cost_summary"])
        mismatched_summary["total_candidates"] += 1
        business_outputs["execution_cost_summary"] = json.dumps(
            mismatched_summary,
            sort_keys=True,
        ).encode()
    contents = _write_outputs(
        run_dir,
        rdp_run_phase4_round._CHILD_OUTPUT_FILES,
        overrides=business_outputs,
    )
    replay_params = json.loads(contents["replay_params_used"])
    outputs = _output_evidence(
        run_dir,
        rdp_run_phase4_round._CHILD_OUTPUT_FILES,
        contents,
    )
    if corrupt_digest:
        outputs["execution_cost_summary"]["sha256"] = "0" * 64
    result = {
        "schema_version": rdp_run_phase4_round._CHILD_RESULT_SCHEMA,
        "status": "succeeded",
        "exit_code": 0,
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "family": _argument(command, "--family"),
        "symbol": _argument(command, "--symbol"),
        "timeframe": _argument(command, "--timeframe"),
        "dataset_version": _argument(command, "--dataset-version"),
        "window": {
            "start": _argument(command, "--start"),
            "end": _argument(command, "--end"),
        },
        "taker_fee_bps": float(_argument(command, "--taker-fee-bps")),
        "resolved_parameter_values_fingerprint": parameter_values_fingerprint(
            replay_params
        ),
        "finished_at": "2026-08-27T12:00:00+00:00",
        "outputs": outputs,
    }
    result.update(result_overrides or {})
    result_path.write_text(json.dumps(result), encoding="utf-8")
    marker = rdp_run_phase4_round._CHILD_RESULT_MARKER + json.dumps(result)
    return SimpleNamespace(returncode=0, stdout=marker.encode(), stderr=b"")


def test_live_attribution_result_sidecar_is_immutable_and_binds_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    contents = _write_outputs(run_dir, rdp_run_live_attribution._RESULT_OUTPUT_FILES)
    result_path = tmp_path / "result.json"

    payload = rdp_run_live_attribution._publish_result_sidecar(
        result_json=str(result_path),
        run_id="run",
        run_dir=run_dir,
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        dataset_version="v1.0",
        start="2026-08-25",
        end="2026-08-26",
        replay_only=True,
        replay_params=json.loads(contents["replay_params_used"]),
        status="succeeded",
        exit_code=0,
    )

    assert json.loads(result_path.read_text(encoding="utf-8")) == payload
    assert rdp_run_live_attribution._RESULT_MARKER_PREFIX in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        rdp_run_live_attribution._publish_result_sidecar(
            result_json=str(result_path),
            run_id="run",
            run_dir=run_dir,
            family="independent",
            symbol="BTC-USDT-SWAP",
            timeframe="15m",
            dataset_version="v1.0",
            start="2026-08-25",
            end="2026-08-26",
            replay_only=True,
            replay_params=json.loads(contents["replay_params_used"]),
            status="succeeded",
            exit_code=0,
        )


def test_execution_result_sidecar_preserves_partial_exit_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "execution_run"
    contents = _write_outputs(
        run_dir,
        rdp_run_execution_realism._RESULT_OUTPUT_FILES,
    )
    result_path = tmp_path / "execution_result.json"

    payload = rdp_run_execution_realism._publish_result_sidecar(
        result_json=str(result_path),
        run_id="execution_run",
        run_dir=run_dir,
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        dataset_version="v1.0",
        start="2026-08-25",
        end="2026-08-26",
        taker_fee_bps=5.0,
        replay_params=json.loads(contents["replay_params_used"]),
        status="partial_success",
        exit_code=2,
    )

    assert payload["status"] == "partial_success"
    assert payload["exit_code"] == 2
    assert json.loads(result_path.read_text(encoding="utf-8")) == payload
    assert rdp_run_execution_realism._RESULT_MARKER_PREFIX in capsys.readouterr().out


def test_phase3_concurrent_children_bind_only_their_unique_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        combo_root = Path(_argument(command, "--artifact-root"))
        (combo_root / "zzzz_pollution").mkdir(exist_ok=True)
        return _write_phase3_result(command)

    monkeypatch.setattr(rdp_run_phase3_round.subprocess, "run", fake_run)
    kwargs = {
        "symbol": "BTC-USDT-SWAP",
        "start": "2026-08-25",
        "end": "2026-08-26",
        "artifact_root": tmp_path / "round" / "per_combo",
        "live_db_url": None,
        "replay_only": True,
        "ensure_schema": False,
        "dataset_version": "v1.0",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                rdp_run_phase3_round._run_single_attribution,
                "independent",
                "15m",
                **kwargs,
            )
            for _ in range(2)
        ]
    results = [future.result() for future in futures]

    assert [result["status"] for result in results] == ["succeeded", "succeeded"]
    assert len({result["run_dir"] for result in results}) == 2
    combo_root = (kwargs["artifact_root"] / "independent_15m").resolve()
    assert all(Path(result["run_dir"]).parent == combo_root for result in results)
    assert len(list(combo_root.glob("result_*.json"))) == 2
    assert all("zzzz_pollution" not in result["run_dir"] for result in results)


def test_phase4_fails_closed_on_sidecar_output_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            corrupt_digest=True,
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "result_output_digest_mismatch:execution_cost_summary" in result["error"]


@pytest.mark.parametrize(
    "module",
    [rdp_run_phase3_round, rdp_run_phase4_round],
)
def test_phase34_manifest_source_keeps_unified_and_compat_status(
    module: object,
) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert '"status": overall_status' in source
    assert '"overall_status": overall_status' in source
    assert '"scope": {' in source


def test_phase3_fails_closed_on_semantically_invalid_bound_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides={"attribution_summary": b"{}"},
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url="postgresql://unused",
        replay_only=False,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "attribution_summary_contract_invalid" in result["error"]


def test_phase3_fails_closed_when_aligned_lineage_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _phase3_business_outputs(
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
    )
    rows = list(csv.DictReader(io.StringIO(outputs["replay_live_alignment"].decode())))
    rows[0]["live_code_version"] = ""
    invalid_alignment = _csv_bytes(
        rows,
        rdp_run_phase3_round._ATTRIBUTION_ALIGNMENT_FIELDS,
    )
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides={"replay_live_alignment": invalid_alignment},
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url="postgresql://unused",
        replay_only=False,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "attribution_aligned_lineage_invalid" in result["error"]


def test_phase3_fails_closed_when_summary_does_not_match_bound_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides={"attribution_summary": b"[]"},
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url=None,
        replay_only=True,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "attribution_summary_detail_mismatch" in result["error"]


def test_phase4_invalid_utf8_csv_returns_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            output_overrides={"slippage_summary": b"\xff\xfe"},
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "phase4_child_artifact_invalid" in result["error"]


def test_phase4_accepts_recomputed_business_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(command),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "succeeded"
    assert result["cost_summary"]["total_candidates"] == 1
    assert result["alignment_stats"] == {
        "total": 1,
        "matched": 1,
        "no_bar_data": 0,
    }


def test_phase4_fails_closed_when_cost_summary_does_not_match_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            cost_summary_mismatch=True,
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "execution_cost_summary_detail_mismatch" in result["error"]


def test_phase4_fails_closed_on_non_finite_alignment_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _phase4_business_outputs(
        run_id="unused",
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        taker_fee_bps=5.0,
    )
    rows = list(csv.DictReader(io.StringIO(outputs["execution_alignment"].decode())))
    rows[0]["candidate_qty"] = "NaN"
    invalid_alignment = _csv_bytes(
        rows,
        rdp_run_phase4_round._EXECUTION_ALIGNMENT_FIELDS,
    )
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            output_overrides={"execution_alignment": invalid_alignment},
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "execution_candidate_qty_number_non_finite" in result["error"]


def test_phase3_replay_only_mode_rejects_aligned_live_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_outputs = _phase3_business_outputs(
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        replay_only=False,
    )
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides=live_outputs,
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url=None,
        replay_only=True,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "attribution_replay_only_mode_status_invalid" in result["error"]


def test_phase3_live_traded_requires_all_waterfall_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _phase3_business_outputs(
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
    )
    rows = list(csv.DictReader(io.StringIO(outputs["replay_live_alignment"].decode())))
    rows[0]["execution_reason"] = ""
    invalid_outputs = _phase3_outputs_for_rows(
        rows,
        family="independent",
        timeframe="15m",
    )
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides=invalid_outputs,
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url="postgresql://unused",
        replay_only=False,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "attribution_success_waterfall_invalid" in result["error"]


@pytest.mark.parametrize(
    "reason",
    ["cost_exceeds_max_acceptable", "rebalance_cooldown"],
)
def test_phase3_accepts_real_replay_strategy_blocking_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    outputs = _phase3_business_outputs(
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        replay_only=True,
    )
    rows = list(csv.DictReader(io.StringIO(outputs["replay_live_alignment"].decode())))
    rows[0].update(
        {
            "replay_selectable": "True",
            "replay_blocking_reasons": reason,
            "final_attribution_category": "strategy_blocked",
            "final_attribution_reason": reason,
            "strategy_reason": reason,
        }
    )
    valid_outputs = _phase3_outputs_for_rows(
        rows,
        family="independent",
        timeframe="15m",
    )
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides=valid_outputs,
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url=None,
        replay_only=True,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "succeeded"


def test_phase3_rejects_duplicate_live_intent_across_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _phase3_business_outputs(
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
    )
    rows = list(csv.DictReader(io.StringIO(outputs["replay_live_alignment"].decode())))
    duplicate = dict(rows[0])
    duplicate["replay_ts"] = "2026-08-25T00:15:00+00:00"
    duplicate["live_ts"] = "2026-08-25T00:15:00+00:00"
    invalid_outputs = _phase3_outputs_for_rows(
        [rows[0], duplicate],
        family="independent",
        timeframe="15m",
    )
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            output_overrides=invalid_outputs,
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url="postgresql://unused",
        replay_only=False,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "attribution_live_intent_duplicate_identity" in result["error"]


def test_phase4_recomputes_partial_status_from_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(command, matched=False),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "execution_result_status_business_mismatch" in result["error"]


def test_phase4_rejects_edge_not_derived_from_replay_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _phase4_business_outputs(
        run_id="unused",
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        taker_fee_bps=5.0,
    )
    rows = list(csv.DictReader(io.StringIO(outputs["execution_alignment"].decode())))
    rows[0]["expected_net_edge_bps"] = "999.0"
    invalid_alignment = _csv_bytes(
        rows,
        rdp_run_phase4_round._EXECUTION_ALIGNMENT_FIELDS,
    )
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            output_overrides={"execution_alignment": invalid_alignment},
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "execution_expected_net_edge_mismatch" in result["error"]


def test_phase4_rejects_noncanonical_candidate_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _phase4_business_outputs(
        run_id="unused",
        family="independent",
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        taker_fee_bps=5.0,
    )
    rows = list(csv.DictReader(io.StringIO(outputs["execution_alignment"].decode())))
    rows[0]["candidate_ts"] = "2026-08-25T00:00:00Z"
    invalid_alignment = _csv_bytes(
        rows,
        rdp_run_phase4_round._EXECUTION_ALIGNMENT_FIELDS,
    )
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            output_overrides={"execution_alignment": invalid_alignment},
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=5.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "execution_candidate_timestamp_not_canonical" in result["error"]


@pytest.mark.parametrize(
    "module",
    [rdp_run_phase3_round, rdp_run_phase4_round],
)
def test_phase34_round_outcome_preserves_partial(module: object) -> None:
    assert module._round_outcome_status(  # type: ignore[attr-defined]
        [{"status": "partial_success"} for _ in range(4)]
    ) == "partial_success"
    assert module._round_outcome_status(  # type: ignore[attr-defined]
        [{"status": "succeeded"} for _ in range(4)]
    ) == "succeeded"
    assert module._round_outcome_status(  # type: ignore[attr-defined]
        [{"status": "failed"} for _ in range(4)]
    ) == "failed"


def test_phase34_empty_writers_preserve_required_headers(tmp_path: Path) -> None:
    attribution_path = tmp_path / "attribution.csv"
    execution_path = tmp_path / "execution.csv"

    rdp_run_live_attribution._write_alignment_csv([], attribution_path)
    rdp_run_execution_realism._write_csv(
        [],
        execution_path,
        list(rdp_run_phase4_round._EXECUTION_ALIGNMENT_FIELDS),
    )

    assert tuple(
        csv.DictReader(io.StringIO(attribution_path.read_text(encoding="utf-8"))).fieldnames
        or ()
    ) == rdp_run_phase3_round._ATTRIBUTION_ALIGNMENT_FIELDS
    assert tuple(
        csv.DictReader(io.StringIO(execution_path.read_text(encoding="utf-8"))).fieldnames
        or ()
    ) == rdp_run_phase4_round._EXECUTION_ALIGNMENT_FIELDS


@pytest.mark.parametrize(
    ("runner", "output_key", "expected_error"),
    [
        (rdp_run_phase3_round, "replay_live_alignment", "replay_live_alignment_empty"),
        (rdp_run_phase4_round, "execution_alignment", "execution_alignment_empty"),
    ],
)
def test_phase34_parent_rejects_zero_byte_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: object,
    output_key: str,
    expected_error: str,
) -> None:
    if runner is rdp_run_phase3_round:
        monkeypatch.setattr(
            rdp_run_phase3_round.subprocess,
            "run",
            lambda command, **_kwargs: _write_phase3_result(
                command,
                output_overrides={output_key: b""},
            ),
        )
        result = rdp_run_phase3_round._run_single_attribution(
            "independent",
            "15m",
            symbol="BTC-USDT-SWAP",
            start="2026-08-25",
            end="2026-08-26",
            artifact_root=tmp_path / "round" / "per_combo",
            live_db_url=None,
            replay_only=True,
            ensure_schema=False,
            dataset_version="v1.0",
        )
    else:
        monkeypatch.setattr(
            rdp_run_phase4_round.subprocess,
            "run",
            lambda command, **_kwargs: _write_phase4_result(
                command,
                output_overrides={output_key: b""},
            ),
        )
        result = rdp_run_phase4_round._run_single_execution_realism(
            "independent",
            "15m",
            symbol="BTC-USDT-SWAP",
            start="2026-08-25",
            end="2026-08-26",
            artifact_root=tmp_path / "round" / "per_combo",
            taker_fee_bps=5.0,
            ensure_schema=False,
            dataset_version="v1.0",
        )

    assert result["status"] == "failed"
    assert expected_error in result["error"]


@pytest.mark.parametrize(
    "runner",
    [rdp_run_phase3_round, rdp_run_phase4_round],
)
def test_phase34_json_decoder_rejects_duplicate_keys(runner: object) -> None:
    with pytest.raises(ValueError, match="duplicate_json_key:status"):
        runner._decode_json(  # type: ignore[attr-defined]
            b'{"status":"succeeded","status":"failed"}',
            label="result_sidecar",
        )


def test_phase3_rejects_non_boolean_replay_only_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase3_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase3_result(
            command,
            result_overrides={"replay_only": 1},
        ),
    )

    result = rdp_run_phase3_round._run_single_attribution(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        live_db_url=None,
        replay_only=True,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "result_replay_only_type_invalid" in result["error"]


def test_phase4_rejects_boolean_taker_fee_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rdp_run_phase4_round.subprocess,
        "run",
        lambda command, **_kwargs: _write_phase4_result(
            command,
            result_overrides={"taker_fee_bps": True},
        ),
    )

    result = rdp_run_phase4_round._run_single_execution_realism(
        "independent",
        "15m",
        symbol="BTC-USDT-SWAP",
        start="2026-08-25",
        end="2026-08-26",
        artifact_root=tmp_path / "round" / "per_combo",
        taker_fee_bps=1.0,
        ensure_schema=False,
        dataset_version="v1.0",
    )

    assert result["status"] == "failed"
    assert "result_taker_fee_type_invalid" in result["error"]
