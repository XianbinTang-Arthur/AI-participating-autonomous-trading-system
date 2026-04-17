"""P0-3：strategy tuning overrides 去 JSON / cache 降级回归。

锁住 DB-主 + 进程内 last-known cache + AATS_P0_TUNING_FAIL_LOUD 开关三条契约：

  1. DB 读成功 → 更新 cache；返回 payload 带 stale=False
  2. DB 抖动 + cache 命中 → 打 warning，返回 cache 副本带 stale=True
  3. cold start（cache 空）+ DB 挂 → RuntimeError
  4. AATS_P0_TUNING_FAIL_LOUD=on → 即使 cache 有也不降级，直接抛
  5. refresh 派生 overrides 后必须刷 cache，保证 apply 后立刻有可降级副本
  6. JSON 副本只在 AATS_P0_TUNING_JSON_EXPORT=on 时导出

其它测试（test_strategy_tuning_review 等）会通过 refresh 路径顺带污染 cache；
这里用 _reset_overrides_cache_for_tests 在每条用例入口清空，确保各 case 独立。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from aats.data_platform.operations import strategy_tuning_registry as mod
from aats.data_platform.operations.strategy_tuning_registry import (
    _reset_overrides_cache_for_tests,
    load_strategy_tuning_overrides,
    refresh_strategy_tuning_overrides,
    save_strategy_tuning_overrides,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """每条用例入口清 cache，避免用例间相互污染。"""
    _reset_overrides_cache_for_tests()
    yield
    _reset_overrides_cache_for_tests()


class _FakeEngine:
    def __init__(self, *, fail_on_session: bool = False) -> None:
        self.disposed = False
        self.fail_on_session = fail_on_session

    def dispose(self) -> None:
        self.disposed = True


class _SessionCtx:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload

    def __enter__(self) -> _SessionCtx:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _patch_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_ok: bool,
    payload: dict[str, Any] | Exception | None = None,
) -> _FakeEngine | None:
    """拼装 try_governance_db + db_load_strategy_tuning_overrides 的替身。

    db_ok=False：模拟 DB 不可达（try_governance_db 返回 None, False）
    db_ok=True + payload=dict：正常读到 payload
    db_ok=True + payload=Exception：DB 可达但查询抛异常
    """
    if not db_ok:
        monkeypatch.setattr(
            mod, "try_governance_db", lambda: (None, False),
        )
        return None

    engine = _FakeEngine()

    monkeypatch.setattr(
        mod, "try_governance_db", lambda: (engine, True),
    )
    monkeypatch.setattr(
        mod, "Session", lambda _engine: _SessionCtx(payload),
    )

    # db_load_strategy_tuning_overrides 在函数内部被 import；
    # 直接 patch 模块里的符号就足够。
    from aats.data_platform.governance import strategy_tuning_db

    def _fake_db_load(_session: Any) -> dict[str, Any]:
        if isinstance(payload, Exception):
            raise payload
        # 负向用例会传非 dict 模拟 DB 契约违反；这里不做 assert，让异常路径
        # 由被测函数里的 isinstance 分支处理
        return payload  # type: ignore[return-value]

    monkeypatch.setattr(
        strategy_tuning_db, "db_load_strategy_tuning_overrides", _fake_db_load,
    )
    return engine


# =====================================================================
# load_strategy_tuning_overrides：DB hit / DB flicker / cold start / fail_loud
# =====================================================================


def test_load_db_hit_populates_cache_and_marks_stale_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """DB 读成功 → 返回 stale=False + source='db'；cache 被刷新。"""
    engine = _patch_db(
        monkeypatch,
        db_ok=True,
        payload={
            "generated_at": "2026-04-17T10:00:00+00:00",
            "combo_overrides": {"directional_1h": {"min_safe_net_edge_bps": 2.0}},
        },
    )

    result = load_strategy_tuning_overrides(tmp_path)

    assert result["stale"] is False
    assert result["source"] == "db"
    assert result["combo_overrides"] == {"directional_1h": {"min_safe_net_edge_bps": 2.0}}
    assert engine is not None and engine.disposed, \
        "engine.dispose() 必须在 finally 里被调到"

    # cache 被刷新：再调一次（即便 DB 失败）也能从 cache 拿到
    _patch_db(monkeypatch, db_ok=False)
    follow_up = load_strategy_tuning_overrides(tmp_path)
    assert follow_up["stale"] is True
    assert follow_up["source"] == "cache"
    assert follow_up["combo_overrides"] == {"directional_1h": {"min_safe_net_edge_bps": 2.0}}


def test_load_db_flicker_returns_cached_with_stale_flag_and_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """先一次 DB 成功填 cache，再模拟 DB 读抛异常 → 返回 cache + stale=True + warning。"""
    _patch_db(
        monkeypatch,
        db_ok=True,
        payload={
            "generated_at": "2026-04-17T10:00:00+00:00",
            "combo_overrides": {"independent_15m": {"min_safe_net_edge_bps": 1.5}},
        },
    )
    load_strategy_tuning_overrides(tmp_path)  # 填 cache

    # DB 可达但查询抛（模拟真实抖动 / timeout 而非连不上）
    _patch_db(monkeypatch, db_ok=True, payload=RuntimeError("db query timed out"))

    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        result = load_strategy_tuning_overrides(tmp_path)

    assert result["stale"] is True
    assert result["source"] == "cache"
    assert result["combo_overrides"] == {"independent_15m": {"min_safe_net_edge_bps": 1.5}}
    assert any(
        "DB 抖动" in rec.getMessage() or "DB 读取失败" in rec.getMessage()
        for rec in caplog.records
    ), "DB 抖动必须有 warning 可见"


def test_load_cold_start_db_unreachable_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """cold start（cache 为空）+ DB 不可达 → RuntimeError。

    这是期望的 fail-loud：没有任何 overrides 比默默跑空配置更安全。
    """
    _patch_db(monkeypatch, db_ok=False)

    with pytest.raises(RuntimeError, match="cold start"):
        load_strategy_tuning_overrides(tmp_path)


def test_load_fail_loud_flag_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """即使 cache 有，AATS_P0_TUNING_FAIL_LOUD=on 下 DB 失败仍抛。

    场景：故障演练 / 强制复现 DB 停机时的行为。
    """
    _patch_db(
        monkeypatch,
        db_ok=True,
        payload={
            "generated_at": "2026-04-17T10:00:00+00:00",
            "combo_overrides": {"independent_15m": {"min_safe_net_edge_bps": 1.5}},
        },
    )
    load_strategy_tuning_overrides(tmp_path)  # 填 cache

    monkeypatch.setenv("AATS_P0_TUNING_FAIL_LOUD", "on")
    _patch_db(monkeypatch, db_ok=False)

    with pytest.raises(RuntimeError, match="FAIL_LOUD"):
        load_strategy_tuning_overrides(tmp_path)


def test_load_db_returns_non_dict_falls_back_to_safe_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """DB 返回不是 dict 时不应炸；用安全 shape 兜底并打 warning。

    动机：半成品 payload 不能污染 cache；load 依旧必须返回可消费结果。
    """
    # 注意：当前 db_load_strategy_tuning_overrides 契约返回 dict，这里模拟
    # 极端回归场景（比如未来有人改签名）让 fake 返回一个 list。
    _patch_db(monkeypatch, db_ok=True, payload=["not", "a", "dict"])  # type: ignore[arg-type]

    result = load_strategy_tuning_overrides(tmp_path)
    # 走了 "payload is not None but not a dict" 兜底分支
    assert result["combo_overrides"] == {}
    assert result["stale"] is False


# =====================================================================
# refresh_strategy_tuning_overrides：cache + JSON 导出开关
# =====================================================================


def test_refresh_populates_cache_and_skips_json_when_flag_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """默认 AATS_P0_TUNING_JSON_EXPORT=off：refresh 只刷 cache，不写 JSON。"""
    monkeypatch.delenv("AATS_P0_TUNING_JSON_EXPORT", raising=False)

    registry = {
        "proposals": [
            {
                "status": "approved",
                "combo_key": "independent_15m",
                "parameter": "min_safe_net_edge_bps",
                "proposed_value": 1.5,
                "reviewed_at": "2026-04-17T09:00:00+00:00",
            },
        ],
    }
    returned_path = refresh_strategy_tuning_overrides(tmp_path, registry)

    assert returned_path == "", "JSON export 关掉时不返回路径"
    # 文件不应被创建
    assert not (tmp_path / "artifacts/governance/strategy_tuning_overrides.json").exists()

    # cache 被填 → 下一次 load 即使 DB 挂也能取到
    _patch_db(monkeypatch, db_ok=False)
    loaded = load_strategy_tuning_overrides(tmp_path)
    assert loaded["combo_overrides"] == {
        "independent_15m": {"min_safe_net_edge_bps": 1.5},
    }
    assert loaded["stale"] is True


def test_refresh_writes_json_when_flag_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """AATS_P0_TUNING_JSON_EXPORT=on：refresh 同时写 JSON 副本，路径返回。"""
    monkeypatch.setenv("AATS_P0_TUNING_JSON_EXPORT", "on")

    registry = {
        "proposals": [
            {
                "status": "approved",
                "combo_key": "directional_1h",
                "parameter": "min_safe_net_edge_bps",
                "proposed_value": 2.1,
                "reviewed_at": "2026-04-17T09:00:00+00:00",
            },
        ],
    }
    returned_path = refresh_strategy_tuning_overrides(tmp_path, registry)

    target = tmp_path / "artifacts/governance/strategy_tuning_overrides.json"
    assert returned_path == str(target)
    assert target.exists()
    disk_payload = json.loads(target.read_text(encoding="utf-8"))
    assert disk_payload["combo_overrides"] == {
        "directional_1h": {"min_safe_net_edge_bps": 2.1},
    }


# =====================================================================
# save_strategy_tuning_overrides：deprecated shim
# =====================================================================


def test_save_overrides_deprecation_warning_and_default_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """save_strategy_tuning_overrides 发出 DeprecationWarning；
    默认 JSON export 关 → 不落盘，返回 None。
    """
    monkeypatch.delenv("AATS_P0_TUNING_JSON_EXPORT", raising=False)

    with pytest.warns(DeprecationWarning, match="save_strategy_tuning_overrides"):
        result = save_strategy_tuning_overrides(
            tmp_path,
            {"combo_overrides": {"independent_15m": {"min_safe_net_edge_bps": 1.5}}},
        )

    assert result is None
    assert not (tmp_path / "artifacts/governance/strategy_tuning_overrides.json").exists()


def test_save_overrides_writes_when_flag_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """AATS_P0_TUNING_JSON_EXPORT=on 时 shim 还是会写出 JSON（历史兼容）。"""
    monkeypatch.setenv("AATS_P0_TUNING_JSON_EXPORT", "on")

    with pytest.warns(DeprecationWarning):
        path = save_strategy_tuning_overrides(
            tmp_path,
            {"combo_overrides": {"independent_15m": {"min_safe_net_edge_bps": 1.5}}},
        )

    assert path is not None
    assert path.exists()


# =====================================================================
# M-R1：save_strategy_tuning_registry 在 DB 不可达时的 fail-loud 契约
# =====================================================================


def test_save_registry_json_only_when_fail_loud_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """默认 AATS_P0_TUNING_FAIL_LOUD 未设置：DB 不可达时走单机兼容模式，
    仅写 JSON 并打 warning，不抛。
    """
    monkeypatch.delenv("AATS_P0_TUNING_FAIL_LOUD", raising=False)
    monkeypatch.setattr(mod, "try_governance_db", lambda: (None, False))

    registry: dict[str, Any] = {
        "proposals": [
            {
                "proposal_id": "tprop_local",
                "combo_key": "independent_15m",
                "parameter": "min_safe_net_edge_bps",
                "proposed_value": 1.5,
                "status": "pending_review",
            },
        ],
    }

    with caplog.at_level(logging.WARNING, logger=mod.__name__):
        path = mod.save_strategy_tuning_registry(tmp_path, registry)

    assert path.exists(), "fail-loud off 时必须写 JSON 副本"
    assert any(
        "单机兼容模式" in rec.getMessage() for rec in caplog.records
    ), "降级走 JSON 必须留 warning"


def test_save_registry_raises_when_fail_loud_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """AATS_P0_TUNING_FAIL_LOUD=on：DB 不可达必须立即抛，绝不能悄悄写 JSON。

    动机：tuning proposals 的真源是 governance DB；实盘开了 flag 后一切 DB
    不可达都要让运营者立刻看到，否则 JSON 副本会变成"从未入库的 ghost 提案"。
    """
    monkeypatch.setenv("AATS_P0_TUNING_FAIL_LOUD", "on")
    monkeypatch.setattr(mod, "try_governance_db", lambda: (None, False))

    registry: dict[str, Any] = {
        "proposals": [
            {
                "proposal_id": "tprop_fail_loud",
                "combo_key": "independent_15m",
                "parameter": "min_safe_net_edge_bps",
                "proposed_value": 1.5,
                "status": "pending_review",
            },
        ],
    }

    with pytest.raises(RuntimeError, match="AATS_P0_TUNING_FAIL_LOUD"):
        mod.save_strategy_tuning_registry(tmp_path, registry)

    assert not (tmp_path / "artifacts/governance/strategy_tuning_proposals.json").exists(), (
        "fail-loud 模式下 DB 不可达不得写 JSON 副本"
    )
