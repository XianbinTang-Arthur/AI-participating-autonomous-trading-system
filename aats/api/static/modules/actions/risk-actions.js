import { textOrFallback } from "../copy.js";
import { buildReconciliationDrawer } from "../detail-drawers.js";
import { ensureNotBusy, setFlash } from "../flash.js";
import { buildPhase1ShadowDrawer } from "../shadow-drawer.js";
import { DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS } from "../store.js";
import { localizeError } from "../terms.js";

// Rebaseline can proxy through gateway -> execution and perform OKX account
// refresh + baseline import + auto-resume. The command bridge allows 90s and
// field runs have been ~35-40s, so the 30s default request timeout is too low.
export const REBASELINE_REQUEST_TIMEOUT_MS = 120_000;

export function createRiskActionHandlers({
  activeExitExecutionHistoryState,
  activeExitExecutionHistoryView,
  activePhase1ShadowBlocker,
  beginAction,
  controlPermissionMessage,
  ensureExitExecutionHistoryState,
  localizedRecoveryReasons,
  openDrawer,
  refreshDashboard,
  renderBanners,
  requestJson,
  runAction,
  runDangerousAction,
  scrollExitExecutionWorkspaceIntoView,
  state,
  syncActiveViewLocationState,
  syncExitExecutionHistoryFilterRoots,
  syncExitExecutionHistoryFiltersAcrossViews,
}) {
  function defaultBlockerActionReason(actionId) {
    const map = {
      "reconcile-now": "operator_validate_from_blocker_panel",
      "accept-rebaseline": "operator_rebaseline_from_blocker_panel",
      "resume-system": "operator_resume_from_blocker_panel",
      "halt-system": "operator_keep_halted_from_blocker_panel",
      "refresh-exchange-state": "operator_refresh_exchange_state_from_blocker_panel",
      "acknowledge-phase1-shadow": "operator_review_phase1_shadow_from_blocker_panel",
      "ai-review-restore": "operator_restore_ai_from_blocker_panel",
      "ai-review-degrade-to-baseline": "operator_degrade_to_baseline_from_blocker_panel",
    };
    return map[actionId] || `operator_${actionId}`;
  }

  function blockerActionPendingLabel(actionId) {
    const map = {
      "reconcile-now": "正在重新对账…",
      "accept-rebaseline": "正在确认新基线…",
      "resume-system": "正在恢复自动运行…",
      "halt-system": "正在保持暂停状态…",
      "refresh-exchange-state": "正在刷新交易所状态…",
      "acknowledge-phase1-shadow": "正在记录影子核查结果…",
      "ai-review-restore": "正在恢复 AI 决策…",
      "ai-review-degrade-to-baseline": "正在切到仅基础策略运行…",
    };
    return map[actionId] || "正在执行阻断处理动作…";
  }

  function blockerActionSuccessMessage(actionId) {
    const map = {
      "reconcile-now": "对账已刷新。",
      "accept-rebaseline": "新基线已确认。",
      "resume-system": "恢复自动运行请求已提交。",
      "halt-system": "系统会继续保持暂停状态。",
      "refresh-exchange-state": "交易所状态已刷新。",
      "acknowledge-phase1-shadow": "已记录影子兼容层人工核查结果。",
      "ai-review-restore": "AI 复核已处理，已恢复 AI 决策资格。",
      "ai-review-degrade-to-baseline": "AI 复核已处理，系统将以仅基础策略继续运行。",
    };
    return map[actionId] || "阻断处理动作已完成。";
  }

  function blockerActionConfirmMessage(actionId) {
    const map = {
      "accept-rebaseline": "确认把当前状态接受为新基线吗？这会覆盖旧的恢复参照。",
      "halt-system": "确认继续保持暂停状态吗？这会阻止系统继续自动交易。",
      "acknowledge-phase1-shadow": "确认已完成人工核查吗？这会留下当前影子兼容层状态记录，但不会解除阻断。",
      "ai-review-restore": "确认恢复 AI 决策链路吗？这会清除当前 AI 结果复核阻断。",
      "ai-review-degrade-to-baseline": "确认改为仅基础策略继续运行吗？这会解除当前 AI 复核阻断，并把 AI 决策权降为仅基础策略。",
    };
    return map[actionId] || "";
  }

  async function triggerReconciliationValidate(target = null) {
    await runAction("/reconciliation/validate", { reason: "ui_manual_validate" }, "已提交人工对账请求。", {
      target,
      pendingLabel: "正在重新对账…",
    });
  }

  async function triggerRebaseline(target = null) {
    await runDangerousAction({
      path: "/system/rebaseline",
      body: { reason: "ui_manual_rebaseline" },
      successMessage: "已把当前账户状态接受为新基线。",
      confirmMessage: "确认把当前账户、仓位和挂单状态接受为新的人工基线吗？这会覆盖旧的恢复参照。",
      target,
      pendingLabel: "正在重设基线…",
      requestOptions: { timeout: REBASELINE_REQUEST_TIMEOUT_MS },
    });
  }

  async function triggerResume(target = null) {
    await runAction("/system/resume", { reason: "ui_manual_resume" }, "已提交恢复自动运行请求。", {
      target,
      pendingLabel: "正在恢复自动运行…",
    });
  }

  async function triggerHalt(target = null) {
    await runDangerousAction({
      path: "/system/halt",
      body: { reason: "ui_manual_halt" },
      successMessage: "系统已暂停自动运行。",
      confirmMessage: "确认立即暂停自动运行吗？",
      target,
      pendingLabel: "正在暂停自动运行…",
    });
  }

  async function recordScalingReview(verdict, target = null) {
    if (!verdict) return;
    const payloadMap = {
      approve_scale_up: {
        reason: "ui_scaling_review_approve_scale_up",
        successMessage: "已记录允许放量的人工评审结论。",
        pendingLabel: "正在记录放量评审…",
        confirmMessage: "确认记录“允许放量”评审结论吗？这表示系统已满足进入下一档资金评审的条件。",
      },
      continue_small_capital: {
        reason: "ui_scaling_review_continue_small_capital",
        successMessage: "已记录继续小资金试盘的人工评审结论。",
        pendingLabel: "正在记录评审结论…",
        confirmMessage: "",
      },
      shrink_trial: {
        reason: "ui_scaling_review_shrink_trial",
        successMessage: "已记录建议缩容试盘的人工评审结论。",
        pendingLabel: "正在记录缩容评审…",
        confirmMessage: "确认记录“建议缩容试盘”评审结论吗？",
      },
      pause_trial: {
        reason: "ui_scaling_review_pause_trial",
        successMessage: "已记录建议暂停试盘的人工评审结论。",
        pendingLabel: "正在记录暂停评审…",
        confirmMessage: "确认记录“建议暂停试盘”评审结论吗？",
      },
    };
    const payload = payloadMap[verdict];
    if (!payload) return;
    if (payload.confirmMessage && !window.confirm(payload.confirmMessage)) return;
    await runAction(
      "/system/scaling-review",
      {
        verdict,
        reason: payload.reason,
      },
      payload.successMessage,
      {
        target,
        pendingLabel: payload.pendingLabel,
      },
    );
  }

  async function recordTrialReview(target = null) {
    await runAction(
      "/system/trial-review/record",
      {
        reason: "ui_trial_review_snapshot",
      },
      "已记录本次试盘复盘摘要。",
      {
        target,
        pendingLabel: "正在记录复盘摘要…",
      },
    );
  }

  async function recordTrialReviewAction(actionType, target = null) {
    if (!actionType) return;
    const payloadMap = {
      review_snapshot: {
        reason: "ui_trial_review_snapshot",
        successMessage: "已记录本次试盘复盘摘要。",
        pendingLabel: "正在记录复盘摘要…",
        confirmMessage: "",
      },
      reset_trial_guard: {
        reason: "ui_trial_guard_manual_reset",
        successMessage: "已重置试盘守护，新的试盘样本窗口会从本次操作后重新开始。",
        pendingLabel: "正在重置试盘守护…",
        confirmMessage: "确认人工重置试盘守护吗？这会清空当前试盘守护的历史观察窗口，但系统仍会保持暂停，后续还需要你手动恢复自动运行。",
      },
      continue_small_capital: {
        reason: "ui_trial_review_continue_small_capital",
        successMessage: "已记录继续小资金试盘的处理结论。",
        pendingLabel: "正在记录处理结论…",
        confirmMessage: "",
      },
      shrink_trial: {
        reason: "ui_trial_review_shrink_trial",
        successMessage: "已记录缩小试盘规模的处理结论。",
        pendingLabel: "正在记录缩容结论…",
        confirmMessage: "确认记录“缩小试盘规模”处理结论吗？",
      },
      pause_trial: {
        reason: "ui_trial_review_pause_trial",
        successMessage: "已记录暂停试盘并复盘的处理结论。",
        pendingLabel: "正在记录暂停结论…",
        confirmMessage: "确认记录“暂停试盘并复盘”处理结论吗？",
      },
      approve_scale_up: {
        reason: "ui_trial_review_approve_scale_up",
        successMessage: "已记录进入下一档资金评审的处理结论。",
        pendingLabel: "正在记录放量评审…",
        confirmMessage: "确认记录“进入下一档资金评审”处理结论吗？",
      },
    };
    const payload = payloadMap[actionType];
    if (!payload) return;
    if (payload.confirmMessage && !window.confirm(payload.confirmMessage)) return;
    await runAction(
      "/system/trial-review/action",
      {
        action_type: actionType,
        reason: payload.reason,
      },
      payload.successMessage,
      {
        target,
        pendingLabel: payload.pendingLabel,
      },
    );
  }

  function normalizeExitExecutionParentIntentId(value) {
    const normalized = String(value || "").trim();
    return normalized || null;
  }

  function exitExecutionActionFlashMessage(result, fallback = "操作已提交。") {
    const base = textOrFallback(result?.message, fallback);
    const blocker = result?.details?.current_blocker_after_action;
    if (!blocker || typeof blocker !== "object") {
      return base;
    }
    const summary = textOrFallback(
      blocker.summary,
      localizeError(blocker.code, "当前还有未解除的退出任务阻断。"),
    );
    return `${base} 当前仍卡在：${summary}`;
  }

  // #28 修复说明：原本这里只写了"和 app.js 的 activateStrategyProfile 一样"的
  // 一句指针，要看完整规范必须切到 app.js。这给阅读 risk-actions 的人增加了
  // 不必要的跳转成本（尤其是新人），所以把规范完整版搬过来。activateStrategyProfile
  // 那边的注释已经反过来引用本块即可。
  //
  // ── confirm → ensureNotBusy → beginAction 顺序契约 ──
  //
  // 所有"会改变运行态、且可能弹 confirm 对话框"的人工动作都必须遵守这一顺序。
  // 这套约束不是审美问题，是为了避免下面这些可观测的 UI 错乱：
  //
  //   1. **先 confirm 再 ensureNotBusy** —— 用户在 confirm 上犹豫时（典型场景：
  //      值班同事打电话过来确认），系统其它通道可能已经改了 actionInFlight / 启动
  //      新的刷新。confirm 后再检查一次 busy 状态，能挡住"用户按了确定，但其实
  //      此时已经有别的 action 占线"的竞态。
  //
  //   2. **先 confirm 再 beginAction** —— beginAction 会立刻把按钮变灰、写一行
  //      "正在处理…"的 flash、把 actionInFlight 翻成 true。如果先 begin 后 confirm，
  //      用户在对话框上点取消，UI 会闪现一次"忙碌"再立即恢复，看起来像 bug。
  //      此外，beginAction 还会取消已经排队的 scheduled refresh，被取消的 confirm
  //      会让自动刷新静默丢一拍。
  //
  //   3. **ensureNotBusy 必须在 beginAction 之前** —— ensureNotBusy 只读不写
  //      （它检查 actionInFlight 等字段）。一旦 begin，actionInFlight 自身就被翻
  //      成 true，再调 ensureNotBusy 就会看到自己的 latch 并误报。
  //
  // 所以正确的写法永远是：
  //
  //     if (confirmMessage && !window.confirm(confirmMessage)) return;
  //     if (!ensureNotBusy(state, renderBanners)) return;
  //     const finishAction = beginAction(target, pendingLabel);
  //     try { ... } finally { finishAction(); }
  //
  // 同样的顺序也是 app.js::activateStrategyProfile 和 admin-actions.js 里
  // 所有 confirm-first handler 的实现，改任何一个时记得同步检查另外两处。
  async function runExitExecutionAction({
    path,
    body,
    successMessage,
    target = null,
    pendingLabel = "正在提交请求…",
    confirmMessage = "",
  } = {}) {
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    if (!ensureNotBusy(state, renderBanners)) return;
    const finishAction = beginAction(target, pendingLabel);
    try {
      const result = await requestJson(path, { method: "POST", body });
      setFlash(state, "info", exitExecutionActionFlashMessage(result, successMessage));
      await refreshDashboard({ manual: true });
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    } finally {
      finishAction();
    }
  }

  async function triggerExitExecutionRefresh(value, target = null) {
    const parentIntentId = normalizeExitExecutionParentIntentId(value);
    await runExitExecutionAction({
      path: "/system/exit-execution/refresh",
      body: {
        reason: "ui_refresh_exit_execution_state",
        parent_intent_id: parentIntentId,
      },
      successMessage: "已提交退出任务状态刷新请求。",
      target,
      pendingLabel: "正在刷新退出任务状态…",
    });
  }

  async function triggerExitExecutionRetryLimitLookup(value, target = null) {
    const parentIntentId = normalizeExitExecutionParentIntentId(value);
    await runExitExecutionAction({
      path: "/system/exit-execution/retry-limit-lookup",
      body: {
        reason: "ui_retry_exit_execution_limit_lookup",
        parent_intent_id: parentIntentId,
      },
      successMessage: "已提交退出任务拆单上限重试请求。",
      target,
      pendingLabel: "正在重试拆单上限查询…",
    });
  }

  async function triggerExitExecutionSafeCancel(value, target = null) {
    const parentIntentId = normalizeExitExecutionParentIntentId(value);
    await runExitExecutionAction({
      path: "/system/exit-execution/safe-cancel",
      body: {
        reason: "ui_safe_cancel_exit_execution",
        parent_intent_id: parentIntentId,
      },
      successMessage: "已提交退出任务安全取消请求。",
      target,
      pendingLabel: "正在安全取消退出任务…",
      confirmMessage: "确认停止这条退出任务，并撤销当前仍可取消的子订单吗？",
    });
  }

  async function applyExitExecutionHistoryWorkspaceFilters(target = null) {
    const historyState = activeExitExecutionHistoryState();
    historyState.offset = 0;
    syncExitExecutionHistoryFiltersAcrossViews(activeExitExecutionHistoryView());
    if (state.activeView === "exitExecution") {
      syncActiveViewLocationState({ pushHistory: false });
    }
    await refreshDashboard({ manual: true });
    scrollExitExecutionWorkspaceIntoView(target);
  }

  async function resetExitExecutionHistoryWorkspaceFilters(target = null) {
    const riskHistoryState = ensureExitExecutionHistoryState("risk");
    const exitExecutionHistoryState = ensureExitExecutionHistoryState("exitExecution");
    Object.assign(riskHistoryState, DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS, { offset: 0 });
    Object.assign(exitExecutionHistoryState, DEFAULT_EXIT_EXECUTION_HISTORY_FILTERS, { offset: 0 });
    if (state.activeView === "exitExecution") {
      syncActiveViewLocationState({ pushHistory: false });
    }
    syncExitExecutionHistoryFilterRoots();
    await refreshDashboard({ manual: true });
    scrollExitExecutionWorkspaceIntoView(target);
  }

  async function paginateExitExecutionHistory(direction, target = null) {
    const historyState = activeExitExecutionHistoryState();
    const limit = Math.max(Number(historyState.limit) || 20, 1);
    const currentOffset = Math.max(Number(historyState.offset) || 0, 0);
    let nextOffset = currentOffset;
    if (direction === "next") {
      nextOffset = currentOffset + limit;
    } else if (direction === "prev") {
      nextOffset = Math.max(currentOffset - limit, 0);
    } else {
      nextOffset = 0;
    }
    historyState.offset = nextOffset;
    if (state.activeView === "exitExecution") {
      syncActiveViewLocationState({ pushHistory: false });
    }
    await refreshDashboard({ manual: true });
    scrollExitExecutionWorkspaceIntoView(target);
  }

  async function inspectReconciliation(reconciliationId) {
    if (!reconciliationId) return;
    try {
      const detail = await requestJson(`/reconciliation/${encodeURIComponent(reconciliationId)}`);
      openDrawer(
        buildReconciliationDrawer(detail, {
          recovery: state.data.systemRecovery?.recovery || {},
          latestReconciliationId: state.data.reconciliationLatest?.reconciliation?.reconciliation_id || "",
          uiHints: {
            recoveryReasonsText: localizedRecoveryReasons(),
            controlPermissionMessage: controlPermissionMessage(),
          },
        }),
      );
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    }
  }

  async function inspectPhase1Shadow() {
    try {
      const [detailResult, historyResult] = await Promise.allSettled([
        requestJson("/system/shadow"),
        requestJson("/system/shadow/history?limit=12"),
      ]);
      const detail = detailResult.status === "fulfilled"
        ? detailResult.value
        : state.data.phase1Shadow || state.data.metrics?.phase1_shadow || {};
      const history = historyResult.status === "fulfilled" ? historyResult.value : { history: [] };
      if (!detail || !Object.keys(detail).length) {
        throw detailResult.status === "rejected" ? detailResult.reason : new Error("当前还没有影子兼容层详情。");
      }
      openDrawer(
        buildPhase1ShadowDrawer(detail, {
          shadowBlocker: activePhase1ShadowBlocker(),
          uiHints: {
            controlPermissionMessage: controlPermissionMessage(),
          },
          history: history?.history || [],
        }),
      );
      if (detailResult.status === "rejected" || historyResult.status === "rejected") {
        setFlash(state, "warning", "已打开当前已缓存的影子兼容层状态，部分历史详情暂时没有返回。");
        renderBanners();
      }
    } catch (error) {
      setFlash(state, "danger", error instanceof Error ? error.message : String(error));
      renderBanners();
    }
  }

  async function triggerBlockerAction(value, target = null) {
    if (!value) return;
    const [actionId, blocker] = String(value).split("::");
    if (!actionId) return;
    const confirmMessage = blockerActionConfirmMessage(actionId);
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    const blockerControl = state.data.blockerControl || {};
    const reason = defaultBlockerActionReason(actionId);
    await runAction(
      `/system/blocker-actions/${encodeURIComponent(actionId)}`,
      {
        panel_version: blockerControl.panel_version || null,
        blocker: blocker || null,
        reason,
      },
      blockerActionSuccessMessage(actionId),
      {
        target,
        pendingLabel: blockerActionPendingLabel(actionId),
        requestOptions: actionId === "accept-rebaseline" ? { timeout: REBASELINE_REQUEST_TIMEOUT_MS } : {},
      },
    );
  }

  return {
    "apply-exit-execution-history-workspace": (_value, target) => applyExitExecutionHistoryWorkspaceFilters(target),
    "inspect-reconciliation": (value) => inspectReconciliation(value),
    "inspect-shadow": () => inspectPhase1Shadow(),
    "paginate-exit-execution-history": (value, target) => paginateExitExecutionHistory(value, target),
    "record-scaling-review": (value, target) => recordScalingReview(value, target),
    "record-trial-review": (_value, target) => recordTrialReview(target),
    "record-trial-review-action": (value, target) => recordTrialReviewAction(value, target),
    "reset-exit-execution-history-workspace": (_value, target) => resetExitExecutionHistoryWorkspaceFilters(target),
    "trigger-blocker-action": (value, target) => triggerBlockerAction(value, target),
    "trigger-exit-execution-refresh": (value, target) => triggerExitExecutionRefresh(value, target),
    "trigger-exit-execution-retry-limit-lookup": (value, target) => triggerExitExecutionRetryLimitLookup(value, target),
    "trigger-exit-execution-safe-cancel": (value, target) => triggerExitExecutionSafeCancel(value, target),
    "trigger-halt": (_value, target) => triggerHalt(target),
    "trigger-rebaseline": (_value, target) => triggerRebaseline(target),
    "trigger-reconciliation-validate": (_value, target) => triggerReconciliationValidate(target),
    "trigger-resume": (_value, target) => triggerResume(target),
  };
}
