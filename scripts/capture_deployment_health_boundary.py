#!/usr/bin/env python3
"""Capture a no-secret fingerprint of one healthy simulation app topology."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import write_deployment_evidence as evidence  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("spot", "derivatives"), required=True)
    parser.add_argument("--runtime-readiness-generation", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--nats-target-manifest-sha256", required=True)
    parser.add_argument("--required-container", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expected_names = evidence._REQUIRED_CONTAINERS_BY_PROFILE[args.profile]
    if set(args.required_container) != set(expected_names) or len(
        args.required_container
    ) != len(expected_names):
        raise ValueError("required_container_set_mismatch")
    if not evidence._READINESS_GENERATION_RE.fullmatch(
        args.runtime_readiness_generation
    ):
        raise ValueError("invalid_runtime_readiness_generation")
    if not re.fullmatch(r"[0-9a-f]{40}", args.deployed_commit):
        raise ValueError("invalid_deployed_commit")
    image_id = evidence._run_command(
        ("docker", "image", "inspect", "aats-base:dev", "--format", "{{.Id}}")
    )
    if not evidence._IMAGE_RE.fullmatch(image_id):
        raise RuntimeError("invalid_base_image_id")
    container_facts, gateway_bindings = evidence._container_snapshot(
        expected_names,
        expected_image_id=image_id,
        expected_generation=args.runtime_readiness_generation,
        expected_commit=args.deployed_commit,
        expected_profile=args.profile,
        expected_target_manifest_sha256=args.nats_target_manifest_sha256,
        run=evidence._run_command,
    )
    print(
        evidence._app_runtime_snapshot_fingerprint(
            container_facts,
            gateway_bindings,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
