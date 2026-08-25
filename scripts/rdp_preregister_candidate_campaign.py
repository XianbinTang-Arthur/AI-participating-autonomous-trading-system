#!/usr/bin/env python3
"""Generate immutable new-candidate plans before development results exist."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aats.data_platform.research_factory.preregistered_campaign import (  # noqa: E402
    load_preregistered_campaign,
    register_preregistered_campaign,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument(
        "--artifact-root",
        type=pathlib.Path,
        default=pathlib.Path("artifacts/research/research_factory"),
    )
    args = parser.parse_args(argv)
    spec = load_preregistered_campaign(args.config)
    result = register_preregistered_campaign(
        spec,
        artifact_root=args.artifact_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
