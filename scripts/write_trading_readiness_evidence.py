#!/usr/bin/env python3
"""Evaluate a strict readiness manifest and write immutable evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.operations.trading_readiness import (
    evaluate_trading_readiness,
    parse_readiness_facts,
)


_SECRET_MARKERS = ("api_key", "authorization", "database_url", "password", "secret", "token")


def _reject_secret_keys(value: Any, *, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS):
                raise ValueError(f"secret_material_forbidden:{path}.{key}")
            _reject_secret_keys(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, path=f"{path}[{index}]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("readiness_manifest_must_be_object")
    _reject_secret_keys(payload)
    evidence = evaluate_trading_readiness(
        target=str(payload["target"]),
        profile=str(payload["profile"]),
        git_commit=str(payload["git_commit"]),
        image_identity=str(payload["image_identity"]),
        schema_revision=str(payload["schema_revision"]),
        facts=parse_readiness_facts(payload),
    )
    digest = immutable_json_write(evidence.to_dict(), args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "sha256": digest,
                "simulation_ready": evidence.simulation_ready,
                "production_ready": evidence.production_ready,
                "trading_ready": evidence.trading_ready,
                "reason_codes": list(evidence.reason_codes),
            },
            ensure_ascii=False,
        )
    )
    return 0 if evidence.simulation_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
