from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the AATS API gateway.")
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives"),
        default=None,
        help="加载对应的环境模板；不传时优先读取根目录 .env。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(__file__).resolve().parent.parent
    load_profiled_dotenv_into_process(project_root, args.profile)
    os.chdir(project_root)
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)
    uvicorn.run(
        "apps.api_gateway.main:app",
        host=os.environ.get("AATS_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("AATS_API_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
