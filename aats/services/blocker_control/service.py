from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from aats.schemas.blocker_control import (
    BlockerActionDefinition,
    BlockerControlItem,
    BlockerControlSnapshot,
)
from aats.services.blocker_control.priority import blocker_priority

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class BlockerControlService:
    _SUBMIT_ONLY = {
        "guarded_execution_dry_run",
        "live_submit_disabled",
        "okx_simulated_trading_required",
        "local_demo_no_exchange_submission",
        "real_market_paper_uses_local_paper_execution",
    }

    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def snapshot(self) -> BlockerControlSnapshot:
        recovery = self.owner.recovery_view()
        items = self._build_items(recovery=recovery)
        payload = {
            "halted": self.owner.runtime.kill_switch.halted,
            "review_required": recovery["review_required"],
            "resume_eligible": recovery["resume_eligible"],
            "safe_to_trade": recovery["safe_to_trade"],
            "blockers": [self._version_payload(item) for item in items],
        }
        panel_version = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        primary = items[0] if items else None
        secondary = items[1:] if len(items) > 1 else []
        return BlockerControlSnapshot(
            panel_version=panel_version,
            halted=self.owner.runtime.kill_switch.halted,
            review_required=recovery["review_required"],
            resume_eligible=recovery["resume_eligible"],
            safe_to_trade=recovery["safe_to_trade"],
            primary_blocker=primary,
            secondary_blockers=secondary,
            blockers=items,
            next_step_summary=self._next_step_summary(primary, secondary),
        )

    def has_active_blocker(self, code: str) -> bool:
        return any(item.blocker == code for item in self.snapshot().blockers)

    def action_target(self, action_id: str) -> tuple[str | None, str | None]:
        snapshot = self.snapshot()
        for item in snapshot.blockers:
            for action in item.actions:
                if action.action_id == action_id:
                    return item.blocker, item.blocker_instance_id
        return None, None

    def _build_items(self, *, recovery: dict[str, Any]) -> list[BlockerControlItem]:
        health_snapshot = self.owner.runtime.health_service.snapshot()
        system_mode = self.owner.system_mode()
        ai_runtime = self.owner.runtime.ai_service.status()
        latest_reconciliation = self.owner._latest_scoped_reconciliation()
        blockers: list[tuple[str, str, bool]] = []
        for code in health_snapshot.blockers:
            blockers.append((code, self._subsystem_for(code), code in self._SUBMIT_ONLY))
        if self.owner.runtime.kill_switch.halted and all(code != "kill_switch_active" for code, _, _ in blockers):
            blockers.append(("kill_switch_active", "execution_control", False))
        for code in system_mode.get("submit_blocked_reasons", []):
            if all(existing != code for existing, _, _ in blockers):
                blockers.append((code, self._subsystem_for(code), True))
        for code in recovery.get("resume_blocked_reasons", []):
            if all(existing != code for existing, _, _ in blockers):
                blockers.append((code, self._subsystem_for(code), code in self._SUBMIT_ONLY))

        root_candidate = self._root_cause_code(blockers)
        items: list[BlockerControlItem] = []
        for code, subsystem, submit_only in blockers:
            category = self._category_for(code, submit_only=submit_only)
            priority = blocker_priority(code, category=category)
            is_surface_halt = code == "kill_switch_active" and root_candidate not in {None, "kill_switch_active"}
            actions = self._actions_for(
                code,
                recovery=recovery,
                latest_reconciliation=latest_reconciliation,
                submit_only=submit_only,
                ai_runtime=ai_runtime,
            )
            title, description, impact, next_step = self._copy_for(
                code,
                recovery=recovery,
                submit_only=submit_only,
                latest_reconciliation=latest_reconciliation,
                is_surface_halt=is_surface_halt,
                ai_runtime=ai_runtime,
            )
            items.append(
                BlockerControlItem(
                    blocker=code,
                    category=category,
                    subsystem=subsystem,
                    priority=priority,
                    resolution_mode=self._resolution_mode_for(code, submit_only=submit_only),
                    title=title,
                    description=description,
                    impact=impact,
                    recommended_next_step=next_step,
                    root_cause=code == root_candidate,
                    derived_from=[] if not is_surface_halt else [root_candidate] if root_candidate else [],
                    affects_execution=not submit_only,
                    submit_only=submit_only,
                    actions=actions,
                )
            )
        items.sort(key=lambda item: (item.priority, 1 if item.blocker == "kill_switch_active" and not item.root_cause else 0, item.blocker))
        return items

    @staticmethod
    def _version_payload(item: BlockerControlItem) -> dict[str, Any]:
        return {
            "blocker": item.blocker,
            "priority": item.priority,
            "category": item.category,
            "root_cause": item.root_cause,
            "actions": [action.action_id for action in item.actions],
        }

    @staticmethod
    def _subsystem_for(code: str) -> str:
        if code.startswith("phase1_shadow"):
            return "phase1_shadow"
        if code.startswith("derivatives_"):
            return "risk_control"
        if code.startswith("market_"):
            return "market_data"
        if code.startswith("account_"):
            return "account_state"
        if code.startswith("okx_"):
            return "execution_adapter"
        if code.startswith("reconciliation_") or code.startswith("operator_rebaseline"):
            return "reconciliation"
        if code.startswith("ai_"):
            return "ai"
        if code.startswith("strategy_profile_"):
            return "profile_control"
        if code.startswith("kill_switch"):
            return "execution_control"
        if code.startswith("guarded_") or code.startswith("live_submit") or code.startswith("real_market_"):
            return "execution_adapter"
        return "system"

    def _category_for(self, code: str, *, submit_only: bool) -> str:
        if submit_only:
            return "submission_mode"
        if code.startswith("ai_"):
            return "system_execution" if code == "ai_degraded_requires_manual_review" else "ai_decision"
        if code.startswith("strategy_profile_"):
            return "profile_control"
        return "system_execution"

    @staticmethod
    def _resolution_mode_for(code: str, *, submit_only: bool) -> str:
        if submit_only or code == "local_demo_no_exchange_submission":
            return "external_only"
        if code in {"market_data_stale", "account_state_stale", "account_snapshot_missing", "market_connection_down"}:
            return "manual_or_auto"
        if code == "rebaseline_in_progress":
            return "auto_only"
        return "manual_only"

    @staticmethod
    def _root_cause_code(blockers: list[tuple[str, str, bool]]) -> str | None:
        if not blockers:
            return None
        codes = [code for code, _, _ in blockers]
        preferred = [code for code in codes if code != "kill_switch_active"]
        if not preferred:
            return "kill_switch_active"
        ranked = [
            (blocker_priority(code, category="submission_mode" if submit_only else "system_execution"), code)
            for code, _, submit_only in blockers
            if code != "kill_switch_active"
        ]
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _actions_for(
        self,
        code: str,
        *,
        recovery: dict[str, Any],
        latest_reconciliation,
        submit_only: bool,
        ai_runtime: dict[str, Any],
    ) -> list[BlockerActionDefinition]:
        actions: list[BlockerActionDefinition] = []
        ai_review_required = bool(ai_runtime.get("outcome_review_required"))
        reconciliation_id = None if latest_reconciliation is None else latest_reconciliation.reconciliation_id
        if code in {"phase1_shadow_lagging", "phase1_shadow_degraded"}:
            return [
                BlockerActionDefinition(
                    action_id="inspect-shadow",
                    label="查看影子详情",
                    kind="client",
                    method="CLIENT",
                    client_action="inspect-shadow",
                    tone="ghost",
                    expected_effect="打开影子兼容层详情，先确认当前积压、最近失败和人工核查记录。",
                ),
                BlockerActionDefinition(
                    action_id="refresh-dashboard",
                    label="刷新当前状态",
                    kind="client",
                    method="CLIENT",
                    client_action="refresh-dashboard",
                    tone="secondary",
                    expected_effect="重新拉取健康状态、阻断面板和影子兼容层快照，确认异常是否仍然存在。",
                ),
                BlockerActionDefinition(
                    action_id="acknowledge-phase1-shadow",
                    label="已核查，继续阻断",
                    endpoint="/system/blocker-actions/acknowledge-phase1-shadow",
                    tone="warning",
                    requires_confirmation=True,
                    confirmation_title="确认已完成人工核查",
                    confirmation_copy="这会留下当前影子兼容层状态记录，但不会解除阻断，也不会恢复自动运行。",
                    expected_effect="记录当前人工核查结果，说明系统因为影子兼容层异常而继续保持阻断。",
                ),
                BlockerActionDefinition(
                    action_id="halt-system",
                    label="继续保持暂停",
                    endpoint="/system/halt",
                    tone="danger",
                    expected_effect="保留当前暂停状态，避免在问题未处理完之前恢复自动交易。",
                ),
            ]
        if code in {"derivatives_margin_buffer_auto_halt", "derivatives_liquidation_proximity_auto_halt"}:
            return [
                BlockerActionDefinition(
                    action_id="open-risk-view",
                    label="查看风险视图",
                    kind="client",
                    method="CLIENT",
                    client_action="navigate-view",
                    value="risk",
                    tone="ghost",
                    expected_effect="切到风险页查看保证金缓冲、强平距离和当前恢复状态。",
                ),
                BlockerActionDefinition(
                    action_id="refresh-dashboard",
                    label="刷新当前状态",
                    kind="client",
                    method="CLIENT",
                    client_action="refresh-dashboard",
                    tone="secondary",
                    expected_effect="重新拉取账户快照、风险缓冲和阻断状态，确认自动停机是否仍然成立。",
                ),
                BlockerActionDefinition(
                    action_id="halt-system",
                    label="继续保持暂停",
                    endpoint="/system/halt",
                    tone="danger",
                    expected_effect="继续保持暂停，避免在保证金风险尚未解除时恢复自动交易。",
                ),
            ]
        if code in {"phase1_shadow_lagging", "phase1_shadow_degraded"}:
            actions.extend(
                [
                    BlockerActionDefinition(
                        action_id="inspect-shadow",
                        label="查看影子同步状态",
                        kind="client",
                        method="CLIENT",
                        client_action="inspect-shadow",
                        tone="ghost",
                        expected_effect="切到风险视图，先确认影子兼容层的阻断状态、积压数量和恢复资格。",
                    ),
                    BlockerActionDefinition(
                        action_id="refresh-dashboard",
                        label="刷新当前状态",
                        kind="client",
                        method="CLIENT",
                        client_action="refresh-dashboard",
                        tone="secondary",
                        expected_effect="重新拉取健康状态、阻断面板和影子兼容层快照，确认异常是否仍然存在。",
                    ),
                ]
            )
            actions.append(
                BlockerActionDefinition(
                    action_id="acknowledge-phase1-shadow",
                    label="已核查，继续阻断",
                    endpoint="/system/blocker-actions/acknowledge-phase1-shadow",
                    tone="warning",
                    requires_confirmation=True,
                    confirmation_title="确认已完成人工核查",
                    confirmation_copy="这会留下当前影子兼容层状态记录，但不会解除阻断，也不会恢复自动运行。",
                    expected_effect="记录当前人工核查结果，说明系统因为影子兼容层异常而继续保持阻断。",
                )
            )
        if reconciliation_id:
            actions.append(
                BlockerActionDefinition(
                    action_id=f"inspect-reconciliation:{reconciliation_id}",
                    label="查看最新对账",
                    kind="client",
                    method="CLIENT",
                    client_action="inspect-reconciliation",
                    value=reconciliation_id,
                    tone="ghost",
                    expected_effect="打开最新对账详情，先确认当前账实状态。",
                )
            )
        if code in {"reconciliation_halt_required", "operator_rebaseline_required", "reconciliation_stale"}:
            actions.append(
                BlockerActionDefinition(
                    action_id="reconcile-now",
                    label="重新对账",
                    endpoint="/reconciliation/validate",
                    tone="secondary",
                    expected_effect="立即刷新当前对账结果，并重新计算恢复资格。",
                )
            )
        if code in {"reconciliation_halt_required", "operator_rebaseline_required"} and recovery.get("rebaseline_available"):
            actions.append(
                BlockerActionDefinition(
                    action_id="accept-rebaseline",
                    label="确认为新基线",
                    endpoint="/system/rebaseline",
                    tone="warning",
                    requires_confirmation=True,
                    confirmation_title="确认接受当前状态为新基线",
                    confirmation_copy="只有在你确认交易所状态和本地状态一致且符合预期时，才应执行这一步。",
                    expected_effect="把当前状态接受为新基线，并重新校验恢复资格。",
                )
            )
        if code == "ai_degraded_requires_manual_review":
            if ai_review_required:
                actions.extend(
                    [
                        BlockerActionDefinition(
                            action_id="ai-review-restore",
                            label="确认恢复 AI 决策",
                            endpoint="/system/ai-review/restore",
                            tone="warning",
                            requires_confirmation=True,
                            confirmation_title="确认恢复 AI 决策链路",
                            confirmation_copy="这会清除当前结果复核阻断，并允许后续恢复 AI 决策权。",
                            expected_effect="清除当前 AI 结果复核阻断，允许在没有其他阻断时恢复自动运行。",
                        ),
                        BlockerActionDefinition(
                            action_id="ai-review-degrade-to-baseline",
                            label="改为仅基础策略继续运行",
                            endpoint="/system/ai-review/degrade-to-baseline",
                            tone="primary",
                            requires_confirmation=True,
                            confirmation_title="确认切到仅基础策略运行",
                            confirmation_copy="这会保留当前暂停态，但把 AI 决策权降为仅基础策略运行，后续可恢复到非 AI 主导模式。",
                            expected_effect="把 AI 决策链路降为仅基础策略运行，并解除当前 AI 复核阻断。",
                        ),
                    ]
                )
            actions.append(
                BlockerActionDefinition(
                    action_id="open-ai-workbench",
                    label="查看 AI 复核详情",
                    kind="client",
                    method="CLIENT",
                    client_action="navigate-view",
                    value="ai",
                    tone="ghost",
                    expected_effect="切到 AI 工作台查看最近影子评估、降级原因和窗口统计。",
                )
            )
        if code == "kill_switch_active":
            actions.append(
                BlockerActionDefinition(
                    action_id="resume-system",
                    label="恢复自动运行",
                    endpoint="/system/resume",
                    tone="warning",
                    enabled=bool(recovery.get("resume_eligible")),
                    disabled_reason="当前还有更高优先级阻断未处理。" if not recovery.get("resume_eligible") else None,
                    expected_effect="在没有其他阻断时解除暂停，恢复自动运行。",
                )
            )
        if code in {"strategy_bundle_recovery_in_progress", "strategy_bundle_recovery_requires_review"}:
            actions.append(
                BlockerActionDefinition(
                    action_id="open-recovery-view",
                    label="查看恢复详情",
                    kind="client",
                    endpoint="/system/recovery",
                    tone="warning",
                    expected_effect="查看当前 bundle / sleeve 恢复摘要，确认未完成腿的收敛状态。",
                )
            )
        if code not in {"kill_switch_active"}:
            actions.append(
                BlockerActionDefinition(
                    action_id="halt-system",
                    label="继续保持暂停",
                    endpoint="/system/halt",
                    tone="danger",
                    expected_effect="保留当前暂停状态，避免在问题未处理完之前恢复自动交易。",
                )
            )
        if code in {"market_data_stale", "market_connection_down", "account_state_stale", "account_snapshot_missing", "rebaseline_in_progress"}:
            actions.append(
                BlockerActionDefinition(
                    action_id="refresh-dashboard",
                    label="刷新当前状态",
                    kind="client",
                    method="CLIENT",
                    client_action="refresh-dashboard",
                    tone="secondary",
                    expected_effect="重新拉取行情、账户、对账和恢复状态，确认阻断是否已自动消失。",
                )
            )
        if submit_only and not actions:
            actions.append(
                BlockerActionDefinition(
                    action_id="refresh-dashboard",
                    label="刷新当前状态",
                    kind="client",
                    method="CLIENT",
                    client_action="refresh-dashboard",
                    tone="secondary",
                    expected_effect="重新确认当前模式限制是否仍然存在。",
                )
            )
        return actions

    def _copy_for(
        self,
        code: str,
        *,
        recovery: dict[str, Any],
        submit_only: bool,
        latest_reconciliation,
        is_surface_halt: bool,
        ai_runtime: dict[str, Any],
    ) -> tuple[str, str, str, str]:
        if code == "phase1_shadow_degraded":
            return (
                "影子兼容层写入失败",
                "Phase 1 影子兼容层最近出现了写入失败。继续运行会削弱新旧执行链和账本链的一致性验证，当前不应恢复自动交易。",
                "如果兼容层持续写入失败，系统会失去对新执行表和影子账本的连续校验能力，恢复后的状态可信度会下降。",
                "先查看影子同步状态和最近错误，确认写入失败原因已消除后，再重新评估是否恢复自动运行。",
            )
        if code == "derivatives_margin_buffer_auto_halt":
            return (
                "保证金缓冲触发自动停机",
                "当前保证金占用已经进入自动停机阈值，系统必须先暂停，避免继续扩大合约风险暴露。",
                "如果在保证金缓冲已经打穿硬阈值后仍继续运行，系统会更接近被动减仓或强平，财务和执行风险都会快速放大。",
                "先查看风险视图确认当前保证金占用、账户风险快照和仓位来源，再决定如何减仓和恢复。",
            )
        if code == "derivatives_liquidation_proximity_auto_halt":
            return (
                "最近仓位距离强平过近",
                "当前至少有一条合约仓位已经逼近自动停机定义的强平距离，系统必须先暂停并优先减仓。",
                "如果在最近仓位已经贴近强平时继续自动交易，系统可能在报单和回报延迟之间直接滑入被动减仓或强平。",
                "先查看风险视图确认最危险仓位、强平价格和保证金模式，再决定如何处理仓位。",
            )
        if code == "phase1_shadow_lagging":
            return (
                "影子兼容层尚未追平",
                "Phase 1 影子兼容层仍然落后于当前主执行链路。新旧链路还没有重新收敛前，不应把系统视为可安全恢复。",
                "如果在影子表仍然积压时继续恢复，后续对新执行模型和影子账本的验证会建立在不完整数据上，容易掩盖状态漂移。",
                "先查看影子同步状态，确认订单、成交和保留金积压已经清零，再重新评估恢复资格。",
            )
        if code == "ai_degraded_requires_manual_review":
            if ai_runtime.get("outcome_review_required"):
                return (
                    "AI 结果复核未完成",
                    "最近的影子评估已触发人工复核。你需要明确决定是恢复 AI 决策，还是改为仅基础策略继续运行。",
                    "在完成这一步之前，系统不会恢复 AI 决策链路，也不会允许自动恢复。",
                    "先完成 AI 复核，再决定是否恢复自动运行。",
                )
            return (
                "AI 当前处于降级状态",
                "当前阻断来自 AI 运行故障或 provider 不可用，不是可人工直接放行的结果复核。",
                "在 AI 服务恢复前，系统不会恢复 AI 决策链路，也不应强制解除这一阻断。",
                "先查看 AI 工作台确认故障原因，待 AI 服务恢复后再刷新当前状态。",
            )
        if code == "operator_rebaseline_required":
            return (
                "需要人工确认新基线",
                "当前账实状态需要人工确认。只有在你确认交易所状态符合预期后，才能接受为新基线。",
                "在完成基线确认前，系统不会恢复自动交易。",
                "先查看最新对账，再决定是否接受当前状态为新基线。",
            )
        if code == "reconciliation_halt_required":
            return (
                "最新对账要求暂停交易",
                "最近一次对账发现了高风险差异，系统要求先停下来处理当前账实状态。",
                "在完成对账处理或重新建立基线前，系统不会恢复自动交易。",
                "先查看最新对账，再决定是否重新对账或接受当前状态为新基线。",
            )
        if code == "reconciliation_stale":
            return (
                "对账结果已过期",
                "当前对账结论已经过时，恢复前需要先重新校验账实状态。",
                "如果继续依赖过期对账结果，恢复资格和风险判断都不可信。",
                "先重新对账，再查看最新恢复状态。",
            )
        if code == "kill_switch_active" and is_surface_halt:
            return (
                "系统当前仍处于暂停状态",
                "暂停是保护结果，不是当前最先处理的根因。你应先处理更高优先级的阻断，再决定是否恢复自动运行。",
                "在上游阻断未清除前，即使手动点恢复，也不会真正恢复执行。",
                "先处理当前第一优先级阻断，处理完后再恢复自动运行。",
            )
        if code == "kill_switch_active":
            if recovery.get("resume_eligible"):
                return (
                    "系统处于手动暂停状态",
                    "当前没有更高优先级的恢复阻断。确认无误后可以直接恢复自动运行。",
                    "暂停会阻止系统继续自动交易，但不影响你查看状态和执行人工动作。",
                    "确认当前状态无误后，直接恢复自动运行。",
                )
            return (
                "系统当前已暂停",
                "当前还有其他阻断未处理，暂停只是系统保持安全的表面状态。",
                "在更高优先级阻断未清除前，系统不会继续自动交易。",
                "先处理优先级更高的阻断，再考虑恢复自动运行。",
            )
        if code == "market_data_stale":
            return (
                "行情快照已过期",
                "当前市场数据已不新鲜，系统不能继续基于过期行情做交易判断。",
                "在行情恢复新鲜前，恢复自动交易会放大错误报价和错误决策风险。",
                "先确认行情连接和最新快照是否恢复，再考虑继续自动运行。",
            )
        if code == "market_connection_down":
            return (
                "行情连接已断开",
                "当前行情源不可用，系统无法获取可信的市场状态。",
                "在行情连接恢复前，系统不应继续做自动交易。",
                "先检查行情连接是否恢复，再刷新当前状态。",
            )
        if code == "account_snapshot_missing":
            return (
                "缺少账户快照",
                "系统暂时拿不到可信的账户快照，因此无法确认余额、仓位和挂单状态。",
                "在账户状态缺失时恢复自动交易，容易造成余额和风控判断错误。",
                "先等待账户快照恢复或刷新当前状态，再决定是否继续。",
            )
        if code == "account_state_stale":
            return (
                "账户状态已过期",
                "最近一次账户状态更新已经过期，当前余额、仓位和挂单信息不再可信。",
                "在账户状态过期时，系统不能安全地继续自动交易。",
                "先刷新当前状态，确认账户快照恢复新鲜后再继续。",
            )
        if code == "okx_simulated_trading_required":
            return (
                "OKX 提交通道与当前环境不一致",
                "当前 OKX 执行通道和运行配置不一致。请确认是否误用了模拟盘开关、错误环境的 API Key，或加载了错误的启动档。",
                "在提交通道和凭证环境不一致时，系统不会继续真实报单，以避免把订单发往错误环境或在提交前反复失败。",
                "先核对 simulated/live 开关、API 凭证所属环境和当前启动档；修正后重启服务，再刷新当前状态。",
            )
        if code == "rebaseline_in_progress":
            return (
                "基线确认仍在进行中",
                "系统正在完成基线确认和恢复状态刷新，请等待这一轮操作结束。",
                "在基线确认完成前，不应继续恢复自动交易。",
                "先刷新当前状态，确认这次基线确认是否已完成。",
            )
        if code == "strategy_bundle_recovery_in_progress":
            return (
                "多腿 bundle 仍在恢复中",
                "系统检测到至少一个策略 bundle 仍有未完成腿，当前正在按 bundle / sleeve 维度跟踪恢复，不应立即恢复新的扩张交易。",
                "如果在未完成腿仍未收敛时继续扩张新仓位，后续库存归属、对冲关系和恢复判断都会被污染。",
                "先查看恢复详情，确认这些 bundle 的未完成腿是否已经自然收敛。",
            )
        if code == "strategy_bundle_recovery_requires_review":
            return (
                "多腿 bundle 恢复身份不完整",
                "当前至少有一个未完成 bundle 缺少一致的 sleeve / allocation 身份，系统无法自动确认这些腿该如何恢复。",
                "在 bundle 身份不完整时继续运行，会让多策略库存归属和恢复链路建立在错误假设上。",
                "先查看恢复详情，确认哪些 bundle 缺少身份信息或腿状态异常，再决定如何处理。",
            )
        if submit_only:
            return (
                "当前模式不会真实报单",
                "当前运行模式或执行线路只允许演练，不会把订单提交到交易所。",
                "这不会阻止系统判断，但会阻止真实报单。",
                "确认当前运行模式是否符合你的预期。",
            )
        return (
            code,
            "当前存在一个尚未完成处理的阻断项。",
            "在这条阻断解除前，系统的自动运行能力会受到影响。",
            "先查看阻断详情并按推荐动作处理。",
        )

    @staticmethod
    def _next_step_summary(primary: BlockerControlItem | None, secondary: list[BlockerControlItem]) -> str:
        if primary is None:
            return "当前没有待处理的阻断项。"
        if not secondary:
            return f"先处理“{primary.title}”，处理完成后即可重新评估是否恢复自动运行。"
        return f"先处理“{primary.title}”，处理完成后系统会继续检查剩余 {len(secondary)} 条次级阻断。"
