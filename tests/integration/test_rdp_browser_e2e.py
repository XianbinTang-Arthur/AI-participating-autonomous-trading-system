from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from aats.api.rdp_routes import rdp_router


def _build_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            operator_auth_enabled=False,
            operator_control_plane_execution_ledger_enabled=False,
            operator_unsafe_write_without_auth=True,
            storage_mode="memory",
        ),
        environment_capabilities=SimpleNamespace(local_only=True),
    )


@contextmanager
def _fake_governance_session():
    yield object()


@contextmanager
def _live_server(app: FastAPI) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    config = uvicorn.Config(app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://{host}:{port}"
    deadline = time.time() + 20
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/e2e/rdp", timeout=1)
            if response.status_code == 200:
                break
        except Exception as exc:  # pragma: no cover - env dependent
            last_error = exc
        time.sleep(0.2)
    else:  # pragma: no cover - defensive
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError(f"failed to start test server: {last_error}")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _build_browser() -> webdriver.Edge:
    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--allow-file-access-from-files")
    return webdriver.Edge(options=options)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _non_get_request_log(driver: webdriver.Edge) -> list[dict]:
    return driver.execute_script(
        "return window.__requestLog.filter((item) => item.method !== 'GET');",
    )


@pytest.mark.integration
def test_rdp_browser_e2e_click_chain_updates_page_and_requests(tmp_path: Path) -> None:
    try:
        driver = _build_browser()
    except WebDriverException as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Edge WebDriver unavailable: {exc}")

    app = FastAPI()
    app.include_router(rdp_router)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parents[2] / "aats" / "api" / "static")),
        name="static",
    )
    app.state.runtime = _build_runtime()

    @app.get("/e2e/rdp", response_class=HTMLResponse)
    async def _rdp_e2e_page() -> str:
        return """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>RDP E2E</title>
  </head>
  <body>
    <div id="app">loading</div>
    <script type="module">
      import { renderRdpControlPanelV2 } from "/static/modules/views/rdp-control-panel.js";
      import { createRdpActionHandlers } from "/static/modules/actions/rdp-actions.js";

      window.confirm = () => true;
      window.prompt = () => null;
      window.__requestLog = [];

      const originalFetch = window.fetch.bind(window);
      window.fetch = async (input, init = {}) => {
        const url = typeof input === "string" ? input : input.url;
        let body = null;
        if (init.body) {
          try {
            body = JSON.parse(init.body);
          } catch (_error) {
            body = init.body;
          }
        }
        window.__requestLog.push({
          url,
          method: init.method || "GET",
          body,
        });
        return originalFetch(input, init);
      };

      const state = {
        actionInFlight: false,
        flash: null,
        data: { rdpControl: null },
      };
      const root = document.getElementById("app");

      async function requestJson(path, options = {}) {
        const response = await fetch(path, {
          method: options.method || "GET",
          headers: { "Content-Type": "application/json" },
          body: options.body ? JSON.stringify(options.body) : undefined,
        });
        return response.json();
      }

      async function refreshDashboard() {
        const payload = await requestJson("/rdp/control-summary");
        state.data.rdpControl = payload;
        root.innerHTML = renderRdpControlPanelV2({
          rdpControl: payload,
          canAdmin: true,
          uiState: {},
        });
        document.body.dataset.ready = "true";
      }

      const handlers = createRdpActionHandlers({
        beginAction: () => {
          state.actionInFlight = true;
          return () => {
            state.actionInFlight = false;
          };
        },
        renderBanners: () => {},
        refreshDashboard,
        requestJson,
        state,
        windowRef: window,
      });
      window.__rdpHandlers = handlers;

      document.addEventListener("click", async (event) => {
        const button = event.target.closest("[data-action]");
        if (!button) return;
        const action = button.dataset.action;
        const value = button.dataset.value || "";
        if (handlers[action]) {
          event.preventDefault();
          await handlers[action](value);
        }
      });

      await refreshDashboard();
    </script>
  </body>
</html>
        """

    root = tmp_path
    (root / "artifacts" / "decision_system").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "governance").mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "production_workflow" / "gates" / "gate_demo_1").mkdir(parents=True, exist_ok=True)

    _write_json(
        root / "artifacts" / "decision_system" / "recommendation_registry.json",
        {
            "generated_at": "2026-04-16T11:50:00Z",
            "version": 1,
            "recommendations": [
                {
                    "recommendation_id": "rec_demo_1",
                    "created_at": "2026-04-16T11:55:00Z",
                    "family": "independent",
                    "symbol": "BTC-USDT-SWAP",
                    "timeframe": "15m",
                    "recommendation_type": "parameter_upgrade",
                    "target_parameter_set_id": "ps_candidate_1",
                    "confidence": "high",
                    "reason": "候选参数已经生成，可审批。",
                    "status": "draft",
                },
            ],
        },
    )
    _write_json(
        root / "artifacts" / "governance" / "current_parameter_registry.json",
        {
            "generated_at": "2026-04-16T11:45:00Z",
            "version": 1,
            "parameter_sets": [
                {
                    "parameter_set_id": "ps_live_0",
                    "family": "independent",
                    "timeframe": "15m",
                    "status": "frozen",
                    "source_round_id": "round_prev",
                    "values": {"entry_threshold": 0.4},
                },
                {
                    "parameter_set_id": "ps_candidate_1",
                    "family": "independent",
                    "timeframe": "15m",
                    "status": "candidate",
                    "source_round_id": "round_demo",
                    "values": {"entry_threshold": 0.42},
                },
            ],
        },
    )

    active_state = {
        "active_sets": {
            "independent_15m": {
                "parameter_set_id": "ps_live_0",
                "family": "independent",
                "timeframe": "15m",
                "status": "active",
                "applied_at": "2026-04-16T11:40:00Z",
                "applied_by": "operator",
                "approval_recommendation_id": "rec_prev",
                "source_round_id": "round_prev",
                "values": {"entry_threshold": 0.4},
            },
        },
    }

    def _active_summary() -> dict[str, object]:
        active_sets = active_state["active_sets"]
        return {
            "generated_at": "2026-04-16T12:00:00Z",
            "governance_managed": True,
            "paused_combos": [],
            "known_combos": ["independent_15m"],
            "active_combos": sorted(active_sets.keys()),
            "missing_combos": [],
            "total_active_sets": len(active_sets),
            "active_sets": active_sets,
            "parameter_sets": [
                {
                    "combo_key": combo_key,
                    "family": entry["family"],
                    "timeframe": entry["timeframe"],
                    "parameter_set_id": entry["parameter_set_id"],
                    "status": entry.get("status", "active"),
                    "applied_at": entry.get("applied_at"),
                    "applied_by": entry.get("applied_by"),
                    "approval_recommendation_id": entry.get("approval_recommendation_id"),
                    "source_round_id": entry.get("source_round_id"),
                    "parameter_count": len(entry.get("values", {})),
                    "values": entry.get("values", {}),
                }
                for combo_key, entry in active_sets.items()
            ],
        }

    def _fake_gate(project_root: Path, recommendation_id: str):
        result = {
            "gate_run_id": "gate_demo_1",
            "recommendation_id": recommendation_id,
            "created_at": "2026-04-16T12:04:00Z",
            "gate_status": "pass",
            "allow_apply": True,
            "blocking_reasons": [],
            "warnings": [],
            "checks": [],
        }
        _write_json(
            project_root / "artifacts" / "production_workflow" / "gates" / "gate_demo_1" / "pre_apply_gate_result.json",
            result,
        )
        return result

    def _fake_apply(
        project_root: Path,
        *,
        recommendation_id: str,
        actor: str = "operator",
        notes: str | None = None,
        dry_run: bool = False,
        release_id: str | None = None,
        gate_result: dict[str, object] | None = None,
    ):
        del project_root, notes, dry_run, gate_result
        active_state["active_sets"]["independent_15m"] = {
            "parameter_set_id": "ps_candidate_1",
            "family": "independent",
            "timeframe": "15m",
            "status": "active",
            "applied_at": "2026-04-16T12:05:00Z",
            "applied_by": actor,
            "approval_recommendation_id": recommendation_id,
            "source_round_id": "round_demo",
            "values": {"entry_threshold": 0.42},
        }
        return {
            "ok": True,
            "message": "apply success",
            "operation_type": "apply",
            "combo_key": "independent_15m",
            "family": "independent",
            "timeframe": "15m",
            "recommendation_id": recommendation_id,
            "parameter_set_id": "ps_candidate_1",
            "release_id": release_id,
        }

    def _fake_rollback(
        project_root: Path,
        *,
        family: str,
        timeframe: str,
        to_parameter_set_id: str | None = None,
        actor: str = "operator",
        notes: str | None = None,
        dry_run: bool = False,
    ):
        del project_root, to_parameter_set_id, notes, dry_run
        active_state["active_sets"]["independent_15m"] = {
            "parameter_set_id": "ps_live_0",
            "family": family,
            "timeframe": timeframe,
            "status": "active",
            "applied_at": "2026-04-16T12:10:00Z",
            "applied_by": actor,
            "approval_recommendation_id": "rec_prev",
            "source_round_id": "round_prev",
            "values": {"entry_threshold": 0.4},
        }
        return {
            "ok": True,
            "message": "rollback success",
            "family": family,
            "timeframe": timeframe,
            "to_parameter_set_id": "ps_live_0",
        }

    with ExitStack() as stack:
        stack.enter_context(patch("aats.api.rdp_routes._project_root", lambda _request: root))
        stack.enter_context(patch("aats.api.rdp_routes._governance_session", _fake_governance_session))
        stack.enter_context(patch("aats.api.rdp_control_summary._project_root", lambda _request: root))
        stack.enter_context(patch("aats.api.rdp_control_summary._governance_session", _fake_governance_session))
        stack.enter_context(
            patch(
                "aats.api.rdp_control_summary._environment_summary",
                return_value={
                    "name": "dev",
                    "strict_environment": False,
                    "description": "开发环境",
                    "require_gate_pass": False,
                    "require_approval": False,
                    "allow_parameter_rollback": True,
                    "direct_apply_allowed": True,
                    "required_observation_window_hours": 24,
                },
            ),
        )
        stack.enter_context(
            patch(
                "aats.api.rdp_control_summary.query_rdp_health",
                return_value={
                    "overall_health": "healthy",
                    "blocking_reasons": [],
                    "warnings": [],
                    "checks": [],
                },
            ),
        )
        stack.enter_context(
            patch(
                "aats.api.rdp_control_summary.query_active_parameter_sets",
                side_effect=lambda _root: _active_summary(),
            ),
        )
        stack.enter_context(
            patch("aats.api.rdp_control_summary.query_latest_decision_round", return_value={"available": False}),
        )
        stack.enter_context(
            patch(
                "aats.api.rdp_control_summary.query_latest_decisions",
                return_value={
                    "available": True,
                    "generated_at": "2026-04-16T12:00:00Z",
                    "status_distribution": {"keep_active": 1},
                    "decisions": [],
                },
            ),
        )
        stack.enter_context(
            patch("aats.data_platform.decision_system.recommendation_registry.try_governance_db", lambda: (None, False)),
        )
        stack.enter_context(
            patch("aats.data_platform.governance.parameter_registry.try_governance_db", lambda: (None, False)),
        )
        stack.enter_context(
            patch("aats.data_platform.production_workflow.release_registry.try_governance_db", lambda: (None, False)),
        )
        stack.enter_context(
            patch("aats.data_platform.production_workflow.observation_window.try_governance_db", lambda: (None, False)),
        )
        stack.enter_context(
            patch("aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate", _fake_gate),
        )
        stack.enter_context(
            patch("aats.data_platform.decision_system.active_parameter_apply.apply_approved_recommendation", _fake_apply),
        )
        stack.enter_context(
            patch("aats.data_platform.decision_system.active_parameter_apply.rollback_active_parameter_set", _fake_rollback),
        )
        stack.enter_context(
            patch(
                "aats.data_platform.governance.rdp_task_db.db_create_task_if_idle",
                return_value=("task_demo_1", None),
            ),
        )
        stack.enter_context(
            patch("aats.data_platform.governance.rdp_task_db.db_get_recent_tasks", return_value=[]),
        )

        with _live_server(app) as base_url:
            try:
                wait = WebDriverWait(driver, 20)
                driver.get(f"{base_url}/e2e/rdp")
                wait.until(lambda d: d.execute_script("return document.body.dataset.ready") == "true")
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-action="rdp-approve-only"]')))

                approve_button = driver.find_element(By.CSS_SELECTOR, '[data-action="rdp-approve-only"]')
                driver.execute_script("arguments[0].click()", approve_button)
                wait.until(lambda d: any(item["url"].endswith("/rdp/recommendations/rec_demo_1/approve") for item in _non_get_request_log(d)))
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-action="rdp-create-release"]')))

                release_button = driver.find_element(By.CSS_SELECTOR, '[data-action="rdp-create-release"]')
                driver.execute_script("arguments[0].click()", release_button)
                wait.until(lambda d: any(item["url"].endswith("/rdp/releases/create") for item in _non_get_request_log(d)))
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-action="rdp-run-observation"]')))

                observation_button = driver.find_element(By.CSS_SELECTOR, '[data-action="rdp-run-observation"]')
                driver.execute_script("arguments[0].click()", observation_button)
                wait.until(lambda d: any(item["url"].endswith("/rdp/observations/run") for item in _non_get_request_log(d)))
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-action="rdp-rollback-parameters"]')))

                rollback_button = driver.find_element(By.CSS_SELECTOR, '[data-action="rdp-rollback-parameters"]')
                rollback_value = rollback_button.get_attribute("data-value")
                assert rollback_value == "independent/15m"

                request_log = _non_get_request_log(driver)

                assert any(
                    item["url"].endswith("/rdp/recommendations/rec_demo_1/approve")
                    and item["body"]["actor"] == "operator"
                    for item in request_log
                )
                assert any(
                    item["url"].endswith("/rdp/releases/create")
                    and item["body"]["observation_window_hours"] == 24
                    and item["body"]["recommendation_id"] == "rec_demo_1"
                    for item in request_log
                )
                assert any(
                    item["url"].endswith("/rdp/observations/run")
                    and item["body"]["window_hours"] == 24
                    for item in request_log
                )
                assert active_state["active_sets"]["independent_15m"]["parameter_set_id"] == "ps_candidate_1"
            finally:
                driver.quit()
