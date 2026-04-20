"""db_create_task_if_idle 单元回归：关闭 has_active_task → create_task 的 TOCTOU.

旧路径 = 两条 SQL：
  1. SELECT ... WHERE workflow=? AND status IN ('pending','running')
  2. INSERT INTO rdp_task_queue ...

API handler 与 scheduler 在高并发下可能同时通过 step 1，再双双 INSERT；第二
次 INSERT 会撞上 ``ix_rdp_task_one_active_per_workflow`` (partial unique on
workflow WHERE status IN ('pending','running')) 抛 IntegrityError，被上层
except Exception 抹平成 "创建任务失败" 的误导错误。

新路径把判断+插入收敛到 INSERT ... ON CONFLICT DO NOTHING RETURNING，用
partial unique index 的冲突语义直接吸收 race：
  * 抢到索引 → RETURNING 返回 task_id → (task_id, None).
  * 冲突 → RETURNING 为空 → 回查现有 active task → (None, existing_dict).

本测试用 FakeSession 锁定 SQL 调用形状和返回契约，不走 testcontainers。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from aats.data_platform.governance.rdp_task_db import (
    VALID_WORKFLOWS,
    db_create_task_if_idle,
)


class _FakeRow:
    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            setattr(self, key, value)


class _FakeResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def fetchone(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """仅覆盖 db_create_task_if_idle 触达的三条 SQL 形态：
      * INSERT INTO governance.rdp_task_queue ... ON CONFLICT (workflow) WHERE ...
      * SELECT task_id, status ... FROM rdp_task_queue WHERE workflow = ... AND status IN (...)
    """

    def __init__(
        self,
        *,
        insert_succeeds: bool = True,
        existing_active: dict[str, Any] | None = None,
    ) -> None:
        self.insert_succeeds = insert_succeeds
        self.existing_active = existing_active
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement).strip()
        self.statements.append((sql, dict(params or {})))

        if sql.startswith("INSERT INTO governance.rdp_task_queue"):
            # 契约校验：SQL 里必须出现 ON CONFLICT ... DO NOTHING 和 RETURNING，
            # 否则就回到了 has_active_task → insert 的旧 TOCTOU 路径。
            assert "ON CONFLICT" in sql, (
                "db_create_task_if_idle 的 INSERT 必须带 ON CONFLICT；"
                "否则并发写会退化成 IntegrityError 路径"
            )
            assert "DO NOTHING" in sql, (
                "ON CONFLICT 分支必须是 DO NOTHING；DO UPDATE 会重写已有活跃任务"
            )
            assert "RETURNING task_id" in sql, (
                "需要 RETURNING task_id 用作 "
                "\"成功/已有冲突\" 的唯一区分信号"
            )
            # 必须以 partial unique index 的谓词匹配
            assert re.search(
                r"ON CONFLICT \(workflow\) WHERE status IN \('pending', 'running'\)",
                sql,
            ), (
                "ON CONFLICT 谓词必须与 ix_rdp_task_one_active_per_workflow "
                "的 WHERE 精确匹配，否则 PostgreSQL 无法选中该 partial index"
            )

            if self.insert_succeeds:
                return _FakeResult([_FakeRow({"task_id": (params or {}).get("task_id")})])
            # ON CONFLICT DO NOTHING → 不返回行
            return _FakeResult([])

        if sql.startswith("SELECT task_id, status"):
            # db_has_active_task 的查询
            if self.existing_active is not None:
                return _FakeResult([_FakeRow(self.existing_active)])
            return _FakeResult([])

        raise AssertionError(f"Unexpected SQL: {sql[:100]}...")


# =====================================================================
# Happy path
# =====================================================================


def test_insert_succeeds_returns_task_id_and_no_existing() -> None:
    session = _FakeSession(insert_succeeds=True)

    task_id, existing = db_create_task_if_idle(session, workflow="research_cycle")

    assert task_id is not None, "insert 成功必须返回非空 task_id"
    assert task_id.startswith("task_"), "task_id 必须保留 task_<hex12> 前缀契约"
    assert existing is None, "insert 成功不应回查 existing"
    # 仅发一条 INSERT，不回查
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["INSERT"], (
        f"成功路径只应发一条 INSERT，实际: {sql_types}"
    )


# =====================================================================
# Conflict path — ON CONFLICT DO NOTHING 命中 existing active
# =====================================================================


def test_conflict_returns_none_task_id_and_queries_existing() -> None:
    """INSERT 被 partial unique index 拦下 → 回查 existing 并返回给 caller."""
    existing = {
        "task_id": "task_existing_abc",
        "status": "running",
        "requested_at": None,
        "started_at": None,
    }
    session = _FakeSession(insert_succeeds=False, existing_active=existing)

    task_id, returned_existing = db_create_task_if_idle(
        session, workflow="research_cycle",
    )

    assert task_id is None, "冲突路径 task_id 必须为 None"
    assert returned_existing is not None
    assert returned_existing["task_id"] == "task_existing_abc"
    assert returned_existing["status"] == "running"

    # 契约：INSERT → 冲突 → SELECT 回查，共两条 SQL
    sql_types = [sql.split()[0] for sql, _ in session.statements]
    assert sql_types == ["INSERT", "SELECT"], (
        f"冲突路径：一条 INSERT + 一条回查 SELECT，实际: {sql_types}"
    )


def test_conflict_without_existing_row_returns_none_existing() -> None:
    """极罕见 race：INSERT 冲突但回查时刚好那行又被清掉 → existing=None，
    caller 需自行兜底（UI 文案已有 fallback）。
    """
    session = _FakeSession(insert_succeeds=False, existing_active=None)

    task_id, existing = db_create_task_if_idle(session, workflow="release_cycle")

    assert task_id is None
    assert existing is None


# =====================================================================
# Invalid workflow rejection
# =====================================================================


def test_invalid_workflow_raises_before_touching_db() -> None:
    """非合法 workflow 必须立刻 raise，不能先插再回滚（浪费 WAL）。"""
    session = _FakeSession(insert_succeeds=True)

    with pytest.raises(ValueError, match="Invalid workflow"):
        db_create_task_if_idle(session, workflow="not_a_workflow")

    assert session.statements == [], (
        "workflow 校验失败时不应触达 DB"
    )


def test_all_valid_workflows_accepted() -> None:
    """契约：VALID_WORKFLOWS 里的每个值都应能被接受。

    若后续 VALID_WORKFLOWS 删除项要同步更新 migration / scheduler tests。
    """
    for wf in VALID_WORKFLOWS:
        session = _FakeSession(insert_succeeds=True)
        task_id, _ = db_create_task_if_idle(session, workflow=wf)
        assert task_id is not None, f"workflow={wf} 应插入成功"


# =====================================================================
# 参数绑定契约
# =====================================================================


def test_requested_by_is_bound_into_insert_params() -> None:
    session = _FakeSession(insert_succeeds=True)

    db_create_task_if_idle(
        session, workflow="decision_cycle", requested_by="scheduler_daemon",
    )

    insert_sql, params = session.statements[0]
    assert insert_sql.startswith("INSERT INTO governance.rdp_task_queue")
    assert params["requested_by"] == "scheduler_daemon"
    assert params["workflow"] == "decision_cycle"


# =====================================================================
# R3 Bug 6 retry: earliest_start_at 延迟入队
# =====================================================================


def test_earliest_start_at_defaults_to_now_when_not_specified() -> None:
    """未显式指定 earliest_start_at 时，参数 eligible_at 默认 = now()。

    契约：scheduler 正常入队不传参，行为保持与之前一致（立即可领）。
    """
    from datetime import datetime, timezone

    session = _FakeSession(insert_succeeds=True)
    before = datetime.now(timezone.utc)
    db_create_task_if_idle(session, workflow="release_cycle")
    after = datetime.now(timezone.utc)

    _, params = session.statements[0]
    assert "eligible_at" in params, "SQL 必须绑定 eligible_at 参数"
    # eligible_at 必须落在 [before, after] 窗口内（= now())
    assert before <= params["eligible_at"] <= after, (
        f"默认 eligible_at 应 = now()，实际 {params['eligible_at']}"
    )


def test_earliest_start_at_honors_explicit_future_timestamp() -> None:
    """R3 auto_retry 路径: 显式传 earliest_start_at=now()+15min 要绑定到 SQL。"""
    from datetime import datetime, timedelta, timezone

    session = _FakeSession(insert_succeeds=True)
    retry_eligible = datetime.now(timezone.utc) + timedelta(minutes=15)

    db_create_task_if_idle(
        session, workflow="observation_cycle",
        requested_by="auto_retry_of_task_abc",
        earliest_start_at=retry_eligible,
    )

    insert_sql, params = session.statements[0]
    assert "earliest_start_at" in insert_sql, (
        "INSERT SQL 必须包含 earliest_start_at 列才能让 claim 延迟生效"
    )
    assert params["eligible_at"] == retry_eligible
    assert params["requested_by"].startswith("auto_retry_of_"), (
        "requested_by 前缀 auto_retry_of_ 供 daemon 防循环判定"
    )


def test_claim_sql_filters_by_earliest_start_at() -> None:
    """db_claim_next_task 的 SELECT 必须过滤 earliest_start_at <= now()。

    契约：没有这个过滤，延迟入队的 retry task 会被立刻 claim，15min 窗口失效。
    """
    import re

    from aats.data_platform.governance import rdp_task_db as mod
    import inspect

    src = inspect.getsource(mod.db_claim_next_task)
    # SQL 里必须有 earliest_start_at <= now() 条件
    assert re.search(r"earliest_start_at\s*<=\s*now\(\)", src), (
        "db_claim_next_task SQL 必须过滤 earliest_start_at <= now() "
        "才能让 R3 retry 延迟生效"
    )


# =====================================================================
# VALID_WORKFLOWS 与 configs/rdp_workflows/*.json 的双向契约
# 2026-04-20 P0-c deploy 发现: 新增 candles_rolling_15m JSON 配置但忘了加到
# VALID_WORKFLOWS, 导致 scheduler 每 10s ERROR 一次 "Invalid workflow"。
# 本测试防止再次漏配: 任何有 JSON 配置的 workflow, 必须在 VALID_WORKFLOWS 里。
# =====================================================================


def test_valid_workflows_covers_all_json_configs() -> None:
    """configs/rdp_workflows/ 下每个 .json 对应的 workflow 名, 必须在 VALID_WORKFLOWS.

    双向同步契约:
      - 加新 workflow: JSON + VALID_WORKFLOWS + WORKFLOW_TIMEOUTS (后者非本测试范围)
      - 删 workflow: 同步删 JSON + VALID_WORKFLOWS
    若本测试失败, 说明某个新增 workflow 配置没同步到 task DB 白名单,
    deploy 后 scheduler 会持续 ERROR 无法 enqueue 该 workflow 的 task。
    """
    import json
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    workflows_dir = project_root / "configs" / "rdp_workflows"
    assert workflows_dir.is_dir(), f"workflows dir missing: {workflows_dir}"

    json_workflow_names = set()
    for path in sorted(workflows_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        wf_name = data.get("workflow")
        assert wf_name, f"{path.name} 缺少 'workflow' 字段"
        json_workflow_names.add(wf_name)

    missing = json_workflow_names - VALID_WORKFLOWS
    assert not missing, (
        f"以下 workflow 有 JSON 配置但不在 VALID_WORKFLOWS: {sorted(missing)}; "
        f"scheduler 会每 tick ERROR 'Invalid workflow'. "
        f"修复: 在 aats/data_platform/governance/rdp_task_db.py::VALID_WORKFLOWS 加入。"
    )

    orphan = VALID_WORKFLOWS - json_workflow_names
    assert not orphan, (
        f"以下 workflow 在 VALID_WORKFLOWS 但无 JSON 配置: {sorted(orphan)}; "
        f"可能是 workflow 被删但 VALID_WORKFLOWS 忘同步。"
    )


# =====================================================================
# meta.ingest_runs.chk_ir_type 与 create_ingest_run 调用点的双向契约
# 2026-04-20 code review 发现: 4907af1 已为 workflow 名加契约, 但 run_type
# 的 chk_ir_type CHECK constraint ({backfill, rolling, gap_repair, gold_build})
# 没有类似契约. P0-a catchup 脚本当时用了 run_type='catchup' 就是被 chk_ir_type
# 直接拒, 部署时才暴露. 本测试扫所有 create_ingest_run 调用点的 run_type
# 字面量, 确保都在白名单内.
# =====================================================================

_IR_TYPE_WHITELIST = frozenset({
    "backfill",
    "rolling",
    "gap_repair",
    "gold_build",
})


def test_create_ingest_run_call_sites_use_whitelisted_run_type() -> None:
    """aats/data_platform/ + scripts/ 下 create_ingest_run 的 run_type 必须在白名单.

    对偶: aats/data_platform/migrations/batch_b_01_core_schema.sql 里
    `chk_ir_type` 允许 {backfill, rolling, gap_repair, gold_build}. 若 code
    path 用了其他字面量, DB INSERT 会直接 CheckViolation, 失败只在 deploy
    才暴露 (本次 P0-a catchup 就是这个 bug).

    扫描策略: grep 所有 `run_type=` 字面量赋值, 过滤掉 keyword-arg 定义
    (如 `def create_ingest_run(... run_type=...)`) 和 test fixture.
    """
    import re
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]

    # 扫描范围: 生产 code 路径 (不扫 test, test 可以有 mock 值)
    search_dirs = [
        project_root / "aats" / "data_platform",
        project_root / "scripts",
    ]

    # 匹配 `run_type="..."` 或 `run_type='...'` (关键字参数字面量调用)
    pattern = re.compile(r'''run_type\s*=\s*["']([a-z_]+)["']''')

    violations: list[str] = []
    for root in search_dirs:
        for py_file in root.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in pattern.finditer(content):
                value = m.group(1)
                if value not in _IR_TYPE_WHITELIST:
                    # 定位到文件行号
                    line_num = content[: m.start()].count("\n") + 1
                    rel = py_file.relative_to(project_root)
                    violations.append(
                        f"  {rel}:{line_num} run_type={value!r} "
                        f"(不在 chk_ir_type 白名单 {sorted(_IR_TYPE_WHITELIST)})"
                    )

    assert not violations, (
        "以下 create_ingest_run 调用点 run_type 不在 meta.ingest_runs.chk_ir_type "
        "CHECK 约束白名单内, deploy 时会 CheckViolation:\n"
        + "\n".join(violations)
        + "\n\n修复: 改字面量到 whitelist 之一, 或扩展 chk_ir_type 约束 "
        "(aats/data_platform/migrations/batch_b_01_core_schema.sql)。"
    )


def test_workflow_timeouts_covers_all_json_configs() -> None:
    """WORKFLOW_TIMEOUTS (scripts/rdp_task_daemon.py) 必须覆盖 configs/rdp_workflows/*.json.

    2026-04-20 code review B-M1: 4907af1 commit 修 VALID_WORKFLOWS 漏配 (candles_rolling_15m)
    时, 自己注释承认 WORKFLOW_TIMEOUTS "(c) 暂靠人工 review". 本测试扫 daemon 源码里
    WORKFLOW_TIMEOUTS dict key, 必须与 JSON config workflow name 严格一致,
    否则 daemon 会用 DEFAULT_TIMEOUT (1800s) 掩盖"超时配置没给对"的真相.
    """
    import re
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    workflows_dir = project_root / "configs" / "rdp_workflows"
    daemon_py = project_root / "scripts" / "rdp_task_daemon.py"

    assert workflows_dir.is_dir()
    assert daemon_py.is_file()

    # workflow JSON name 集合
    import json
    json_workflows = set()
    for p in workflows_dir.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        json_workflows.add(data["workflow"])

    # 从 daemon py 抽 WORKFLOW_TIMEOUTS dict 的 key
    src = daemon_py.read_text(encoding="utf-8")
    m = re.search(r"WORKFLOW_TIMEOUTS\s*=\s*\{([^}]+)\}", src, flags=re.DOTALL)
    assert m, "WORKFLOW_TIMEOUTS dict 未找到, 若已迁到其他文件请更新本测试"

    # 抽所有 key (形如 "name": N)
    keys_in_dict = set(re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:\s*\d+', m.group(1)))

    missing = json_workflows - keys_in_dict
    assert not missing, (
        f"以下 workflow 有 JSON 配置但 WORKFLOW_TIMEOUTS 无对应 timeout: {sorted(missing)}; "
        f"daemon 会 fallback DEFAULT_TIMEOUT=1800s 掩盖问题, 显式列出 timeout."
    )

    orphan = keys_in_dict - json_workflows
    assert not orphan, (
        f"以下 workflow 在 WORKFLOW_TIMEOUTS 但无 JSON 配置: {sorted(orphan)}; "
        f"可能被删但 WORKFLOW_TIMEOUTS 忘同步, 留 dead entry."
    )


def test_ir_type_whitelist_matches_orm_check_constraint() -> None:
    """_IR_TYPE_WHITELIST 必须和 SQLAlchemy ORM CheckConstraint 中 chk_ir_type 一致.

    constraint 定义在 aats/data_platform/rdp_models.py (不在 SQL migration).
    防止未来有人改 ORM 但忘同步测试 (或反之).
    """
    import re
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    models_py = project_root / "aats" / "data_platform" / "rdp_models.py"
    assert models_py.is_file(), f"rdp_models.py 缺失: {models_py}"

    src = models_py.read_text(encoding="utf-8")
    # 匹配 CheckConstraint("run_type IN ('a','b',...)", name="chk_ir_type")
    m = re.search(
        r'''CheckConstraint\s*\(\s*["']run_type\s+IN\s*\(([^)]+)\)["']\s*,\s*name\s*=\s*["']chk_ir_type["']''',
        src,
    )
    assert m, (
        "无法从 rdp_models.py 找到 chk_ir_type CheckConstraint; "
        "若改了 constraint 形式 (如迁 SQL migration), 需更新本测试."
    )
    orm_values = frozenset(
        v.strip().strip("'\"") for v in m.group(1).split(",")
    )
    assert orm_values == _IR_TYPE_WHITELIST, (
        f"ORM chk_ir_type 白名单 = {sorted(orm_values)}, "
        f"测试 _IR_TYPE_WHITELIST = {sorted(_IR_TYPE_WHITELIST)}, "
        f"两者不一致, 同步修正."
    )
