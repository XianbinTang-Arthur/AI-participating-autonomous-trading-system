from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml"
EVIDENCE_SCRIPT = REPO_ROOT / "scripts" / "write_deployment_evidence.py"


def _load_evidence_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("write_deployment_evidence_fs005", EVIDENCE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gateway_host_port_is_fixed_to_ipv4_loopback() -> None:
    source = COMPOSE_FILE.read_text(encoding="utf-8")

    assert '"127.0.0.1:${AATS_API_PORT:-8000}:${AATS_API_PORT:-8000}"' in source
    assert '- "${AATS_API_PORT:-8000}:${AATS_API_PORT:-8000}"' not in source
    assert '"--host"\n      - "0.0.0.0"' in source


def test_gateway_binding_evidence_accepts_only_loopback() -> None:
    module = _load_evidence_module()
    bindings_json = json.dumps(
        {
            "8001/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": "8001"},
                {"HostIp": "::1", "HostPort": "8001"},
            ]
        }
    )

    result = module._gateway_published_bindings(lambda _args, _cwd=None: bindings_json)

    assert {item["host_ip"] for item in result} == {"127.0.0.1", "::1"}


@pytest.mark.parametrize("host_ip", ["", "0.0.0.0", "::", "192.0.2.10"])
def test_gateway_binding_evidence_rejects_non_loopback(host_ip: str) -> None:
    module = _load_evidence_module()
    bindings_json = json.dumps({"8001/tcp": [{"HostIp": host_ip, "HostPort": "8001"}]})

    with pytest.raises(RuntimeError, match="gateway_published_on_non_loopback"):
        module._gateway_published_bindings(lambda _args, _cwd=None: bindings_json)


@pytest.mark.parametrize("raw", ["not-json", "null", "{}", '{"8001/tcp": null}'])
def test_gateway_binding_evidence_rejects_missing_or_malformed_bindings(raw: str) -> None:
    module = _load_evidence_module()

    with pytest.raises(RuntimeError):
        module._gateway_published_bindings(lambda _args, _cwd=None: raw)


def test_deployment_evidence_requires_gateway_in_required_topology() -> None:
    module = _load_evidence_module()

    with pytest.raises(ValueError, match="required_gateway_missing"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives",
            overlay="docker-compose.aats.derivatives.yml",
            schema_job_status="passed",
            runtime_readiness_generation="aaaaaaaaaaaa-20260824T000000Z-123-456",
            required_containers=("aats-execution",),
        )
