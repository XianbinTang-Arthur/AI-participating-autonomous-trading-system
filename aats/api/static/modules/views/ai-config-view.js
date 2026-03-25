import { actorTags, actionButton, callout, kvList, summaryStrip, surfaceCard } from "../components.js";
import { summarizeLocalizedList } from "../copy.js";
import { formatMaybeTimestamp, formatNumber } from "../formatters.js";
import { readableState } from "../terms.js";

const PROFILE_OPTIONS = [
  ["trend_aggressive", "趋势激进", "primary"],
  ["trend_normal", "趋势标准", "secondary"],
  ["trend_strict", "趋势严格", "secondary"],
  ["range_defensive", "震荡防御", "warning"],
  ["high_volatility_defensive", "高波动防御", "warning"],
  ["execution_degraded_safe", "执行降级安全", "warning"],
];

const MANUAL_MODE_OPTIONS = [
  ["baseline_only", "仅按基础策略运行", "warning"],
  ["ai_assisted", "AI 辅助决策", "secondary"],
  ["ai_decision_maker", "AI 决策者", "primary"],
];

export function renderAIConfigView(data) {
  const session = data.session || {};
  const summary = data.summary || {};
  const runtime = data.aiRuntime || summary.ai || {};
  const aiState = summary.ai || {};
  const runtimeProfiles = summary.runtime_profile || {};
  const strategyProfiles = summary.strategy_profile || {};
  const activation = strategyProfiles.activation || {};
  const activeRevision = strategyProfiles.active_revision || {};
  const latestSelectionDecision = strategyProfiles.latest_selection_decision || {};
  const latestOptimizationReport = strategyProfiles.latest_optimization_report || {};
  const latestProfileControl = aiState.latest_profile_control_decision || {};
  const uiState = data.uiState || {};
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const summaryError = data.error || null;

  if (summaryError) {
    return surfaceCard({
      title: "AI 配置暂时不可用",
      kicker: "读取失败",
      copy: "现在还拿不到配置摘要。",
      content: callout({
        title: "请先检查登录状态和后端接口",
        copy: summaryError,
        pills: [actorTags("system")],
      }),
    });
  }

  return `
    <div class="panel-grid ai-config-layout">
      <div class="span-6 workspace-stack">
        ${renderManualOperatingModePanel({ runtime, canAdmin, uiState })}
      </div>
      <div class="span-6 workspace-stack">
        ${renderProfileControlPanel({
          runtime,
          activeRevision,
          activation,
          latestProfileControl,
          latestSelectionDecision,
          latestOptimizationReport,
          canAdmin,
          uiState,
        })}
      </div>
      <div class="span-12 workspace-stack">
        ${renderCurrentConfigurationCard({ runtimeProfiles, runtime, aiState, activeRevision, activation })}
      </div>
    </div>
  `;
}

function renderManualOperatingModePanel({ runtime = {}, canAdmin = false, uiState = {} }) {
  const mode = currentOperatingMode(runtime);
  const summary = runtimeModeSummary(runtime);
  const manualEditing = Boolean(uiState.modeManualEditing || runtime.manual_override_active);
  const buttons = MANUAL_MODE_OPTIONS.map(([value, label, tone]) =>
    actionButton(
      value === mode ? `${label}（当前）` : label,
      "manual-set-ai-operating-mode",
      value,
      value === mode ? "primary" : tone,
      {
        disabled: !canAdmin || !manualEditing || (value === mode && runtime.manual_override_active),
        title: !canAdmin
          ? "当前账号只有查看权限"
          : !manualEditing
            ? "先切到手动模式，下面的按钮才能点击"
            : value === mode
              ? "保持当前模式，但切入手动接管"
              : `切换到${label}`,
      },
    ),
  ).join("");

  return surfaceCard({
    title: "运行模式切换",
    kicker: "人工入口",
    copy: "这里只决定 AI 是否参与最终交易决策。",
    actions: renderControlModeActions({
      canAdmin,
      manualEditing,
      manualAction: "set-ai-mode-editing",
      autoAction: "set-ai-mode-editing",
      manualLabel: "手动接管",
      autoLabel: "跟随配置",
      autoTitle: "恢复跟随配置",
    }),
    content: `
      ${callout({
        title: summary.title,
        copy: summary.copy,
        pills: [actorTags(...summary.actors)],
      })}
      ${summaryStrip([
        {
          label: "当前模式",
          value: readableMode(mode),
          meta: `默认模式：${readableMode(runtime.configured_operating_mode || "baseline_only")}`,
          tone: runtime.manual_override_active ? "warning" : "info",
          badge: actorTags(runtime.manual_override_active ? "admin" : "system"),
        },
        {
          label: "当前状态",
          value: runtime.manual_override_active ? "管理员已接管" : "跟随配置运行",
          meta: runtime.manual_override_active
            ? runtime.manual_override_freeze_until
              ? `恢复时间：${formatMaybeTimestamp(runtime.manual_override_freeze_until)}`
              : "会一直保持当前模式，直到手动恢复"
            : "现在没有管理员手动覆盖，系统按配置模式运行",
          tone: runtime.manual_override_active ? "warning" : "outline",
          badge: actorTags(runtime.manual_override_active ? "admin" : "system"),
        },
      ])}
      <div class="table-actions table-actions--compact manual-profile-switch-actions manual-profile-switch-actions--centered">
        ${buttons}
      </div>
    `,
  });
}

function renderProfileControlPanel({
  runtime = {},
  activeRevision = {},
  activation = {},
  latestProfileControl = {},
  latestSelectionDecision = {},
  latestOptimizationReport = {},
  canAdmin = false,
  uiState = {},
}) {
  const activeProfileId = currentStrategyProfile(activeRevision, activation);
  const summary = autoControlSummary(runtime, latestProfileControl, latestSelectionDecision, latestOptimizationReport);
  const controlSummary = latestOptimizationReport.control_summary || {};
  const adaptiveControls = controlSummary.adaptive_controls || {};
  const riskBudget = adaptiveControls.risk_budget || {};
  const executionAggressiveness = adaptiveControls.execution_aggressiveness || {};
  const configured = Boolean(runtime.strategy_profile_auto_control_configured);
  const manuallyPaused = runtime.strategy_profile_auto_control_reason === "manually_paused_by_admin";
  const manualEditing = Boolean(!configured || uiState.profileManualEditing || manuallyPaused);
  const candidateProfileId = latestOptimizationReport.recommended_profile_id || latestSelectionDecision.candidate_profile_id || "";
  const profileButtons = PROFILE_OPTIONS.map(([profileId, label, tone]) =>
    actionButton(
      profileId === activeProfileId ? `${label}（当前）` : label,
      "manual-activate-strategy-profile",
      profileId,
      profileId === activeProfileId ? "primary" : tone,
      {
        disabled: !canAdmin || !manualEditing || (profileId === activeProfileId && manuallyPaused),
        title: !canAdmin
          ? "当前账号只有查看权限"
          : !manualEditing
            ? "先切到手动模式，下面的按钮才能点击"
            : profileId === activeProfileId
              ? "保持当前档位，但切入手动接管"
              : `切换到${label}`,
      },
    ),
  ).join("");

  return surfaceCard({
    title: "自动换档控制",
    kicker: "独立功能",
    copy: "这里决定系统会不会自己换档。",
    actions: renderControlModeActions({
      canAdmin,
      manualEditing,
      manualAction: "set-profile-editing",
      autoAction: "set-profile-editing",
      manualLabel: "手动切档",
      autoLabel: "自动切档",
      manualTitle: !configured ? "当前配置只允许手动切换档位" : "",
      autoDisabled: !configured,
      autoTitle: !configured ? "恢复自动切档（当前没有启用自动换档）" : "恢复自动切档",
    }),
    content: `
      ${callout({
        title: summary.title,
        copy: summary.copy,
        pills: [actorTags(...summary.actors)],
      })}
      ${summaryStrip([
        {
          label: "自动换档",
          value: configured ? (runtime.strategy_profile_auto_control_effective ? "已启用" : "已暂停") : "未启用",
          meta: configured
            ? runtime.strategy_profile_auto_control_effective
              ? "系统会自己评估是否切换档位"
              : "现在不会自动改档"
            : "当前只允许手动切换档位",
          tone: runtime.strategy_profile_auto_control_effective ? "positive" : configured ? "warning" : "outline",
          badge: actorTags("system"),
        },
        {
          label: "当前档位",
          value: readableProfile(activeProfileId, "待确认"),
          meta: "策略档位切换",
          tone: "info",
          badge: actorTags(manuallyPaused ? "admin" : "system"),
        },
        {
          label: "紧急安全切档",
          value: latestSelectionDecision.fast_track_applied ? "已启用" : latestSelectionDecision.fast_track_eligible ? "条件已满足" : "未触发",
          meta: summarizeLocalizedList(latestSelectionDecision.gating_state?.fast_track_reasons, {
            fallback: "当前没有触发紧急安全快速通道。",
            limit: 3,
          }),
          tone: latestSelectionDecision.fast_track_applied ? "danger" : latestSelectionDecision.fast_track_eligible ? "warning" : "outline",
          badge: actorTags("risk_control"),
        },
      ])}
      <div class="table-actions table-actions--compact manual-profile-switch-actions manual-profile-switch-actions--centered">
        ${profileButtons}
      </div>
      ${kvList([
        [
          "候选策略档位",
          readableProfile(candidateProfileId, "当前没有新的候选策略档位"),
          summarizeList(latestSelectionDecision.blocked_reasons, "当前没有新的自动切档阻断原因。"),
        ],
        [
          "切换分类",
          readableState(latestSelectionDecision.transition_class || "unknown"),
          textOrFallback(latestSelectionDecision.operator_summary, "当前没有额外切换摘要。"),
        ],
        [
          "自动切档闸门",
          profileGateSummary(latestSelectionDecision),
          latestSelectionDecision.gating_state?.reconciliation_clean ? "当前对账状态干净，可以继续评估。" : "当前对账未完全干净，系统会更谨慎。",
        ],
        [
          "风险预算乘数",
          multiplierLabel(riskBudget.multiplier, riskBudget.status),
          summarizeAdaptiveReasons(riskBudget, "当前风险预算没有自动收缩。"),
        ],
        [
          "执行侵略性乘数",
          multiplierLabel(executionAggressiveness.multiplier, executionAggressiveness.status),
          summarizeAdaptiveReasons(executionAggressiveness, "当前执行侵略性没有自动收缩。"),
        ],
      ])}
    `,
  });
}

function renderControlModeActions({
  canAdmin = false,
  manualEditing = false,
  manualAction,
  autoAction,
  manualLabel = "手动模式",
  autoLabel = "自动模式",
  manualTitle = "",
  autoDisabled = false,
  autoTitle = "",
}) {
  return `
    <div class="table-actions table-actions--compact">
      ${actionButton(manualLabel, manualAction, "manual", manualEditing ? "primary" : "secondary", {
        disabled: !canAdmin,
        title: !canAdmin ? "当前账号只有查看权限" : manualTitle || "解锁下面的按钮，允许手动调整",
      })}
      ${actionButton(autoLabel, autoAction, "auto", !manualEditing ? "primary" : "secondary", {
        disabled: !canAdmin || autoDisabled,
        title: !canAdmin ? "当前账号只有查看权限" : autoTitle || "锁定下面的按钮，并恢复系统自动逻辑",
      })}
    </div>
  `;
}

function renderCurrentConfigurationCard({ runtimeProfiles = {}, runtime = {}, aiState = {}, activeRevision = {}, activation = {} }) {
  const runtimePayload = runtimeProfiles.current_runtime_payload || {};
  const strategyShadowEnabled = Boolean(aiState.shadow_mode_enabled ?? runtime.shadow_mode_enabled);
  const executionShadow = executionShadowState(
    runtime.execution_suggestion_mode
      || aiState.execution_suggestion_mode
      || runtime.configured_execution_suggestion_mode
      || "disabled",
  );

  return surfaceCard({
    title: "运行参数概览",
    kicker: "当前生效",
    copy: "这里只看真正会影响运行的状态。",
    content: `
      ${summaryStrip([
        {
          label: "运行模式",
          value: readableMode(runtime.effective_operating_mode || aiState.effective_operating_mode || "baseline_only"),
          meta: `默认模式：${readableMode(runtime.configured_operating_mode || aiState.configured_operating_mode || "baseline_only")}`,
          tone: runtime.manual_override_active ? "warning" : "info",
          badge: actorTags(runtime.manual_override_active ? "admin" : "system"),
        },
        {
          label: "策略档位",
          value: readableProfile(activeRevision.profile_id || activation.active_profile_id, "待确认"),
          meta: activation.active_profile_id ? "当前阈值由这套档位控制" : "当前没有活动档位",
          tone: activation.active_profile_id ? "positive" : "outline",
          badge: actorTags("system"),
        },
        {
          label: "策略层 shadow",
          value: strategyShadowEnabled ? "已开启" : "未开启",
          meta: strategyShadowEnabled ? "比较 AI 决策和基础策略" : "当前不记录策略层对照样本",
          tone: strategyShadowEnabled ? "info" : "outline",
          badge: actorTags("ai", "system"),
        },
        {
          label: "执行层 shadow",
          value: executionShadow.value,
          meta: executionShadow.meta,
          tone: executionShadow.tone,
          badge: actorTags("ai", "system"),
        },
      ])}
      ${kvList([
        [
          "主交易标的",
          textOrFallback(runtimePayload.default_symbol, "待配置"),
          listText(runtimePayload.allowed_symbols, "当前没有额外允许交易的标的"),
        ],
        [
          "产品与保证金",
          readableState(runtimePayload.trading_product_type || "unknown"),
          readableState(runtimePayload.margin_mode || "unknown"),
        ],
        [
          "默认下单量",
          formatNumber(runtimePayload.default_order_qty, 6, "待配置"),
          `单标的名义上限 ${formatNumber(runtimePayload.max_notional_per_symbol, 2, "待配置")}`,
        ],
        [
          "持有与冷却",
          `最小持仓 ${formatNumber(runtimePayload.strategy_min_hold_seconds, 0, "待配置")} 秒`,
          `平仓后冷却 ${formatNumber(runtimePayload.strategy_post_close_cooldown_seconds, 0, "待配置")} 秒`,
        ],
        [
          "低边际保护",
          `低边际阈值 ${formatNumber(runtimePayload.strategy_low_edge_threshold_bps, 1, "待配置")} bps / 连续 ${formatNumber(runtimePayload.strategy_low_edge_streak_limit, 0, "待配置")} 次`,
          `低边际冷却 ${formatNumber(runtimePayload.strategy_low_edge_cooldown_seconds, 0, "待配置")} 秒`,
        ],
      ])}
    `,
  });
}

function runtimeModeSummary(runtime = {}) {
  if (!runtime.manual_override_active) {
    return {
      title: `当前配置模式：${readableMode(runtime.configured_operating_mode || "baseline_only")}`,
      copy: "现在没有管理员手动接管，系统按配置模式运行。",
      actors: ["system"],
    };
  }
  const activeMode = readableMode(runtime.manual_override_mode || runtime.effective_operating_mode || "baseline_only");
  return {
    title: `管理员已切到${activeMode}`,
    copy: runtime.manual_override_freeze_until
      ? `系统会在 ${formatMaybeTimestamp(runtime.manual_override_freeze_until)} 后恢复自动逻辑。`
      : "会一直保持当前模式，直到你点击自动模式。",
    actors: ["admin"],
  };
}

function autoControlSummary(runtime = {}, latestProfileControl = {}, latestSelectionDecision = {}, latestOptimizationReport = {}) {
  const configured = Boolean(runtime.strategy_profile_auto_control_configured);
  const enabled = Boolean(runtime.strategy_profile_auto_control_effective);
  const manuallyPaused = runtime.strategy_profile_auto_control_reason === "manually_paused_by_admin";
  const candidate = latestOptimizationReport.recommended_profile_id || latestSelectionDecision.candidate_profile_id || "";

  if (!configured) {
    return {
      title: "自动换档未启用",
      copy: "系统现在不会自己改档。",
      tone: "outline",
      actors: ["system"],
    };
  }
  if (manuallyPaused) {
    return {
      title: "自动换档已暂停",
      copy: "恢复自动切档前，系统会保持当前手动档位。",
      tone: "warning",
      actors: ["admin", "system"],
    };
  }
  if (latestProfileControl.applied) {
    return {
      title: `本轮已切到${readableProfile(latestProfileControl.requested_profile_id)}`,
      copy: "系统已经完成这一轮自动换档。",
      tone: "positive",
      actors: ["system", "ai"],
    };
  }
  if (candidate) {
    return {
      title: `正在观察${readableProfile(candidate)}`,
      copy: summarizeList(latestSelectionDecision.blocked_reasons, "系统还在比较证据"),
      tone: "info",
      actors: ["system", "ai"],
    };
  }
  if (enabled) {
    return {
      title: "自动换档已启用",
      copy: "本轮没有新的切档动作。",
      tone: "info",
      actors: ["system"],
    };
  }
  return {
    title: "自动换档已暂停",
    copy: "系统继续保持当前档位。",
    tone: "warning",
    actors: ["system"],
  };
}

function executionShadowState(mode) {
  const normalized = String(mode || "disabled").trim().toLowerCase();
  if (normalized === "enabled_live") {
    return {
      value: "已进入受限实盘",
      meta: "AI 执行建议会在保护边界内参与真实执行",
      tone: "warning",
    };
  }
  if (normalized === "shadow_translation") {
    return {
      value: "已开启预演",
      meta: "会预演 AI 的执行建议，但不会直接改真实委托",
      tone: "info",
    };
  }
  if (normalized === "diagnostic_only") {
    return {
      value: "只做记录",
      meta: "只记录 AI 执行建议，不影响下单",
      tone: "outline",
    };
  }
  return {
    value: "已关闭",
    meta: "当前不启用执行层 shadow",
    tone: "outline",
  };
}

function currentOperatingMode(runtime = {}) {
  const normalized = String(
    runtime.manual_override_mode
      || runtime.effective_operating_mode
      || runtime.configured_operating_mode
      || "baseline_only",
  ).trim();
  if (normalized === "ai_decision_maker_with_profile_control") return "ai_decision_maker";
  return normalized;
}

function currentStrategyProfile(activeRevision = {}, activation = {}) {
  return String(activeRevision.profile_id || activation.active_profile_id || "").trim();
}

function readableProfile(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return readableState(String(value), fallback);
}

function readableMode(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  const normalized = String(value).trim();
  if (normalized === "ai_decision_maker_with_profile_control") {
    return "AI 决策者";
  }
  return readableState(normalized, fallback);
}

function textOrFallback(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function listText(value, fallback = "暂无") {
  if (!Array.isArray(value) || !value.length) return fallback;
  return value.join("、");
}

function summarizeList(items, fallback = "当前没有额外说明") {
  if (!Array.isArray(items) || !items.length) return fallback;
  return items.slice(0, 2).map((item) => readableState(String(item), String(item))).join("；");
}

function profileGateSummary(selection = {}) {
  const gating = selection.gating_state || {};
  const parts = [];
  if (gating.confidence_floor !== null && gating.confidence_floor !== undefined) {
    parts.push(`最低置信度 ${formatNumber(gating.confidence_floor, 2, "待确认")}`);
  }
  if (gating.next_eligible_switch_at) {
    parts.push(`最早可切换时间 ${formatMaybeTimestamp(gating.next_eligible_switch_at)}`);
  }
  if ((gating.remaining_closed_trades || 0) > 0 || (gating.remaining_replay_validations || 0) > 0) {
    parts.push(
      `还差 ${formatNumber(gating.remaining_closed_trades, 0, "0")} 笔已平仓交易、${formatNumber(gating.remaining_replay_validations, 0, "0")} 次 replay`,
    );
  }
  if ((gating.remaining_consecutive_wins || 0) > 0) {
    parts.push(`还差 ${formatNumber(gating.remaining_consecutive_wins, 0, "0")} 次连续胜出`);
  }
  return parts.join("；") || "当前没有额外闸门说明。";
}

function multiplierLabel(multiplier, status) {
  return `${formatNumber(multiplier, 2, "待确认")}（${readableState(status || "unknown")}）`;
}

function summarizeAdaptiveReasons(state = {}, fallback = "当前没有额外说明。") {
  const localizedReasons = summarizeLocalizedList(state.reasons, {
    fallback,
    limit: 2,
  });
  if (state.multiplier === null || state.multiplier === undefined) {
    return localizedReasons;
  }
  return `当前乘数 ${formatNumber(state.multiplier, 2, "待确认")}，${localizedReasons}`;
}
