import base64
from pathlib import Path

import pytest

from scripts import check_nats_durable_cutover as cutover
from scripts import nats_target_env_snapshot as snapshot


def _rendered_snapshot(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "profile.env"
    source.write_text(
        "\n".join(
            (
                "DATABASE_URL=postgresql://private-user:private-password@db/aats",
                "export AATS_NATS_EVENTS_MAX_AGE_SECONDS='172800'",
                'AATS_NATS_MARKET_MAX_BYTES="3221225472"',
                "UNRELATED_TOKEN=do-not-copy-this",
                "",
            )
        ),
        encoding="utf-8",
    )
    digest, encoded = snapshot.render_snapshot(source)
    payload = base64.b64decode(encoded, validate=True).decode("ascii")
    snapshot_path = tmp_path / "target.env"
    snapshot_path.write_text(payload, encoding="ascii", newline="\n")
    return snapshot_path, digest, payload


def test_render_emits_exact_canonical_allowlist_and_matches_manifest(
    tmp_path: Path,
) -> None:
    snapshot_path, digest, payload = _rendered_snapshot(tmp_path)

    lines = payload.splitlines()
    expected_keys = tuple(sorted(cutover._TARGET_ENV_FIELDS))
    assert len(lines) == 8
    assert tuple(line.partition("=")[0] for line in lines) == expected_keys
    assert payload.endswith("\n")
    assert "private-password" not in payload
    assert "do-not-copy-this" not in payload
    assert "DATABASE_URL" not in payload
    assert "UNRELATED_TOKEN" not in payload
    assert "AATS_NATS_EVENTS_MAX_AGE_SECONDS=172800.0" in lines
    assert "AATS_NATS_MARKET_MAX_BYTES=3221225472" in lines
    assert snapshot.verify_snapshot(snapshot_path, digest) == digest
    assert cutover.load_target_stream_manifest(snapshot_path)["sha256"] == digest


def test_render_is_deterministic_across_source_order_and_unrelated_values(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text(
        "AATS_NATS_EVENTS_MAX_MSGS=6000000\nSECRET=one\n",
        encoding="utf-8",
    )
    second.write_text(
        "SECRET=two\nAATS_NATS_EVENTS_MAX_MSGS=6000000\n",
        encoding="utf-8",
    )

    assert snapshot.render_snapshot(first) == snapshot.render_snapshot(second)


def test_render_cli_outputs_only_digest_and_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "profile.env"
    source.write_text(
        "SECRET_TOKEN=never-print-me\nAATS_NATS_MARKET_MAX_MSGS=7000000\n",
        encoding="utf-8",
    )

    assert snapshot.main(("render", "--source", str(source))) == 0

    captured = capsys.readouterr()
    digest, separator, encoded = captured.out.rstrip("\n").partition("\t")
    assert separator == "\t"
    assert snapshot._SHA256_RE.fullmatch(digest)
    assert base64.b64decode(encoded, validate=True).count(b"\n") == 8
    assert "never-print-me" not in captured.out + captured.err
    assert str(source) not in captured.out + captured.err


def test_verify_cli_outputs_only_verified_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot_path, digest, _payload = _rendered_snapshot(tmp_path)

    assert (
        snapshot.main(
            (
                "verify",
                "--snapshot",
                str(snapshot_path),
                "--expected-sha256",
                digest,
            )
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == f"{digest}\n"
    assert captured.err == ""
    assert str(snapshot_path) not in captured.out


@pytest.mark.parametrize(
    "mutation",
    (
        lambda lines: lines[1:],
        lambda lines: [*lines, "AATS_NATS_EXTRA=1"],
        lambda lines: [lines[0], lines[0], *lines[2:]],
        lambda lines: ["# forbidden", *lines[1:]],
        lambda lines: [f"export {lines[0]}", *lines[1:]],
        lambda lines: [lines[0].replace("=", "='", 1) + "'", *lines[1:]],
        lambda lines: [lines[1], lines[0], *lines[2:]],
        lambda lines: [lines[0] + "=1", *lines[1:]],
    ),
    ids=(
        "missing",
        "extra",
        "duplicate",
        "comment",
        "export",
        "quoted",
        "unsorted",
        "extra_equals",
    ),
)
def test_verify_rejects_noncanonical_structure(
    tmp_path: Path,
    mutation,
) -> None:
    snapshot_path, digest, payload = _rendered_snapshot(tmp_path)
    mutated = mutation(payload.splitlines())
    snapshot_path.write_bytes(("\n".join(mutated) + "\n").encode("ascii"))

    with pytest.raises(RuntimeError, match="nats_target_snapshot_invalid_format"):
        snapshot.verify_snapshot(snapshot_path, digest)


@pytest.mark.parametrize(
    ("key", "replacement", "error"),
    (
        (
            "AATS_NATS_EVENTS_MAX_BYTES",
            "01",
            "nats_target_snapshot_invalid_numeric",
        ),
        (
            "AATS_NATS_EVENTS_MAX_BYTES",
            "+1",
            "nats_target_snapshot_invalid_numeric",
        ),
        (
            "AATS_NATS_EVENTS_MAX_AGE_SECONDS",
            "86400",
            "nats_target_snapshot_noncanonical_numeric",
        ),
        (
            "AATS_NATS_EVENTS_MAX_AGE_SECONDS",
            "86400.00",
            "nats_target_snapshot_noncanonical_numeric",
        ),
        (
            "AATS_NATS_EVENTS_MAX_AGE_SECONDS",
            "nan",
            "nats_target_snapshot_invalid_manifest",
        ),
        (
            "AATS_NATS_EVENTS_MAX_AGE_SECONDS",
            "-1.0",
            "nats_target_snapshot_invalid_manifest",
        ),
    ),
)
def test_verify_rejects_noncanonical_or_invalid_numeric_values(
    tmp_path: Path,
    key: str,
    replacement: str,
    error: str,
) -> None:
    snapshot_path, digest, payload = _rendered_snapshot(tmp_path)
    lines = [
        f"{key}={replacement}" if line.startswith(f"{key}=") else line
        for line in payload.splitlines()
    ]
    snapshot_path.write_bytes(("\n".join(lines) + "\n").encode("ascii"))

    with pytest.raises(RuntimeError, match=error):
        snapshot.verify_snapshot(snapshot_path, digest)


@pytest.mark.parametrize(
    "payload_suffix",
    (
        "no_final_newline",
        "crlf",
    ),
)
def test_verify_rejects_noncanonical_line_endings(
    tmp_path: Path,
    payload_suffix: str,
) -> None:
    snapshot_path, digest, payload = _rendered_snapshot(tmp_path)
    if payload_suffix == "no_final_newline":
        snapshot_path.write_bytes(payload.rstrip("\n").encode("ascii"))
    else:
        snapshot_path.write_bytes(payload.replace("\n", "\r\n").encode("ascii"))

    with pytest.raises(RuntimeError, match="nats_target_snapshot_invalid_format"):
        snapshot.verify_snapshot(snapshot_path, digest)


def test_verify_rejects_wrong_or_malformed_expected_hash(tmp_path: Path) -> None:
    snapshot_path, digest, _payload = _rendered_snapshot(tmp_path)
    wrong_digest = "sha256:" + ("0" * 64)
    assert wrong_digest != digest

    with pytest.raises(
        RuntimeError,
        match="nats_target_snapshot_manifest_hash_mismatch",
    ):
        snapshot.verify_snapshot(snapshot_path, wrong_digest)
    with pytest.raises(
        RuntimeError,
        match="nats_target_snapshot_invalid_expected_sha256",
    ):
        snapshot.verify_snapshot(snapshot_path, "not-a-digest")


def test_render_failure_does_not_echo_source_path_or_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "credential-bearing-profile.env"
    source.write_text(
        "SECRET_TOKEN=never-print-me\nAATS_NATS_EVENTS_MAX_MSGS=invalid\n",
        encoding="utf-8",
    )

    assert snapshot.main(("render", "--source", str(source))) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "nats_cutover_invalid_target_env_override\n"
    assert "never-print-me" not in captured.err
    assert str(source) not in captured.err
