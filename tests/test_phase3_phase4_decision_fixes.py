"""Phase 3 / Phase 4 / Phase 6 (Decision) P0 修复验证测试.

覆盖审查中发现的所有 P0 问题修复:
  P0-1: run_dir 相对路径 → 绝对路径（Phase 3/4 manifest + evidence_bundle）
  P0-2: positive_adjusted_edge_ratio 字段名错配 → positive_edge_ratio
  P0-3: approve/reject_recommendation 状态守卫失效 → return None
  P0-4: readiness_evaluator Check 2 读取不存在的 manifest["status"] → 从 combos 推导
  P0-5: rdp_run_live_attribution exit code 2 从未设置 → live_fallback 追踪
  P0-6: Phase 4 SQL 表名直接拼接 → 白名单校验
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


# =========================================================================
# Section 1: rdp_run_live_attribution — main() 返回 int (P0-5)
# =========================================================================


class TestLiveAttributionMainReturnType:
    """P0-5: main() 应返回 int（0 或 2），不再直接 sys.exit。"""

    def test_main_return_annotation_is_int(self):
        source = _PROJECT_ROOT / "scripts" / "rdp_run_live_attribution.py"
        with open(source, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                assert node.returns is not None, "main() 应有返回类型注解"
                if isinstance(node.returns, ast.Name):
                    assert node.returns.id == "int", (
                        f"main() 返回类型应为 int，实际为 {node.returns.id}"
                    )
                elif isinstance(node.returns, ast.Constant):
                    assert str(node.returns.value) == "int"
                break
        else:
            pytest.fail("未找到 main() 函数定义")

    def test_main_module_uses_sys_exit(self):
        """if __name__ == '__main__' 应调用 sys.exit(main())。"""
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_live_attribution.py").read_text(
            encoding="utf-8",
        )
        assert "sys.exit(main())" in text, "应使用 sys.exit(main()) 而非直接调用 main()"

    def test_live_fallback_variable_exists(self):
        """main() 中应有 live_fallback 变量追踪。"""
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_live_attribution.py").read_text(
            encoding="utf-8",
        )
        assert "live_fallback" in text, "应有 live_fallback 变量追踪"
        assert "live_fallback = True" in text, "应在无 live URL 时设置 live_fallback = True"
        assert "return 2" in text, "应在 live_fallback 时返回 exit code 2"

    def test_no_bare_sys_exit_zero(self):
        """不应有裸 sys.exit(0) 调用（应改为 return 0）。"""
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_live_attribution.py").read_text(
            encoding="utf-8",
        )
        # 过滤掉 "sys.exit(main())" 后不应再有 sys.exit(0)
        lines = text.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "sys.exit(0)" in stripped:
                pytest.fail(f"不应有裸 sys.exit(0)，应改为 return 0: {stripped}")


# =========================================================================
# Section 2: Phase 3/4 manifest — overall_status 字段 (P0-1, P0-4 根因)
# =========================================================================


class TestPhase3ManifestOverallStatus:
    """P0-4 根因修复: Phase 3 manifest 应包含 overall_status。"""

    def test_phase3_manifest_has_overall_status_in_source(self):
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_phase3_round.py").read_text(
            encoding="utf-8",
        )
        assert '"overall_status"' in text, "Phase 3 manifest 应包含 overall_status 字段"

    def test_phase3_artifact_root_resolved(self):
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_phase3_round.py").read_text(
            encoding="utf-8",
        )
        assert "args.artifact_root).resolve()" in text, (
            "artifact_root 应通过 .resolve() 转为绝对路径"
        )

    def test_phase3_stderr_logging(self):
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_phase3_round.py").read_text(
            encoding="utf-8",
        )
        assert "stderr_text" in text, "应记录 subprocess stderr 以便调试"


class TestPhase4ManifestOverallStatus:
    """P0-4 根因修复: Phase 4 manifest 应包含 overall_status。"""

    def test_phase4_manifest_has_overall_status_in_source(self):
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_phase4_round.py").read_text(
            encoding="utf-8",
        )
        assert '"overall_status"' in text, "Phase 4 manifest 应包含 overall_status 字段"

    def test_phase4_artifact_root_resolved(self):
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_phase4_round.py").read_text(
            encoding="utf-8",
        )
        assert "args.artifact_root).resolve()" in text, (
            "artifact_root 应通过 .resolve() 转为绝对路径"
        )

    def test_phase4_stderr_logging(self):
        text = (_PROJECT_ROOT / "scripts" / "rdp_run_phase4_round.py").read_text(
            encoding="utf-8",
        )
        assert "stderr_text" in text, "应记录 subprocess stderr 以便调试"


# =========================================================================
# Section 3: evidence_bundle — 字段名修复 + 路径解析 (P0-1, P0-2, P0-4)
# =========================================================================


class TestEvidenceBundleFieldNameFix:
    """P0-2: positive_adjusted_edge_ratio → positive_edge_ratio。"""

    def test_no_positive_adjusted_edge_ratio_in_source(self):
        source = (
            _PROJECT_ROOT
            / "aats" / "data_platform" / "decision_system" / "evidence_bundle.py"
        )
        text = source.read_text(encoding="utf-8")
        assert "positive_adjusted_edge_ratio" not in text, (
            "应使用 positive_edge_ratio（匹配 execution_cost_model 输出）"
        )

    def test_positive_edge_ratio_present_in_source(self):
        source = (
            _PROJECT_ROOT
            / "aats" / "data_platform" / "decision_system" / "evidence_bundle.py"
        )
        text = source.read_text(encoding="utf-8")
        assert "positive_edge_ratio" in text

    def test_execution_cost_model_field_matches(self):
        """验证 execution_cost_model 实际输出的字段名。"""
        from aats.data_platform.execution_realism.execution_cost_model import (
            build_execution_cost_summary,
        )

        # 空数据 → _empty_summary 也应包含一致字段名
        empty = build_execution_cost_summary([])
        assert "positive_edge_ratio" in empty, (
            "execution_cost_model 应输出 positive_edge_ratio 字段"
        )
        assert "positive_adjusted_edge_ratio" not in empty

    def test_execution_cost_model_with_data_field_name(self):
        """有数据时也应输出 positive_edge_ratio。"""
        from aats.data_platform.execution_realism.execution_cost_model import (
            build_execution_cost_summary,
        )

        test_row: dict[str, Any] = {
            "feasibility_category": "fully_fillable",
            "estimated_slippage_bps": 1.5,
            "estimated_total_execution_cost_bps": 3.0,
            "cost_adjusted_edge_bps": 2.0,
            "cost_vs_assumed_bps": -0.5,
            "candidate_action": "open",
        }
        summary = build_execution_cost_summary([test_row])
        assert "positive_edge_ratio" in summary
        assert summary["positive_edge_ratio"] == 1.0  # 1/1 positive edge
        assert "positive_adjusted_edge_ratio" not in summary


class TestEvidenceBundlePathResolution:
    """P0-1: run_dir 路径解析防御。"""

    def test_source_has_is_absolute_check(self):
        source = (
            _PROJECT_ROOT
            / "aats" / "data_platform" / "decision_system" / "evidence_bundle.py"
        )
        text = source.read_text(encoding="utf-8")
        assert "is_absolute" in text, "应检查 run_dir 是否为绝对路径"
        assert "resolve()" in text, "非绝对路径应 resolve()"


class TestEvidenceBundleOverallStatusFallback:
    """P0-4: evidence_bundle fallback 应读取 overall_status。"""

    def test_fallback_reads_overall_status(self):
        source = (
            _PROJECT_ROOT
            / "aats" / "data_platform" / "decision_system" / "evidence_bundle.py"
        )
        text = source.read_text(encoding="utf-8")
        assert 'manifest.get("overall_status"' in text, (
            "fallback 路径应从 manifest 读取 overall_status"
        )

    def test_enrich_uses_manifest_overall_status(self):
        """_enrich_round_from_manifest 应使用 manifest.overall_status 作为后备。"""
        source = (
            _PROJECT_ROOT
            / "aats" / "data_platform" / "decision_system" / "evidence_bundle.py"
        )
        text = source.read_text(encoding="utf-8")
        # enriched 的 status 构造应引用 manifest.get("overall_status")
        count = text.count('manifest.get("overall_status"')
        assert count >= 3, (
            f"应在多处引用 overall_status (enriched 初始化 + Phase 3 fallback + Phase 4 fallback), "
            f"实际出现 {count} 次"
        )


# =========================================================================
# Section 4: recommendation_registry — 状态守卫 (P0-3)
# =========================================================================


class TestRecommendationRegistryStateGuard:
    """P0-3: approve/reject 非 draft 状态应返回 None 而非继续执行。"""

    @pytest.fixture(autouse=True)
    def _stub_governance_db(self, monkeypatch: Any) -> None:
        """A-0.3 之后，add/approve/reject/supersede 在 DB 不可达时会直接抛
        ``DBUnavailableError``。本类里的测试只关心 in-memory 状态机，不测 DB，
        所以把 ``_db_*`` 辅助函数全部打桩成 no-op / 固定返回值——这与之前
        "DB 不可达就悄悄跳过" 的行为等价，让测试意图保持不变。
        """
        from aats.data_platform.decision_system import recommendation_registry as rr

        monkeypatch.setattr(rr, "_db_sync_recommendation", lambda *a, **kw: None)
        monkeypatch.setattr(rr, "_db_update_rec_status", lambda *a, **kw: True)
        monkeypatch.setattr(rr, "_db_sync_active_decision", lambda *a, **kw: None)

    def test_approve_draft_succeeds(self):
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            approve_recommendation,
            create_recommendation,
            load_recommendation_registry,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        result = approve_recommendation(reg, rec_id, approved_by="test")
        assert result is not None
        assert result["status"] == "approved"

    def test_approve_non_draft_returns_none(self):
        """已经 approved 的 recommendation 再次 approve 应返回 None。"""
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            approve_recommendation,
            create_recommendation,
            load_recommendation_registry,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        # First approve
        approve_recommendation(reg, rec_id)

        # Second approve should fail (already approved, not draft)
        result = approve_recommendation(reg, rec_id)
        assert result is None, "非 draft 状态应返回 None"

    def test_approve_non_draft_does_not_modify_status(self):
        """非 draft 的 recommendation approve 后状态不应改变。"""
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            approve_recommendation,
            create_recommendation,
            load_recommendation_registry,
            reject_recommendation,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        # Reject first
        reject_recommendation(reg, rec_id)
        assert rec["status"] == "rejected"

        # Try to approve — should fail, status remains rejected
        result = approve_recommendation(reg, rec_id)
        assert result is None
        assert rec["status"] == "rejected", "状态不应被修改"

    def test_reject_draft_succeeds(self):
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            create_recommendation,
            load_recommendation_registry,
            reject_recommendation,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="pause",
            confidence="low",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        result = reject_recommendation(reg, rec_id, rejected_by="test")
        assert result is not None
        assert result["status"] == "rejected"

    def test_reject_non_draft_returns_none(self):
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            create_recommendation,
            load_recommendation_registry,
            reject_recommendation,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="pause",
            confidence="low",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        # First reject
        reject_recommendation(reg, rec_id)

        # Second reject should fail
        result = reject_recommendation(reg, rec_id)
        assert result is None

    def test_reject_approved_returns_none_preserves_status(self):
        """已 approved 的 recommendation 尝试 reject 应失败且保持 approved。"""
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            approve_recommendation,
            create_recommendation,
            load_recommendation_registry,
            reject_recommendation,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        approve_recommendation(reg, rec_id)
        assert rec["status"] == "approved"

        result = reject_recommendation(reg, rec_id)
        assert result is None
        assert rec["status"] == "approved", "状态不应从 approved 变为 rejected"

    def test_supersede_any_state_succeeds(self):
        """supersede 不限制来源状态（系统操作）。"""
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            approve_recommendation,
            create_recommendation,
            load_recommendation_registry,
            supersede_recommendation,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="test",
        )
        add_recommendation(reg, rec)
        rec_id = rec["recommendation_id"]

        approve_recommendation(reg, rec_id)
        result = supersede_recommendation(reg, rec_id, actor="system")
        assert result is not None
        assert result["status"] == "superseded"

    def test_add_recommendation_keeps_different_recommendation_types_side_by_side(self):
        """不同 recommendation_type 不应互相 supersede。"""
        from aats.data_platform.decision_system.recommendation_registry import (
            add_recommendation,
            create_recommendation,
            load_recommendation_registry,
        )

        reg = load_recommendation_registry(pathlib.Path("/nonexistent"))
        parameter_rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="parameter_upgrade",
            confidence="high",
            reason="candidate ready",
        )
        pause_rec = create_recommendation(
            family="independent",
            timeframe="15m",
            recommendation_type="pause",
            confidence="high",
            reason="failure ratio too high",
        )

        add_recommendation(reg, parameter_rec)
        add_recommendation(reg, pause_rec)

        assert parameter_rec["status"] == "draft"
        assert pause_rec["status"] == "draft"


# =========================================================================
# Section 5: readiness_evaluator — Check 2 从 combos 推导状态 (P0-4)
# =========================================================================


class TestReadinessEvaluatorCheck2:
    """P0-4: Check 2 应从 combos 推导状态，不依赖顶层 status。"""

    def _build_evidence(
        self,
        *,
        p3_round_count: int = 1,
        p3_combos: dict | None = None,
        p3_latest_extra: dict | None = None,
    ) -> dict[str, Any]:
        """构建测试用 evidence_bundle。"""
        p3_evidence: dict[str, Any] = {
            "round_count": p3_round_count,
            "trusted_round_count": p3_round_count,
        }

        if p3_round_count > 0:
            latest: dict[str, Any] = {"round_id": "test_round"}
            if p3_combos is not None:
                latest["combos"] = p3_combos
            if p3_latest_extra:
                latest.update(p3_latest_extra)
            p3_evidence["latest_round"] = latest

        return {
            "phase2_evidence": {
                "aggregate_stats": {
                    "experiments_with_openings": 2,
                    "mean_positive_edge_ratio": 0.3,
                    "max_opening_count": 5,
                },
            },
            "phase3_evidence": p3_evidence,
            "phase4_evidence": {"round_count": 0},
            "phase5_governance_evidence": {"quality_health": "healthy"},
        }

    @staticmethod
    def _get_check(result: dict[str, Any], name: str) -> dict[str, Any]:
        return next(c for c in result["checks"] if c["check"] == name)

    def test_check2_passes_when_combos_succeeded(self):
        """combos 有 succeeded → attribution_ok = True。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_combos={
                "independent_15m": {"status": "succeeded"},
                "independent_1h": {"status": "succeeded"},
            },
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is True, "combos 全部 succeeded 时应通过"

    def test_check2_passes_when_combos_partial_success(self):
        """combos 有部分 succeeded + 部分 failed → attribution_ok = True。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_combos={
                "independent_15m": {"status": "succeeded"},
                "independent_1h": {"status": "failed"},
            },
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is True, "至少一个 combo 成功时应通过"

    def test_check2_fails_when_all_combos_failed(self):
        """combos 全部 failed → attribution_ok = False。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_combos={
                "independent_15m": {"status": "failed"},
                "independent_1h": {"status": "failed"},
            },
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is False

    def test_check2_uses_overall_status_when_no_combos(self):
        """无 combos 但有 overall_status → 使用 overall_status。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_round_count=1,
            p3_combos={},  # empty combos
            p3_latest_extra={"overall_status": "succeeded"},
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is True

    def test_check2_no_status_no_combos_fails(self):
        """无 combos 且无 status/overall_status → unknown → 不通过。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_round_count=1,
            p3_combos={},  # empty combos, no overall_status either
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is False

    def test_check2_combo_partial_success_status(self):
        """combo status = partial_success 应视为通过。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_combos={
                "independent_15m": {"status": "partial_success"},
            },
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is True

    def test_check2_no_phase3_data_fails_readiness(self):
        """无 Phase 3 数据必须 failed check；不能再"跳过即通过"。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(p3_round_count=0)
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = self._get_check(result, "attribution_no_severe_issue")
        assert check2["passed"] is False
        assert result["readiness"] != "ready_for_next_live_test"

    def test_check2_phase3_replay_only_fails(self):
        """latest_round.replay_only=True 时 Phase 3 attribution 必须 failed。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = self._build_evidence(
            p3_combos={"independent_15m": {"status": "succeeded"}},
            p3_latest_extra={"replay_only": True, "overall_status": "succeeded"},
        )
        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = self._get_check(result, "attribution_no_severe_issue")
        assert check2["passed"] is False, "replay_only attribution 不应 promote"
        assert "replay_only" in check2["detail"]
        assert result["readiness"] != "ready_for_next_live_test"


# =========================================================================
# Section 6: market_alignment — SQL 表名白名单校验 (P0-6)
# =========================================================================


class TestMarketAlignmentSqlSafety:
    """P0-6: SQL 表名应通过白名单校验防止注入。"""

    def test_valid_timeframe_15m(self):
        from aats.data_platform.execution_realism.market_alignment import (
            _gold_table_name,
        )

        assert _gold_table_name("15m") == "gold.market_swap_replay_bars_15m"

    def test_valid_timeframe_1h_case_insensitive(self):
        from aats.data_platform.execution_realism.market_alignment import (
            _gold_table_name,
        )

        assert _gold_table_name("1H") == "gold.market_swap_replay_bars_1h"
        assert _gold_table_name("1h") == "gold.market_swap_replay_bars_1h"

    def test_valid_timeframe_4h(self):
        from aats.data_platform.execution_realism.market_alignment import (
            _gold_table_name,
        )

        assert _gold_table_name("4h") == "gold.market_swap_replay_bars_4h"

    def test_valid_timeframe_1d(self):
        from aats.data_platform.execution_realism.market_alignment import (
            _gold_table_name,
        )

        assert _gold_table_name("1d") == "gold.market_swap_replay_bars_1d"

    def test_invalid_timeframe_raises_value_error(self):
        from aats.data_platform.execution_realism.market_alignment import (
            _gold_table_name,
        )

        with pytest.raises(ValueError, match="Invalid timeframe"):
            _gold_table_name("'; DROP TABLE users; --")

    def test_sql_injection_attempts_rejected(self):
        from aats.data_platform.execution_realism.market_alignment import (
            _gold_table_name,
        )

        malicious_inputs = [
            "15m; DROP TABLE gold.bars",
            "1h UNION SELECT * FROM pg_catalog",
            "../../../etc/passwd",
            "",
            "99x",
            "5m",
            "2h",
        ]
        for inp in malicious_inputs:
            with pytest.raises(ValueError):
                _gold_table_name(inp)

    def test_whitelist_is_frozen(self):
        """白名单应为 frozenset 以防止意外修改。"""
        from aats.data_platform.execution_realism.market_alignment import (
            _VALID_TIMEFRAMES,
        )

        assert isinstance(_VALID_TIMEFRAMES, frozenset)


# =========================================================================
# Section 7: 端到端集成校验 — 跨模块字段一致性
# =========================================================================


class TestCrossCuttingFieldConsistency:
    """验证跨模块字段名完全一致。"""

    def test_execution_cost_summary_fields(self):
        """execution_cost_summary 输出的字段名与 evidence_bundle 读取一致。"""
        from aats.data_platform.execution_realism.execution_cost_model import (
            build_execution_cost_summary,
        )

        # 有数据的 summary
        test_row: dict[str, Any] = {
            "feasibility_category": "fully_fillable",
            "estimated_slippage_bps": 1.5,
            "estimated_total_execution_cost_bps": 3.0,
            "cost_adjusted_edge_bps": 2.0,
            "cost_vs_assumed_bps": -0.5,
            "candidate_action": "open",
        }
        summary = build_execution_cost_summary([test_row])

        # evidence_bundle._enrich_round_from_manifest 读取这些字段:
        expected_keys = [
            "total_candidates",
            "full_fill_ratio",
            "positive_edge_ratio",
            "slippage",
            "total_execution_cost",
            "cost_adjusted_edge",
        ]
        for key in expected_keys:
            assert key in summary, f"execution_cost_summary 应包含 {key}"

        # 确保没有旧字段名
        assert "positive_adjusted_edge_ratio" not in summary

    def test_cost_summary_slippage_has_mean(self):
        """slippage 统计应包含 mean 字段（evidence_bundle 读取）。"""
        from aats.data_platform.execution_realism.execution_cost_model import (
            build_execution_cost_summary,
        )

        test_row: dict[str, Any] = {
            "feasibility_category": "fully_fillable",
            "estimated_slippage_bps": 1.5,
            "estimated_total_execution_cost_bps": 3.0,
            "cost_adjusted_edge_bps": 2.0,
            "cost_vs_assumed_bps": -0.5,
            "candidate_action": "open",
        }
        summary = build_execution_cost_summary([test_row])
        assert "mean" in summary["slippage"], "slippage 应包含 mean 字段"
        assert "mean" in summary["cost_adjusted_edge"]


# =========================================================================
# Section 8: 边界 — readiness_evaluator 综合场景
# =========================================================================


class TestReadinessEvaluatorIntegration:
    """readiness_evaluator 综合场景（结合多个修复验证）。"""

    def test_full_evidence_all_passed(self):
        """完整证据、所有维度通过 → ready_for_next_live_test。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence: dict[str, Any] = {
            "phase2_evidence": {
                "combo_stats": {
                    "independent_15m": {
                        "available": True,
                        "experiments_with_openings": 3,
                        "mean_positive_edge_ratio": 0.4,
                    },
                },
            },
            "phase3_evidence": {
                "round_count": 1,
                "trusted_round_count": 1,
                "latest_round": {
                    "round_id": "test",
                    "overall_status": "succeeded",
                    "replay_only": False,
                    "combos": {
                        "independent_15m": {"status": "succeeded"},
                        "independent_1h": {"status": "succeeded"},
                    },
                },
            },
            "phase4_evidence": {
                "round_count": 1,
                "trusted_round_count": 1,
                "latest_round": {
                    "round_id": "test",
                    "combos": {
                        "independent_15m": {
                            "status": "succeeded",
                            "cost_summary": {
                                "cost_adjusted_edge_mean": 2.0,
                                "full_fill_ratio": 0.8,
                                "total_candidates": 10,
                            },
                        },
                    },
                },
            },
            "phase5_governance_evidence": {
                "quality_health": "healthy",
                "frozen_parameter_sets": [{"parameter_set_id": "ps_001"}],
                "candidate_parameter_sets": [],
            },
        }

        upgrade_candidates = [
            {
                "decision": "promote_candidate",
                "parameter_set_id": "ps_001",
                "score_ratio": 0.85,
            },
        ]
        ft_decisions = [
            {
                "family": "independent",
                "timeframe": "15m",
                "combo_key": "independent_15m",
                "decision": "keep_active",
                "confidence": "high",
            },
        ]

        result = evaluate_promotion_readiness(
            evidence, upgrade_candidates, ft_decisions,
        )
        assert result["readiness"] == "ready_for_next_live_test"
        assert result["checks_failed"] == 0

    def test_missing_phase3_combos_old_manifest_format(self):
        """旧格式 manifest（无 combos、无 overall_status）→ Check 2 FAIL."""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence: dict[str, Any] = {
            "phase2_evidence": {
                "aggregate_stats": {
                    "experiments_with_openings": 2,
                    "mean_positive_edge_ratio": 0.3,
                    "max_opening_count": 5,
                },
            },
            "phase3_evidence": {
                "round_count": 1,
                "trusted_round_count": 1,
                # 模拟旧 manifest 无 combos 也无 overall_status
                "latest_round": {
                    "round_id": "old_round",
                    "status": "unknown",
                },
            },
            "phase4_evidence": {"round_count": 0},
            "phase5_governance_evidence": {"quality_health": "healthy"},
        }

        result = evaluate_promotion_readiness(evidence, [], [])

        check2 = next(
            c for c in result["checks"]
            if c["check"] == "attribution_no_severe_issue"
        )
        assert check2["passed"] is False, "旧 manifest 无有效状态应 FAIL"


# =========================================================================
# Section 9: readiness_evaluator — gate hardening (SoW: golden_path_readiness_gate_hardening)
# =========================================================================


def _gate_evidence_with_overrides(
    *,
    phase3: dict[str, Any] | None = None,
    phase4: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """完整可 promote 证据模板；调用方用 overrides 只替换需要被测的 Phase。"""
    return {
        "phase2_evidence": {
            "combo_stats": {
                "independent_15m": {
                    "available": True,
                    "experiments_with_openings": 3,
                    "mean_positive_edge_ratio": 0.4,
                },
            },
        },
        "phase3_evidence": phase3 if phase3 is not None else {
            "round_count": 1,
            "trusted_round_count": 1,
            "latest_round": {
                "round_id": "r1",
                "replay_only": False,
                "overall_status": "succeeded",
                "combos": {
                    "independent_15m": {"status": "succeeded"},
                },
            },
        },
        "phase4_evidence": phase4 if phase4 is not None else {
            "round_count": 1,
            "trusted_round_count": 1,
            "latest_round": {
                "round_id": "r1",
                "combos": {
                    "independent_15m": {
                        "status": "succeeded",
                        "cost_summary": {
                            "cost_adjusted_edge_mean": 2.0,
                            "full_fill_ratio": 0.8,
                            "total_candidates": 10,
                        },
                    },
                },
            },
        },
        "phase5_governance_evidence": {
            "quality_health": "healthy",
            "frozen_parameter_sets": [{"parameter_set_id": "ps_001"}],
            "candidate_parameter_sets": [],
        },
    }


_GATE_PROMOTE_CANDIDATES = [
    {"decision": "promote_candidate", "parameter_set_id": "ps_001", "score_ratio": 0.85},
]
_GATE_FT_DECISIONS = [
    {"combo_key": "independent_15m", "decision": "keep_active", "confidence": "high"},
]


class TestReadinessGateHardening:
    """SoW: 只认完整、非 replay-only 的 Phase 3/4 证据；移除 critical subset 中径。"""

    def test_baseline_full_evidence_is_ready(self):
        """完整证据仍应 ready_for_next_live_test (确保 fixture 自身有效)。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        result = evaluate_promotion_readiness(
            _gate_evidence_with_overrides(),
            _GATE_PROMOTE_CANDIDATES,
            _GATE_FT_DECISIONS,
        )
        assert result["readiness"] == "ready_for_next_live_test"
        assert result["overall_confidence"] == "high"
        assert result["checks_failed"] == 0

    def test_missing_phase3_blocks_readiness(self):
        """Phase 3 无数据不能再跳过通过，必须阻塞 readiness。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = _gate_evidence_with_overrides(phase3={"round_count": 0})
        result = evaluate_promotion_readiness(
            evidence, _GATE_PROMOTE_CANDIDATES, _GATE_FT_DECISIONS,
        )
        check = next(
            c for c in result["checks"] if c["check"] == "attribution_no_severe_issue"
        )
        assert check["passed"] is False
        assert result["readiness"] == "not_ready_attribution_issue"
        assert any("Phase 3" in b for b in result["blockers"])

    def test_replay_only_phase3_blocks_readiness(self):
        """Phase 3 latest round replay_only=True 必须 failed，并写出 replay_only 说明。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = _gate_evidence_with_overrides(
            phase3={
                "round_count": 1,
                "trusted_round_count": 1,
                "latest_round": {
                    "round_id": "r_replay",
                    "replay_only": True,
                    "overall_status": "succeeded",
                    "combos": {
                        "independent_15m": {"status": "succeeded"},
                    },
                },
            },
        )
        result = evaluate_promotion_readiness(
            evidence, _GATE_PROMOTE_CANDIDATES, _GATE_FT_DECISIONS,
        )
        check = next(
            c for c in result["checks"] if c["check"] == "attribution_no_severe_issue"
        )
        assert check["passed"] is False
        assert "replay_only" in check["detail"]
        assert any("replay_only" in b for b in result["blockers"])
        assert result["readiness"] != "ready_for_next_live_test"

    def test_missing_phase4_blocks_readiness(self):
        """Phase 4 无数据不能再跳过通过，必须阻塞 readiness。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = _gate_evidence_with_overrides(phase4={"round_count": 0})
        result = evaluate_promotion_readiness(
            evidence, _GATE_PROMOTE_CANDIDATES, _GATE_FT_DECISIONS,
        )
        check = next(
            c for c in result["checks"] if c["check"] == "execution_not_severe"
        )
        assert check["passed"] is False
        assert result["readiness"] == "not_ready_execution_issue"
        assert any("Phase 4" in b for b in result["blockers"])

    def test_phase4_latest_round_missing_cost_summary_blocks_readiness(self):
        """Phase 4 latest round 全部 combo 缺可用 cost_summary 时必须 failed。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        evidence = _gate_evidence_with_overrides(
            phase4={
                "round_count": 1,
                "trusted_round_count": 1,
                "latest_round": {
                    "round_id": "r1",
                    "combos": {
                        "independent_15m": {"status": "succeeded", "cost_summary": {}},
                    },
                },
            },
        )
        result = evaluate_promotion_readiness(
            evidence, _GATE_PROMOTE_CANDIDATES, _GATE_FT_DECISIONS,
        )
        check = next(
            c for c in result["checks"] if c["check"] == "execution_not_severe"
        )
        assert check["passed"] is False
        assert result["readiness"] == "not_ready_execution_issue"

    def test_critical_subset_only_no_longer_ready(self):
        """旧中径：research+governance+promote_candidate 通过但 attribution 失败，
        不应再返回 ready_for_next_live_test。"""
        from aats.data_platform.decision_system.readiness_evaluator import (
            evaluate_promotion_readiness,
        )

        # Phase 3 全部 combo failed → attribution 失败；其余保持"关键子集"通过
        evidence = _gate_evidence_with_overrides(
            phase3={
                "round_count": 1,
                "trusted_round_count": 1,
                "latest_round": {
                    "round_id": "r1",
                    "replay_only": False,
                    "combos": {
                        "independent_15m": {"status": "failed"},
                    },
                },
            },
        )
        result = evaluate_promotion_readiness(
            evidence, _GATE_PROMOTE_CANDIDATES, _GATE_FT_DECISIONS,
        )
        # research_stability / governance_healthy / has_promote_candidate 都通过
        passed_names = {c["check"] for c in result["checks"] if c["passed"]}
        assert "research_stability" in passed_names
        assert "governance_healthy" in passed_names
        assert "has_promote_candidate" in passed_names
        # 但 attribution failed，不应再被 critical subset 救回来
        assert result["readiness"] == "not_ready_attribution_issue"
        assert result["overall_confidence"] != "medium" or result["readiness"] != "ready_for_next_live_test"
