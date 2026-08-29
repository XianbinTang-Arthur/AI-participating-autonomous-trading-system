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

    result = module._gateway_published_bindings_from_inspect(json.loads(bindings_json))

    assert {item["host_ip"] for item in result} == {"127.0.0.1", "::1"}


@pytest.mark.parametrize("host_ip", ["", "0.0.0.0", "::", "192.0.2.10"])
def test_gateway_binding_evidence_rejects_non_loopback(host_ip: str) -> None:
    module = _load_evidence_module()
    bindings_json = json.dumps({"8001/tcp": [{"HostIp": host_ip, "HostPort": "8001"}]})

    with pytest.raises(RuntimeError, match="gateway_binding_not_loopback"):
        module._gateway_published_bindings_from_inspect(json.loads(bindings_json))


@pytest.mark.parametrize("raw", [None, "not-json", {}, {"8001/tcp": None}])
def test_gateway_binding_evidence_rejects_missing_or_malformed_bindings(raw: object) -> None:
    module = _load_evidence_module()

    with pytest.raises(RuntimeError):
        module._gateway_published_bindings_from_inspect(raw)


def test_deployment_evidence_requires_exact_profile_topology() -> None:
    module = _load_evidence_module()

    with pytest.raises(ValueError, match="required_container_set_mismatch"):
        module.build_evidence(
            repo_root=REPO_ROOT,
            profile="derivatives",
            overlay="docker-compose.aats.derivatives.yml",
            schema_job_status="passed",
            runtime_readiness_generation="aaaaaaaaaaaa-20260824T000000Z-123-456",
            deployment_lock_id="test-deployment-lock",
            deployed_commit="a" * 40,
            required_containers=("aats-execution",),
            nats_stream_probe=lambda: (),
            lifecycle_monitor_control_dir=Path("/tmp/unused-fs005-monitor"),
            lifecycle_monitor_token="unused-fs005-monitor-token",
            app_up_authorized_ns=1,
            health_boundary_started_ns=1,
            health_boundary_app_fingerprint="sha256:" + "1" * 64,
            collector_heartbeat_epochs={},
        )
