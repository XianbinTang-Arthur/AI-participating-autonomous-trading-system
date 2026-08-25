#!/usr/bin/env python3
"""Evaluate an isolated derivatives fault-drill manifest and write it once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.operations.fault_matrix import (
    evaluate_fault_matrix,
    parse_fault_observations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("fault_manifest_must_be_object")
    evidence = evaluate_fault_matrix(parse_fault_observations(payload))
    digest = immutable_json_write(evidence.to_dict(), args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "sha256": digest,
                "passed": evidence.passed,
                "reason_codes": list(evidence.reason_codes),
            },
            ensure_ascii=False,
        )
    )
    return 0 if evidence.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
