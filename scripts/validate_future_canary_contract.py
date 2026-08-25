#!/usr/bin/env python3
"""Validate the design-only future canary contract without enabling it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aats.data_platform.operations.canary_contract import validate_canary_contract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("configs/canary/derivatives_canary_contract.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.contract.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("canary_contract_must_be_object")
    result = validate_canary_contract(payload)
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.valid and result.deployable is False else 2


if __name__ == "__main__":
    raise SystemExit(main())
