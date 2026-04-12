import { actorTags, actionButton, callout, kvList, summaryStrip, surfaceCard } from "../components.js";
// #6 / #7 / #8 修复：此前本文件复制了三份 copy.js 的 helper（listText、textOrFallback、
// summarizeList），直接 import 统一版本，删除本地重复定义。
import { localizeList, summarizeLocalizedList, textOrFallback } from "../copy.js";
import { escapeHtml, formatNumber } from "../formatters.js";
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
  const rdpControl = data.rdpControl || {};
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
        ${renderManualOperatingModePanel({ runtime, canAdmin })}
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
        })}
      </div>
      <div class="span-12 workspace-stack">
        ${renderRdpControlPanel({ rdpControl, canAdmin })}
      </div>
      <div class="span-12 workspace-stack">
        ${renderCurrentConfigurationCard({ runtimeProfiles, runtime, aiState, activeRevision, activation })}
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
      "select-ai-operating-mode",
      value,
      value === mode ? "primary" : tone,
      {
        disabled: !canAdmin || value === mode,
        title: !canAdmin
          ? "当前账号只有查看权限"
          : value === mode
            ? "当前运行模式"
            : `切换到${label}`,
      },
    ),
  ).join("");

  return surfaceCard({
    title: "运行模式切换",
    kicker: "交易决策入口",
    copy: "这里决定最终下单前由谁拍板：完全按基础策略、让 AI 辅助判断，还是直接由 AI 参与决策。配置文件只负责设默认值，你仍可在这里临时切换。",
    content: `
      ${callout({
        title: summary.title,
        copy: summary.copy,
        pills: [actorTags(...summary.actors)],
      })}
      ${summaryStrip([
        {
          label: "配置默认",
          value: readableMode(runtime.configured_operating_mode || "baseline_only"),
          meta: "启动时优先按这个模式运行",
          tone: "outline",
          badge: actorTags("config"),
        },
        {
          label: "当前运行",
          value: readableMode(mode),
          meta: mode === (runtime.configured_operating_mode || "baseline_only")
            ? "当前正按配置默认模式运行"
            : "当前已手动切到其他模式运行",
          tone: mode === (runtime.configured_operating_mode || "baseline_only") ? "info" : "warning",
          badge: actorTags(runtime.operating_mode_source === "manual_selection" ? "admin" : "system"),
        },
        {
          label: "AI 状态",
          value: runtime.degraded ? "已降级" : "正常",
          meta: runtime.degradation_reason || "当前没有新的 AI 降级原因",
          tone: runtime.degraded ? "warning" : "positive",
          badge: actorTags("ai", "system"),
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
}) {
  const activeProfileId = currentStrategyProfile(activeRevision, activation);
  const summary = autoControlSummary(runtime, latestProfileControl, latestSelectionDecision, latestOptimizationReport);
  const configured = Boolean(runtime.strategy_profile_auto_control_configured);
  const autoEnabled = Boolean(runtime.strategy_profile_auto_control_effective);
  const profileButtons = PROFILE_OPTIONS.map(([profileId, label, tone]) =>
    actionButton(
      profileId === activeProfileId ? `${label}（当前）` : label,
      "manual-activate-strategy-profile",
      profileId,
      profileId === activeProfileId ? "primary" : tone,
      {
        disabled: !canAdmin || autoEnabled || profileId === activeProfileId,
        title: !canAdmin
          ? "当前账号只有查看权限"
          : autoEnabled
            ? "当前正在自动切档，下面的按钮暂时不可点击"
            : profileId === activeProfileId
              ? "当前正在使用这个档位"
              : `切换到${label}`,
      },
    ),
  ).join("");

  return surfaceCard({
    title: "自动换档控制",
    kicker: "策略档位切换",
    copy: "这里用唯一的自动换档主开关决定 6 个策略档位是由系统自动评估并自动激活，还是由你手动固定。开启自动换档后，系统会默认启用自动激活规则，并锁定下面 6 个档位按钮；切回手动后才能再次点击。",
    actions: renderProfileControlModeActions({ canAdmin, autoEnabled }),
    content: `
      ${callout({
        title: summary.title,
        copy: summary.copy,
        pills: [actorTags(...summary.actors)],
      })}
      ${summaryStrip([
        {
          label: "配置默认",
          value: configured ? "自动切档" : "手动切档",
          meta: configured ? "配置文件默认启用自动换档，系统会按规则自动评估并自动激活档位" : "配置文件默认关闭自动换档，当前只允许手动切档",
          tone: "outline",
          badge: actorTags("config"),
        },
        {
          label: "当前控制",
          value: autoEnabled ? "自动切档" : "手动切档",
          meta: autoEnabled ? "系统会自动评估候选档位，并按激活规则自动切换" : "现在由你手动固定档位，系统不会自动评估或自动激活",
          tone: autoEnabled ? "positive" : "warning",
          badge: actorTags(autoEnabled ? "system" : "admin"),
        },
        {
          label: "当前档位",
          value: readableProfile(activeProfileId, "待确认"),
          meta: autoEnabled ? "当前正在自动管理这个档位" : "当前手动固定在这个档位",
          tone: "info",
          badge: actorTags("system"),
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
    `,
  });
}

function renderProfileControlModeActions({
  canAdmin = false,
  autoEnabled = false,
}) {
  return `
    <div class="table-actions table-actions--compact">
      ${actionButton("手动切档", "set-profile-control-mode", "manual", !autoEnabled ? "primary" : "secondary", {
        disabled: !canAdmin || !autoEnabled,
        title: !canAdmin ? "当前账号只有查看权限" : !autoEnabled ? "当前已经是手动切档" : "关闭自动切档，改为手动选择档位",
      })}
      ${actionButton("自动切档", "set-profile-control-mode", "auto", autoEnabled ? "primary" : "secondary", {
        disabled: !canAdmin || autoEnabled,
        title: !canAdmin ? "当前账号只有查看权限" : autoEnabled ? "当前已经是自动切档" : "开启自动切档，下面的档位按钮会锁定",
      })}
    </div>
  `;
}

// ── RDP 控制卡片 ──────────────────────────────────────────────────

const WORKFLOW_LABELS = {
  data_maintenance: "数据采集",
  research_cycle: "研究管线",
};

function taskStatusSummary(taskInfo) {
  if (!taskInfo) return { value: "未运行", meta: "还没有运行记录", tone: "outline" };
  const status = taskInfo.status || "unknown";
  if (status === "running") {
    const since = taskInfo.started_at ? relativeTime(taskInfo.started_at) : "";
    return { value: "运行中", meta: since ? `开始于 ${since}` : "正在执行", tone: "info" };
  }
  if (status === "done") {
    const ago = taskInfo.finished_at ? relativeTime(taskInfo.finished_at) : "";
    return { value: "已完成", meta: ago ? `完成于 ${ago}` : "上次成功", tone: "positive" };
  }
  if (status === "failed") {
    const msg = taskInfo.error_message || "执行失败";
    return { value: "失败", meta: msg, tone: "danger" };
  }
  if (status === "pending") {
    return { value: "排队中", meta: "等待 daemon 执行", tone: "warning" };
  }
  return { value: status, meta: "", tone: "outline" };
}

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
  } catch (err) {
    console.warn("[relativeTime] parse error:", err);
    return "";
  }
}

function isWorkflowBusy(taskInfo) {
  if (!taskInfo) return false;
  return taskInfo.status === "pending" || taskInfo.status === "running";
}

function renderRdpControlPanel({ rdpControl = {}, canAdmin = false }) {
  const tasks = rdpControl.tasks || {};
  const tasksError = rdpControl.tasks_error || null;
  const pendingRecs = rdpControl.pending_recommendations || [];
  const activeParams = rdpControl.active_parameters || {};

  const dataMaint = tasks.data_maintenance || null;
  const research = tasks.research_cycle || null;
  const dataMaintStatus = taskStatusSummary(dataMaint);
  const researchStatus = taskStatusSummary(research);
  const dataMaintBusy = isWorkflowBusy(dataMaint);
  const researchBusy = isWorkflowBusy(research);

  // ── workflow trigger buttons ──
  const workflowButtons = `
    <div class="table-actions table-actions--compact manual-profile-switch-actions manual-profile-switch-actions--centered">
      ${actionButton(
        dataMaintBusy ? "数据采集中…" : "拉取近期数据",
        "rdp-trigger-workflow",
        "data_maintenance",
        "secondary",
        {
          disabled: !canAdmin || dataMaintBusy,
          title: !canAdmin
            ? "当前账号只有查看权限"
            : dataMaintBusy
              ? "数据采集任务正在进行"
              : "触发 data_maintenance workflow：拉取 OKX 近期数据",
        },
      )}
      ${actionButton(
        researchBusy ? "研究进行中…" : "运行研究管线",
        "rdp-trigger-workflow",
        "research_cycle",
        "secondary",
        {
          disabled: !canAdmin || researchBusy,
          title: !canAdmin
            ? "当前账号只有查看权限"
            : researchBusy
              ? "研究管线任务正在进行"
              : "触发 research_cycle workflow：全管线 Phase 2→Decision",
        },
      )}
    </div>
  `;

  // ── pending recommendations table ──
  const recsSection = pendingRecs.length > 0
    ? `
      <div class="rdp-section">
        <p class="meta-copy" style="margin: 0.75rem 0 0.5rem"><strong>待处理建议</strong></p>
        <table class="mini-table">
          <thead><tr>
            <th>建议 ID</th><th>交易对</th><th>策略/周期</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>
            ${pendingRecs.map((rec) => {
              const isApproved = rec.status === "approved";
              return `
                <tr>
                  <td class="mono-cell">${escapeHtml(truncateId(rec.recommendation_id))}</td>
                  <td>${escapeHtml(rec.symbol || "")}</td>
                  <td>${escapeHtml(rec.family || "")}/${escapeHtml(rec.timeframe || "")}</td>
                  <td>${isApproved ? "已审批" : escapeHtml(rec.action || "promote")}</td>
                  <td class="table-actions table-actions--compact">
                    ${isApproved
                      ? actionButton("应用参数", "rdp-apply-only", rec.recommendation_id, "primary", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "应用已审批的参数",
                        })
                      : actionButton("审批并应用", "rdp-approve-and-apply", rec.recommendation_id, "primary", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "审批此建议并立即应用参数",
                        })
                    }
                    ${!isApproved
                      ? actionButton("拒绝", "rdp-reject-recommendation", rec.recommendation_id, "ghost", {
                          disabled: !canAdmin,
                          title: !canAdmin ? "当前账号只有查看权限" : "拒绝此建议",
                        })
                      : ""
                    }
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `
    : `<p class="meta-copy" style="margin: 0.75rem 0 0.25rem">当前没有待处理的参数变更建议。</p>`;

  // ── active parameters table ──
  const activeEntries = Object.entries(activeParams);
  const activeSection = activeEntries.length > 0
    ? `
      <div class="rdp-section">
        <p class="meta-copy" style="margin: 0.75rem 0 0.5rem"><strong>当前生效参数</strong></p>
        <table class="mini-table">
          <thead><tr>
            <th>组合</th><th>参数集 ID</th><th>应用时间</th><th>操作</th>
          </tr></thead>
          <tbody>
            ${activeEntries.map(([combo, info]) => {
              const family = info.family || combo.split("_")[0] || "";
              const timeframe = info.timeframe || combo.split("_").slice(1).join("_") || "";
              return `
                <tr>
                  <td>${escapeHtml(combo)}</td>
                  <td class="mono-cell">${escapeHtml(truncateId(info.parameter_set_id))}</td>
                  <td>${info.applied_at ? relativeTime(info.applied_at) : "—"}</td>
                  <td class="table-actions table-actions--compact">
                    ${actionButton("回滚", "rdp-rollback-parameters", `${family}/${timeframe}`, "ghost", {
                      disabled: !canAdmin,
                      title: !canAdmin ? "当前账号只有查看权限" : "回滚到上一版参数",
                    })}
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    `
    : `<p class="meta-copy" style="margin: 0.75rem 0 0.25rem">当前没有生效的 active 参数。</p>`;

  return surfaceCard({
    title: "RDP 研究管线控制",
    kicker: "数据 + 研究 + 参数审批",
    copy: "从这里触发数据采集和参数研究流程，审批并应用研究结果。数据采集和研究任务由宿主机 daemon 异步执行。",
    content: `
      ${tasksError
        ? callout({
            title: "任务状态查询失败",
            copy: "无法连接任务队列数据库，下方的任务状态可能不准确。",
            pills: [actorTags("system")],
          })
        : ""
      }
      ${summaryStrip([
        {
          label: "数据采集",
          value: dataMaintStatus.value,
          meta: dataMaintStatus.meta,
          tone: dataMaintStatus.tone,
          badge: actorTags("system"),
        },
        {
          label: "研究管线",
          value: researchStatus.value,
          meta: researchStatus.meta,
          tone: researchStatus.tone,
          badge: actorTags("ai", "system"),
        },
        {
          label: "待审批",
          value: pendingRecs.length > 0 ? `${pendingRecs.length} 条建议` : "无",
          meta: pendingRecs.length > 0 ? "有待审批的参数变更建议" : "当前没有待审批的建议",
          tone: pendingRecs.length > 0 ? "warning" : "outline",
          badge: actorTags("operator"),
        },
      ])}
      ${workflowButtons}
      ${recsSection}
      ${activeSection}
    `,
  });
}

function truncateId(id) {
  if (!id) return "—";
  if (id.length <= 20) return id;
  return id.slice(0, 8) + "…" + id.slice(-8);
}

// ── 运行参数概览 ──────────────────────────────────────────────────

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
          tone: runtime.operating_mode_source === "manual_selection" ? "warning" : "info",
          badge: actorTags(runtime.operating_mode_source === "manual_selection" ? "admin" : "system"),
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
          // #6：改用 copy.localizeList；原本的本地 listText 只 join 不 localize，
          // 现在享受 localizeError 词条化的好处。
          localizeList(runtimePayload.allowed_symbols, "当前没有额外允许交易的标的"),
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
  const configured = readableMode(runtime.configured_operating_mode || "baseline_only");
  const effective = readableMode(runtime.effective_operating_mode || "baseline_only");
  if ((runtime.effective_operating_mode || "baseline_only") === (runtime.configured_operating_mode || "baseline_only")) {
    return {
      title: `当前按配置运行：${configured}`,
      copy: "当前运行模式与配置文件默认值一致。下面另外两个按钮代表可临时切换的模式，点选后会立刻改成那个模式运行。",
      actors: ["config", "system"],
    };
  }
  return {
    title: `当前手动切到：${effective}`,
    copy: `配置默认仍是 ${configured}。如果你想回到配置默认，只要点回对应的模式按钮即可，不需要额外再点“跟随配置”。`,
    actors: ["admin", "config"],
  };
}

function autoControlSummary(runtime = {}, latestProfileControl = {}, latestSelectionDecision = {}, latestOptimizationReport = {}) {
  const configured = Boolean(runtime.strategy_profile_auto_control_configured);
  const enabled = Boolean(runtime.strategy_profile_auto_control_effective);
  const candidate = latestOptimizationReport.recommended_profile_id || latestSelectionDecision.candidate_profile_id || "";

  if (!configured && !enabled) {
    return {
      title: "当前按配置手动切档",
      copy: "配置文件默认关闭自动换档，所以现在由你手动选择下面 6 个档位。系统不会自动评估或自动激活档位，直到你重新开启自动换档。",
      tone: "outline",
      actors: ["config", "admin"],
    };
  }
  if (configured && !enabled) {
    return {
      title: "当前改为手动切档",
      copy: "配置文件默认启用自动换档，但你现在临时切到了手动模式。系统会保持当前档位，不会自动评估或自动激活，直到你重新开启自动换档。",
      tone: "warning",
      actors: ["admin", "config"],
    };
  }
  if (!configured && enabled) {
    return {
      title: "当前已开启自动切档",
      copy: "配置文件默认是手动切档，但你已经从页面临时开启了自动换档。现在由系统自动评估候选档位，并按自动激活规则切换，下方按钮会锁定。",
      tone: "positive",
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
      // #8：改用 copy.summarizeLocalizedList；原本的本地 summarizeList 只看 slice(0,2)
      // 且不 localizeError。统一走 copy.js 的 limit=2 版本。
      copy: summarizeLocalizedList(latestSelectionDecision.blocked_reasons, {
        fallback: "系统还在比较证据",
        limit: 2,
      }),
      tone: "info",
      actors: ["system", "ai"],
    };
  }
  if (enabled) {
    return {
      title: "当前按配置自动切档",
      copy: "当前已按配置启用自动换档。系统会自动评估候选档位，并按自动激活规则决定是否切换；本轮没有新的切档动作，所以继续保持现有档位。",
      tone: "info",
      actors: ["config", "system"],
    };
  }
  return {
    title: "当前保持手动档位",
    copy: "系统会继续保持当前手动档位，直到你主动切到别的档位，或者重新开启自动切档。",
    tone: "warning",
    actors: ["admin"],
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

// #12 修复：把"故意把 ai_decision_maker_with_profile_control 折叠成
// ai_decision_maker"这件事写清楚——后端真实存了带 _with_profile_control
// 后缀的 enum，但是顶部"运行模式"按钮组只有 baseline_only / ai_advisor /
// ai_decision_maker 三个 radio。如果不在这里折叠，按钮组会出现"高亮没有任何
// 一个按钮"的尴尬状态，用户会以为运行模式坏了。
//
// 折叠仅作用于"按钮组高亮 / 当前模式标签"的展示层；底层的真实 enum 仍由
// runtime.effective_operating_mode 等字段保留，对外暴露的 readableMode（下方）
// 单独识别这个值并显示成"AI 决策者"，二者并不冲突。如果未来按钮组扩成 4 项，
// 把这一行删掉即可。
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

// #6 / #7 / #8 修复：原本这里定义了三个和 copy.js 重复的 helper
// (textOrFallback / listText / summarizeList)。全部删除，改为 top-of-file 直接
// import copy.js 的统一版本，避免 triple source of truth。
