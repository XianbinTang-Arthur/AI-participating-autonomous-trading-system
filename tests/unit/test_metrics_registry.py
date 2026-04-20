"""MetricsRegistry 单元测试。

P0-b Task 2.4 新增 labeled counter 支持，补齐基础 registry 的单测。
"""
from __future__ import annotations

import pytest

from aats.bootstrap.metrics import MetricsRegistry


def test_increment_accumulates() -> None:
    registry = MetricsRegistry()
    registry.increment("cycles")
    registry.increment("cycles", 4)
    assert registry.snapshot()["cycles"] == 5


def test_snapshot_is_independent_copy() -> None:
    registry = MetricsRegistry()
    registry.increment("a", 1)
    snap = registry.snapshot()
    registry.increment("a", 9)
    # 快照拿到的 dict 与内部 state 不共享
    assert snap["a"] == 1
    assert registry.snapshot()["a"] == 10


def test_increment_labeled_groups_by_sorted_labels() -> None:
    registry = MetricsRegistry()
    registry.increment_labeled("runtime_ai_operating_mode", labels={"mode": "baseline_only"})
    registry.increment_labeled("runtime_ai_operating_mode", labels={"mode": "baseline_only"})
    registry.increment_labeled("runtime_ai_operating_mode", labels={"mode": "ai_assisted"})

    snap = registry.labeled_snapshot()
    assert snap[("runtime_ai_operating_mode", (("mode", "baseline_only"),))] == 2
    assert snap[("runtime_ai_operating_mode", (("mode", "ai_assisted"),))] == 1


def test_increment_labeled_stable_key_regardless_of_label_insertion_order() -> None:
    registry = MetricsRegistry()
    # 不同插入顺序的 label 组应命中同一 key
    registry.increment_labeled(
        "runtime_ai_operating_mode",
        labels={"mode": "ai_decision_maker", "process_role": "decision"},
    )
    registry.increment_labeled(
        "runtime_ai_operating_mode",
        labels={"process_role": "decision", "mode": "ai_decision_maker"},
    )
    snap = registry.labeled_snapshot()
    key = (
        "runtime_ai_operating_mode",
        (("mode", "ai_decision_maker"), ("process_role", "decision")),
    )
    assert snap[key] == 2


def test_increment_labeled_non_mapping_labels_warns_and_skips() -> None:
    """2026-04-20 code review A-L3: 非 Mapping 时不再 raise TypeError.

    旧语义 raise TypeError 但 caller 都 `except Exception: pass` 吞掉, 开发
    期错误永不可见. 新语义 warn log + skip, Loki 可追溯 "metric 没 emit".
    """
    import logging
    registry = MetricsRegistry()

    # 捕 aats.metrics logger 输出
    caplog_handler = logging.getLogger("aats.metrics")
    import io
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    caplog_handler.addHandler(handler)

    try:
        # 不 raise
        registry.increment_labeled("x", labels=[("mode", "baseline_only")])  # type: ignore[arg-type]
    finally:
        caplog_handler.removeHandler(handler)

    # labeled_snapshot 应为空 (skip 路径)
    assert registry.labeled_snapshot() == {}
    # Log 有 warning
    output = stream.getvalue()
    assert "Mapping" in output
    assert "'x'" in output or "\"x\"" in output


def test_labeled_and_unlabeled_storage_are_independent() -> None:
    """``snapshot()`` 不能把 labeled 计数器泄进来污染历史消费方。"""
    registry = MetricsRegistry()
    registry.increment("decision_cycles", 3)
    registry.increment_labeled("runtime_ai_operating_mode", labels={"mode": "baseline_only"})

    unlabeled = registry.snapshot()
    assert "runtime_ai_operating_mode" not in unlabeled
    assert unlabeled["decision_cycles"] == 3
