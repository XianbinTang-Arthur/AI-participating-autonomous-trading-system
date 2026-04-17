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
    return `${id.slice(0, 10)}…${id.slice(-10)}`;
  }

  function defaultObservationWindowHours() {
    const hours = Number(
      state?.data?.rdpControl?.environment?.required_observation_window_hours || 24,
    );
    return Number.isFinite(hours) && hours > 0 ? Math.floor(hours) : 24;
  }

  function resolveObservationWindowHours(rawValue) {
    const parsed = Number.parseInt(String(rawValue || "").trim(), 10);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
    return defaultObservationWindowHours();
  }

  async function triggerWorkflow(workflow) {
    if (!workflow) return;
    const labels = {
      data_maintenance: "刷新数据",
      research_cycle: "运行完整 RDP",
      governance_cycle: "治理检查",
      decision_cycle: "决策链",
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
        setFlash(state, "info", `${label}任务已提交（${result.task_id}），daemon 会继续处理。`);
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

  async function approveOnly(recommendationId) {
    if (!recommendationId) return;
    if (!windowRef.confirm(`确认审批 ${truncateForConfirm(recommendationId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在审批建议…");
    try {
      const result = await requestJson(
        `/rdp/recommendations/${encodeURIComponent(recommendationId)}/approve`,
        { method: "POST", body: { actor: "operator", notes: "UI 审批" } },
      );
      if (result.ok) {
        const recommendationType = String(result.recommendation?.recommendation_type || "");
        let message = `${truncateForConfirm(recommendationId)} 已审批。`;
        if (recommendationType === "parameter_upgrade") {
          message = `${truncateForConfirm(recommendationId)} 已批准，请到“待发布候选”里运行 Gate 或创建发布。`;
        } else if (recommendationType === "keep_active") {
          message = `${truncateForConfirm(recommendationId)} 已确认保持当前，本轮到此结束，不会创建新发布。`;
        } else if (recommendationType === "lower_priority") {
          message = `${truncateForConfirm(recommendationId)} 已确认降优先级，本轮不会创建新发布。`;
        } else if (recommendationType === "pause") {
          message = `${truncateForConfirm(recommendationId)} 已确认暂停，本轮不会创建新发布。`;
        } else if (recommendationType === "require_review") {
          message = `${truncateForConfirm(recommendationId)} 已确认需人工复核，本轮不会创建新发布。`;
        }
        setFlash(state, "info", message);
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
        setFlash(state, "info", `${truncateForConfirm(recommendationId)} 已拒绝。`);
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

  async function runGate(recommendationId) {
    if (!recommendationId) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在运行发布 Gate…");
    try {
      const result = await requestJson("/rdp/gates/run", {
        method: "POST",
        body: { recommendation_id: recommendationId, actor: "operator", notes: "UI 运行 Gate" },
      });
      if (result.gate_status === "block" || result.ok === false || result.allow_apply === false) {
        setFlash(state, "warning", result.blocking_reasons?.[0] || result.message || "Gate 阻断了这次发布。");
      } else if (result.gate_status === "warn") {
        setFlash(state, "info", result.warnings?.[0] || "Gate 通过，但带有警告。");
      } else {
        setFlash(state, "info", "Gate 通过，可以进入发布步骤。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function createRelease(recommendationId, { skipConfirm = false } = {}) {
    if (!recommendationId) return;
    const windowHours = defaultObservationWindowHours();
    if (!skipConfirm && !windowRef.confirm(`确认基于 ${truncateForConfirm(recommendationId)} 创建发布吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在创建发布…");
    try {
      const result = await requestJson("/rdp/releases/create", {
        method: "POST",
        body: {
          recommendation_id: recommendationId,
          actor: "operator",
          observation_window_hours: windowHours,
          notes: "UI 创建发布",
        },
      });
      if (result.ok) {
        const releaseId = result.release?.release_id || "release";
        setFlash(state, "info", `${releaseId} 已创建，已按默认 ${windowHours} 小时观察窗口推进。`);
      } else {
        setFlash(state, "warning", result.message || "创建发布失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function approveAndCreateRelease(recommendationId) {
    if (!recommendationId) return;
    if (!windowRef.confirm(`确认审批并推进 ${truncateForConfirm(recommendationId)} 到发布流程吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在审批并创建发布…");
    try {
      const approveResult = await requestJson(
        `/rdp/recommendations/${encodeURIComponent(recommendationId)}/approve`,
        { method: "POST", body: { actor: "operator", notes: "UI 审批并创建发布" } },
      );
      if (!approveResult.ok) {
        setFlash(state, "warning", approveResult.message || "审批失败。");
        renderBanners();
        return;
      }
      const windowHours = defaultObservationWindowHours();
      const releaseResult = await requestJson("/rdp/releases/create", {
        method: "POST",
        body: {
          recommendation_id: recommendationId,
          actor: "operator",
          observation_window_hours: windowHours,
          notes: "UI 审批并创建发布",
        },
      });
      if (releaseResult.ok) {
        setFlash(state, "info", `${truncateForConfirm(recommendationId)} 已审批，并创建了新的发布。`);
      } else {
        setFlash(state, "warning", releaseResult.message || "发布创建失败，审批已经完成。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function runObservation(value) {
    if (!value) return;
    const [releaseId, defaultHoursRaw] = String(value).split("|");
    const windowHours = resolveObservationWindowHours(defaultHoursRaw);
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在运行观察…");
    try {
      const result = await requestJson("/rdp/observations/run", {
        method: "POST",
        body: { release_id: releaseId, window_hours: windowHours },
      });
      if (result.status === "rollback_recommended") {
        setFlash(state, "warning", `${truncateForConfirm(releaseId)} 在 ${windowHours} 小时窗口下建议回滚。`);
      } else if (result.status) {
        setFlash(state, "info", `${truncateForConfirm(releaseId)} 观察状态：${result.status}（窗口 ${windowHours} 小时）。`);
      } else if (result.ok === false) {
        setFlash(state, "warning", result.message || "运行观察失败。");
      } else {
        setFlash(state, "info", `${truncateForConfirm(releaseId)} 观察任务已完成（窗口 ${windowHours} 小时）。`);
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function rollbackParameters(comboKey) {
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
        setFlash(state, "info", `${family}/${timeframe} 已回滚。`);
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

  async function approveTuningProposal(proposalId) {
    if (!proposalId) return;
    if (!windowRef.confirm(`确认批准调优提案 ${truncateForConfirm(proposalId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在批准调优提案…");
    try {
      const result = await requestJson(
        `/rdp/tuning/proposals/${encodeURIComponent(proposalId)}/approve`,
        { method: "POST", body: { actor: "operator", notes: "UI 批准调优提案" } },
      );
      if (result.ok) {
        setFlash(state, "info", `${truncateForConfirm(proposalId)} 已批准，后续 research 默认值会按新 override 生效。`);
      } else {
        setFlash(state, "warning", result.message || "批准调优提案失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function rejectTuningProposal(proposalId) {
    if (!proposalId) return;
    if (!windowRef.confirm(`确认拒绝调优提案 ${truncateForConfirm(proposalId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在拒绝调优提案…");
    try {
      const result = await requestJson(
        `/rdp/tuning/proposals/${encodeURIComponent(proposalId)}/reject`,
        { method: "POST", body: { actor: "operator", notes: "UI 拒绝调优提案" } },
      );
      if (result.ok) {
        setFlash(state, "info", `${truncateForConfirm(proposalId)} 已拒绝。`);
      } else {
        setFlash(state, "warning", result.message || "拒绝调优提案失败。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function applyOnly(recommendationId) {
    await createRelease(recommendationId);
  }

  return {
    "rdp-trigger-workflow": (workflow) => triggerWorkflow(workflow),
    "rdp-approve-and-apply": (recommendationId) => approveAndCreateRelease(recommendationId),
    "rdp-approve-only": (recommendationId) => approveOnly(recommendationId),
    "rdp-apply-only": (recommendationId) => applyOnly(recommendationId),
    "rdp-reject-recommendation": (recommendationId) => rejectRecommendation(recommendationId),
    "rdp-rollback-parameters": (combo) => rollbackParameters(combo),
    "rdp-run-gate": (recommendationId) => runGate(recommendationId),
    "rdp-create-release": (recommendationId) => createRelease(recommendationId),
    "rdp-run-observation": (value) => runObservation(value),
    "rdp-approve-tuning-proposal": (proposalId) => approveTuningProposal(proposalId),
    "rdp-reject-tuning-proposal": (proposalId) => rejectTuningProposal(proposalId),
  };
}
