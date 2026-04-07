"""Stage 5 单元测试：StrategyExecutionBundle 乐观并发控制。

只覆盖 In-Memory 实现，因为 Postgres 实现需要真实数据库。Postgres 路径
的等价行为通过共享 base.py 协议和相同的 OptimisticLockError 来保证语义
一致；Stage 5 的集成测试会在 docker-compose 起来后跑端到端验证。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aats.schemas.strategy_runtime import StrategyExecutionBundle
from aats.storage.base import OptimisticLockError
from aats.storage.strategy_runtime_repo import InMemoryStrategyRuntimeRepository


def _make_bundle(bundle_id: str = "bnd-1", *, status: str = "planned") -> StrategyExecutionBundle:
    return StrategyExecutionBundle.model_validate(
        {
            "bundle_id": bundle_id,
            "decision_id": "dec-1",
            "family": "directional",
            "strategy_sleeve_id": "slv-1",
            "allocation_id": "alloc-1",
            "product_type": "spot",
            "margin_mode": "cash",
            "route_action": "override_target",
            "bundle_type": "single_sleeve",
            "bundle_priority": "standard",
            "status": status,
            "selected_symbol": "BTC-USDT",
            "gross_requested_exposure": Decimal("100"),
            "net_approved_exposure": Decimal("100"),
            "expected_cost_bps": Decimal("3"),
            "expected_edge_bps": Decimal("8"),
            "portfolio_risk_budget_state": None,
            "created_at": datetime.now(timezone.utc),
            "reason_codes": [],
        }
    )


# ─────────────────────────────────────────────────────────────────────
# 首次插入 (expected_row_version=None)
# ─────────────────────────────────────────────────────────────────────


def test_first_insert_sets_row_version_to_one() -> None:
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    result = repo.save_execution_bundle_versioned(bundle, expected_row_version=None)
    assert result.row_version == 1
    assert result.created is True
    assert result.bundle.bundle_id == bundle.bundle_id


def test_first_insert_when_row_already_exists_raises() -> None:
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    repo.save_execution_bundle_versioned(bundle, expected_row_version=None)
    with pytest.raises(OptimisticLockError) as ctx:
        repo.save_execution_bundle_versioned(bundle, expected_row_version=None)
    assert ctx.value.bundle_id == bundle.bundle_id
    # expected=None 表示 caller 期望"首次插入"，而不是"版本号 0"
    assert ctx.value.expected_row_version is None
    assert ctx.value.actual_row_version == 1


# ─────────────────────────────────────────────────────────────────────
# CAS 更新 (expected_row_version=N)
# ─────────────────────────────────────────────────────────────────────


def test_cas_update_with_correct_version_succeeds_and_bumps() -> None:
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    insert_result = repo.save_execution_bundle_versioned(bundle, expected_row_version=None)
    assert insert_result.row_version == 1

    # 模拟"读 → 修改 → 写"循环
    bundle_v2 = bundle.model_copy(update={"status": "submitted"})
    update_result = repo.save_execution_bundle_versioned(
        bundle_v2,
        expected_row_version=insert_result.row_version,
    )
    assert update_result.row_version == 2
    assert update_result.created is False

    fetched = repo.get_execution_bundle_with_version(bundle.bundle_id)
    assert fetched is not None
    fetched_bundle, fetched_version = fetched
    assert fetched_version == 2
    assert fetched_bundle.status == "submitted"


def test_cas_update_with_stale_version_raises_and_does_not_overwrite() -> None:
    """两个写者并发修改同一 bundle，后到的应当被 CAS 拒绝。"""
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    repo.save_execution_bundle_versioned(bundle, expected_row_version=None)

    # writer A 把状态改成 submitted（成功，row_version 1 → 2）
    writer_a_view = repo.get_execution_bundle_with_version(bundle.bundle_id)
    assert writer_a_view is not None
    a_bundle, a_version = writer_a_view
    repo.save_execution_bundle_versioned(
        a_bundle.model_copy(update={"status": "submitted"}),
        expected_row_version=a_version,
    )

    # writer B 拿的是更早的版本 1，尝试改成 cancelled
    with pytest.raises(OptimisticLockError) as ctx:
        repo.save_execution_bundle_versioned(
            bundle.model_copy(update={"status": "cancelled"}),
            expected_row_version=1,
        )
    assert ctx.value.expected_row_version == 1
    assert ctx.value.actual_row_version == 2

    # 状态没有被覆盖
    fetched = repo.get_execution_bundle(bundle.bundle_id)
    assert fetched is not None
    assert fetched.status == "submitted"


def test_cas_update_on_missing_row_raises() -> None:
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    with pytest.raises(OptimisticLockError) as ctx:
        repo.save_execution_bundle_versioned(bundle, expected_row_version=1)
    assert ctx.value.actual_row_version is None


# ─────────────────────────────────────────────────────────────────────
# Merge-and-retry 习语
# ─────────────────────────────────────────────────────────────────────


def test_merge_and_retry_pattern_recovers_from_conflict() -> None:
    """文档中推荐的 merge-and-retry 习语在内存实现中能正确收敛。"""
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    repo.save_execution_bundle_versioned(bundle, expected_row_version=None)

    # writer A 提交了 status=submitted
    a_view = repo.get_execution_bundle_with_version(bundle.bundle_id)
    assert a_view is not None
    repo.save_execution_bundle_versioned(
        a_view[0].model_copy(update={"status": "submitted"}),
        expected_row_version=a_view[1],
    )

    # writer B 想把 priority 改成 urgent，第一次基于 v1 失败
    desired_change = {"bundle_priority": "urgent"}
    last_seen_version = 1
    for _ in range(5):
        try:
            repo.save_execution_bundle_versioned(
                bundle.model_copy(update=desired_change),
                expected_row_version=last_seen_version,
            )
            break
        except OptimisticLockError as exc:
            # merge-and-retry：拿最新版本，把自己的修改 merge 进去
            current = repo.get_execution_bundle_with_version(bundle.bundle_id)
            assert current is not None
            current_bundle, current_version = current
            last_seen_version = current_version
            bundle = current_bundle
            assert exc.actual_row_version == current_version

    final = repo.get_execution_bundle_with_version(bundle.bundle_id)
    assert final is not None
    final_bundle, final_version = final
    # 既保留了 writer A 的 status=submitted，也接受了 writer B 的 priority=urgent
    assert final_bundle.status == "submitted"
    assert final_bundle.bundle_priority == "urgent"
    assert final_version == 3  # 1 (insert) + 1 (writer A) + 1 (writer B retry)


# ─────────────────────────────────────────────────────────────────────
# 兼容性：旧 save_execution_bundle 仍能工作
# ─────────────────────────────────────────────────────────────────────


def test_legacy_save_still_works_and_bumps_version() -> None:
    """旧调用方继续用 save_execution_bundle 不会报错，且 row_version 可见。"""
    repo = InMemoryStrategyRuntimeRepository()
    bundle = _make_bundle()
    repo.save_execution_bundle(bundle)
    repo.save_execution_bundle(bundle.model_copy(update={"status": "submitted"}))

    fetched = repo.get_execution_bundle_with_version(bundle.bundle_id)
    assert fetched is not None
    fetched_bundle, fetched_version = fetched
    assert fetched_bundle.status == "submitted"
    assert fetched_version == 2  # 两次 legacy save → row_version 升到 2
