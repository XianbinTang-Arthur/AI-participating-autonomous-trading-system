"""Verify the repository's reproducible dependency and image contracts.

This script intentionally uses only the Python standard library so that CI can
run it before installing any third-party package.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "requirements" / "runtime-py312-linux-x86_64.lock"
CI_LOCK = ROOT / "requirements" / "ci-py312-linux-x86_64.lock"
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "deploy" / "wsl2-dev" / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "quality.yml"
COMPOSE_DIR = ROOT / "deploy" / "wsl2-dev"

PYTHON_IMAGE = (
    "python:3.12-slim@"
    "sha256:3ecf5ebe01fef4b6e81be34511fb40bf378ea7fd81ab215ba15b2775ef85413d"
)
EXPECTED_EXTERNAL_IMAGES = {
    "postgres:16-alpine": "cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
    "redis:7-alpine": "ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf",
    "oliver006/redis_exporter:v1.58.0-alpine": (
        "f8b9ce3393afb619696f43e000c93369258109b0ea82a37ba4d29d000c277f2f"
    ),
    "nats:2.10-alpine": "b83efabe3e7def1e0a4a31ec6e078999bb17c80363f881df35edc70fcb6bb927",
    "grafana/loki:3.0.0": "757b5fadf816a1396f1fea598152947421fa49cb8b2db1ddd2a6e30fae003253",
    "jaegertracing/all-in-one:1.57": (
        "8f165334f418ca53691ce358c19b4244226ed35c5d18408c5acf305af2065fb9"
    ),
    "prom/prometheus:v2.51.0": (
        "5ccad477d0057e62a7cd1981ffcc43785ac10c5a35522dc207466ff7e7ec845f"
    ),
    "grafana/grafana:12.4.3": (
        "2e986801428cd689c2358605289c90ab37d2b39e24808874971f54c99bcdc412"
    ),
    "grafana/promtail:3.0.0": (
        "d3de3da9431cfbe74a6a94555050df5257f357e827be8e63f8998d509c37af8b"
    ),
}

_LOCK_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*\\?$"
)
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s*\\)?$")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_IMAGE_LINE = re.compile(r"^\s*image:\s*([^\s#]+)", re.MULTILINE)


def canonical_name(name: str) -> str:
    """Return the PEP 503 normalized distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Parse and strictly validate a uv-generated hashed requirements lock."""

    packages: dict[str, tuple[str, tuple[str, ...]]] = {}
    current_name: str | None = None
    current_version: str | None = None
    current_hashes: list[str] = []

    def commit() -> None:
        nonlocal current_name, current_version, current_hashes
        if current_name is None or current_version is None:
            return
        if not current_hashes:
            raise ValueError(f"{path}: {current_name} has no SHA-256 hashes")
        name = canonical_name(current_name)
        if name in packages:
            raise ValueError(f"{path}: duplicate package {name}")
        packages[name] = (current_version, tuple(current_hashes))
        current_name = None
        current_version = None
        current_hashes = []

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = _LOCK_REQUIREMENT.fullmatch(line)
        if requirement:
            commit()
            current_name, current_version = requirement.groups()
            continue
        digest = _HASH.fullmatch(line)
        if digest and current_name is not None:
            current_hashes.append(digest.group(1))
            continue
        raise ValueError(f"{path}:{line_number}: unsupported lock syntax: {raw_line!r}")

    commit()
    if not packages:
        raise ValueError(f"{path}: lock is empty")
    return packages


def dependency_names(requirements: list[str]) -> set[str]:
    """Extract normalized direct distribution names from PEP 508 requirements."""

    names: set[str] = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement)
        if match is None:
            raise ValueError(f"unsupported project requirement: {requirement!r}")
        names.add(canonical_name(match.group(1)))
    return names


def expected_direct_dependencies(*extras: str) -> set[str]:
    """Read current direct dependencies and selected extras from pyproject.toml."""

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    requirements = list(project["dependencies"])
    optional = project["optional-dependencies"]
    for extra in extras:
        requirements.extend(optional[extra])
    return dependency_names(requirements)


def verify_lock_coverage() -> tuple[int, int]:
    """Verify strict lock syntax and all current direct dependency coverage."""

    runtime = parse_lock(RUNTIME_LOCK)
    ci = parse_lock(CI_LOCK)
    runtime_expected = expected_direct_dependencies("nats", "redis", "otel") | {
        "greenlet",
        "setuptools",
        "wheel",
    }
    ci_expected = expected_direct_dependencies(
        "test",
        "lint",
        "postgres-integration",
    ) | {"greenlet"}

    for label, expected, locked in (
        ("runtime", runtime_expected, set(runtime)),
        ("ci", ci_expected, set(ci)),
    ):
        missing = sorted(expected - locked)
        if missing:
            raise ValueError(f"{label} lock misses direct dependencies: {missing}")
    return len(runtime), len(ci)


def verify_dockerfile() -> None:
    """Verify digest-pinned base stages and the hash-locked install path."""

    text = DOCKERFILE.read_text(encoding="utf-8")
    if text.count(f"FROM {PYTHON_IMAGE}") != 2:
        raise ValueError("Dockerfile must pin both Python stages to the approved digest")
    required = (
        "requirements/runtime-py312-linux-x86_64.lock",
        "--require-hashes",
        "--only-binary=:all:",
        "pip install --no-deps --no-build-isolation -e .",
    )
    for token in required:
        if token not in text:
            raise ValueError(f"Dockerfile misses dependency contract token: {token}")
    forbidden = ('pip install --upgrade', 'pip install "grpcio', 'pip install -e ".[nats')
    for token in forbidden:
        if token in text:
            raise ValueError(f"Dockerfile retains an unlocked install path: {token}")


def verify_workflow() -> None:
    """Verify CI checks and installs the committed CI lock before tests."""

    text = WORKFLOW.read_text(encoding="utf-8")
    required = (
        "python scripts/verify_dependency_locks.py",
        "requirements/ci-py312-linux-x86_64.lock",
        "--require-hashes",
        "--only-binary=:all:",
    )
    for token in required:
        if token not in text:
            raise ValueError(f"quality workflow misses dependency contract token: {token}")
    if "pip install -e" in text:
        raise ValueError("quality workflow must not resolve editable project dependencies")


def verify_compose_images() -> int:
    """Verify every external Compose image uses the reviewed tag and digest."""

    found: dict[str, str] = {}
    for path in sorted(COMPOSE_DIR.glob("docker-compose*.yml")):
        for reference in _IMAGE_LINE.findall(path.read_text(encoding="utf-8")):
            if reference == "aats-base:dev":
                continue
            if "@sha256:" not in reference:
                raise ValueError(f"{path}: external image is not digest pinned: {reference}")
            tag, digest = reference.rsplit("@sha256:", 1)
            if tag in found and found[tag] != digest:
                raise ValueError(f"external image has conflicting digests: {tag}")
            found[tag] = digest

    if found != EXPECTED_EXTERNAL_IMAGES:
        missing = sorted(set(EXPECTED_EXTERNAL_IMAGES) - set(found))
        unexpected = sorted(set(found) - set(EXPECTED_EXTERNAL_IMAGES))
        changed = sorted(
            tag
            for tag in set(found) & set(EXPECTED_EXTERNAL_IMAGES)
            if found[tag] != EXPECTED_EXTERNAL_IMAGES[tag]
        )
        raise ValueError(
            "Compose image contract drifted: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )
    return len(found)


def main() -> int:
    """Run all dependency reproducibility contract checks."""

    runtime_count, ci_count = verify_lock_coverage()
    verify_dockerfile()
    verify_workflow()
    image_count = verify_compose_images()
    print(
        "dependency lock contract OK: "
        f"runtime={runtime_count} ci={ci_count} external_images={image_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
