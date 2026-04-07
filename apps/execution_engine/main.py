"""Stage 5d：execution 进程入口。

装 shared + execution + portfolio + reconciliation slice。订阅 decision 发出的
order intents 并通过 OKX adapter 下单。
通过 process_role="execution" 让 build_runtime 跳过 market/decision/UI gateway。
"""
from __future__ import annotations

import sys

from aats.bootstrap.process_lifecycle import run_process_sync
from aats.bootstrap.settings import PROCESS_ROLE_EXECUTION


def main() -> int:
    return run_process_sync(
        process_role=PROCESS_ROLE_EXECUTION,
        app_name="apps.execution_engine",
    )


if __name__ == "__main__":
    sys.exit(main())
