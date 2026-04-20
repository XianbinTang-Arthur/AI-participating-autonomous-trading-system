"""S5 锚点测试：防止未来无脑改 ``_SINGLEFLIGHT_WAIT_SECONDS``。

gateway_slow_query_systematic_fix_sow.md §S5 明确：

- follower 最长等待 25s
- 必须 **严格小于** 前端 ``api-client.js::DEFAULT_TIMEOUT_MS`` 的秒数
  （当前 30s），否则 follower 会被前端 abort 变成孤儿

2026-04-20 事故期曾因为误判惊群问题，把值调到 60s（并要求前端超时调
到 75s+）。SOW §S5 推翻这条路径，把它改回 25s 并把前端超时守在 30s。

本测试只是把这条约束写进代码，避免后续人员看到 "25s" 不懂为什么、
又改回 "60s"。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from aats.services.operator.query_service import OperatorQueryService


_API_CLIENT_JS = (
    Path(__file__).resolve().parents[2]
    / "aats"
    / "api"
    / "static"
    / "modules"
    / "api-client.js"
)


def _parse_default_timeout_ms() -> int:
    """从 api-client.js 解析 ``DEFAULT_TIMEOUT_MS = 30_000`` 这样的字面量。

    如果前端文件改了变量名/格式，测试会找不到然后失败——那也是 signal：
    重新核对 SOW §S5 的 "前端红线 30s" 约束是否仍然成立。
    """
    text = _API_CLIENT_JS.read_text(encoding="utf-8")
    # 锁定到 `const DEFAULT_TIMEOUT_MS = 30_000;` 赋值行，而不是注释里的
    # `DEFAULT_TIMEOUT_MS=60s` 这种历史说明文字。
    m = re.search(r"const\s+DEFAULT_TIMEOUT_MS\s*=\s*([\d_]+)", text)
    if m is None:
        raise AssertionError(
            f"无法在 {_API_CLIENT_JS} 里找到 `const DEFAULT_TIMEOUT_MS = ...` "
            f"赋值——前端 api-client.js 结构变了？请同步更新 SOW §S5 的约束。"
        )
    return int(m.group(1).replace("_", ""))


class TestSingleflightWaitSeconds(unittest.TestCase):
    def test_value_matches_sow_s5(self) -> None:
        """SOW §S5 明确要求 follower 最多等 25s。"""
        self.assertEqual(
            OperatorQueryService._SINGLEFLIGHT_WAIT_SECONDS,
            25,
            "SOW §S5 的 follower 等待窗口是 25s；改这个值必须先更新 SOW。",
        )

    def test_value_strictly_less_than_frontend_timeout(self) -> None:
        """必须 < 前端 DEFAULT_TIMEOUT_MS（秒）。

        如果 follower 等 >= 前端超时，前端会先 abort，follower 即使拿到
        leader 结果也没人接，变成孤儿。留 ≥ 2s 的 buffer 给网络 RTT 和
        服务端 serialize/flush 时间。
        """
        frontend_timeout_ms = _parse_default_timeout_ms()
        frontend_timeout_s = frontend_timeout_ms / 1000
        self.assertLess(
            OperatorQueryService._SINGLEFLIGHT_WAIT_SECONDS,
            frontend_timeout_s - 2,
            f"follower 等 {OperatorQueryService._SINGLEFLIGHT_WAIT_SECONDS}s "
            f"不应 ≥ 前端超时 {frontend_timeout_s}s - 2s buffer。"
            f"follower 变孤儿的风险 → UI 显示 signal aborted。",
        )


if __name__ == "__main__":
    unittest.main()
