from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import rdp_backfill_okx_rest_history as cli


def test_database_config_reads_only_allowlisted_keys_without_env_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    for key in cli._DATABASE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("AATS_TRADING_PRODUCT_TYPE", raising=False)
    monkeypatch.delenv("AATS_DERIVATIVES_POSITION_MODE", raising=False)
    (tmp_path / ".env.wsl2").write_text(
        "POSTGRES_USER=test-user\n"
        "AATS_TRADING_PRODUCT_TYPE=spot\n"
        "AATS_DERIVATIVES_POSITION_MODE=hedge\n",
        encoding="utf-8",
    )

    values = cli._database_configuration()

    assert values == {"POSTGRES_USER": "test-user"}
    assert "AATS_TRADING_PRODUCT_TYPE" not in os.environ
    assert "AATS_DERIVATIVES_POSITION_MODE" not in os.environ


@pytest.mark.parametrize(
    ("argv", "reason_code"),
    (
        (
            [
                "--symbol",
                "DOGE-USDT-SWAP",
                "--skip-oi",
                "--skip-mark",
                "--apply",
            ],
            "instrument_scope_unsupported_or_unproven",
        ),
        (
            [
                "--symbol",
                "BTC-USDT-SWAP",
                "--ccy",
                "DOGE",
                "--skip-oi",
                "--skip-mark",
                "--apply",
            ],
            "instrument_scope_symbol_ccy_mismatch",
        ),
    ),
)
def test_invalid_scope_stops_before_database_network_or_worker(
    argv: list[str],
    reason_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    forbidden = AssertionError("scope gate must stop before side effects")

    with (
        patch.object(cli, "resolve_db_url", side_effect=forbidden),
        patch.object(cli, "_build_session_factory", side_effect=forbidden),
        patch.object(cli, "run_verify", side_effect=forbidden),
        patch.object(cli, "run_oi_history", side_effect=forbidden),
        patch.object(cli, "run_mark_candles", side_effect=forbidden),
        patch.object(cli, "run_ls_ratio", side_effect=forbidden),
        caplog.at_level(logging.ERROR, logger="rdp_backfill_okx_rest_history"),
    ):
        result = cli.main(argv)

    assert result == 1
    assert reason_code in caplog.text


def test_supported_swap_binds_normalized_base_currency_to_ls_worker() -> None:
    estimate = {
        "estimated_pages": 0,
        "estimated_rows": 0,
        "estimated_seconds_at_default_rate": 0.0,
    }

    with patch.object(cli, "run_ls_ratio", return_value=estimate) as run_ls_ratio:
        result = cli.main(
            [
                "--symbol",
                " eth-usdt-swap ",
                "--ccy",
                " eth ",
                "--skip-oi",
                "--skip-mark",
            ]
        )

    assert result == 0
    run_ls_ratio.assert_called_once()
    assert run_ls_ratio.call_args.kwargs["ccy"] == "ETH"
