"""L1 回归：_precheck_recommendation_transitionable 返回 (registry, rec) 元组。

过去 approve / reject / supersede 每个 handler 先调一遍 precheck（内部 load），再
在 handler 体里重新 load_recommendation_registry 一次，等于每次请求跑两遍
DB-first + JSON fallback。L1 让 precheck 直接把 registry 与命中的 rec 返回给
caller，消除二次读。

此文件锁定 precheck 的三类外部可观察行为：
  1. 命中 → 返回 ``(registry, rec)`` 二元组；registry 包含命中的 rec
  2. recommendation 不存在 → HTTPException(404)
  3. status 不在 expected_current → HTTPException(409)，detail 带 current / expected

precheck 自己读 JSON 即可；DB-first 分支由 load_recommendation_registry 的
单测覆盖，这里通过 skip_db=True 的默认行为 + 不存在 DB engine 的 tmp_path
让 fallback 自动走 JSON。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest


def _write_registry(root: pathlib.Path, recs: list[dict[str, Any]]) -> None:
    path = root / "artifacts/decision_system/recommendation_registry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "recommendations": recs}),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _disable_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 load_recommendation_registry 走 JSON 副本路径。

    registry 的 DB-first 路径有自己的测试；这里只关心 precheck 的返回值契约，
    所以把 DB 直接打掉。
    """
    from aats.data_platform.governance import _db_util

    monkeypatch.setattr(_db_util, "try_governance_db", lambda: (None, False))


def test_precheck_returns_registry_and_rec_tuple(tmp_path: pathlib.Path) -> None:
    """命中时返回 (registry, rec)；registry 至少包含命中的 rec。"""
    from aats.api.rdp_routes import _precheck_recommendation_transitionable

    _write_registry(tmp_path, [
        {"recommendation_id": "rec_a", "status": "draft"},
        {"recommendation_id": "rec_b", "status": "approved"},
    ])

    registry, rec = _precheck_recommendation_transitionable(
        tmp_path, "rec_a", expected_current=("draft",),
    )

    assert isinstance(registry, dict)
    assert rec["recommendation_id"] == "rec_a"
    assert rec["status"] == "draft"
    # handler 用这个 registry 应该能复原完整列表（避免二次 load）
    ids_in_registry = {item["recommendation_id"] for item in registry.get("recommendations", [])}
    assert ids_in_registry == {"rec_a", "rec_b"}


def test_precheck_missing_rec_raises_404(tmp_path: pathlib.Path) -> None:
    """recommendation_id 不在 registry → HTTPException(404)，detail 带 recommendation_id。"""
    from fastapi import HTTPException

    from aats.api.rdp_routes import _precheck_recommendation_transitionable

    _write_registry(tmp_path, [{"recommendation_id": "rec_only", "status": "draft"}])

    with pytest.raises(HTTPException) as exc_info:
        _precheck_recommendation_transitionable(
            tmp_path, "rec_missing", expected_current=("draft",),
        )
    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["recommendation_id"] == "rec_missing"


def test_precheck_bad_status_raises_409_with_expected_list(
    tmp_path: pathlib.Path,
) -> None:
    """rec 存在但 status 不在 expected → HTTPException(409) + detail.current_status/expected_status。"""
    from fastapi import HTTPException

    from aats.api.rdp_routes import _precheck_recommendation_transitionable

    _write_registry(tmp_path, [
        {"recommendation_id": "rec_approved", "status": "approved"},
    ])

    with pytest.raises(HTTPException) as exc_info:
        _precheck_recommendation_transitionable(
            tmp_path, "rec_approved", expected_current=("draft",),
        )
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["current_status"] == "approved"
    assert detail["expected_status"] == ["draft"]
