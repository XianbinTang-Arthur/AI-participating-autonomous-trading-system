#!/usr/bin/env python3
"""下载并校验已通过容量门的官方历史恢复计划文件。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aats.data_platform.data_governance.historical_campaign import download_manifest_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--target-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    if args.apply != args.confirm:
        print("--apply 与 --confirm 必须同时使用", file=sys.stderr)
        return 4
    if not args.manifest.expanduser().is_absolute() or not args.target_dir.expanduser().is_absolute():
        print("manifest 与 target-dir 必须是绝对路径", file=sys.stderr)
        return 4
    manifest_path = args.manifest.expanduser().resolve()
    if not manifest_path.is_file():
        print("manifest 不存在", file=sys.stderr)
        return 4
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not args.apply:
        files = {
            item["filename"]
            for partition in manifest.get("partitions", [])
            for item in (*partition["trade_files"], partition["l2_file"])
        }
        print(json.dumps({"dry_run": True, "file_count": len(files)}, indent=2))
        return 2
    with httpx.Client(headers={"User-Agent": "AATS-RDP-Historical-Recovery/1.0"}) as client:
        results = download_manifest_files(client, manifest, args.target_dir)
    print(json.dumps([asdict(item) for item in results], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
