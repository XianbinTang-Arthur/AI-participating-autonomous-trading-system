"""M3 回归：save_recommendation_registry JSON-only 路径的 version CAS。

单机兼容（DB 不可达）下 load → mutate → save 两条并发脚本原本会 silent clobber：
后写方把前写方的变更覆盖且无任何提示。M3 在落盘前 read 磁盘 version 与内存
base_version 对比，不一致时抛 RuntimeError，显式迫使 caller 重跑序列。

注意：这不是严格 CAS（read/write 之间仍有 TOCTOU 窗口），但能捕获最常见的
human-sequential 竞态（operator 手动跑两遍脚本）。
"""

from __future__ import annotations

import json
import pathlib
import threading
from typing import Any

import pytest

from aats.data_platform.decision_system.recommendation_registry import (
    save_recommendation_registry,
)


def _registry(version: int, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"version": version, "recommendations": list(items or [])}


def test_save_registry_from_empty_path_ok(tmp_path: pathlib.Path) -> None:
    """文件不存在 → 直接落盘，version 从 base 0 bump 到 1。"""
    path = tmp_path / "recommendation_registry.json"
    registry = _registry(0, [{"recommendation_id": "rec_a", "status": "draft"}])

    save_recommendation_registry(registry, path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert on_disk["recommendations"][0]["recommendation_id"] == "rec_a"


def test_save_registry_version_matches_disk_bumps_normally(
    tmp_path: pathlib.Path,
) -> None:
    """磁盘 version == 内存 base_version → CAS 通过，正常 bump 到 base+1。"""
    path = tmp_path / "recommendation_registry.json"
    # 磁盘先存一份 v2
    path.write_text(
        json.dumps({"version": 2, "recommendations": []}),
        encoding="utf-8",
    )

    # caller 拿到 v2 并 mutate → 再保存
    registry = _registry(2, [{"recommendation_id": "rec_b", "status": "draft"}])
    save_recommendation_registry(registry, path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 3
    assert len(on_disk["recommendations"]) == 1


def test_save_registry_version_mismatch_raises_cas_conflict(
    tmp_path: pathlib.Path,
) -> None:
    """磁盘 version 已经被抢先 bump（v2 → v3），内存 base_version 还是 v2 → 抛。

    这是 silent clobber 的真实场景：operator 脚本 A 读 v2，脚本 B 也读 v2 并
    先写入 v3；A 再试图写就应被明确拦下，而不是把 B 的新增改回 v2+1 覆盖掉。
    """
    path = tmp_path / "recommendation_registry.json"
    # 磁盘被另一个 writer 抢先写到 v3（模拟并发写入）
    path.write_text(
        json.dumps({
            "version": 3,
            "recommendations": [{"recommendation_id": "rec_other", "status": "draft"}],
        }),
        encoding="utf-8",
    )

    # 本方拿着过期的 base_version=2 尝试落盘
    stale_registry = _registry(2, [{"recommendation_id": "rec_mine", "status": "draft"}])
    with pytest.raises(RuntimeError, match="CAS 冲突"):
        save_recommendation_registry(stale_registry, path)

    # 磁盘保持被他人写入的状态，没有被 clobber
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 3
    assert on_disk["recommendations"][0]["recommendation_id"] == "rec_other"


def test_save_registry_corrupt_disk_skips_cas_and_proceeds(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """磁盘 JSON 损坏 → 跳过 CAS 并打 warning；不抛，按 base_version 继续落盘。

    动机：如果磁盘文件被外部进程损坏，严格阻断反而让系统卡死；留一条 warning
    记录这次无 CAS 保护的写入，让 operator 可以事后审计。
    """
    import logging

    path = tmp_path / "recommendation_registry.json"
    path.write_text("{not valid json", encoding="utf-8")

    registry = _registry(5, [{"recommendation_id": "rec_c", "status": "draft"}])
    with caplog.at_level(logging.WARNING):
        save_recommendation_registry(registry, path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 6  # base=5 → bump 到 6
    assert any(
        "CAS 读磁盘 version 失败" in rec.getMessage() for rec in caplog.records
    ), "损坏的磁盘副本必须有 warning 可见，便于事后审计"


def test_save_registry_older_disk_version_also_raises(
    tmp_path: pathlib.Path,
) -> None:
    """磁盘 version 比内存 base 还小 → 同样算冲突。

    极端但真实：磁盘被回滚 / 某脚本持久化错了版本号，应阻断而不是顺走一条 bump。
    """
    path = tmp_path / "recommendation_registry.json"
    path.write_text(
        json.dumps({"version": 1, "recommendations": []}),
        encoding="utf-8",
    )

    registry = _registry(4, [{"recommendation_id": "rec_d", "status": "draft"}])
    with pytest.raises(RuntimeError, match="CAS 冲突"):
        save_recommendation_registry(registry, path)


def test_db_load_stamps_disk_version_for_cas(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：DB-loaded registry 必须把磁盘 version 戳进内存,否则下次 save 必 500。

    真实事故回放:第一次 approve 成功(磁盘 v0→v1),第二次 approve DB 加载回
    registry(无 version 字段)、写 DB 成功、再 save 文件时 base_version=0 vs
    磁盘=1 → CAS 冲突 → 500 冒泡到 UI,但 DB 状态其实已经变了——split-brain。
    修复后 load 应该把磁盘 version 戳到 registry 上,让 save CAS 通过。
    """
    from aats.data_platform.decision_system import recommendation_registry as mod

    path = tmp_path / "recommendation_registry.json"
    # 审计副本已经被前一轮 save 推到 v3(模拟已运行过几轮 approve)
    path.write_text(
        json.dumps({"version": 3, "recommendations": [{"recommendation_id": "rec_old"}]}),
        encoding="utf-8",
    )

    # Mock DB 返回一份没有 version 字段的 payload（DB 不跟踪文件 version）
    def fake_try_governance_db() -> tuple[object, bool]:
        class _FakeEngine:
            def dispose(self) -> None: ...
        return _FakeEngine(), True

    def fake_db_load(session: object) -> dict[str, Any]:
        return {
            "generated_at": "2026-04-18T00:00:00+00:00",
            "recommendations": [{"recommendation_id": "rec_from_db", "status": "draft"}],
        }

    class _FakeSession:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_FakeSession": return self
        def __exit__(self, *_: object) -> None: ...

    monkeypatch.setattr(mod, "try_governance_db", fake_try_governance_db)
    import aats.data_platform.governance.recommendations_db as recs_db_mod
    monkeypatch.setattr(recs_db_mod, "db_load_recommendation_registry", fake_db_load)
    # load_recommendation_registry 里 import sqlalchemy.orm.Session,打补丁避免真连库
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _FakeSession)

    registry = mod.load_recommendation_registry(path)
    assert registry["version"] == 3, "DB 加载必须把磁盘 version 戳进来"

    # save 必须成功——disk=3, memory base=3 → bump 到 4
    mod.save_recommendation_registry(registry, path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 4
    assert on_disk["recommendations"][0]["recommendation_id"] == "rec_from_db"


def test_db_load_no_file_stamps_version_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 加载但磁盘文件不存在（首次运行）→ version 戳 0,save 后磁盘 v1。"""
    from aats.data_platform.decision_system import recommendation_registry as mod

    path = tmp_path / "recommendation_registry.json"
    assert not path.exists()

    def fake_try_governance_db() -> tuple[object, bool]:
        class _FakeEngine:
            def dispose(self) -> None: ...
        return _FakeEngine(), True

    def fake_db_load(session: object) -> dict[str, Any]:
        return {
            "generated_at": "2026-04-18T00:00:00+00:00",
            "recommendations": [{"recommendation_id": "rec_x", "status": "draft"}],
        }

    class _FakeSession:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_FakeSession": return self
        def __exit__(self, *_: object) -> None: ...

    monkeypatch.setattr(mod, "try_governance_db", fake_try_governance_db)
    import aats.data_platform.governance.recommendations_db as recs_db_mod
    monkeypatch.setattr(recs_db_mod, "db_load_recommendation_registry", fake_db_load)
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _FakeSession)

    registry = mod.load_recommendation_registry(path)
    assert registry["version"] == 0

    mod.save_recommendation_registry(registry, path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1


def test_db_empty_is_authoritative_and_does_not_resurrect_stale_json(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.decision_system import recommendation_registry as mod

    path = tmp_path / "artifacts/decision_system/recommendation_registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 7,
                "recommendations": [
                    {
                        "recommendation_id": "rec_deleted_from_db",
                        "status": "approved",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class _FakeEngine:
        def dispose(self) -> None: ...

    class _FakeSession:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_FakeSession": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(mod, "try_governance_db", lambda: (_FakeEngine(), True))
    import aats.data_platform.governance.recommendations_db as recs_db_mod
    monkeypatch.setattr(
        recs_db_mod,
        "db_load_recommendation_registry",
        lambda _session: {"generated_at": "now", "recommendations": []},
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _FakeSession)

    registry = mod.load_recommendation_registry(path)

    assert registry["recommendations"] == []
    assert registry["version"] == 7


def test_managed_db_error_denies_stale_recommendation_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.decision_system import recommendation_registry as mod
    from aats.data_platform.governance._exceptions import DBUnavailableError

    path = tmp_path / "artifacts/decision_system/recommendation_registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"recommendations": [{"recommendation_id": "rec_stale"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "AATS_ACTIVE_PARAMETER_DB_URL",
        "postgresql+psycopg://managed.invalid/aats_research",
    )
    monkeypatch.setattr(mod, "try_governance_db", lambda: (None, False))

    with pytest.raises(DBUnavailableError, match="stale recommendation JSON fallback denied"):
        mod.load_recommendation_registry(path)


def test_active_decision_db_empty_is_authoritative(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.decision_system import recommendation_registry as mod

    path = tmp_path / "artifacts/decision_system/active_decision_registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"decisions": [{"combo_key": "stale_combo", "current_status": "active"}]}),
        encoding="utf-8",
    )

    class _Engine:
        def dispose(self) -> None: ...

    class _Session:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_Session": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(mod, "has_explicit_governance_db_configuration", lambda _root: True)
    monkeypatch.setattr(mod, "try_governance_db", lambda: (_Engine(), True))
    import aats.data_platform.governance.recommendations_db as recs_db_mod
    monkeypatch.setattr(
        recs_db_mod,
        "db_load_active_decisions",
        lambda _session: {"generated_at": "now", "decisions": []},
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)

    registry = mod.load_active_decision_registry(path)

    assert registry["decisions"] == []


def test_managed_active_decision_error_denies_stale_json(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aats.data_platform.decision_system import recommendation_registry as mod
    from aats.data_platform.governance._exceptions import DBUnavailableError

    path = tmp_path / "artifacts/decision_system/active_decision_registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"decisions": [{"combo_key": "stale_combo", "current_status": "active"}]}),
        encoding="utf-8",
    )

    class _Engine:
        def dispose(self) -> None: ...

    class _Session:
        def __init__(self, _engine: object) -> None: ...
        def __enter__(self) -> "_Session": return self
        def __exit__(self, *_args: object) -> None: ...

    monkeypatch.setattr(mod, "has_explicit_governance_db_configuration", lambda _root: True)
    monkeypatch.setattr(mod, "try_governance_db", lambda: (_Engine(), True))
    import aats.data_platform.governance.recommendations_db as recs_db_mod
    monkeypatch.setattr(
        recs_db_mod,
        "db_load_active_decisions",
        lambda _session: (_ for _ in ()).throw(RuntimeError("synthetic decision read failure")),
    )
    import sqlalchemy.orm
    monkeypatch.setattr(sqlalchemy.orm, "Session", _Session)

    with pytest.raises(DBUnavailableError, match="stale JSON fallback denied"):
        mod.load_active_decision_registry(path)


def test_offline_registry_write_failure_remains_strict(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯文件模式没有 DB 真源，写失败必须继续向上抛出。"""
    from aats.data_platform.decision_system import recommendation_registry as mod
    from aats.data_platform.governance import _atomic_io

    path = tmp_path / "recommendation_registry.json"
    registry = _registry(0, [{"recommendation_id": "rec_offline"}])
    monkeypatch.setattr(
        _atomic_io,
        "atomic_json_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("synthetic offline write failure")
        ),
    )

    with pytest.raises(OSError, match="synthetic offline write failure"):
        mod.save_recommendation_registry(registry, path)


def test_db_committed_mirror_refresh_marks_corrupt_json_degraded(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 已提交时不覆盖损坏镜像：返回 False 并保留可审计错误。"""
    import logging

    from aats.data_platform.decision_system import recommendation_registry as mod

    path = tmp_path / "recommendation_registry.json"
    path.write_text("{corrupt-json", encoding="utf-8")
    registry = _registry(0, [{"recommendation_id": "rec_db_committed"}])
    monkeypatch.setattr(
        mod,
        "_load_canonical_recommendation_registry_for_audit_mirror",
        lambda _path: registry,
    )

    with caplog.at_level(logging.ERROR):
        refreshed = mod.refresh_recommendation_audit_mirror_after_db_commit(
            path,
            recommendation_id="rec_db_committed",
            transition="approve",
        )

    assert refreshed is False
    assert path.read_text(encoding="utf-8") == "{corrupt-json"
    assert any(
        "audit mirror degraded after canonical DB commit" in record.getMessage()
        for record in caplog.records
    )


def test_db_readback_and_file_cas_preserve_distinct_concurrent_transitions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同 recommendation 并发提交时，旧快照不得覆盖已提交真值。

    A 先读到“A/B 均已批准”的 canonical DB 快照和磁盘 base v1，
    但停在写入前；B 用较早的“仅 B 已批准”快照先写成 v2。A 的
    首次写入命中 CAS 后必须重读 DB + 当前 v2，再写成 v3；最终镜像
    必须保留 A/B 两条已提交转移，不得留在“A 仍 draft”的假状态。
    """
    from aats.data_platform.decision_system import recommendation_registry as mod

    path = tmp_path / "recommendation_registry.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "recommendations": [
                    {"recommendation_id": "rec_a", "status": "draft"},
                    {"recommendation_id": "rec_b", "status": "draft"},
                ],
            }
        ),
        encoding="utf-8",
    )
    stale_b_readback = {
        "version": 1,
        "recommendations": [
            {"recommendation_id": "rec_a", "status": "draft"},
            {"recommendation_id": "rec_b", "status": "approved"},
        ],
    }
    readback_counts: dict[str, int] = {}

    def _readback(_path: pathlib.Path) -> dict[str, Any]:
        thread_name = threading.current_thread().name
        readback_counts[thread_name] = readback_counts.get(thread_name, 0) + 1
        if thread_name == "mirror-b":
            return stale_b_readback
        return {
            # A 的首次 readback 在 B 写前绑定 v1；CAS 失败后第二次
            # readback 必须看到 B 已推进的 v2。
            "version": 1 if readback_counts[thread_name] == 1 else 2,
            "recommendations": [
                {"recommendation_id": "rec_a", "status": "approved"},
                {"recommendation_id": "rec_b", "status": "approved"},
            ],
        }

    real_save = mod.save_recommendation_registry
    a_waiting_to_write = threading.Event()
    b_write_finished = threading.Event()

    def _coordinated_save(
        registry: dict[str, Any],
        target: pathlib.Path,
        **kwargs: Any,
    ) -> None:
        if (
            threading.current_thread().name == "mirror-a"
            and int(registry["version"]) == 1
        ):
            a_waiting_to_write.set()
            if not b_write_finished.wait(timeout=5):
                raise TimeoutError("B mirror writer did not finish")
            # 磁盘已被 B 推到 v2，这次 v1 写入必须 CAS 失败，helper
            # 应回到 DB readback 开始下一次尝试。
            real_save(registry, target, **kwargs)
            return
        if threading.current_thread().name == "mirror-b":
            try:
                real_save(registry, target, **kwargs)
            finally:
                b_write_finished.set()
            return
        # A 的第二次尝试：canonical 快照 + 当前 base v2。
        real_save(registry, target, **kwargs)

    monkeypatch.setattr(
        mod,
        "_load_canonical_recommendation_registry_for_audit_mirror",
        _readback,
    )
    monkeypatch.setattr(mod, "save_recommendation_registry", _coordinated_save)

    results: dict[str, bool] = {}

    def _refresh(key: str, recommendation_id: str) -> None:
        results[key] = mod.refresh_recommendation_audit_mirror_after_db_commit(
            path,
            recommendation_id=recommendation_id,
            transition="approve",
        )

    b_thread = threading.Thread(
        target=_refresh,
        args=("b", "rec_b"),
        name="mirror-b",
    )
    a_thread = threading.Thread(
        target=_refresh,
        args=("a", "rec_a"),
        name="mirror-a",
    )
    a_thread.start()
    assert a_waiting_to_write.wait(timeout=5)
    b_thread.start()
    a_thread.join(timeout=5)
    b_thread.join(timeout=5)

    assert not a_thread.is_alive()
    assert not b_thread.is_alive()
    assert results == {"a": True, "b": True}
    assert readback_counts == {"mirror-a": 2, "mirror-b": 1}
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 3
    assert {
        item["recommendation_id"]: item["status"]
        for item in on_disk["recommendations"]
    } == {"rec_a": "approved", "rec_b": "approved"}
