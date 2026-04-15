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
  return HEALTH_LABELS[status] || status || "未知";
}

function labelForGate(status) {
  return GATE_LABELS[status] || status || "未运行";
}

function labelForObservationStatus(status) {
  return OBSERVATION_STATUS_LABELS[status] || status || "未开始";
}

function labelForApplyResult(status) {
  return APPLY_RESULT_LABELS[status] || status || "未执行";
}

function labelForConfidence(confidence) {
  return CONFIDENCE_LABELS[confidence] || confidence || "未知";
}

function labelForRecommendationType(type) {
  return RECOMMENDATION_TYPE_LABELS[type] || type || "未知类型";
}

function labelForRecommendationStatus(status) {
  if (status === "draft") return "待审批";
  if (status === "approved") return "已批准";
  if (status === "rejected") return "已拒绝";
  if (status === "superseded") return "已替代";
  return status || "未知状态";
}

function labelForEffectiveness(conclusion) {
  return EFFECTIVENESS_LABELS[conclusion] || conclusion || "未评估";
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
  return status || "未知";
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

function sortRecommendationsByCreatedAt(recommendations = []) {
  return [...(recommendations || [])]
    .filter((item) => item && typeof item === "object")
    .sort((left, right) => {
      const leftAt = Date.parse(left.created_at || "") || 0;
      const rightAt = Date.parse(right.created_at || "") || 0;
      return rightAt - leftAt;
    });
}

function latestRecommendationsByCombo(recommendations = []) {
  const byCombo = new Map();
  sortRecommendationsByCreatedAt(recommendations).forEach((item) => {
    const key = comboKey(item.family, item.timeframe);
    if (key && !byCombo.has(key)) {
      byCombo.set(key, item);
    }
  });
  return byCombo;
}

function buildParameterCandidateEntries({
  pendingRecommendations = [],
  recommendationHistory = [],
  governanceState = {},
}) {
  const comboStates = governanceStateByCombo(governanceState);
  const sortedHistory = sortRecommendationsByCreatedAt(recommendationHistory);
  const parameterHistory = sortedHistory.filter((item) => item.recommendation_type === "parameter_upgrade");
  const governanceHistoryByCombo = latestRecommendationsByCombo(
    sortedHistory.filter((item) => item.recommendation_type !== "parameter_upgrade"),
  );
  const latestParameterByCombo = latestRecommendationsByCombo(parameterHistory);

  const comboKeys = new Set();
  comboStates.forEach((state, key) => {
    if (state?.candidate_parameter_set_id) comboKeys.add(key);
  });
  latestParameterByCombo.forEach((_item, key) => comboKeys.add(key));

  const entries = Array.from(comboKeys).map((key) => {
    const comboState = comboStates.get(key) || {};
    const parameterRecommendation = latestParameterByCombo.get(key) || null;
    const governanceRecommendation = governanceHistoryByCombo.get(key) || null;
    const createdAt = firstNonEmpty(
      parameterRecommendation?.created_at,
      comboState.latest_recommendation_created_at,
    );
    return {
      combo_key: key,
      family: comboState.family || parameterRecommendation?.family,
      timeframe: comboState.timeframe || parameterRecommendation?.timeframe,
      symbol: parameterRecommendation?.symbol || "BTC-USDT-SWAP",
      created_at: createdAt,
      candidate_parameter_set_id: firstNonEmpty(
        parameterRecommendation?.target_parameter_set_id,
        comboState.candidate_parameter_set_id,
      ),
      candidate_parameter_status: firstNonEmpty(
        comboState.candidate_parameter_status,
        parameterRecommendation?.status,
      ),
      parameter_recommendation: parameterRecommendation,
      governance_recommendation: governanceRecommendation,
      latest_round_reasons: comboState.latest_round_reasons || [],
    };
  });

  entries.sort((left, right) => {
    const leftAt = Date.parse(left.created_at || "") || 0;
    const rightAt = Date.parse(right.created_at || "") || 0;
    return rightAt - leftAt;
  });

  return {
    currentEntries: entries,
    historicalParameterRecommendations: parameterHistory.filter((item) => {
      const latest = latestParameterByCombo.get(comboKey(item.family, item.timeframe));
      return latest?.recommendation_id !== item.recommendation_id;
    }),
  };
}

function translateCheckDetail(check = {}, environment = {}) {
  const raw = firstNonEmpty(check.detail, "当前没有额外说明");
  const strictEnvironment = Boolean(environment.strict_environment);
  const key = `${check.category || ""}:${check.name || ""}`;
  const isOk = check.status === "ok";

  if (key === "governance_db:connection") {
    if (isOk) {
      return { summary: "治理数据库连接正常，发布链路可用。", nextStep: "", raw };
    }
    return {
      summary: strictEnvironment
        ? "治理数据库还没有接通，发布链路现在不可用。"
        : "治理数据库暂时不可达，当前只能查看已有产物，不能可靠地推进发布。",
      nextStep: "检查容器内治理库连接配置是否指向 postgres 服务，而不是 127.0.0.1。",
      raw,
    };
  }
  if (key === "runtime:rdp-daemon") {
    if (isOk) {
      return { summary: "RDP 守护进程运行正常，后台任务处理链路可信。", nextStep: "", raw };
    }
    return {
      summary: raw.includes("heartbeat not found")
        ? "还没有看到 RDP 守护进程的有效心跳。"
        : "RDP 守护进程状态不稳定，后台任务处理链路暂时不可信。",
      nextStep: "先确认 rdp-daemon 容器已启动，并能写入 governance.rdp_runtime_status。",
      raw,
    };
  }
  if (key === "alerts:current_alerts") {
    if (isOk) {
      return { summary: "当前没有可靠性告警，系统运行正常。", nextStep: "", raw };
    }
    return {
      summary: raw.includes("not found")
        ? "当前还没有最新的可靠性告警快照。"
        : "可靠性告警存在未处理项目，先确认是否允许继续推进。",
      nextStep: raw.includes("not found")
        ? "先运行一次"刷新数据"，让告警快照重新生成。"
        : "先打开告警详情确认阻断是否已经处理。",
      raw,
    };
  }
  if (key === "workflow_runs:freshness") {
    if (isOk) {
      return { summary: "工作流快照新鲜度正常，最近一轮结果可用于发布判断。", nextStep: "", raw };
    }
    return {
      summary: raw.includes("missing")
        ? "研究/治理/决策工作流还没有形成完整的新鲜快照。"
        : "工作流快照已经过期，当前结论不适合直接用于发布。",
      nextStep: "先运行"刷新数据"和"运行研究"，确认最近一轮结果已更新。",
      raw,
    };
  }
  if (key === "live_db:readonly_access") {
    if (isOk) {
      return { summary: "生产库只读链路已连接，发布前校验可用。", nextStep: "", raw };
    }
    return {
      summary: raw.includes("RDP_LIVE_DATABASE_URL")
        ? "还没有配置生产库只读连接，所以发布前无法核验生产事实数据。"
        : "生产库只读链路不可用，发布前检查缺少关键校验。",
      nextStep: raw.includes("RDP_LIVE_DATABASE_URL")
        ? "在 RDP 环境配置中补上生产库只读连接。"
        : "先检查只读数据库连接和权限配置。",
      raw,
    };
  }
  if (key === "parameters:active_parameter_sets") {
    if (isOk) {
      return { summary: "已读取到当前 active 参数集。", nextStep: "", raw };
    }
    return {
      summary: raw === "count=0"
        ? "当前还没有 active 参数在运行。"
        : "已读取到当前 active 参数集。",
      nextStep: raw === "count=0"
        ? "如果这是首次接入可以忽略；否则先确认参数发布链路是否已经跑通。"
        : "",
      raw,
    };
  }
  // artifact 类检查
  if (check.category === "artifacts") {
    if (isOk) {
      return { summary: "数据已就绪。", nextStep: "", raw };
    }
    return {
      summary: raw.includes("容器环境")
        ? raw
        : "对应数据文件缺失，相关功能可能不可用。",
      nextStep: "",
      raw,
    };
  }
  if (key === "task_queue:queue_state") {
    if (isOk) {
      return { summary: "任务队列正常，无积压或失败。", nextStep: "", raw };
    }
    return { summary: "任务队列有积压或失败任务。", nextStep: "查看任务列表确认是否需要重试。", raw };
  }
  return {
    summary: raw,
    nextStep: "",
    raw,
  };
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
  draftRecommendations = [],
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
  } else if (draftRecommendations.length > 0) {
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
        label: "待处理建议",
        value: `${draftRecommendations.length} 条`,
        meta: draftRecommendations.length ? "先看最新 4 组，再决定审批或拒绝" : "当前没有待处理的候选建议",
        tone: draftRecommendations.length ? "warning" : "outline",
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
  recommendationHistory = [],
  activeParameters = {},
  governanceState = {},
  uiState = {},
  canAdmin = false,
}) {
  const appliedRecommendationIds = buildAppliedRecommendationIds(activeParameters);
  const draftRecommendations = pendingRecommendations.filter((item) => item.status === "draft");
  const approvedRecommendations = pendingRecommendations.filter((item) =>
    item.recommendation_type === "parameter_upgrade" && item.status === "approved",
  );
  const draftGovernanceRecommendations = Array.from(
    latestRecommendationsByCombo(
      pendingRecommendations.filter((item) => item.recommendation_type !== "parameter_upgrade" && item.status === "draft"),
    ).values(),
  );
  const {
    currentEntries: parameterCandidateEntries,
    historicalParameterRecommendations,
  } = buildParameterCandidateEntries({
    pendingRecommendations,
    recommendationHistory,
    governanceState,
  });
  const visibleCurrentCandidates = parameterCandidateEntries
    .filter((item) => item.parameter_recommendation?.status !== "approved")
    .slice(0, 4);
  const historyExtraCount = Number(uiState.rdpRecommendationHistoryExtraCount || 0);
  const historyVisibleCount = 4 + (
    Number.isFinite(historyExtraCount) && historyExtraCount > 0 ? historyExtraCount : 0
  );
  const visibleHistory = historicalParameterRecommendations.slice(
    0,
    historyVisibleCount,
  );
  const remainingHistoryCount = Math.max(
    0,
    historicalParameterRecommendations.length - visibleHistory.length,
  );

  const draftCards = visibleCurrentCandidates.map((entry) => {
    const parameterRecommendation = entry.parameter_recommendation || {};
    const governanceRecommendation = entry.governance_recommendation || {};
    const blockedByGovernanceDraft = (
      governanceRecommendation.recommendation_id
      && governanceRecommendation.status === "draft"
      && governanceRecommendation.recommendation_type !== "parameter_upgrade"
    );
    const hasDraftParameterRecommendation = parameterRecommendation.status === "draft";
    const candidateReason = firstNonEmpty(
      parameterRecommendation.reason,
      (entry.latest_round_reasons || []).join("；"),
      "这组参数还没有补充说明。",
    );
    const governanceReason = firstNonEmpty(
      governanceRecommendation.reason,
      "",
    );
    const candidateSetId = firstNonEmpty(entry.candidate_parameter_set_id, parameterRecommendation.target_parameter_set_id);
    const body = `
      <p class="meta-copy">${
        candidateSetId
          ? `本轮生成参数集 ${escapeHtml(shortId(candidateSetId))}，状态 ${escapeHtml(entry.candidate_parameter_status || "待确认")}。`
          : "当前还没有可展示的候选参数集。"
      }</p>
      <p class="meta-copy">候选依据：${escapeHtml(candidateReason)}</p>
      <p class="meta-copy">${
        governanceRecommendation.recommendation_id
          ? `当前治理建议：${escapeHtml(labelForRecommendationType(governanceRecommendation.recommendation_type))}（${escapeHtml(labelForRecommendationStatus(governanceRecommendation.status))}）。${governanceReason ? ` 原因：${escapeHtml(governanceReason)}` : ""}`
          : hasDraftParameterRecommendation
            ? "当前主路径仍是调参建议，可在这里完成审批。"
            : "这组参数目前没有新的治理动作。"
      }</p>
    `;
    const actions = hasDraftParameterRecommendation
      ? [
        actionButton("审批", "rdp-approve-only", parameterRecommendation.recommendation_id, "primary", {
          disabled: !canAdmin || blockedByGovernanceDraft,
          title: !canAdmin
            ? "当前账号只有查看权限"
            : blockedByGovernanceDraft
              ? "同组合还有待处理的治理建议，先处理治理结论"
              : "只完成审批，不直接推动生产生效",
        }),
        actionButton("拒绝", "rdp-reject-recommendation", parameterRecommendation.recommendation_id, "ghost", {
          disabled: !canAdmin || blockedByGovernanceDraft,
          title: !canAdmin
            ? "当前账号只有查看权限"
            : blockedByGovernanceDraft
              ? "同组合还有待处理的治理建议，先处理治理结论"
              : "拒绝这条调参建议",
        }),
      ].join("")
      : "";
    return renderWorkItem({
      tone: blockedByGovernanceDraft ? "warning" : toneForRecommendationStatus(parameterRecommendation.status),
      kicker: `${entry.symbol || "参数候选"} / ${entry.family || "未知策略"} / ${entry.timeframe || "未知周期"}`,
      title: candidateSetId ? shortId(candidateSetId) : shortId(parameterRecommendation.recommendation_id),
      pills: [
        parameterRecommendation.confidence
          ? `<span class="signal-pill tone-${escapeHtml(toneForConfidence(parameterRecommendation.confidence))}">置信度 ${escapeHtml(labelForConfidence(parameterRecommendation.confidence))}</span>`
          : "",
        `<span class="signal-pill tone-${escapeHtml(hasDraftParameterRecommendation ? "warning" : "outline")}">${
          escapeHtml(hasDraftParameterRecommendation ? "待审批" : "候选已生成")
        }</span>`,
        governanceRecommendation.recommendation_id
          ? `<span class="signal-pill tone-${escapeHtml(toneForRecommendationStatus(governanceRecommendation.status))}">治理建议 ${escapeHtml(labelForRecommendationType(governanceRecommendation.recommendation_type))}</span>`
          : "",
      ].filter(Boolean),
      body,
      meta: [
        `生成于 ${relativeTime(entry.created_at)}`,
        hasDraftParameterRecommendation
          ? "这里只做审批与拒绝，不直接让参数生效"
          : blockedByGovernanceDraft
            ? "这组参数已经产出，但当前治理建议要求先暂停或复核"
            : "这组参数当前没有新的审批动作",
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

  const governanceCards = draftGovernanceRecommendations.map((item) => renderWorkItem({
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

  const historyCards = visibleHistory.map((item) => renderWorkItem({
    tone: "outline",
    kicker: `${item.symbol || "参数历史"} / ${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
    title: shortId(item.target_parameter_set_id || item.recommendation_id),
    pills: [
      `<span class="signal-pill tone-outline">${escapeHtml(labelForRecommendationStatus(item.status))}</span>`,
      item.confidence
        ? `<span class="signal-pill tone-${escapeHtml(toneForConfidence(item.confidence))}">置信度 ${escapeHtml(labelForConfidence(item.confidence))}</span>`
        : "",
    ].filter(Boolean),
    body: `<p class="meta-copy">${escapeHtml(firstNonEmpty(item.reason, "这组历史参数没有额外说明。"))}</p>`,
    meta: [`生成于 ${relativeTime(item.created_at)}`],
  }));

  let content = "";
  if (!draftCards.length && !approvedCards.length && !governanceCards.length && !historyCards.length) {
    content = notice("当前没有需要处理的建议。新的 recommendation 出来后，这里会先进入审批阶段。", "info");
  } else {
    content = `
      <div class="rdp-inline-block">
        <p class="meta-copy rdp-subtle-heading">本轮最新参数候选</p>
        ${draftCards.length
          ? `<div class="rdp-worklist">${draftCards.join("")}</div>`
          : notice("当前没有可展示的最新参数候选。", "outline")}
      </div>
      ${approvedCards.length ? `
        <div class="rdp-inline-block">
          <p class="meta-copy rdp-subtle-heading">已批准，等待进入发布步骤</p>
          <div class="rdp-worklist">${approvedCards.join("")}</div>
        </div>
      ` : ""}
      ${governanceCards.length ? `
        <div class="rdp-inline-block">
          <p class="meta-copy rdp-subtle-heading">治理建议</p>
          <div class="rdp-worklist">${governanceCards.join("")}</div>
        </div>
      ` : ""}
      ${(historyCards.length || remainingHistoryCount > 0) ? `
        <div class="rdp-inline-block">
          <p class="meta-copy rdp-subtle-heading">历史参数候选</p>
          ${historyCards.length
            ? `<div class="rdp-worklist">${historyCards.join("")}</div>`
            : notice("当前仅展示本轮最新 4 组参数。点击下方按钮可继续展开更早的历史候选。", "outline")}
          <div class="table-actions table-actions--compact">
            ${remainingHistoryCount > 0 ? actionButton(
              `加载更多历史建议（剩余 ${remainingHistoryCount} 条）`,
              "load-more-rdp-recommendations",
              "",
              "secondary",
            ) : ""}
            ${historyCards.length ? actionButton(
              "收起历史建议",
              "collapse-rdp-recommendations",
              "",
              "ghost",
            ) : ""}
          </div>
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
        ${(() => {
          const detail = translateCheckDetail(check, environment);
          return `
        <article class="rdp-checkitem tone-${escapeHtml(checkTone(check))}">
          <div class="panel-head">
            <strong>${escapeHtml(checkLabel(check))}</strong>
            <span class="signal-pill tone-${escapeHtml(checkTone(check))}">${escapeHtml(checkStatusText(check))}</span>
          </div>
          <p class="meta-copy">${escapeHtml(detail.summary)}</p>
          ${detail.nextStep ? `<p class="meta-copy">${escapeHtml(`下一步：${detail.nextStep}`)}</p>` : ""}
          ${detail.raw && detail.raw !== detail.summary ? `<details class="rdp-inline-block"><summary class="meta-copy">查看原始检查详情</summary><p class="meta-copy">${escapeHtml(detail.raw)}</p></details>` : ""}
        </article>
      `;
        })()}
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
    : notice("还没有针对具体建议运行 Gate。这里先显示系统级发布前检查，帮助你判断现在能不能推进。", "info");

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
  const releaseEmptyState = !cards.length && !timelineItems.length
    ? notice("当前既没有已批准待发布的建议，也还没有历史 release 记录。先回到第 1 步处理最新候选建议。", "info")
    : !cards.length
      ? notice("当前没有已批准且待发布的参数建议。下面保留最近的 release 记录供你参考。", "info")
      : "";

  return surfaceCard({
    title: "3. 发布执行",
    kicker: "生产语义统一走 release",
    copy: "这里才允许参数进入运行态。生产环境不再把"应用参数"当成主路径，而是统一通过 release 承载 gate、apply、observation 和审计。",
    content: `
      ${cards.length ? `<div class="rdp-worklist">${cards.join("")}</div>` : releaseEmptyState}
      ${timelineItems.length ? `
        <div class="rdp-inline-block">
          <p class="meta-copy rdp-subtle-heading">最近发布记录</p>
          ${timeline(timelineItems, "")}
        </div>
      ` : ""}
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

export function renderRdpControlPanelV2({ rdpControl = {}, canAdmin = false, uiState = {} }) {
  const tasks = rdpControl.tasks || {};
  const pendingRecommendations = rdpControl.pending_recommendations || [];
  const recommendationHistory = rdpControl.recommendation_history || [];
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
  const draftRecommendations = pendingRecommendations.filter((item) =>
    item.status === "draft",
  );

  return `
    <div class="rdp-ops-shell">
      ${renderCommandBar({
        environment,
        health,
        operationsSummary,
        tasks,
        releaseCandidates,
        draftRecommendations,
        observationQueue,
        canAdmin,
      })}
      ${renderRecommendationStep({
        pendingRecommendations,
        recommendationHistory,
        activeParameters,
        governanceState,
        uiState,
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
