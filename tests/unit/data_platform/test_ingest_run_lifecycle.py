from __future__ import annotations

from typing import Any

from aats.data_platform.jobs.run_registry import mark_orphaned_ingest_runs


class _FakeResult:
    rowcount = 3


class _CaptureSession:
    def __init__(self) -> None:
        self.sql: str | None = None
        self.params: dict[str, Any] | None = None

    def execute(self, stmt, params):  # noqa: ANN001
        self.sql = str(stmt)
        self.params = dict(params)
        return _FakeResult()


def test_mark_orphaned_ingest_runs_closes_only_matching_running_runs() -> None:
    session = _CaptureSession()

    count = mark_orphaned_ingest_runs(
        session,  # type: ignore[arg-type]
        run_type="rolling",
        dataset_domain="microstructure",
        instrument_type="SWAP",
        timeframe="microstructure-ws",
        trigger_mode="daemon",
        reason="orphaned_by_test",
    )

    assert count == 3
    assert session.sql is not None
    assert "WHERE status = 'running'" in session.sql
    assert "run_type = :run_type" in session.sql
    assert "dataset_domain = :domain" in session.sql
    assert "trigger_mode = CAST(:trigger_mode AS TEXT)" in session.sql
    assert "instrument_type = CAST(:instrument_type AS TEXT)" in session.sql
    assert "timeframe = CAST(:timeframe AS TEXT)" in session.sql
    assert session.params is not None
    assert session.params["run_type"] == "rolling"
    assert session.params["domain"] == "microstructure"
    assert session.params["instrument_type"] == "SWAP"
    assert session.params["timeframe"] == "microstructure-ws"
    assert session.params["trigger_mode"] == "daemon"
    assert session.params["reason"] == "orphaned_by_test"
