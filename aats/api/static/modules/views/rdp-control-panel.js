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

const WORKFLOW_LABELS = {
  data_maintenance: "刷新数据",
  research_cycle: "运行完整 RDP",
  governance_cycle: "治理检查",
  decision_cycle: "决策链",
  release_cycle: "发布与观察",
};

const EVIDENCE_PHASE_LABELS = {
  phase2: "Step2 研究",
  phase3: "Phase3 归因",
  phase4: "Phase4 执行",
  readiness: "就绪度",
};

const EVIDENCE_STATUS_LABELS = {
  available: "可用",
  blocked: "阻断",
  incomplete: "不完整",
  missing: "缺失",
};

const EVIDENCE_METRIC_LABELS = {
  experiments_with_openings: "有开仓信号的实验数",
  max_opening_count: "最大开仓次数",
  mean_positive_edge_ratio: "平均正向收益占比",
  status: "状态",
  failure_ratio: "失败占比",
  failure_count: "失败数",
  total_count: "总样本数",
  full_fill_ratio: "完整成交率",
  cost_adjusted_edge_proxy_bps: "成本后边际（bps）",
  mean_cost_bps: "平均成本（bps）",
  decision_status: "当前决策状态",
  runtime_source: "当前实盘参数来源",
};

const SOURCE_ROUND_LABELS = {
  phase2_round_id: "Step2 轮次",
  phase3_round_id: "Phase3 轮次",
  phase4_round_id: "Phase4 轮次",
  decision_round_id: "决策轮次",
};

const DECISION_STATUS_LABELS = {
  keep_active: "保持当前",
  lower_priority: "降低优先级",
  pause: "暂停",
  require_review: "需复核",
  parameter_upgrade: "参数升级",
};

const RUNTIME_SOURCE_LABELS = {
  active_parameters: "已生效参数",
  governance_pause: "治理暂停状态",
  governance_managed: "治理参数",
  unknown: "未知",
};

const INCOMPLETE_REASON_LABELS = {
  manifest_missing_on_disk: "缺少 round_manifest",
  file_incomplete: "快照不完整",
  insufficient_data: "数据不足",
  query_failed: "查询失败",
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

function labelForWorkflow(workflow) {
  return WORKFLOW_LABELS[workflow] || workflow || "暂无";
}

function labelForEvidencePhase(phase) {
  return EVIDENCE_PHASE_LABELS[phase] || phase || "未知阶段";
}

function labelForEvidenceStatus(status) {
  return EVIDENCE_STATUS_LABELS[status] || status || "未知";
}

function labelForEvidenceMetric(key) {
  return EVIDENCE_METRIC_LABELS[key] || key || "指标";
}

function labelForSourceRound(key) {
  return SOURCE_ROUND_LABELS[key] || key || "来源轮次";
}

function labelForIncompleteReason(reason) {
  return INCOMPLETE_REASON_LABELS[reason] || reason || "未知";
}

function formatEvidenceMetricValue(key, value) {
  if (key === "decision_status") return DECISION_STATUS_LABELS[value] || value;
  if (key === "runtime_source") return RUNTIME_SOURCE_LABELS[value] || value;
  return value;
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
  return actionButton("运行观察", "rdp-run-observation", item.release_id, tone, {
    disabled: !canAdmin,
    title: !canAdmin ? "当前账号只有查看权限" : "按当前观察窗口重新评估这次发布",
    dataAttrs: { hours: item.observation_window_hours || 24 },
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
  const allActions = [
    overview.primary_action,
    ...(overview.secondary_actions || []),
  ].filter(Boolean);
  const primaryAction = overview.primary_action
    ? renderActionDescriptor(overview.primary_action, canAdmin, "primary")
    : "";
  const secondaryActions = (overview.secondary_actions || [])
    .map((action) => renderActionDescriptor(action, canAdmin, "ghost"))
    .join("");
  const disabledActionNotes = canAdmin
    ? allActions
      .filter((action) => action && action.enabled === false && action.disabled_reason)
      .map((action) => `<span>${escapeHtml(`${action.label}：${action.disabled_reason}`)}</span>`)
      .join("")
    : "";
  const counts = overview.summary_counts || {};
  const runtime = overview.current_execution || {};
  const nextQueue = overview.next_queue || {};
  const heroActions = `
    <div class="rdp-command-actions">
      ${primaryAction}
      ${secondaryActions}
    </div>
    ${disabledActionNotes ? `<div class="rdp-inline-meta">${disabledActionNotes}</div>` : ""}
  `;

  return primaryStatusPanel({
    eyebrow: "RDP 工作台",
    title: "当前轮次",
    headline: overview.headline || "当前没有新的治理动作",
    summary: overview.subheadline || "先处理这轮结论，再决定是否发布或回滚。",
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
        label: "待处理",
        value: `${counts.pending_items || 0} 条`,
        meta: "只算当前轮次",
        tone: counts.pending_items ? "warning" : "outline",
        badge: actorTags("operator"),
      },
      {
        label: "当前运行",
        value: labelForWorkflow(runtime.workflow),
        meta: runtime.started_at ? `开始于 ${relativeTime(runtime.started_at)}` : "当前没有运行中的流程",
        tone: runtime.workflow ? "info" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "下一项",
        value: labelForWorkflow(nextQueue.workflow),
        meta: nextQueue.requested_at ? `排队于 ${relativeTime(nextQueue.requested_at)}` : "当前没有新的排队任务",
        tone: nextQueue.workflow ? "warning" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "观察中发布",
        value: `${counts.observing_releases || 0} 条`,
        meta: counts.observing_releases ? "这些发布还在观察窗口内" : "当前没有观察中的发布",
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
      <p class="meta-copy">${escapeHtml(item.decision_summary || "先看这轮结论，再决定是否批准。")}</p>
      ${item.approval_effect_summary
        ? `<p class="meta-copy">如果批准：${escapeHtml(item.approval_effect_summary)}</p>`
        : ""}
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
        ? `<p class="meta-copy">还缺：${escapeHtml(item.missing_evidence.join("；"))}</p>`
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
    copy: "每个组合只保留一张主卡。",
    content: cards.length
      ? `<div class="rdp-worklist">${cards.join("")}</div>`
      : notice("当前没有新的待处理组合。", "info"),
  });
}

function renderReleaseCandidatesCard({
  payload = {},
  canAdmin = false,
}) {
  const items = payload.items || [];
  if (!items.length) return "";
  const cards = items.map((item) => renderWorkItem({
    tone: "info",
    kicker: `${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
    title: item.headline || "已批准，待发布",
    pills: [
      `<span class="signal-pill tone-info">待发布</span>`,
      item.gate_status && item.gate_status !== "not_run"
        ? `<span class="signal-pill tone-${escapeHtml(toneForGate(item.gate_status))}">Gate ${escapeHtml(labelForGate(item.gate_status))}</span>`
        : "",
    ].filter(Boolean),
    body: `
      <p class="meta-copy">${escapeHtml(item.decision_summary || "这组参数已经批准，可以继续进入发布。")}</p>
      ${item.gate_note ? `<p class="meta-copy">最近 Gate：${escapeHtml(item.gate_note)}</p>` : ""}
      ${item.reason_summary?.length
        ? `<ul class="rdp-bullet-list">${item.reason_summary.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
        : ""}
    `,
    meta: [
      item.created_at ? `批准于 ${relativeTime(item.created_at)}` : "",
    ],
    actions: (item.actions || [])
      .map((action, index) => renderActionDescriptor(action, canAdmin, index === 1 ? "primary" : "ghost"))
      .join(""),
  }));
  return surfaceCard({
    title: "待发布候选",
    kicker: "审批后的下一步",
    copy: "先跑 Gate，再决定是否创建发布。",
    content: `<div class="rdp-worklist">${cards.join("")}</div>`,
  });
}

function renderIntegrityAlertsCard(alerts = []) {
  const integrity = alerts.integrity_alerts || [];
  const operational = alerts.operational_alerts || [];
  return surfaceCard({
    title: "当前阻断",
    kicker: "审批前先看",
    copy: "先处理这些问题，再继续审批和发布。",
    content: `
      ${summaryStrip([
        {
          label: "证据问题",
          value: `${integrity.length} 条`,
          meta: integrity.length ? "这些问题会直接挡住审批" : "当前没有新的证据阻断",
          tone: integrity.length ? "danger" : "positive",
          badge: actorTags("system"),
        },
        {
          label: "系统问题",
          value: `${operational.length} 条`,
          meta: operational.length ? "这些问题会拖慢流程推进" : "当前没有额外系统问题",
          tone: operational.length ? "warning" : "outline",
          badge: actorTags("system"),
        },
      ])}
      ${(integrity.length || operational.length)
        ? `<div class="rdp-worklist">${[...integrity, ...operational].slice(0, 3).map((alert) => renderWorkItem({
            tone: alert.severity === "danger" ? "danger" : "warning",
            kicker: alert.phase ? `${labelForEvidencePhase(String(alert.phase))}` : "系统状态",
            title: alert.title || "当前存在阻断",
            body: `<p class="meta-copy">${escapeHtml(alert.message || "当前告警缺少详细说明。")}</p>`,
            meta: [
              alert.blocks_approval ? "这个问题会阻断审批" : "",
            ],
          })).join("")}</div>`
        : notice("当前没有新的阻断。", "info")}
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
    title: "后台状态",
    kicker: "只看流程推进",
    copy: "这里只看现在在跑什么、下一步是什么。",
    content: summaryStrip([
      {
        label: "当前运行",
        value: labelForWorkflow(overview.current_execution?.workflow),
        meta: overview.current_execution?.started_at
          ? `开始于 ${relativeTime(overview.current_execution.started_at)}`
          : "当前没有运行中的任务",
        tone: overview.current_execution?.workflow ? "info" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "下一项",
        value: labelForWorkflow(overview.next_queue?.workflow),
        meta: overview.next_queue?.requested_at
          ? `排队于 ${relativeTime(overview.next_queue.requested_at)}`
          : "当前没有新的排队任务",
        tone: overview.next_queue?.workflow ? "warning" : "outline",
        badge: actorTags("system"),
      },
      {
        label: "后台服务",
        value: labelForHealth(health.daemon),
        meta: environment.name ? `环境 ${labelForEnvironment(environment.name)}` : "运行环境待确认",
        tone: toneForHealth(health.daemon),
        badge: actorTags("system"),
      },
      {
        label: "Gate 结果",
        value: labelForGate(health.latest_gate),
        meta: "发布前仍需以最新 Gate 为准",
        tone: toneForGate(health.latest_gate),
        badge: actorTags("system"),
      },
    ]),
  });
}

function renderReleaseHistoryStaleNotice(releaseHistoryStatus = {}) {
  // H-R1 + H2：后端 load_release_history 已经在 DB 不可达 / 数据为副本时打了
  // source + stale + stale_reason。UI 必须把 stale=true 显式透给运营者，
  // 否则 DB 抖动期间的副本会被当成实时真源，放大 H-R1 想解决的问题。
  if (releaseHistoryStatus?.stale !== true) return "";
  const reason = String(releaseHistoryStatus?.stale_reason || "").trim();
  const copy = reason === "db_unreachable"
    ? "当前治理 DB 暂不可达，显示的是上次保存的 JSON 副本；观察与回滚决策前请刷新确认真源。"
    : "当前发布历史非实时真源，请谨慎操作。";
  return callout({
    title: "发布历史数据为副本",
    copy,
    tone: "warning",
    pills: [actorTags("system")],
  });
}

function renderObservationRailCard({
  rdpControl = {},
  canAdmin = false,
}) {
  const observationQueue = rdpControl.observation_queue || [];
  const items = observationQueue.slice(0, 4);
  const staleNotice = renderReleaseHistoryStaleNotice(rdpControl.release_history_status || {});
  const queueBody = items.length
    ? `<div class="rdp-worklist">${items.map((item) => buildObservationCard(item, canAdmin)).join("")}</div>`
    : notice("当前没有观察中的发布或回滚建议。", "info");
  return surfaceCard({
    title: "观察与回滚",
    kicker: "发布后链路",
    copy: "这里只看观察中的发布和回滚建议。",
    content: `${staleNotice}${queueBody}`,
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
    kicker: "只看需要人工确认的提案",
    copy: tuningOverview.headline || "这里只展示待审核的调优提案。",
    content: `
      ${summaryStrip([
        {
          label: "待审核",
          value: `${tuningOverview.pending_review_count || 0} 条`,
          meta: "人工确认后才会写入调优规则",
          tone: tuningOverview.pending_review_count ? "warning" : "outline",
          badge: actorTags("ai"),
        },
        {
          label: "已生效规则",
          value: `${tuningOverview.active_override_count || 0} 组`,
          meta: "会影响后续研究、回放和默认值",
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
          ${rdpWorkbenchOverview.overall_status === "rollback_required"
            ? ""
            : renderReleaseCandidatesCard({
              payload: rdpWorkbenchItems.release_candidates || {},
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
      ${entries.map(([key, value]) => `<li>${escapeHtml(labelForEvidenceMetric(String(key)))}：${escapeHtml(String(formatEvidenceMetricValue(String(key), value)))}</li>`).join("")}
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
              阶段：${escapeHtml(labelForEvidencePhase(String(entry.phase || "")))}
              · 状态：${escapeHtml(labelForEvidenceStatus(String(entry.status || "")))}
              ${entry.round_id ? `· 轮次：${escapeHtml(String(entry.round_id))}` : ""}
            </p>
            ${entry.incomplete_reason
              ? `<p class="meta-copy">不完整原因：${escapeHtml(labelForIncompleteReason(String(entry.incomplete_reason)))}</p>`
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
                  .map(([key, value]) => `<li>${escapeHtml(labelForSourceRound(String(key)))}：${escapeHtml(String(value))}</li>`)
                  .join("")}
              </ul>
            </section>
          `
          : ""}
      </div>
    </details>
  `;
}
