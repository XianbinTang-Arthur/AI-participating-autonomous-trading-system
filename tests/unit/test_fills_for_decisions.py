"""2026-04-21 锚点测试：execution_repo.fills_for_decisions SQL-side 过滤。

## 背景

旧 `_decision_fills()` 调用 `execution_repo.fills()` 载入全表 + Python 侧
`[f for f in ... if f.decision_id in allowed]` 过滤。问题：

- `fills` / `fill_events` 表无界增长（每笔成交 +1 行）
- 每次 AI shadow evaluation 会扫全表
- 生产里今天 25 行没问题，但 1 万 / 10 万行时会变成明确瓶颈

修复：增加 `fills_for_decisions(decision_ids)` 走 SQL `WHERE decision_id
IN (...)`（Postgres 侧 FillEventModel.decision_id 有 index）。本测试：

1. InMemoryExecutionRepository 正确实现了新 protocol 方法，结果与旧路径
   `[f for f in repo.fills() if f.decision_id in allowed]` 一致
2. 空/None decision_ids 返回空列表
3. 重复 decision_ids 不会返回重复 fills
4. 排序稳定（按 fill_processing_sort_key）
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from aats.schemas.execution import FillEvent
from aats.storage.execution_repo import InMemoryExecutionRepository


def _make_fill(
    *,
    fill_id: str,
    client_order_id: str,
    decision_id: str,
    price: float = 100.0,
    qty: float = 1.0,
    ts_offset_s: int = 0,
) -> FillEvent:
    ts = datetime(2026, 4, 21, 12, 0, ts_offset_s, tzinfo=timezone.utc)
    return FillEvent(
        fill_id=fill_id,
        decision_id=decision_id,
        intent_id=f"intent_{fill_id}",
        client_order_id=client_order_id,
        exchange_order_id=f"ex_{fill_id}",
        symbol="BTC-USDT-SWAP",
        venue="OKX",
        side="buy",
        fill_qty=qty,
        fill_price=price,
        fee_amount=0.1,
        fee_currency="USDT",
        product_type="derivatives",
        target_leverage=1.0,
        margin_mode="cross",
        exposure_side="long",
        position_intent="open_long",
        liquidity_role="taker",
        exchange_timestamp=ts,
        ingestion_timestamp=ts,
        order_status_after_fill="FILLED",
    )


class TestFillsForDecisionsInMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryExecutionRepository()
        self.fills = [
            _make_fill(fill_id="f1", client_order_id="o1", decision_id="d1", ts_offset_s=1),
            _make_fill(fill_id="f2", client_order_id="o2", decision_id="d2", ts_offset_s=2),
            _make_fill(fill_id="f3", client_order_id="o3", decision_id="d1", ts_offset_s=3),
            _make_fill(fill_id="f4", client_order_id="o4", decision_id="d3", ts_offset_s=4),
        ]
        for fill in self.fills:
            self.repo.save_fill(fill)

    def test_returns_only_fills_matching_decision_ids(self) -> None:
        result = self.repo.fills_for_decisions(["d1"])
        self.assertEqual([f.fill_id for f in result], ["f1", "f3"])

    def test_multiple_decision_ids(self) -> None:
        result = self.repo.fills_for_decisions(["d1", "d3"])
        self.assertEqual([f.fill_id for f in result], ["f1", "f3", "f4"])

    def test_empty_list_returns_empty(self) -> None:
        self.assertEqual(self.repo.fills_for_decisions([]), [])

    def test_all_empty_strings_returns_empty(self) -> None:
        """过滤掉空字符串以免误伤（None/空字符串都应跳过）。"""
        self.assertEqual(self.repo.fills_for_decisions(["", ""]), [])

    def test_unknown_decision_id_returns_empty(self) -> None:
        self.assertEqual(self.repo.fills_for_decisions(["does_not_exist"]), [])

    def test_duplicate_decision_ids_dedup(self) -> None:
        # 重复 id 不应导致重复 fills
        result = self.repo.fills_for_decisions(["d1", "d1", "d1"])
        self.assertEqual([f.fill_id for f in result], ["f1", "f3"])

    def test_matches_old_fullscan_behavior(self) -> None:
        """anchor：新 fills_for_decisions 与旧 `[f for f in repo.fills() if ...]`
        应该返回相同结果（顺序和内容都应一致）。
        """
        decision_ids = ["d1", "d2"]
        old_path = sorted(
            [f for f in self.repo.fills() if f.decision_id in set(decision_ids)],
            key=lambda f: (f.exchange_timestamp, f.ingestion_timestamp, f.fill_id),
        )
        new_path = self.repo.fills_for_decisions(decision_ids)
        self.assertEqual(
            [f.fill_id for f in old_path],
            [f.fill_id for f in new_path],
            "新 fills_for_decisions 行为偏离旧 fullscan，可能引起 AI shadow "
            "evaluation 语义漂移 —— 需要核对。",
        )


class TestInferenceDecisionFillsFallback(unittest.TestCase):
    """inference._decision_fills 使用 getattr 做 fallback：没有
    fills_for_decisions 方法的 repo stub 应该 fallback 到旧路径。"""

    def test_repo_without_fills_for_decisions_falls_back_to_fills(self) -> None:
        from aats.services.ai_service.inference import AIInferenceService

        class _StubRepoWithoutBatchMethod:
            """历史老 stub，只有 fills()。"""

            def __init__(self, fills: list[FillEvent]) -> None:
                self._fills = fills

            def fills(self) -> list[FillEvent]:
                return list(self._fills)

        fills = [
            _make_fill(fill_id="f1", client_order_id="o1", decision_id="d1", ts_offset_s=1),
            _make_fill(fill_id="f2", client_order_id="o2", decision_id="d2", ts_offset_s=2),
        ]
        stub = _StubRepoWithoutBatchMethod(fills)

        service = AIInferenceService.__new__(AIInferenceService)
        service.execution_repo = stub

        result = service._decision_fills(["d1"])
        self.assertEqual(len(result["d1"]), 1)
        self.assertEqual(result["d1"][0].fill_id, "f1")

    def test_repo_with_fills_for_decisions_uses_batch_method(self) -> None:
        """有新方法的 repo 应该走新路径（不调用 .fills()）。"""
        from aats.services.ai_service.inference import AIInferenceService

        fills_call_count = [0]
        batch_call_count = [0]

        class _RepoWithBatchMethod:
            def __init__(self, fills: list[FillEvent]) -> None:
                self._fills = fills

            def fills(self) -> list[FillEvent]:
                fills_call_count[0] += 1
                return list(self._fills)

            def fills_for_decisions(self, decision_ids: list[str]) -> list[FillEvent]:
                batch_call_count[0] += 1
                allowed = set(decision_ids)
                return [f for f in self._fills if f.decision_id in allowed]

        fills = [
            _make_fill(fill_id="f1", client_order_id="o1", decision_id="d1", ts_offset_s=1),
        ]
        repo = _RepoWithBatchMethod(fills)

        service = AIInferenceService.__new__(AIInferenceService)
        service.execution_repo = repo

        service._decision_fills(["d1"])

        self.assertEqual(
            fills_call_count[0],
            0,
            "不应该在 repo 有 fills_for_decisions 时还调全扫 fills() —— "
            "fix 没生效或 fallback 逻辑反了",
        )
        self.assertEqual(batch_call_count[0], 1)


if __name__ == "__main__":
    unittest.main()
