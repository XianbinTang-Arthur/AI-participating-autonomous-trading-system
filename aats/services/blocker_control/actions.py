from __future__ import annotations

from typing import TYPE_CHECKING

from aats.schemas.blocker_control import BlockerActionExecutionResult

if TYPE_CHECKING:
    from aats.schemas.operator import AuthSource, OperatorRole
    from aats.services.operator.query_service import OperatorQueryService


class BlockerActionService:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    async def execute(
        self,
        *,
        action_id: str,
        panel_version: str | None,
        blocker: str | None,
        parent_intent_id: str | None,
        reason: str,
        actor_role: "OperatorRole",
        actor_identity: str | None = None,
        auth_source: "AuthSource" = "anonymous",
    ) -> BlockerActionExecutionResult:
        snapshot = self.owner._build_blocker_control()
        if panel_version and panel_version != snapshot.panel_version:
            raise ValueError("blocker_control_state_changed")
        if blocker and all(item.blocker != blocker for item in snapshot.blockers):
            raise ValueError(f"blocker_not_active:{blocker}")

        status = "completed"
        if action_id == "reconcile-now":
            await self.owner.validate_reconciliation(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已重新执行对账，并刷新恢复状态。"
        elif action_id == "accept-rebaseline":
            await self.owner.rebaseline(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已接受当前状态为新基线，并重新计算恢复资格。"
        elif action_id == "resume-system":
            await self.owner.resume(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已执行恢复自动运行请求。"
        elif action_id == "halt-system":
            await self.owner.halt(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "系统会继续保持暂停状态。"
        elif action_id == "clear-shadow-cache":
            await self.owner.clear_obligation_cache(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已清除 Redis obligation 缓存并重建本地缓存，幻影 backlog 应已消除。"
        elif action_id == "acknowledge-phase1-shadow":
            self.owner.record_phase1_shadow_review(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已记录影子兼容层人工核查结果，当前阻断会继续保留，直到兼容层恢复正常。"
        elif action_id == "ai-review-restore":
            await self.owner.ai_review_restore(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已确认恢复 AI 决策链路。"
        elif action_id == "ai-review-degrade-to-baseline":
            await self.owner.ai_review_degrade_to_baseline(
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            message = "已将系统切到仅基础策略继续运行。"
        elif action_id == "refresh-exchange-state":
            result = await self.owner.refresh_exchange_state(
                blocker=blocker,
                parent_intent_id=parent_intent_id,
                reason=reason,
                actor_role=actor_role,
                actor_identity=actor_identity,
                auth_source=auth_source,
            )
            status = str(result.get("status") or "completed")
            message = str(result.get("message") or "已刷新交易所状态。")
        else:
            raise ValueError(f"unsupported_blocker_action:{action_id}")

        return BlockerActionExecutionResult(
            action_id=action_id,
            status=status,
            message=message,
            blocker_control=self.owner._build_blocker_control(),
        )
