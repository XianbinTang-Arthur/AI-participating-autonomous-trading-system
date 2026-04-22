"""2026-04-22 · AI shadow block 异常隔离锚点测试。

## 背景

Round 3 设计过程中发现：AI shadow block 在 orchestrator.run_cycle 里
没有单独的异常保护。如果 `target_engine.build_shadow` 或 `publish_model
(AI_SHADOW_DECISIONS)` 抛异常，会冒到外层 try/except 然后触发
_publish_failure_best_effort —— **AI shadow 失败会 kill 整个 live decision
cycle**。

这是**遗留 bug**，不是 paper trading 引入的。同时 fix 掉符合 "shadow
永远不破坏 live" 的整体设计原则。

## 本测试锁定

1. AI shadow block 的源码里存在显式 try/except
2. catch 的是 Exception (不是 BaseException，保留 KeyboardInterrupt/SystemExit)
3. emit 名为 `ai_shadow_decision_build_or_publish_failed` 的 warning log

本测试用源码级 inspection，不启动真实 orchestrator —— 旨在防止 future
refactor 意外删掉这层保护。
"""
from __future__ import annotations

import inspect
import unittest

from aats.services.decision_engine.orchestrator import DecisionOrchestrator


class TestAIShadowBlockExceptionIsolation(unittest.TestCase):
    def test_source_has_try_except_around_shadow_block(self) -> None:
        """_run_cycle_body 里的 AI shadow block 必须被 try/except 包。"""
        src = inspect.getsource(DecisionOrchestrator._run_cycle_body)
        # 定位 AI shadow block 开头
        anchor = "if shadow_assessment is not None:"
        idx = src.find(anchor)
        self.assertNotEqual(idx, -1, "AI shadow block 定义不见了")

        # 取该块 + 之后 30 行（含 try/except 结构）
        block = src[idx : idx + 1500]

        self.assertIn("try:", block, "AI shadow block 必须 try: 保护")
        self.assertIn(
            "except Exception",
            block,
            "必须 catch Exception（不是 BaseException）以保留中断信号语义",
        )
        self.assertIn(
            "ai_shadow_decision_build_or_publish_failed",
            block,
            "必须 emit 清晰的告警事件名（Grafana alerting 依赖）",
        )

    def test_exception_caught_not_re_raised(self) -> None:
        """catch 块里应当 log_event 并不 re-raise。"""
        src = inspect.getsource(DecisionOrchestrator._run_cycle_body)
        anchor = "ai_shadow_decision_build_or_publish_failed"
        idx = src.find(anchor)
        self.assertNotEqual(idx, -1)
        tail = src[idx : idx + 400]
        # 不应该 raise
        # 一行内出现 'raise' + 紧邻 exc 就是 re-raise，本 pattern 我们不要
        self.assertNotIn(
            "raise exc",
            tail,
            "shadow 异常必须 swallow, 不能 re-raise",
        )
        self.assertNotIn(
            "raise\n",
            tail,
            "shadow 异常必须 swallow, 不能裸 raise",
        )


if __name__ == "__main__":
    unittest.main()
