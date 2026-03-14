from __future__ import annotations

import unittest

from aats.bootstrap.config import build_runtime, load_settings


class TestRuntimeControls(unittest.IsolatedAsyncioTestCase):
    async def test_halt_blocks_execution_and_resume_allows_it(self) -> None:
        settings = load_settings()
        runtime = await build_runtime(settings)

        runtime.kill_switch.halt("test_halt")
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=2,
            interval_seconds=0.0,
        )
        self.assertEqual(len(runtime.execution_repo.order_states()), 0)

        runtime.kill_switch.resume()
        await runtime.market_gateway.run_local_publisher(
            symbol=settings.default_symbol,
            iterations=1,
            interval_seconds=0.0,
        )

        self.assertEqual(len(runtime.execution_repo.order_states()), 1)
        self.assertEqual(len(runtime.execution_repo.fills()), 1)


if __name__ == "__main__":
    unittest.main()

