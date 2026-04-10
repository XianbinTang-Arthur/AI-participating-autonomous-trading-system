from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the AATS API gateway.")
    parser.add_argument(
        "--profile",
        choices=("spot", "derivatives", "spot_live", "derivatives_live"),
        required=True,
        help="Profile template to load before starting the API gateway.",
    )
    parser.add_argument("--host", default=None, help="Override the bind host for this process.")
    parser.add_argument("--port", type=int, default=None, help="Override the bind port for this process.")
    return parser.parse_args()


def apply_runtime_bind_overrides(*, host: str | None, port: int | None) -> None:
    if host is not None:
        os.environ["AATS_API_HOST"] = host
    if port is not None:
        os.environ["AATS_API_PORT"] = str(port)


def resolved_api_bind() -> tuple[str, int]:
    from aats.bootstrap.config import load_settings

    settings = load_settings()
    return settings.api_host, settings.api_port


def main() -> None:
    from aats.bootstrap.env_profiles import load_profiled_dotenv_into_process

    args = parse_args()
    project_root = ROOT
    load_profiled_dotenv_into_process(project_root, args.profile)
    apply_runtime_bind_overrides(host=args.host, port=args.port)
    os.chdir(project_root)
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)
    host, port = resolved_api_bind()
    print(f"Starting AATS API gateway with profile={args.profile} on http://{host}:{port}")
    uvicorn.run(
        "apps.api_gateway.main:app",
        host=host,
        port=port,
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()
