"""_emit_subprocess_output_to_parent_stdout 单测.

锁定 2026-04-22 诊断: rdp_task_daemon 子进程 stdout 必须透传父进程 stdout,
否则 Promtail/Loki 看不到 silver_microstructure_etl 等子进程事件,
dashboard 面板和 sev3-micro-silver-etl-slow 告警永远 no-data.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "rdp_task_daemon.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("rdp_task_daemon", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    spec.loader.exec_module(mod)
    return mod


def test_emit_empty_input_is_noop(capsys):
    mod = _load_module()
    mod._emit_subprocess_output_to_parent_stdout("microstructure_silver_15m", "")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_emit_whitespace_only_is_noop(capsys):
    mod = _load_module()
    mod._emit_subprocess_output_to_parent_stdout("x", "\n\n\n")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_emit_passes_through_each_nonempty_line(capsys):
    mod = _load_module()
    combined = (
        "2026-04-23 02:15:08,889 INFO merger: COMMITTED silver_microstructure_etl duration=0.265s\n"
        "\n"
        "2026-04-23 02:15:08,894 INFO rdp_microstructure_silver: bar_start=2026-04-22T18:00:00+00:00\n"
    )
    mod._emit_subprocess_output_to_parent_stdout("microstructure_silver_15m", combined)
    captured = capsys.readouterr()
    out_lines = captured.out.strip().splitlines()
    assert len(out_lines) == 2
    assert "silver_microstructure_etl" in out_lines[0]
    assert "duration=0.265s" in out_lines[0]
    assert "bar_start=2026-04-22T18:00:00+00:00" in out_lines[1]


def test_emit_logs_boundary_markers_with_line_count(caplog):
    mod = _load_module()
    combined = "line1\nline2\n"
    with caplog.at_level(logging.INFO, logger="rdp_task_daemon"):
        mod._emit_subprocess_output_to_parent_stdout("wf_x", combined)
    msgs = [rec.getMessage() for rec in caplog.records]
    begin_msgs = [m for m in msgs if "stdout begin" in m and "wf_x" in m and "2 lines" in m]
    end_msgs = [m for m in msgs if "stdout end" in m and "wf_x" in m]
    assert len(begin_msgs) == 1
    assert len(end_msgs) == 1


def test_emit_preserves_duration_regex_match(capsys):
    """验证 promtail/Loki regex duration=(?P<duration>[0-9.]+)s 仍能匹配."""
    import re

    mod = _load_module()
    payload = "COMMITTED silver_microstructure_etl symbol=BTC-USDT-SWAP duration=1.234s error=None"
    mod._emit_subprocess_output_to_parent_stdout("microstructure_silver_15m", payload + "\n")
    captured = capsys.readouterr()
    m = re.search(r"duration=(?P<duration>[0-9.]+)s", captured.out)
    assert m is not None
    assert m.group("duration") == "1.234"
