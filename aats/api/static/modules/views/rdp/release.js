import { actionButton, surfaceCard } from "../../components.js";
import { escapeHtml } from "../../formatters.js";
import { readableState } from "../../terms.js";
import {
  gateLabel,
  observationLabel,
  renderAction,
  shortId,
  statusPill,
  statusTone,
} from "./formatters.js";

const EFFECTIVENESS_LABELS = {
  conclusion: "结论",
  behavior: "行为指标",
  execution: "执行指标",
  operations: "运维指标",
  governance: "治理结论",
};

const EFFECTIVENESS_VALUE_LABELS = {
  rollback_triggered: "已触发回滚",
  positive: "正向",
  negative: "负向",
  mixed: "混合",
};

const RECOMMENDATION_LABELS = {
  review: "人工复核",
  keep: "继续观察",
  rollback_recommended: "建议回滚",
};

function effectivenessSummary(effectiveness = {}) {
  const detail = String(effectiveness.detail || "").trim();
  if (!detail) return "";
  if (!detail.includes("=")) return escapeHtml(detail);
  const values = Object.fromEntries(detail.split(";").map((part) => {
    const [key, ...rest] = part.trim().split("=");
    return [String(key || "").trim(), rest.join("=").trim()];
  }));
  const parts = Object.entries(EFFECTIVENESS_LABELS)
    .filter(([key]) => values[key])
    .map(([key, label]) => `${label} ${EFFECTIVENESS_VALUE_LABELS[values[key]] || readableState(values[key], "待确认")}`);
  return escapeHtml(parts.length ? `观察评估：${parts.join("；")}` : "观察评估已记录");
}

function renderCandidate(candidate = {}, canAdmin = false) {
  const actions = Array.isArray(candidate.actions) ? candidate.actions : [];
  const auditOnly = Boolean(candidate.audit_only);
  return `
    <article class="rdp-v3-release-item">
      <div class="rdp-v3-release-item__head">
        <div>
          <span class="rdp-v3-eyebrow">${escapeHtml(`${candidate.family || "未知策略"} · ${candidate.timeframe || "未知周期"}`)}</span>
          <strong>${escapeHtml(candidate.headline || "已批准，待发布")}</strong>
        </div>
        ${auditOnly
          ? statusPill("仅审计", "neutral")
          : statusPill(`门禁${gateLabel(candidate.gate_status)}`, statusTone(candidate.gate_status))}
      </div>
      <p>${escapeHtml(candidate.decision_summary || "发布前仍需要重新执行硬门禁。")}</p>
      ${actions.length
        ? `<div class="rdp-v3-actions">${actions.map((action, index) => renderAction(action, canAdmin, index === 0 ? "secondary" : "primary")).join("")}</div>`
        : ""}
    </article>
  `;
}

function renderObservation(item = {}, canAdmin = false, releaseReadOnly = false) {
  const releaseId = item.release_id || "";
  const combo = item.combo_key || `${item.family || ""}/${item.timeframe || ""}`;
  const rollbackValue = item.family && item.timeframe
    ? `${item.family}/${item.timeframe}`
    : (String(combo).includes("/") ? combo : "");
  const recommendation = String(item.observation?.recommendation || "").trim();
  const evaluation = effectivenessSummary(item.effectiveness || {});
  return `
    <article class="rdp-v3-observation-item">
      <div>
        <strong>${escapeHtml(combo || shortId(releaseId))}</strong>
        <span>${escapeHtml(shortId(releaseId))}</span>
        ${recommendation ? `<span>${escapeHtml(`建议 ${RECOMMENDATION_LABELS[recommendation] || readableState(recommendation, "待确认")}`)}</span>` : ""}
        ${evaluation ? `<p>${evaluation}</p>` : ""}
      </div>
      ${statusPill(observationLabel(item.observation_status), statusTone(item.observation_status))}
      <div class="rdp-v3-actions">
        ${actionButton(item.observation_status === "completed" ? "重新运行观察" : "运行观察", "rdp-run-observation", releaseId, "secondary", {
          disabled: !canAdmin || !releaseId || releaseReadOnly,
          title: !canAdmin
            ? "当前会话没有观察写入权限。"
            : releaseReadOnly
              ? "发布历史数据当前为副本，请刷新确认真源后再运行观察。"
              : "",
          dataAttrs: { hours: item.required_window_hours || 24 },
        })}
        ${item.observation_status === "rollback_recommended"
          ? actionButton("发起回滚", "rdp-rollback-parameters", rollbackValue, "warning", {
            disabled: !canAdmin || !rollbackValue || releaseReadOnly,
            title: releaseReadOnly ? "发布历史数据当前为副本，请刷新确认真源后再执行回滚。" : "",
          })
          : ""}
      </div>
    </article>
  `;
}

function renderTuning(tuning = {}, canAdmin = false) {
  const proposals = Array.isArray(tuning.proposals) ? tuning.proposals : [];
  if (!proposals.length) return '<p class="rdp-v3-muted">当前没有待审核调优提案。</p>';
  return `
    <div class="rdp-v3-tuning-list">
      ${proposals.slice(0, 4).map((proposal) => `
        <article>
          <div><strong>${escapeHtml(proposal.title || proposal.headline || proposal.proposal_id || "调优提案")}</strong><span>${escapeHtml(proposal.summary || proposal.reason || proposal.reason_summary?.[0] || "需要人工复核。")}</span></div>
          <div class="rdp-v3-actions">
            ${Array.isArray(proposal.actions) && proposal.actions.length
              ? proposal.actions.map((action, index) => renderAction(action, canAdmin, index === 0 ? "secondary" : "ghost")).join("")
              : `${actionButton("批准调优", "rdp-approve-tuning-proposal", proposal.proposal_id || "", "secondary", { disabled: !canAdmin })}${actionButton("拒绝调优", "rdp-reject-tuning-proposal", proposal.proposal_id || "", "ghost", { disabled: !canAdmin })}`}
          </div>
        </article>
      `).join("")}
    </div>
  `;
}

export function renderRelease(release = {}, tuning = {}, canAdmin = false) {
  const candidates = Array.isArray(release.candidates) ? release.candidates : [];
  const observations = Array.isArray(release.observations) ? release.observations : [];
  const isEligible = release.selection_status === "eligible_for_release_review";
  const releaseReadOnly = Boolean(release.release_history_status?.stale);
  return surfaceCard({
    title: "发布、观察与调优",
    kicker: "最优必须先合格",
    copy: release.selection_explanation || "无合格候选时不会应用任何参数。",
    actions: statusPill(
      isEligible ? "有候选可复核" : "无合格候选",
      isEligible ? "warning" : "neutral",
    ),
    classes: "rdp-v3-card rdp-v3-release-card",
    panelKey: "rdpWorkspace",
    content: `
      <div class="rdp-v3-three-columns">
        <section>
          <div class="rdp-v3-subsection__head"><h4>发布候选</h4><span>${candidates.length} 项</span></div>
          ${candidates.length
            ? `<div class="rdp-v3-release-list">${candidates.map((item) => renderCandidate(item, canAdmin)).join("")}</div>`
            : '<p class="rdp-v3-muted">没有已批准的参数候选。</p>'}
        </section>
        <section>
          <div class="rdp-v3-subsection__head"><h4>观察与回滚</h4><span>${observations.length} 项</span></div>
          ${observations.length
            ? `<div class="rdp-v3-observation-list">${observations.map((item) => renderObservation(item, canAdmin, releaseReadOnly)).join("")}</div>`
            : '<p class="rdp-v3-muted">当前没有发布处于观察窗口。</p>'}
        </section>
        <section>
          <div class="rdp-v3-subsection__head"><h4>研究调优</h4><span>${Array.isArray(tuning.proposals) ? tuning.proposals.length : 0} 项</span></div>
          ${renderTuning(tuning, canAdmin)}
        </section>
      </div>
    `,
  });
}
