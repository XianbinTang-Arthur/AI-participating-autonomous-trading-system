from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from apps.decision_engine.main import main as decision_process_main
from scripts import run_local


def test_legacy_entry_has_no_runtime_or_profile_loading_imports() -> None:
    source = Path(run_local.__file__).read_text(encoding="utf-8")

    assert "import asyncio" not in source
    assert "load_profiled_dotenv_into_process" not in source
    assert "from apps.decision_engine.main import main" not in source
    assert "asyncio.run" not in source


def test_legacy_entry_without_arguments_returns_migration_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run_local.main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "scripts/run_local.py 已停用" in captured.err
    assert "scripts/start_api.py --profile derivatives" in captured.err
    assert "tests/integration" in captured.err
    assert "未加载任何 .env profile" in captured.err


def test_legacy_arguments_are_recognized_but_still_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run_local.main(
        [
            "--profile",
            "spot",
            "--iterations",
            "3",
            "--interval-seconds",
            "0.25",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err == run_local.MIGRATION_MESSAGE + "\n"


def test_legacy_entry_rejects_live_profile_before_migration_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_local.main(["--profile", "derivatives_live"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert captured.out == ""
    assert "invalid choice" in captured.err
    assert run_local.MIGRATION_MESSAGE not in captured.err


def test_legacy_script_subprocess_exits_nonzero_without_stdout() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "run_local.py"),
            "--profile",
            "derivatives",
            "--iterations",
            "1",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == run_local.MIGRATION_MESSAGE + "\n"


def test_decision_process_entry_remains_synchronous_and_parameterless() -> None:
    assert not inspect.iscoroutinefunction(decision_process_main)
    assert tuple(inspect.signature(decision_process_main).parameters) == ()
