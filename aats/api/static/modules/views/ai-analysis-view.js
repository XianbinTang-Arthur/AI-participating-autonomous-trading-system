import { actorTags, callout, kvList, summaryStrip, surfaceCard } from "../components.js";
import { localizeList, summarizeLocalizedList, textOrFallback } from "../copy.js";
import { formatMaybeTimestamp, formatNumber, formatSigned } from "../formatters.js";
import { readableState } from "../terms.js";
import { hasExecutionSuggestionContent, renderAIAnalysisSectionCards, renderAISections } from "./ai-view.js";

function readableProfile(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return readableState(String(value), fallback);
}

const KNOWN_PROFILE_ID_PATTERN = /\b(trend_aggressive|trend_normal|trend_strict|range_defensive|high_volatility_defensive|execution_degraded_safe)\b/giu;

function localizeKnownProfilesInText(value, fallback) {
  const text = textOrFallback(value, fallback);
  return text.replace(KNOWN_PROFILE_ID_PATTERN, (profileId) => readableProfile(profileId, profileId));
}

function activeProfileSummary(activeRevision = {}, activation = {}) {
  const profileName = activeRevision.profile_label || activation.active_profile_id || "";
  return {
    value: readableProfile(profileName, "当前没有生效中的策略档位"),
    meta: profileName
      ? "这是当前真正控制运行参数的策略档位。"
      : "当前还没有已登记的生效档位信息。",
  };
}

function candidateSummary(selection = {}, optimization = {}) {
  const gateCandidate = selection.candidate_profile_id || selection.recommended_profile_id || "";
  const optimizationCandidate = optimization.recommended_profile_id || "";
  const candidate = gateCandidate || optimizationCandidate;
  const blockedReasons = Array.isArray(selection.blocked_reasons) ? selection.blocked_reasons : [];
  const notes = Array.isArray(optimization.notes) ? optimization.notes : [];
  const optimizerMeta = optimizationCandidate && optimizationCandidate !== candidate
    ? `优化器建议 ${readableProfile(optimizationCandidate)}，但最终门控候选为 ${readableProfile(candidate)}。`
    : "";

  if (!candidate) {
    return {
      value: "当前没有新的候选策略档位",
      meta: "系统这轮没有给出新的候选档位。",
    };
  }

  if (blockedReasons.length) {
    return {
      value: readableProfile(candidate),
      meta: `${optimizerMeta}系统已经选出门控候选档位，但暂时不能自动切过去：${summarizeLocalizedList(blockedReasons, {
        fallback: "当前没有额外阻断说明",
        limit: 2,
      })}`,
    };
  }

  if (notes.length) {
    return {
      value: readableProfile(candidate),
      meta: `${optimizerMeta}${summarizeLocalizedList(notes, {
        fallback: "当前没有额外候选说明",
        limit: 2,
      })}`,
    };
  }

  if (optimization.score_delta_vs_active !== null && optimization.score_delta_vs_active !== undefined) {
    return {
      value: readableProfile(candidate),
      meta: `${optimizerMeta}相对当前档位的综合分差为 ${formatSigned(optimization.score_delta_vs_active, 2)}。`,
    };
  }

  return {
    value: readableProfile(candidate),
    meta: optimizerMeta || "当前没有额外候选说明。",
  };
}

function controlStateSummary(state = {}, fallback = "当前没有额外说明。") {
  const multiplier = state.multiplier;
  const reasons = summarizeLocalizedList(state.reasons, {
    fallback,
    limit: 2,
  });
  if (multiplier === null || multiplier === undefined) return reasons;
  return `当前乘数 ${formatNumber(multiplier, 2, "待确认")}，${reasons}`;
}

function gatingSummary(selection = {}) {
  const gating = selection.gating_state || {};
  const segments = [];
  if (gating.reconciliation_clean === false) {
    segments.push("对账未清洁");
  }
  if (gating.confidence_floor !== null && gating.confidence_floor !== undefined) {
    segments.push(`自动切档最低置信度 ${formatNumber(gating.confidence_floor, 2, "待确认")}`);
  }
  if (gating.next_eligible_switch_at) {
    segments.push(`下一次最早可切换时间 ${formatMaybeTimestamp(gating.next_eligible_switch_at)}`);
  }
  if ((gating.remaining_closed_trades || 0) > 0 || (gating.remaining_replay_validations || 0) > 0) {
    segments.push(
      `还差 ${formatNumber(gating.remaining_closed_trades, 0, "0")} 笔已平仓交易、${formatNumber(gating.remaining_replay_validations, 0, "0")} 次 replay`,
    );
  }
  if ((gating.remaining_consecutive_wins || 0) > 0) {
    segments.push(`同一候选还差 ${formatNumber(gating.remaining_consecutive_wins, 0, "0")} 次连续胜出`);
  }
  return segments.join("；") || "当前没有额外闸门说明。";
}

function fastTrackSummary(selection = {}) {
  if (selection.fast_track_applied) {
    return {
      value: "已启用",
      meta: summarizeLocalizedList(selection.gating_state?.fast_track_reasons, {
        fallback: "系统已经走紧急安全快速通道。",
        limit: 3,
      }),
      tone: "danger",
    };
  }
  if (selection.fast_track_eligible) {
    return {
      value: "条件已满足",
      meta: "当前具备紧急安全快速通道条件，但仍有其它硬阻断没有解除。",
      tone: "warning",
    };
  }
  return {
    value: "未应用",
    meta: summarizeLocalizedList(selection.gating_state?.fast_track_reasons, {
      fallback: "当前没有应用快速通道。",
      limit: 3,
    }),
    tone: "outline",
  };
}

function blockedReasonSummary(selection = {}) {
  const reasons = Array.isArray(selection.blocked_reasons) ? selection.blocked_reasons : [];
  if (!reasons.length) return "当前没有新的自动切档阻断原因。";
  const summary = summarizeLocalizedList(reasons, {
    fallback: "当前没有新的自动切档阻断原因。",
    limit: 4,
  });
  return reasons.length > 4 ? `${summary}（共 ${formatNumber(reasons.length, 0)} 条）` : summary;
}

function profileEvidenceCallout(controlSummary = {}, evidence = {}, latestCandidate = {}) {
  const selection = controlSummary.latest_selection_decision || {};
  if (selection.fast_track_applied) {
    return callout({
      title: "当前通过紧急安全快速通道切档",
      copy: localizeKnownProfilesInText(
        selection.operator_summary,
        "系统已经跳过慢速样本门槛，允许直接切向更保守的安全档位。",
      ),
      pills: [actorTags("system", "risk_control")],
    });
  }
  const title = controlSummary.safety_profile_required
    ? "当前以安全保护优先"
    : evidence.cold_start_active
      ? "当前仍处于冷启动观察期"
      : "当前继续比较盈利档候选";

  const copy = controlSummary.safety_profile_required
    ? "只有明确触发安全事件时，系统才允许自动切到安全档。这里重点用来解释这次保护是否真的有足够依据。"
    : evidence.cold_start_active
      ? "样本不足时，系统会先维持当前主档位，不会因为证据不够就默认切到最保守档。AI 仍会继续给出比较依据。"
      : `当前最新候选策略档位是 ${latestCandidate.value}。这里重点解释为什么系统自动切了、没切，或只是给出了候选但继续保持当前档位。`;

  return callout({
    title,
    copy,
    pills: [actorTags("system", "ai")],
  });
}

function profileEvidenceCard(data) {
  const profileControl = data.profileControlSummary || {};
  const controlSummary = profileControl.control_summary || {};
  const evidence = controlSummary.evidence || {};
  const activation = profileControl.activation || {};
  const activeRevision = profileControl.active_revision || {};
  const selection = profileControl.latest_selection_decision || {};
  const optimization = profileControl.latest_optimization_report || {};
  const adaptiveControls = controlSummary.adaptive_controls || {};
  const riskBudget = adaptiveControls.risk_budget || {};
  const executionAggressiveness = adaptiveControls.execution_aggressiveness || {};
  const currentProfile = activeProfileSummary(activeRevision, activation);
  const latestCandidate = candidateSummary(selection, optimization);
  const fastTrack = fastTrackSummary(selection);

  return surfaceCard({
    title: "档位控制证据",
    kicker: "为什么切档或不切档",
    panelKey: "profileControlSummary",
    copy: "这里集中解释当前档位、冷启动锁、候选档位和阻断原因，避免把样本不足误读成策略应该更保守。",
    content: `
      ${profileEvidenceCallout(
        {
          ...controlSummary,
          latest_selection_decision: selection,
        },
        evidence,
        latestCandidate,
      )}
      ${summaryStrip([
        {
          label: "当前策略档位",
          value: currentProfile.value,
          meta: currentProfile.meta,
          tone: "info",
          badge: actorTags("system", "ai"),
        },
        {
          label: "冷启动观察期",
          value: evidence.cold_start_active ? "仍在观察期" : "已解除",
          meta: `${formatNumber(evidence.closed_trades, 0, "0")} / ${formatNumber(evidence.min_closed_trades, 0, "0")} 笔已闭合交易，${formatNumber(evidence.replay_validations, 0, "0")} / ${formatNumber(evidence.min_replay_validations, 0, "0")} 次 replay`,
          tone: evidence.cold_start_active ? "warning" : "positive",
          badge: actorTags("system"),
        },
        {
          label: "安全档触发",
          value: controlSummary.safety_profile_required ? "允许切入安全档" : "当前不允许自动切入安全档",
          meta: controlSummary.safety_profile_required
            ? "当前存在明确安全事件，所以系统允许自动切入安全档。"
            : "没有明确安全事件时，系统应继续在盈利档之间比较。",
          tone: controlSummary.safety_profile_required ? "danger" : "positive",
          badge: actorTags("system"),
        },
        {
          label: "候选策略档位",
          value: latestCandidate.value,
          meta: latestCandidate.meta,
          tone: "info",
          badge: actorTags("ai", "system"),
        },
        {
          label: "切换分类",
          value: readableState(selection.transition_class || "unknown"),
          meta: localizeKnownProfilesInText(selection.operator_summary, "当前没有额外切换摘要。"),
          tone: "info",
          badge: actorTags("system"),
        },
        {
          label: "快速通道",
          value: fastTrack.value,
          meta: fastTrack.meta,
          tone: fastTrack.tone,
          badge: actorTags("risk_control", "system"),
        },
        {
          label: "风险预算乘数",
          value: formatNumber(riskBudget.multiplier, 2, "待确认"),
          meta: controlStateSummary(riskBudget),
          tone: Number(riskBudget.multiplier || 1) < 1 ? "warning" : "positive",
          badge: actorTags("risk_control"),
        },
        {
          label: "执行侵略性乘数",
          value: formatNumber(executionAggressiveness.multiplier, 2, "待确认"),
          meta: controlStateSummary(executionAggressiveness),
          tone: Number(executionAggressiveness.multiplier || 1) < 1 ? "warning" : "positive",
          badge: actorTags("risk_control"),
        },
      ])}
      ${kvList([
        [
          "当前判定原则",
          controlSummary.safety_profile_required ? "当前以安全保护优先" : "当前以维持盈利档并继续积累证据为主",
          summarizeLocalizedList(optimization.notes, { fallback: "当前没有额外判定说明。", limit: 3 }),
        ],
        [
          "自动切档阻断原因",
          blockedReasonSummary(selection),
          "这里优先解释为什么系统保持当前档位，或者为什么只是给出候选但没有真正切档。",
        ],
        [
          "自动切档闸门",
          gatingSummary(selection),
          `当前对账状态：${selection.gating_state?.reconciliation_clean ? "已清洁" : "仍需复核"}`,
        ],
        [
          "快速通道依据",
          localizeList(selection.gating_state?.fast_track_reasons, "当前没有触发紧急安全快速通道。"),
          localizeList(selection.gating_state?.fast_track_bypass_gates, "当前没有被快速通道绕过的闸门。"),
        ],
        [
          "风险预算自适应",
          `${formatNumber(riskBudget.multiplier, 2, "待确认")}（${readableState(riskBudget.status || "unknown")}）`,
          controlStateSummary(riskBudget),
        ],
        [
          "执行侵略性自适应",
          `${formatNumber(executionAggressiveness.multiplier, 2, "待确认")}（${readableState(executionAggressiveness.status || "unknown")}）`,
          controlStateSummary(executionAggressiveness),
        ],
        [
          "最近一次切档时间",
          formatMaybeTimestamp(activation.last_activation_at),
          readableState(activation.last_activation_result || "none"),
        ],
        [
          "候选档位摘要",
          latestCandidate.value,
          latestCandidate.meta,
        ],
      ])}
    `,
  });
}

export function renderAIAnalysisView(data) {
  const workspace = renderAISections(data);
  const analysis = renderAIAnalysisSectionCards(data);
  const showExecutionSuggestion = hasExecutionSuggestionContent(
    data.aiOverview?.latest_execution_suggestion || data.aiLatest?.execution_suggestion || {},
  );

  return `
    <div class="workspace-stack ai-analysis-workspace">
      <div class="layout-flow layout-flow--5-7 ai-analysis-flow">
        <div class="layout-flow__column">
          <div class="ai-analysis-flow__hero">${workspace.aiHero}</div>
          <div class="ai-analysis-flow__profile">${profileEvidenceCard(data)}</div>
        </div>
        <div class="layout-flow__column">
          <div class="ai-analysis-flow__latest">${workspace.aiLatest}</div>
          <div class="ai-analysis-flow__performance">${analysis.aiPerformanceReports}</div>
        </div>
      </div>
      ${workspace.aiReview ? `<div>${workspace.aiReview}</div>` : ""}
      ${showExecutionSuggestion ? `<div>${analysis.aiExecutionSuggestion}</div>` : ""}
      <div>${analysis.aiHistory}</div>
    </div>
  `;
}
