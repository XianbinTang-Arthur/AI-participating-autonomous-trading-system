import { actorTags, actionButton, callout, kvList, summaryStrip, surfaceCard } from "../components.js";
import { localizeList, summarizeLocalizedList, textOrFallback } from "../copy.js";
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
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const summaryError = data.error || null;

  if (summaryError) {
    return surfaceCard({
      title: "AI 配置暂时不可用",
      kicker: "读取失败",
      copy: "当前无法读取 AI 配置摘要，先确认登录状态、后端接口和管理员权限是否正常。",
      content: callout({
        title: "配置摘要读取失败",
        copy: summaryError,
        pills: [actorTags("system")],
      }),
    });
  }

  return `
    <div class="panel-grid ai-config-layout">
      <div class="span-6 workspace-stack">
        ${renderManualOperatingModePanel({ runtime, canAdmin })}
        ${renderAutoProfileControlPanel({
          runtime,
          latestProfileControl,
          latestSelectionDecision,
          latestOptimizationReport,
          canAdmin,
        })}
        ${renderManualProfilePanel({ activeRevision, activation, canAdmin })}
      </div>
      <div class="span-6 workspace-stack">
        ${renderRuntimeParameterCard({
          runtimeProfiles,
          runtime,
          aiState,
          activeRevision,
          activation,
          latestSelectionDecision,
          latestOptimizationReport,
        })}
      </div>
    </div>
  `;
}

function renderManualOperatingModePanel({ runtime = {}, canAdmin = false }) {
  const mode = currentOperatingMode(runtime);
  const summary = runtimeModeSummary(runtime);
  const buttons = MANUAL_MODE_OPTIONS.map(([value, label, tone]) =>
    actionButton(
      value === mode ? `${label}（当前）` : label,
      "manual-set-ai-operating-mode",
      value,
      value === mode ? "primary" : tone,
      {
        disabled: !canAdmin || value === mode,
        title: !canAdmin
          ? "当前账号只有查看权限，不能手动切换运行模式。"
          : value === mode
            ? "当前运行模式已经生效，无需重复切换。"
            : `切换到 ${label}`,
      },
    ),
  ).join("");

  const restoreButton = actionButton(
    "恢复自动模式",
    "restore-ai-operating-mode-auto",
    "",
    "secondary",
    {
      disabled: !canAdmin || !runtime.manual_override_active,
      title: !canAdmin
        ? "当前账号只有查看权限，不能恢复自动模式。"
        : runtime.manual_override_active
          ? "提前结束管理员手动覆盖，恢复系统自动运行模式逻辑。"
          : "当前没有管理员手动覆盖中的运行模式。",
    },
  );

  return surfaceCard({
    title: "运行模式切换",
    kicker: "人工入口",
    copy: "这里控制 AI 是否参与最终决策，以及参与到什么程度。",
    content: `
      ${callout({
        title: summary.value,
        copy: summary.copy,
        pills: [actorTags(...summary.actors)],
      })}
      ${summaryStrip([
        {
          label: "当前运行模式",
          value: readableMode(mode),
          meta: `默认运行模式 ${readableMode(runtime.configured_operating_mode || "baseline_only")}`,
          tone: runtime.manual_override_active ? "warning" : "info",
          badge: actorTags(runtime.manual_override_active ? "admin" : "system"),
        },
        {
          label: "人工覆盖状态",
          value: runtime.manual_override_active ? "管理员正在接管" : "当前未手动覆盖",
          meta: runtime.manual_override_active
            ? runtime.manual_override_freeze_until
              ? `恢复系统自动逻辑时间：${formatMaybeTimestamp(runtime.manual_override_freeze_until)}`
              : "当前会一直保持手动模式，直到管理员恢复自动模式。"
            : "手动切换后会一直保持当前模式，直到管理员恢复自动模式。",
          tone: runtime.manual_override_active ? "warning" : "outline",
          badge: actorTags(runtime.manual_override_active ? "admin" : "system"),
        },
      ])}
      ${kvList([
        [
          "这个卡片负责什么",
          "只控制 AI 最终决策模式",
          "你可以在基础策略、AI 辅助决策和 AI 决策者之间切换，这里不负责换策略档位。",
        ],
        [
          "如何理解当前状态",
          summary.value,
          summary.meta,
        ],
      ])}
      <div class="table-actions table-actions--compact manual-profile-switch-actions manual-profile-switch-actions--centered">
        ${buttons}
        ${restoreButton}
      </div>
    `,
  });
}

function renderAutoProfileControlPanel({
  runtime = {},
  latestProfileControl = {},
  latestSelectionDecision = {},
  latestOptimizationReport = {},
  canAdmin = false,
}) {
  const summary = autoControlSummary(runtime, latestProfileControl, latestSelectionDecision, latestOptimizationReport);
  const configured = Boolean(runtime.strategy_profile_auto_control_configured);
  const enabled = Boolean(runtime.strategy_profile_auto_control_effective);
  const manuallyPaused = runtime.strategy_profile_auto_control_reason === "manually_paused_by_admin";
  const restoreButton = actionButton(
    "恢复自动切档",
    "restore-strategy-profile-auto",
    "",
    "secondary",
    {
      disabled: !canAdmin || !configured || !manuallyPaused,
      title: !canAdmin
        ? "当前账号只有查看权限，不能恢复自动切档。"
        : !configured
          ? "当前没有启用自动换档，无法恢复自动切档。"
          : manuallyPaused
            ? "恢复系统自动换档逻辑。"
            : "当前没有被管理员暂停的自动换档逻辑。",
    },
  );

  return surfaceCard({
    title: "自动换档控制",
    kicker: "独立功能",
    copy: "这里控制系统是否允许自动评估和切换策略档位。",
    actions: `<div class="table-actions table-actions--compact">${restoreButton}</div>`,
    content: `
      ${callout({
        title: summary.value,
        copy: summary.copy,
        pills: [actorTags(...summary.actors)],
      })}
      ${summaryStrip([
        {
          label: "自动换档开关",
          value: enabled ? "已启用" : configured ? "已暂停" : "未启用",
          meta: enabled ? "系统会自动评估是否切换策略档位。" : configured ? "当前由管理员暂停自动换档。" : "当前不会自动切换策略档位。",
          tone: enabled ? "positive" : configured ? "warning" : "outline",
          badge: actorTags("system"),
        },
        {
          label: "当前状态",
          value: summary.value,
          meta: summary.meta,
          tone: summary.tone,
          badge: actorTags(...summary.actors),
        },
        {
          label: "候选策略档位",
          value: readableProfile(latestOptimizationReport.recommended_profile_id || latestSelectionDecision.candidate_profile_id),
          meta: latestOptimizationReport.recommended_profile_id
            ? `相对当前档领先 ${formatNumber(latestOptimizationReport.score_delta_vs_active, 2, "0.00")}`
            : "当前没有新的候选策略档位。",
          tone: latestOptimizationReport.recommended_profile_id ? "info" : "outline",
          badge: actorTags("system", "ai"),
        },
        {
          label: "管理员状态",
          value: manuallyPaused ? "已手动暂停自动换档" : "当前未手动暂停",
          meta: manuallyPaused ? "会一直保持当前策略档位，直到管理员恢复自动切档。" : "当前没有管理员手动暂停自动换档。",
          tone: manuallyPaused ? "warning" : "outline",
          badge: actorTags("admin"),
        },
      ])}
      ${kvList([
        [
          "这个卡片负责什么",
          "只控制策略档位自动切换",
          "开启后，系统会自己评估是否需要换到另一套策略参数；关闭后，只能手动切换。",
        ],
        [
          "当前为什么没有自动切档",
          summarizeLocalizedList(latestSelectionDecision.blocked_reasons || latestProfileControl.blocked_reasons, {
            fallback: "当前没有新的自动换档阻断说明。",
            limit: 3,
          }),
          "这里解释系统为什么继续保持原档位，或为什么还在观察候选档位。",
        ],
      ])}
    `,
  });
}

function renderManualProfilePanel({ activeRevision = {}, activation = {}, canAdmin = false }) {
  const activeProfileId = currentStrategyProfile(activeRevision, activation);
  const buttons = PROFILE_OPTIONS.map(([profileId, label, tone]) =>
    actionButton(
      profileId === activeProfileId ? `${label}（当前）` : label,
      "manual-activate-strategy-profile",
      profileId,
      profileId === activeProfileId ? "primary" : tone,
      {
        disabled: !canAdmin || profileId === activeProfileId,
        title: !canAdmin
          ? "当前账号只有查看权限，不能手动切换策略档位。"
          : profileId === activeProfileId
            ? "当前策略档位已经生效，无需重复切换。"
            : `切换到 ${label}`,
      },
    ),
  ).join("");

  return surfaceCard({
    title: "策略档位切换",
    kicker: "人工入口",
    copy: "这里负责管理员手动切换当前策略档位。手动切档会留下审计记录，并在冻结窗口内压住自动换档。",
    content: `
      ${callout({
        title: `当前策略档位：${readableProfile(activeRevision.profile_id || activeRevision.profile_label || activation.active_profile_id, "当前没有生效中的策略档位")}`,
        copy: canAdmin
          ? "这里是管理员手动切换策略档位的地方。手动切换会留下审计记录，并在冻结窗口内压住自动换档。"
          : "这里只展示当前生效的策略档位。当前账号只有查看权限，不能手动切换。",
        pills: [actorTags("admin")],
      })}
      ${kvList([
        [
          "当前活动档位",
          readableProfile(activeProfileId, "当前没有生效中的策略档位"),
          "这是当前真正控制阈值和保护规则的策略档位。",
        ],
        [
          "这个卡片负责什么",
          canAdmin ? "管理员可以直接手动切换档位" : "当前账号只有查看权限",
          "手动切档会先检查安全门槛，并留下审计记录。",
        ],
      ])}
      <div class="table-actions table-actions--compact manual-profile-switch-actions manual-profile-switch-actions--centered">
        ${buttons}
      </div>
    `,
  });
}

function renderRuntimeParameterCard({
  runtimeProfiles = {},
  runtime = {},
  aiState = {},
  activeRevision = {},
  activation = {},
  latestSelectionDecision = {},
  latestOptimizationReport = {},
}) {
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
    kicker: "当前真正生效的配置",
    copy: "这里集中解释当前生效的运行模式、策略档位、策略层 shadow 和执行层 shadow。",
    content: `
      ${summaryStrip([
        {
          label: "当前运行模式",
          value: readableMode(runtime.effective_operating_mode || aiState.effective_operating_mode || "baseline_only"),
          meta: `默认运行模式 ${readableMode(runtime.configured_operating_mode || aiState.configured_operating_mode || "baseline_only")}`,
          tone: runtime.manual_override_active ? "warning" : "info",
          badge: actorTags(runtime.manual_override_active ? "admin" : "system"),
        },
        {
          label: "当前策略档位",
          value: readableProfile(activeRevision.profile_id || activation.active_profile_id),
          meta: activation.active_profile_id ? "当前阈值由这套策略档位控制。" : "当前还没有登记的生效策略档位。",
          tone: activation.active_profile_id ? "positive" : "outline",
          badge: actorTags("system"),
        },
        {
          label: "策略层 shadow",
          value: strategyShadowEnabled ? "已开启" : "未开启",
          meta: strategyShadowEnabled ? "比较 AI 决策路线和基础策略路线。" : "当前不积累策略层 shadow 对照样本。",
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
      ${callout({
        title: "策略层 shadow 和执行层 shadow 是两条独立辅助线",
        copy: "策略层 shadow 用来比较 AI 决策路线与基础策略；执行层 shadow 用来预演 AI 执行建议会怎样翻译成下单计划。它们都不等于自动换档。",
        pills: [actorTags("ai", "system")],
      })}
      ${kvList([
        [
          "参数来源",
          readableState(runtimeProfiles.profile_source || "env_fallback"),
          runtimeProfiles.control_plane_status === "deprecated_readonly"
            ? "当前仍由环境文件控制运行参数，页面主要负责解释和人工切换。"
            : "页面展示的是当前真正生效的运行参数。",
        ],
        [
          "主交易标的",
          textOrFallback(runtimePayload.default_symbol, "待配置"),
          localizeList(runtimePayload.allowed_symbols, "当前没有额外允许交易的标的。"),
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
          "最近自动换档候选",
          readableProfile(latestOptimizationReport.recommended_profile_id || latestSelectionDecision.candidate_profile_id),
          latestOptimizationReport.recommended_profile_id
            ? summarizeLocalizedList(latestOptimizationReport.notes, { fallback: "当前没有额外候选说明。", limit: 2 })
            : "当前没有新的自动换档候选。",
        ],
      ])}
    `,
  });
}

function runtimeModeSummary(runtime = {}) {
  if (!runtime.manual_override_active) {
    return {
      value: "当前未启用管理员手动覆盖",
      meta: `系统仍按默认运行模式 ${readableMode(runtime.configured_operating_mode || "baseline_only")} 自动运行。`,
      copy: "当前没有管理员手动接管。这里控制的是 AI 运行模式，不会直接决定策略档位是否自动切换。",
      actors: ["system"],
    };
  }
  return {
    value: `管理员已手动覆盖为 ${readableMode(runtime.manual_override_mode || runtime.effective_operating_mode || "baseline_only")}`,
    meta: runtime.manual_override_freeze_until
      ? `恢复系统自动逻辑时间：${formatMaybeTimestamp(runtime.manual_override_freeze_until)}`
      : "当前会一直保持手动模式，直到管理员恢复自动模式。",
    copy: runtime.manual_override_freeze_until
      ? "当前运行模式由管理员临时接管。冻结时间结束后，系统才会恢复自动运行模式逻辑。"
      : "当前运行模式由管理员手动接管，会一直保持到你主动恢复自动模式为止。",
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
      value: "当前未启用自动换档",
      meta: "当前配置没有开启自动换档。",
      copy: "当前自动换档是关闭状态。你仍然可以手动切换策略档位，但系统不会自己改档。",
      tone: "outline",
      actors: ["system"],
    };
  }
  if (manuallyPaused) {
    return {
      value: "管理员已暂停自动换档",
      meta: "当前会一直保持手动控制，直到管理员恢复自动切档。",
      copy: "管理员手动切档后，系统不会再自己改档，除非你主动恢复自动切档。",
      tone: "warning",
      actors: ["admin", "system"],
    };
  }
  if (latestProfileControl.applied) {
    return {
      value: `本轮已自动切到 ${readableProfile(latestProfileControl.requested_profile_id)}`,
      meta: "系统已按独立的自动换档逻辑完成这轮切换。",
      copy: "自动换档当前已启用，且本轮已经实际完成档位切换。",
      tone: "positive",
      actors: ["system", "ai"],
    };
  }
  if (candidate) {
    return {
      value: `当前正在评估候选策略档位 ${readableProfile(candidate)}`,
      meta: summarizeLocalizedList(latestSelectionDecision.blocked_reasons, {
        fallback: "当前还在比较证据，尚未满足自动切换条件。",
        limit: 2,
      }),
      copy: "自动换档当前已启用，但系统还在比较证据，尚未决定真正切换。",
      tone: "info",
      actors: ["system", "ai"],
    };
  }
  return {
    value: "自动换档已启用，当前保持原档位",
    meta: "系统会独立评估是否需要切换策略档位，但本轮没有新的自动切换动作。",
    copy: "当前自动换档当前已启用，但这轮没有新的切档动作。",
    tone: "info",
    actors: ["system", "ai"],
  };
}

function executionShadowState(mode) {
  const normalized = String(mode || "disabled").trim().toLowerCase();
  if (normalized === "enabled_live") {
    return {
      value: "已进入受限实盘",
      meta: "AI 执行建议会在保护边界内参与真实执行。",
      tone: "warning",
    };
  }
  if (normalized === "shadow_translation") {
    return {
      value: "已开启预演",
      meta: "系统会把 AI 执行建议翻译成影子下单计划，但不会直接改写真委托。",
      tone: "info",
    };
  }
  if (normalized === "diagnostic_only") {
    return {
      value: "仅记录诊断",
      meta: "系统只记录 AI 执行建议，不生成可落地的影子执行计划。",
      tone: "outline",
    };
  }
  return {
    value: "已关闭",
    meta: "当前不启用执行层 shadow，也不会采纳 AI 执行建议。",
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
