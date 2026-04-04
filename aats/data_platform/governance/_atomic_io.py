"""原子写入工具.

防止并发写入造成 JSON 文件损坏：
  写入临时文件 → flush → fsync → rename 替换原文件。
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
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
