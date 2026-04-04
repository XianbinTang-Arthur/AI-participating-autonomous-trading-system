"""指标注册表 — 存储/加载 metrics snapshot 和历史.

工作包 A: 生成 metrics snapshot，维护 metrics 历史。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aats.data_platform.metrics.metric_calculator import (
    calculate_all_metrics,
    flatten_metrics,
)


def _atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _snapshot_path(root: Path) -> Path:
    return root / "artifacts" / "metrics" / "current_metrics_snapshot.json"


def _history_path(root: Path) -> Path:
    return root / "artifacts" / "metrics" / "metrics_history.json"


def build_metrics_snapshot(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict:
    """生成一次 metrics snapshot.

    Returns:
        snapshot dict with metrics_by_layer, flat_metrics, metadata
    """
    now = datetime.now(timezone.utc)
    by_layer = calculate_all_metrics(root, family, timeframe)
    flat = flatten_metrics(by_layer)

    snapshot = {
        "snapshot_id": f"snap_{now.strftime('%Y%m%d_%H%M%S')}",
        "generated_at": now.isoformat(),
        "filter": {
            "family": family,
            "timeframe": timeframe,
        },
        "metrics_by_layer": by_layer,
        "flat_metrics": flat,
        "summary": _build_summary(by_layer),
    }

    # 保存 current snapshot
    _atomic_write_json(_snapshot_path(root), snapshot)

    # 追加到历史
    _append_to_history(root, snapshot)

    return snapshot


def _build_summary(by_layer: dict) -> dict:
    """从分层指标构建摘要."""
    total_metrics = 0
    non_zero = 0
    for layer, metrics in by_layer.items():
        for k, v in metrics.items():
            total_metrics += 1
            if isinstance(v, (int, float)) and v != 0:
                non_zero += 1

    return {
        "total_metrics": total_metrics,
        "non_zero_metrics": non_zero,
        "layers": list(by_layer.keys()),
    }


def load_current_snapshot(root: Path) -> dict | None:
    """加载当前 snapshot."""
    fp = _snapshot_path(root)
    if not fp.exists():
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def _append_to_history(root: Path, snapshot: dict) -> None:
    """将 snapshot 追加到历史."""
    fp = _history_path(root)
    if fp.exists():
        with open(fp, "r", encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = {"snapshots": [], "generated_at": None}

    # 只保存精简版本
    entry = {
        "snapshot_id": snapshot["snapshot_id"],
        "generated_at": snapshot["generated_at"],
        "filter": snapshot["filter"],
        "flat_metrics": snapshot["flat_metrics"],
    }
    history["snapshots"].append(entry)
    history["generated_at"] = snapshot["generated_at"]

    # 限制历史长度: 保留最近 200 条
    if len(history["snapshots"]) > 200:
        history["snapshots"] = history["snapshots"][-200:]

    _atomic_write_json(fp, history)


def load_metrics_history(root: Path) -> dict:
    """加载 metrics 历史."""
    fp = _history_path(root)
    if not fp.exists():
        return {"snapshots": [], "generated_at": None}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def get_latest_snapshot_for_filter(
    root: Path,
    family: str | None = None,
    timeframe: str | None = None,
) -> dict | None:
    """从历史中获取匹配 filter 的最新 snapshot."""
    history = load_metrics_history(root)
    for snap in reversed(history.get("snapshots", [])):
        filt = snap.get("filter", {})
        if filt.get("family") == family and filt.get("timeframe") == timeframe:
            return snap
    return None


def compare_snapshots(
    current: dict, baseline: dict
) -> dict[str, dict]:
    """比较两个 snapshot 的指标差异.

    Returns:
        {metric_name: {"current": v, "baseline": v, "delta": v, "direction": str}}
    """
    from aats.data_platform.metrics.definitions import METRICS_BY_NAME

    cur_flat = current.get("flat_metrics", {})
    base_flat = baseline.get("flat_metrics", {})

    comparison = {}
    all_keys = set(cur_flat.keys()) | set(base_flat.keys())
    for k in sorted(all_keys):
        cur_v = cur_flat.get(k, 0)
        base_v = base_flat.get(k, 0)
        delta = round(cur_v - base_v, 6) if isinstance(cur_v, (int, float)) else 0

        defn = METRICS_BY_NAME.get(k)
        if defn and defn.direction == "higher_is_better":
            trend = "improved" if delta > 0 else ("regressed" if delta < 0 else "unchanged")
        elif defn and defn.direction == "lower_is_better":
            trend = "improved" if delta < 0 else ("regressed" if delta > 0 else "unchanged")
        else:
            trend = "changed" if delta != 0 else "unchanged"

        comparison[k] = {
            "current": cur_v,
            "baseline": base_v,
            "delta": delta,
            "trend": trend,
        }

    return comparison
