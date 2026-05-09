from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from aats.schemas.blocker_control import (
    BlockerActionDefinition,
    BlockerControlItem,
    BlockerControlSnapshot,
    BlockerControlTask,
)
from aats.services.blocker_control.priority import blocker_priority
from aats.services.operator._parallel import parallel_fetch

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
        mode_builder = getattr(getattr(self.owner, "recovery_queries", None), "build_system_mode", None)
        recovery_loader = getattr(self.owner, "recovery_view_dashboard", None)
        if not callable(recovery_loader):
            recovery_loader = self.owner.recovery_view
        mode_context_available = (
            callable(mode_builder)
            and hasattr(getattr(self.owner.runtime, "mode_controller", None), "snapshot")
            and hasattr(getattr(self.owner.runtime, "execution_adapter", None), "readiness")
            and hasattr(self.owner, "trial_guard")
        )
        queries = {
            "recovery": recovery_loader,
            "latest_reconciliation": self.owner._latest_scoped_reconciliation,
            "health_snapshot": self.owner.runtime.health_service.snapshot,
            "ai_runtime": self.owner.ai_runtime,
        }
        if mode_context_available:
            queries.update(
                {
                    "mode_snapshot": lambda: dict(self.owner.runtime.mode_controller.snapshot()),
                    "readiness": self.owner.runtime.execution_adapter.readiness,
                    "trial_guard": self.owner.trial_guard,
                }
            )
        r = parallel_fetch(queries)
        recovery = r["recovery"]
        latest_reconciliation = r["latest_reconciliation"]
        health_snapshot = r["health_snapshot"]
        health_blockers = list(getattr(health_snapshot, "blockers", []) or [])
        system_mode = (
            mode_builder(
                recovery=recovery,
                snapshot=r["mode_snapshot"],
                readiness=r["readiness"],
                health_blockers=health_blockers,
                trial_guard=r["trial_guard"],
            )
            if mode_context_available
            else mode_builder(
                recovery=recovery,
                health_blockers=health_blockers,
            )
            if callable(mode_builder)
            else self.owner.system_mode()
        )
        items = self._build_items(
            recovery=recovery,
            health_snapshot=health_snapshot,
            system_mode=system_mode,
            ai_runtime=r["ai_runtime"],
            latest_reconciliation=latest_reconciliation,
        )
        primary, secondary = self._primary_and_secondary_items(items)
        primary_task = self._primary_task(
            primary=primary,
            secondary=secondary,
            recovery=recovery,
            latest_reconciliation=latest_reconciliation,
        )
        payload = {
            "halted": self.owner.runtime.kill_switch.halted,
            "review_required": recovery["review_required"],
            "resume_eligible": recovery["resume_eligible"],
            "safe_to_trade": recovery["safe_to_trade"],
            "blockers": [self._version_payload(item) for item in items],
            "primary_task": self._version_task_payload(primary_task),
        }
        panel_version = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        return BlockerControlSnapshot(
            panel_version=panel_version,
            halted=self.owner.runtime.kill_switch.halted,
            review_required=recovery["review_required"],
            resume_eligible=recovery["resume_eligible"],
            safe_to_trade=recovery["safe_to_trade"],
            primary_blocker=primary,
            secondary_blockers=secondary,
            blockers=items,
            primary_task=primary_task,
            next_step_summary=self._next_step_summary(
                primary,
                secondary,
                recovery=recovery,
                latest_reconciliation=latest_reconciliation,
            ),
        )

    def execution_blocker_summary(
        self,
        *,
        recovery: dict[str, Any],
        submit_blocked_reasons: list[str],
        health_snapshot: Any | None = None,
    ) -> dict[str, Any]:
        if health_snapshot is None:
            health_snapshot = self.owner.runtime.health_service.snapshot()
        blockers: list[tuple[str, bool]] = []

        def _add(code: Any, *, submit_only: bool) -> None:
            normalized = str(code or "").strip()
            if not normalized:
                return
            if any(existing == normalized for existing, _ in blockers):
                return
            blockers.append((normalized, submit_only))

        for code in getattr(health_snapshot, "blockers", []) or []:
            _add(code, submit_only=str(code or "").strip() in self._SUBMIT_ONLY)
        if self.owner.runtime.kill_switch.halted:
            _add("kill_switch_active", submit_only=False)
        for code in submit_blocked_reasons:
            _add(code, submit_only=True)
        for code in recovery.get("resume_blocked_reasons", []) or []:
            _add(code, submit_only=str(code or "").strip() in self._SUBMIT_ONLY)

        return {
            "halted": bool(self.owner.runtime.kill_switch.halted),
            "review_required": bool(recovery.get("review_required")),
            "resume_eligible": bool(recovery.get("resume_eligible")),
            "safe_to_trade": bool(recovery.get("safe_to_trade")),
            "blockers": [
                {
                    "blocker": code,
                    "subsystem": self._subsystem_for(code),
                    "affects_execution": not submit_only,
                    "submit_only": submit_only,
                    "recommended_action": "Inspect subsystem status and operator logs before resuming execution.",
                }
                for code, submit_only in blockers
            ],
            "summary_source": "minimal_execution_blocker_summary",
        }

    def has_active_blocker(self, code: str) -> bool:
        return any(item.blocker == code for item in self.snapshot().blockers)

    def action_target(self, action_id: str) -> tuple[str | None, str | None]:
        snapshot = self.snapshot()
        for item in snapshot.blockers:
            for action in item.actions:
                if action.action_id == action_id:
                    return item.blocker, item.blocker_instance_id
        return None, None

    def _build_items(
        self,
        *,
        recovery: dict[str, Any],
        health_snapshot: Any,
        system_mode: dict[str, Any],
        ai_runtime: dict[str, Any],
        latest_reconciliation: Any | None,
    ) -> list[BlockerControlItem]:
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
        ai_review_blocked = (
            bool(ai_runtime.get("degraded"))
            and not bool(ai_runtime.get("auto_downgrade_active"))
            and str(ai_runtime.get("effective_operating_mode") or "") != "baseline_only"
            and str(ai_runtime.get("manual_override_mode") or "") != "baseline_only"
        )
        if ai_review_blocked and all(existing != "ai_degraded_requires_manual_review" for existing, _, _ in blockers):
            blockers.append(
                (
                    "ai_degraded_requires_manual_review",
                    self._subsystem_for("ai_degraded_requires_manual_review"),
                    False,
                )
            )

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
        items.sort(
            key=lambda item: (
                1 if self._is_surface_halt_item(item) else 0,
                item.priority,
                item.blocker,
            )
        )
        return items

    @staticmethod
    def _version_payload(item: BlockerControlItem) -> dict[str, Any]:
        return {
            "blocker": item.blocker,
            "priority": item.priority,
            "category": item.category,
            "root_cause": item.root_cause,
            "actions": [BlockerControlService._version_action_payload(action) for action in item.actions],
        }

    @staticmethod
    def _version_action_payload(action: BlockerActionDefinition) -> dict[str, Any]:
        return {
            "action_id": action.action_id,
            "label": action.label,
            "kind": action.kind,
            "tone": action.tone,
            "endpoint": action.endpoint,
            "method": action.method,
            "client_action": action.client_action,
            "value": action.value,
            "enabled": action.enabled,
            "disabled_reason": action.disabled_reason,
            "requires_confirmation": action.requires_confirmation,
            "confirmation_title": action.confirmation_title,
            "confirmation_copy": action.confirmation_copy,
            "expected_effect": action.expected_effect,
        }

    @classmethod
    def _version_task_payload(cls, task: BlockerControlTask) -> dict[str, Any]:
        return {
            "kind": task.kind,
            "title": task.title,
            "summary": task.summary,
            "reason": task.reason,
            "completion_outcome": task.completion_outcome,
            "source_blocker": task.source_blocker,
            "secondary_blocker_count": task.secondary_blocker_count,
            "actions": [cls._version_action_payload(action) for action in task.actions],
        }

    @staticmethod
    def _is_surface_halt_item(item: BlockerControlItem) -> bool:
        return item.blocker == "kill_switch_active" and bool(item.derived_from)

    def _primary_and_secondary_items(
        self,
        items: list[BlockerControlItem],
    ) -> tuple[BlockerControlItem | None, list[BlockerControlItem]]:
        if not items:
            return None, []
        primary = next((item for item in items if not self._is_surface_halt_item(item)), None)
        if primary is None:
            primary = items[0]
        secondary = [item for item in items if item.blocker_instance_id != primary.blocker_instance_id]
        return primary, secondary

    def _primary_task(
        self,
        *,
        primary: BlockerControlItem | None,
        secondary: list[BlockerControlItem],
        recovery: dict[str, Any],
        latest_reconciliation: Any | None,
    ) -> BlockerControlTask:
        reconciliation_id = None if latest_reconciliation is None else latest_reconciliation.reconciliation_id
        if primary is not None:
            return BlockerControlTask(
                kind="resolve_blocker",
                title=primary.title,
                summary=primary.recommended_next_step,
                reason=primary.description,
                completion_outcome=(
                    f"处理完后系统还会继续检查剩余 {len(secondary)} 条次级阻断。"
                    if secondary
                    else "处理完成后系统会立即重新评估是否可以恢复自动运行。"
                ),
                source_blocker=primary.blocker,
                secondary_blocker_count=len(secondary),
                actions=list(primary.actions),
            )
        if recovery.get("review_required") or self._should_show_rebaseline_action(
            latest_reconciliation=latest_reconciliation,
            recovery=recovery,
        ):
            return BlockerControlTask(
                kind="review_reconciliation",
                title="先确认当前账实状态",
                summary="先查看最新对账和交易所账单；只有确认当前状态符合预期后，才接受为新基线。",
                reason=(
                    "当前没有新的主阻断，但系统仍处于人工确认流程。常见原因是最近一次对账、基线切换或恢复事件还没有完全收敛。"
                ),
                completion_outcome="确认完成后，系统会重新评估是否能够自动恢复运行。",
                actions=self._generic_task_actions(
                    reconciliation_id=reconciliation_id,
                    recovery=recovery,
                    latest_reconciliation=latest_reconciliation,
                    include_inspect=True,
                    include_validate=True,
                    include_rebaseline=True,
                ),
            )
        if recovery.get("halted") and recovery.get("resume_eligible"):
            return BlockerControlTask(
                kind="resume",
                title="可以直接恢复自动运行",
                summary="当前没有更高优先级阻断。确认最新对账和账户快照无误后，直接恢复自动运行。",
                reason="系统目前只是处于暂停状态，不是因为新的硬阻断被拦停。",
                completion_outcome="恢复后系统会立刻重新校验当前状态，并继续自动运行。",
                actions=self._generic_task_actions(
                    reconciliation_id=reconciliation_id,
                    recovery=recovery,
                    latest_reconciliation=latest_reconciliation,
                    include_resume=True,
                ),
            )
        if bool(getattr(latest_reconciliation, "observational_only", False)) and bool(recovery.get("safe_to_trade")):
            return BlockerControlTask(
                kind="observe",
                title="当前以观察为主",
                summary="当前只有轻度动态漂移，不需要立即重设基线。继续观察保证金、浮盈和仓位快照即可。",
                reason="这类差异通常来自行情变化引起的动态观察值漂移，不代表订单、成交或账务真相已经失真。",
                completion_outcome="如果后续出现结构性或财务差异，系统会自动重新升级处理级别。",
                actions=self._generic_task_actions(
                    reconciliation_id=reconciliation_id,
                    recovery=recovery,
                    latest_reconciliation=latest_reconciliation,
                    include_inspect=True,
                    include_validate=True,
                ),
            )
        if not recovery.get("safe_to_trade"):
            return BlockerControlTask(
                kind="refresh_state",
                title="先确认恢复受限原因",
                summary="当前没有新的主阻断，但系统仍未满足恢复条件。先查看最新对账与恢复限制原因。",
                reason=(
                    "这通常表示系统还在等待某个恢复条件收敛，例如最近一次对账、恢复状态刷新或更上游的运行态检查。"
                ),
                completion_outcome="限制原因消失后，系统会重新计算是否允许恢复自动运行。",
                actions=self._generic_task_actions(
                    reconciliation_id=reconciliation_id,
                    recovery=recovery,
                    latest_reconciliation=latest_reconciliation,
                    include_inspect=True,
                    include_validate=True,
                    # 全新环境首次启动时 Operator 需要能触发 resume 流程来推进
                    # 状态机；后端 /system/resume 会做完整校验（刷新账户快照 →
                    # 对账 → resume_check），不通过时返回具体 blockers。
                    include_resume=True,
                ),
            )
        return BlockerControlTask(
            kind="healthy",
            title="当前无需人工处理",
            summary="当前没有新的第一优先级任务，系统已经具备继续自动运行的条件。",
            reason="最新对账和恢复状态都没有给出新的硬阻断或人工复核要求。",
            completion_outcome="如果仍想再次确认状态，可以手动重新对账（刷新交易所状态）。",
            actions=self._generic_task_actions(
                reconciliation_id=reconciliation_id,
                recovery=recovery,
                latest_reconciliation=latest_reconciliation,
            ),
        )

    def _generic_task_actions(
        self,
        *,
        reconciliation_id: str | None,
        recovery: dict[str, Any],
        latest_reconciliation: Any | None,
        include_inspect: bool = False,
        include_validate: bool = False,
        include_rebaseline: bool = False,
        include_resume: bool = False,
    ) -> list[BlockerActionDefinition]:
        actions: list[BlockerActionDefinition] = []
        if include_inspect and reconciliation_id and self._should_show_inspect_reconciliation_action(
            latest_reconciliation=latest_reconciliation,
            recovery=recovery,
        ):
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
        if include_validate and self._should_show_validate_action(
            latest_reconciliation=latest_reconciliation,
            recovery=recovery,
        ):
            actions.append(
                BlockerActionDefinition(
                    action_id="reconcile-now",
                    label="重新对账（刷新交易所状态）",
                    kind="client",
                    method="CLIENT",
                    client_action="trigger-reconciliation-validate",
                    tone="secondary",
                    expected_effect="立即刷新当前对账结论，并重新计算恢复资格。",
                )
            )
        if include_rebaseline and self._should_show_rebaseline_action(
            latest_reconciliation=latest_reconciliation,
            recovery=recovery,
        ):
            actions.append(
                BlockerActionDefinition(
                    action_id="accept-rebaseline",
                    label="接受当前状态为新基线",
                    kind="client",
                    method="CLIENT",
                    client_action="trigger-rebaseline",
                    tone="warning",
                    requires_confirmation=True,
                    confirmation_title="确认接受当前状态为新的人工基线？",
                    confirmation_copy="只有在你确认交易所当前状态、仓位和挂单都符合预期时，才应执行这一步。",
                    expected_effect="把当前状态接受为新基线，并重新评估恢复资格。",
                )
            )
        if include_resume and self._should_show_resume_action(recovery=recovery):
            # resume_eligible=false 时仍允许点击：后端 /system/resume 会做
            # 完整校验（刷新账户快照 → 对账 → resume_check），不通过时返回
            # 具体 blockers，不会绕过安全检查。禁用按钮只在 halted 且已有
            # 明确 resume_eligible 资格判定时才生效——全新环境下两者都是
            # false，此时应允许 Operator 主动触发完整恢复流程。
            can_attempt_resume = bool(
                recovery.get("resume_eligible")
                or not recovery.get("safe_to_trade")
            )
            actions.append(
                BlockerActionDefinition(
                    action_id="resume-system",
                    label="恢复自动运行",
                    kind="client",
                    method="CLIENT",
                    client_action="trigger-resume",
                    tone="warning",
                    enabled=can_attempt_resume,
                    disabled_reason=(
                        "当前仍有恢复限制，需先处理上游条件后才能恢复自动运行。"
                        if not can_attempt_resume
                        else None
                    ),
                    expected_effect="在没有剩余阻断时解除暂停，恢复自动运行。",
                )
            )
        return actions

    @staticmethod
    def _should_show_validate_action(
        *,
        latest_reconciliation: Any | None,
        recovery: dict[str, Any],
    ) -> bool:
        return bool(
            BlockerControlService._reconciliation_requires_attention(latest_reconciliation)
            or recovery.get("review_required")
            # 全新环境首次启动时 safe_to_trade=false 但没有对账记录也没有
            # review_required，仍需让 Operator 能触发对账来推进恢复状态机。
            or not recovery.get("safe_to_trade")
        )

    @staticmethod
    def _should_show_inspect_reconciliation_action(
        *,
        latest_reconciliation: Any | None,
        recovery: dict[str, Any],
    ) -> bool:
        return bool(
            latest_reconciliation is not None
            and (
                BlockerControlService._reconciliation_requires_attention(latest_reconciliation)
                or recovery.get("review_required")
            )
        )

    @staticmethod
    def _should_show_rebaseline_action(
        *,
        latest_reconciliation: Any | None,
        recovery: dict[str, Any],
    ) -> bool:
        recommended_action = None if latest_reconciliation is None else getattr(
            latest_reconciliation,
            "recommended_operator_action",
            None,
        )
        return bool(
            recovery.get("rebaseline_available")
            or bool(getattr(latest_reconciliation, "review_required", False))
            or ("rebaseline" in str(recommended_action or "").lower())
        )

    @staticmethod
    def _should_show_resume_action(*, recovery: dict[str, Any]) -> bool:
        # 原条件仅 halted || resume_eligible，导致全新环境下
        # safe_to_trade=false 但 halted=false、resume_eligible=false 时按钮消失。
        # 补充 !safe_to_trade 条件，让 Operator 能主动触发 resume 流程
        # （后端 /system/resume 会做完整校验，不会绕过安全检查）。
        return bool(
            recovery.get("halted")
            or recovery.get("resume_eligible")
            or not recovery.get("safe_to_trade")
        )

    @staticmethod
    def _subsystem_for(code: str) -> str:
        if code.startswith("phase1_shadow"):
            return "phase1_shadow"
        if code.startswith("derivatives_"):
            return "risk_control"
        if code.startswith("trial_guard"):
            return "trial_guard"
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
        if BlockerControlService._supports_exchange_state_refresh(code):
            return "manual_or_auto"
        if code == "rebaseline_in_progress":
            return "auto_only"
        return "manual_only"

    @staticmethod
    def _supports_exchange_state_refresh(code: str) -> bool:
        return code in {
            "market_data_stale",
            "market_connection_down",
            "account_state_stale",
            "account_snapshot_missing",
            "derivatives_risk_snapshot_missing_grace_active",
            "derivatives_risk_snapshot_missing_requires_only_reduce",
            "derivatives_risk_snapshot_missing_auto_halt",
        }

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
                    action_id="clear-shadow-cache",
                    label="清除陈旧缓存并解除",
                    endpoint="/system/blocker-actions/clear-shadow-cache",
                    tone="primary",
                    requires_confirmation=True,
                    confirmation_title="确认清除影子兼容层缓存",
                    confirmation_copy="清除 Redis 中的 obligation 缓存并重启缓存。如果阻断是由重启后 Redis 陈旧数据引起的，清除后阻断会在下次刷新时自动解除。",
                    expected_effect="清除 Redis obligation 缓存，消除 cache/DB 不一致造成的幻影 backlog。",
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
            ]
        if code == "trial_guard_threshold_breached":
            return [
                BlockerActionDefinition(
                    action_id="open-strategy-view",
                    label="查看试盘审查",
                    kind="client",
                    method="CLIENT",
                    client_action="navigate-view",
                    value="strategy",
                    tone="warning",
                    expected_effect="切到策略判断页，先查看试盘守护和系统自动试盘结论，确认这次自动停机是硬阈值触发，还是只是观察/放量建议收紧。",
                ),
                BlockerActionDefinition(
                    action_id="open-execution-view",
                    label="查看委托与成交",
                    kind="client",
                    method="CLIENT",
                    client_action="navigate-view",
                    value="execution",
                    tone="ghost",
                    expected_effect="切到委托与成交页，优先核对最近成交、手续费、滑点和成交链路是否符合预期。",
                ),
                BlockerActionDefinition(
                    action_id="reset-trial-guard",
                    label="人工重置试盘守护",
                    kind="client",
                    method="CLIENT",
                    client_action="record-trial-review-action",
                    value="reset_trial_guard",
                    tone="warning",
                    expected_effect="在确认这次硬停机已经完成复盘后，把试盘守护切回新的观察窗口；系统仍会保持暂停，后续还需要人工点击恢复。",
                ),
                BlockerActionDefinition(
                    action_id="refresh-dashboard",
                    label="刷新当前状态",
                    kind="client",
                    method="CLIENT",
                    client_action="refresh-dashboard",
                    tone="secondary",
                    expected_effect="重新拉取试盘守护、恢复状态和最近成交摘要，确认试盘守护是否仍然处于 breached。",
                ),
            ]
        if code in {
            "derivatives_risk_snapshot_missing_grace_active",
            "derivatives_risk_snapshot_missing_requires_only_reduce",
            "derivatives_risk_snapshot_missing_auto_halt",
        }:
            return [
                BlockerActionDefinition(
                    action_id="open-execution-view",
                    label="查看委托与成交",
                    kind="client",
                    method="CLIENT",
                    client_action="navigate-view",
                    value="execution",
                    tone="ghost",
                    expected_effect="切到委托与成交页，先确认最近的挂单、成交和异常是否已经真实收敛，再决定是否继续刷新交易所状态。",
                ),
                BlockerActionDefinition(
                    action_id="refresh-exchange-state",
                    endpoint="/system/blocker-actions/refresh-exchange-state",
                    tone="secondary",
                    label="刷新交易所状态",
                    expected_effect="立刻拉取最新行情、账户与风险快照，并在本轮内按最大次数重试，确认当前风险快照阻断是否已经解除。",
                ),
            ]
        if reconciliation_id and self._should_show_inspect_reconciliation_action(
            latest_reconciliation=latest_reconciliation,
            recovery=recovery,
        ):
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
                    kind="client",
                    method="CLIENT",
                    client_action="trigger-reconciliation-validate",
                    tone="secondary",
                    expected_effect="立即刷新当前对账结果，并重新计算恢复资格。",
                )
            )
        if code in {"reconciliation_halt_required", "operator_rebaseline_required"} and recovery.get("rebaseline_available"):
            actions.append(
                BlockerActionDefinition(
                    action_id="accept-rebaseline",
                    label="确认为新基线",
                    kind="client",
                    method="CLIENT",
                    client_action="trigger-rebaseline",
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
                    value="aiAnalysis",
                    tone="ghost",
                    expected_effect="切到 AI 工作台查看最近影子评估、降级原因和窗口统计。",
                )
            )
        if code == "kill_switch_active":
            actions.append(
                BlockerActionDefinition(
                    action_id="resume-system",
                    label="恢复自动运行",
                    kind="client",
                    method="CLIENT",
                    client_action="trigger-resume",
                    tone="warning",
                    enabled=bool(recovery.get("resume_eligible")),
                    disabled_reason="当前还有更高优先级阻断未处理。" if not recovery.get("resume_eligible") else None,
                    expected_effect="在没有其他阻断时解除暂停，恢复自动运行。",
                )
            )
        if code in {"strategy_bundle_recovery_in_progress", "strategy_bundle_recovery_requires_review"}:
            actions.append(
                BlockerActionDefinition(
                    action_id="open-execution-view",
                    label="查看委托与成交",
                    kind="client",
                    method="CLIENT",
                    client_action="navigate-view",
                    value="execution",
                    tone="warning",
                    expected_effect="切到委托与成交页，先确认当前 bundle 对应的委托、成交和异常有没有真正收敛。",
                )
            )
        if self._supports_exchange_state_refresh(code):
            actions.append(
                BlockerActionDefinition(
                    action_id="refresh-exchange-state",
                    label="刷新交易所状态",
                    endpoint="/system/blocker-actions/refresh-exchange-state",
                    tone="secondary",
                    expected_effect="立即拉取最新行情、账户与风险快照，并在本轮内按最大次数重试，确认当前阻断是否已经解除。",
                )
            )
        elif code == "rebaseline_in_progress":
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
        if code == "trial_guard_threshold_breached":
            trial_guard_getter = getattr(self.owner, "trial_guard", None)
            trial_guard = trial_guard_getter() if callable(trial_guard_getter) else {}
            hard_stop = dict(trial_guard.get("hard_stop") or {})
            breaches = list(trial_guard.get("breaches") or [])
            recovery_requirements = list((trial_guard.get("recovery_requirements") or {}).get("items") or [])
            breach_summary = "；".join(str(item.get("title") or item.get("code") or "") for item in breaches if item)
            recovery_summary = "；".join(
                str(item.get("requirement") or "")
                for item in recovery_requirements
                if item
            )
            return (
                "试盘守护已触发自动停机",
                hard_stop.get("summary")
                or breach_summary
                or "最近一轮小资金试盘已经命中试盘守护硬停机阈值，系统会自动暂停，避免继续扩大风险。",
                "当试盘守护仍处于 breached 时，即使手动点击恢复，后台轮询也会再次把系统停回去；如果确认要重新开始采样，应先人工重置试盘守护。",
                recovery_summary
                or "先查看试盘审查和最近成交，确认触发阈值为什么命中，以及这些条件是否已经自然解除。",
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
    def _next_step_summary(
        primary: BlockerControlItem | None,
        secondary: list[BlockerControlItem],
        *,
        recovery: dict[str, Any],
        latest_reconciliation: Any | None,
    ) -> str:
        if primary is None:
            if bool(getattr(latest_reconciliation, "observational_only", False)) and bool(recovery.get("safe_to_trade")):
                return "当前没有新的主阻断。最新对账只有轻度动态漂移，系统可继续运行，建议持续观察保证金、浮盈和仓位快照。"
            if bool(recovery.get("review_required")):
                return "当前没有新的主阻断，但系统仍处于人工确认流程。请优先查看最新对账、恢复状态和交易所账单，确认是否还有未收敛的复核条件。"
            if bool(recovery.get("halted")) and bool(recovery.get("resume_eligible")):
                return "当前没有新的主阻断。系统处于手动暂停状态，确认最新对账和账户快照无误后即可恢复自动运行。"
            if not bool(recovery.get("safe_to_trade")):
                if recovery.get("resume_blocked_reasons"):
                    return "当前没有新的主阻断，但系统仍未满足恢复条件。请先处理恢复状态中的限制原因。"
                return "当前没有新的主阻断，但系统仍未恢复到可自动运行状态。请先查看恢复状态和最新对账。"
            return "当前没有待处理的阻断项。"
        if not secondary:
            return f"先处理“{primary.title}”，处理完成后即可重新评估是否恢复自动运行。"
        return f"先处理“{primary.title}”，处理完成后系统会继续检查剩余 {len(secondary)} 条次级阻断。"

    @staticmethod
    def _reconciliation_requires_attention(latest_reconciliation: Any | None) -> bool:
        if latest_reconciliation is None:
            return False
        severity = str(getattr(latest_reconciliation, "severity", "") or "").upper()
        return bool(
            getattr(latest_reconciliation, "halt_required", False)
            or getattr(latest_reconciliation, "review_required", False)
            or severity not in {"", "CLEAN"}
        )
