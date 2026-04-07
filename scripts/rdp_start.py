#!/usr/bin/env python3
"""Research Data Platform — unified launcher.

⚠️ DEPRECATION NOTICE (2026-04-07)
─────────────────────────────────────────────────────────────────────
本启动器的 daemon 常驻模式已被废弃。原因:
  - RDP 所有消费方都是 daily/weekly cadence (data_maintenance / governance_cycle
    / research_cycle / decision_cycle), 不需要 intra-minute 新鲜度。
  - 实盘交易引擎自有 OKX websocket 直连, 不消费 RDP 数据。
  - 60s tick 每天产生 ~7800 次 OKX REST 调用 (4 symbol × 5 tf), 99% 浪费。

✅ 推荐用法 (Step 1+3 迁移路径):
  改用 cron / Task Scheduler 每天调用一次 data_maintenance workflow:

  # Linux crontab
  0 4 * * * python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance

  # Windows Task Scheduler
  schtasks /create /tn "RDP_DataMaintenance" \\
      /tr "python scripts/rdp_run_scheduled_workflow.py --workflow data_maintenance" \\
      /sc daily /st 04:00

historical daemon 同样推荐改为手工拖完 ZIP 后调用 --once:
  python scripts/rdp_historical_daemon.py --once

详见: docs/operations/rdp_scheduling_strategy.md
─────────────────────────────────────────────────────────────────────

历史功能 (保留以兼容旧代码路径):
  1. 历史数据聚合 daemon  (扫描 incoming/, 消费 ZIP)
  2. 实时数据聚合 daemon  (滚动采集 + Gold + Gap)

两个 daemon 各跑在独立线程中，共享同一进程。
Ctrl+C 优雅退出。

Usage (legacy, deprecated):
    # ⚠️ 不再推荐: 启动全部 (会打印 deprecation 警告)
    python scripts/rdp_start.py

    # 只启动历史 daemon
    python scripts/rdp_start.py --historical-only

    # 只启动实时 daemon
    python scripts/rdp_start.py --realtime-only

    # 自定义间隔
    python scripts/rdp_start.py --historical-interval 60 --realtime-interval 30
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdp_start")

# 全局停止信号
_stop_event = threading.Event()


def _signal_handler(signum: int, frame: object) -> None:
    log.info("Received signal %d, shutting down...", signum)
    _stop_event.set()


def _run_historical_thread(interval: int) -> None:
    """历史 daemon 线程入口。"""
    from pathlib import Path

    from aats.data_platform.config import get_settings

    settings = get_settings()
    incoming = Path(settings.historical_incoming_dir)
    completed = Path(settings.historical_completed_dir)
    failed = Path(settings.historical_failed_dir)

    for d in (incoming, completed, failed):
        d.mkdir(parents=True, exist_ok=True)

    log.info("[Historical] Thread started, scanning %s every %ds", incoming, interval)

    # 导入 scan_and_consume_once — 通过 importlib 加载脚本模块
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "rdp_historical_daemon",
        str(Path(__file__).resolve().parent / "rdp_historical_daemon.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    scan_and_consume_once = _mod.scan_and_consume_once

    while not _stop_event.is_set():
        try:
            stats = scan_and_consume_once(incoming, completed, failed)
            if stats["discovered"] > 0:
                log.info(
                    "[Historical] discovered=%d, ok=%d, fail=%d, skip=%d",
                    stats["discovered"], stats["succeeded"],
                    stats["failed"], stats["skipped"],
                )
        except Exception:
            log.exception("[Historical] Scan cycle error")

        _stop_event.wait(timeout=interval)

    log.info("[Historical] Thread stopped.")


def _run_realtime_thread(interval: int) -> None:
    """实时 daemon 线程入口。"""
    import importlib.util
    from pathlib import Path

    from aats.data_platform.config import get_settings

    # 导入 run_one_cycle �� 通过 importlib 加载脚本模块
    _spec = importlib.util.spec_from_file_location(
        "rdp_realtime_daemon",
        str(Path(__file__).resolve().parent / "rdp_realtime_daemon.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    run_one_cycle = _mod.run_one_cycle

    settings = get_settings()
    cycle_count = 0

    log.info("[Realtime] Thread started, interval=%ds", interval)

    while not _stop_event.is_set():
        try:
            run_one_cycle(settings, cycle_count=cycle_count)
        except Exception:
            log.exception("[Realtime] Cycle error")

        cycle_count += 1
        _stop_event.wait(timeout=interval)

    log.info("[Realtime] Thread stopped after %d cycles.", cycle_count)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Research Data Platform — unified launcher",
    )
    parser.add_argument("--historical-only", action="store_true",
                        help="Only start the historical daemon")
    parser.add_argument("--realtime-only", action="store_true",
                        help="Only start the realtime daemon")
    parser.add_argument("--historical-interval", type=int, default=None,
                        help="Historical scan interval (seconds)")
    parser.add_argument("--realtime-interval", type=int, default=60,
                        help="Realtime cycle interval (seconds)")
    args = parser.parse_args()

    # 初始化
    from aats.data_platform.config import get_settings
    from aats.data_platform.db import run_migrations

    settings = get_settings()
    run_migrations(settings)

    hist_interval = args.historical_interval or settings.historical_scan_interval_seconds
    rt_interval = args.realtime_interval

    # 注册信号
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    run_hist = not args.realtime_only
    run_rt = not args.historical_only

    threads: list[threading.Thread] = []

    print("=" * 60)
    print("  Research Data Platform — Unified Launcher")
    print("=" * 60)
    print()
    print("  ⚠️  DEPRECATION WARNING")
    print("  ─────────────────────────────────────────────────────")
    print("  daemon 常驻 60s tick 模式已废弃 (2026-04-07)。")
    print("  推荐改为 cron 每天一次:")
    print("    0 4 * * * python scripts/rdp_run_scheduled_workflow.py \\")
    print("              --workflow data_maintenance")
    print("  详见: docs/operations/rdp_scheduling_strategy.md")
    print("  ─────────────────────────────────────────────────────")
    print()

    if run_hist:
        print(f"  [Historical] incoming scan every {hist_interval}s")
        t = threading.Thread(
            target=_run_historical_thread,
            args=(hist_interval,),
            name="historical-daemon",
            daemon=True,
        )
        threads.append(t)

    if run_rt:
        print(f"  [Realtime]   rolling cycle every {rt_interval}s")
        t = threading.Thread(
            target=_run_realtime_thread,
            args=(rt_interval,),
            name="realtime-daemon",
            daemon=True,
        )
        threads.append(t)

    if not threads:
        print("  Nothing to start (both --historical-only and --realtime-only?)")
        return

    print("=" * 60)
    print("  Press Ctrl+C to stop")
    print("=" * 60)

    for t in threads:
        t.start()

    # 主线程等待停止信号
    try:
        while not _stop_event.is_set():
            _stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received")
        _stop_event.set()

    # 等待线程退出
    for t in threads:
        t.join(timeout=10)

    print("\nResearch Data Platform stopped.")


if __name__ == "__main__":
    main()
