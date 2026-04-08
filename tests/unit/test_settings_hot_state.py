"""Stage 6 Slice 6.1 单元测试：AATS_HOT_STATE_BACKEND / AATS_HOT_STATE_REDIS_URL 设置门控。

覆盖：

1. 默认值：hot_state_backend=memory（保持单进程拓扑零外部依赖）
2. 字段加载：env var AATS_HOT_STATE_BACKEND / AATS_HOT_STATE_REDIS_URL /
   AATS_HOT_STATE_GLOBAL_PREFIX 都被 BaseSettings 自动加载
3. 跨字段约束：当 hot_state_backend=redis 时，hot_state_redis_url 必须非空 +
   scheme 是 redis:// 或 rediss://

这些测试都是 settings 层的"形状验证"——不实际连 Redis 服务器，
让 build_runtime 之前就能 fail-fast，避免到了 build_hot_state_store 真正去
connect Redis 才崩出难诊断的连接错误。

设计文档：docs/task/stage_6_redis_hot_state_design.md
"""
from __future__ import annotations

import pytest

from aats.bootstrap.settings import AATSSettings


# ─────────────────────────────────────────────────────────────────────
# 默认值
# ─────────────────────────────────────────────────────────────────────


def test_default_hot_state_backend_is_memory() -> None:
    """默认未设置：memory（单进程拓扑零外部依赖）。"""
    settings = AATSSettings.model_validate({})
    assert settings.hot_state_backend == "memory"


def test_default_hot_state_redis_url_points_to_local_docker_compose() -> None:
    """默认 hot_state_redis_url 指向 deploy/wsl2-dev/docker-compose 暴露的本地端口。"""
    settings = AATSSettings.model_validate({})
    assert settings.hot_state_redis_url == "redis://127.0.0.1:6379/0"


def test_default_hot_state_global_prefix_is_empty() -> None:
    """默认全局前缀为空——key 直接走 aats:hot:* namespace。"""
    settings = AATSSettings.model_validate({})
    assert settings.hot_state_global_prefix == ""


# ─────────────────────────────────────────────────────────────────────
# 显式 dict 加载
# ─────────────────────────────────────────────────────────────────────


def test_explicit_memory_backend_accepted() -> None:
    """显式指定 memory 也合法，等价于默认。"""
    settings = AATSSettings.model_validate({"hot_state_backend": "memory"})
    assert settings.hot_state_backend == "memory"


def test_explicit_redis_backend_with_default_url_accepted() -> None:
    """指定 redis backend 时使用默认 URL 也是合法的（开发态）。"""
    settings = AATSSettings.model_validate({"hot_state_backend": "redis"})
    assert settings.hot_state_backend == "redis"
    assert settings.hot_state_redis_url == "redis://127.0.0.1:6379/0"


def test_explicit_redis_backend_with_custom_url_accepted() -> None:
    """生产环境通过自定义 URL 指向真 Redis cluster。"""
    settings = AATSSettings.model_validate(
        {
            "hot_state_backend": "redis",
            "hot_state_redis_url": "redis://prod-redis.internal:6379/2",
        }
    )
    assert settings.hot_state_redis_url == "redis://prod-redis.internal:6379/2"


def test_explicit_redis_backend_with_rediss_scheme_accepted() -> None:
    """rediss:// (TLS) 也是合法 scheme。"""
    settings = AATSSettings.model_validate(
        {
            "hot_state_backend": "redis",
            "hot_state_redis_url": "rediss://secure-redis.example.com:6380/0",
        }
    )
    assert settings.hot_state_redis_url == "rediss://secure-redis.example.com:6380/0"


@pytest.mark.parametrize(
    "bad_value",
    ["bogus", "in_memory", "memcached", "kafka", "x"],
)
def test_invalid_hot_state_backend_rejected(bad_value: str) -> None:
    """非法 hot_state_backend 必须被 ValidationError 拒绝。
    注意 'in_memory' 也被拒绝——HotStateStore 关键字是 memory（短一些）。"""
    with pytest.raises(ValueError):
        AATSSettings.model_validate({"hot_state_backend": bad_value})


# ─────────────────────────────────────────────────────────────────────
# 环境变量加载
# ─────────────────────────────────────────────────────────────────────


def test_env_var_aats_hot_state_backend_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AATS_HOT_STATE_BACKEND 环境变量必须被 BaseSettings 自动加载。"""
    monkeypatch.setenv("AATS_HOT_STATE_BACKEND", "redis")
    monkeypatch.setenv("AATS_HOT_STATE_REDIS_URL", "redis://127.0.0.1:6379/0")
    settings = AATSSettings()
    assert settings.hot_state_backend == "redis"


def test_env_var_aats_hot_state_redis_url_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AATS_HOT_STATE_REDIS_URL 可被环境变量覆盖。"""
    monkeypatch.setenv("AATS_HOT_STATE_BACKEND", "redis")
    monkeypatch.setenv(
        "AATS_HOT_STATE_REDIS_URL", "redis://staging-redis.internal:6379/1"
    )
    settings = AATSSettings()
    assert settings.hot_state_redis_url == "redis://staging-redis.internal:6379/1"


def test_env_var_aats_hot_state_global_prefix_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AATS_HOT_STATE_GLOBAL_PREFIX 用于多环境共享同一 Redis 时的命名空间隔离。"""
    monkeypatch.setenv("AATS_HOT_STATE_GLOBAL_PREFIX", "prod:")
    settings = AATSSettings()
    assert settings.hot_state_global_prefix == "prod:"


def test_env_var_hot_state_backend_unset_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置环境变量时仍为 memory。"""
    monkeypatch.delenv("AATS_HOT_STATE_BACKEND", raising=False)
    settings = AATSSettings()
    assert settings.hot_state_backend == "memory"


# ─────────────────────────────────────────────────────────────────────
# 跨字段 sanity check（model_validator after）
# ─────────────────────────────────────────────────────────────────────


def test_memory_backend_does_not_require_redis_url() -> None:
    """memory 模式即便 hot_state_redis_url 为空也应当通过校验——
    因为根本不会用 Redis。"""
    settings = AATSSettings.model_validate(
        {
            "hot_state_backend": "memory",
            "hot_state_redis_url": "",
        }
    )
    assert settings.hot_state_backend == "memory"


def test_redis_backend_requires_non_empty_redis_url() -> None:
    """redis backend 必须有非空 hot_state_redis_url。"""
    with pytest.raises(
        ValueError, match="hot_state_backend_requires_non_empty_redis_url"
    ):
        AATSSettings.model_validate(
            {
                "hot_state_backend": "redis",
                "hot_state_redis_url": "",
            }
        )


def test_redis_backend_requires_non_whitespace_redis_url() -> None:
    """全空白也算空——避免 ' ' 这种排版漏洞。"""
    with pytest.raises(
        ValueError, match="hot_state_backend_requires_non_empty_redis_url"
    ):
        AATSSettings.model_validate(
            {
                "hot_state_backend": "redis",
                "hot_state_redis_url": "   ",
            }
        )


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost:6379",
        "https://localhost:6379",
        "tcp://localhost:6379",
        "127.0.0.1:6379",
        "localhost",
    ],
)
def test_redis_backend_rejects_invalid_url_scheme(bad_url: str) -> None:
    """hot_state_redis_url 必须以 redis:// 或 rediss:// 开头，避免误填 http/tcp scheme。"""
    with pytest.raises(
        ValueError, match="hot_state_redis_url_must_start_with_redis_or_rediss_scheme"
    ):
        AATSSettings.model_validate(
            {
                "hot_state_backend": "redis",
                "hot_state_redis_url": bad_url,
            }
        )


@pytest.mark.parametrize(
    "valid_url",
    [
        "redis://127.0.0.1:6379/0",
        "redis://prod-redis.internal:6379/2",
        "rediss://secure-redis.example.com:6380/0",
    ],
)
def test_redis_backend_accepts_valid_url_schemes(valid_url: str) -> None:
    """redis:// 和 rediss:// 都是合法 scheme。"""
    settings = AATSSettings.model_validate(
        {
            "hot_state_backend": "redis",
            "hot_state_redis_url": valid_url,
        }
    )
    assert settings.hot_state_redis_url == valid_url
