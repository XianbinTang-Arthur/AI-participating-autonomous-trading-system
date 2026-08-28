from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from aats.data_platform.decision_system import evidence_bundle
from aats.data_platform.decision_system import promotion_qualification
from aats.data_platform.governance import research_artifact_contract as contract


def test_stable_reader_returns_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    payload = b'{"status":"succeeded"}'
    path.write_bytes(payload)

    assert contract.read_stable_regular_artifact_file(
        path,
        parent=tmp_path,
        max_bytes=len(payload),
    ) == payload


def test_stable_reader_rejects_declared_capacity_overrun(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b"123456789")

    with pytest.raises(ValueError, match="research_round_file_too_large"):
        contract.read_stable_regular_artifact_file(
            path,
            parent=tmp_path,
            max_bytes=8,
        )


def test_stable_reader_detects_replacement_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.json"
    replacement = tmp_path / "replacement.json"
    path.write_bytes(b'{"version":"old"}')
    replacement.write_bytes(b'{"version":"replacement-with-different-size"}')
    original_open = contract.os.open

    def _replace_then_open(target: os.PathLike[str], flags: int) -> int:
        os.replace(replacement, path)
        return original_open(target, flags)

    monkeypatch.setattr(contract.os, "open", _replace_then_open)

    with pytest.raises(
        ValueError,
        match="research_round_file_changed_during_read",
    ):
        contract.read_stable_regular_artifact_file(
            path,
            parent=tmp_path,
        )


def test_stable_reader_detects_descriptor_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b'{"status":"succeeded"}')
    original_fstat = contract.os.fstat
    calls = 0

    def _changing_fstat(descriptor: int) -> os.stat_result | SimpleNamespace:
        nonlocal calls
        current = original_fstat(descriptor)
        calls += 1
        if calls == 1:
            return current
        return SimpleNamespace(
            st_dev=current.st_dev,
            st_ino=current.st_ino,
            st_mode=stat.S_IFREG | 0o600,
            st_size=current.st_size,
            st_mtime_ns=current.st_mtime_ns + 1,
        )

    monkeypatch.setattr(contract.os, "fstat", _changing_fstat)

    with pytest.raises(
        ValueError,
        match="research_round_file_changed_during_read",
    ):
        contract.read_stable_regular_artifact_file(
            path,
            parent=tmp_path,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"status":"succeeded","status":"failed"}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e400}',
        b'"\\ud800"',
        b"\xff",
    ],
)
def test_strict_json_decoder_rejects_ambiguous_or_invalid_json(
    payload: bytes,
) -> None:
    with pytest.raises(ValueError, match="research_artifact_json_invalid"):
        contract.decode_strict_json_artifact(payload)


def test_stable_json_reader_returns_payload_and_enforces_top_level_type(
    tmp_path: Path,
) -> None:
    path = tmp_path / "artifact.json"
    raw = b'{"status":"succeeded","count":1}'
    path.write_bytes(raw)

    payload, returned_raw = contract.read_stable_json_artifact(
        path,
        parent=tmp_path,
        expected_type=dict,
    )

    assert payload == {"status": "succeeded", "count": 1}
    assert returned_raw == raw
    with pytest.raises(ValueError, match="research_artifact_json_invalid"):
        contract.read_stable_json_artifact(
            path,
            parent=tmp_path,
            expected_type=list,
        )


def test_promotion_reader_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "round_manifest.json"
    path.write_bytes(b'{"status":"succeeded","status":"failed"}')

    assert promotion_qualification._strict_json(path, expected=dict) is None


def test_hash_bound_evidence_reader_preserves_invalid_json_reason(
    tmp_path: Path,
) -> None:
    path = tmp_path / "summary.json"
    raw = b'{"status":"succeeded","status":"failed"}'
    path.write_bytes(raw)

    payload, reason = evidence_bundle._load_hash_bound_json_object(
        path,
        expected_hash=hashlib.sha256(raw).hexdigest(),
    )

    assert payload is None
    assert reason == "backtest_manifest_bound_json_invalid"


def test_backtest_hash_reader_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "equity_curve.csv"
    path.write_bytes(b"123456789")
    monkeypatch.setattr(
        evidence_bundle,
        "_FORMAL_BACKTEST_ARTIFACT_MAX_BYTES",
        8,
    )

    with pytest.raises(ValueError, match="research_round_file_too_large"):
        evidence_bundle._sha256_file(path)
