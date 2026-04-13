import { ensureNotBusy as ensureNotBusyHelper, setFlash } from "../flash.js";

export function createRdpActionHandlers({
  beginAction,
  renderBanners,
  refreshDashboard,
  requestJson,
  state,
  windowRef = window,
}) {
  function ensureNotBusy() {
    return ensureNotBusyHelper(state, renderBanners);
  }

  function truncateForConfirm(id) {
    if (!id || id.length <= 24) return id || "";
    return id.slice(0, 10) + "…" + id.slice(-10);
  }

  // ── 触发 workflow（数据采集 / 研究管线）──────────────────────────

  async function triggerWorkflow(workflow) {
    if (!workflow) return;
    const labels = {
      data_maintenance: "数据采集",
      research_cycle: "研究管线",
    };
    const label = labels[workflow] || workflow;

    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在触发${label}…`);
    try {
      const result = await requestJson("/rdp/tasks/trigger", {
        method: "POST",
        body: { workflow, actor: "operator" },
      });
      if (result.ok) {
        setFlash(state, "info", `${label}任务已提交（${result.task_id}），daemon 将自动执行。`);
      } else {
        setFlash(state, "warning", result.message || `${label}触发失败。`);
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // ── 审批并应用 recommendation（仅限有 target_parameter_set_id 的）──

  async function approveAndApply(recommendationId) {
    if (!recommendationId) return;
    if (!windowRef.confirm(`确认审批并应用 ${truncateForConfirm(recommendationId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在审批并应用参数…");
    try {
      // Step 1: approve
      const approveResult = await requestJson(
        `/rdp/recommendations/${encodeURIComponent(recommendationId)}/approve`,
        { method: "POST", body: { actor: "operator", notes: "UI 一键审批并应用" } },
      );
      if (!approveResult.ok) {
        setFlash(state, "warning", approveResult.message || "审批失败。");
        renderBanners();
        return;
      }
      // Step 2: apply
      const applyResult = await requestJson("/rdp/parameters/apply", {
        method: "POST",
        body: { recommendation_id: recommendationId, actor: "operator", notes: "UI 一键审批并应用" },
      });
      if (applyResult.ok) {
        setFlash(state, "info", `${recommendationId} 已审批并应用到 active parameters。`);
      } else {
        setFlash(state, "warning", applyResult.message || "参数应用失败（审批已完成）。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // ── 仅审批（策略指导类建议，没有 target_parameter_set_id）─────

  async function approveOnly(recommendationId) {
    if (!recommendationId) return;
    if (!windowRef.confirm(`确认审批 ${truncateForConfirm(recommendationId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在审批…");
    try {
      const result = await requestJson(
        `/rdp/recommendations/${encodeURIComponent(recommendationId)}/approve`,
        { method: "POST", body: { actor: "operator", notes: "UI 审批（策略指导，无参数应用）" } },
      );
      if (result.ok) {
        setFlash(state, "info", `${truncateForConfirm(recommendationId)} 已审批。`);
      } else {
        setFlash(state, "warning", result.message || "审批失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // ── 仅应用已审批的 recommendation ─────────────────────────────

  async function applyOnly(recommendationId) {
    if (!recommendationId) return;
    if (!windowRef.confirm(`确认应用已审批的 ${truncateForConfirm(recommendationId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在应用参数…");
    try {
      const result = await requestJson("/rdp/parameters/apply", {
        method: "POST",
        body: { recommendation_id: recommendationId, actor: "operator", notes: "UI 应用已审批参数" },
      });
      if (result.ok) {
        setFlash(state, "info", `${truncateForConfirm(recommendationId)} 已应用到 active parameters。`);
      } else {
        setFlash(state, "warning", result.message || "参数应用失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // ── 拒绝 recommendation ───────────────────────────────────────

  async function rejectRecommendation(recommendationId) {
    if (!recommendationId) return;
    if (!windowRef.confirm(`确认拒绝 ${truncateForConfirm(recommendationId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在拒绝建议…");
    try {
      const result = await requestJson(
        `/rdp/recommendations/${encodeURIComponent(recommendationId)}/reject`,
        { method: "POST", body: { actor: "operator", notes: "UI 拒绝" } },
      );
      if (result.ok) {
        setFlash(state, "info", `${recommendationId} 已拒绝。`);
      } else {
        setFlash(state, "warning", result.message || "拒绝失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  // ── 回滚 active parameters ────────────────────────────────────

  async function rollbackParameters(comboKey) {
    // comboKey format: "family/timeframe" e.g. "independent/15m"
    if (!comboKey) return;
    const [family, timeframe] = comboKey.split("/");
    if (!family || !timeframe) return;
    if (!windowRef.confirm(`确认回滚 ${family}/${timeframe} 的参数到上一版吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, `正在回滚 ${family}/${timeframe}…`);
    try {
      const result = await requestJson("/rdp/parameters/rollback", {
        method: "POST",
        body: { family, timeframe, actor: "operator", notes: "UI 回滚" },
      });
      if (result.ok) {
        setFlash(state, "info", `${family}/${timeframe} 参数已回滚。`);
      } else {
        setFlash(state, "warning", result.message || "回滚失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  return {
    "rdp-trigger-workflow": (workflow) => triggerWorkflow(workflow),
    "rdp-approve-and-apply": (recId) => approveAndApply(recId),
    "rdp-approve-only": (recId) => approveOnly(recId),
    "rdp-apply-only": (recId) => applyOnly(recId),
    "rdp-reject-recommendation": (recId) => rejectRecommendation(recId),
    "rdp-rollback-parameters": (combo) => rollbackParameters(combo),
  };
}
