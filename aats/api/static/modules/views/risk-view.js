import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table, timeline } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { localizeError, readableState, toneForRuntimeState } from "../terms.js";

export function renderRiskSections(data) {
  const account = data.accountState || {};
  const portfolio = data.portfolio?.portfolio || {};
  const blockers = data.blockers?.blockers || [];
  const blockerHistoryPayload = data.blockerHistory || {};
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const reconciliationRecentPayload = data.reconciliationRecent || {};
  const mismatchSummary = data.reconciliationLatest?.mismatch_summary || {};
  const recovery = data.systemRecovery?.recovery || {};
  const replay = data.replayStatus || {};
  const replayRecentPayload = data.replayRecentValidations || {};
  const metrics = data.metrics || {};
  const uiHints = data.uiHints || {};

  return {
    riskHero: surfaceCard({
      title: "交易安全总览",
      kicker: "系统现在能不能继续自动交易",
      copy: "这一页只回答风险相关的问题：现在为什么能交易、为什么不能交易、最近的阻断点和对账结论有没有恶化。",
      classes: "hero-card",
      actions: reconciliation?.reconciliation_id ? actionButton("查看最新对账详情", "inspect-reconciliation", reconciliation.reconciliation_id) : "",
      content: `
        ${callout({
          title: riskHeadline({ blockers, reconciliation, recovery }),
          copy: riskNarrative({ blockers, reconciliation, recovery }),
          pills: [
            pill(`运行状态：${readableState(data.health?.runtime_state || data.health?.overall_status)}`, toneForRuntimeState(data.health?.runtime_state || data.health?.overall_status)),
            pill(`允许继续交易：${booleanWord(recovery.safe_to_trade)}`, recovery.safe_to_trade ? "positive" : "danger"),
            pill(`等待人工确认：${booleanWord(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
          ],
        })}
        ${statGrid([
          { label: "当前 blocker 数", value: formatNumber(blockers.length), meta: blockers[0] ? localizeError(blockers[0].blocker) : "当前没有 blocker" },
          { label: "对账状态", value: readableState(reconciliation?.severity || "unknown"), meta: reconciliation?.reconciliation_id || "-" },
          { label: "恢复状态", value: readableState(recovery.recovery_state), meta: uiHints.recoveryReasonsText || listOrDash(recovery.resume_blocked_reasons) },
          { label: "账户快照新鲜度", value: booleanWord(account.fresh), meta: formatMaybeTimestamp(account.last_refresh_timestamp) },
        ])}
      `,
    }),
    riskAccount: surfaceCard({
      title: "账户与权益",
      kicker: "资金安全",
      copy: "这里看账户净值、收益、保证金占用和账户快照是否可信。",
      content: kvList([
        ["总权益", formatNumber(portfolio.total_equity), "账户当前总价值"],
        ["已实现收益", formatSigned(portfolio.realized_pnl), "已确认收益"],
        ["未实现收益", formatSigned(portfolio.unrealized_pnl), "浮动盈亏"],
        ["总敞口", formatNumber(portfolio.gross_exposure), `净敞口 ${formatSigned(portfolio.net_exposure)}`],
        ["保证金占用", formatNumber(portfolio.margin_usage), "来自当前账户快照"],
        ["账户快照可用", booleanWord(account.ready), listOrDash(account.blockers)],
      ]),
    }),
    riskReconciliation: surfaceCard({
      title: "对账状态",
      kicker: "本地记录 vs 交易所",
      copy: "当本地记录和交易所状态不一致时，应该先在这里发现，而不是等到异常扩散到交易执行。",
      content: kvList([
        ["对账级别", readableState(reconciliation?.severity || "-"), reconciliation?.reconciliation_id || "-"],
        ["是否要求停机", booleanWord(reconciliation?.halt_required), reconciliation?.exchange_comparison_enabled ? "已比对交易所" : "仅校验本地记录"],
        ["差异原因", listOrDash(mismatchSummary.mismatch_reasons), listOrDash(mismatchSummary.mismatch_categories)],
        ["建议动作", localizeError(mismatchSummary.recommended_operator_action || "-"), listOrDash(mismatchSummary.safety_impacts)],
      ]),
    }),
    riskRecovery: surfaceCard({
      title: "恢复与回放",
      kicker: "可恢复性",
      copy: "恢复和回放决定了系统在异常之后还能不能继续被信任。",
      content: kvList([
        ["恢复状态", readableState(recovery.recovery_state), recovery.safe_to_trade ? "当前允许继续自动交易" : "当前不允许继续自动交易"],
        ["能否恢复运行", booleanWord(recovery.resume_eligible), uiHints.recoveryReasonsText || listOrDash(recovery.resume_blocked_reasons)],
        ["是否需要人工确认", booleanWord(recovery.review_required), recovery.rebaseline_available ? "允许重建基线" : "当前不允许重建基线"],
        ["回放健康度", booleanWord(replay.healthy), replay.last_validation?.decision_id || "最近没有回放验证"],
      ]),
    }),
    riskBlockers: surfaceCard({
      title: "当前 blocker 明细",
      kicker: "阻断列表",
      copy: blockers.length ? "下面这些 blocker 正在直接影响交易资格。" : "当前没有 blocker，说明系统没有被明确阻断。",
      content: timeline(
        blockers.map((item) => ({
          title: localizeError(item.blocker),
          subtitle: item.subsystem ? `来源：${readableState(item.subsystem)}` : "系统阻断",
          detail: localizeError(item.recommended_action || item.blocker),
          pill: pill(item.submit_only ? "仅阻断发单" : "阻断执行", item.affects_execution ? "danger" : "warning"),
        })),
        "当前没有 blocker。"
      ),
    }),
    riskMetrics: surfaceCard({
      title: "风险观察指标",
      kicker: "辅助判断",
      copy: "这些指标用来帮助判断系统是不是开始偏离可信状态。",
      content: statGrid([
        { label: "拒单数", value: formatNumber(metrics.rejection_count), meta: "越高越要检查门禁和执行条件" },
        { label: "当前活动委托数", value: formatNumber(metrics.current_open_order_count), meta: "要和 blocker、本地 reservation 一起看" },
        { label: "对账异常数", value: formatNumber(metrics.reconciliation_mismatch_count), meta: "持续非零说明状态还没完全收敛" },
        { label: "最近回放时间", value: formatMaybeTimestamp(replay.last_validation?.validated_at), meta: formatRelativeAge(replay.last_validation?.validated_at) },
      ]),
    }),
    riskReconciliationHistory: surfaceCard({
      title: "最近对账报告",
      kicker: "历史核对记录",
      copy: "这里保留最近的对账历史，方便判断异常是一次性尖峰，还是持续反复出现。可继续加载更多。",
      content: `${renderReconciliationHistory(reconciliationRecentPayload)}${renderPaginationFooter({
        payload: reconciliationRecentPayload,
        key: "reconciliations",
        singular: "对账报告",
        loadAction: "load-more-reconciliations",
        collapseAction: "collapse-reconciliations",
      })}`,
    }),
    riskBlockerHistory: surfaceCard({
      title: "最近 blocker 快照",
      kicker: "阻断轨迹",
      copy: "这里展示最近几次 blocker 快照，用来看阻断是一次性事件还是持续性的运行问题。",
      content: `${renderBlockerHistory(blockerHistoryPayload)}${renderPaginationFooter({
        payload: blockerHistoryPayload,
        key: "history",
        singular: "blocker 快照",
        loadAction: "load-more-blocker-history",
        collapseAction: "collapse-blocker-history",
      })}`,
    }),
    riskReplayHistory: surfaceCard({
      title: "最近回放验证",
      kicker: "重建一致性",
      copy: "回放历史用来判断事件链和组合重建是否还保持一致。若 divergence 持续增多，说明恢复可信度在下降。",
      content: `${renderReplayValidationHistory(replayRecentPayload)}${renderPaginationFooter({
        payload: replayRecentPayload,
        key: "validations",
        singular: "回放验证",
        loadAction: "load-more-replay-validations",
        collapseAction: "collapse-replay-validations",
      })}`,
    }),
  };
}

export function renderRiskView(data) {
  const sections = renderRiskSections(data);
  return `
    <div class="panel-grid">
      <div class="span-12">${sections.riskHero}</div>
      <div class="span-4">${sections.riskAccount}</div>
      <div class="span-4">${sections.riskReconciliation}</div>
      <div class="span-4">${sections.riskRecovery}</div>
      <div class="span-6">${sections.riskBlockers}</div>
      <div class="span-6">${sections.riskMetrics}</div>
      <div class="span-6">${sections.riskReconciliationHistory}</div>
      <div class="span-6">${sections.riskBlockerHistory}</div>
      <div class="span-12">${sections.riskReplayHistory}</div>
    </div>
  `;
}

function renderReconciliationHistory(payload) {
  const reconciliations = payload?.reconciliations || [];
  return table(
    ["时间", "级别", "差异摘要", "停机", "详情"],
    reconciliations.map((item) => [
      `<div><strong>${formatRelativeAge(item.as_of_ts)}</strong><div class="table-meta">${formatMaybeTimestamp(item.as_of_ts)}</div></div>`,
      `<div><strong>${readableState(item.severity || "-")}</strong><div class="table-meta">${item.reconciliation_id || "-"}</div></div>`,
      `<div><strong>${listOrDash(item.mismatch_reasons)}</strong><div class="table-meta">${listOrDash(item.mismatch_categories)}</div></div>`,
      `<div><strong>${booleanWord(item.halt_required)}</strong><div class="table-meta">${item.exchange_comparison_enabled ? "已比对交易所" : "仅本地"}</div></div>`,
      item.reconciliation_id ? actionButton("详情", "inspect-reconciliation", item.reconciliation_id) : "",
    ]),
    "最近还没有对账报告。"
  );
}

function renderBlockerHistory(payload) {
  const history = payload?.history || [];
  return table(
    ["时间", "运行状态", "阻断摘要", "执行状态"],
    history.map((item) => [
      `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
      `<div><strong>${readableState(item.runtime_state || "-")}</strong><div class="table-meta">${readableState(item.operating_state || "-")}</div></div>`,
      `<div><strong>${formatNumber((item.blockers || []).length)}</strong><div class="table-meta">${(item.blockers || []).length ? localizeError(item.blockers[0].blocker) : "无 blocker"}</div></div>`,
      `<div><strong>${booleanWord(item.execution_blocked)}</strong><div class="table-meta">${item.halted ? "已暂停" : item.submit_blocked ? "仅阻断发单" : "允许执行"}</div></div>`,
    ]),
    "最近还没有 blocker 快照。"
  );
}

function renderReplayValidationHistory(payload) {
  const validations = payload?.validations || [];
  return table(
    ["时间", "健康度", "决策", "差异数", "问题摘要"],
    validations.map((item) => [
      `<div><strong>${formatRelativeAge(item.validated_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.validated_at)}</div></div>`,
      `<div><strong>${booleanWord(item.healthy)}</strong><div class="table-meta">${item.validation_id || "-"}</div></div>`,
      `<div><strong>${item.decision_id || "-"}</strong><div class="table-meta">${formatNumber(item.replayed_event_count)} 个事件</div></div>`,
      `<div><strong>${formatNumber(item.divergence_count)}</strong><div class="table-meta">baseline 切换 ${formatNumber(item.baseline_switch_count)}</div></div>`,
      `<div><strong>${issuesSummary(item)}</strong><div class="table-meta">${listOrDash(item.execution_chain_issues)}</div></div>`,
    ]),
    "最近还没有回放验证记录。"
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
      <p class="meta-copy">当前展示 ${shown} / ${total} 条${singular}。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${singular}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}

function issuesSummary(item) {
  const issueCount =
    (item.portfolio_issues || []).length +
    (item.decision_chain_issues || []).length +
    (item.execution_chain_issues || []).length +
    (item.audit_issues || []).length +
    (item.baseline_switch_issues || []).length;
  return issueCount > 0 ? `${issueCount} 项问题` : "未发现问题";
}

function riskHeadline({ blockers, reconciliation, recovery }) {
  if (blockers.length > 0) return "当前存在明确阻断，系统不应继续自动发单。";
  if (reconciliation?.halt_required) return "最新对账要求立即暂停自动交易。";
  if (!recovery.safe_to_trade) return "当前恢复状态仍不允许继续自动交易。";
  return "系统当前没有明显阻断，但仍应持续观察账户与对账状态。";
}

function riskNarrative({ blockers, reconciliation, recovery }) {
  if (blockers.length > 0) {
    return `当前最直接的风险来自 ${localizeError(blockers[0].blocker)}，需要先处理这个阻断原因，再判断是否继续交易。`;
  }
  if (reconciliation?.halt_required) {
    return `最新对账结果为 ${readableState(reconciliation.severity)}，并且已经要求暂停交易，说明系统对本地状态与交易所状态的一致性还没有恢复信心。`;
  }
  if (!recovery.safe_to_trade) {
    return `当前恢复状态为 ${readableState(recovery.recovery_state)}，系统仍不满足继续自动交易的条件：${listOrDash(recovery.resume_blocked_reasons)}。`;
  }
  return "当前没有明确 blocker，对账与恢复状态也没有给出强制暂停信号，但仍要继续关注账户快照新鲜度和后续异常。";
}
