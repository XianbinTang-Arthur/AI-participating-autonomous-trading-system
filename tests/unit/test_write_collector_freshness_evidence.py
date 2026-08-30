from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import write_collector_freshness_evidence as writer


def test_writer_uses_archive_mtime_with_current_observation_scope(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    observed_at = datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC)
    clock_values = iter(
        (observed_at, observed_at + timedelta(seconds=1), observed_at + timedelta(seconds=2))
    )
    image_id = "sha256:" + "a" * 64
    copied: list[tuple[str, str]] = []

    def fake_run(args: tuple[str, ...], _cwd: Path | None = None) -> str:
        assert args[:4] == (
            "docker",
            "inspect",
            "--format",
            writer._CONTAINER_INSPECT_TEMPLATE,
        )
        assert args[4] in writer._COLLECTORS
        return "\n".join(
            (json.dumps("running"), json.dumps("healthy"), json.dumps(image_id))
        )

    def fake_copy(container: str, path: str) -> int:
        copied.append((container, path))
        return int((observed_at - timedelta(seconds=10)).timestamp())

    output = tmp_path / "collector.json"
    monkeypatch.setattr(writer, "_run_command", fake_run)
    monkeypatch.setattr(writer, "_copy_container_file_mtime", fake_copy)
    monkeypatch.setattr(writer, "_utc_now", lambda: next(clock_values))
    monkeypatch.setattr(
        sys,
        "argv",
        ["write_collector_freshness_evidence.py", "--output", str(output)],
    )

    assert writer.main() == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["generated_at"] == (observed_at + timedelta(seconds=2)).isoformat()
    assert payload["collector_containers"] == [
        {
            "name": name,
            "status": "running",
            "health": "healthy",
            "image_id": image_id,
        }
        for name in writer._COLLECTORS
    ]
    assert copied == [
        (name, writer._COLLECTOR_HEARTBEATS[name]) for name in writer._COLLECTORS
    ]
    assert [row["heartbeat_age_seconds"] for row in payload["collector_freshness"]] == [
        10.0,
        11.0,
    ]
    for row in payload["collector_freshness"]:
        assert row["observation_phase"] == "current"
        assert row["observation_method"] == "docker_archive_mtime"
        assert row["fresh"] is True
    assert payload["production_ready"] is False
    assert payload["trading_ready"] is False

    result = json.loads(capsys.readouterr().out)
    assert result["output"] == output.as_posix()
    assert result["fresh"] is True

