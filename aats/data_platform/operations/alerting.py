"""告警管理模块.

工作包 C: 基于可靠性检查结果生成告警摘要，写入 current_alerts.json。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aats.data_platform.operations.reliability_checks import (
    ReliabilityCheckResult,
    run_all_checks,
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


def _alerts_path(root: Path) -> Path:
    return root / "artifacts" / "operations" / "alerts" / "current_alerts.json"


def _alert_history_dir(root: Path) -> Path:
    return root / "artifacts" / "operations" / "alerts" / "history"


def build_alert_summary(
    root: Path,
    results: list[ReliabilityCheckResult] | None = None,
) -> dict:
    """基于可靠性检查结果生成告警摘要.

    Returns:
        alert summary dict
    """
    if results is None:
        results = run_all_checks(root)

    now = datetime.now(timezone.utc)
    alerts = []
    for r in results:
        if not r.passed:
            alerts.append({
                "alert_id": f"alert_{r.name}_{now.strftime('%Y%m%d_%H%M%S')}",
                "check_name": r.name,
                "category": r.category,
                "severity": r.severity,
                "detail": r.detail,
                "timestamp": now.isoformat(),
                "acknowledged": False,
            })

    total_checks = len(results)
    passed_checks = sum(1 for r in results if r.passed)
    failed_checks = total_checks - passed_checks

    critical_count = sum(1 for a in alerts if a["severity"] == "critical")
    warning_count = sum(1 for a in alerts if a["severity"] == "warning")

    overall = "healthy"
    if critical_count > 0:
        overall = "critical"
    elif warning_count > 0:
        overall = "warning"

    summary = {
        "generated_at": now.isoformat(),
        "overall_status": overall,
        "total_checks": total_checks,
        "passed": passed_checks,
        "failed": failed_checks,
        "critical_alerts": critical_count,
        "warning_alerts": warning_count,
        "alerts": alerts,
        "check_results": [asdict(r) for r in results],
    }

    # 保存 current_alerts.json
    _atomic_write_json(_alerts_path(root), summary)

    # 保存到 history
    history_dir = _alert_history_dir(root)
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"alerts_{now.strftime('%Y%m%d_%H%M%S')}.json"
    _atomic_write_json(history_file, summary)

    return summary


def load_current_alerts(root: Path) -> dict | None:
    """加载当前告警摘要."""
    fp = _alerts_path(root)
    if not fp.exists():
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def acknowledge_alert(root: Path, alert_id: str) -> bool:
    """确认一条告警."""
    data = load_current_alerts(root)
    if data is None:
        return False
    for alert in data.get("alerts", []):
        if alert.get("alert_id") == alert_id:
            alert["acknowledged"] = True
            _atomic_write_json(_alerts_path(root), data)
            return True
    return False


def format_alert_summary_text(summary: dict) -> str:
    """将告警摘要格式化为文本输出."""
    lines = []
    lines.append(f"RDP Reliability Alert Summary")
    lines.append(f"Generated: {summary['generated_at']}")
    lines.append(f"Overall:   {summary['overall_status'].upper()}")
    lines.append(
        f"Checks:    {summary['passed']}/{summary['total_checks']} passed"
    )
    if summary["critical_alerts"] > 0:
        lines.append(f"CRITICAL:  {summary['critical_alerts']} alert(s)")
    if summary["warning_alerts"] > 0:
        lines.append(f"WARNING:   {summary['warning_alerts']} alert(s)")
    lines.append("")

    if summary["alerts"]:
        lines.append("Active Alerts:")
        for a in summary["alerts"]:
            icon = "🔴" if a["severity"] == "critical" else "🟡"
            ack = " [ACK]" if a.get("acknowledged") else ""
            lines.append(f"  {icon} [{a['severity'].upper()}] {a['check_name']}{ack}")
            lines.append(f"     {a['detail']}")
        lines.append("")

    lines.append("All Check Results:")
    for r in summary.get("check_results", []):
        icon = "[OK]  " if r["passed"] else "[FAIL]"
        lines.append(f"  {icon} {r['name']} ({r['category']})")
        lines.append(f"         {r['detail']}")

    return "\n".join(lines)
