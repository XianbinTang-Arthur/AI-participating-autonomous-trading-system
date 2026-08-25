import { ensureNotBusy as ensureNotBusyHelper, setFlash } from "../flash.js";
import { escapeHtml } from "../formatters.js";

export function createRdpActionHandlers({
  beginAction,
  openDrawer,
  renderBanners,
  renderShell,
  refreshDashboard,
  refreshPanels,
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

  // 观察窗口上界:1 年(8760 小时)。data-hours DOM 属性或 POST body 被篡改时,
  // 过大的值会让后端时间窗口查询扫全表,拒绝超界值走默认兜底。
  const OBSERVATION_WINDOW_MAX_HOURS = 8760;

  function resolveObservationWindowHours(rawValue) {
    const parsed = Number.parseInt(String(rawValue || "").trim(), 10);
    if (Number.isFinite(parsed) && parsed > 0 && parsed <= OBSERVATION_WINDOW_MAX_HOURS) {
      return parsed;
    }
    return defaultObservationWindowHours();
  }

  function formatRunTime(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return String(value);
    return parsed.toLocaleString("zh-CN", { hour12: false });
  }

  function buildRunDrawer(detail) {
    const run = detail?.run || {};
    const attempts = detail?.attempts || [];
    const steps = detail?.steps || [];
    const events = detail?.events || [];
    const attemptRows = attempts.length
      ? attempts.map((attempt) => `
          <article class="rdp-workitem tone-${attempt.status === "done" ? "positive" : (attempt.status === "failed" ? "danger" : "warning")}">
            <div class="rdp-workitem__header">
              <strong>尝试 ${escapeHtml(String(attempt.attempt_no || 1))}</strong>
              <span>${escapeHtml(String(attempt.status || "unknown"))}</span>
            </div>
            <p class="meta-copy">任务 ${escapeHtml(String(attempt.task_id || "—"))}</p>
            <p class="meta-copy">可执行时间 ${escapeHtml(formatRunTime(attempt.earliest_start_at))}；退出码 ${escapeHtml(String(attempt.exit_code ?? "—"))}</p>
            ${attempt.error_message ? `<p class="meta-copy">${escapeHtml(attempt.error_message)}</p>` : ""}
          </article>
        `).join("")
      : '<p class="meta-copy">尚无执行尝试。</p>';
    const stepRows = steps.length
      ? steps.map((step) => `
          <div class="kv-row">
            <span class="kv-row__label">${escapeHtml(step.step_key || "未命名步骤")}</span>
            <strong class="kv-row__value">${escapeHtml(step.status || "pending")}</strong>
            <span class="meta-copy">尝试 ${escapeHtml(String(step.attempt_no || 1))}${step.error_summary ? ` · ${escapeHtml(step.error_summary)}` : ""}</span>
          </div>
        `).join("")
      : '<p class="meta-copy">步骤尚未开始上报。</p>';
    const eventRows = events.slice(-20).reverse().map((event) => `
      <li><strong>${escapeHtml(event.event_type || "event")}</strong> · ${escapeHtml(formatRunTime(event.occurred_at))}</li>
    `).join("");
    return {
      eyebrow: "RDP 运行详情",
      title: String(run.run_id || "运行详情"),
      summary: `${run.workflow || "未知流程"} · ${run.status || "unknown"}`,
      body: `
        <div class="kv-list">
          <div class="kv-row"><span class="kv-row__label">触发来源</span><strong class="kv-row__value">${escapeHtml(run.trigger_kind || "unknown")}</strong></div>
          <div class="kv-row"><span class="kv-row__label">进度</span><strong class="kv-row__value">${escapeHtml(`${run.completed_steps || 0}/${run.total_steps || 0}`)}</strong><span class="meta-copy">${escapeHtml(run.current_step_key || "当前无执行步骤")}</span></div>
          <div class="kv-row"><span class="kv-row__label">开始 / 完成</span><strong class="kv-row__value">${escapeHtml(formatRunTime(run.started_at))}</strong><span class="meta-copy">${escapeHtml(formatRunTime(run.finished_at))}</span></div>
        </div>
        ${run.error_summary ? `<div class="notice tone-danger">${escapeHtml(run.error_summary)}</div>` : ""}
        <h3 class="rdp-subtle-heading">执行尝试</h3>
        <div class="rdp-worklist">${attemptRows}</div>
        <h3 class="rdp-subtle-heading">步骤</h3>
        <div class="kv-list">${stepRows}</div>
        <h3 class="rdp-subtle-heading">最近事件</h3>
        ${eventRows ? `<ul class="rdp-bullet-list">${eventRows}</ul>` : '<p class="meta-copy">暂无事件。</p>'}
      `,
    };
  }

  function mergeRunIntoState(run) {
    if (!run?.run_id) return;
    const current = Array.isArray(state.data.rdpRuns?.items)
      ? state.data.rdpRuns.items
      : [];
    state.data.rdpRuns = {
      ...(state.data.rdpRuns || {}),
      items: [
        run,
        ...current.filter((item) => item?.run_id !== run.run_id),
      ].slice(0, Number(state.data.rdpRuns?.limit || 20)),
      limit: Number(state.data.rdpRuns?.limit || 20),
    };
    if (typeof renderShell === "function") renderShell();
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
    let actionFinished = false;
    state.ui.rdp = state.ui.rdp || { idempotencyKeys: {} };
    state.ui.rdp.idempotencyKeys = state.ui.rdp.idempotencyKeys || {};
    const idempotencyKey = state.ui.rdp.idempotencyKeys[workflow]
      || `ui-${workflow}-${windowRef.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
    state.ui.rdp.idempotencyKeys[workflow] = idempotencyKey;
    try {
      const result = await requestJson("/rdp/v2/runs", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: {
          workflow,
          idempotency_key: idempotencyKey,
          payload: { source: "operator_ui" },
        },
      });
      delete state.ui.rdp.idempotencyKeys[workflow];
      const run = result?.run || {};
      const replayText = result?.idempotent_replay ? "（已识别为同一次请求）" : "";
      mergeRunIntoState(run);
      setFlash(
        state,
        "info",
        `${label}已创建：${run.run_id || "Run"}，状态 ${run.status || "queued"}${replayText}。`,
      );
      finishAction();
      actionFinished = true;
      await refreshPanels(["rdpRuns"]);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      if (!actionFinished) finishAction();
    }
  }

  async function openRun(runId, target = null) {
    if (!runId) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在读取运行详情…");
    try {
      const detail = await requestJson(`/rdp/v2/runs/${encodeURIComponent(runId)}`);
      openDrawer(buildRunDrawer(detail), target);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function cancelRun(runId) {
    if (!runId || !windowRef.confirm(`确认取消运行 ${truncateForConfirm(runId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在请求取消运行…");
    let actionFinished = false;
    try {
      const result = await requestJson(`/rdp/v2/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
      });
      mergeRunIntoState(result?.run);
      setFlash(state, "info", `运行 ${truncateForConfirm(runId)} 已进入 ${result?.run?.status || "取消流程"}。`);
      finishAction();
      actionFinished = true;
      await refreshPanels(["rdpRuns"]);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      if (!actionFinished) finishAction();
    }
  }

  async function retryRun(runId) {
    if (!runId) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在创建新的执行尝试…");
    let actionFinished = false;
    try {
      const result = await requestJson(`/rdp/v2/runs/${encodeURIComponent(runId)}/retry`, {
        method: "POST",
      });
      mergeRunIntoState(result?.run);
      setFlash(state, "info", `运行 ${truncateForConfirm(runId)} 已创建尝试 ${result?.attempts?.length || "—"}。`);
      finishAction();
      actionFinished = true;
      await refreshPanels(["rdpRuns"]);
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      if (!actionFinished) finishAction();
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
          message = `${truncateForConfirm(recommendationId)} 已批准参数候选。下一步请到“待发布候选”里运行 Gate 或创建发布。`;
        } else if (recommendationType === "keep_active") {
          message = `${truncateForConfirm(recommendationId)} 已同意“保持当前”。这轮不会创建新发布。`;
        } else if (recommendationType === "lower_priority") {
          message = `${truncateForConfirm(recommendationId)} 已同意“降低优先级”。这轮不会创建新发布。`;
        } else if (recommendationType === "pause") {
          message = `${truncateForConfirm(recommendationId)} 已同意“暂停”。这轮不会创建新发布。`;
        } else if (recommendationType === "require_review") {
          message = `${truncateForConfirm(recommendationId)} 已转入人工复核，这轮不会创建新发布。`;
        }
        setFlash(state, "info", message);
      } else {
        setFlash(state, "warning", result.message || "审批失败。");
      }
      await refreshDashboard({ manual: true });
      if (result.ok && result.recommendation) {
        const approved = result.recommendation;
        const pending = state.data.rdpControl?.pending_recommendations || [];
        const controlItem = pending.find((item) => item.recommendation_id === recommendationId);
        if (controlItem) Object.assign(controlItem, approved);
        const workbench = state.data.rdpWorkbenchItems;
        if (workbench?.items) {
          workbench.items = workbench.items.filter(
            (item) => item.recommendation_id !== recommendationId,
          );
        }
        if (recommendationType === "parameter_upgrade" && workbench) {
          workbench.release_candidates = workbench.release_candidates || { items: [] };
          workbench.release_candidates.items = [
            {
              family: approved.family,
              timeframe: approved.timeframe,
              recommendation_id: recommendationId,
              headline: "已批准，待发布",
              decision_summary: "这组参数已经批准，下一步可以运行 Gate 或创建发布。",
              created_at: approved.approved_at || approved.created_at,
              actions: [
                { label: "运行 Gate", ui_action: "rdp-run-gate", value: recommendationId, enabled: true },
                { label: "创建发布", ui_action: "rdp-create-release", value: recommendationId, enabled: true },
              ],
            },
            ...(workbench.release_candidates.items || []).filter(
              (item) => item.recommendation_id !== recommendationId,
            ),
          ];
        }
        if (typeof renderShell === "function") renderShell();
      }
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
    if (!windowRef.confirm(`确认审批并发布 ${truncateForConfirm(recommendationId)} 吗？`)) return;
    if (!ensureNotBusy()) return;
    const finishAction = beginAction(null, "正在审批并发布…");
    try {
      // Path B：原本要跑 `/approve` + `/releases/create` 两个独立 HTTP 请求，
      // 中间任何一步失败都会留下 "approved 但没发布" 的中间态。改用服务端
      // 原子端点 `/recommendations/{id}/approve-and-release`，后端把 approve →
      // gate → release record → apply 打包成一条 audit 链；前端只需要处理一个
      // 响应、三种结果：成功 / integrity 阻断 / 部分失败（approve 已落但 gate/apply
      // 未通过）。
      const windowHours = defaultObservationWindowHours();
      const result = await requestJson(
        `/rdp/recommendations/${encodeURIComponent(recommendationId)}/approve-and-release`,
        {
          method: "POST",
          body: {
            actor: "operator",
            approval_notes: "UI 审批并发布",
            release_notes: "UI 审批并发布",
            observation_window_hours: windowHours,
          },
        },
      );
      const release = result.release || {};
      const applyOutcome = release.apply_result;
      if (result.integrity_blocked) {
        setFlash(state, "warning", result.message || "Step2 数据完整性检查未通过，审批已拒绝。");
      } else if (result.ok) {
        setFlash(state, "info", `${truncateForConfirm(recommendationId)} 已审批并发布。`);
      } else if (applyOutcome === "blocked_by_gate") {
        setFlash(state, "warning", result.message || "Pre-apply gate 阻断；审批已落库，可先处理阻断原因再重试发布。");
      } else if (applyOutcome === "failed") {
        setFlash(state, "warning", result.message || "应用失败；审批已落库，可在发布面板单独重试。");
      } else {
        setFlash(state, "warning", result.message || "审批并发布未成功。");
      }
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function runObservation(releaseId, target = null) {
    if (!releaseId) return;
    // release_id 走 data-value，观察窗口走 data-hours。之前两者被塞进同一个
    // data-value 再用 "|" split，release_id 里一旦出现 "|" 就会把 hours 吃掉。
    const defaultHoursRaw = target?.dataset?.hours;
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
      const tokenPayload = await requestJson("/rdp/operator-tokens", {
        method: "POST",
        body: { action: "rollback" },
      });
      const rollbackToken = tokenPayload?.token;
      if (!rollbackToken) {
        throw new Error("回滚令牌签发失败，请刷新后重试。");
      }
      const result = await requestJson("/rdp/parameters/rollback", {
        method: "POST",
        headers: { "X-Rdp-Apply-Token": rollbackToken },
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
    "rdp-open-run": (runId, target) => openRun(runId, target),
    "rdp-cancel-run": (runId) => cancelRun(runId),
    "rdp-retry-run": (runId) => retryRun(runId),
    "rdp-approve-and-apply": (recommendationId) => approveAndCreateRelease(recommendationId),
    "rdp-approve-only": (recommendationId) => approveOnly(recommendationId),
    "rdp-apply-only": (recommendationId) => applyOnly(recommendationId),
    "rdp-reject-recommendation": (recommendationId) => rejectRecommendation(recommendationId),
    "rdp-rollback-parameters": (combo) => rollbackParameters(combo),
    "rdp-run-gate": (recommendationId) => runGate(recommendationId),
    "rdp-create-release": (recommendationId) => createRelease(recommendationId),
    "rdp-run-observation": (releaseId, target) => runObservation(releaseId, target),
    "rdp-approve-tuning-proposal": (proposalId) => approveTuningProposal(proposalId),
    "rdp-reject-tuning-proposal": (proposalId) => rejectTuningProposal(proposalId),
  };
}
