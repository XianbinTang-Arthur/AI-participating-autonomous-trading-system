"""Stage 5d：decision 进程入口。

装 shared + market + decision slice。决策结果通过 EventBus 发往 execution 进程。
通过 process_role="decision" 让 build_runtime 跳过 execution/portfolio/reconciliation。
"""
from __future__ import annotations

import sys

from aats.bootstrap.process_lifecycle import run_process_sync
from aats.bootstrap.settings import PROCESS_ROLE_DECISION


def main() -> int:
    return run_process_sync(
        process_role=PROCESS_ROLE_DECISION,
        app_name="apps.decision_engine",
    )


if __name__ == "__main__":
    sys.exit(main())
