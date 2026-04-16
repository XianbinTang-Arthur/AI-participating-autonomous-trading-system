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
  rolled_back: "已回滚",
};

const CHECK_LABELS = {
  "governance_db:connection": "治理数据库连接",
  "task_queue:queue_state": "任务队列状态",
  "runtime:rdp-daemon": "RDP 守护进程",
  "alerts:current_alerts": "当前可靠性告警",
  "workflow_runs:freshness": "工作流新鲜度",
  "live_db:readonly_access": "生产库只读链路",
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

function labelForConfidence(confidence) {
  return CONFIDENCE_LABELS[confidence] || confidence || "未知";
}

function labelForObservationStatus(status) {
  return OBSERVATION_STATUS_LABELS[status] || status || "未知";
}

function labelForApplyResult(status) {
  return APPLY_RESULT_LABELS[status] || status || "未执行";
}

function workflowStatus(taskInfo) {
  if (!taskInfo) {
    return { value: "未运行", meta: "还没有相关执行记录", tone: "outline" };
  }
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
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function comboKey(family, timeframe) {
  if (!family || !timeframe) return "";
  return `${family}_${String(timeframe).toLowerCase()}`;
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

function governanceStateByCombo(governanceState = {}) {
  const byCombo = new Map();
  (governanceState.combo_states || []).forEach((item) => {
    if (item?.combo_key) byCombo.set(item.combo_key, item);
  });
  return byCombo;
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

function buildCurrentCandidateEntries({
  pendingRecommendations = [],
  governanceState = {},
}) {
  const comboStates = governanceStateByCombo(governanceState);
  const parameterDrafts = latestRecommendationsByCombo(
    pendingRecommendations.filter((item) =>
      item?.recommendation_type === "parameter_upgrade" && item?.status === "draft"
    ),
  );
  const governanceDrafts = latestRecommendationsByCombo(
    pendingRecommendations.filter((item) =>
      item?.recommendation_type !== "parameter_upgrade" && item?.status === "draft"
    ),
  );

  const comboKeys = new Set([
    ...comboStates.keys(),
    ...parameterDrafts.keys(),
    ...governanceDrafts.keys(),
  ]);

  return Array.from(comboKeys)
    .map((key) => {
      const comboState = comboStates.get(key) || {};
      const parameterRecommendation = parameterDrafts.get(key) || null;
      const governanceRecommendation = governanceDrafts.get(key) || null;
      const createdAt = firstNonEmpty(
        governanceRecommendation?.created_at,
        parameterRecommendation?.created_at,
        comboState.latest_recommendation_created_at,
      );
      return {
        combo_key: key,
        family: comboState.family || parameterRecommendation?.family || governanceRecommendation?.family,
        timeframe: comboState.timeframe || parameterRecommendation?.timeframe || governanceRecommendation?.timeframe,
        symbol: parameterRecommendation?.symbol || governanceRecommendation?.symbol || "BTC-USDT-SWAP",
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
    })
    .sort((left, right) => {
      const leftAt = Date.parse(left.created_at || "") || 0;
      const rightAt = Date.parse(right.created_at || "") || 0;
      return rightAt - leftAt;
    });
}

function checkLabel(check) {
  const key = `${check?.category || ""}:${check?.name || ""}`;
  return CHECK_LABELS[key] || check?.name || "检查项";
}

function translateCheckDetail(check = {}, environment = {}) {
  const raw = firstNonEmpty(check.detail, "当前没有额外说明");
  const strictEnvironment = Boolean(environment.strict_environment);
  const key = `${check.category || ""}:${check.name || ""}`;
  const isOk = check.status === "ok";

  if (key === "governance_db:connection") {
    if (isOk) return { summary: "治理数据库连接正常，发布链路可用。", nextStep: "", raw };
    return {
      summary: strictEnvironment
        ? "治理数据库还没有接通，发布链路现在不可用。"
        : "治理数据库暂时不可达，当前只能查看已有产物，不能可靠推进发布。",
      nextStep: "检查容器内治理库连接配置是否指向 postgres 服务，而不是 127.0.0.1。",
      raw,
    };
  }
  if (key === "runtime:rdp-daemon") {
    if (isOk) return { summary: "RDP 守护进程运行正常。", nextStep: "", raw };
    return {
      summary: raw.includes("heartbeat not found")
        ? "还没有看到 RDP 守护进程的有效心跳。"
        : "RDP 守护进程状态不稳定，后台任务链路暂时不可信。",
      nextStep: "确认 rdp-daemon 已启动，并持续写入运行状态。",
      raw,
    };
  }
  if (key === "alerts:current_alerts") {
    if (isOk) return { summary: "当前没有可靠性告警。", nextStep: "", raw };
    return {
      summary: raw.includes("not found")
        ? "当前还没有最新的可靠性告警快照。"
        : "当前存在未处理的可靠性告警，先处理再继续推进。",
      nextStep: raw.includes("not found") ? "先运行一次刷新数据。" : "先打开告警详情确认阻断是否已处理。",
      raw,
    };
  }
  if (key === "workflow_runs:freshness") {
    if (isOk) return { summary: "最近一轮工作流结果仍然新鲜。", nextStep: "", raw };
    return {
      summary: raw.includes("missing")
        ? "研究或决策工作流还没有形成完整快照。"
        : "工作流结果已经过旧，不适合直接拿来推进发布。",
      nextStep: "先刷新数据并重新运行研究。",
      raw,
    };
  }
  if (key === "live_db:readonly_access") {
    if (isOk) return { summary: "生产库只读链路可用。", nextStep: "", raw };
    return {
      summary: raw.includes("RDP_LIVE_DATABASE_URL")
        ? "还没有配置生产库只读连接，发布前缺少关键校验。"
        : "生产库只读链路不可用，发布前检查缺少关键校验。",
      nextStep: raw.includes("RDP_LIVE_DATABASE_URL")
        ? "补齐生产库只读连接配置。"
        : "检查只读数据库连接和权限配置。",
      raw,
    };
  }
  if (key === "parameters:active_parameter_sets") {
    if (isOk) return { summary: "已经读取到当前 active 参数集。", nextStep: "", raw };
    return {
      summary: raw === "count=0" ? "当前还没有 active 参数在运行。" : raw,
      nextStep: raw === "count=0" ? "如果不是首次接入，先确认发布链是否已经跑通。" : "",
      raw,
    };
  }
  if (check.category === "artifacts") {
    if (isOk) return { summary: "相关产物可用。", nextStep: "", raw };
    return { summary: "对应数据文件缺失，相关功能可能不可用。", nextStep: "", raw };
  }
  if (key === "task_queue:queue_state") {
    if (isOk) return { summary: "任务队列状态正常。", nextStep: "", raw };
    return {
      summary: "任务队列存在积压或失败任务。",
      nextStep: "查看任务列表，确认是否需要重试。",
      raw,
    };
  }
  return { summary: raw, nextStep: "", raw };
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
  const rollbackItem = observationQueue.find((item) => item?.observation_status === "rollback_recommended");
  const activeObservation = observationQueue.find((item) => item?.observation_status === "observing");
  const firstReleaseCandidate = releaseCandidates[0] || null;
  const defaultObservationWindowHours = environment.required_observation_window_hours || 24;

  let headline = "当前没有待处理阻断，按需刷新数据或运行研究。";
  let summary = "这个面板只保留当前能推进闭环的核心状态和动作。";
  let tone = "neutral";

  if (rollbackItem) {
    headline = "有发布进入回滚建议状态，优先处理回滚。";
    summary = "先把当前 release 的风险收口，再继续推进新的参数候选。";
    tone = "warning";
  } else if (environment.name === "prod" && !environment.production_apply_enabled) {
    headline = "当前处于生产冻结状态，禁止直接推进参数进入生产。";
    summary = "研究和审批可以继续做，但真正生效仍要通过受控发布链。";
    tone = "danger";
  } else if (health.overall_health === "blocked") {
    headline = "当前存在阻断项，先处理阻断再推进。";
    summary = "页面下方只保留会真正卡住流程的关键原因。";
    tone = "danger";
  } else if (firstReleaseCandidate) {
    headline = "有已批准参数，下一步是创建发布。";
    summary = `发布会按当前环境默认的 ${defaultObservationWindowHours} 小时观察窗口推进。`;
    tone = "positive";
  } else if (draftRecommendations.length) {
    headline = "有待审批建议，先处理当前轮次。";
    summary = "这里不再展开历史 recommendation，只保留最新待处理项。";
    tone = "warning";
  } else if (activeObservation) {
    headline = "当前有发布仍在观察期。";
    summary = "优先确认观察结论，再决定是否继续推进下一轮。";
    tone = "info";
  }

  let primaryAction = "";
  if (rollbackItem) {
    primaryAction = buildRollbackAction(rollbackItem, canAdmin);
  } else if (firstReleaseCandidate) {
    primaryAction = actionButton("创建发布", "rdp-create-release", firstReleaseCandidate.recommendation_id, "primary", {
      disabled: !canAdmin || health.overall_health === "blocked",
      title: !canAdmin
        ? "当前账号只有查看权限"
        : health.overall_health === "blocked"
          ? "先处理阻断项，再创建发布"
          : `按默认 ${defaultObservationWindowHours} 小时观察窗口创建 release`,
    });
  } else if (activeObservation) {
    primaryAction = buildObservationAction(activeObservation, canAdmin, "primary");
  }

  const actions = `
    <div class="rdp-command-actions">
      ${primaryAction}
      ${actionButton(
        maintenanceBusy ? "数据刷新进行中" : "刷新数据",
        "rdp-trigger-workflow",
        "data_maintenance",
        "secondary",
        {
          disabled: !canAdmin || maintenanceBusy,
          title: !canAdmin ? "当前账号只有查看权限" : "刷新近期数据和基础产物",
        },
      )}
      ${actionButton(
        researchBusy ? "研究进行中" : "运行研究",
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
    eyebrow: "RDP 核心面板",
    title: "只保留当前能推进的工作",
    headline,
    summary,
    tone,
    actions,
    pills: [
      actorTags("system"),
      `<span class="signal-pill tone-outline">环境 ${escapeHtml(labelForEnvironment(environment.name))}</span>`,
      `<span class="signal-pill tone-${escapeHtml(toneForHealth(health.overall_health))}">健康 ${escapeHtml(labelForHealth(health.overall_health))}</span>`,
      `<span class="signal-pill tone-${escapeHtml(toneForGate(operationsSummary.latest_gate_status))}">最新 Gate ${escapeHtml(labelForGate(operationsSummary.latest_gate_status))}</span>`,
    ],
    metrics: [
      {
        label: "数据刷新",
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
        meta: draftRecommendations.length ? "优先看当前轮次" : "当前没有待审批建议",
        tone: draftRecommendations.length ? "warning" : "outline",
        badge: actorTags("operator"),
      },
      {
        label: "观察中发布",
        value: `${operationsSummary.observing_release_count || 0} 条`,
        meta: operationsSummary.rollback_recommended_count
          ? `其中 ${operationsSummary.rollback_recommended_count} 条建议回滚`
          : "当前没有回滚建议",
        tone: operationsSummary.rollback_recommended_count ? "danger" : "info",
        badge: actorTags("system"),
      },
    ],
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

function renderCoreQueue({
  pendingRecommendations = [],
  activeParameters = {},
  governanceState = {},
  recentGateResults = [],
  observationQueue = [],
  environment = {},
  canAdmin = false,
}) {
  const appliedRecommendationIds = buildAppliedRecommendationIds(activeParameters);
  const comboStates = governanceStateByCombo(governanceState);
  const gateByRecommendation = latestGateByRecommendation(recentGateResults);
  const candidateEntries = buildCurrentCandidateEntries({
    pendingRecommendations,
    governanceState,
  });

  const cards = [];
  const parameterDraftComboKeys = new Set(
    candidateEntries
      .filter((entry) => entry.parameter_recommendation?.status === "draft")
      .map((entry) => entry.combo_key),
  );

  cards.push(
    ...observationQueue
      .filter((item) => item?.observation_status === "rollback_recommended")
      .map((item) => buildObservationCard(item, canAdmin)),
  );

  cards.push(
    ...sortRecommendationsByCreatedAt(
      pendingRecommendations.filter((item) =>
        item?.recommendation_type === "parameter_upgrade"
        && item?.status === "approved"
        && !appliedRecommendationIds.has(item.recommendation_id)
      ),
    ).map((item) => {
      const gate = gateByRecommendation.get(item.recommendation_id);
      const comboState = comboStates.get(comboKey(item.family, item.timeframe)) || {};
      const targetParameterSetId = firstNonEmpty(
        item.target_parameter_set_id,
        comboState.candidate_parameter_set_id,
      );
      return renderWorkItem({
        tone: toneForGate(gate?.gate_status) === "danger" ? "danger" : "positive",
        kicker: `${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
        title: `待发布 ${shortId(item.recommendation_id)}`,
        pills: [
          `<span class="signal-pill tone-info">已批准</span>`,
          `<span class="signal-pill tone-${escapeHtml(toneForGate(gate?.gate_status))}">Gate ${escapeHtml(labelForGate(gate?.gate_status))}</span>`,
        ],
        body: `
          <p class="meta-copy">目标参数集 ${escapeHtml(shortId(targetParameterSetId))}</p>
          <p class="meta-copy">${escapeHtml(firstNonEmpty(item.reason, "建议已经批准，可以推进发布。"))}</p>
          <p class="meta-copy">观察窗口 ${escapeHtml(String(environment.required_observation_window_hours || 24))} 小时（按当前环境默认值）</p>
        `,
        meta: [
          item.created_at ? `创建于 ${relativeTime(item.created_at)}` : "",
          gate?.created_at ? `最近 Gate ${relativeTime(gate.created_at)}` : "建议先运行 Gate",
        ],
        actions: [
          actionButton("运行 Gate", "rdp-run-gate", item.recommendation_id, "secondary", {
            disabled: !canAdmin,
            title: !canAdmin ? "当前账号只有查看权限" : "先验证这条建议当前是否允许发布",
          }),
          actionButton("创建发布", "rdp-create-release", item.recommendation_id, "primary", {
            disabled: !canAdmin,
            title: !canAdmin ? "当前账号只有查看权限" : "统一通过 release 流程让参数进入运行态",
          }),
        ].join(""),
      });
    }),
  );

  cards.push(
    ...candidateEntries
      .filter((entry) => entry.parameter_recommendation?.status === "draft")
      .map((entry) => {
        const parameterRecommendation = entry.parameter_recommendation || {};
        const governanceRecommendation = entry.governance_recommendation || {};
        const blockedByGovernanceDraft = Boolean(
          governanceRecommendation.recommendation_id
          && governanceRecommendation.status === "draft"
          && governanceRecommendation.recommendation_type !== "parameter_upgrade",
        );
        const candidateSetId = firstNonEmpty(
          entry.candidate_parameter_set_id,
          parameterRecommendation.target_parameter_set_id,
        );
        const candidateReason = firstNonEmpty(
          parameterRecommendation.reason,
          (entry.latest_round_reasons || []).join("；"),
          "这组参数还没有补充说明。",
        );
        const governanceReason = firstNonEmpty(governanceRecommendation.reason, "");
        const reviewRecommendationId = blockedByGovernanceDraft
          ? governanceRecommendation.recommendation_id
          : parameterRecommendation.recommendation_id;
        const reviewApproveLabel = blockedByGovernanceDraft ? "审批治理" : "审批";
        const reviewRejectLabel = blockedByGovernanceDraft ? "拒绝治理" : "拒绝";
        const reviewTitle = blockedByGovernanceDraft
          ? `待处理治理 ${labelForRecommendationType(governanceRecommendation.recommendation_type)}`
          : `参数候选 ${shortId(candidateSetId || parameterRecommendation.recommendation_id)}`;

        return renderWorkItem({
          tone: blockedByGovernanceDraft ? "warning" : toneForRecommendationStatus(parameterRecommendation.status),
          kicker: `${entry.family || "未知策略"} / ${entry.timeframe || "未知周期"}`,
          title: reviewTitle,
          pills: [
            parameterRecommendation.confidence
              ? `<span class="signal-pill tone-${escapeHtml(toneForConfidence(parameterRecommendation.confidence))}">置信度 ${escapeHtml(labelForConfidence(parameterRecommendation.confidence))}</span>`
              : "",
            `<span class="signal-pill tone-warning">待审批</span>`,
            governanceRecommendation.recommendation_id
              ? `<span class="signal-pill tone-${escapeHtml(toneForRecommendationStatus(governanceRecommendation.status))}">治理建议 ${escapeHtml(labelForRecommendationType(governanceRecommendation.recommendation_type))}</span>`
              : "",
          ].filter(Boolean),
          body: `
            <p class="meta-copy">本轮生成参数集 ${escapeHtml(shortId(candidateSetId))}</p>
            <p class="meta-copy">候选依据：${escapeHtml(candidateReason)}</p>
            ${governanceRecommendation.recommendation_id
              ? `<p class="meta-copy">当前治理建议：${escapeHtml(labelForRecommendationType(governanceRecommendation.recommendation_type))}（${escapeHtml(labelForRecommendationStatus(governanceRecommendation.status))}）。${governanceReason ? ` 原因：${escapeHtml(governanceReason)}` : ""}</p>`
              : ""}
          `,
          meta: [
            entry.created_at ? `生成于 ${relativeTime(entry.created_at)}` : "",
            blockedByGovernanceDraft ? "同组合还有待处理治理建议，先处理治理结论" : "这里只做审批，不会直接生效",
          ],
          actions: [
            actionButton(reviewApproveLabel, "rdp-approve-only", reviewRecommendationId, "primary", {
              disabled: !canAdmin,
              title: !canAdmin
                ? "当前账号只有查看权限"
                : blockedByGovernanceDraft
                  ? "先确认当前治理结论"
                  : "确认这组参数候选",
            }),
            actionButton(reviewRejectLabel, "rdp-reject-recommendation", reviewRecommendationId, "ghost", {
              disabled: !canAdmin,
              title: !canAdmin
                ? "当前账号只有查看权限"
                : blockedByGovernanceDraft
                  ? "拒绝当前治理结论"
                  : "拒绝这条参数候选",
            }),
          ].join(""),
        });
      }),
  );

  cards.push(
    ...Array.from(
      latestRecommendationsByCombo(
        pendingRecommendations.filter((item) =>
          item?.recommendation_type !== "parameter_upgrade"
          && item?.status === "draft"
          && !parameterDraftComboKeys.has(comboKey(item.family, item.timeframe))
        ),
      ).values(),
    ).map((item) => renderWorkItem({
      tone: toneForRecommendationStatus(item.status),
      kicker: `${item.family || "未知策略"} / ${item.timeframe || "未知周期"}`,
      title: `治理建议 ${labelForRecommendationType(item.recommendation_type)}`,
      pills: [
        `<span class="signal-pill tone-${escapeHtml(toneForRecommendationStatus(item.status))}">${escapeHtml(labelForRecommendationStatus(item.status))}</span>`,
        item.confidence
          ? `<span class="signal-pill tone-${escapeHtml(toneForConfidence(item.confidence))}">置信度 ${escapeHtml(labelForConfidence(item.confidence))}</span>`
          : "",
      ].filter(Boolean),
      body: `<p class="meta-copy">${escapeHtml(firstNonEmpty(item.reason, "这条治理建议还没有补充说明。"))}</p>`,
      meta: [item.created_at ? `创建于 ${relativeTime(item.created_at)}` : ""],
      actions: [
        actionButton("审批", "rdp-approve-only", item.recommendation_id, "secondary", {
          disabled: !canAdmin,
          title: !canAdmin ? "当前账号只有查看权限" : "确认这条治理建议",
        }),
        actionButton("拒绝", "rdp-reject-recommendation", item.recommendation_id, "ghost", {
          disabled: !canAdmin,
          title: !canAdmin ? "当前账号只有查看权限" : "拒绝这条治理建议",
        }),
      ].join(""),
    })),
  );

  cards.push(
    ...observationQueue
      .filter((item) => item?.observation_status === "observing")
      .map((item) => buildObservationCard(item, canAdmin)),
  );

  const visibleCards = cards.slice(0, 6);

  return surfaceCard({
    title: "当前待处理",
    kicker: "只看当前轮次",
    copy: "这里只保留审批、发布和观察中的核心事项。",
    content: visibleCards.length
      ? `<div class="rdp-worklist">${visibleCards.join("")}</div>`
      : notice("当前没有待处理事项。", "info"),
  });
}

function buildIssueItems({
  environment = {},
  health = {},
  recentGateResults = [],
}) {
  const latestGate = recentGateResults[0] || null;
  const items = [];

  (health.blocking_reasons || []).forEach((reason) => {
    items.push({
      tone: "danger",
      kicker: "系统阻断",
      title: "先处理健康阻断",
      body: `<p class="meta-copy">${escapeHtml(reason)}</p>`,
      meta: ["RDP 健康状态为阻断"],
    });
  });

  (latestGate?.blocking_reasons || []).forEach((reason) => {
    items.push({
      tone: "danger",
      kicker: "最新 Gate",
      title: "Gate 返回阻断",
      body: `<p class="meta-copy">${escapeHtml(reason)}</p>`,
      meta: [latestGate?.created_at ? `运行于 ${relativeTime(latestGate.created_at)}` : ""],
    });
  });

  (health.checks || [])
    .filter((check) => ["blocked", "warn"].includes(String(check?.status || "").toLowerCase()))
    .forEach((check) => {
      const detail = translateCheckDetail(check, environment);
      items.push({
        tone: check.status === "blocked" ? "danger" : "warning",
        kicker: check.status === "blocked" ? "阻断检查" : "风险检查",
        title: checkLabel(check),
        body: `<p class="meta-copy">${escapeHtml(detail.summary)}</p>`,
        meta: [detail.nextStep ? `下一步：${detail.nextStep}` : ""],
      });
    });

  (health.warnings || []).forEach((reason) => {
    items.push({
      tone: "warning",
      kicker: "系统警告",
      title: "当前还有警告项",
      body: `<p class="meta-copy">${escapeHtml(reason)}</p>`,
      meta: [],
    });
  });

  (latestGate?.warnings || []).forEach((reason) => {
    items.push({
      tone: "warning",
      kicker: "最新 Gate",
      title: "Gate 返回警告",
      body: `<p class="meta-copy">${escapeHtml(reason)}</p>`,
      meta: [latestGate?.created_at ? `运行于 ${relativeTime(latestGate.created_at)}` : ""],
    });
  });

  const deduped = [];
  const seen = new Set();
  items.forEach((item) => {
    const key = `${item.title}|${item.body}`;
    if (!seen.has(key)) {
      seen.add(key);
      deduped.push(item);
    }
  });

  return { latestGate, items: deduped.slice(0, 5) };
}

function renderBlockers({
  environment = {},
  health = {},
  recentGateResults = [],
}) {
  const { latestGate, items } = buildIssueItems({
    environment,
    health,
    recentGateResults,
  });

  const latestGateSummary = latestGate
    ? callout({
      title: `最新 Gate：${labelForGate(latestGate.gate_status)}`,
      copy: firstNonEmpty(
        latestGate.blocking_reasons?.[0],
        latestGate.warnings?.[0],
        "最近一次 Gate 没有返回额外提示。",
      ),
      pills: [
        actorTags("system"),
        `<span class="signal-pill tone-${escapeHtml(toneForGate(latestGate.gate_status))}">${escapeHtml(labelForGate(latestGate.gate_status))}</span>`,
      ],
    })
    : notice("当前还没有最新 Gate 结果。", "outline");

  return surfaceCard({
    title: "当前阻断",
    kicker: "只保留关键问题",
    copy: "不再展开完整检查清单，只显示会真正卡住当前流程的原因。",
    content: `
      ${summaryStrip([
        {
          label: "当前环境",
          value: labelForEnvironment(environment.name),
          meta: environment.require_gate_pass ? "需要通过 Gate" : "当前环境不强制 Gate",
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
          label: "观察窗口",
          value: `${environment.required_observation_window_hours || 24} 小时`,
          meta: environment.name === "prod" ? "生产环境最短观察窗口" : "当前环境默认观察窗口",
          tone: "info",
          badge: actorTags("config"),
        },
      ])}
      ${latestGateSummary}
      ${items.length
        ? `<div class="rdp-worklist">${items.map((item) => renderWorkItem(item)).join("")}</div>`
        : notice("当前没有额外阻断项。", "info")}
    `,
  });
}

export function renderRdpControlPanelV2({ rdpControl = {}, canAdmin = false, uiState = {} }) {
  void uiState;

  const tasks = rdpControl.tasks || {};
  const pendingRecommendations = rdpControl.pending_recommendations || [];
  const activeParameters = rdpControl.active_parameters || {};
  const governanceState = rdpControl.governance_state || {};
  const health = rdpControl.health || {};
  const environment = rdpControl.environment || {};
  const operationsSummary = rdpControl.operations_summary || {};
  const recentGateResults = rdpControl.recent_gate_results || [];
  const observationQueue = rdpControl.observation_queue || [];

  const appliedRecommendationIds = buildAppliedRecommendationIds(activeParameters);
  const releaseCandidates = pendingRecommendations.filter((item) =>
    item.recommendation_type === "parameter_upgrade"
    && item.status === "approved"
    && !appliedRecommendationIds.has(item.recommendation_id),
  );
  const draftRecommendations = pendingRecommendations.filter((item) => item.status === "draft");

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
      ${renderCoreQueue({
        pendingRecommendations,
        activeParameters,
        governanceState,
        recentGateResults,
        observationQueue,
        environment,
        canAdmin,
      })}
      ${renderBlockers({
        environment,
        health,
        recentGateResults,
      })}
    </div>
  `;
}
