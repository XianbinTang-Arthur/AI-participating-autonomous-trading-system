#!/usr/bin/env python3
"""Render and verify a canonical, no-secret NATS target environment snapshot.

The source deployment profile can contain credentials.  Rendering delegates
profile parsing to :func:`load_target_stream_manifest`, which intentionally
retains only the eight allowlisted NATS capacity values.  Output is limited to
the target manifest digest and a base64-encoded canonical snapshot.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import check_nats_durable_cutover as cutover  # noqa: E402


_EXPECTED_TARGET_KEY_COUNT = 8
_MAX_SNAPSHOT_BYTES = 4 * 1024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_POSITIVE_CANONICAL_INT_RE = re.compile(r"^[1-9][0-9]*$")


def _target_keys() -> tuple[str, ...]:
    keys = tuple(sorted(cutover._TARGET_ENV_FIELDS))
    if len(keys) != _EXPECTED_TARGET_KEY_COUNT:
        raise RuntimeError("nats_target_snapshot_allowlist_contract_changed")
    return keys


def _canonical_numeric(
    value: object,
    converter: type[int] | type[float],
) -> str:
    if converter is int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        return str(value)
    if converter is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        return repr(number)
    raise RuntimeError("nats_target_snapshot_invalid_manifest")


def _manifest_values(manifest: Mapping[str, object]) -> dict[str, int | float]:
    streams = manifest.get("streams")
    digest = manifest.get("sha256")
    if not isinstance(streams, list) or not isinstance(digest, str):
        raise RuntimeError("nats_target_snapshot_invalid_manifest")
    if _SHA256_RE.fullmatch(digest) is None:
        raise RuntimeError("nats_target_snapshot_invalid_manifest")

    by_name: dict[str, Mapping[str, object]] = {}
    for row in streams:
        if not isinstance(row, Mapping):
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        identity = row.get("identity")
        config = row.get("immutable_config")
        if not isinstance(identity, Mapping) or not isinstance(config, Mapping):
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        name = identity.get("name")
        if not isinstance(name, str) or name in by_name:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        by_name[name] = config

    values: dict[str, int | float] = {}
    for key in _target_keys():
        stream_name, field, converter = cutover._TARGET_ENV_FIELDS[key]
        config = by_name.get(stream_name)
        if config is None or field not in config:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        value = config[field]
        canonical = _canonical_numeric(value, converter)
        values[key] = converter(canonical)
    return values


def _snapshot_text(values: Mapping[str, int | float]) -> str:
    lines: list[str] = []
    for key in _target_keys():
        if key not in values:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        converter = cutover._TARGET_ENV_FIELDS[key][2]
        lines.append(f"{key}={_canonical_numeric(values[key], converter)}")
    return "\n".join(lines) + "\n"


def render_snapshot(source_path: Path) -> tuple[str, str]:
    """Return ``(manifest_sha256, base64_snapshot)`` without secret fields."""

    manifest = cutover.load_target_stream_manifest(source_path)
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise RuntimeError("nats_target_snapshot_invalid_manifest")
    snapshot = _snapshot_text(_manifest_values(manifest)).encode("ascii")
    encoded = base64.b64encode(snapshot).decode("ascii")
    return digest, encoded


def _read_snapshot(snapshot_path: Path) -> str:
    resolved = snapshot_path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise RuntimeError("nats_target_snapshot_not_regular_file")
    with resolved.open("rb") as handle:
        payload = handle.read(_MAX_SNAPSHOT_BYTES + 1)
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise RuntimeError("nats_target_snapshot_too_large")
    try:
        return payload.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("nats_target_snapshot_invalid_format") from exc


def _strict_snapshot_values(text: str) -> dict[str, int | float]:
    keys = _target_keys()
    if "\r" in text or not text.endswith("\n"):
        raise RuntimeError("nats_target_snapshot_invalid_format")
    lines = text.split("\n")
    if lines[-1] != "" or len(lines) != len(keys) + 1:
        raise RuntimeError("nats_target_snapshot_invalid_format")
    lines.pop()

    values: dict[str, int | float] = {}
    for expected_key, line in zip(keys, lines, strict=True):
        if not line or line.startswith("#") or line.startswith("export "):
            raise RuntimeError("nats_target_snapshot_invalid_format")
        key, separator, raw_value = line.partition("=")
        if (
            separator != "="
            or key != expected_key
            or not raw_value
            or "=" in raw_value
            or raw_value != raw_value.strip()
            or raw_value[0] in {'"', "'"}
            or raw_value[-1] in {'"', "'"}
            or key in values
        ):
            raise RuntimeError("nats_target_snapshot_invalid_format")

        converter = cutover._TARGET_ENV_FIELDS[key][2]
        try:
            if converter is int:
                if _POSITIVE_CANONICAL_INT_RE.fullmatch(raw_value) is None:
                    raise ValueError
                value: int | float = int(raw_value)
            else:
                value = float(raw_value)
        except (OverflowError, ValueError) as exc:
            raise RuntimeError("nats_target_snapshot_invalid_numeric") from exc
        if raw_value != _canonical_numeric(value, converter):
            raise RuntimeError("nats_target_snapshot_noncanonical_numeric")
        values[key] = value

    if tuple(values) != keys:
        raise RuntimeError("nats_target_snapshot_invalid_format")
    return values


def _manifest_from_values(values: Mapping[str, int | float]) -> dict[str, object]:
    default_manifest = cutover._default_target_stream_manifest()
    streams = copy.deepcopy(default_manifest.get("streams"))
    if not isinstance(streams, list):
        raise RuntimeError("nats_target_snapshot_invalid_manifest")

    configs: dict[str, dict[str, object]] = {}
    for row in streams:
        if not isinstance(row, dict):
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        identity = row.get("identity")
        config = row.get("immutable_config")
        if not isinstance(identity, dict) or not isinstance(config, dict):
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        name = identity.get("name")
        if not isinstance(name, str) or name in configs:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        configs[name] = config

    for key in _target_keys():
        stream_name, field, converter = cutover._TARGET_ENV_FIELDS[key]
        config = configs.get(stream_name)
        if config is None or key not in values:
            raise RuntimeError("nats_target_snapshot_invalid_manifest")
        canonical = _canonical_numeric(values[key], converter)
        config[field] = converter(canonical)

    canonical_streams = json.dumps(streams, sort_keys=True, separators=(",", ":"))
    return {
        "source": "profile_env_allowlist",
        "streams": streams,
        "sha256": "sha256:"
        + hashlib.sha256(canonical_streams.encode("utf-8")).hexdigest(),
    }


def verify_snapshot(snapshot_path: Path, expected_sha256: str) -> str:
    """Strictly verify an eight-line snapshot and return its manifest digest."""

    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise RuntimeError("nats_target_snapshot_invalid_expected_sha256")
    values = _strict_snapshot_values(_read_snapshot(snapshot_path))
    actual_sha256 = _manifest_from_values(values)["sha256"]
    if actual_sha256 != expected_sha256:
        raise RuntimeError("nats_target_snapshot_manifest_hash_mismatch")
    return expected_sha256


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render or verify a canonical no-secret NATS target snapshot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--source", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--snapshot", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "render":
            digest, encoded = render_snapshot(args.source)
            print(f"{digest}\t{encoded}")
            return 0
        if args.command == "verify":
            print(verify_snapshot(args.snapshot, args.expected_sha256))
            return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("nats_target_snapshot_io_error", file=sys.stderr)
        return 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
