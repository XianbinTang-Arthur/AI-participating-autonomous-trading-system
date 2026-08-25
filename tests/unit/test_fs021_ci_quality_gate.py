"""FS-021: repository-level CI quality gate contract tests."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "quality.yml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
LONG_SHORT_TEST_PATH = ROOT / "tests" / "unit" / "test_long_short_poller.py"


def _workflow() -> tuple[str, dict]:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return text, parsed


def test_quality_workflow_has_safe_triggers_read_only_permissions_and_bounds() -> None:
    _text, workflow = _workflow()

    assert set(workflow["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True

    job = workflow["jobs"]["python-quality"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 20


def test_third_party_actions_are_full_sha_pinned_and_checkout_is_read_only() -> None:
    _text, workflow = _workflow()
    steps = workflow["jobs"]["python-quality"]["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]

    assert uses == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
    ]
    assert all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", item) for item in uses)
    assert steps[0]["with"]["persist-credentials"] is False
    assert steps[1]["with"] == {"python-version": "3.12", "check-latest": False}


def test_quality_commands_cover_full_ruff_strict_unit_and_warning_budget() -> None:
    _text, workflow = _workflow()
    steps = workflow["jobs"]["python-quality"]["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in steps)

    assert "python scripts/verify_dependency_locks.py" in commands
    assert "python -m pip install --require-hashes --only-binary=:all:" in commands
    assert "-r requirements/ci-py312-linux-x86_64.lock" in commands
    assert "python -m ruff check ." in commands
    assert "python -m pytest tests/unit/ -x -q" in commands
    assert "-p no:cacheprovider" in commands
    assert "--strict-markers" in commands
    assert "-W error" in commands
    assert (
        "-W 'ignore:The default datetime adapter is deprecated as of Python "
        "3.12:DeprecationWarning'"
    ) in commands
    assert "Python 3\\.12" not in commands


def test_workflow_contains_no_live_deploy_secret_or_fail_open_path() -> None:
    text, workflow = _workflow()
    lowered = text.lower()

    for forbidden in (
        "pull_request_target",
        "secrets.",
        ".env",
        "docker",
        "deploy",
        "continue-on-error",
        "|| true",
    ):
        assert forbidden not in lowered

    assert "environment" not in workflow["jobs"]["python-quality"]
    assert all(value == "read" for value in workflow["permissions"].values())


def test_test_and_lint_extras_are_explicit_and_bounded() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]

    assert extras["test"] == [
        "pytest>=8,<10",
        "pytest-asyncio>=0.23,<2",
        "nats-py>=2.7,<3",
    ]
    assert extras["lint"] == ["ruff==0.15.8"]


def test_pytest_markers_are_registered_for_strict_collection() -> None:
    config = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["tool"]["pytest"]
    markers = config["ini_options"]["markers"]

    assert any(item.startswith("asyncio:") for item in markers)
    assert any(item.startswith("integration:") for item in markers)


def test_long_short_http_response_sync_method_is_not_an_async_mock() -> None:
    source = LONG_SHORT_TEST_PATH.read_text(encoding="utf-8")

    assert "from unittest.mock import AsyncMock, Mock, patch" in source
    assert "raise_for_status = AsyncMock" not in source
    assert source.count("raise_for_status = Mock()") == 5
