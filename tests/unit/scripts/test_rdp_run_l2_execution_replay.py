from __future__ import annotations

import importlib.util
import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "rdp_run_l2_execution_replay.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("rdp_run_l2_execution_replay", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace_tmp() -> Iterator[Path]:
    path = Path(".pytest_workspace_tmp") / f"l2_cli_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_eligibility_manifest_requires_every_15m_window(workspace_tmp: Path) -> None:
    module = _load_module()
    start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    path = workspace_tmp / "eligibility.json"
    path.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "eligible_for_research": True,
                        "evidence_fingerprint": "a" * 64,
                        "observation": {
                            "symbol": "BTC-USDT-SWAP",
                            "window_start": start.isoformat(),
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="eligibility_manifest_window_gap"):
        module._eligibility_manifest_fingerprint(
            path,
            symbol="BTC-USDT-SWAP",
            window_start=start,
            window_end=start + timedelta(minutes=16),
        )


def test_eligibility_manifest_fingerprint_is_deterministic(workspace_tmp: Path) -> None:
    module = _load_module()
    start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    path = workspace_tmp / "eligibility.json"
    path.write_text(
        json.dumps(
            {
                "reports": [
                    {
                        "eligible_for_research": True,
                        "evidence_fingerprint": "a" * 64,
                        "observation": {
                            "symbol": "BTC-USDT-SWAP",
                            "window_start": start.isoformat(),
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    first = module._eligibility_manifest_fingerprint(
        path,
        symbol="BTC-USDT-SWAP",
        window_start=start,
        window_end=start + timedelta(minutes=1),
    )
    second = module._eligibility_manifest_fingerprint(
        path,
        symbol="BTC-USDT-SWAP",
        window_start=start,
        window_end=start + timedelta(minutes=1),
    )
    assert first == second
    assert first.startswith("micro_")


def test_request_evidence_context_is_exact_and_fail_closed() -> None:
    module = _load_module()
    valid = {
        "plan_id": "v2replay_example",
        "timeframe": "15m",
        "benchmark_segment": "valid",
        "dataset_fingerprint": "rfds_" + "a" * 64,
    }
    assert module._request_evidence_context(valid) == (
        "v2replay_example",
        "15m",
        "rfds_" + "a" * 64,
    )
    with pytest.raises(ValueError, match="benchmark_segment"):
        module._request_evidence_context({**valid, "benchmark_segment": "test"})
    with pytest.raises(ValueError, match="dataset_fingerprint_invalid"):
        module._request_evidence_context({**valid, "dataset_fingerprint": "rfds_bad"})
