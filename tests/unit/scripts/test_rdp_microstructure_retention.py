"""Narrow unit tests for scripts/rdp_microstructure_retention.py.

锁定:
    - --apply 缺 --confirm 走保护层 (exit 4)
    - 默认 (无 --apply/--dry-run) 走 dry-run (exit 2)
    - RETENTION_PLAN 严格 = 设计文档策略 (30/14/14/7)
    - dry-run 对 4 张表各调用一次 COUNT 并产出 summary
    - apply 对 4 张表各调用一次 DELETE, 汇总 deleted rows
    - data_maintenance.json 里有对应 task, command 指向脚本且带 --apply --confirm

不触真实 Postgres: session/execute 全部 monkeypatch 成 fake。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "rdp_microstructure_retention.py"
_WORKFLOW_CONFIG = (
    _ROOT / "configs" / "rdp_workflows" / "data_maintenance.json"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "rdp_microstructure_retention", _SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 在 Python 3.14 下会从 sys.modules[cls.__module__] 取字典做
    # 字符串注解解析, 先注册再 exec_module 避免 AttributeError。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeResult:
    def __init__(self, *, n: int = 0, rowcount: int = 0):
        self._n = n
        self.rowcount = rowcount

    def fetchone(self):
        class _Row:
            def __init__(self, n):
                self.n = n

            def __getitem__(self, idx):
                if idx == 0:
                    return self.n
                raise IndexError(idx)
        return _Row(self._n)


class _FakeSession:
    """记录所有 execute() 调用的 fake, 不真连 DB."""

    def __init__(self, *, count_per_table: int = 3, rowcount_per_delete: int = 7):
        self.executions: list[tuple[str, dict]] = []
        self._count = count_per_table
        self._rowcount = rowcount_per_delete

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executions.append((sql, params or {}))
        if sql.lstrip().upper().startswith("SELECT"):
            return _FakeResult(n=self._count)
        if sql.lstrip().upper().startswith("DELETE"):
            return _FakeResult(rowcount=self._rowcount)
        return _FakeResult()


def _session_factory_stub(fake: _FakeSession):
    @contextmanager
    def _factory():
        yield fake

    return _factory


class TestRetentionPlan(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()

    def test_retention_plan_strict_mapping(self) -> None:
        self.assertEqual(
            self.mod.RETENTION_PLAN,
            {
                "bronze.market_trades": 30,
                "bronze.market_orderbook_bbo": 14,
                "bronze.market_orderbook_books5": 14,
                "staging.market_oi_funding_ticks": 7,
            },
        )


class TestArgProtection(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()

    def test_apply_without_confirm_rejected_with_exit_4(self) -> None:
        rc = self.mod.main(["--apply"])
        self.assertEqual(rc, 4)

    def test_default_falls_through_to_dry_run(self) -> None:
        """没传 --apply/--dry-run 应走 dry-run (exit 2), 不应误入 apply。"""
        fake = _FakeSession(count_per_table=5)
        with mock.patch.object(
            self.mod,
            "_build_session_factory",
            return_value=_session_factory_stub(fake),
        ):
            rc = self.mod.main([])
        self.assertEqual(rc, 2)
        # 只 SELECT 不 DELETE
        for sql, _ in fake.executions:
            self.assertIn("SELECT", sql.upper())
            self.assertNotIn("DELETE", sql.upper())

    def test_explicit_dry_run_exits_2(self) -> None:
        fake = _FakeSession()
        with mock.patch.object(
            self.mod,
            "_build_session_factory",
            return_value=_session_factory_stub(fake),
        ):
            rc = self.mod.main(["--dry-run"])
        self.assertEqual(rc, 2)


class TestDryRunPath(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()

    def test_dry_run_queries_each_table_once(self) -> None:
        fake = _FakeSession(count_per_table=11)
        with mock.patch.object(
            self.mod,
            "_build_session_factory",
            return_value=_session_factory_stub(fake),
        ):
            summary = self.mod._run_dry_run(
                _session_factory_stub(fake),
                datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(summary.mode, "dry_run")
        self.assertEqual(len(summary.tables), 4)
        tables_counted = {t.table for t in summary.tables}
        self.assertEqual(tables_counted, set(self.mod.RETENTION_PLAN.keys()))
        for t in summary.tables:
            self.assertEqual(t.mode, "dry_run")
            self.assertEqual(t.row_count, 11)
            self.assertIsNone(t.error)
        # 对 4 张表各 SELECT 一次
        selects = [e for e in fake.executions if "SELECT" in e[0].upper()]
        self.assertEqual(len(selects), 4)


class TestApplyPath(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()

    def test_apply_confirm_runs_delete_and_exits_0(self) -> None:
        fake = _FakeSession(rowcount_per_delete=42)
        with mock.patch.object(
            self.mod,
            "_build_session_factory",
            return_value=_session_factory_stub(fake),
        ):
            rc = self.mod.main(["--apply", "--confirm"])
        self.assertEqual(rc, 0)
        deletes = [e for e in fake.executions if "DELETE" in e[0].upper()]
        self.assertEqual(len(deletes), 4)
        # 每条 DELETE 应针对 retention plan 中的一张表
        for table in self.mod.RETENTION_PLAN:
            self.assertTrue(
                any(table in sql for sql, _ in deletes),
                f"expected DELETE for {table}, got {deletes!r}",
            )


class TestWorkflowConfigWired(unittest.TestCase):
    def test_data_maintenance_contains_retention_task(self) -> None:
        raw = _WORKFLOW_CONFIG.read_text(encoding="utf-8")
        config = json.loads(raw)
        self.assertEqual(config["workflow"], "data_maintenance")
        tasks = config["tasks"]
        retention = [t for t in tasks if t["name"] == "microstructure_retention"]
        self.assertEqual(
            len(retention), 1,
            "exactly one microstructure_retention task expected",
        )
        task = retention[0]
        self.assertIn(
            "scripts/rdp_microstructure_retention.py", task["command"],
        )
        self.assertIn("--apply", task["command"])
        self.assertIn("--confirm", task["command"])
        self.assertTrue(task.get("allow_failure") is True)
        self.assertTrue(task.get("enabled") is True)


if __name__ == "__main__":
    sys.exit(unittest.main())
