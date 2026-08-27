#!/usr/bin/env python3
"""下载并校验已通过容量门的官方历史恢复计划文件。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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
    if args.apply:
        print(
            "独立 manifest 下载入口已停用；完整 campaign 执行也保持冻结，"
            "等待持久 fencing 与不可变 Silver 验收后再开放",
            file=sys.stderr,
        )
        return 4
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {
        item["filename"]
        for partition in manifest.get("partitions", [])
        for item in (*partition["trade_files"], partition["l2_file"])
    }
    print(
        json.dumps(
            {
                "dry_run": True,
                "file_count": len(files),
                "download_authorized": False,
                "campaign_execution_available": False,
            },
            indent=2,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
