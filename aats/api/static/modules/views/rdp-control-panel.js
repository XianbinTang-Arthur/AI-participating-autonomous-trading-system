import {
  actorTags,
  actionButton,
  callout,
  notice,
  primaryStatusPanel,
  summaryStrip,
  surfaceCard,
  timeline,
} from "../components.js";
import { escapeHtml } from "../formatters.js";

const WORKFLOW_LABELS = {
  data_maintenance: "数据维护",
  governance_cycle: "治理流程",
  research_cycle: "研究流程",
  decision_cycle: "决策流程",
};

const RECOMMENDATION_TYPE_LABELS = {
  parameter_upgrade: "参数升级",
  keep_active: "保持当前",
  lower_priority: "降低优先级",
  pause: "暂停",
  require_review: "需要复核",
};

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
};

const EFFECTIVENESS_LABELS = {
  effective: "效果正向",
  mixed: "结果混合",
  ineffective: "效果不佳",
  rollback_triggered: "已触发回滚",
  insufficient_evidence: "证据不足",
};

const CHECK_LABELS = {
  "governance_db:connection": "治理数据库连接",
  "task_queue:queue_state": "任务队列状态",
  "runtime:rdp-daemon": "RDP 守护进程",
  "alerts:current_alerts": "当前可靠性告警",
  "workflow_runs:freshness": "工作流新鲜度",
  "live_db:readonly_access": "Live 只读链路",
  "parameters:active_parameter_sets": "当前 active 参数集",
};

function relativeTime(isoString) {
  if (!isoString) return "待确认";
  const timestamp = new Date(isoString).getTime();
  if (!Number.isFinite(timestamp)) return "待确认";
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
  if (!text) return "待确认";
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

function toneForObservationStatus(status) {
  if (status === "completed") return "positive";
  if (status === "observing") return "info";
  if (status === "rollback_recommended") return "danger";
  return "outline";
}

function toneForApplyResult(status) {
  if (status === "success") return "positive";
  if (status === "failed" || status === "blocked_by_gate") return "danger";
  return "outline";
}

function toneForConfidence(confidence) {
  if (confidence === "high") return "positive";
  if (confidence === "medium") return "info";
  if (confidence === "low") return "warning";
  return "outline";
}

function toneForRecommendationStatus(status) {
  if (status === "approved") return "info";
  if (status === "draft") return "warning";
  if (status === "rejected" || status === "superseded") return "outline";
  return "outline";
}

function labelForEnvironment(environment) {
  if (environment === "prod") return "生产";
  if (environment === "staging") return "预发";
  if (environment === "dev") return "开发";
  return environment || "待确认";
}

function labelForHealth(status) {
  return HEALTH_LABELS[status] || status || "待确认";
}

function labelForGate(status) {
  return GATE_LABELS[status] || status || "未运行";
}

function labelForObservationStatus(status) {
  return OBSERVATION_STATUS_LABELS[status] || status || "待确认";
}

function labelForApplyResult(status) {
  return APPLY_RESULT_LABELS[status] || status || "待确认";
}

function labelForConfidence(confidence) {
  return CONFIDENCE_LABELS[confidence] || confidence || "待确认";
}

function labelForRecommendationType(type) {
  return RECOMMENDATION_TYPE_LABELS[type] || type || "待确认";
}

function labelForRecommendationStatus(status) {
  if (status === "draft") return "待审批";
  if (status === "approved") return "已批准";
  if (status === "rejected") return "已拒绝";
  if (status === "superseded") return "已替代";
  return status || "待确认";
}

function labelForEffectiveness(conclusion) {
  return EFFECTIVENESS_LABELS[conclusion] || conclusion || "待确认";
}

function checkLabel(check) {
  const key = `${check?.category || ""}:${check?.name || ""}`;
  return CHECK_LABELS[key] || check?.name || "检查项";
}

function checkTone(check) {
  const status = String(check?.status || "").toLowerCase();
  if (status === "ok") return "positive";
  if (status === "warn") return "warning";
  if (status === "blocked") return "danger";
  return "outline";
}

function checkStatusText(check) {
  const status = String(check?.status || "").toLowerCase();
  if (status === "ok") return "通过";
  if (status === "warn") return "警告";
  if (status === "blocked") return "阻断";
  if (status === "missing") return "缺失";
  return status || "待确认";
}

function workflowStatus(taskInfo) {
  if (!taskInfo) return { value: "未运行", meta: "还没有相关执行记录", tone: "outline" };
  const status = taskInfo.status || "unknown";
  if (status === "running") {
    return {
      value: "运行中",
      meta: taskInfo.started_at ? `开始于 ${relativeTime(taskInfo.started_at)}` : "正在执行",
      tone: "info",
    };
  }
  if (status === "done") {
    return {
      value: "已完成",
      meta: taskInfo.finished_at ? `完成于 ${relativeTime(taskInfo.finished_at)}` : "最近一次执行成功",
      tone: "positive",
    };
  }
  if (status === "failed") {
    return {
      value: "失败",
      meta: taskInfo.error_message || "任务执行失败",
      tone: "danger",
    };
  }
  if (status === "pending") {
    return { value: "排队中", meta: "等待 daemon 处理", tone: "warning" };
  }
  return { value: status, meta: "状态待确认", tone: "outline" };
}

function isBusy(taskInfo) {
  return Boolean(taskInfo && ["pending", "running"].includes(taskInfo.status));
}

function firstNonEmpty(...values) {
  for (const value of values) {
    if (value === null || value === undefined) continue;
    if (String(value).trim()) return String(value).trim();
  }
  return "";
}

function buildAppliedRecommendationIds(activeParameters = {}) {
  const result = new Set();
  Object.values(activeParameters || {}).forEach((item) => {
    if (item?.approval_recommendation_id) result.add(item.approval_recommendation_id);
  });
  return result;
}

function latestGateByRecommendation(recentGateResults = []) {
  const byRecommendation = new Map();
  recentGateResults.forEach((item) => {
    const recommendationId = String(item?.recommendation_id || "").trim();
    if (recommendationId && !byRecommendation.has(recommendationId)) {
      byRecommendation.set(recommendationId, item);
    }
  });
  return byRecommendation;
}

function latestReleaseByRecommendation(recentReleases = []) {
  const byRecommendation = new Map();
  recentReleases.forEach((item) => {
    const recommendationId = String(item?.recommendation_id || "").trim();
    if (recommendationId && !byRecommendation.has(recommendationId)) {
      byRecommendation.set(recommendationId, item);
    }
  });
  return byRecommendation;
}

function governanceStateByCombo(governanceState = {}) {
  const byCombo = new Map();
  (governanceState.combo_states || []).forEach((item) => {
    if (item?.combo_key) byCombo.set(item.combo_key, item);
  });
  return byCombo;
}

function comboKey(family, timeframe) {
  if (!family || !timeframe) return "";
  return `${family}_${String(timeframe).toLowerCase()}`;
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

function renderCommandBar({
  environment = {},
  health = {},
  operationsSummary = {},
  tasks = {},
  releaseCandidates = [],
  draftParameterRecommendations = [],
  observationQueue = [],
  canAdmin = false,
}) {
  const dataMaintenance = tasks.data_maintenance || null;
  const researchCycle = tasks.research_cycle || null;
  const maintenanceBusy = isBusy(dataMaintenance);
  const researchBusy = isBusy(researchCycle);
  const activeObservation = observationQueue.find((item) =>
    ["rollback_recommended", "observing"].includes(item?.observation_status),
  );
  const firstReleaseCandidate = releaseCandidates[0] || null;

  let headline = "先确认当前是否允许变更，再决定推进哪条建议。";
  let summary = `当前处于${labelForEnvironment(environment.name)}环境。`;
  let tone = "neutral";

  if (environment.name === "prod" && !environment.production_apply_enabled) {
    headline = "当前处于生产冻结状态，禁止直接推动参数进入生产。";
    summary = "生产 direct apply 已冻结。只有解除冻结并通过发布流程后，新的参数才允许生效。";
    tone = "danger";
  } else if (health.overall_health === "blocked") {
    headline = "当前发布前检查存在阻断项，先处理阻断再发。";
    summary = `RDP 健康状态为${labelForHealth(health.overall_health)}，页面下方会直接列出阻断原因。`;
    tone = "danger";
  } else if (firstReleaseCandidate) {
    headline = "当前已有已批准建议，可以进入发布。";
    summary = `优先处理 ${firstReleaseCandidate.family || "策略"}/${firstReleaseCandidate.timeframe || "周期"}，并至少保持 ${environment.required_observation_window_hours || 24} 小时观察窗口。`;
    tone = "positive";
  } else if (draftParameterRecommendations.length > 0) {
    headline = "当前有候选建议待审批，先做筛选再谈发布。";
    summary = "不要直接冲到参数生效。先确认建议是否成立，再把合格建议推到发布步骤。";
    tone = "warning";
  } else if (activeObservation) {
    headline = "当前没有新的发布候选，优先盯住观察中的 release。";
    summary = `release ${shortId(activeObservation.release_id)} 仍处于${labelForObservationStatus(activeObservation.observation_status)}。`;
    tone = "info";
  }

  let primaryAction = "";
  if (firstReleaseCandidate) {
    primaryAction = actionButton("创建发布", "rdp-create-release", firstReleaseCandidate.recommendation_id, "primary", {
      disabled: !canAdmin || Boolean(operationsSummary.health_blocked),
      title: !canAdmin
        ? "当前账号只有查看权限"
        : operationsSummary.health_blocked
          ? "当前发布前检查仍有阻断项，先处理第 2 步中的问题"
          : "把已批准建议推进到 release 流程",
    });
  } else if (activeObservation) {
    const observationValue = `${activeObservation.release_id}|${activeObservation.observation_window_hours || environment.required_observation_window_hours || 24}`;
    primaryAction = actionButton("运行观察", "rdp-run-observation", observationValue, "primary", {
      disabled: !canAdmin,
      title: !canAdmin ? "当前账号只有查看权限" : "对当前观察中的 release 重新跑一次观察结论",
    });
  }

  const actions = `
    <div class="rdp-command-actions">
      ${primaryAction}
      ${actionButton(
        maintenanceBusy ? "数据维护进行中" : "刷新数据",
        "rdp-trigger-workflow",
        "data_maintenance",
        "secondary",
        {
          disabled: !canAdmin || maintenanceBusy,
          title: !canAdmin ? "当前账号只有查看权限" : "刷新近期数据和基础产物",
        },
      )}
      ${actionButton(
        researchBusy ? "研究流程进行中" : "运行研究",
        "rdp-trigger-workflow",
        "research_cycle",
        "secondary",
        {
          disabled: !canAdmin || researchBusy,
          title: !canAdmin ? "当前账号只有查看权限" : "刷新研究结论和参数候选",
        },
      )}
    </div>
  `;

  return primaryStatusPanel({
    eyebrow: "RDP 发布指挥台",
    title: "先判断能不能动，再决定动哪条建议",
    headline,
    summary,
    tone,
    actions,
    pills: [
      actorTags("system"),
      `<span class="signal-pill tone-outline">环境 ${escapeHtml(labelForEnvironment(environment.name))}</span>`,
      `<span class="signal-pill tone-${escapeHtml(toneForHealth(health.overall_health))}">健康 ${escapeHtml(labelForHealth(health.overall_health))}</span>`,
      `<span class="signal-pill tone-${escapeHtml(toneForGate(operationsSummary.latest_gate_status))}">最近 Gate ${escapeHtml(labelForGate(operationsSummary.latest_gate_status))}</span>`,
      `<span class="signal-pill tone-info">观察中 ${escapeHtml(String(operationsSummary.observing_release_count || 0))}</span>`,
    ],
    metrics: [
      {
        label: "数据维护",
        value: workflowStatus(dataMaintenance).value,
        meta: workflowStatus(dataMaintenance).meta,
        tone: workflowStatus(dataMaintenance).tone,
        badge: actorTags("system"),
      },
      {
        label: "研究流程",
        value: workflowStatus(researchCycle).value,
        meta: workflowStatus(researchCycle).meta,
        tone: workflowStatus(researchCycle).tone,
        badge: actorTags("ai", "system"),
      },
      {
        label: "待审批建议",
        value: `${draftParameterRecommendations.length} 条`,
        meta: draftParameterRecommendations.length ? "先审批，再进入发布步骤" : "当前没有待审批的参数建议",
        tone: draftParameterRecommendations.length ? "warning" : "outline",
        badge: actorTags("operator"),
      },
      {
        label: "可发布建议",
        value: `${releaseCandidates.length} 条`,
        meta: releaseCandidates.length ? "已批准，下一步创建 release" : "当前没有待发的参数建议",
        tone: releaseCandidates.length ? "positive" : "outline",
        badge: actorTags("system"),
      },
    ],
  });
}

function renderRecommendationStep({
  pendingRecommendations = [],
  activeParameters = {},
  governanceState = {},
  canAdmin = false,
}) {
  const appliedRecommendationIds = buildAppliedRecommendationIds(activeParameters);
  const comboStates = governanceStateByCombo(governanceState);
  const draftRecommendations = pendingRecommendations.filter((item) => item.recommendation_type === "parameter_upgrade" && item.status === "draft");
  const approvedRecommendations = pendingRecommendations.filter((item) => item.recommendation_type === "parameter_upgrade" && item.status === "approved");
  const strategicRecommendations = pendingRecommendations.filter((item) => item.recommendation_type !== "parameter_upgrade");

  const draftCards = draftRecommendations.map((item) => {
    const state = comboStates.get(comboKey(item.family, item.timeframe)) || {};
    const body = `
      <p class="meta-copy">${escapeHtml(firstNonEmpty(item.reason, (state.latest_round_reasons || []).join("；"), "这条建议还没有补充说明。"))}</p>
      <p class="meta-copy">建议动作：${escapeHtml(labelForRecommendationType(item.recommendation_type))}。${state.candidate_parameter_set_id ? ` 候选参数集 ${escapeHtml(shortId(state.candidate_parameter_set_id))}。` : ""}</p>
    `;
    const actions = [
      actionButton("审批", "rdp-approve-only", item.recommendation_id, "primary", {
        disabled: !canAdmin,
        title: !canAdmin ? "当前账号只有查看权限" : "只完成审批，不直接推动生产生效",
      }),
      actionButton("拒绝", "rdp-reject-recommendation", item.recommendation_id, "ghost", {
        disabled: !canAdmin,
        title: !canAdmin ? "当前账号只有查看权限" : "拒绝这条建议",
      }),
    ].join("");
    return renderWorkItem({
      tone: toneForRecommendationStatus(item.status),
      kicker: `${item.symbol || "参数建议"} / ${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
      title: shortId(item.recommendation_id),
      pills: [
        `<span class="signal-pill tone-${escapeHtml(toneForConfidence(item.confidence))}">置信度 ${escapeHtml(labelForConfidence(item.confidence))}</span>`,
        `<span class="signal-pill tone-warning">${escapeHtml(labelForRecommendationStatus(item.status))}</span>`,
      ],
      body,
      meta: [
        `创建于 ${relativeTime(item.created_at)}`,
        "这里只做审批与拒绝，不直接让参数生效",
      ],
      actions,
    });
  });

  const approvedCards = approvedRecommendations
    .filter((item) => !appliedRecommendationIds.has(item.recommendation_id))
    .map((item) => renderWorkItem({
      tone: "info",
      kicker: `${item.symbol || "参数建议"} / ${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
      title: shortId(item.recommendation_id),
      pills: [
        `<span class="signal-pill tone-info">${escapeHtml(labelForRecommendationStatus(item.status))}</span>`,
      ],
      body: `<p class="meta-copy">这条建议已经批准。下一步去第 3 步创建发布，而不是直接应用参数。</p>`,
      meta: [`创建于 ${relativeTime(item.created_at)}`],
    }));

  const strategicCards = strategicRecommendations.map((item) => renderWorkItem({
    tone: toneForRecommendationStatus(item.status),
    kicker: `${item.symbol || "策略建议"} / ${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
    title: labelForRecommendationType(item.recommendation_type),
    pills: [
      `<span class="signal-pill tone-${escapeHtml(toneForRecommendationStatus(item.status))}">${escapeHtml(labelForRecommendationStatus(item.status))}</span>`,
    ],
    body: `<p class="meta-copy">${escapeHtml(firstNonEmpty(item.reason, "这条策略建议还没有补充说明。"))}</p>`,
    meta: [`创建于 ${relativeTime(item.created_at)}`],
    actions: item.status === "draft"
      ? [
        actionButton("审批", "rdp-approve-only", item.recommendation_id, "secondary", {
          disabled: !canAdmin,
          title: !canAdmin ? "当前账号只有查看权限" : "确认这条策略建议",
        }),
        actionButton("拒绝", "rdp-reject-recommendation", item.recommendation_id, "ghost", {
          disabled: !canAdmin,
          title: !canAdmin ? "当前账号只有查看权限" : "拒绝这条策略建议",
        }),
      ].join("")
      : "",
  }));

  let content = "";
  if (!draftCards.length && !approvedCards.length && !strategicCards.length) {
    content = notice("当前没有需要处理的建议。新的 recommendation 出来后，这里会先进入审批阶段。", "info");
  } else {
    content = `
      ${draftCards.length ? `<div class="rdp-worklist">${draftCards.join("")}</div>` : notice("当前没有待审批的参数建议。", "outline")}
      ${approvedCards.length ? `
        <div class="rdp-inline-block">
          <p class="meta-copy rdp-subtle-heading">已批准，等待进入发布步骤</p>
          <div class="rdp-worklist">${approvedCards.join("")}</div>
        </div>
      ` : ""}
      ${strategicCards.length ? `
        <div class="rdp-inline-block">
          <p class="meta-copy rdp-subtle-heading">治理建议</p>
          <div class="rdp-worklist">${strategicCards.join("")}</div>
        </div>
      ` : ""}
    `;
  }

  return surfaceCard({
    title: "1. 候选建议",
    kicker: "先做筛选",
    copy: "先判断哪条建议值得推进。审批和拒绝在这里完成，生产生效不在这里做。",
    content,
  });
}

function renderPreflightStep({
  environment = {},
  health = {},
  recentGateResults = [],
}) {
  const latestGate = recentGateResults[0] || null;
  const checks = (health.checks || []).filter((item) =>
    ["governance_db", "task_queue", "runtime", "alerts", "workflow_runs", "live_db", "parameters"].includes(item.category),
  );

  const checkCards = checks.length
    ? `<div class="rdp-checklist">${checks.map((check) => `
        <article class="rdp-checkitem tone-${escapeHtml(checkTone(check))}">
          <div class="panel-head">
            <strong>${escapeHtml(checkLabel(check))}</strong>
            <span class="signal-pill tone-${escapeHtml(checkTone(check))}">${escapeHtml(checkStatusText(check))}</span>
          </div>
          <p class="meta-copy">${escapeHtml(check.detail || "当前没有额外说明")}</p>
        </article>
      `).join("")}</div>`
    : notice("当前还没有可展示的发布前检查结果。", "outline");

  const latestGateNotice = latestGate
    ? callout({
      title: `最近一次 Gate：${labelForGate(latestGate.gate_status)}`,
      copy: latestGate.blocking_reasons?.length
        ? latestGate.blocking_reasons[0]
        : latestGate.warnings?.length
          ? latestGate.warnings[0]
          : "最近一次 Gate 没有返回阻断或警告。",
      pills: [
        actorTags("system"),
        `<span class="signal-pill tone-${escapeHtml(toneForGate(latestGate.gate_status))}">${escapeHtml(labelForGate(latestGate.gate_status))}</span>`,
      ],
    })
    : notice("还没有针对具体 recommendation 运行 Gate。下面先展示系统级发布前检查。", "info");

  return surfaceCard({
    title: "2. 发布前检查",
    kicker: "先回答能不能发",
    copy: "这里解释当前为什么能发、不能发，或者只能谨慎推进。不要让操作者自己从几十个字段里猜。",
    content: `
      ${summaryStrip([
        {
          label: "当前环境",
          value: labelForEnvironment(environment.name),
          meta: environment.require_gate_pass ? "必须经过 Gate" : "当前环境不强制 Gate",
          tone: "outline",
          badge: actorTags("config"),
        },
        {
          label: "RDP 健康",
          value: labelForHealth(health.overall_health),
          meta: health.blocking_reasons?.length
            ? `阻断 ${health.blocking_reasons.length} 项`
            : health.warnings?.length
              ? `警告 ${health.warnings.length} 项`
              : "当前没有已知阻断",
          tone: toneForHealth(health.overall_health),
          badge: actorTags("system"),
        },
        {
          label: "最近 Gate",
          value: labelForGate(latestGate?.gate_status),
          meta: latestGate ? `运行于 ${relativeTime(latestGate.created_at)}` : "还没有运行 Gate",
          tone: toneForGate(latestGate?.gate_status),
          badge: actorTags("system"),
        },
        {
          label: "观察窗口",
          value: `${environment.required_observation_window_hours || 24} 小时`,
          meta: environment.name === "prod" ? "生产环境的最短观察窗口" : "当前环境默认观察窗口",
          tone: "info",
          badge: actorTags("config"),
        },
      ])}
      ${latestGateNotice}
      ${checkCards}
    `,
  });
}

function renderReleaseStep({
  environment = {},
  health = {},
  pendingRecommendations = [],
  activeParameters = {},
  governanceState = {},
  recentGateResults = [],
  recentReleases = [],
  canAdmin = false,
}) {
  const appliedRecommendationIds = buildAppliedRecommendationIds(activeParameters);
  const comboStates = governanceStateByCombo(governanceState);
  const gateByRecommendation = latestGateByRecommendation(recentGateResults);
  const releaseByRecommendation = latestReleaseByRecommendation(recentReleases);

  const candidates = pendingRecommendations.filter((item) =>
    item.recommendation_type === "parameter_upgrade"
    && item.status === "approved"
    && !appliedRecommendationIds.has(item.recommendation_id),
  );

  const cards = candidates.map((item) => {
    const gate = gateByRecommendation.get(item.recommendation_id);
    const latestRelease = releaseByRecommendation.get(item.recommendation_id);
    const comboState = comboStates.get(comboKey(item.family, item.timeframe)) || {};
    const targetParameterSetId = firstNonEmpty(
      item.target_parameter_set_id,
      comboState.candidate_parameter_set_id,
    );
    return renderWorkItem({
      tone: health.overall_health === "blocked" ? "danger" : "positive",
      kicker: `${item.symbol || "参数建议"} / ${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
      title: shortId(item.recommendation_id),
      pills: [
        `<span class="signal-pill tone-${escapeHtml(toneForGate(gate?.gate_status))}">Gate ${escapeHtml(labelForGate(gate?.gate_status))}</span>`,
        `<span class="signal-pill tone-${escapeHtml(toneForConfidence(item.confidence))}">置信度 ${escapeHtml(labelForConfidence(item.confidence))}</span>`,
      ],
      body: `
        <p class="meta-copy">目标参数集：${escapeHtml(shortId(targetParameterSetId))}。${latestRelease ? ` 最近一次 release 为 ${escapeHtml(shortId(latestRelease.release_id))}。` : " 还没有相关 release 记录。"}</p>
        <p class="meta-copy">${escapeHtml(firstNonEmpty(item.reason, "这条建议已经通过审批，可以进入发布流程。"))}</p>
      `,
      meta: [
        `观察窗口至少 ${environment.required_observation_window_hours || 24} 小时`,
        gate?.created_at ? `最近 Gate 于 ${relativeTime(gate.created_at)}` : "建议先运行 Gate，再创建发布",
      ],
      actions: [
        actionButton("运行 Gate", "rdp-run-gate", item.recommendation_id, "secondary", {
          disabled: !canAdmin,
          title: !canAdmin ? "当前账号只有查看权限" : "先验证这条建议当前是否允许发布",
        }),
        actionButton("创建发布", "rdp-create-release", item.recommendation_id, "primary", {
          disabled: !canAdmin || health.overall_health === "blocked",
          title: !canAdmin
            ? "当前账号只有查看权限"
            : health.overall_health === "blocked"
              ? "当前发布前检查仍有阻断项，先处理第 2 步中的问题"
              : "统一通过 release 流程让参数进入运行态",
        }),
      ].join(""),
    });
  });

  const timelineItems = (recentReleases || []).slice(0, 5).map((item) => ({
    title: `${item.family || "未知策略"}/${item.timeframe || "未知周期"} · ${shortId(item.release_id)}`,
    subtitle: `${labelForApplyResult(item.apply_result)} / ${labelForObservationStatus(item.observation_status)}`,
    detail: `参数集 ${shortId(item.parameter_set_id)}，Gate ${labelForGate(item.gate_status)}`,
    timestamp: item.created_at ? relativeTime(item.created_at) : "待确认",
    pill: `<span class="signal-pill tone-${escapeHtml(toneForApplyResult(item.apply_result))}">${escapeHtml(labelForApplyResult(item.apply_result))}</span>`,
  }));

  return surfaceCard({
    title: "3. 发布执行",
    kicker: "生产语义统一走 release",
    copy: "这里才允许参数进入运行态。生产环境不再把“应用参数”当成主路径，而是统一通过 release 承载 gate、apply、observation 和审计。",
    content: `
      ${cards.length
        ? `<div class="rdp-worklist">${cards.join("")}</div>`
        : notice("当前没有已批准且待发布的参数建议。先回到第 1 步完成审批。", "info")}
      <div class="rdp-inline-block">
        <p class="meta-copy rdp-subtle-heading">最近发布记录</p>
        ${timeline(timelineItems, "当前还没有 release 记录。")}
      </div>
    `,
  });
}

function renderObservationStep({
  observationQueue = [],
  canAdmin = false,
}) {
  const activeItems = observationQueue.filter((item) =>
    ["observing", "rollback_recommended", "completed", "not_started"].includes(item.observation_status),
  );

  const cards = activeItems.map((item) => {
    const observation = item.observation || {};
    const effectiveness = item.effectiveness || {};
    const rolloutActions = [];
    const observationValue = `${item.release_id}|${item.observation_window_hours || 24}`;
    rolloutActions.push(
      actionButton("运行观察", "rdp-run-observation", observationValue, "secondary", {
        disabled: !canAdmin,
        title: !canAdmin ? "当前账号只有查看权限" : "重新评估这次发布的观察结论",
      }),
    );
    rolloutActions.push(
      actionButton(
        "执行回滚",
        "rdp-rollback-parameters",
        `${item.family}/${item.timeframe}`,
        item.observation_status === "rollback_recommended" ? "warning" : "ghost",
        {
          disabled: !canAdmin || !item.is_current_active_release || item.apply_result !== "success",
          title: !canAdmin
            ? "当前账号只有查看权限"
            : !item.is_current_active_release
              ? "当前已不是这次 release 在生效，禁止直接回滚"
              : item.apply_result !== "success"
                ? "只有已成功生效的 release 才允许执行回滚"
                : "回滚到这次 release 之前的参数版本",
        },
      ),
    );

    return renderWorkItem({
      tone: toneForObservationStatus(item.observation_status),
      kicker: `${item.family || "未知策略"}/${item.timeframe || "未知周期"}`,
      title: shortId(item.release_id),
      pills: [
        `<span class="signal-pill tone-${escapeHtml(toneForObservationStatus(item.observation_status))}">${escapeHtml(labelForObservationStatus(item.observation_status))}</span>`,
        `<span class="signal-pill tone-${escapeHtml(toneForApplyResult(item.apply_result))}">${escapeHtml(labelForApplyResult(item.apply_result))}</span>`,
      ],
      body: `
        <p class="meta-copy">当前参数集 ${escapeHtml(shortId(item.parameter_set_id))}，上一版 ${escapeHtml(shortId(item.previous_parameter_set_id))}。${item.is_current_active_release ? " 这次 release 仍然是当前生效版本。" : " 这次 release 已经不是当前生效版本。"}</p>
        <p class="meta-copy">观察结论：${escapeHtml(labelForObservationStatus(observation.status || item.observation_status))}。${observation.recommendation ? ` 建议 ${escapeHtml(observation.recommendation)}。` : ""}${effectiveness.conclusion ? ` Effectiveness=${escapeHtml(labelForEffectiveness(effectiveness.conclusion))}。` : ""}</p>
      `,
      meta: [
        `发布时间 ${relativeTime(item.created_at)}`,
        observation.evaluated_at ? `最近观察于 ${relativeTime(observation.evaluated_at)}` : "还没有观察结果",
        effectiveness.detail || "",
      ],
      actions: rolloutActions.join(""),
    });
  });

  return surfaceCard({
    title: "4. 观察与回滚",
    kicker: "发布后先盯 release，不是盯参数组合",
    copy: "发布后先判断 release 是否仍然健康，再决定继续观察还是执行回滚。回滚只能作用于当前仍在生效的 release。",
    content: cards.length
      ? `<div class="rdp-worklist">${cards.join("")}</div>`
      : notice("当前没有需要跟踪的 release。新的发布进入观察期后，这里会自动出现。", "outline"),
  });
}

export function renderRdpControlPanelV2({ rdpControl = {}, canAdmin = false }) {
  const tasks = rdpControl.tasks || {};
  const pendingRecommendations = rdpControl.pending_recommendations || [];
  const activeParameters = rdpControl.active_parameters || {};
  const governanceState = rdpControl.governance_state || {};
  const health = rdpControl.health || {};
  const environment = rdpControl.environment || {};
  const operationsSummary = rdpControl.operations_summary || {};
  const recentGateResults = rdpControl.recent_gate_results || [];
  const recentReleases = rdpControl.recent_releases || [];
  const observationQueue = rdpControl.observation_queue || [];

  const releaseCandidates = pendingRecommendations.filter((item) =>
    item.recommendation_type === "parameter_upgrade"
    && item.status === "approved"
    && !buildAppliedRecommendationIds(activeParameters).has(item.recommendation_id),
  );
  const draftParameterRecommendations = pendingRecommendations.filter((item) =>
    item.recommendation_type === "parameter_upgrade" && item.status === "draft",
  );

  return `
    <div class="rdp-ops-shell">
      ${renderCommandBar({
        environment,
        health,
        operationsSummary,
        tasks,
        releaseCandidates,
        draftParameterRecommendations,
        observationQueue,
        canAdmin,
      })}
      ${renderRecommendationStep({
        pendingRecommendations,
        activeParameters,
        governanceState,
        canAdmin,
      })}
      ${renderPreflightStep({
        environment,
        health,
        recentGateResults,
      })}
      ${renderReleaseStep({
        environment,
        health,
        pendingRecommendations,
        activeParameters,
        governanceState,
        recentGateResults,
        recentReleases,
        canAdmin,
      })}
      ${renderObservationStep({
        observationQueue,
        canAdmin,
      })}
    </div>
  `;
}
