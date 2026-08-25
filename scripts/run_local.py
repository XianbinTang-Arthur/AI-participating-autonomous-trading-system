from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

MIGRATION_MESSAGE = """\
scripts/run_local.py 已停用：它属于旧的单进程 paper-loop 架构，不能启动当前 AATS runtime。
本地 API/UI 模拟联调请使用：.venv\\Scripts\\python.exe scripts/start_api.py --profile derivatives
有限迭代业务闭环请运行明确选择的 tests/integration 场景；不要把本脚本当作成功路径。
本次调用未加载任何 .env profile，也未启动服务。"""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="已停用的 AATS 单进程 paper-loop 兼容入口。",
        epilog="该命令只输出迁移指引并以非零状态退出。",
    )
    parser.add_argument("--iterations", type=int, default=None, help="Number of local market snapshots to publish.")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=None,
        help="Sleep interval between market snapshots.",
    )
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives"),
        default=None,
        help="旧参数，仅用于识别迁移调用；不会加载环境模板。",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    parse_args(argv)
    print(MIGRATION_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
