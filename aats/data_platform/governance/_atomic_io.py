"""原子写入工具.

防止并发写入造成 JSON 文件损坏：
  写入临时文件 → flush → fsync → rename 替换原文件。
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import tempfile
import uuid
from typing import Any


def atomic_json_write(
    data: Any,
    path: pathlib.Path,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """原子写入 JSON 文件.

    1. 写入同目录临时文件
    2. flush + fsync
    3. os.replace() 原子替换
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_path = path.parent

    fd, tmp_path = tempfile.mkstemp(
        dir=str(dir_path),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def immutable_json_write(
    data: Any,
    path: pathlib.Path,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> str:
    """Write JSON exactly once and return its SHA-256 digest.

    The temporary file is fully flushed before a hard link atomically claims
    the final name.  An existing target raises ``FileExistsError`` and is never
    replaced.  This is intended for audit evidence, not mutable registries.
    """

    import hashlib

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            data,
            indent=indent,
            ensure_ascii=ensure_ascii,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return digest
