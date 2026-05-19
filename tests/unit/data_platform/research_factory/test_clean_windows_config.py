import json
from pathlib import Path


def test_research_factory_clean_windows_config_is_parseable() -> None:
    path = Path("configs/research_factory/clean_windows.json")

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "research_factory_clean_windows_v1"
    assert payload["runtime_mutation_allowed"] is False
    assert payload["active_parameter_write_allowed"] is False
    assert payload["runtime_config_write_allowed"] is False
    assert payload["okx_write_allowed"] is False
    btc = payload["windows"]["BTC-USDT-SWAP"]
    assert btc["1h"]["rows"] == 2223
    assert btc["15m"]["rows"] == 8895
    assert btc["1h"]["source_funding_dataset_versions"] == ["v1.0"]
    assert btc["15m"]["source_funding_dataset_versions"] == ["v1.0"]
