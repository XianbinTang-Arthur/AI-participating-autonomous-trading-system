from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger, log_event
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import EventEnvelope, utc_now
from aats.schemas.operator import (
    BlockerSnapshotRecord,
    ExecutionErrorSummary,
    OperatorActionRecord,
    OperatorRole,
    ReconciliationValidationSummary,
    ReplayValidationSummary,
)
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.services.reconciliation_service.replay import ReplayEngine, ReplayResult

if TYPE_CHECKING:
    from aats.bootstrap.config import ApplicationRuntime


class OperatorQueryService:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self.runtime = runtime
        self.logger = get_logger("aats.operator_api")

    def payload(self, envelope: EventEnvelope | None) -> dict[str, Any] | None:
        if envelope is None:
            return None
        payload = dict(envelope.payload)
        payload["_event_id"] = envelope.event_id
        payload["_topic"] = envelope.topic
        return payload

    def payload_by_ref(self, ref: str | None) -> dict[str, Any] | None:
        if ref is None:
            return None
        return self.payload(self.runtime.event_store.get(ref))

    def payloads_by_refs(self, refs: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ref in refs:
            payload = self.payload_by_ref(ref)
            if payload is not None:
                rows.append(payload)
        return rows

    def latest_order(self):
        rows = self.runtime.execution_repo.order_states()
        return max(rows, key=lambda item: item.last_update_ts or item.created_at, default=None)

    def latest_fill(self):
        rows = self.runtime.execution_repo.fills()
        return max(rows, key=lambda item: item.ingestion_timestamp, default=None)

    def recent_fills(self, *, limit: int = 50):
        return sorted(
            self.runtime.execution_repo.fills(),
            key=lambda item: (item.ingestion_timestamp, item.fill_id),
            reverse=True,
        )[:limit]

    def latest_decision_id(self) -> str | None:
        latest = max(self.runtime.audit_repo.all(), key=lambda item: item.created_at, default=None)
        return latest.decision_id if latest is not None else None

    def system_mode(self) -> dict[str, Any]:
        snapshot = dict(self.runtime.mode_controller.snapshot())
        readiness = self.runtime.execution_adapter.readiness()
        submit_blocked_reasons = list(
            dict.fromkeys(
                list(snapshot.get("submit_blocked_reasons", []))
                + list(readiness.get("submit_blocked_reasons", []))
            )
        )
        health_blockers = list(dict.fromkeys(self.runtime.health_service.execution_blockers()))
        exchange_submit_allowed = bool(
            readiness.get("exchange_submit_allowed", snapshot.get("exchange_submit_allowed", False))
        )
        execution_blockers = list(health_blockers)
        if self.runtime.kill_switch.halted and "kill_switch_active" not in execution_blockers:
            execution_blockers.insert(0, "kill_switch_active")

        guarded_exchange_mode = self.runtime.mode_controller.mode == "guarded_live"
        if guarded_exchange_mode:
            execution_blockers = list(dict.fromkeys(execution_blockers + submit_blocked_reasons))

        snapshot["exchange_submit_allowed"] = exchange_submit_allowed
        snapshot["submit_blocked"] = bool(submit_blocked_reasons) or not exchange_submit_allowed
        snapshot["submit_blocked_reasons"] = submit_blocked_reasons
        snapshot["execution_blocked"] = bool(execution_blockers)
        snapshot["blocked_reason"] = execution_blockers[0] if execution_blockers else None
        return snapshot

    def system_health(self) -> dict[str, Any]:
        snapshot = self.runtime.health_service.snapshot()
        mode_snapshot = self.system_mode()
        market = self.runtime.market_gateway.status()
        account = self.runtime.account_service.status()
        execution = self.runtime.execution_adapter.readiness()
        latest_reconciliation = self.runtime.reconciliation_repo.latest()
        latest_portfolio = self.runtime.portfolio_repo.latest()
        blockers = self.blockers()
        warnings = [
            {
                "component": component.component,
                "detail": component.detail,
                "blockers": component.blockers,
            }
            for component in snapshot.components
            if component.status == "warn"
        ]
        if self.runtime.kill_switch.halted:
            runtime_state = "halted"
        elif any(item["affects_execution"] for item in blockers):
            runtime_state = "blocked"
        elif warnings:
            runtime_state = "degraded"
        else:
            runtime_state = "healthy"
        self._persist_blocker_snapshot(
            source="system_health",
            runtime_state=runtime_state,
            mode_snapshot=mode_snapshot,
            blockers=blockers,
        )
        return {
            "overall_status": snapshot.status,
            "runtime_state": runtime_state,
            "operating_state": snapshot.operating_state,
            "mode": snapshot.mode,
            "halted": self.runtime.kill_switch.halted,
            "blockers": blockers,
            "warnings": warnings,
            "execution_blocked": mode_snapshot["execution_blocked"],
            "submit_blocked": mode_snapshot["submit_blocked"],
            "submit_blocked_reasons": mode_snapshot["submit_blocked_reasons"],
            "subsystems": {
                "market_data": market,
                "account_state": account,
                "execution_adapter": execution,
                "reconciliation": {
                    "ready": latest_reconciliation is not None and not latest_reconciliation.halt_required,
                    "fresh": latest_reconciliation is not None,
                    "last_update_ts": latest_reconciliation.as_of_ts if latest_reconciliation else None,
                    "severity": latest_reconciliation.severity if latest_reconciliation else None,
                    "halt_required": latest_reconciliation.halt_required if latest_reconciliation else False,
                },
                "storage": {
                    "ready": True,
                    "fresh": True,
                    "detail": self.runtime.settings.storage_mode,
                },
                "audit_replay": {
                    "ready": True,
                    "fresh": bool(self.runtime.replay_validation_history),
                    "audit_record_count": self.runtime.audit_repo.count(),
                    "last_replay_validation": (
                        self.runtime.replay_validation_history[-1]
                        if self.runtime.replay_validation_history
                        else None
                    ),
                },
            },
            "freshness": {
                "market_fresh": market.get("fresh", False),
                "account_fresh": account.get("fresh", False),
                "reconciliation_fresh": latest_reconciliation is not None,
            },
            "last_success_timestamps": {
                "market": market.get("last_update_ts"),
                "account": account.get("last_update_ts"),
                "portfolio": latest_portfolio.snapshot_ts if latest_portfolio else None,
                "reconciliation": latest_reconciliation.as_of_ts if latest_reconciliation else None,
            },
            "recovery": self.runtime.recovery_status.model_dump(mode="json"),
            "mode_contract": mode_snapshot,
        }

    def system_runtime(self) -> dict[str, Any]:
        latest_decision = self.runtime.event_store.latest(topics.DECISION_CONTEXTS)
        latest_fill = self.latest_fill()
        latest_reconciliation = self.runtime.reconciliation_repo.latest()
        now = utc_now()
        return {
            "symbols": [self.runtime.settings.default_symbol],
            "enabled_timeframes": list(self.runtime.settings.enabled_decision_timeframes),
            "decision_cadence": {
                "decision_min_interval_seconds_15m": self.runtime.settings.decision_min_interval_seconds_15m,
                "decision_min_interval_seconds_1h": self.runtime.settings.decision_min_interval_seconds_1h,
                "decision_min_price_move_bps": self.runtime.settings.decision_min_price_move_bps,
                "decision_min_momentum_delta": self.runtime.settings.decision_min_momentum_delta,
                "max_decisions_per_minute": self.runtime.settings.max_decisions_per_minute,
            },
            "storage_mode": self.runtime.settings.storage_mode,
            "operator_auth_enabled": self.runtime.settings.operator_auth_enabled,
            "startup_timestamp": self.runtime.started_at,
            "uptime_seconds": max((now - self.runtime.started_at).total_seconds(), 0.0),
            "last_decision_timestamp": latest_decision.event_timestamp if latest_decision else None,
            "last_fill_timestamp": latest_fill.ingestion_timestamp if latest_fill else None,
            "last_reconciliation_timestamp": latest_reconciliation.as_of_ts if latest_reconciliation else None,
        }

    def decision_view(self, decision_id: str) -> dict[str, Any]:
        audit = self.runtime.audit_repo.get(decision_id)
        if audit is None:
            raise KeyError(f"decision_not_found:{decision_id}")
        decision_context = self.payload_by_ref(audit.decision_context_ref)
        health_snapshot = None
        if decision_context is not None:
            health_snapshot = self.payload_by_ref(decision_context.get("health_snapshot_ref"))
        order_intents = self.payloads_by_refs(audit.order_intent_refs)
        order_updates = self.payloads_by_refs(audit.order_state_refs)
        fills = self.payloads_by_refs(audit.fill_event_refs)
        reconciliations = self.payloads_by_refs(audit.reconciliation_refs)
        return {
            "decision_id": decision_id,
            "health_snapshot": health_snapshot,
            "decision_context": decision_context,
            "baseline_assessment": self.payload_by_ref(audit.baseline_assessment_ref),
            "ai_assessment": self.payload_by_ref(audit.ai_market_assessment_ref),
            "position_target": self.payload_by_ref(audit.position_target_ref),
            "policy_decision": self.payload_by_ref(audit.policy_decision_ref),
            "risk_decision": self.payload_by_ref(audit.risk_decision_ref),
            "execution_plan": self.payload_by_ref(audit.execution_plan_ref),
            "audit": audit.model_dump(mode="json"),
            "latest_order_intent": order_intents[-1] if order_intents else None,
            "latest_order_update": order_updates[-1] if order_updates else None,
            "latest_fill_event": fills[-1] if fills else None,
            "latest_reconciliation": reconciliations[-1] if reconciliations else None,
            "order_intents": order_intents,
            "order_updates": order_updates,
            "fills": fills,
            "portfolio_snapshot": self.payload_by_ref(audit.portfolio_delta_ref),
            "reconciliations": reconciliations,
        }

    def recent_decisions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = sorted(self.runtime.audit_repo.all(), key=lambda item: item.created_at, reverse=True)[:limit]
        payloads: list[dict[str, Any]] = []
        for record in rows:
            context = self.payload_by_ref(record.decision_context_ref)
            target = self.payload_by_ref(record.position_target_ref)
            policy = self.payload_by_ref(record.policy_decision_ref)
            risk = self.payload_by_ref(record.risk_decision_ref)
            payloads.append(
                {
                    "decision_id": record.decision_id,
                    "symbol": context.get("symbol") if context else None,
                    "timeframe": context.get("timeframe") if context else None,
                    "decision_time": context.get("as_of_ts") if context else None,
                    "target_delta_qty": target.get("delta_position_qty") if target else None,
                    "policy_result": policy.get("execution_allowed") if policy else None,
                    "risk_result": risk.get("approved") if risk else None,
                    "execution_result": {
                        "order_count": len(record.order_intent_refs),
                        "fill_count": len(record.fill_event_refs),
                        "reconciled": bool(record.reconciliation_refs),
                    },
                }
            )
        return payloads

    def latest_decision(self) -> dict[str, Any]:
        decision_id = self.latest_decision_id()
        if decision_id is None:
            return {
                "decision_id": None,
                "decision_context": None,
                "baseline_assessment": None,
                "ai_assessment": None,
                "position_target": None,
                "policy_decision": None,
                "risk_decision": None,
                "execution_plan": None,
                "audit": None,
                "order_intents": [],
                "order_updates": [],
                "fills": [],
                "portfolio_snapshot": None,
                "reconciliations": [],
                "summary": None,
            }
        detail = self.decision_view(decision_id)
        detail["summary"] = next(
            (item for item in self.recent_decisions(limit=1) if item["decision_id"] == decision_id),
            None,
        )
        return detail

    def latest_risk(self) -> dict[str, Any]:
        return self._latest_topic_summary(topics.RISK_DECISIONS)

    def recent_risks(self, *, limit: int = 20) -> dict[str, Any]:
        return {"risks": self._recent_topic_summaries(topics.RISK_DECISIONS, limit=limit)}

    def latest_policy(self) -> dict[str, Any]:
        return self._latest_topic_summary(topics.POLICY_DECISIONS)

    def recent_policies(self, *, limit: int = 20) -> dict[str, Any]:
        return {"policies": self._recent_topic_summaries(topics.POLICY_DECISIONS, limit=limit)}

    def blockers(self) -> list[dict[str, Any]]:
        snapshot = self.runtime.health_service.snapshot()
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for blocker in snapshot.blockers:
            subsystem = "system"
            if blocker.startswith("market_"):
                subsystem = "market_data"
            elif blocker.startswith("account_"):
                subsystem = "account_state"
            elif blocker.startswith("reconciliation_"):
                subsystem = "reconciliation"
            elif blocker.startswith("kill_switch"):
                subsystem = "execution_control"
            elif blocker.startswith("guarded_") or blocker.startswith("live_submit"):
                subsystem = "execution_adapter"
            rows.append(self._blocker_entry(blocker=blocker, subsystem=subsystem))
            seen.add(blocker)
        if self.runtime.kill_switch.halted and "kill_switch_active" not in seen:
            rows.insert(0, self._blocker_entry(blocker="kill_switch_active", subsystem="execution_control"))
            seen.add("kill_switch_active")
        for blocker in self.system_mode()["submit_blocked_reasons"]:
            if blocker in seen:
                continue
            subsystem = "execution_adapter"
            if blocker in {"local_demo_no_exchange_submission", "real_market_paper_uses_local_paper_execution"}:
                subsystem = "mode"
            rows.append(self._blocker_entry(blocker=blocker, subsystem=subsystem, submit_only=True))
        return rows

    def blocker_history(self, *, limit: int = 20) -> dict[str, Any]:
        return {
            "history": [
                item.payload
                for item in self.runtime.event_store.by_topic(topics.BLOCKER_SNAPSHOTS)[-limit:]
            ]
        }

    def metrics(self) -> dict[str, Any]:
        snapshot = self.runtime.portfolio_repo.latest()
        metrics = self.runtime.metrics.snapshot()
        return {
            "decision_cycle_count": metrics.get("decision_cycles", 0),
            "order_intent_count": metrics.get("order_intents_generated", 0),
            "fill_count": len(self.runtime.execution_repo.fills()),
            "rejection_count": len(self.runtime.execution_repo.recent_order_states(limit=200, statuses=("FAILED", "REJECTED", "BLOCKED"))),
            "reconciliation_mismatch_count": metrics.get("reconciliation_mismatches", 0),
            "current_open_order_count": len(self.runtime.execution_repo.open_order_states()),
            "recent_execution_errors": self.execution_errors()["errors"][:10],
            "exposure_summary": None if snapshot is None else {
                "gross_exposure": snapshot.gross_exposure,
                "net_exposure": snapshot.net_exposure,
                "total_equity": snapshot.total_equity,
            },
        }

    def portfolio_latest(self) -> dict[str, Any]:
        snapshot = self.runtime.portfolio_repo.latest()
        return {
            "portfolio": snapshot.model_dump(mode="json") if snapshot is not None else None,
            "latest_update_timestamp": snapshot.snapshot_ts if snapshot is not None else None,
        }

    def portfolio_history(self, *, limit: int = 20) -> dict[str, Any]:
        history = self.runtime.portfolio_repo.history()
        return {
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in history[-limit:]],
            "total_available": len(history),
        }

    def balances(self) -> dict[str, Any]:
        snapshot = self.runtime.portfolio_repo.latest()
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_balances": snapshot.balances if snapshot is not None else {},
            "exchange_balances": [item.model_dump(mode="json") for item in exchange.balances] if exchange is not None else [],
        }

    def positions(self) -> dict[str, Any]:
        snapshot = self.runtime.portfolio_repo.latest()
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_positions": [item.model_dump(mode="json") for item in snapshot.positions] if snapshot is not None else [],
            "exchange_positions": [item.model_dump(mode="json") for item in exchange.positions] if exchange is not None else [],
        }

    def account_state(self) -> dict[str, Any]:
        status = self.runtime.account_service.status()
        return {
            "backend": self.runtime.settings.account_backend,
            "read_enabled": self.runtime.settings.account_read_enabled,
            "last_refresh_timestamp": status.get("last_update_ts"),
            "fresh": status.get("fresh", False),
            "connected": status.get("connected", False),
            "ready": status.get("ready", False),
            "last_error": status.get("last_error"),
            "blockers": status.get("blockers", []),
            "current_blocking_reason": next(iter(status.get("blockers", [])), None),
            "detail": status.get("detail"),
        }

    def account_open_orders(self) -> dict[str, Any]:
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_open_orders": [order.model_dump(mode="json") for order in self.runtime.execution_repo.open_order_states()],
            "exchange_open_orders": [order.model_dump(mode="json") for order in exchange.open_orders] if exchange is not None else [],
        }

    def account_recent_fills(self) -> dict[str, Any]:
        exchange = self.runtime.account_service.latest_snapshot()
        return {
            "local_fills": [fill.model_dump(mode="json") for fill in self.recent_fills(limit=50)],
            "exchange_fills": [fill.model_dump(mode="json") for fill in exchange.fills[:50]] if exchange is not None else [],
        }

    def orders_open(self) -> dict[str, Any]:
        return self.account_open_orders()

    def orders_recent(self, *, limit: int = 50) -> dict[str, Any]:
        return {"orders": [order.model_dump(mode="json") for order in self.runtime.execution_repo.recent_order_states(limit=limit)]}

    def order_detail(self, client_order_id: str) -> dict[str, Any]:
        order = next(
            (item for item in self.runtime.execution_repo.order_states() if item.client_order_id == client_order_id),
            None,
        )
        if order is None:
            raise KeyError(f"order_not_found:{client_order_id}")
        return {
            "order": order.model_dump(mode="json"),
            "fills": [fill.model_dump(mode="json") for fill in self.runtime.execution_repo.fills_for_order(client_order_id)],
        }

    def fills_recent(self, *, limit: int = 50) -> dict[str, Any]:
        return {"fills": [fill.model_dump(mode="json") for fill in self.recent_fills(limit=limit)]}

    def fill_detail(self, fill_id: str) -> dict[str, Any]:
        fill = next((item for item in self.runtime.execution_repo.fills() if item.fill_id == fill_id), None)
        if fill is None:
            raise KeyError(f"fill_not_found:{fill_id}")
        return {"fill": fill.model_dump(mode="json")}

    def execution_latest(self) -> dict[str, Any]:
        latest_order = self.latest_order()
        latest_fill = self.latest_fill()
        latest_reconciliation = self.runtime.reconciliation_repo.latest()
        return {
            "mode": self.system_mode(),
            "execution": self.runtime.execution_adapter.readiness(),
            "latest_order": latest_order.model_dump(mode="json") if latest_order is not None else None,
            "latest_fill": latest_fill.model_dump(mode="json") if latest_fill is not None else None,
            "latest_reconciliation": latest_reconciliation.model_dump(mode="json") if latest_reconciliation is not None else None,
            "recent_failures": self.execution_errors()["errors"],
            "recovery": self.runtime.recovery_status.model_dump(mode="json"),
        }

    def execution_errors(self) -> dict[str, Any]:
        persisted = [
            item.payload
            for item in self.runtime.event_store.by_topic(topics.EXECUTION_ERROR_SUMMARIES)[-20:]
        ]
        if persisted:
            return {"errors": persisted}
        errors = []
        for order in self.runtime.execution_repo.recent_order_states(limit=20, statuses=("FAILED", "REJECTED", "BLOCKED")):
            errors.append(
                {
                    "timestamp": order.last_update_ts or order.created_at,
                    "subsystem": "execution_engine",
                    "severity": "error" if order.status == "FAILED" else "warning",
                    "message": order.execution_error or order.cancel_reason or order.status,
                    "decision_id": order.decision_id,
                    "order_id": order.client_order_id,
                    "status": order.status,
                }
            )
        return {"errors": errors}

    def reconciliation_latest(self) -> dict[str, Any]:
        report = self.runtime.reconciliation_repo.latest()
        latest_validation = self.runtime.event_store.latest(topics.RECONCILIATION_VALIDATIONS)
        return {
            "reconciliation": report.model_dump(mode="json") if report is not None else None,
            "mismatch_summary": self._reconciliation_mismatch_summary(report),
            "latest_validation": latest_validation.payload if latest_validation is not None else None,
        }

    def reconciliation_recent(self, *, limit: int = 20) -> dict[str, Any]:
        history = self.runtime.reconciliation_repo.history()
        return {"reconciliations": [report.model_dump(mode="json") for report in history[-limit:]]}

    def reconciliation_mismatches(self, *, limit: int = 20) -> dict[str, Any]:
        reports = [report for report in self.runtime.reconciliation_repo.history() if report.severity != "CLEAN"][-limit:]
        return {"mismatches": [self._reconciliation_mismatch_summary(report) for report in reports]}

    def reconciliation_detail(self, reconciliation_id: str) -> dict[str, Any]:
        report = next((item for item in self.runtime.reconciliation_repo.history() if item.reconciliation_id == reconciliation_id), None)
        if report is None:
            raise KeyError(f"reconciliation_not_found:{reconciliation_id}")
        return {
            "reconciliation": report.model_dump(mode="json"),
            "mismatch_summary": self._reconciliation_mismatch_summary(report),
        }

    def audit_latest(self) -> dict[str, Any]:
        latest = max(self.runtime.audit_repo.all(), key=lambda item: item.created_at, default=None)
        return {"audit": latest.model_dump(mode="json") if latest is not None else None}

    def audit_detail(self, decision_id: str) -> dict[str, Any]:
        detail = self.decision_view(decision_id)
        return {
            "audit": detail["audit"],
            "history_length": len(self.runtime.audit_repo.history(decision_id)),
            "linked_events": {
                "decision_context": detail["decision_context"],
                "baseline_assessment": detail["baseline_assessment"],
                "ai_assessment": detail["ai_assessment"],
                "position_target": detail["position_target"],
                "policy_decision": detail["policy_decision"],
                "risk_decision": detail["risk_decision"],
                "execution_plan": detail["execution_plan"],
                "order_intents": detail["order_intents"],
                "order_updates": detail["order_updates"],
                "fills": detail["fills"],
                "portfolio_snapshot": detail["portfolio_snapshot"],
                "reconciliations": detail["reconciliations"],
            },
        }

    def replay_status(self) -> dict[str, Any]:
        persisted = self.runtime.event_store.by_topic(topics.REPLAY_VALIDATIONS)
        latest = persisted[-1].payload if persisted else (
            self.runtime.replay_validation_history[-1] if self.runtime.replay_validation_history else None
        )
        recent = [item.payload for item in persisted[-10:]] if persisted else list(self.runtime.replay_validation_history[-10:])
        return {
            "supported": True,
            "healthy": latest is None or latest["divergence_count"] == 0,
            "last_validation": latest,
            "recent_validations": recent,
        }

    def replay_validate(self, *, decision_id: str) -> dict[str, Any]:
        engine = ReplayEngine(
            event_store=self.runtime.event_store,
            reconstruction_service=PortfolioReconstructionService(
                initial_usdt_balance=self.runtime.settings.initial_usdt_balance,
                snapshot_builder=self.runtime.portfolio_service.snapshot_builder,
            ),
            audit_repo=self.runtime.audit_repo,
            portfolio_repo=self.runtime.portfolio_repo,
        )
        result = engine.replay(decision_id=decision_id)
        summary = self._replay_summary(result)
        self._append_event(
            topic=topics.REPLAY_VALIDATIONS,
            key=decision_id or "all",
            payload_model=ReplayValidationSummary(**summary),
        )
        self.runtime.replay_validation_history.append(summary)
        self.runtime.replay_validation_history[:] = self.runtime.replay_validation_history[-20:]
        return summary

    def replay_recent_validations(self) -> dict[str, Any]:
        persisted = self.runtime.event_store.by_topic(topics.REPLAY_VALIDATIONS)
        if persisted:
            return {"validations": [item.payload for item in persisted[-20:]]}
        return {"validations": list(self.runtime.replay_validation_history[-20:])}

    async def validate_reconciliation(
        self,
        *,
        reason: str,
        actor_role: OperatorRole,
    ) -> dict[str, Any]:
        report = await self.runtime.reconciliation_service.validate_now(reason=reason)
        summary = ReconciliationValidationSummary(
            trigger=reason,
            reconciliation_id=report.reconciliation_id,
            decision_id=report.decision_id,
            severity=report.severity,
            halt_required=report.halt_required,
            exchange_comparison_enabled=report.exchange_comparison_enabled,
            mismatch_reasons=list(report.mismatch_reasons),
            safety_impacts=list(report.safety_impacts),
            validated_at=utc_now(),
        )
        self._append_event(
            topic=topics.RECONCILIATION_VALIDATIONS,
            key=report.decision_id or "portfolio",
            payload_model=summary,
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="reconciliation_validate",
                actor_role=actor_role,
                reason=reason,
                status="completed",
                decision_id=report.decision_id,
            ),
        )
        self._persist_blocker_snapshot(
            source="reconciliation_validate",
            runtime_state=self.system_health()["runtime_state"],
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {
            "reconciliation": report.model_dump(mode="json"),
            "validation": summary.model_dump(mode="json"),
        }

    def halt(self, *, reason: str, actor_role: OperatorRole = "anonymous") -> dict[str, Any]:
        was_halted = self.runtime.kill_switch.halted
        self.runtime.kill_switch.halt(reason=reason)
        log_event(self.logger, "operator_halt", level="warning", reason=reason, already_halted=was_halted)
        status = "already_halted" if was_halted else "halted"
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="halt",
                actor_role=actor_role,
                reason=reason,
                status=status,
            ),
        )
        self._persist_blocker_snapshot(
            source="operator_halt",
            runtime_state="halted",
            mode_snapshot=self.system_mode(),
            blockers=self.blockers(),
        )
        return {"status": status, "halted": True, "reason": reason}

    def resume(self, *, reason: str, actor_role: OperatorRole = "anonymous") -> dict[str, Any]:
        was_halted = self.runtime.kill_switch.halted
        self.runtime.kill_switch.resume()
        mode_state = self.system_mode()
        blockers = self.blockers()
        status = "already_resumed" if not was_halted else "resumed"
        log_event(
            self.logger,
            "operator_resume",
            level="info",
            reason=reason,
            was_halted=was_halted,
            blockers=[item["blocker"] for item in blockers],
        )
        self._append_event(
            topic=topics.OPERATOR_ACTIONS,
            key="system",
            payload_model=OperatorActionRecord(
                action="resume",
                actor_role=actor_role,
                reason=reason,
                status=status,
            ),
        )
        self._persist_blocker_snapshot(
            source="operator_resume",
            runtime_state="blocked" if mode_state["execution_blocked"] else "healthy",
            mode_snapshot=mode_state,
            blockers=blockers,
        )
        return {
            "status": status,
            "halted": False,
            "reason": reason,
            "runnable": not mode_state["execution_blocked"],
            "blockers": blockers,
        }

    def _latest_topic_summary(self, topic: str) -> dict[str, Any]:
        envelope = self.runtime.event_store.latest(topic)
        if envelope is None:
            return {"decision_id": None, "payload": None}
        decision_id = envelope.payload.get("decision_id")
        return {
            "decision_id": decision_id if isinstance(decision_id, str) else None,
            "payload": self.payload(envelope),
        }

    def _recent_topic_summaries(self, topic: str, *, limit: int) -> list[dict[str, Any]]:
        return [self.payload(item) for item in reversed(self.runtime.event_store.by_topic(topic)[-limit:])]

    @staticmethod
    def _reconciliation_mismatch_summary(report) -> dict[str, Any] | None:
        if report is None:
            return None
        return {
            "reconciliation_id": report.reconciliation_id,
            "severity": report.severity,
            "halt_required": report.halt_required,
            "mismatch_reasons": report.mismatch_reasons,
            "safety_impacts": report.safety_impacts,
            "exchange_comparison_enabled": report.exchange_comparison_enabled,
        }

    @staticmethod
    def _replay_summary(result: ReplayResult) -> dict[str, Any]:
        return {
            "validated_at": utc_now(),
            "decision_id": result.selected_decision_id,
            "replayed_event_count": result.replayed_event_count,
            "stored_snapshot_count": result.stored_snapshot_count,
            "divergence_count": result.divergence_count,
            "portfolio_issues": result.portfolio_issues,
            "decision_chain_issues": result.decision_chain_issues,
            "execution_chain_issues": result.execution_chain_issues,
            "audit_issues": result.audit_issues,
            "healthy": result.divergence_count == 0,
        }

    def _append_event(self, *, topic: str, key: str, payload_model: Any) -> None:
        envelope = build_envelope(
            topic=topic,
            key=key,
            payload_model=payload_model,
            source_component="operator_api",
        )
        self.runtime.event_store.append(envelope)

    def _persist_blocker_snapshot(
        self,
        *,
        source: str,
        runtime_state: str,
        mode_snapshot: dict[str, Any],
        blockers: list[dict[str, Any]],
    ) -> None:
        latest = self.runtime.event_store.latest(topics.BLOCKER_SNAPSHOTS)
        candidate = BlockerSnapshotRecord(
            source=source,
            runtime_state=runtime_state,
            operating_state=mode_snapshot["operating_state"],
            mode=mode_snapshot["mode"],
            halted=bool(mode_snapshot["halted"]),
            execution_blocked=bool(mode_snapshot["execution_blocked"]),
            submit_blocked=bool(mode_snapshot["submit_blocked"]),
            blockers=blockers,
        )
        if latest is not None:
            payload = latest.payload
            if (
                payload.get("runtime_state") == candidate.runtime_state
                and payload.get("operating_state") == candidate.operating_state
                and payload.get("halted") == candidate.halted
                and payload.get("execution_blocked") == candidate.execution_blocked
                and payload.get("submit_blocked") == candidate.submit_blocked
                and payload.get("blockers") == candidate.blockers
            ):
                return
        self._append_event(topic=topics.BLOCKER_SNAPSHOTS, key="system", payload_model=candidate)

    @staticmethod
    def _blocker_entry(blocker: str, *, subsystem: str, submit_only: bool | None = None) -> dict[str, Any]:
        submit_only_value = submit_only if submit_only is not None else blocker in {
            "guarded_execution_dry_run",
            "live_submit_disabled",
            "local_demo_no_exchange_submission",
            "real_market_paper_uses_local_paper_execution",
            "real_money_live_not_supported",
            "guarded_live_blocked_by_default",
        }
        affects_execution = not submit_only_value
        recommended_action = "Inspect subsystem status and operator logs before resuming execution."
        if blocker == "local_demo_no_exchange_submission":
            recommended_action = "No action required. Local demo mode intentionally never submits exchange orders."
        elif blocker == "real_market_paper_uses_local_paper_execution":
            recommended_action = "No action required. Real-market paper mode intentionally uses local paper fills."
        elif blocker in {"guarded_execution_dry_run", "live_submit_disabled"}:
            recommended_action = "Enable the guarded simulated submit flags only if you intend to test demo exchange submission."
        elif blocker == "real_money_live_not_supported":
            recommended_action = "Do not attempt real-money live trading in this repository."
        return {
            "blocker": blocker,
            "subsystem": subsystem,
            "affects_execution": affects_execution,
            "affects_account_synchronization": subsystem == "account_state",
            "submit_only": submit_only_value,
            "recommended_action": recommended_action,
        }
