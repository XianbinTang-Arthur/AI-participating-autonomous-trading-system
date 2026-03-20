import { callout, kvList, summaryStrip, surfaceCard } from "../components.js";
import { escapeHtml, formatMaybeTimestamp, formatNumber, listOrDash } from "../formatters.js";
import { readableState } from "../terms.js";

const PROFILE_OPTIONS = [
  ["trend_aggressive", "趋势激进", "primary"],
  ["trend_normal", "趋势标准", "secondary"],
  ["trend_strict", "趋势严格", "secondary"],
  ["range_defensive", "范围防御", "warning"],
  ["high_volatility_defensive", "高波动防御", "warning"],
  ["execution_degraded_safe", "执行降级安全", "warning"],
];

export function renderAIConfigView(data) {
  const session = data.session || {};
  const summary = data.summary || {};
  const aiState = summary.ai || {};
  const runtimeProfiles = summary.runtime_profile || {};
  const runtimePayload = runtimeProfiles.current_runtime_payload || {};
  const strategyProfiles = summary.strategy_profile || {};
  const activation = strategyProfiles.activation || {};
  const activeRevision = strategyProfiles.active_revision || null;
  const latestSelectionDecision = strategyProfiles.latest_selection_decision || null;
  const latestProfileControl = aiState.latest_profile_control_decision || null;
  const activationHistory = Array.isArray(strategyProfiles.activation_history) ? strategyProfiles.activation_history : [];
  const summaryError = data.error || null;
  const overlay = describeOverlay({ activationHistory, activeRevision });
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";

  return `
    <div class="panel-grid ai-config-layout">
      <div class="span-6 workspace-stack">
        ${surfaceCard({
          title: "运行参数概览",
          kicker: "运行状态",
          copy: "这里只保留当前真实生效的运行参数与档位来源，避免把说明性信息和控制面混在一起。",
          content: summaryError
            ? callout({ title: "暂时无法读取 AI 配置摘要", copy: summaryError })
            : `
                ${summaryStrip([
                  {
                    label: "参数来源",
                    value: readableState(runtimeProfiles.profile_source || "env_fallback"),
                    meta: runtimeProfiles.control_plane_status === "deprecated_readonly"
                      ? "运行时参数仍由环境文件决定，页面只读展示。"
                      : "页面当前只读展示已经生效的参数。",
                    tone: "info",
                  },
                  {
                    label: "当前覆盖来源",
                    value: overlay.label,
                    meta: overlay.meta,
                    tone: overlay.tone,
                  },
                  {
                    label: "当前策略档位",
                    value: readableProfile(activeRevision?.profile_label || activation.active_profile_id, "待确认"),
                    meta: activeRevision?.profile_id || "当前暂无已登记的活动策略档位。",
                    tone: activeRevision ? "positive" : "neutral",
                  },
                  {
                    label: "主交易标的",
                    value: textOrFallback(runtimePayload.default_symbol, "待配置"),
                    meta: listText(runtimePayload.allowed_symbols, "当前没有额外允许交易的标的。"),
                    tone: "info",
                  },
                  {
                    label: "产品与保证金",
                    value: readableState(runtimePayload.trading_product_type || "unknown"),
                    meta: readableState(runtimePayload.margin_mode || "unknown"),
                    tone: "info",
                  },
                  {
                    label: "基础下单量",
                    value: formatNumber(runtimePayload.default_order_qty),
                    meta: `单标的名义上限 ${formatNumber(runtimePayload.max_notional_per_symbol)}`,
                    tone: "info",
                  },
                ])}
                ${kvList([
                  ["AI 运行模式", readableState(aiState.effective_operating_mode || "baseline_only"), `配置值 ${readableState(aiState.configured_operating_mode || "baseline_only")}`],
                  ["影子评估", aiState.shadow_mode_enabled ? "常开" : "未开启", readableShadowMeta(aiState.shadow_summary)],
                ])}
              `,
        })}

        ${surfaceCard({
          title: "策略档位切换",
          kicker: "人工入口",
          copy: "管理员手动切换仍是最高优先级入口。切换前会检查安全门槛，并留下审计记录。",
          content: summaryError
            ? callout({ title: "暂时无法读取 AI 配置摘要", copy: summaryError })
            : renderManualProfilePanel({ activeRevision, canAdmin }),
        })}
      </div>

      <div class="span-6 workspace-stack">
        ${surfaceCard({
          title: "档位概览",
          kicker: "档位状态",
          copy: "这里只展示当前档位、最近一次自动切换结论和阻断原因，供当前判断使用。",
          content: summaryError
            ? callout({
                title: "暂时无法读取 AI 配置状态",
                copy: summaryError,
              })
            : `
                ${summaryStrip([
                  {
                    label: "当前档位",
                    value: readableProfile(activeRevision?.profile_label || activation.active_profile_id, "待确认"),
                    meta: activeRevision?.profile_id || "尚未读取到活动策略档位。",
                    tone: activeRevision ? "positive" : "neutral",
                  },
                  {
                    label: "最近一次自动切换结论",
                    value: latestProfileControl
                      ? latestProfileControl.applied
                        ? "AI 自动切换已执行"
                        : "AI 自动切换未执行"
                      : readableState(latestSelectionDecision?.decision_status || "none"),
                    meta: latestProfileControl
                      ? readableProfile(latestProfileControl.requested_profile_id, "当前暂无新的自动切换请求")
                      : readableSelectionMeta(latestSelectionDecision),
                    tone: latestProfileControl?.applied ? "info" : "neutral",
                  },
                  {
                    label: "当前档位来源",
                    value: overlay.label,
                    meta: overlay.meta,
                    tone: overlay.tone,
                  },
                  {
                    label: "影子评估状态",
                    value: aiState.shadow_mode_enabled ? "自动常开" : "未开启",
                    meta: readableShadowMeta(aiState.shadow_summary),
                    tone: aiState.shadow_mode_enabled ? "info" : "neutral",
                  },
                ])}
                ${kvList([
                  [
                    "自动切换阻断原因",
                    latestProfileControl
                      ? readableReasons(latestProfileControl.blocked_reasons)
                      : readableReasons(latestSelectionDecision?.blocked_reasons),
                    latestProfileControl?.frozen_by_admin_override
                      ? `管理员覆盖冻结到 ${formatMaybeTimestamp(latestProfileControl.freeze_until)}`
                      : "当前没有额外冻结说明。",
                  ],
                  [
                    "当前档位摘要",
                    profileSummary(activeRevision),
                    "展示当前档位最关键的节奏、门槛和风控参数。",
                  ],
                ])}
              `,
        })}
      </div>
    </div>
  `;
}

function renderManualProfilePanel({ activeRevision, canAdmin }) {
  const activeProfileId = activeRevision?.profile_id || "";
  const buttons = PROFILE_OPTIONS.map(([profileId, label, tone]) => {
    const active = profileId === activeProfileId;
    const className = active ? "secondary-button" : buttonClass(tone);
    const disabled = !canAdmin || active ? "disabled" : "";
    const title = !canAdmin
      ? "当前账号只有查看权限，不能手动切换策略档位。"
      : active
        ? "当前策略档位已经生效，无需重复切换。"
        : `切换到 ${label}`;
    return `
      <button
        class="${escapeHtml(className)}"
        data-action="manual-activate-strategy-profile"
        data-value="${escapeHtml(profileId)}"
        title="${escapeHtml(title)}"
        ${disabled}
      >${escapeHtml(active ? `${label}（当前）` : label)}</button>
    `;
  }).join("");

  return `
    ${kvList([
      ["当前活动档位", readableProfile(activeRevision?.profile_label || activeProfileId, "待确认"), activeRevision?.profile_id || "当前没有已登记的活动策略档位。"],
      ["操作说明", canAdmin ? "点击下方按钮即可立刻切换" : "当前账号只有查看权限", "切换前仍会检查活动委托等安全门槛，并留下审计记录。"],
    ])}
    <div class="table-actions table-actions--compact manual-profile-switch-actions">
      ${buttons}
    </div>
  `;
}

function describeOverlay({ activationHistory, activeRevision }) {
  if (!activeRevision) {
    return {
      label: "当前没有活动覆盖",
      meta: "当前没有已登记的活动策略档位可供说明。",
      tone: "neutral",
    };
  }
  const latest = activationHistory.find((item) => item?.to_profile_id === activeRevision.profile_id) || null;
  const triggerType = String(latest?.trigger_type || "").toLowerCase();
  if (triggerType === "ai_auto") {
    return {
      label: "AI 自动切换",
      meta: "当前档位由主链自动评估后切换，只影响运行时内存参数，不会回写环境文件。",
      tone: "info",
    };
  }
  if (triggerType === "manual") {
    return {
      label: "管理员手动覆盖",
      meta: "当前档位由管理员手动切换触发，AI 自动切换会按配置冻结一段时间。",
      tone: "warning",
    };
  }
  return {
    label: "环境文件默认值",
    meta: "当前档位仍来自环境文件或启动时默认种子。",
    tone: "neutral",
  };
}

function profileSummary(activeRevision) {
  const payload = activeRevision?.payload || null;
  if (!payload) return "当前没有活动档位摘要。";
  return [
    `15m 最小间隔 ${formatNumber(payload.decision_min_interval_seconds_15m, 0)} 秒`,
    `净边际门槛 ${formatNumber(payload.strategy_min_net_edge_bps, 1)} 个基点`,
    `开仓阈值 ${formatNumber(payload.strategy_entry_alpha_min, 2)}`,
  ].join(" / ");
}

function readableShadowMeta(summary) {
  if (!summary) return "影子评估数据暂时不可用。";
  if (!summary.window_count) return "影子评估常开，但当前窗口样本还不够。";
  return `最近窗口 ${formatNumber(summary.window_count, 0)} 个，优于基础策略占比 ${formatNumber((summary.outperformed_rate || 0) * 100, 1)}%。`;
}

function readableSelectionMeta(decision) {
  if (!decision) return "当前暂无新的自动切换结论。";
  if (decision.candidate_profile_id) {
    return `候选档位 ${readableProfile(decision.candidate_profile_id)} / 来源 ${readableState(decision.candidate_source || "none")}`;
  }
  return "当前暂无新的候选档位。";
}

function readableReasons(reasons) {
  return listText(reasons, "当前没有记录阻断原因。");
}

function readableProfile(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return readableState(String(value), fallback);
}

function textOrFallback(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function listText(value, fallback = "当前没有额外说明。") {
  return listOrDash(value, fallback);
}

function buttonClass(tone) {
  if (tone === "primary") return "primary-button";
  if (tone === "secondary") return "secondary-button";
  if (tone === "warning") return "warning-button";
  return "table-button";
}
