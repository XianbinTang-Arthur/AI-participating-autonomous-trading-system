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
