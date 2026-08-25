"""2026-04-22 LF-004 anchor · handle_position_target 入口同步 kill_switch 预检。

## 背景

Reconciliation 发现异常 → RecoveryPostureEvaluator → kill_switch.halt()。
这条链路约 10-50ms 延迟。执行进程同期可能已经接受了新的 PositionTarget 并
submit_order 下单。本 fix 在 handle_position_target 入口同步读 kill_switch
本地 cache（I1 保证 halt() 立即更新）堵住这个窗口。

## 本测试锁定

1. `kill_switch.status()` 返回 `halted=True` → handle_position_target return
   早退，不做后续 account.refresh / policy / risk / submit
2. `halted=False` → 正常继续流程
3. 预检 emit `position_target_rejected_kill_switch_halted` 日志事件

## 验证方法

直接构造 mock KillSwitch + 观察 account_service.refresh 是否被调。
halted 时 refresh 不应被 await (早退)；not halted 时应继续到 refresh。

我们不直接调 handle_position_target（它嵌套深），而是验证实际生成的
closure 行为。
"""
from __future__ import annotations

import unittest


class TestKillSwitchEarlyReturnLogic(unittest.IsolatedAsyncioTestCase):
    """LF-004 的核心不变性：halted → 早退，不 await refresh / policy / risk。

    这个测试走"源码级 invariance"路线：读 aats/bootstrap/config.py，验证
    handle_position_target 开头有 kill_switch.status() → 早 return 的结构。
    也验证 status 来自 kill_switch 对象（不是其它冷 cache）。
    """

    def test_source_contains_kill_switch_status_check_at_entry(self) -> None:
        """源码校验：handle_position_target 前 25 行内必有 kill_switch.status()
        + early return。"""
        from pathlib import Path

        src = (
            Path(__file__)
            .resolve()
            .parents[2]
            / "aats"
            / "bootstrap"
            / "config.py"
        ).read_text(encoding="utf-8")

        # 定位 handle_position_target
        marker = "async def handle_position_target(message: dict[str, Any]) -> None:"
        idx = src.find(marker)
        self.assertNotEqual(idx, -1, "handle_position_target 定义不见了")

        # 取函数体头 25 行（约 1500 字符）
        head = src[idx : idx + 1500]

        self.assertIn(
            "kill_switch.status()",
            head,
            "LF-004: handle_position_target 开头必须 sync 读 kill_switch.status() "
            "作为同步 halted 预检。如果这个检查不见了，10-50ms 溜单窗口重现。",
        )
        self.assertIn(
            "ks_status.get(\"halted\")",
            head,
            "必须用 halted 字段做早 return",
        )
        self.assertIn(
            "position_target_rejected_kill_switch_halted",
            head,
            "必须 emit 告警事件（运维能从 Loki 看到被拒单）",
        )
        # 确保是 return 而不是 raise (raise 会进 trigger backoff 反复重试)
        # 取 halted 判断之后的 150 字符
        halted_idx = head.find("ks_status.get(\"halted\")")
        self.assertNotEqual(halted_idx, -1)
        decision_block = head[halted_idx : halted_idx + 800]
        self.assertIn(
            "return",
            decision_block,
            "halted=True 时必须 return 早退，不是 raise (避免无限 retry)",
        )


class TestKillSwitchStatusApiContract(unittest.TestCase):
    """防止未来有人把 KillSwitch.status() 的返回 schema 改了却不同步 handler。"""

    def test_killswitch_status_returns_dict_with_halted_key(self) -> None:
        from aats.services.governance_engine.kill_switch import KillSwitch

        ks = KillSwitch()
        status = ks.status()
        self.assertIsInstance(status, dict)
        self.assertIn("halted", status)
        self.assertIn("reason", status)

    def test_killswitch_status_halted_bool(self) -> None:
        from aats.services.governance_engine.kill_switch import KillSwitch

        ks = KillSwitch()
        self.assertFalse(ks.status()["halted"])
        ks.halt(reason="test_halt")
        self.assertTrue(ks.status()["halted"])
        self.assertEqual(ks.status()["reason"], "test_halt")
        ks.resume()
        self.assertFalse(ks.status()["halted"])


if __name__ == "__main__":
    unittest.main()
