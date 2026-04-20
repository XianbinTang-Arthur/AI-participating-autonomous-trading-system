"""Stage 4 单元测试：AATS_EVENT_BUS_BACKEND / AATS_NATS_URL 等设置门控。

覆盖：

1. settings.event_bus_backend 默认 in_memory（向后兼容）
2. AATSSettings 验证器：合法集合归一化、空值处理、非法值拒绝
3. AATS_EVENT_BUS_BACKEND 环境变量被 BaseSettings 自动加载
4. 跨字段约束：当 backend 选择 hybrid/nats 时，nats_url / stream_name /
   max_age 必须 sanity-check 通过

这些测试都是 settings 层的"形状验证"——不实际连 NATS 服务器，
是为了让 build_runtime 之前就能 fail-fast，避免到了 _build_shared_runtime_slice
才崩出难诊断的连接错误。
"""
from __future__ import annotations

import pytest

from aats.bootstrap.settings import (
    ALLOWED_EVENT_BUS_BACKENDS,
    EVENT_BUS_BACKEND_HYBRID,
    EVENT_BUS_BACKEND_IN_MEMORY,
    EVENT_BUS_BACKEND_NATS,
    AATSSettings,
)


# ─────────────────────────────────────────────────────────────────────
# 默认值 / 合法集合
# ─────────────────────────────────────────────────────────────────────


def test_default_event_bus_backend_is_in_memory() -> None:
    """默认未设置：in_memory（保持单进程拓扑零外部依赖）。"""
    settings = AATSSettings.model_validate({})
    assert settings.event_bus_backend == "in_memory"


def test_default_nats_url_points_to_local_docker_compose() -> None:
    """默认 nats_url 指向 deploy/wsl2-dev/docker-compose 暴露的本地端口。"""
    settings = AATSSettings.model_validate({})
    assert settings.nats_url == "nats://127.0.0.1:4222"


def test_default_nats_stream_name_matches_runbook() -> None:
    """默认 stream 名跟 RUNBOOK / docker-compose 文档约定一致。"""
    settings = AATSSettings.model_validate({})
    assert settings.nats_stream_name == "AATS_EVENTS"


def test_default_nats_stream_max_age_is_one_day() -> None:
    """默认 retention 1 天 — NATS stream 是 hot buffer，不承担长期存档
    （长期合规/回放由 PG event_store 承担，见
    docs/task/aats_events_stream_retention_root_fix_sow.md）。"""
    settings = AATSSettings.model_validate({})
    assert settings.nats_stream_max_age_seconds == 24 * 60 * 60


def test_allowed_event_bus_backends_set_contents() -> None:
    """合法集合 = {in_memory, hybrid, nats}，与 README 命名一致。"""
    assert ALLOWED_EVENT_BUS_BACKENDS == frozenset(
        {
            EVENT_BUS_BACKEND_IN_MEMORY,
            EVENT_BUS_BACKEND_HYBRID,
            EVENT_BUS_BACKEND_NATS,
        }
    )


# ─────────────────────────────────────────────────────────────────────
# 字符串归一化
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("in_memory", "in_memory"),
        ("IN_MEMORY", "in_memory"),
        ("In_Memory", "in_memory"),
        ("hybrid", "hybrid"),
        ("HYBRID", "hybrid"),
        ("  hybrid  ", "hybrid"),
        ("nats", "nats"),
        ("NATS", "nats"),
        ("", "in_memory"),
        ("   ", "in_memory"),
        (None, "in_memory"),
    ],
)
def test_event_bus_backend_normalized(raw: object, expected: str) -> None:
    """字符串归一化：去空白 + 转小写；空值 fallback 到 in_memory 默认。"""
    settings = AATSSettings.model_validate({"event_bus_backend": raw})
    assert settings.event_bus_backend == expected


@pytest.mark.parametrize(
    "bad_value",
    ["bogus", "memory", "nats_jetstream", "kafka", "rabbitmq", "x"],
)
def test_invalid_event_bus_backend_rejected(bad_value: str) -> None:
    """非法 event_bus_backend 必须被 ValueError 拒绝。
    注意 'memory' 也被拒绝——README 明确规定关键字是 in_memory。"""
    with pytest.raises(ValueError, match="event_bus_backend"):
        AATSSettings.model_validate({"event_bus_backend": bad_value})


def test_non_string_event_bus_backend_rejected() -> None:
    """非字符串类型被拒绝。"""
    with pytest.raises(ValueError, match="event_bus_backend must be string"):
        AATSSettings.model_validate({"event_bus_backend": 123})


# ─────────────────────────────────────────────────────────────────────
# 环境变量加载
# ─────────────────────────────────────────────────────────────────────


def test_env_var_aats_event_bus_backend_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AATS_EVENT_BUS_BACKEND 环境变量必须被 BaseSettings 自动加载。"""
    monkeypatch.setenv("AATS_EVENT_BUS_BACKEND", "hybrid")
    monkeypatch.setenv("AATS_NATS_URL", "nats://127.0.0.1:4222")
    settings = AATSSettings()
    assert settings.event_bus_backend == "hybrid"


def test_env_var_event_bus_backend_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量大小写/空白也走归一化。"""
    monkeypatch.setenv("AATS_EVENT_BUS_BACKEND", "  HYBRID  ")
    monkeypatch.setenv("AATS_NATS_URL", "nats://127.0.0.1:4222")
    settings = AATSSettings()
    assert settings.event_bus_backend == "hybrid"


def test_env_var_event_bus_backend_invalid_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量非法值在加载阶段就抛错。"""
    monkeypatch.setenv("AATS_EVENT_BUS_BACKEND", "bogus_backend")
    with pytest.raises(ValueError, match="event_bus_backend"):
        AATSSettings()


def test_env_var_event_bus_backend_unset_falls_back_to_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置环境变量时仍为 in_memory。"""
    monkeypatch.delenv("AATS_EVENT_BUS_BACKEND", raising=False)
    settings = AATSSettings()
    assert settings.event_bus_backend == "in_memory"


def test_env_var_nats_url_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """AATS_NATS_URL 环境变量被加载，覆盖默认 127.0.0.1:4222。"""
    monkeypatch.setenv("AATS_EVENT_BUS_BACKEND", "hybrid")
    monkeypatch.setenv("AATS_NATS_URL", "nats://nats-server.internal:4222")
    settings = AATSSettings()
    assert settings.nats_url == "nats://nats-server.internal:4222"


def test_env_var_nats_stream_name_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """AATS_NATS_STREAM_NAME 可被环境变量覆盖。"""
    monkeypatch.setenv("AATS_EVENT_BUS_BACKEND", "hybrid")
    monkeypatch.setenv("AATS_NATS_STREAM_NAME", "AATS_TEST_STREAM")
    settings = AATSSettings()
    assert settings.nats_stream_name == "AATS_TEST_STREAM"


# ─────────────────────────────────────────────────────────────────────
# 跨字段 sanity check（model_validator after）
# ─────────────────────────────────────────────────────────────────────


def test_in_memory_backend_does_not_require_nats_url() -> None:
    """in_memory 模式即便 nats_url 为空也应当通过校验——
    因为根本不会用 NATS。"""
    settings = AATSSettings.model_validate(
        {
            "event_bus_backend": "in_memory",
            "nats_url": "",
        }
    )
    assert settings.event_bus_backend == "in_memory"


@pytest.mark.parametrize("backend", ["hybrid", "nats"])
def test_nats_backend_requires_non_empty_nats_url(backend: str) -> None:
    """hybrid / nats 模式必须有非空 nats_url。"""
    with pytest.raises(ValueError, match="event_bus_backend_requires_non_empty_nats_url"):
        AATSSettings.model_validate(
            {
                "event_bus_backend": backend,
                "nats_url": "",
            }
        )


@pytest.mark.parametrize("backend", ["hybrid", "nats"])
def test_nats_backend_rejects_invalid_url_scheme(backend: str) -> None:
    """nats_url 必须以 nats:// 或 tls:// 开头，避免误填 http/ws scheme。"""
    with pytest.raises(ValueError, match="nats_url_must_start_with_nats_or_tls_scheme"):
        AATSSettings.model_validate(
            {
                "event_bus_backend": backend,
                "nats_url": "http://localhost:4222",
            }
        )


@pytest.mark.parametrize(
    "valid_url",
    [
        "nats://127.0.0.1:4222",
        "nats://nats-server.internal:4222",
        "tls://nats.example.com:4443",
    ],
)
def test_nats_backend_accepts_valid_url_schemes(valid_url: str) -> None:
    """nats:// 和 tls:// 都是合法 scheme。"""
    settings = AATSSettings.model_validate(
        {
            "event_bus_backend": "hybrid",
            "nats_url": valid_url,
        }
    )
    assert settings.nats_url == valid_url


@pytest.mark.parametrize("backend", ["hybrid", "nats"])
def test_nats_backend_requires_non_empty_stream_name(backend: str) -> None:
    """hybrid / nats 模式必须有非空 stream_name。"""
    with pytest.raises(
        ValueError, match="event_bus_backend_requires_non_empty_nats_stream_name"
    ):
        AATSSettings.model_validate(
            {
                "event_bus_backend": backend,
                "nats_stream_name": "   ",
            }
        )


@pytest.mark.parametrize("backend", ["hybrid", "nats"])
def test_nats_backend_requires_positive_max_age(backend: str) -> None:
    """nats_stream_max_age_seconds 必须 ≥ 1（不允许 0 或负数）。"""
    with pytest.raises(ValueError, match="nats_stream_max_age_seconds_must_be_positive"):
        AATSSettings.model_validate(
            {
                "event_bus_backend": backend,
                "nats_stream_max_age_seconds": 0,
            }
        )
