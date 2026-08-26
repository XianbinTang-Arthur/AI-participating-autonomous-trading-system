from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from aats.data_platform.governance.retry_logic import generate_retry_plan


def _load_script_module():
    path = Path("scripts/rdp_retry_failed_round.py")
    spec = importlib.util.spec_from_file_location("rdp_retry_failed_round_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retry_plan_preserves_zero_fee_and_emits_structured_argv(tmp_path: Path) -> None:
    round_dir = tmp_path / "round"
    round_dir.mkdir()
    (round_dir / "round_manifest.json").write_text(
        json.dumps(
            {
                "round_id": "round_1",
                "window": {"start": "2026-08-01", "end": "2026-08-02"},
                "taker_fee_bps": 0,
                "symbol": "BTC-USDT-SWAP; echo should-not-run",
                "combos": [{"key": "independent_15m", "status": "failed"}],
            },
        ),
        encoding="utf-8",
    )

    plan = generate_retry_plan(round_dir, phase="phase4")

    assert plan["full_rerun_argv"][-2:] == ["--taker-fee-bps", "0"]
    assert plan["retry_commands"][0]["argv"][7] == "BTC-USDT-SWAP; echo should-not-run"


def test_retry_script_executes_structured_argv_without_shell(
    monkeypatch,
) -> None:
    module = _load_script_module()
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        module,
        "generate_retry_plan",
        lambda *_args, **_kwargs: {
            "phase": "phase4",
            "original_round_id": "round_1",
            "original_status": "failed",
            "failed_combos": [],
            "notes": [],
            "retry_commands": [],
            "full_rerun_command": "python scripts/rdp_run_phase4_round.py --start safe",
            "full_rerun_argv": [
                "python",
                "scripts/rdp_run_phase4_round.py",
                "--start",
                "safe; echo should-not-run",
            ],
        },
    )

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", _run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rdp_retry_failed_round.py",
            "--action",
            "rerun",
            "--round-dir",
            "round",
            "--phase",
            "phase4",
        ],
    )

    try:
        module.main()
    except SystemExit as exc:
        assert exc.code == 0

    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[-1] == "safe; echo should-not-run"
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
