#!/usr/bin/env bash
set -euo pipefail

# Build the repository-standard WSL2 Python environment without relying on
# Ubuntu's system Python or pip. The downloaded uv archive is checksum-verified
# and used only from a temporary directory.

UV_VERSION="0.12.5"
PYTHON_VERSION="3.12.14"
UV_TARGET="x86_64-unknown-linux-gnu"
UV_SHA256="68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
AATS_VENV_PATH="${AATS_WSL2_VENV:-${HOME}/aats-venv}"

case "${AATS_VENV_PATH}" in
  /*) ;;
  *)
    echo "AATS_WSL2_VENV must be an absolute path" >&2
    exit 2
    ;;
esac

if [[ "${AATS_VENV_PATH}" == "/" || "${AATS_VENV_PATH}" == "${HOME}" ]]; then
  echo "Refusing unsafe virtual-environment target: ${AATS_VENV_PATH}" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d -t aats-uv-bootstrap.XXXXXX)"
cleanup() {
  rm -rf -- "${TMP_DIR}"
}
trap cleanup EXIT

ASSET="uv-${UV_TARGET}.tar.gz"
RELEASE_BASE="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"
curl --fail --location --proto '=https' --tlsv1.2 \
  --output "${TMP_DIR}/${ASSET}" "${RELEASE_BASE}/${ASSET}"
(
  cd -- "${TMP_DIR}"
  printf '%s  %s\n' "${UV_SHA256}" "${ASSET}" | sha256sum --check -
  tar -xzf "${ASSET}"
)
UV_BIN="${TMP_DIR}/uv-${UV_TARGET}/uv"
"${UV_BIN}" --version

"${UV_BIN}" python install "${PYTHON_VERSION}"
if [[ ! -e "${AATS_VENV_PATH}" ]]; then
  "${UV_BIN}" venv --python "${PYTHON_VERSION}" "${AATS_VENV_PATH}"
elif [[ ! -x "${AATS_VENV_PATH}/bin/python" ]]; then
  echo "Target exists but is not a valid venv: ${AATS_VENV_PATH}" >&2
  echo "Move it aside after reviewing its contents, then rerun this script." >&2
  exit 3
fi

ACTUAL_VERSION="$("${AATS_VENV_PATH}/bin/python" -c 'import platform; print(platform.python_version())')"
if [[ "${ACTUAL_VERSION}" != "${PYTHON_VERSION}" ]]; then
  echo "Existing venv has Python ${ACTUAL_VERSION}; expected ${PYTHON_VERSION}." >&2
  echo "Move it aside after reviewing its contents, then rerun this script." >&2
  exit 3
fi

"${UV_BIN}" pip install \
  --python "${AATS_VENV_PATH}/bin/python" \
  --require-hashes \
  --requirement "${PROJECT_ROOT}/requirements/runtime-py312-linux-x86_64.lock"
"${UV_BIN}" pip install \
  --python "${AATS_VENV_PATH}/bin/python" \
  --require-hashes \
  --requirement "${PROJECT_ROOT}/requirements/ci-py312-linux-x86_64.lock"
"${UV_BIN}" pip install \
  --python "${AATS_VENV_PATH}/bin/python" \
  --no-deps \
  --editable "${PROJECT_ROOT}"

"${AATS_VENV_PATH}/bin/python" "${PROJECT_ROOT}/scripts/verify_dependency_locks.py"
"${AATS_VENV_PATH}/bin/python" -c \
  'import aats, psycopg, pyarrow, pytest, sqlalchemy; print("WSL_VENV_READY")'

echo "WSL2 environment ready: ${AATS_VENV_PATH} (Python ${ACTUAL_VERSION})"
