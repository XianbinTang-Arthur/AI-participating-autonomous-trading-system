"""Regression guard: app.js must pass every field the view actually reads.

Background — the bug this guards against:

The commit a5218fb (feat: rebuild rdp workbench task flow) added new fields
`rdpWorkbenchOverview / rdpWorkbenchItems / rdpWorkbenchAlerts /
rdpTuningOverview / rdpTuningProposals` to ai-config-view.js and extended
store.js's viewSpec to fetch them into state.data. But the commit forgot to
update the `renderAIConfigView({...})` call site inside app.js, so all five
workbench/tuning panels arrived at the view as `undefined` → fell through to
`|| {}`, which made the view's `Object.keys(rdpWorkbenchOverview).length === 0`
guard fire unconditionally — the live dashboard showed only the placeholder
callout "RDP 数据暂未就绪" no matter what the backend returned.

The existing test_dashboard_ui suite never caught this because it calls
`renderAIConfigView(...)` directly with hand-rolled data; it never runs app.js.
This test fills that gap with a static check: for every `renderXxxView({…})`
call site in app.js, assert that the object literal passed in includes every
top-level key that the corresponding view module reads off `data`.

If the view later starts reading a new key, store.js must fetch it (fails in
test_dashboard_ui), and app.js must pass it through (fails here).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"
APP_JS = STATIC_DIR / "app.js"
VIEW_DIR = STATIC_DIR / "modules" / "views"


def _render_call_keys(app_source: str, render_fn: str) -> set[str]:
    """Extract the top-level keys passed to ``renderFnName({...})`` in app.js.

    Handles the shape ``renderFnName({ key1: ..., key2: ... })`` by scanning
    the argument block with brace-balance counting so nested object literals
    don't confuse the parser. Only the OUTERMOST keys are returned — exactly
    what the view's destructure reads from ``data``.
    """
    pattern = re.compile(re.escape(render_fn) + r"\s*\(")
    match = pattern.search(app_source)
    assert match, f"{render_fn} call not found in app.js"
    cursor = match.end()
    # Find the start of the object literal argument.
    while cursor < len(app_source) and app_source[cursor].isspace():
        cursor += 1
    assert app_source[cursor] == "{", f"{render_fn} first arg is not an object literal"
    start = cursor
    depth = 0
    end = -1
    for idx in range(start, len(app_source)):
        char = app_source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    assert end > start, f"unterminated object literal for {render_fn}"
    body = app_source[start + 1 : end]

    # Walk the body, collecting identifiers that sit at depth 0 and are
    # followed by ``:``. Depth tracks nested braces / brackets / parens so we
    # don't mistake a value's inner key for an outer key.
    keys: set[str] = set()
    depth = 0
    token = ""
    i = 0
    while i < len(body):
        char = body[i]
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif depth == 0 and char == ":":
            name = token.strip().strip("'").strip('"')
            name = name.split()[-1] if name else ""
            if re.fullmatch(r"\w+", name):
                keys.add(name)
            token = ""
        elif depth == 0 and char == ",":
            token = ""
        else:
            token += char
        i += 1
    return keys


def _view_reads_from_data(view_source: str) -> set[str]:
    """Return top-level keys that the view reads off ``data``.

    Scans for both ``const x = data.key`` / ``data.key ||`` forms and the
    destructuring form ``const { key1, key2 } = data``. Keeps it permissive —
    false positives are fine (the check only fails if app.js forgets a key the
    view is known to read).
    """
    keys: set[str] = set()
    for match in re.finditer(r"\bdata\.(\w+)", view_source):
        keys.add(match.group(1))
    # data?.key is common too
    for match in re.finditer(r"\bdata\?\.(\w+)", view_source):
        keys.add(match.group(1))
    # Destructuring: const { a, b, c } = data
    for match in re.finditer(
        r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*data\b", view_source, re.DOTALL
    ):
        body = match.group(1)
        for raw in body.split(","):
            cleaned = raw.strip().split(":")[0].split("=")[0].strip()
            if re.fullmatch(r"\w+", cleaned):
                keys.add(cleaned)
    return keys


class TestDashboardRenderWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.app_source = APP_JS.read_text(encoding="utf-8")

    def _check(self, render_fn: str, view_filename: str) -> None:
        view_source = (VIEW_DIR / view_filename).read_text(encoding="utf-8")
        passed_keys = _render_call_keys(self.app_source, render_fn)
        read_keys = _view_reads_from_data(view_source)
        missing = sorted(read_keys - passed_keys)
        self.assertFalse(
            missing,
            msg=(
                f"{view_filename} reads data.{missing!r} but app.js's "
                f"{render_fn}(...) call site does not include those keys. "
                f"The view will see them as undefined and fall through to "
                f"whatever placeholder it renders on empty data. Add the "
                f"missing keys to the {render_fn} call in app.js."
            ),
        )

    def test_ai_config_view_receives_all_keys_it_reads(self) -> None:
        """B2 regression: a5218fb added 5 rdp*Workbench*/rdp*Tuning* keys plus
        errors/authProviders to ai-config-view.js but forgot to update the
        call site. The live dashboard showed only the "RDP 数据暂未就绪"
        placeholder because all five panels arrived undefined."""
        self._check("renderAIConfigView", "ai-config-view.js")

    def test_all_other_render_views_pass_viewdata_by_reference(self) -> None:
        """M3 regression guard: 其它 render*View 调用要么传 ``viewData`` /
        ``state.data`` 整体（受 ``{...state.data}`` spread 保护），要么传
        ``viewData, state.ui.*`` 这种额外 context——无论哪种都保证 view 读任意
        ``data.key`` 都能拿到 state.data 里的同名字段。

        如果有人把别的 render*View 也改成 ``render*View({...})`` 对象字面量
        模式（像 AIConfigView 那样），就会重现 B2：新字段加到 view 但忘了
        call site → undefined → placeholder。本测试用静态规则拦住这种改法：
        任何新增的 object-literal 调用都必须显式加到上方的 ``_check``
        白名单，否则此测试失败，强制开发者扩展 per-view 的字段对齐检查。
        """
        render_calls = re.findall(
            r"\b(render(?:AI|Overview|Home|Strategy|Execution|Risk|ExitExecution|Replay|Admin|AIAnalysis|AIConfig)View)\s*\(([^)]{0,400})",
            self.app_source,
        )
        # 允许以下 render 用对象字面量（每个都有对应 _check 测试覆盖）
        object_literal_allowed = {"renderAIConfigView"}
        violators: list[tuple[str, str]] = []
        for render_fn, raw_first_arg in render_calls:
            first_arg = raw_first_arg.lstrip()
            if first_arg.startswith("{") and render_fn not in object_literal_allowed:
                violators.append((render_fn, first_arg[:60]))
        self.assertFalse(
            violators,
            msg=(
                f"发现未登记的 object-literal render 调用: {violators!r}。"
                f"任何新增的 render*View({{...}}) 调用都必须在本测试里加一条 "
                f"_check(...) 用例（类似 test_ai_config_view_receives_all_keys_it_reads），"
                f"验证 object literal 里的 key 覆盖 view 实际读取的 data.xxx，"
                f"否则会重现 B2 回归：view 新加字段但 call site 漏 pass → undefined。"
                f"确认已加测试后，把函数名加到 object_literal_allowed 集合。"
            ),
        )


class TestWorkbenchPhaseGate(unittest.TestCase):
    """M9: server-side 审批门禁的行为级测试。

    H5 修复前，``_build_workbench_alerts_payload`` 里如果 phase3 / phase4 的
    payload 回报 ``available=False`` 但没有 ``incomplete_reason`` 字段，就会
    静默 ``continue`` —— 结果是告警列表里看不到这一缺席，审批门禁（上游按
    ``blocks_approval=True`` 过滤）就认为没问题，能在零 evidence 的情况下放行
    审批。H5 改为：available=False 一律写告警，code 回退为 ``missing_round``。

    本测试直接调用修复后的函数，验证告警行为。
    """

    def test_phase3_missing_round_without_reason_still_blocks_approval(self) -> None:
        from unittest.mock import patch

        from aats.api import rdp_control_summary

        def fake_phase3(_root):
            # 模拟 DB/文件回报 "没有最新 round"，但没明确 incomplete_reason
            return {"available": False}

        def fake_phase4(_root):
            return {"available": True, "combos": []}

        with patch.object(rdp_control_summary, "query_latest_attribution", fake_phase3), \
             patch.object(rdp_control_summary, "query_latest_execution_realism", fake_phase4):
            payload = rdp_control_summary._build_workbench_alerts_payload(
                Path("."),
                summary={"health": {}},
            )
        phase3_alerts = [
            alert for alert in payload["integrity_alerts"]
            if alert.get("phase") == "phase3"
        ]
        self.assertTrue(
            phase3_alerts,
            "phase3 缺 round 且无 incomplete_reason 时必须仍写告警，否则审批门禁会绕过",
        )
        self.assertTrue(
            all(alert["blocks_approval"] for alert in phase3_alerts),
            "phase3 缺席告警的 blocks_approval 必须为 True",
        )
        self.assertEqual(
            phase3_alerts[0]["code"],
            "missing_round",
            "缺 reason 时 code 要统一为 missing_round，便于下游识别",
        )

    def test_phase4_query_failure_degrades_with_blocking_alert(self) -> None:
        """H6: 查询抛异常时不能让整个 bundle 500，必须降级 + 阻塞审批。"""
        from unittest.mock import patch

        from aats.api import rdp_control_summary

        def fake_phase3(_root):
            return {"available": True, "combos": []}

        def boom(_root):
            raise RuntimeError("simulated DB hiccup")

        with patch.object(rdp_control_summary, "query_latest_attribution", fake_phase3), \
             patch.object(rdp_control_summary, "query_latest_execution_realism", boom):
            payload = rdp_control_summary._build_workbench_alerts_payload(
                Path("."),
                summary={"health": {}},
            )
        phase4_alerts = [
            alert for alert in payload["integrity_alerts"]
            if alert.get("phase") == "phase4"
        ]
        self.assertTrue(phase4_alerts, "phase4 query 失败时仍要写告警")
        self.assertTrue(
            all(alert["blocks_approval"] for alert in phase4_alerts),
            "query_failed 告警必须阻塞审批",
        )

    def test_manifest_missing_severity_is_danger(self) -> None:
        """M8: manifest_missing_on_disk 语义等同 step2_manifest_missing，severity=danger。"""
        from unittest.mock import patch

        from aats.api import rdp_control_summary

        def fake_phase3(_root):
            return {"available": False, "incomplete_reason": "manifest_missing_on_disk"}

        def fake_phase4(_root):
            return {"available": True, "combos": []}

        with patch.object(rdp_control_summary, "query_latest_attribution", fake_phase3), \
             patch.object(rdp_control_summary, "query_latest_execution_realism", fake_phase4):
            payload = rdp_control_summary._build_workbench_alerts_payload(
                Path("."),
                summary={"health": {}},
            )
        phase3_alerts = [
            alert for alert in payload["integrity_alerts"]
            if alert.get("phase") == "phase3" and alert.get("code") == "manifest_missing_on_disk"
        ]
        self.assertTrue(phase3_alerts, "manifest_missing_on_disk 必须写告警")
        self.assertEqual(
            phase3_alerts[0]["severity"],
            "danger",
            "manifest_missing_on_disk = 清单从磁盘消失，必须是 danger 级，"
            "historic warning 语义与 blocks_approval=True 矛盾",
        )

    def test_same_operational_blocker_title_is_rendered_once(self) -> None:
        from unittest.mock import patch

        from aats.api import rdp_control_summary

        summary = {
            "health": {
                "checks": [
                    {
                        "category": "database",
                        "name": "readonly_access",
                        "status": "warn",
                        "detail": "read only",
                    }
                ],
                "blocking_reasons": ["live_db_unhealthy"],
                "warnings": [],
            }
        }
        with (
            patch(
                "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
                return_value=None,
            ),
            patch(
                "aats.data_platform.governance.snapshot_db.is_snapshot_incomplete",
                return_value=False,
            ),
        ):
            payload = rdp_control_summary._build_workbench_alerts_payload(
                Path("."),
                summary,
                phase_payloads={
                    "phase3": {"available": True},
                    "phase4": {"available": True},
                },
            )

        matching = [
            alert
            for alert in payload["operational_alerts"]
            if alert["title"] == "生产数据库连接"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["severity"], "danger")


class TestStep2IntegrityGuard(unittest.TestCase):
    """H2 regression guard: Step2 guard 异常路径不能泄漏 str(exc) 到用户响应。"""

    def test_lookup_failure_returns_fixed_message_without_exc_detail(self) -> None:
        from unittest.mock import patch

        from aats.data_platform.governance import step2_integrity_guard

        class _Secret(Exception):
            def __str__(self) -> str:
                return "postgres://admin:SECRET@db/aats"

        def boom(*_args, **_kwargs):
            raise _Secret()

        with patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            boom,
        ):
            reason = step2_integrity_guard.step2_integrity_blocking_reason(Path("."))
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertNotIn("SECRET", reason)
        self.assertNotIn("postgres://", reason)
        self.assertIn("fail-closed", reason)

    def test_missing_snapshot_returns_blocking_reason(self) -> None:
        """H-A1 回归：快照为 None（fresh deploy / 迁库丢数据）必须 fail-closed。

        历史 bug：``is_snapshot_incomplete(None)`` 按契约返回 False（该 helper 被
        evidence_bundle / rollback / observation 多处共用，None→False 是共享契约），
        guard 的条件 ``if is_snapshot_incomplete(snapshot):`` 对 None 不触发，结果
        approve/supersede/tuning_approve 在没有任何 Step2 证据的环境里直接放行——
        这是门禁最该锁死的时刻。修复后 guard 在走 is_snapshot_incomplete 之前先
        独立判断 None 并返回明确的阻塞原因。
        """
        from unittest.mock import patch

        from aats.data_platform.governance import step2_integrity_guard

        with patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value=None,
        ):
            reason = step2_integrity_guard.step2_integrity_blocking_reason(Path("."))
        self.assertIsNotNone(reason, "Step2 快照不存在时 guard 必须返回阻塞原因")
        assert reason is not None
        self.assertIn("fail-closed", reason)
        self.assertIn("不存在", reason)

    def test_complete_snapshot_returns_none(self) -> None:
        """健康路径：有完整快照（不是 file_incomplete 也没 manifest_synthesized）
        guard 必须返回 None 允许写入操作继续。"""
        from unittest.mock import patch

        from aats.data_platform.governance import step2_integrity_guard

        healthy_snapshot = {
            "round_id": "20260416_120000_abcd1234",
            "data_source": "db",
            "status": "succeeded",
            "finished_at": "2026-04-16T12:10:00+00:00",
            "manifest": {"round_id": "20260416_120000_abcd1234"},
        }
        with patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value=healthy_snapshot,
        ):
            reason = step2_integrity_guard.step2_integrity_blocking_reason(Path("."))
        self.assertIsNone(reason, "健康快照不应触发 guard 阻塞")

    def test_failed_snapshot_returns_blocking_reason(self) -> None:
        """有 manifest 不等于成功；failed/partial Step2 均不得批准。"""
        from unittest.mock import patch

        from aats.data_platform.governance import step2_integrity_guard

        with patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value={
                "round_id": "20260416_120000_deadbeef",
                "data_source": "db",
                "status": "failed",
                "manifest": {},
            },
        ):
            assessment = step2_integrity_guard.assess_step2_integrity(Path("."))

        self.assertFalse(assessment["ok"])
        self.assertEqual(assessment["code"], "snapshot_status_invalid")

    def test_incomplete_snapshot_returns_reason(self) -> None:
        """file_incomplete 快照（round 目录缺 round_manifest.json）应被 guard 拒掉。"""
        from unittest.mock import patch

        from aats.data_platform.governance import step2_integrity_guard

        incomplete_snapshot = {
            "round_id": "20260416_120000_beefdead",
            "data_source": "file_incomplete",
        }
        with patch(
            "aats.data_platform.governance.snapshot_db.load_latest_research_round_snapshot",
            return_value=incomplete_snapshot,
        ):
            reason = step2_integrity_guard.step2_integrity_blocking_reason(Path("."))
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("不完整", reason)


if __name__ == "__main__":
    unittest.main()
