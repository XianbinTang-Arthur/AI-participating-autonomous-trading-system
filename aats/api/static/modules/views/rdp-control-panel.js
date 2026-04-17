import {
  actorTags,
  actionButton,
  callout,
  notice,
  primaryStatusPanel,
  summaryStrip,
  surfaceCard,
} from "../components.js";
import { escapeHtml } from "../formatters.js";

const CONFIDENCE_LABELS = {
  high: "高",
  medium: "中",
  low: "低",
};

const HEALTH_LABELS = {
  healthy: "健康",
  degraded: "降级",
  blocked: "阻断",
  not_initialized: "未初始化",
};

const GATE_LABELS = {
  pass: "通过",
  warn: "警告",
  block: "阻断",
};

const APPLY_RESULT_LABELS = {
  pending: "等待执行",
  success: "已生效",
  failed: "执行失败",
  blocked_by_gate: "被 Gate 阻断",
};

const OBSERVATION_STATUS_LABELS = {
  pending: "待观察",
  observing: "观察中",
  completed: "观察完成",
  not_started: "未开始",
  rollback_recommended: "建议回滚",
  rolled_back: "已回滚",
};

function relativeTime(isoString) {
  if (!isoString) return "暂无记录";
  const timestamp = new Date(isoString).getTime();
  if (!Number.isFinite(timestamp)) return "时间格式异常";
  const diff = Date.now() - timestamp;
  if (diff < 60_000) return "刚刚";
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  return `${days} 天前`;
}

function shortId(value) {
  const text = String(value || "").trim();
  if (!text) return "—";
  if (text.length <= 24) return text;
  return `${text.slice(0, 10)}…${text.slice(-8)}`;
}

function toneForHealth(status) {
  if (status === "healthy") return "positive";
  if (status === "degraded") return "warning";
  if (status === "blocked") return "danger";
  return "outline";
}

function toneForGate(status) {
  if (status === "pass") return "positive";
  if (status === "warn") return "warning";
  if (status === "block") return "danger";
  return "outline";
}

function toneForConfidence(confidence) {
  if (confidence === "high") return "positive";
  if (confidence === "medium") return "info";
  if (confidence === "low") return "warning";
  return "outline";
}

function toneForObservationStatus(status) {
  if (status === "rollback_recommended") return "danger";
  if (status === "observing") return "info";
  if (status === "completed") return "positive";
  return "outline";
}

function toneForApplyResult(status) {
  if (status === "success") return "positive";
  if (status === "failed" || status === "blocked_by_gate") return "danger";
  return "outline";
}

function labelForEnvironment(environment) {
  if (environment === "prod") return "生产";
  if (environment === "staging") return "预发";
  if (environment === "dev") return "开发";
  return environment || "待确认";
}

function labelForHealth(status) {
  return HEALTH_LABELS[status] || status || "未知";
}

function labelForGate(status) {
  return GATE_LABELS[status] || status || "未运行";
}

function labelForConfidence(confidence) {
  return CONFIDENCE_LABELS[confidence] || confidence || "未知";
}

function labelForObservationStatus(status) {
  return OBSERVATION_STATUS_LABELS[status] || status || "未知";
}

function labelForApplyResult(status) {
  return APPLY_RESULT_LABELS[status] || status || "未执行";
}

function renderWorkItem({
  tone = "outline",
  kicker = "",
  title = "",
  pills = [],
  body = "",
  meta = [],
  actions = "",
}) {
  return `
    <article class="rdp-workitem tone-${escapeHtml(tone)}">
      <div class="rdp-workitem__header">
        <div>
          ${kicker ? `<p class="panel-kicker">${escapeHtml(kicker)}</p>` : ""}
          <h4>${escapeHtml(title)}</h4>
        </div>
        ${pills.length ? `<div class="inline-pills">${pills.join("")}</div>` : ""}
      </div>
      ${body ? `<div class="rdp-workitem__body">${body}</div>` : ""}
      ${meta.length
        ? `<div class="rdp-inline-meta">${meta.filter(Boolean).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>`
        : ""}
      ${actions ? `<div class="table-actions table-actions--compact rdp-workitem__actions">${actions}</div>` : ""}
    </article>
  `;
}

function buildObservationAction(item, canAdmin, tone = "secondary") {
  const observationValue = `${item.release_id}|${item.observation_window_hours || 24}`;
  return actionButton("运行观察", "rdp-run-observation", observationValue, tone, {
    disabled: !canAdmin,
    title: !canAdmin ? "当前账号只有查看权限" : "按当前观察窗口重新评估这次发布",
  });
}

function buildRollbackAction(item, canAdmin, tone = "warning") {
  return actionButton("执行回滚", "rdp-rollback-parameters", `${item.family}/${item.timeframe}`, tone, {
    disabled: !canAdmin || !item?.is_current_active_release || item?.apply_result !== "success",
    title: !canAdmin
      ? "当前账号只有查看权限"
      : !item?.is_current_active_release
        ? "当前已经不是这次 release 在生效，禁止直接回滚"
        : item?.apply_result !== "success"
          ? "只有已成功生效的 release 才允许回滚"
          : "回滚到上一个版本",
  });
}

function buildObservationCard(item, canAdmin) {
  const observation = item.observation || {};
  const effectiveness = item.effectiveness || {};
  const rollbackFirst = item.observation_status === "rollback_recommended";
  return renderWorkItem({
    tone: toneForObservationStatus(item.observation_status),
    kicker: `${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
    title: `${labelForObservationStatus(item.observation_status)} ${shortId(item.release_id)}`,
    pills: [
      `<span class="signal-pill tone-${escapeHtml(toneForObservationStatus(item.observation_status))}">${escapeHtml(labelForObservationStatus(item.observation_status))}</span>`,
      `<span class="signal-pill tone-${escapeHtml(toneForApplyResult(item.apply_result))}">${escapeHtml(labelForApplyResult(item.apply_result))}</span>`,
    ],
    body: `
      <p class="meta-copy">当前参数集 ${escapeHtml(shortId(item.parameter_set_id))}，上一版 ${escapeHtml(shortId(item.previous_parameter_set_id))}</p>
      <p class="meta-copy">观察结论：${escapeHtml(labelForObservationStatus(observation.status || item.observation_status))}${observation.recommendation ? `，建议 ${escapeHtml(observation.recommendation)}` : ""}</p>
      ${effectiveness.detail ? `<p class="meta-copy">${escapeHtml(effectiveness.detail)}</p>` : ""}
    `,
    meta: [
      item.created_at ? `发布于 ${relativeTime(item.created_at)}` : "",
      observation.evaluated_at ? `最近评估于 ${relativeTime(observation.evaluated_at)}` : "还没有新的观察结果",
    ],
    actions: rollbackFirst
      ? [
        buildRollbackAction(item, canAdmin),
        buildObservationAction(item, canAdmin, "ghost"),
      ].join("")
      : [
        buildObservationAction(item, canAdmin),
        buildRollbackAction(item, canAdmin, "ghost"),
      ].join(""),
  });
}

function renderActionDescriptor(action = {}, canAdmin, tone = "secondary") {
  const enabled = Boolean(action.enabled);
  const title = !canAdmin
    ? "当前账号只有查看权限"
    : (!enabled ? (action.disabled_reason || "当前不可执行") : undefined);
  return actionButton(
    action.label || "执行",
    action.ui_action || "",
    action.value || "",
    tone,
    {
      disabled: !canAdmin || !enabled,
      title,
    },
  );
}

function renderWorkbenchHero({
  overview = {},
  canAdmin = false,
}) {
  const blockers = overview.blockers || [];
  const primaryAction = overview.primary_action
    ? renderActionDescriptor(overview.primary_action, canAdmin, "primary")
    : "";
  const secondaryActions = (overview.secondary_actions || [])
    .map((action) => renderActionDescriptor(action, canAdmin, "ghost"))
    .join("");
  const counts = overview.summary_counts || {};
  const runtime = overview.current_execution || {};
  const nextQueue = overview.next_queue || {};
  const heroActions = `
    <div class="rdp-command-actions">
      ${primaryAction}
      ${secondaryActions}
    </div>
  `;

  return primaryStatusPanel({
    eyebrow: "RDP 工作台",
    title: "先处理当前轮次，再决定发布与回滚",
    headline: overview.headline || "当前没有新的治理动作",
    summary: overview.subheadline || "工作台只展示当前轮次真正需要处理的事项。",
    tone: blockers.length ? "warning" : "neutral",
    actions: heroActions,
    pills: [
      actorTags("operator"),
      counts.integrity_blocked_items
        ? `<span class="signal-pill tone-danger">证据阻断 ${escapeHtml(String(counts.integrity_blocked_items))}</span>`
        : `<span class="signal-pill tone-positive">证据完整</span>`,
      counts.tuning_pending
        ? `<span class="signal-pill tone-info">调优待审核 ${escapeHtml(String(counts.tuning_pending))}</span>`
        : "",
    ].filter(Boolean),
    metrics: [
      {
        label: "当前待审批",
        value: `${counts.pending_items || 0} 条`,
        meta: "按 combo 聚合，只看当前轮次",
        tone: counts.pending_items ? "warning" : "outline",
        badge: actorTags("operator"),
      },
      {
        label: "当前执行中",
        value: runtime.workflow || "无",
        meta: runtime.started_at ? `开始于 ${relativeTime(runtime.started_at)}` : "当前没有运行中的流程",
        tone: runtime.workflow ? "info" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "下一待执行",
        value: nextQueue.workflow || "无",
        meta: nextQueue.requested_at ? `排队于 ${relativeTime(nextQueue.requested_at)}` : "当前没有新的排队任务",
        tone: nextQueue.workflow ? "warning" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "观察中发布",
        value: `${counts.observing_releases || 0} 条`,
        meta: counts.observing_releases ? "仍需继续跟踪观察窗口" : "当前没有观察中的发布",
        tone: counts.observing_releases ? "info" : "outline",
        badge: actorTags("system"),
      },
    ],
  });
}

function renderWorkbenchItemsCard({
  items = [],
  canAdmin = false,
}) {
  const cards = items.map((item) => renderWorkItem({
    tone: item.integrity_status === "blocked" ? "danger" : "warning",
    kicker: `${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
    title: item.headline || "当前组合待处理",
    pills: [
      item.confidence
        ? `<span class="signal-pill tone-${escapeHtml(toneForConfidence(item.confidence))}">置信度 ${escapeHtml(labelForConfidence(item.confidence))}</span>`
        : "",
      `<span class="signal-pill tone-warning">待审批</span>`,
      item.integrity_status === "blocked"
        ? `<span class="signal-pill tone-danger">证据不完整</span>`
        : "",
    ].filter(Boolean),
    body: `
      <p class="meta-copy">${escapeHtml(item.decision_summary || "当前治理结论已生成，请先处理这一组组合。")}</p>
      ${item.approval_enabled === false && item.approval_blocked_reason
        ? callout({
          title: "审批已阻断",
          copy: item.approval_blocked_reason,
          tone: "danger",
          pills: [actorTags("system")],
        })
        : ""}
      ${item.reason_summary?.length
        ? `<ul class="rdp-bullet-list">${item.reason_summary.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
        : ""}
      ${item.missing_evidence?.length
        ? `<p class="meta-copy">缺失项：${escapeHtml(item.missing_evidence.join("；"))}</p>`
        : ""}
      ${renderEvidenceDigest(item)}
    `,
    meta: [
      item.created_at ? `生成于 ${relativeTime(item.created_at)}` : "",
      item.blocking_flags?.length ? `风险提示：${item.blocking_flags[0]}` : "",
    ],
    actions: (item.actions || [])
      .map((action, index) => renderActionDescriptor(action, canAdmin, index === 0 ? "primary" : "ghost"))
      .join(""),
  }));

  return surfaceCard({
    title: "当前待处理",
    kicker: "只看当前轮次",
    copy: "每个 combo 只保留一张主卡片：先结论，再原因，再决定审批还是拒绝。",
    content: cards.length
      ? `<div class="rdp-worklist">${cards.join("")}</div>`
      : notice("当前没有新的治理审批事项。", "info"),
  });
}

function renderIntegrityAlertsCard(alerts = []) {
  const integrity = alerts.integrity_alerts || [];
  const operational = alerts.operational_alerts || [];
  return surfaceCard({
    title: "数据完整性与阻断",
    kicker: "审批前先看这里",
    copy: "不完整证据会直接阻断治理审批，系统阻断和运行异常则影响后续发布链。",
    content: `
      ${summaryStrip([
        {
          label: "完整性告警",
          value: `${integrity.length} 条`,
          meta: integrity.length ? "存在不可直接审批的研究/归因/执行缺口" : "当前没有完整性阻断",
          tone: integrity.length ? "danger" : "positive",
          badge: actorTags("system"),
        },
        {
          label: "系统阻断",
          value: `${operational.length} 条`,
          meta: operational.length ? "存在运行态警告或阻断" : "当前没有额外系统阻断",
          tone: operational.length ? "warning" : "outline",
          badge: actorTags("system"),
        },
      ])}
      ${(integrity.length || operational.length)
        ? `<div class="rdp-worklist">${[...integrity, ...operational].slice(0, 5).map((alert) => renderWorkItem({
            tone: alert.severity === "danger" ? "danger" : "warning",
            kicker: alert.phase ? `${String(alert.phase).toUpperCase()} 告警` : "系统告警",
            title: alert.title || "当前存在阻断",
            body: `<p class="meta-copy">${escapeHtml(alert.message || "当前告警缺少详细说明。")}</p>`,
            meta: [
              alert.blocks_approval ? "该告警会阻断审批" : "",
            ],
          })).join("")}</div>`
        : notice("当前轮次没有新的完整性告警。", "info")}
    `,
  });
}

function renderRuntimeRailCard({
  overview = {},
  rdpControl = {},
}) {
  const health = overview.health || {};
  const environment = rdpControl.environment || {};
  return surfaceCard({
    title: "系统状态",
    kicker: "次级信息",
    copy: "这里保留运行态信息，但不再让 workflow 状态抢占首页主视野。",
    content: summaryStrip([
      {
        label: "当前执行中",
        value: overview.current_execution?.workflow || "无",
        meta: overview.current_execution?.started_at
          ? `开始于 ${relativeTime(overview.current_execution.started_at)}`
          : "当前没有运行中的任务",
        tone: overview.current_execution?.workflow ? "info" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "下一待执行",
        value: overview.next_queue?.workflow || "无",
        meta: overview.next_queue?.requested_at
          ? `排队于 ${relativeTime(overview.next_queue.requested_at)}`
          : "当前没有新的排队任务",
        tone: overview.next_queue?.workflow ? "warning" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "Daemon",
        value: labelForHealth(health.daemon),
        meta: environment.name ? `环境 ${labelForEnvironment(environment.name)}` : "运行环境待确认",
        tone: toneForHealth(health.daemon),
        badge: actorTags("system"),
      },
      {
        label: "最新 Gate",
        value: labelForGate(health.latest_gate),
        meta: "发布前仍需以最新 Gate 为准",
        tone: toneForGate(health.latest_gate),
        badge: actorTags("system"),
      },
    ]),
  });
}

function renderObservationRailCard({
  rdpControl = {},
  canAdmin = false,
}) {
  const observationQueue = rdpControl.observation_queue || [];
  const items = observationQueue.slice(0, 4);
  return surfaceCard({
    title: "观察与回滚",
    kicker: "发布后链路",
    copy: "发布后的观察与回滚独立成一栏，不再与治理审批混成同一种工作项。",
    content: items.length
      ? `<div class="rdp-worklist">${items.map((item) => buildObservationCard(item, canAdmin)).join("")}</div>`
      : notice("当前没有观察中的发布或回滚建议。", "info"),
  });
}

function renderTuningCard({
  tuningOverview = {},
  tuningProposals = {},
  canAdmin = false,
}) {
  const items = tuningProposals.items || [];
  return surfaceCard({
    title: "自动调优",
    kicker: "strategy_tuning_review",
    copy: tuningOverview.headline || "自动调优会生成提案，但只有审核通过后才会影响后续 research 默认值。",
    content: `
      ${summaryStrip([
        {
          label: "待审核提案",
          value: `${tuningOverview.pending_review_count || 0} 条`,
          meta: "需人工确认后才会进入 override",
          tone: tuningOverview.pending_review_count ? "warning" : "outline",
          badge: actorTags("ai"),
        },
        {
          label: "已生效 override",
          value: `${tuningOverview.active_override_count || 0} 组`,
          meta: "已经会影响后续 research / replay 默认值",
          tone: tuningOverview.active_override_count ? "info" : "outline",
          badge: actorTags("system"),
        },
      ])}
      ${items.length
        ? `<div class="rdp-worklist">${items.map((item) => renderWorkItem({
            tone: "info",
            kicker: `${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
            title: item.headline || "待审核调优提案",
            pills: [
              `<span class="signal-pill tone-warning">待审核</span>`,
            ],
            body: `
              <p class="meta-copy">${escapeHtml((item.proposed_changes || []).map((change) => `${change.key}: ${change.from} -> ${change.to}`).join("；"))}</p>
              ${item.reason_summary?.length
                ? `<ul class="rdp-bullet-list">${item.reason_summary.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
                : ""}
            `,
            meta: [
              item.created_at ? `生成于 ${relativeTime(item.created_at)}` : "",
            ],
            actions: (item.actions || [])
              .map((action, index) => renderActionDescriptor(action, canAdmin, index === 0 ? "secondary" : "ghost"))
              .join(""),
          })).join("")}</div>`
        : notice("当前没有待审核的自动调优提案。", "outline")}
    `,
  });
}

export function renderRdpControlPanelV2({
  rdpControl = {},
  rdpWorkbenchOverview = {},
  rdpWorkbenchItems = {},
  rdpWorkbenchAlerts = {},
  rdpTuningOverview = {},
  rdpTuningProposals = {},
  canAdmin = false,
  uiState = {},
}) {
  void uiState;

  return `
    <div class="rdp-ops-shell">
      ${renderWorkbenchHero({
        overview: rdpWorkbenchOverview,
        canAdmin,
      })}
      <div class="panel-grid ai-config-layout">
        <div class="span-8 workspace-stack">
          ${renderWorkbenchItemsCard({
            items: rdpWorkbenchItems.items || [],
            canAdmin,
          })}
          ${renderTuningCard({
            tuningOverview: rdpTuningOverview,
            tuningProposals: rdpTuningProposals,
            canAdmin,
          })}
        </div>
        <div class="span-4 workspace-stack">
          ${renderIntegrityAlertsCard(rdpWorkbenchAlerts)}
          ${renderRuntimeRailCard({
            overview: rdpWorkbenchOverview,
            rdpControl,
          })}
          ${renderObservationRailCard({
            rdpControl,
            canAdmin,
          })}
        </div>
      </div>
    </div>
  `;
}

function renderEvidenceMetricList(metrics = {}) {
  const entries = Object.entries(metrics || {})
    .filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== "");
  if (!entries.length) return "";
  return `
    <ul class="rdp-bullet-list">
      ${entries.map(([key, value]) => `<li>${escapeHtml(String(key))}：${escapeHtml(String(value))}</li>`).join("")}
    </ul>
  `;
}

function renderEvidenceDigest(item = {}) {
  const evidenceDigest = item.evidence_digest || [];
  const detailSummary = item.detail_summary || {};
  const sourceRounds = item.source_rounds || {};
  if (!evidenceDigest.length && !detailSummary.risk_summary?.length && !Object.values(sourceRounds).some(Boolean)) {
    return "";
  }
  return `
    <details class="rdp-inline-detail">
      <summary>查看证据详情</summary>
      <div class="rdp-inline-detail__body">
        ${evidenceDigest.map((entry) => `
          <section class="rdp-inline-detail__section">
            <h5>${escapeHtml(String(entry.headline || entry.phase || "证据摘要"))}</h5>
            <p class="meta-copy">
              阶段：${escapeHtml(String(entry.phase || "未知"))}
              · 状态：${escapeHtml(String(entry.status || "unknown"))}
              ${entry.round_id ? `· 轮次：${escapeHtml(String(entry.round_id))}` : ""}
            </p>
            ${entry.incomplete_reason
              ? `<p class="meta-copy">不完整原因：${escapeHtml(String(entry.incomplete_reason))}</p>`
              : ""}
            ${renderEvidenceMetricList(entry.metrics || {})}
          </section>
        `).join("")}
        ${detailSummary.risk_summary?.length
          ? `
            <section class="rdp-inline-detail__section">
              <h5>风险摘要</h5>
              <ul class="rdp-bullet-list">
                ${detailSummary.risk_summary.map((risk) => `<li>${escapeHtml(String(risk))}</li>`).join("")}
              </ul>
            </section>
          `
          : ""}
        ${Object.values(sourceRounds).some(Boolean)
          ? `
            <section class="rdp-inline-detail__section">
              <h5>来源轮次</h5>
              <ul class="rdp-bullet-list">
                ${Object.entries(sourceRounds)
                  .filter(([, value]) => value)
                  .map(([key, value]) => `<li>${escapeHtml(String(key))}：${escapeHtml(String(value))}</li>`)
                  .join("")}
              </ul>
            </section>
          `
          : ""}
      </div>
    </details>
  `;
}
