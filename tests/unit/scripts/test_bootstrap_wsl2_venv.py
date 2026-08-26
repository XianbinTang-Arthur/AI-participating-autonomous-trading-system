from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/bootstrap_wsl2_venv.sh"


def test_bootstrap_wsl2_venv_is_checksum_verified_and_lock_driven() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert 'AATS_VENV_PATH="${AATS_WSL2_VENV:-${HOME}/aats-venv}"' in content
    assert 'UV_SHA256="68a509da24b06b4223a1c0175fb5eb5bc' in content
    assert "sha256sum --check -" in content
    assert "${ASSET}.sha256" not in content
    assert "--require-hashes" in content
    assert "runtime-py312-linux-x86_64.lock" in content
    assert "ci-py312-linux-x86_64.lock" in content
    assert "verify_dependency_locks.py" in content
    assert "psycopg, pyarrow, pytest" in content
    assert "rm -rf -- \"${TMP_DIR}\"" in content


def test_bootstrap_wsl2_venv_refuses_broad_or_invalid_existing_target() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert '"${AATS_VENV_PATH}" == "/"' in content
    assert '"${AATS_VENV_PATH}" == "${HOME}"' in content
    assert "Target exists but is not a valid venv" in content
    assert "Move it aside after reviewing its contents" in content
