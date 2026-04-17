#!/usr/bin/env python3
"""Dashboard mock server — UX-audit only.

Serves ``aats/api/static`` and returns stubbed ``/dashboard/bundle`` payloads
so the RDP-inside-AI-Config UX can be verified in a browser without spinning
up the real backend.

Not a production API. No auth. No persistence. One listener, hard-coded fixtures.
Launched via ``.claude/launch.json`` → ``preview_start`` with name ``dashboard-mock``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "aats" / "api" / "static"
PORT = int(os.environ.get("AATS_MOCK_PORT", "18765"))


def _json_response(handler: "BaseHTTPRequestHandler", body: dict, status: int = 200) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _serve_static(handler: "BaseHTTPRequestHandler", relative: str) -> None:
    safe = (STATIC / relative.lstrip("/")).resolve()
    try:
        safe.relative_to(STATIC)
    except ValueError:
        handler.send_error(403)
        return
    if not safe.is_file():
        handler.send_error(404)
        return
    ctype, _ = mimetypes.guess_type(str(safe))
    if safe.suffix in {".js", ".mjs"}:
        ctype = "text/javascript; charset=utf-8"
    elif safe.suffix == ".css":
        ctype = "text/css; charset=utf-8"
    elif safe.suffix == ".html":
        ctype = "text/html; charset=utf-8"
    data = safe.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _session_panel() -> dict:
    return {
        "identity": "mock-admin",
        "role": "admin",
        "authenticated": True,
        "display_name": "UX Audit",
    }


def _auth_providers_panel() -> dict:
    return {"auth_enabled": True, "providers": ["session"]}


def _recovery_panel() -> dict:
    return {
        "recovery": {
            "halted": False,
            "resume_eligible": True,
            "safe_to_trade": True,
            "only_reduce_reasons": [],
            "resume_blocked_reasons": [],
        }
    }


def _rdp_workbench_overview() -> dict:
    return {
        "headline": "待审核 1 条发布候选",
        "subheadline": "先确认证据完整性再继续发布。",
        "overall_status": "pending",
        "blockers": [],
        "summary_counts": {
            "pending_items": 3,
            "integrity_blocked_items": 1,
            "tuning_pending": 2,
            "observing_releases": 2,
        },
        "current_execution": {
            "workflow": "research_cycle",
            "started_at": "2026-04-17T08:30:00Z",
        },
        "next_queue": {
            "workflow": "release_cycle",
            "requested_at": "2026-04-17T09:00:00Z",
        },
        "primary_action": {
            "label": "运行研究链",
            "ui_action": "rdp-trigger-workflow",
            "value": "research_cycle",
            "enabled": True,
        },
        "secondary_actions": [
            {
                "label": "刷新数据",
                "ui_action": "rdp-trigger-workflow",
                "value": "data_maintenance",
                "enabled": True,
            },
            {
                "label": "发布与观察",
                "ui_action": "rdp-trigger-workflow",
                "value": "release_cycle",
                "enabled": False,
                "disabled_reason": "当前轮次尚未形成可发布候选",
            },
        ],
    }


def _rdp_workbench_items() -> dict:
    return {
        "items": [
            {
                "kind": "recommendation_pending",
                "family": "trend_breakout",
                "timeframe": "15m",
                "recommendation_id": "rec-2026-04-17-A1",
                "parameter_set_id": "ps-2026-04-17-aa",
                "confidence": "high",
                "gate_status": "pass",
                "created_at": "2026-04-17T09:10:00Z",
                "summary": "新的趋势突破参数集，证据链齐全。",
                "evidence_digest": [
                    {
                        "phase": "phase2",
                        "headline": "Step2 研究覆盖 42 组实验",
                        "status": "available",
                        "round_id": "r2-0417",
                        "metrics": {
                            "experiments_with_openings": 38,
                            "mean_positive_edge_ratio": 0.62,
                        },
                    }
                ],
                "actions": [
                    {
                        "label": "通过",
                        "ui_action": "rdp-approve-recommendation",
                        "value": "rec-2026-04-17-A1",
                        "enabled": True,
                    },
                    {
                        "label": "驳回",
                        "ui_action": "rdp-reject-recommendation",
                        "value": "rec-2026-04-17-A1",
                        "enabled": True,
                    },
                ],
            },
            {
                "kind": "integrity_blocked",
                "family": "meanrev_basis",
                "timeframe": "5m",
                "recommendation_id": "rec-2026-04-17-B2",
                "created_at": "2026-04-17T08:50:00Z",
                "summary": "Phase3 归因证据缺失，暂不能推进。",
                "evidence_digest": [
                    {
                        "phase": "phase3",
                        "status": "incomplete",
                        "incomplete_reason": "manifest_missing_on_disk",
                    }
                ],
            },
        ],
        "release_candidates": {
            "items": [
                {
                    "family": "trend_breakout",
                    "timeframe": "15m",
                    "release_id": "rel-2026-04-17-C3",
                    "parameter_set_id": "ps-2026-04-17-aa",
                    "gate_status": "pass",
                    "confidence": "high",
                    "created_at": "2026-04-17T09:20:00Z",
                    "is_current_active_release": False,
                    "apply_result": "pending",
                    "actions": [
                        {
                            "label": "立即发布",
                            "ui_action": "rdp-create-release",
                            "value": "rel-2026-04-17-C3",
                            "enabled": True,
                        }
                    ],
                }
            ]
        },
    }


def _rdp_workbench_alerts() -> dict:
    return {
        "alerts": [
            {
                "severity": "warning",
                "headline": "1 条 Phase3 证据不完整",
                "body": "meanrev_basis/5m 的 round_manifest 未落地，治理链无法继续。",
                "reported_at": "2026-04-17T08:55:00Z",
            }
        ]
    }


def _rdp_tuning_overview() -> dict:
    return {
        "headline": "2 条调优提案待审核",
        "subheadline": "调优闸已关闭，全部提案需要显式批准。",
        "summary_counts": {"pending": 2, "accepted": 0, "rejected": 0},
    }


def _rdp_tuning_proposals() -> dict:
    return {
        "items": [
            {
                "proposal_id": "tune-2026-04-17-T1",
                "family": "trend_breakout",
                "timeframe": "15m",
                "confidence": "medium",
                "summary": "将止损从 1.2% 放宽到 1.4%，样本内胜率提高 3.1pp。",
                "created_at": "2026-04-17T07:00:00Z",
                "actions": [
                    {
                        "label": "批准",
                        "ui_action": "rdp-accept-tuning",
                        "value": "tune-2026-04-17-T1",
                        "enabled": True,
                    },
                    {
                        "label": "驳回",
                        "ui_action": "rdp-reject-tuning",
                        "value": "tune-2026-04-17-T1",
                        "enabled": True,
                    },
                ],
            }
        ]
    }


def _rdp_control() -> dict:
    return {
        "environment": "dev",
        "observation_items": [
            {
                "family": "trend_breakout",
                "timeframe": "15m",
                "release_id": "rel-2026-04-16-X1",
                "parameter_set_id": "ps-2026-04-16-xx",
                "previous_parameter_set_id": "ps-2026-04-14-ww",
                "observation_status": "observing",
                "apply_result": "success",
                "is_current_active_release": True,
                "observation_window_hours": 24,
                "created_at": "2026-04-16T10:00:00Z",
                "observation": {
                    "status": "observing",
                    "recommendation": "保持观察",
                    "evaluated_at": "2026-04-17T06:00:00Z",
                },
                "effectiveness": {"detail": "样本外 6 小时胜率 58%，略优于基线。"},
            },
            {
                "family": "meanrev_basis",
                "timeframe": "5m",
                "release_id": "rel-2026-04-15-Y2",
                "parameter_set_id": "ps-2026-04-15-yy",
                "previous_parameter_set_id": "ps-2026-04-13-vv",
                "observation_status": "rollback_recommended",
                "apply_result": "success",
                "is_current_active_release": True,
                "observation_window_hours": 24,
                "created_at": "2026-04-15T12:00:00Z",
                "observation": {
                    "status": "rollback_recommended",
                    "recommendation": "建议回滚",
                    "evaluated_at": "2026-04-17T05:30:00Z",
                },
                "effectiveness": {"detail": "样本外连续 3 窗口负向，建议回滚到 ps-2026-04-13-vv。"},
            },
        ],
    }


def _ai_config_summary() -> dict:
    return {
        "ai_operating_mode": "advisory",
        "config_version": "v3.2.1",
        "last_applied_at": "2026-04-17T04:00:00Z",
        "active_profile": {"profile_id": "balanced", "profile_label": "均衡档"},
    }


def _ai_runtime() -> dict:
    return {
        "effective_operating_mode": "advisory",
        "advisor_mode_since": "2026-04-15T00:00:00Z",
        "latest_assessment_id": "a-2026-04-17-01",
    }


CORE_PANELS = {
    "session": _session_panel,
    "authProviders": _auth_providers_panel,
    "health": lambda: {"halted": False, "healthy": True},
    "mode": lambda: {"mode": "derivatives_live", "trading_enabled": True},
    "runtime": lambda: {"operator_auth": {"unsafe_write_without_auth": False}},
    "systemRecovery": _recovery_panel,
    "blockerControl": lambda: {"blockers": [], "primary_blocker": None, "secondary_blockers": []},
    "blockers": lambda: {"blockers": []},
    "metrics": lambda: {"metrics": {"pnl_24h": 0.0, "open_positions": 0}},
    "portfolio": lambda: {"portfolio": {"equity": 10000.0, "unrealized_pnl": 0.0}},
    "positions": lambda: {"positions": []},
    "latestDecision": lambda: None,
    "executionLatest": lambda: None,
    "reconciliationLatest": lambda: None,
    "accountState": lambda: {"equity": 10000.0, "available": 10000.0},
    "aiConfigModel": _ai_config_summary,
    "aiRuntime": _ai_runtime,
    "rdpControl": _rdp_control,
    "rdpWorkbenchOverview": _rdp_workbench_overview,
    "rdpWorkbenchItems": _rdp_workbench_items,
    "rdpWorkbenchAlerts": _rdp_workbench_alerts,
    "rdpTuningOverview": _rdp_tuning_overview,
    "rdpTuningProposals": _rdp_tuning_proposals,
    "operatorUsers": lambda: {"users": []},
    "strategyRuntime": lambda: {"profiles": []},
    "strategyAttribution": lambda: {"items": []},
    "positionLifecycleAttribution": lambda: {"items": []},
    "recentDecisions": lambda: {"items": []},
    "recentOrders": lambda: {"items": []},
    "recentFills": lambda: {"items": []},
    "executionErrors": lambda: {"items": []},
    "trialReviewSummary": lambda: {"items": []},
    "trialReviewHistory": lambda: {"items": []},
    "trialGuard": lambda: {"status": "inactive"},
    "guardedLivePreflight": lambda: {"status": "not_required"},
    "guardedLiveRunPacket": lambda: None,
    "replayStatus": lambda: {"status": "idle"},
    "replayRecentValidations": lambda: {"items": []},
    "exitExecutionActionHistoryPage": lambda: {"items": [], "total": 0},
    "aiOverview": lambda: {"summary": {}},
    "aiLatest": lambda: None,
    "aiShadowLatest": lambda: None,
    "aiRecent": lambda: {"items": []},
    "aiShadowRecent": lambda: {"items": []},
    "aiShadowEvaluations": lambda: {"items": []},
    "profileControlSummary": lambda: {"items": []},
}


def _build_bundle(panels: list[str]) -> dict:
    result: dict[str, dict] = {}
    for key in panels:
        factory = CORE_PANELS.get(key)
        if factory is None:
            result[key] = {"data": None, "error": f"mock-unknown-panel:{key}"}
        else:
            result[key] = {"data": factory(), "error": None}
    return {
        "panels": result,
        "auth": {
            "access_state": "ready",
            "auth_blocked_reason": None,
            "primary_error": None,
        },
        "timing": {"started_at": "2026-04-17T10:00:00Z", "finished_at": "2026-04-17T10:00:01Z"},
    }


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        sys.stderr.write(f"[mock] {self.address_string()} - {format % args}\n")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/dashboard/bundle":
            qs = parse_qs(parsed.query)
            panels = qs.get("panel", [])
            _json_response(self, _build_bundle(panels))
            return
        if path == "/login":
            _serve_static(self, "login.html")
            return
        # /ui/<asset> → static file if the asset exists under aats/api/static.
        # Unknown /ui/<view> paths fall through to dashboard-shell.html so
        # view-router can pick the view from window.location.
        if path.startswith("/ui/"):
            rel = path[len("/ui/"):]
            candidate = (STATIC / rel).resolve()
            try:
                candidate.relative_to(STATIC)
            except ValueError:
                self.send_error(403)
                return
            if candidate.is_file():
                _serve_static(self, rel)
                return
            _serve_static(self, "dashboard-shell.html")
            return
        if path == "/" or path == "/ui":
            _serve_static(self, "dashboard-shell.html")
            return
        # Individual REST endpoints — rarely used by the dashboard (it goes
        # through /dashboard/bundle), but the resume/halt buttons on the shell
        # header can hit these directly.
        if path == "/auth/session":
            _json_response(self, _session_panel())
            return
        if path == "/auth/providers":
            _json_response(self, _auth_providers_panel())
            return
        if path == "/healthz":
            _json_response(self, {"ok": True})
            return
        _json_response(self, {"mock": True, "path": path}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        _json_response(self, {"ok": True, "message": f"mock POST {parsed.path}"})


def main() -> None:
    # Force the /ui/* prefix to resolve statics — the production gateway mounts
    # static assets under /ui/, so modulepreload hrefs in dashboard-shell.html
    # use absolute /ui/... paths. The fallback at "/" only triggers for the
    # dashboard root itself.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), MockHandler)
    sys.stderr.write(f"[mock] listening on http://127.0.0.1:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
