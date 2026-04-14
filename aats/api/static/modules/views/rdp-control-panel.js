import { actorTags, actionButton, callout, kvList, summaryStrip, surfaceCard } from "../components.js";
import { escapeHtml } from "../formatters.js";

const WORKFLOW_LABELS = {
  data_maintenance: "数据维护",
  research_cycle: "研究管线",
};

const REC_TYPE_LABELS = {
  parameter_upgrade: "参数升级",
  keep_active: "保持当前",
  lower_priority: "降低优先级",
  pause: "暂停",
  require_review: "需要复核",
};

const DECISION_LABELS = {
  keep_active: "保持运行",
  lower_priority: "降低优先级",
  pause: "暂停",
  require_review: "需要复核",
};

const PARAMETER_SOURCE_LABELS = {
  active_parameters: "active 参数",
  governance_pause: "治理暂停后回退默认参数",
  profile_defaults: "默认参数",
  mixed: "混合状态",
};

const PARAMETER_STATUS_LABELS = {
  active: "运行中",
  draft: "草稿",
  candidate: "候选",
  frozen: "冻结",
  deprecated: "废弃",
  unknown: "未知",
};

const RECOMMENDATION_STATUS_LABELS = {
  draft: "待审批",
  approved: "已审批",
  rejected: "已拒绝",
  superseded: "已替代",
};

function relativeTime(isoString) {
  if (!isoString) return "";
  try {
    const diff = Date.now() - new Date(isoString).getTime();
    if (diff < 0) return "刚刚";
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return "刚刚";
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    const days = Math.floor(hours / 24);
    return `${days} 天前`;
  } catch (error) {
    console.warn("[rdp-control-panel] relativeTime parse error:", error);
    return "";
  }
}

function taskStatusSummary(taskInfo) {
  if (!taskInfo) {
    return { value: "未运行", meta: "还没有执行记录", tone: "outline" };
  }
  const status = taskInfo.status || "unknown";
  if (status === "running") {
    const since = taskInfo.started_at ? relativeTime(taskInfo.started_at) : "";
    return { value: "运行中", meta: since ? `开始于 ${since}` : "正在执行", tone: "info" };
  }
  if (status === "done") {
    const ago = taskInfo.finished_at ? relativeTime(taskInfo.finished_at) : "";
    return { value: "已完成", meta: ago ? `完成于 ${ago}` : "上次执行成功", tone: "positive" };
  }
  if (status === "failed") {
    return {
      value: "失败",
      meta: taskInfo.error_message || "任务执行失败",
      tone: "danger",
    };
  }
  if (status === "pending") {
    return { value: "排队中", meta: "等待 daemon 执行", tone: "warning" };
  }
  return { value: status, meta: "", tone: "outline" };
}

function isWorkflowBusy(taskInfo) {
  if (!taskInfo) return false;
  return taskInfo.status === "pending" || taskInfo.status === "running";
}

function readableRecType(type) {
  return REC_TYPE_LABELS[type] || type || "未知";
}

function readableDecision(decision) {
  return DECISION_LABELS[decision] || decision || "未知";
}

function readableParameterStatus(status) {
  return PARAMETER_STATUS_LABELS[status] || status || "未知";
}

function readableConfidence(confidence) {
  const labels = {
    high: "高",
    medium: "中",
    low: "低",
  };
  return labels[confidence] || confidence || "未给出";
}

function confidenceTone(confidence) {
  if (confidence === "high") return "positive";
  if (confidence === "medium") return "info";
  if (confidence === "low") return "warning";
  return "outline";
}

function decisionTone(decision) {
  if (decision === "keep_active") return "positive";
  if (decision === "pause") return "warning";
  if (decision === "require_review") return "info";
  if (decision === "lower_priority") return "outline";
  return "outline";
}

function sourceTone(mode) {
  if (mode === "active_parameters") return "positive";
  if (mode === "governance_pause") return "warning";
  if (mode === "mixed") return "info";
  return "outline";
}

function readableRecommendationStatus(status) {
  return RECOMMENDATION_STATUS_LABELS[status] || status || "未知";
}

function truncateId(id) {
  if (!id) return "—";
  if (id.length <= 20) return id;
  return `${id.slice(0, 8)}…${id.slice(-8)}`;
}

function formatStatusDistribution(statusDistribution = {}) {
  const entries = Object.entries(statusDistribution);
  if (!entries.length) return "暂无";
  return entries.map(([status, count]) => `${status}:${count}`).join(" / ");
}

function summarizeParameterSource(runtimeParameterSource = {}, activeEntries = []) {
  const mode = runtimeParameterSource.mode || (activeEntries.length > 0 ? "active_parameters" : "profile_defaults");
  const activeCount = runtimeParameterSource.active_count ?? activeEntries.length;
  const pausedCount = (runtimeParameterSource.paused_combos || []).length;
  if (mode === "active_parameters") {
    return {
      value: PARAMETER_SOURCE_LABELS[mode],
      meta: activeCount > 0 ? `当前有 ${activeCount} 组 active 参数在实盘生效` : "当前实盘依赖 active 参数，但摘要里没有返回明细",
      tone: sourceTone(mode),
    };
  }
  if (mode === "governance_pause") {
    return {
      value: PARAMETER_SOURCE_LABELS[mode],
      meta: pausedCount > 0 ? `治理层已暂停 ${pausedCount} 个组合，实盘回退默认参数` : "治理层已接管，但当前组合被暂停",
      tone: sourceTone(mode),
    };
  }
  if (mode === "mixed") {
    return {
      value: PARAMETER_SOURCE_LABELS[mode],
      meta: `部分组合使用 active 参数，部分组合使用默认参数或治理暂停；active=${activeCount}，paused=${pausedCount}`,
      tone: sourceTone(mode),
    };
  }
  return {
    value: PARAMETER_SOURCE_LABELS.profile_defaults,
    meta: "当前没有 active 参数，实盘使用档位默认参数",
    tone: sourceTone("profile_defaults"),
  };
}

function renderLatestRoundSection(latestRoundSummary = {}) {
  if (!latestRoundSummary.available) {
    return `<p class="meta-copy" style="margin: 0.75rem 0 0.25rem">当前还没有可展示的研究轮次。</p>`;
  }
  return callout({
    title: `最近一次研究：${latestRoundSummary.round_id || "未知轮次"}`,
    copy: `本轮产出 ${latestRoundSummary.candidate_count || 0} 个候选参数，${latestRoundSummary.conclusion_count || 0} 条策略线结论。`
      + `${latestRoundSummary.readiness_status ? ` readiness=${latestRoundSummary.readiness_status}。` : ""}`
      + `${latestRoundSummary.has_conclusion_report ? " 已生成结论报告。" : " 尚未生成结论报告。"}`,
    pills: [actorTags("ai", "system")],
  });
}

function renderResearchConclusionSection(latestResearchConclusions = []) {
  return `
    <div class="rdp-section">
      <p class="meta-copy" style="margin: 0.75rem 0 0.25rem">
        <strong>最新研究结论</strong>
        <span style="font-size: 0.8rem; color: var(--color-text-muted); margin-left: 0.5rem">
          这里只展示最近一轮 research 对每条策略线的判断。
        </span>
      </p>
      ${latestResearchConclusions.length > 0
        ? `
          <table class="mini-table">
            <thead><tr>
              <th>策略/周期</th><th>研究结论</th><th>置信度</th><th>主要原因</th>
            </tr></thead>
            <tbody>
              ${latestResearchConclusions.map((item) => `
                <tr>
                  <td>${escapeHtml(item.family || "")}/${escapeHtml(item.timeframe || "")}</td>
                  <td><span class="signal-pill tone-${decisionTone(item.decision)}">${escapeHtml(readableDecision(item.decision))}</span></td>
                  <td><span class="signal-pill tone-${confidenceTone(item.confidence)}">${escapeHtml(readableConfidence(item.confidence))}</span></td>
                  <td>${escapeHtml((item.reasons || []).slice(0, 2).join("；") || "—")}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        `
        : `<p class="meta-copy" style="margin: 0.5rem 0 0.25rem">最近一轮研究还没有 family/timeframe 结论。</p>`
      }
    </div>
  `;
}

function renderGovernanceOverview(governanceState = {}, canAdmin = false) {
  const comboStates = governanceState.combo_states || [];
  const header = kvList([
    [
      "参数来源模式",
      PARAMETER_SOURCE_LABELS[governanceState.parameter_source_mode] || governanceState.parameter_source_mode || "默认参数",
      governanceState.governance_managed ? "治理层已接管参数状态解释" : "当前仍以默认参数和 active 参数注册表为主",
    ],
    [
      "决策分布",
      formatStatusDistribution(governanceState.status_distribution || {}),
      (governanceState.paused_combos || []).length > 0
        ? `治理暂停：${governanceState.paused_combos.join(", ")}`
        : "当前没有明确的治理暂停组合",
    ],
  ]);

  if (!comboStates.length) {
    return `
      <div class="rdp-section">
        <p class="meta-copy" style="margin: 0.75rem 0 0.25rem">
          <strong>治理状态总览</strong>
        </p>
        ${header}
        <p class="meta-copy" style="margin: 0.5rem 0 0.25rem">当前还没有可展示的 combo 级治理状态。</p>
      </div>
    `;
  }

  return `
    <div class="rdp-section">
      <p class="meta-copy" style="margin: 0.75rem 0 0.25rem">
        <strong>治理状态总览</strong>
        <span style="font-size: 0.8rem; color: var(--color-text-muted); margin-left: 0.5rem">
          同时解释研究结论、治理决策、候选参数和当前实盘实际使用的参数来源。
        </span>
      </p>
      ${header}
      <table class="mini-table" style="margin-top: 0.75rem">
        <thead><tr>
          <th>策略/周期</th><th>研究</th><th>治理</th><th>候选参数</th><th>当前实盘</th><th>来源</th><th>操作</th>
        </tr></thead>
        <tbody>
          ${comboStates.map((item) => {
            const hasRuntimeActive = Boolean(item.runtime_active_parameter_set_id);
            const runtimeLabel = hasRuntimeActive
              ? `${truncateId(item.runtime_active_parameter_set_id)}${item.runtime_active_parameter_status ? ` (${readableParameterStatus(item.runtime_active_parameter_status)})` : ""}`
              : item.runtime_source === "governance_pause"
                ? "治理暂停"
                : "默认参数";
            const candidateLabel = item.candidate_parameter_set_id
              ? `${truncateId(item.candidate_parameter_set_id)}${item.candidate_parameter_status ? ` (${readableParameterStatus(item.candidate_parameter_status)})` : ""}`
              : "—";
            const actionButtonHtml = hasRuntimeActive
              ? actionButton("回滚", "rdp-rollback-parameters", `${item.family}/${item.timeframe}`, "ghost", {
                  disabled: !canAdmin,
                  title: !canAdmin ? "当前账号只有查看权限" : "回滚到上一版 active 参数",
                })
              : `<span class="meta-copy">—</span>`;
            return `
              <tr>
                <td>${escapeHtml(item.family || "")}/${escapeHtml(item.timeframe || "")}</td>
                <td><span class="signal-pill tone-${decisionTone(item.latest_round_decision)}">${escapeHtml(readableDecision(item.latest_round_decision))}</span></td>
                <td><span class="signal-pill tone-${decisionTone(item.decision_status)}">${escapeHtml(readableDecision(item.decision_status))}</span></td>
                <td class="mono-cell">${escapeHtml(candidateLabel)}</td>
                <td class="mono-cell">${escapeHtml(runtimeLabel)}</td>
                <td><span class="signal-pill tone-${sourceTone(item.runtime_source)}">${escapeHtml(PARAMETER_SOURCE_LABELS[item.runtime_source] || item.runtime_source || "默认参数")}</span></td>
                <td class="table-actions table-actions--compact">${actionButtonHtml}</td>
              </tr>
              ${item.inconsistencies && item.inconsistencies.length > 0
                ? `<tr class="rdp-reason-row"><td colspan="7" class="meta-copy" style="padding: 0.1rem 0.75rem 0.5rem; font-size: 0.8rem; border-top: none; color: var(--color-text-muted)">风险提示：${escapeHtml(item.inconsistencies.join("；"))}</td></tr>`
                : item.pending_operator_action
                  ? `<tr class="rdp-reason-row"><td colspan="7" class="meta-copy" style="padding: 0.1rem 0.75rem 0.5rem; font-size: 0.8rem; border-top: none; color: var(--color-text-muted)">待处理：${escapeHtml(readableRecType(item.latest_recommendation_type))}${item.latest_recommendation_status ? ` / ${readableRecommendationStatus(item.latest_recommendation_status)}` : ""}</td></tr>`
                  : ""
              }
            `;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPendingRecommendations(pendingRecs = [], appliedRecIds = new Set(), canAdmin = false) {
  const parameterRecs = pendingRecs.filter((rec) => rec.recommendation_type === "parameter_upgrade");
  const strategicRecs = pendingRecs.filter((rec) => rec.recommendation_type !== "parameter_upgrade");

  const parameterSection = parameterRecs.length > 0
    ? `
      <div class="rdp-section">
        <p class="meta-copy" style="margin: 0.75rem 0 0.25rem">
          <strong>待处理参数建议</strong>
          <span style="font-size: 0.8rem; color: var(--color-text-muted); margin-left: 0.5rem">
            审批并应用后，新的 active 参数会写入实盘运行参数。
          </span>
        </p>
        <table class="mini-table">
          <thead><tr>
            <th>交易对</th><th>策略/周期</th><th>置信度</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>
            ${parameterRecs.map((rec) => {
              const isApproved = rec.status === "approved";
              const isAlreadyApplied = appliedRecIds.has(rec.recommendation_id);
              const statusLabel = isAlreadyApplied ? "已应用" : isApproved ? "待应用" : "待审批";
              const statusTone = isAlreadyApplied ? "positive" : isApproved ? "info" : "warning";
              return `
                <tr>
                  <td>${escapeHtml(rec.symbol || "")}</td>
                  <td>${escapeHtml(rec.family || "")}/${escapeHtml(rec.timeframe || "")}</td>
                  <td><span class="signal-pill tone-${confidenceTone(rec.confidence)}">${escapeHtml(readableConfidence(rec.confidence))}</span></td>
                  <td><span class="signal-pill tone-${statusTone}">${statusLabel}</span></td>
                  <td class="table-actions table-actions--compact">
                    ${isAlreadyApplied
                      ? `<span class="meta-copy" title="${escapeHtml(rec.recommendation_id)}">已生效</span>`
                      : ""
                    }
                    ${!isAlreadyApplied && isApproved
                      ? actionButton("应用参数", "rdp-apply-only", rec.recommendation_id, "primary", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "把已审批参数写入 active 注册表",
                        })
                      : ""
                    }
                    ${!isApproved
                      ? actionButton("审批并应用", "rdp-approve-and-apply", rec.recommendation_id, "primary", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "审批该建议并立即应用参数",
                        })
                      : ""
                    }
                    ${!isApproved
                      ? actionButton("拒绝", "rdp-reject-recommendation", rec.recommendation_id, "ghost", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "拒绝这条参数建议",
                        })
                      : ""
                    }
                  </td>
                </tr>
                ${rec.reason
                  ? `<tr class="rdp-reason-row"><td colspan="5" class="meta-copy" style="padding: 0.1rem 0.75rem 0.5rem; font-size: 0.8rem; border-top: none; color: var(--color-text-muted)">${escapeHtml(rec.reason)}</td></tr>`
                  : ""
                }
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `
    : "";

  const strategicSection = strategicRecs.length > 0
    ? `
      <div class="rdp-section">
        <p class="meta-copy" style="margin: 0.75rem 0 0.25rem">
          <strong>待确认策略建议</strong>
          <span style="font-size: 0.8rem; color: var(--color-text-muted); margin-left: 0.5rem">
            这类建议不会直接改参数，只更新治理层状态。
          </span>
        </p>
        <table class="mini-table">
          <thead><tr>
            <th>交易对</th><th>策略/周期</th><th>建议类型</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>
            ${strategicRecs.map((rec) => {
              const isApproved = rec.status === "approved";
              return `
                <tr>
                  <td>${escapeHtml(rec.symbol || "")}</td>
                  <td>${escapeHtml(rec.family || "")}/${escapeHtml(rec.timeframe || "")}</td>
                  <td>${escapeHtml(readableRecType(rec.recommendation_type))}</td>
                  <td><span class="signal-pill tone-${isApproved ? "positive" : "warning"}">${isApproved ? "已确认" : "待确认"}</span></td>
                  <td class="table-actions table-actions--compact">
                    ${isApproved
                      ? `<span class="meta-copy" title="${escapeHtml(rec.recommendation_id)}">已确认</span>`
                      : actionButton("确认", "rdp-approve-only", rec.recommendation_id, "secondary", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "确认这条策略建议",
                        })
                    }
                    ${!isApproved
                      ? actionButton("拒绝", "rdp-reject-recommendation", rec.recommendation_id, "ghost", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "拒绝这条策略建议",
                        })
                      : ""
                    }
                  </td>
                </tr>
                ${rec.reason
                  ? `<tr class="rdp-reason-row"><td colspan="5" class="meta-copy" style="padding: 0.1rem 0.75rem 0.5rem; font-size: 0.8rem; border-top: none; color: var(--color-text-muted)">${escapeHtml(rec.reason)}</td></tr>`
                  : ""
                }
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `
    : "";

  if (!parameterSection && !strategicSection) {
    return `<p class="meta-copy" style="margin: 0.75rem 0 0.25rem">当前没有待处理建议。</p>`;
  }
  return `${parameterSection}${strategicSection}`;
}

export function renderRdpControlPanelV2({ rdpControl = {}, canAdmin = false }) {
  const tasks = rdpControl.tasks || {};
  const tasksError = rdpControl.tasks_error || null;
  const pendingRecs = rdpControl.pending_recommendations || [];
  const activeParams = rdpControl.active_parameters || {};
  const runtimeParameterSource = rdpControl.runtime_parameter_source || {};
  const latestRoundSummary = rdpControl.latest_round_summary || {};
  const latestResearchConclusions = rdpControl.latest_research_conclusions || [];
  const governanceState = rdpControl.governance_state || {};

  const activeEntries = Object.entries(activeParams);
  const appliedRecIds = new Set();
  for (const [, info] of activeEntries) {
    if (info.approval_recommendation_id) {
      appliedRecIds.add(info.approval_recommendation_id);
    }
  }

  const dataMaint = tasks.data_maintenance || null;
  const research = tasks.research_cycle || null;
  const dataMaintStatus = taskStatusSummary(dataMaint);
  const researchStatus = taskStatusSummary(research);
  const dataMaintBusy = isWorkflowBusy(dataMaint);
  const researchBusy = isWorkflowBusy(research);
  const sourceSummary = summarizeParameterSource(runtimeParameterSource, activeEntries);

  const workflowButtons = `
    <div class="table-actions table-actions--compact manual-profile-switch-actions manual-profile-switch-actions--centered">
      ${actionButton(
        dataMaintBusy ? "数据维护进行中" : "拉取近期数据",
        "rdp-trigger-workflow",
        "data_maintenance",
        "secondary",
        {
          disabled: !canAdmin || dataMaintBusy,
          title: !canAdmin
            ? "当前账号只有查看权限"
            : dataMaintBusy
              ? "数据维护任务正在执行"
              : "先刷新近期数据，再查看研究结果",
        },
      )}
      ${actionButton(
        researchBusy ? "研究运行中" : "运行完整研究",
        "rdp-trigger-workflow",
        "research_cycle",
        "secondary",
        {
          disabled: !canAdmin || researchBusy,
          title: !canAdmin
            ? "当前账号只有查看权限"
            : researchBusy
              ? "研究任务正在执行"
              : "执行数据刷新和研究全链路",
        },
      )}
    </div>
  `;

  return surfaceCard({
    title: "RDP 研究与应用",
    kicker: "数据维护 / 研究 / 治理",
    copy: "先看研究结论和治理状态，再决定是应用参数还是保持暂停。这里会明确展示当前实盘到底使用 active 参数、治理暂停回退，还是纯默认参数。",
    content: `
      ${tasksError
        ? callout({
            title: "任务状态读取失败",
            copy: "任务队列状态暂时不可用，下面展示的运行状态可能不完整。",
            pills: [actorTags("system")],
          })
        : ""
      }
      ${summaryStrip([
        {
          label: WORKFLOW_LABELS.data_maintenance,
          value: dataMaintStatus.value,
          meta: dataMaintStatus.meta,
          tone: dataMaintStatus.tone,
          badge: actorTags("system"),
        },
        {
          label: WORKFLOW_LABELS.research_cycle,
          value: researchStatus.value,
          meta: researchStatus.meta,
          tone: researchStatus.tone,
          badge: actorTags("ai", "system"),
        },
        {
          label: "当前实盘参数",
          value: sourceSummary.value,
          meta: sourceSummary.meta,
          tone: sourceSummary.tone,
          badge: actorTags("system"),
        },
        {
          label: "待处理建议",
          value: pendingRecs.length > 0 ? `${pendingRecs.length} 条` : "无",
          meta: pendingRecs.length > 0 ? "包含参数建议和治理建议" : "当前没有待处理建议",
          tone: pendingRecs.some((rec) => rec.status === "draft")
            ? "warning"
            : pendingRecs.length > 0
              ? "info"
              : "outline",
          badge: actorTags("operator"),
        },
      ])}
      ${workflowButtons}
      ${renderLatestRoundSection(latestRoundSummary)}
      ${renderResearchConclusionSection(latestResearchConclusions)}
      ${renderGovernanceOverview(governanceState, canAdmin)}
      ${renderPendingRecommendations(pendingRecs, appliedRecIds, canAdmin)}
    `,
  });
}
