"""Replay result writer: save decisions to file artifacts.

Phase 2 数据存储策略（§14）：
- Registry 进库
- 大结果落文件
- 文件路径必须回写 experiment registry

支持格式：CSV, JSON（Phase 2 首批不强制 parquet，减少依赖）。
"""

from __future__ import annotations

import csv
import json
import logging
import pathlib
from typing import Any

from aats.data_platform.replay.core.replay_context import ReplayDecision

log = logging.getLogger(__name__)


def _ensure_dir(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_decisions_csv(
    decisions: list[ReplayDecision],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """将 replay decisions 写入 CSV 文件。"""
    _ensure_dir(output_path)

    if not decisions:
        log.warning("No decisions to write.")
        output_path.write_text("", encoding="utf-8")
        return output_path

    fieldnames = list(decisions[0].to_flat_dict().keys())
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for d in decisions:
            writer.writerow(d.to_flat_dict())

    log.info("Wrote %d decisions to %s", len(decisions), output_path)
    return output_path


def write_decisions_json(
    decisions: list[ReplayDecision],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """将 replay decisions 写入 JSON 文件。"""
    _ensure_dir(output_path)

    data = [d.to_flat_dict() for d in decisions]
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    log.info("Wrote %d decisions to %s", len(decisions), output_path)
    return output_path


def write_summary_json(
    summary: dict[str, Any],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """将 experiment summary 写入 JSON 文件。"""
    _ensure_dir(output_path)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    log.info("Wrote summary to %s", output_path)
    return output_path


def write_summary_csv(
    summary: dict[str, Any],
    output_path: pathlib.Path,
) -> pathlib.Path:
    """将 experiment summary 写入 CSV 文件（单行）。"""
    _ensure_dir(output_path)

    # 展平嵌套字典
    flat: dict[str, Any] = {}
    for k, v in summary.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat[f"{k}__{kk}"] = vv
        elif isinstance(v, list):
            flat[k] = json.dumps(v, ensure_ascii=False)
        else:
            flat[k] = v

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat.keys()))
        writer.writeheader()
        writer.writerow(flat)

    log.info("Wrote summary CSV to %s", output_path)
    return output_path
