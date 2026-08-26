"""FS-022: reproducible Python dependencies and image reference contracts."""

from __future__ import annotations

import re

from scripts import verify_dependency_locks as locks


def test_runtime_and_ci_locks_are_complete_pinned_and_hashed() -> None:
    runtime = locks.parse_lock(locks.RUNTIME_LOCK)
    ci = locks.parse_lock(locks.CI_LOCK)

    assert len(runtime) == 47
    assert len(ci) == 41
    for packages in (runtime, ci):
        assert all(version and not version.startswith((">", "<", "~", "!")) for version, _ in packages.values())
        assert all(hashes for _version, hashes in packages.values())
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", digest)
            for _version, hashes in packages.values()
            for digest in hashes
        )


def test_locks_cover_current_direct_dependencies_and_build_tools() -> None:
    runtime = set(locks.parse_lock(locks.RUNTIME_LOCK))
    ci = set(locks.parse_lock(locks.CI_LOCK))

    assert locks.expected_direct_dependencies("nats", "redis", "otel") <= runtime
    assert {"greenlet", "setuptools", "wheel"} <= runtime
    assert locks.expected_direct_dependencies(
        "test",
        "lint",
        "postgres-integration",
    ) <= ci
    assert "greenlet" in ci


def test_dockerfile_uses_digest_base_and_hash_locked_binary_install() -> None:
    locks.verify_dockerfile()


def test_quality_workflow_uses_ci_lock_without_editable_resolution() -> None:
    locks.verify_workflow()


def test_all_external_compose_images_match_reviewed_digests() -> None:
    assert locks.verify_compose_images() == 9


def test_repository_dependency_contract_entrypoint() -> None:
    assert locks.main() == 0
