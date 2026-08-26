from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from aats.bootstrap import process_lifecycle
from aats.bootstrap.process_lifecycle import (
    _announce_runtime_ready,
    _ready_key,
    _runtime_readiness_generation,
    _strict_peer_readiness_required,
    _wait_for_peer_roles_ready,
    _withdraw_runtime_ready,
    run_process,
)
from aats.bootstrap.settings import AATSSettings
from apps.api_gateway import main as gateway_main


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATION = "00b6df0f8a8d-20260824T120000Z-123-456"


class _RecordingHotStateStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.deleted: list[str] = []

    async def set(self, key: str, value: object, *, ttl_seconds: float | None = None) -> None:
        assert ttl_seconds == 300.0
        self.values[key] = value

    async def get_many(self, keys: list[str]) -> dict[str, object]:
        return {key: self.values[key] for key in keys if key in self.values}

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)


def _settings(**updates):
    values = {
        "event_bus_backend": "hybrid",
        "runtime_readiness_generation": GENERATION,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_readiness_generation_setting_normalizes_and_rejects_key_injection() -> None:
    settings = AATSSettings.model_validate(
        {"runtime_readiness_generation": f"  {GENERATION}  "}
    )
    assert settings.runtime_readiness_generation == GENERATION

    for invalid in ("bad generation", "bad/generation", "x" * 129, 123):
        with pytest.raises(ValueError, match="runtime_readiness_generation"):
            AATSSettings.model_validate({"runtime_readiness_generation": invalid})


def test_strict_requirement_and_missing_generation_fail_before_runtime_build() -> None:
    assert _strict_peer_readiness_required(role="market", settings=_settings()) is True
    assert (
        _strict_peer_readiness_required(
            role="market",
            settings=_settings(event_bus_backend="in_memory"),
        )
        is False
    )
    assert _strict_peer_readiness_required(role="monolith", settings=_settings()) is False

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:market"):
        _runtime_readiness_generation(
            role="market",
            settings=_settings(runtime_readiness_generation=None),
            required=True,
        )


@pytest.mark.asyncio
async def test_strict_announce_is_generation_scoped_and_redis_failure_is_fatal() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.announce")
    await _announce_runtime_ready(
        role="market",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
        required=True,
    )
    key = _ready_key("market", generation=GENERATION)
    assert key == f"aats:runtime:ready:{GENERATION}:market"
    assert store.values[key]["generation"] == GENERATION
    assert store.values[key]["process_role"] == "market"

    class _BrokenStore:
        async def set(self, *_args, **_kwargs) -> None:
            raise ConnectionError("sensitive redis endpoint must not be forwarded")

    with pytest.raises(RuntimeError, match="runtime_ready_gate_announce_failed:market") as raised:
        await _announce_runtime_ready(
            role="market",
            hot_state_store=_BrokenStore(),
            logger=logger,
            generation=GENERATION,
            required=True,
        )
    assert "endpoint" not in str(raised.value)

    with pytest.raises(RuntimeError, match="runtime_ready_gate_hot_state_required:market"):
        await _announce_runtime_ready(
            role="market",
            hot_state_store=None,
            logger=logger,
            generation=GENERATION,
            required=True,
        )

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:market"):
        await _announce_runtime_ready(
            role="market",
            hot_state_store=store,
            logger=logger,
            required=True,
        )


@pytest.mark.asyncio
async def test_strict_wait_requires_exact_generation_and_role_then_succeeds() -> None:
    store = _RecordingHotStateStore()
    logger = logging.getLogger("test.fs016.wait")
    peer_key = _ready_key("market", generation=GENERATION)
    store.values[peer_key] = {
        "process_role": "execution",
        "generation": GENERATION,
    }

    with pytest.raises(RuntimeError, match="runtime_ready_gate_timeout:decision:market"):
        await _wait_for_peer_roles_ready(
            role="decision",
            hot_state_store=store,
            logger=logger,
            peers=("market",),
            timeout_seconds=0.0,
            poll_interval=0.0,
            generation=GENERATION,
            required=True,
        )

    store.values[peer_key] = {
        "process_role": "market",
        "generation": "old-generation",
    }
    with pytest.raises(RuntimeError, match="runtime_ready_gate_timeout:decision:market"):
        await _wait_for_peer_roles_ready(
            role="decision",
            hot_state_store=store,
            logger=logger,
            peers=("market",),
            timeout_seconds=0.0,
            poll_interval=0.0,
            generation=GENERATION,
            required=True,
        )

    store.values[peer_key] = {
        "process_role": "market",
        "generation": GENERATION,
    }
    await _wait_for_peer_roles_ready(
        role="decision",
        hot_state_store=store,
        logger=logger,
        peers=("market",),
        timeout_seconds=0.0,
        poll_interval=0.0,
        generation=GENERATION,
        required=True,
    )


@pytest.mark.asyncio
async def test_strict_poll_error_is_fixed_failure_and_withdraw_is_exact() -> None:
    logger = logging.getLogger("test.fs016.poll")

    class _BrokenStore:
        async def get_many(self, _keys):
            raise ConnectionError("redis://credential@host")

    with pytest.raises(RuntimeError, match="runtime_ready_gate_poll_failed:execution") as raised:
        await _wait_for_peer_roles_ready(
            role="execution",
            hot_state_store=_BrokenStore(),
            logger=logger,
            peers=("decision",),
            generation=GENERATION,
            required=True,
        )
    assert "credential" not in str(raised.value)

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:execution"):
        await _wait_for_peer_roles_ready(
            role="execution",
            hot_state_store=_RecordingHotStateStore(),
            logger=logger,
            peers=("decision",),
            required=True,
        )

    store = _RecordingHotStateStore()
    key = _ready_key("execution", generation=GENERATION)
    store.values[key] = {"process_role": "execution", "generation": GENERATION}
    await _withdraw_runtime_ready(
        role="execution",
        hot_state_store=store,
        logger=logger,
        generation=GENERATION,
    )
    assert store.deleted == [key]
    assert key not in store.values


def test_run_process_missing_generation_never_builds_or_starts(monkeypatch) -> None:
    build_calls = 0

    async def _build(*_args, **_kwargs):
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("build_runtime must not run")

    monkeypatch.setattr(process_lifecycle, "build_runtime", _build)
    monkeypatch.setattr(process_lifecycle, "configure_logging_for_settings", lambda _settings: None)

    result = asyncio.run(
        run_process(
            process_role="market",
            app_name="test.fs016.run_process",
            settings=_settings(runtime_readiness_generation=None),
            stop_event=asyncio.Event(),
        )
    )
    assert result == 1
    assert build_calls == 0


@pytest.mark.asyncio
async def test_gateway_missing_generation_fails_before_schema_or_runtime(monkeypatch) -> None:
    schema_calls = 0
    build_calls = 0

    def _validate_schema() -> None:
        nonlocal schema_calls
        schema_calls += 1

    async def _build(*_args, **_kwargs):
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("build_runtime must not run")

    monkeypatch.setattr(
        gateway_main,
        "load_settings",
        lambda: _settings(runtime_readiness_generation=None),
    )
    monkeypatch.setattr(gateway_main, "configure_logging_for_settings", lambda _settings: None)
    monkeypatch.setattr(gateway_main, "build_runtime", _build)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", _validate_schema)

    with pytest.raises(RuntimeError, match="runtime_ready_gate_generation_required:gateway"):
        async with gateway_main.lifespan(FastAPI()):
            raise AssertionError("lifespan must not yield")
    assert schema_calls == 0
    assert build_calls == 0


@pytest.mark.asyncio
async def test_gateway_barrier_failure_stops_runtime_without_starting_publishers(monkeypatch) -> None:
    calls: list[str] = []
    runtime = SimpleNamespace(
        hot_state_store=_RecordingHotStateStore(),
        start_background_tasks=lambda: None,
    )

    async def _start() -> None:
        calls.append("start")

    async def _stop() -> None:
        calls.append("stop")

    runtime.start_background_tasks = _start
    runtime.stop_background_tasks = _stop

    monkeypatch.setattr(gateway_main, "load_settings", lambda: _settings())
    monkeypatch.setattr(gateway_main, "configure_logging_for_settings", lambda _settings: None)
    monkeypatch.setattr(
        gateway_main,
        "build_runtime",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime),
    )

    async def _fail_wait(**_kwargs) -> None:
        raise RuntimeError("runtime_ready_gate_timeout:gateway:market")

    monkeypatch.setattr(gateway_main, "_wait_for_peer_roles_ready", _fail_wait)
    monkeypatch.setattr("aats.data_platform.db.validate_rdp_schema", lambda: None)

    local_app = FastAPI()
    with pytest.raises(RuntimeError, match="runtime_ready_gate_timeout"):
        async with gateway_main.lifespan(local_app):
            raise AssertionError("lifespan must not yield")
    assert calls == ["stop"]
    assert not hasattr(local_app.state, "runtime")


def test_standard_deploy_generates_and_injects_required_generation() -> None:
    deploy_source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    compose_source = (
        REPO_ROOT / "deploy" / "wsl2-dev" / "docker-compose.aats.yml"
    ).read_text(encoding="utf-8")

    assert "prepare_runtime_readiness_generation()" in deploy_source
    assert deploy_source.index("step_sync\n") < deploy_source.index(
        "prepare_runtime_readiness_generation\n"
    ) < deploy_source.index("step_build\n")
    assert "AATS_RUNTIME_READINESS_GENERATION='" in deploy_source
    assert "AATS_DEPLOYED_GIT_COMMIT='" in deploy_source
    assert "--runtime-readiness-generation '$RUNTIME_READINESS_GENERATION'" in deploy_source
    assert (
        'AATS_RUNTIME_READINESS_GENERATION: "${AATS_RUNTIME_READINESS_GENERATION:?'
        in compose_source
    )
    assert (
        'AATS_DEPLOYED_GIT_COMMIT: "${AATS_DEPLOYED_GIT_COMMIT:?'
        in compose_source
    )
