from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values


def load_dotenv_into_process(dotenv_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("AATS_"):
            os.environ.pop(key, None)

    for key, value in dotenv_values(dotenv_path).items():
        if key is None or value is None:
            continue
        os.environ[key] = value


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    dotenv_path = project_root / ".env"
    load_dotenv_into_process(dotenv_path)
    os.chdir(project_root)
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)
    uvicorn.run("apps.api_gateway.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
