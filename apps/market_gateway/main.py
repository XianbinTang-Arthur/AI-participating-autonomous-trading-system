"""Stage 5d：market 进程入口。

只装 shared + market slice，不持有 decision/execution/portfolio/reconciliation。
通过 process_role="market" 让 build_runtime 内部按 _slice_active 跳过其余 slice。
"""
from __future__ import annotations

import sys

from aats.bootstrap.process_lifecycle import run_process_sync
from aats.bootstrap.settings import PROCESS_ROLE_MARKET


def main() -> int:
    return run_process_sync(
        process_role=PROCESS_ROLE_MARKET,
        app_name="apps.market_gateway",
    )


if __name__ == "__main__":
    sys.exit(main())
