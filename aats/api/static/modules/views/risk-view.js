import { actionButton, pill, primaryStatusPanel, responsiveTable, summaryStrip, surfaceCard, timeline } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, middleEllipsis } from "../formatters.js";
import { localizeError, readableState, toneForRuntimeState } from "../terms.js";

export function renderRiskSections(data) {
  const account = data.accountState || {};
  const portfolio = data.portfolio?.portfolio || {};
  const blockers = data.blockers?.blockers || [];
  const blockerHistoryPayload = data.blockerHistory || {};
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const reconciliationRecentPayload = data.reconciliationRecent || {};
  const mismatchSummary = data.reconciliationLatest?.mismatch_summary || {};
  const billsSummary = data.reconciliationLatest?.exchange_bills_summary || {};
  const recovery = data.systemRecovery?.recovery || {};
  const replay = data.replayStatus || {};
  const replayRecentPayload = data.replayRecentValidations || {};
  const metrics = data.metrics || {};
  const health = data.health || {};
  const uiHints = data.uiHints || {};

  return {
    riskHero: primaryStatusPanel({
      eyebrow: "风险与恢复",
      headline: riskHeadline({ blockers, reconciliation, recovery }),
      summary: blockers.length
        ? "先看阻断项，再看对账和恢复状态。"
        : "当前没有硬阻断，但仍要确认对账、账户快照和恢复条件。",
      tone: riskTone({ blockers, reconciliation, recovery, health }),
      actions: reconciliation?.reconciliation_id ? actionButton("查看最新对账详情", "inspect-reconciliation", reconciliation.reconciliation_id) : "",
      pills: [
        pill(`运行状态 ${readableState(health.runtime_state || health.overall_status || "unknown")}`, toneForRuntimeState(health.runtime_state || health.overall_status)),
        pill(`允许继续交易 ${booleanWord(recovery.safe_to_trade)}`, recovery.safe_to_trade ? "positive" : "danger"),
        pill(`等待人工确认 ${booleanWord(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
      ],
      metrics: [
        {
          label: "当前阻断数",
          value: formatNumber(blockers.length, 0),
          meta: blockers[0] ? localizeError(blockers[0].blocker) : "当前没有阻断项",
          tone: blockers.length > 0 ? "danger" : "positive",
        },
        {
          label: "最新对账",
          value: readableState(reconciliation?.severity || "unknown"),
          meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "当前还没有最新对账编号"),
          tone: reconciliation?.halt_required ? "danger" : reconciliation?.severity ? "warning" : "neutral",
        },
        {
          label: "恢复状态",
          value: readableState(recovery.recovery_state || "unknown"),
          meta: uiHints.recoveryReasonsText || listText(recovery.resume_blocked_reasons, "当前没有额外恢复说明"),
          tone: recovery.safe_to_trade ? "positive" : recovery.review_required ? "warning" : "danger",
        },
        {
          label: "账户快照",
          value: booleanWord(account.fresh),
          meta: formatMaybeTimestamp(account.last_refresh_timestamp),
          tone: account.fresh ? "positive" : "warning",
        },
      ],
    }),
    riskActions: surfaceCard({
      title: "异常对账处理",
      kicker: "下一步操作",
      copy: reconciliationActionCopy({ reconciliation, recovery }),
      content: renderReconciliationControls({ reconciliation, recovery, uiHints }),
    }),
    riskEvidence: surfaceCard({
      title: "第一屏判断依据",
      kicker: "阻断 / 对账 / 恢复",
      copy: "把最影响自动交易资格的三条证据放在同一处，减少来回跳读。",
      content: summaryStrip([
        {
          label: "首要阻断",
          value: blockers[0] ? localizeError(blockers[0].blocker) : "当前没有阻断项",
          meta: blockers[0] ? localizeError(blockers[0].recommended_action || blockers[0].blocker) : "当前没有需要立刻处理的阻断项",
          tone: blockers[0] ? "danger" : "positive",
        },
        {
          label: "最新对账",
          value: readableState(reconciliation?.severity || "unknown"),
          meta: reconciliation?.halt_required ? "最新对账已要求暂停交易" : middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "当前没有对账结论"),
          tone: reconciliation?.halt_required ? "danger" : reconciliation?.severity ? "warning" : "neutral",
        },
        {
          label: "恢复资格",
          value: booleanWord(recovery.resume_eligible),
          meta: uiHints.recoveryReasonsText || listText(recovery.resume_blocked_reasons, "当前没有额外恢复限制说明"),
          tone: recovery.resume_eligible ? "positive" : "warning",
        },
      ]),
    }),
    riskAccount: surfaceCard({
      title: "账户与权益",
      kicker: "资金安全",
      copy: "这里主要看账户是否可信，不把账户状态和阻断原因混在一起。",
      content: summaryStrip([
        { label: "总权益", value: formatNumber(portfolio.total_equity), meta: "账户当前总价值", tone: "info" },
        { label: "已实现收益", value: formatNumber(portfolio.realized_pnl), meta: "已经确认的盈亏", tone: Number(portfolio.realized_pnl || 0) >= 0 ? "positive" : "warning" },
        { label: "未实现收益", value: formatNumber(portfolio.unrealized_pnl), meta: "持仓浮动盈亏", tone: Number(portfolio.unrealized_pnl || 0) >= 0 ? "positive" : "warning" },
        { label: "保证金占用", value: formatNumber(portfolio.margin_usage), meta: `总敞口 ${formatNumber(portfolio.gross_exposure)}`, tone: Number(portfolio.margin_usage || 0) > 0 ? "warning" : "neutral" },
      ]),
    }),
    riskRecovery: surfaceCard({
      title: "恢复与回放",
      kicker: "可恢复性",
      copy: "恢复和回放共同决定系统在异常后还能不能继续被信任。",
      content: summaryStrip([
        { label: "恢复状态", value: readableState(recovery.recovery_state || "unknown"), meta: recovery.safe_to_trade ? "当前允许继续自动交易" : "当前不允许继续自动交易", tone: recovery.safe_to_trade ? "positive" : recovery.review_required ? "warning" : "danger" },
        { label: "人工复核", value: booleanWord(recovery.review_required), meta: recovery.rebaseline_available ? "允许重新确认基线" : "当前不允许重建基线", tone: recovery.review_required ? "warning" : "positive" },
        { label: "回放健康度", value: booleanWord(replay.healthy), meta: textOrFallback(replay.last_validation?.decision_id, "最近没有回放验证"), tone: replay.healthy ? "positive" : "warning" },
        { label: "最近回放时间", value: formatMaybeTimestamp(replay.last_validation?.validated_at), meta: formatRelativeAge(replay.last_validation?.validated_at), tone: replay.last_validation?.validated_at ? "info" : "neutral" },
      ]),
    }),
    riskBlockers: surfaceCard({
      title: "当前阻断明细",
      kicker: "阻断列表",
      copy: blockers.length ? "下面这些阻断项正在直接影响交易资格。" : "当前没有阻断项，说明系统没有被明确拦停。",
      content: timeline(
        blockers.map((item) => ({
          title: localizeError(item.blocker),
          subtitle: item.subsystem ? `来源：${readableState(item.subsystem)}` : "系统阻断",
          detail: localizeError(item.recommended_action || item.blocker),
          pill: pill(item.submit_only ? "仅阻断发单" : "阻断执行", item.affects_execution ? "danger" : "warning"),
        })),
        "当前没有阻断项。"
      ),
    }),
    riskMetrics: surfaceCard({
      title: "风险观察指标",
      kicker: "辅助判断",
      copy: "这些指标用来判断系统是不是开始偏离可被信任的状态。",
      classes: "is-muted",
      content: summaryStrip([
        { label: "拒单数", value: formatNumber(metrics.rejection_count, 0), meta: "偏高时要检查门禁和执行条件", tone: Number(metrics.rejection_count || 0) > 0 ? "warning" : "positive" },
        { label: "活动委托数", value: formatNumber(metrics.current_open_order_count, 0), meta: "需要和阻断项、保留额度一起看", tone: Number(metrics.current_open_order_count || 0) > 0 ? "warning" : "positive" },
        { label: "对账异常数", value: formatNumber(metrics.reconciliation_mismatch_count, 0), meta: "持续非零说明状态还没真正收敛", tone: Number(metrics.reconciliation_mismatch_count || 0) > 0 ? "danger" : "positive" },
        { label: "账户快照", value: booleanWord(account.ready), meta: listText(account.blockers, "当前没有额外账户阻断说明"), tone: account.ready ? "positive" : "warning" },
      ]),
    }),
    riskReconciliationHistory: surfaceCard({
      title: "最近对账报告",
      kicker: "历史核对记录",
      copy: "用来判断异常是一次性事件，还是在持续反复出现。",
      content: `${renderReconciliationHistory(reconciliationRecentPayload)}${renderPaginationFooter({ payload: reconciliationRecentPayload, key: "reconciliations", singular: "对账报告", loadAction: "load-more-reconciliations", collapseAction: "collapse-reconciliations" })}`,
    }),
    riskBlockerHistory: surfaceCard({
      title: "最近 blocker 快照",
      kicker: "阻断轨迹",
      copy: "观察阻断是偶发事件还是持续性的运行问题。",
      content: `${renderBlockerHistory(blockerHistoryPayload)}${renderPaginationFooter({ payload: blockerHistoryPayload, key: "history", singular: "blocker 快照", loadAction: "load-more-blocker-history", collapseAction: "collapse-blocker-history" })}`,
    }),
    riskReplayHistory: surfaceCard({
      title: "最近 replay 验证",
      kicker: "重建一致性",
      copy: "如果 divergence 持续增加，说明恢复可信度在下降。",
      content: `${renderReplayValidationHistory(replayRecentPayload)}${renderPaginationFooter({ payload: replayRecentPayload, key: "validations", singular: "replay 验证", loadAction: "load-more-replay-validations", collapseAction: "collapse-replay-validations" })}`,
    }),
    riskReconciliation: surfaceCard({
      title: "最新对账上下文",
      kicker: "本地记录与交易所",
      copy: "这里保留最近一次对账的关键上下文，不再和首屏裁决抢主次。",
      classes: "is-muted",
      content: summaryStrip([
        { label: "对账级别", value: readableState(reconciliation?.severity || "unknown"), meta: middleEllipsis(reconciliation?.reconciliation_id, 10, 6, "当前还没有对账编号"), tone: reconciliation?.halt_required ? "danger" : reconciliation?.severity ? "warning" : "neutral" },
        { label: "是否要求停机", value: booleanWord(reconciliation?.halt_required), meta: reconciliation?.exchange_comparison_enabled ? "已比对交易所" : "仅校验本地记录", tone: reconciliation?.halt_required ? "danger" : "positive" },
        { label: "差异原因", value: listText(mismatchSummary.mismatch_reasons, "当前没有额外差异原因"), meta: listText(mismatchSummary.mismatch_categories, "当前没有额外差异分类"), tone: mismatchSummary.mismatch_reasons?.length ? "warning" : "positive" },
        { label: "建议动作", value: mismatchSummary.recommended_operator_action ? localizeError(mismatchSummary.recommended_operator_action) : "当前没有额外建议动作", meta: listText(mismatchSummary.safety_impacts, "当前没有额外安全影响说明"), tone: mismatchSummary.recommended_operator_action ? "info" : "neutral" },
      ]),
    }),
    riskBills: surfaceCard({
      title: "交易所账单",
      kicker: "对账辅助证据",
      copy: billsSummary.available ? "最近账单已缓存，可作为交易所侧余额与对账辅助证据。" : "当前还没有最新账单摘要缓存。",
      classes: "is-muted",
      content: summaryStrip([
        { label: "账单数量", value: formatNumber(billsSummary.count || 0, 0), meta: textOrFallback(billsSummary.latest_bill_id, "当前还没有最新账单编号"), tone: Number(billsSummary.count || 0) > 0 ? "info" : "neutral" },
        { label: "最新账单时间", value: formatMaybeTimestamp(billsSummary.latest_bill_ts), meta: formatRelativeAge(billsSummary.latest_bill_ts), tone: billsSummary.latest_bill_ts ? "info" : "neutral" },
        { label: "涉及币种", value: listText(billsSummary.currencies, "当前没有账单币种摘要"), meta: "最近交易所侧账务变动范围", tone: (billsSummary.currencies || []).length ? "warning" : "neutral" },
        { label: "高频账单类别", value: renderBillCategories(billsSummary.top_categories), meta: billsSummary.last_error || "已按类型、子类型和币种聚合", tone: billsSummary.last_error ? "warning" : "positive" },
      ]),
    }),
  };
}

export function renderRiskView(data) {
  const sections = renderRiskSections(data);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.riskHero}</div>
      <div class="span-12">${sections.riskActions}</div>
      <div class="span-12">${sections.riskEvidence}</div>
      <div class="span-4">${sections.riskAccount}</div>
      <div class="span-4">${sections.riskReconciliation}</div>
      <div class="span-4">${sections.riskRecovery}</div>
      <div class="span-12">${sections.riskBills}</div>
      <div class="span-6">${sections.riskBlockers}</div>
      <div class="span-6">${sections.riskMetrics}</div>
      <div class="span-12">${sections.riskReconciliationHistory}</div>
      <div class="span-12">${sections.riskBlockerHistory}</div>
      <div class="span-12">${sections.riskReplayHistory}</div>
    </div>
  `;
}

export function renderReconciliationControls({
  reconciliation = null,
  recovery = {},
  uiHints = {},
  includeInspect = false,
  compact = false,
} = {}) {
  const permissionMessage = textOrFallback(uiHints.controlPermissionMessage, "");
  const canWrite = !permissionMessage;
  const buttons = [];
  if (includeInspect && reconciliation?.reconciliation_id) {
    buttons.push(actionButton("查看对账详情", "inspect-reconciliation", reconciliation.reconciliation_id, "ghost"));
  }
  if (shouldShowValidateAction({ reconciliation, recovery })) {
    buttons.push(
      actionButton("立即对账", "trigger-reconciliation-validate", "", "secondary", {
        disabled: !canWrite,
        title: permissionMessage,
      })
    );
  }
  if (shouldShowRebaselineAction({ reconciliation, recovery })) {
    buttons.push(
      actionButton("接受当前账户为新基线", "trigger-rebaseline", "", "warning", {
        disabled: !canWrite,
        title: permissionMessage,
      })
    );
  }
  if (shouldShowResumeAction({ recovery })) {
    buttons.push(
      actionButton("恢复自动交易", "trigger-resume", "", "primary", {
        disabled: !canWrite || !recovery.resume_eligible,
        title: !canWrite ? permissionMessage : resumeActionHint({ recovery, uiHints }),
      })
    );
  }
  buttons.push(
    actionButton("暂停自动交易", "trigger-halt", "", "danger", {
      disabled: !canWrite,
      title: permissionMessage,
    })
  );
  if (!buttons.length) return `<p class="meta-copy">${reconciliationActionCopy({ reconciliation, recovery })}</p>`;
  return `<div class="stack-actions ${compact ? "table-actions--compact" : ""}">${buttons.join("")}</div>`;
}

export function reconciliationActionCopy({ reconciliation = null, recovery = {}, isHistorical = false } = {}) {
  if (isHistorical) {
    return "这是历史对账记录。下面的操作会作用于当前运行态，请先确认最新对账结论是否仍然一致。";
  }
  if (reconciliation?.halt_required) {
    return "最新对账已要求暂停交易。先核对差异原因；确认交易所当前状态才是正确状态后，再接受为新基线。";
  }
  if (reconciliation?.review_required || shouldShowRebaselineAction({ reconciliation, recovery })) {
    return "当前账实状态需要人工确认。先重新对账或核对交易所账单，确认状态符合预期后再接受为新基线。";
  }
  if (!recovery.safe_to_trade) {
    return "当前还不能恢复自动交易。建议先重新对账，等恢复条件满足后再恢复运行。";
  }
  return "当前没有必须人工处理的对账异常；如果想再次确认状态，可以手动重新对账。";
}

function renderReconciliationHistory(payload) {
  const reconciliations = payload?.reconciliations || [];
  return responsiveTable(
    ["时间", "级别", "差异摘要", "停机", "详情"],
    reconciliations.map((item) => [
      `<div><strong>${formatRelativeAge(item.as_of_ts)}</strong><div class="table-meta">${formatMaybeTimestamp(item.as_of_ts)}</div></div>`,
      `<div><strong>${readableState(item.severity || "unknown")}</strong><div class="table-meta mono">${middleEllipsis(item.reconciliation_id, 10, 6, "当前没有对账编号")}</div></div>`,
      `<div><strong>${listText(item.mismatch_reasons, "当前没有额外差异原因")}</strong><div class="table-meta">${listText(item.mismatch_categories, "当前没有额外差异分类")}</div></div>`,
      `<div><strong>${booleanWord(item.halt_required)}</strong><div class="table-meta">${item.exchange_comparison_enabled ? "已比对交易所" : "仅校验本地记录"}</div></div>`,
      item.reconciliation_id
        ? actionButton(
            item.severity && item.severity !== "CLEAN" ? "查看并处理" : "详情",
            "inspect-reconciliation",
            item.reconciliation_id,
            item.severity && item.severity !== "CLEAN" ? "warning" : "ghost"
          )
        : "",
    ]),
    "最近还没有对账报告。"
  );
}

function renderBlockerHistory(payload) {
  const history = payload?.history || [];
  return responsiveTable(
    ["时间", "运行状态", "阻断摘要", "执行状态"],
    history.map((item) => [
      `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
      `<div><strong>${readableState(item.runtime_state || "unknown")}</strong><div class="table-meta">${readableState(item.operating_state || "unknown")}</div></div>`,
      `<div><strong>${formatNumber((item.blockers || []).length, 0)}</strong><div class="table-meta">${(item.blockers || []).length ? localizeError(item.blockers[0].blocker) : "当前没有阻断项"}</div></div>`,
      `<div><strong>${booleanWord(item.execution_blocked)}</strong><div class="table-meta">${item.halted ? "已暂停" : item.submit_blocked ? "仅阻断发单" : "允许执行"}</div></div>`,
    ]),
    "最近还没有 blocker 快照。"
  );
}

function renderReplayValidationHistory(payload) {
  const validations = payload?.validations || [];
  return responsiveTable(
    ["时间", "健康度", "决策", "差异数", "问题摘要"],
    validations.map((item) => [
      `<div><strong>${formatRelativeAge(item.validated_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.validated_at)}</div></div>`,
      `<div><strong>${booleanWord(item.healthy)}</strong><div class="table-meta">${textOrFallback(item.validation_id, "当前没有验证编号")}</div></div>`,
      `<div><strong>${textOrFallback(item.decision_id, "当前没有决策编号")}</strong><div class="table-meta">${formatNumber(item.replayed_event_count, 0)} 个事件</div></div>`,
      `<div><strong>${formatNumber(item.divergence_count, 0)}</strong><div class="table-meta">基线切换 ${formatNumber(item.baseline_switch_count, 0)}</div></div>`,
      `<div><strong>${issuesSummary(item)}</strong><div class="table-meta">${listText(item.execution_chain_issues, "当前没有执行链异常")}</div></div>`,
    ]),
    "最近还没有 replay 验证记录。"
  );
}

function renderPaginationFooter({ payload, key, singular, loadAction, collapseAction }) {
  const shown = Number(payload?.[key]?.length || 0);
  const total = Number(payload?.total_available || shown);
  const hasMore = Boolean(payload?.has_more);
  const limit = Number(payload?.limit || shown);
  if (!shown) return "";
  return `
    <div class="history-footer">
      <p class="meta-copy">当前显示 ${shown} / ${total} 条${singular}。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${singular}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function renderBillCategories(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return "当前没有账单分类";
  return rows
    .slice(0, 3)
    .map((item) => `${item.type}/${item.sub_type}/${item.currency} x${formatNumber(item.count, 0)}`)
    .join(" | ");
}

function issuesSummary(item) {
  const issueCount =
    (item.portfolio_issues || []).length +
    (item.decision_chain_issues || []).length +
    (item.execution_chain_issues || []).length +
    (item.audit_issues || []).length +
    (item.baseline_switch_issues || []).length;
  return issueCount > 0 ? `${issueCount} 项需要关注的问题` : "暂未发现异常问题";
}

function riskHeadline({ blockers, reconciliation, recovery }) {
  if (blockers.length > 0) return "当前存在明确阻断，系统不应继续自动发单";
  if (reconciliation?.halt_required) return "最新对账要求立即暂停自动交易";
  if (!recovery.safe_to_trade) return "恢复条件尚未满足，当前不应恢复自动交易";
  return "当前没有明确阻断，但仍需持续关注账户、对账和恢复状态";
}

function riskTone({ blockers, reconciliation, recovery, health }) {
  if (health?.halted || blockers.length > 0 || reconciliation?.halt_required) return "danger";
  if (!recovery.safe_to_trade || recovery.review_required) return "warning";
  return "positive";
}

function shouldShowValidateAction({ reconciliation, recovery }) {
  return Boolean(reconciliation?.reconciliation_id || !recovery.safe_to_trade || recovery.review_required);
}

function shouldShowRebaselineAction({ reconciliation, recovery }) {
  return Boolean(
    recovery.rebaseline_available
    || reconciliation?.review_required
    || actionSuggestsRebaseline(reconciliation?.recommended_operator_action)
  );
}

function shouldShowResumeAction({ recovery }) {
  return Boolean(!recovery.safe_to_trade || recovery.resume_eligible);
}

function actionSuggestsRebaseline(value) {
  return String(value || "").toLowerCase().includes("rebaseline");
}

function resumeActionHint({ recovery, uiHints }) {
  if (recovery.resume_eligible) return "";
  return textOrFallback(
    uiHints.recoveryReasonsText,
    listText(recovery.resume_blocked_reasons, "当前恢复条件尚未满足")
  );
}

function textOrFallback(value, fallback = "待确认") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text ? text : fallback;
}

function listText(value, fallback = "当前没有额外说明") {
  if (Array.isArray(value)) {
    const filtered = value.map((item) => String(item ?? "").trim()).filter(Boolean);
    return filtered.length ? filtered.join(" / ") : fallback;
  }
  return textOrFallback(value, fallback);
}
