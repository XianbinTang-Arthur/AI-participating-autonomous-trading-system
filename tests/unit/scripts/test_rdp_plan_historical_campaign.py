from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from scripts import rdp_plan_historical_campaign
from aats.domain.instrument_contract import InstrumentContract
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot


def test_apply_without_instrument_snapshot_blocks_before_database_or_network(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def forbidden_session(*_args, **_kwargs):  # pragma: no cover - safety assertion
        raise AssertionError("unbound apply must stop before database access")

    monkeypatch.setattr(
        rdp_plan_historical_campaign,
        "get_session",
        forbidden_session,
    )

    code = rdp_plan_historical_campaign.main(
        [
            "--symbol",
            "BTC-USDT-SWAP",
            "--start",
            "2026-08-01",
            "--days",
            "1",
            "--storage-root",
            str(tmp_path / "storage"),
            "--manifest-output",
            str(tmp_path / "manifest.json"),
            "--apply",
            "--confirm",
        ]
    )

    assert code == 4
    assert "必须提供覆盖全窗口的 instrument snapshot" in capsys.readouterr().err


def test_unproven_scope_blocks_before_capacity_database_or_network(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def forbidden_session(*_args, **_kwargs):  # pragma: no cover - safety assertion
        raise AssertionError("unsupported scope must stop before database access")

    monkeypatch.setattr(
        rdp_plan_historical_campaign,
        "get_session",
        forbidden_session,
    )

    code = rdp_plan_historical_campaign.main(
        [
            "--symbol",
            "DOGE-USDT-SWAP",
            "--start",
            "2026-08-01",
            "--days",
            "1",
            "--storage-root",
            str(tmp_path / "storage"),
            "--manifest-output",
            str(tmp_path / "manifest.json"),
        ]
    )

    assert code == 4
    assert "instrument_scope_unsupported_or_unproven" in capsys.readouterr().err


def test_unverified_snapshot_blocks_before_database_or_network(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    def forbidden_session(*_args, **_kwargs):  # pragma: no cover - safety assertion
        raise AssertionError("unverified snapshot must stop before database access")

    monkeypatch.setattr(rdp_plan_historical_campaign, "get_session", forbidden_session)
    start = rdp_plan_historical_campaign._day("2026-08-01")
    snapshot = InstrumentContractSnapshot(
        venue="OKX",
        contract=InstrumentContract(
            symbol="BTC-USDT-SWAP",
            instrument_type="swap",
            contract_type="linear",
            base_currency="BTC",
            quote_currency="USDT",
            settle_currency="USDT",
            contract_value=Decimal("0.01"),
            contract_multiplier=Decimal("1"),
            contract_value_currency="BTC",
            lot_size=Decimal("1"),
            min_size=Decimal("1"),
            tick_size=Decimal("0.1"),
        ),
        observed_at=start + timedelta(days=1),
        effective_from=start,
        effective_to=start + timedelta(days=1),
        evidence_kind="observed_forward",
        source_locator="immutable://test/unverified-observation",
        source_schema="aats-instrument-observation-window-v1",
        source_payload_sha256="e" * 64,
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    code = rdp_plan_historical_campaign.main(
        [
            "--symbol",
            "BTC-USDT-SWAP",
            "--start",
            "2026-08-01",
            "--days",
            "1",
            "--storage-root",
            str(tmp_path / "storage"),
            "--manifest-output",
            str(tmp_path / "manifest.json"),
            "--instrument-snapshot",
            str(snapshot_path),
            "--apply",
            "--confirm",
        ]
    )

    assert code == 4
    assert "instrument_snapshot_observation_evidence_unverified" in (
        capsys.readouterr().err
    )
