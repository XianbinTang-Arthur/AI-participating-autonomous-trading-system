"""Tiny helper: 从 stdin 读 JSON，递归打印包含 obligation/cache/phase1 关键字的 key。

仅用于 probe §9.8.4 快速扫描 API 返回。
"""
from __future__ import annotations

import json
import sys


def find(obj: object, prefix: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            np = f"{prefix}.{k}" if prefix else k
            lk = k.lower()
            if "oblig" in lk or "cache" in lk or "phase1" in lk:
                disp = v if not isinstance(v, (dict, list)) else type(v).__name__
                print(f"{np} = {disp}")
            find(v, np)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            find(v, f"{prefix}[{i}]")


if __name__ == "__main__":
    data = json.load(sys.stdin)
    find(data)
