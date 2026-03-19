import { actionButton, callout, kvList, pill, summaryStrip, surfaceCard } from "../components.js";
import { booleanWord, escapeHtml, formatMaybeTimestamp, formatNumber, listOrDash } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

const RISK_LEVEL_LABELS = {
  conservative: "保守",
  normal: "常规",
  aggressive: "激进",
};

const CAPABILITY_STATUS_LABELS = {
  reserved_not_enabled: "保留未启用",
  diagnostic_only: "仅用于诊断",
  planner_recorded_suggestion_only: "仅记录建议",
  planner_translated_execution_preview: "仅生成影子翻译",
  enabled_live: "允许进入实盘执行",
};

const MANUAL_PROFILE_OPTIONS = [
  { profileId: "trend_aggressive", label: "趋势激进", tone: "primary" },
  { profileId: "trend_normal", label: "趋势标准", tone: "secondary" },
  { profileId: "trend_strict", label: "趋势严格", tone: "secondary" },
  { profileId: "range_defensive", label: "范围防御", tone: "warning" },
  { profileId: "high_volatility_defensive", label: "高波动防御", tone: "warning" },
  { profileId: "execution_degraded_safe", label: "执行降级安全", tone: "warning" },
];

export function renderAIConfigView(data) {
  const session = data.session || {};
  const runtimeProfiles = data.runtimeProfiles || {};
  const runtimePayload = runtimeProfiles.current_runtime_payload || {};
  const strategyProfiles = data.strategyProfiles || {};
  const activation = strategyProfiles.activation || {};
  const activeRevision = strategyProfiles.active_revision || null;
  const pendingRevision = strategyProfiles.pending_revision || null;
  const latestRecommendation = strategyProfiles.latest_recommendation || null;
  const optimizationReport = strategyProfiles.latest_optimization_report || {};
  const selectionDecision = strategyProfiles.latest_selection_decision || {};
  const autoRollbackPolicy = strategyProfiles.auto_rollback_policy || {};
  const stagedAutoRollbackPolicy = strategyProfiles.auto_rollback_policy_staged || null;
  const autoRollbackPolicyHistory = strategyProfiles.auto_rollback_policy_history || [];
  const activationPolicy = strategyProfiles.activation_policy || {};
  const stagedActivationPolicy = strategyProfiles.activation_policy_staged || null;
  const activationPolicyHistory = strategyProfiles.activation_policy_history || [];
  const executionSuggestionCapability = strategyProfiles.execution_parameter_suggestion_capability || {};
  const profileSpace = strategyProfiles.profile_space || {};
  const comparisonReport = strategyProfiles.comparison_report || {};
  const activationHistory = Array.isArray(strategyProfiles.activation_history) ? strategyProfiles.activation_history : [];
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const runtimeProfilesError = data.errors?.runtimeProfiles || null;
  const strategyProfilesError = data.errors?.strategyProfiles || null;
  const overlaySource = describeOverlaySource({ activation, activationHistory, activeRevision });

  return `
    <div class="panel-grid ai-config-layout">
      <div class="span-6 workspace-stack">
        ${surfaceCard({
          title: "当前运行参数",
          kicker: "生效中的档位上下文",
          copy: "先确认当前真正生效的参数和产品范围，再决定是否要调整档位、回滚策略或激活策略。",
          content: runtimeProfilesError
            ? callout({ title: "暂时无法读取运行参数", copy: runtimeProfilesError, pills: [pill("需要管理员权限", "warning")] })
            : summaryStrip([
                {
                  label: "运行基础来源",
                  value: readableState(runtimeProfiles.profile_source || "env_fallback"),
                  meta: "这里显示基础运行参数从哪里来；不会因为右侧档位切换而改写环境文件",
                  tone: runtimeProfiles.management_enabled ? "positive" : "warning",
                },
                {
                  label: "当前覆盖来源",
                  value: overlaySource.label,
                  meta: overlaySource.meta,
                  tone: overlaySource.tone,
                },
                {
                  label: "当前治理档位",
                  value: readableProfile(activeRevision?.profile_label || latestRecommendation?.recommended_profile_id, "暂无已采纳档位"),
                  meta: latestRecommendation
                    ? `最近建议 ${readableState(latestRecommendation.decision_status || "pending")}；可在右侧继续采纳、回滚或手动切换`
                    : "当前没有新的 AI 档位建议",
                  tone: activeRevision || latestRecommendation ? "info" : "neutral",
                },
                {
                  label: "主交易标的",
                  value: textOrFallback(runtimePayload.default_symbol, "待配置"),
                  meta: listText(runtimePayload.allowed_symbols, "当前没有额外允许标的"),
                  tone: "info",
                },
                {
                  label: "产品类型",
                  value: readableState(runtimePayload.trading_product_type || "unknown"),
                  meta: readableState(runtimePayload.margin_mode || "unknown"),
                  tone: "info",
                },
                {
                  label: "基础下单量",
                  value: formatNumber(runtimePayload.default_order_qty),
                  meta: `名义上限 ${formatNumber(runtimePayload.max_notional_per_symbol)}`,
                  tone: "info",
                },
              ]),
        })}
        ${surfaceCard({
          title: "管理员手动切换档位",
          kicker: "安全入口",
          copy: "这里直接切换已注册档位，只覆盖运行时内存参数，不回写环境文件。",
          classes: "manual-profile-card",
          content: strategyProfilesError
            ? ""
            : renderManualProfileSwitchPanel({
                canAdmin,
                activeRevision,
              }),
        })}
      </div>

      <div class="span-6 workspace-stack">
        ${surfaceCard({
          title: "当前档位与推荐摘要",
          kicker: "当前档位 / AI 推荐",
          copy: "把当前档位、最新推荐、选择结果和对比胜出档位放在一起，减少频繁跳页。",
          actions: renderStrategyActions({ canAdmin, latestRecommendation, pendingRevision, activation }),
          content: strategyProfilesError
            ? callout({ title: "暂时无法读取 AI 配置状态", copy: strategyProfilesError, pills: [pill("需要管理员权限", "warning")] })
            : `
                ${summaryStrip([
                  {
                    label: "当前档位",
                    value: readableProfile(activeRevision?.profile_label || activeRevision?.profile_id, "待确认"),
                    meta: readableRiskLevel(activeRevision?.risk_level),
                    tone: activeRevision ? "positive" : "neutral",
                  },
                  {
                    label: "最新推荐",
                    value: readableProfile(latestRecommendation?.recommended_profile_id, "暂无推荐"),
                    meta: readableState(latestRecommendation?.decision_status || "none"),
                    tone: latestRecommendation ? "warning" : "neutral",
                  },
                  {
                    label: "当前决策",
                    value: readableState(selectionDecision.decision_status || "unknown"),
                    meta: `${readableProfile(selectionDecision.active_profile_id, "当前档位未记录")} -> ${readableProfile(selectionDecision.candidate_profile_id, "暂无候选档位")}`,
                    tone: selectionDecision.candidate_profile_id ? "info" : "neutral",
                  },
                  {
                    label: "对比胜出档位",
                    value: readableProfile(optimizationReport.recommended_profile_id, "尚未生成"),
                    meta: optimizationReport.created_at ? formatMaybeTimestamp(optimizationReport.created_at) : "当前还没有最新对比报告",
                    tone: optimizationReport.recommended_profile_id ? "positive" : "neutral",
                  },
                ])}
                ${kvList([
                  ["推荐理由", readableCodeList(latestRecommendation?.reason_codes, "当前没有额外推荐理由"), readableCodeList(latestRecommendation?.risk_notes, "当前没有额外风险提示")],
                  ["下一步动作", readableState(selectionDecision.recommended_action || "unknown"), readableCodeList(selectionDecision.notes, "当前没有额外决策说明")],
                  ["当前档位摘要", profileSummary(activeRevision), "这里展示当前档位最关键的决策门槛与节奏参数"],
                  ["档位空间", `${formatNumber((profileSpace.registered_profiles || []).length || 0, 0)} 个已注册档位`, comparisonReport.recommended_profile_id ? `最近对比建议：${readableProfile(comparisonReport.recommended_profile_id)}` : "当前还没有最新对比建议"],
                ])}
              `,
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "自动回滚策略生命周期",
          kicker: "暂存 / 审批 / 冻结",
          copy: "自动回滚策略单独成卡，明确展示暂存、审批和冻结状态。",
          classes: "policy-card",
          content: strategyProfilesError ? "" : renderAutoRollbackPolicyPanel(autoRollbackPolicy, stagedAutoRollbackPolicy, autoRollbackPolicyHistory, canAdmin),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "档位激活策略",
          kicker: "真实放权边界",
          copy: "这里控制胜出档位何时允许真正进入激活链路。",
          classes: "policy-card",
          content: strategyProfilesError ? "" : renderActivationPolicyPanel(activationPolicy, stagedActivationPolicy, activationPolicyHistory, canAdmin),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "受限执行建议",
          kicker: "AI 执行边界",
          copy: "明确告诉用户当前执行建议处于哪一层，避免把诊断能力误解成真实自动执行。",
          content: strategyProfilesError ? callout({ title: "暂时无法读取执行边界", copy: strategyProfilesError, pills: [pill("等待数据", "warning")] }) : renderExecutionSuggestionCapability(executionSuggestionCapability),
        })}
      </div>
    </div>
  `;
}

function renderStrategyActions({ canAdmin, latestRecommendation, pendingRevision, activation }) {
  const actions = [];
  if (canAdmin) {
    actions.push(
      actionButton(
        latestRecommendation?.decision_status === "pending" ? "重新评估并生成建议" : "立即评估并生成建议",
        "evaluate-strategy-profile",
        "",
        "primary"
      )
    );
    actions.push(
      actionButton(
        latestRecommendation?.decision_status === "pending" ? "重新评估并允许自动切换" : "评估并允许自动切换",
        "evaluate-strategy-profile-with-auto-switch",
        "",
        "warning"
      )
    );
  }
  if (canAdmin && latestRecommendation?.recommendation_id && latestRecommendation.decision_status === "pending") {
    actions.push(actionButton("立即采纳建议", "accept-strategy-profile-now", latestRecommendation.recommendation_id, "primary"));
    actions.push(actionButton("保存为待审批", "stage-strategy-profile", latestRecommendation.recommendation_id, "secondary"));
  }
  if (canAdmin && pendingRevision?.revision_id) {
    actions.push(actionButton("激活待审批档位", "activate-pending-strategy-profile", pendingRevision.revision_id, "secondary"));
  }
  if (canAdmin && activation?.previous_active_revision_id) {
    actions.push(actionButton("回滚到上一稳定档位", "rollback-strategy-profile", activation.previous_active_revision_id, "warning"));
  }
  return actions.length ? `<div class="table-actions">${actions.join("")}</div>` : "";
}

function renderAutoRollbackPolicyPanel(policy, stagedPolicy = null, history = [], canAdmin = false) {
  const editable = stagedPolicy || policy || {};
  return `
    <div class="policy-summary-grid">
      ${kvList([
        ["自动回滚开关", policy.enabled ? "已启用" : "未启用", `${readableState(policy.policy_status || "unknown")} / ${Boolean(policy.effective) ? "当前生效" : "尚未生效"}`],
        ["复核与阈值", `仅在需复核时触发 ${booleanWord(policy.review_required_only)}`, `最少成交 ${formatNumber(policy.min_trade_count || 0, 0)} / 冷却 ${formatNumber(policy.cooldown_seconds || 0, 0)} 秒`],
      ])}
      ${kvList([
        ["放权矩阵", `允许标的 ${listText(policy.matrix_allowed_symbols, "当前没有限制标的")}`, `市场状态 ${readableCodeList(policy.matrix_allowed_regimes, "当前没有限制市场状态")} / 档位 ${readableCodeList(policy.matrix_allowed_profiles, "当前没有限制档位")}`],
        ["审批 / 冻结", `${textOrFallback(policy.approved_by, "待审批")} / ${policy.frozen ? "已冻结" : "正常"}`, policy.frozen ? `冻结原因 ${textOrFallback(policy.freeze_reason, "未填写")}` : `历史记录 ${formatNumber(history.length || 0, 0)} 条`],
      ])}
    </div>
    <form id="autoRollbackPolicyForm" class="field-grid policy-form-grid">
      <div class="panel-grid policy-form-panel">
        <div class="span-3"><label class="field-label" for="autoRollbackEnabled">是否启用</label><select id="autoRollbackEnabled" ${canAdmin ? "" : "disabled"}><option value="true" ${editable.enabled ? "selected" : ""}>已启用</option><option value="false" ${editable.enabled ? "" : "selected"}>已关闭</option></select></div>
        <div class="span-3"><label class="field-label" for="autoRollbackReviewOnly">仅在需复核时触发</label><select id="autoRollbackReviewOnly" ${canAdmin ? "" : "disabled"}><option value="true" ${editable.review_required_only !== false ? "selected" : ""}>是</option><option value="false" ${editable.review_required_only === false ? "selected" : ""}>否</option></select></div>
        <div class="span-3"><label class="field-label" for="autoRollbackMinTrades">最少成交笔数</label><input id="autoRollbackMinTrades" type="number" step="1" min="0" value="${formatNumber(editable.min_trade_count || 0, 0)}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-3"><label class="field-label" for="autoRollbackCooldown">冷却秒数</label><input id="autoRollbackCooldown" type="number" step="1" min="0" value="${formatNumber(editable.cooldown_seconds || 0, 0)}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-6"><label class="field-label" for="autoRollbackSymbols">允许标的列表（逗号分隔）</label><input id="autoRollbackSymbols" type="text" value="${escapeHtml((editable.matrix_allowed_symbols || []).join(", "))}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-6"><label class="field-label" for="autoRollbackRegimes">允许市场状态列表（逗号分隔）</label><input id="autoRollbackRegimes" type="text" value="${escapeHtml((editable.matrix_allowed_regimes || []).join(", "))}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-12"><label class="field-label" for="autoRollbackProfiles">允许档位列表（逗号分隔）</label><input id="autoRollbackProfiles" type="text" value="${escapeHtml((editable.matrix_allowed_profiles || []).join(", "))}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-12"><label class="field-label" for="autoRollbackReason">暂存原因</label><input id="autoRollbackReason" type="text" value="${escapeHtml(stagedPolicy?.update_reason || "页面暂存自动回滚策略")}" ${canAdmin ? "" : "disabled"}></div>
      </div>
      <div class="stack-actions policy-actions">
        <button class="primary-button" type="submit" ${canAdmin ? "" : "disabled"}>暂存策略</button>
        ${actionButton("审批暂存版本", "approve-auto-rollback-policy", stagedPolicy?.policy_id || "", "secondary")}
        ${actionButton(policy.frozen ? "解除冻结" : "冻结当前策略", "toggle-freeze-auto-rollback-policy", policy.frozen ? "false" : "true", policy.frozen ? "secondary" : "warning")}
      </div>
    </form>
  `;
}

function renderActivationPolicyPanel(policy, stagedPolicy = null, history = [], canAdmin = false) {
  const editable = stagedPolicy || policy || {};
  return `
    <div class="policy-summary-grid">
      ${kvList([
        ["激活策略开关", policy.enabled ? "已启用" : "未启用", `${readableState(policy.policy_status || "unknown")} / ${Boolean(policy.effective) ? "当前生效" : "尚未生效"}`],
        ["激活阈值", `综合分 ${formatNumber(policy.min_composite_score || 0, 3)} / 回放得分 ${formatNumber(policy.min_offline_replay_score || 0, 3)}`, `推荐强度 ${formatNumber(policy.min_recommendation_strength || 0, 3)}`],
      ])}
      ${kvList([
        ["安全保护", `要求正向回放 ${booleanWord(policy.require_positive_replay_consensus)}`, `影子复核阻断 ${booleanWord(policy.disallow_when_shadow_review_required)}`],
        ["放权矩阵", `允许标的 ${listText(policy.matrix_allowed_symbols, "当前没有限制标的")}`, `市场状态 ${readableCodeList(policy.matrix_allowed_regimes, "当前没有限制市场状态")} / 档位 ${readableCodeList(policy.matrix_allowed_profiles, "当前没有限制档位")}`],
        ["审批 / 冻结", `${textOrFallback(policy.approved_by, "待审批")} / ${policy.frozen ? "已冻结" : "正常"}`, policy.frozen ? `冻结原因 ${textOrFallback(policy.freeze_reason, "未填写")}` : `历史记录 ${formatNumber(history.length || 0, 0)} 条`],
      ])}
    </div>
    <form id="activationPolicyForm" class="field-grid policy-form-grid">
      <div class="panel-grid policy-form-panel">
        <div class="span-3"><label class="field-label" for="activationPolicyEnabled">是否启用</label><select id="activationPolicyEnabled" ${canAdmin ? "" : "disabled"}><option value="true" ${editable.enabled ? "selected" : ""}>已启用</option><option value="false" ${editable.enabled ? "" : "selected"}>已关闭</option></select></div>
        <div class="span-3"><label class="field-label" for="activationPolicyComposite">最低综合分</label><input id="activationPolicyComposite" type="number" step="0.001" value="${formatNumber(editable.min_composite_score || 0, 3)}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-3"><label class="field-label" for="activationPolicyReplay">最低回放得分</label><input id="activationPolicyReplay" type="number" step="0.001" value="${formatNumber(editable.min_offline_replay_score || 0, 3)}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-3"><label class="field-label" for="activationPolicyStrength">最低推荐强度</label><input id="activationPolicyStrength" type="number" step="0.001" value="${formatNumber(editable.min_recommendation_strength || 0, 3)}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-3"><label class="field-label" for="activationPolicyReplayConsensus">要求正向回放</label><select id="activationPolicyReplayConsensus" ${canAdmin ? "" : "disabled"}><option value="true" ${editable.require_positive_replay_consensus ? "selected" : ""}>是</option><option value="false" ${editable.require_positive_replay_consensus ? "" : "selected"}>否</option></select></div>
        <div class="span-3"><label class="field-label" for="activationPolicyShadowReview">影子复核阻断</label><select id="activationPolicyShadowReview" ${canAdmin ? "" : "disabled"}><option value="true" ${editable.disallow_when_shadow_review_required ? "selected" : ""}>是</option><option value="false" ${editable.disallow_when_shadow_review_required ? "" : "selected"}>否</option></select></div>
        <div class="span-6"><label class="field-label" for="activationPolicySymbols">允许标的列表（逗号分隔）</label><input id="activationPolicySymbols" type="text" value="${escapeHtml((editable.matrix_allowed_symbols || []).join(", "))}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-6"><label class="field-label" for="activationPolicyRegimes">允许市场状态列表（逗号分隔）</label><input id="activationPolicyRegimes" type="text" value="${escapeHtml((editable.matrix_allowed_regimes || []).join(", "))}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-6"><label class="field-label" for="activationPolicyProfiles">允许档位列表（逗号分隔）</label><input id="activationPolicyProfiles" type="text" value="${escapeHtml((editable.matrix_allowed_profiles || []).join(", "))}" ${canAdmin ? "" : "disabled"}></div>
        <div class="span-6"><label class="field-label" for="activationPolicyReason">暂存原因</label><input id="activationPolicyReason" type="text" value="${escapeHtml(stagedPolicy?.update_reason || "页面暂存激活策略")}" ${canAdmin ? "" : "disabled"}></div>
      </div>
      <div class="stack-actions policy-actions">
        <button class="primary-button" type="submit" ${canAdmin ? "" : "disabled"}>暂存激活策略</button>
        ${actionButton("审批暂存版本", "approve-activation-policy", stagedPolicy?.policy_id || "", "secondary")}
        ${actionButton(policy.frozen ? "解除冻结" : "冻结当前策略", "toggle-freeze-activation-policy", policy.frozen ? "false" : "true", policy.frozen ? "secondary" : "warning")}
      </div>
    </form>
  `;
}

function renderExecutionSuggestionCapability(capability) {
  return kvList([
    ["状态", readableCapabilityStatus(capability.status || capability.capability_status), capability.applied_to_live_execution ? "当前允许进入真实执行" : "当前只在诊断或影子翻译层"],
    ["允许字段", listText(capability.allowed_fields, "当前没有允许字段说明"), listText(capability.blocked_fields, "当前没有阻断字段说明")],
    ["执行边界", textOrFallback(capability.translation_mode, "待确认"), textOrFallback(capability.live_translation_guard, "当前仍由确定性执行器做最终裁剪")],
    ["说明", textOrFallback(capability.description, "当前没有额外说明"), textOrFallback(capability.preview_note, "执行建议不会直接改写真实委托")],
  ]);
}

function renderManualProfileSwitchPanel({ canAdmin, activeRevision }) {
  const activeProfileId = activeRevision?.profile_id || "";
  const activeProfileLabel = readableProfile(activeRevision?.profile_label || activeProfileId, "待确认");
  const buttons = MANUAL_PROFILE_OPTIONS.map(({ profileId, label, tone }) => {
    const isActive = profileId === activeProfileId;
    return `
      <button
        class="${escapeHtml(isActive ? "secondary-button" : buttonToneClass(tone))}"
        data-action="manual-activate-strategy-profile"
        data-value="${escapeHtml(profileId)}"
        ${!canAdmin || isActive ? "disabled" : ""}
        title="${escapeHtml(
          !canAdmin
            ? "只有管理员可以执行手动档位切换"
            : isActive
              ? "当前档位已经生效，无需重复切换"
              : `切换到 ${label}`
        )}"
      >${escapeHtml(isActive ? `${label}（当前）` : label)}</button>
    `;
  }).join("");

  return `
    <div class="manual-profile-switch-grid">
      <div class="kv-list">
        <div class="kv-row">
          <span class="kv-row__label">当前手动切换目标</span>
          <strong class="kv-row__value">${escapeHtml(activeProfileLabel)}</strong>
          <span class="meta-copy">只允许切换已注册档位；切换前仍会检查活动委托等安全门禁</span>
        </div>
        <div class="kv-row">
          <span class="kv-row__label">操作说明</span>
          <strong class="kv-row__value">${canAdmin ? "点击下方按钮即可立即切换" : "当前账号只有查看权限"}</strong>
          <span class="meta-copy">管理员手动切换会留下审计记录，便于回查是谁在什么时间切换了档位</span>
        </div>
      </div>
      <div class="table-actions table-actions--compact manual-profile-switch-actions">
        ${buttons}
      </div>
    </div>
  `;
}

function profileSummary(revision) {
  if (!revision?.payload) return "当前还没有生效档位摘要";
  const payload = revision.payload;
  return [
    `15m 最小间隔 ${formatNumber(payload.decision_min_interval_seconds_15m, 0)} 秒`,
    `净边际门槛 ${formatNumber(payload.strategy_min_net_edge_bps, 1)} 个基点`,
    `开仓阈值 ${formatNumber(payload.strategy_entry_alpha_min, 2)}`,
  ].join(" / ");
}

function readableRiskLevel(value) {
  return RISK_LEVEL_LABELS[String(value || "").toLowerCase()] || readableState(value || "unknown");
}

function readableCapabilityStatus(value) {
  return CAPABILITY_STATUS_LABELS[String(value || "").toLowerCase()] || readableState(value || "unknown");
}

function textOrFallback(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function listText(value, fallback = "当前没有额外说明") {
  return listOrDash(value, fallback);
}

function readableProfile(value, fallback = "待确认") {
  if (value === null || value === undefined || value === "") return fallback;
  return readableState(String(value), fallback);
}

function readableCodeItem(value, fallback = "当前没有额外说明") {
  const text = String(value ?? "").trim();
  if (!text) return fallback;
  if (text.includes("=")) {
    const [rawKey, ...rest] = text.split("=");
    const rawValue = rest.join("=").trim();
    const key = String(rawKey || "").trim().toLowerCase();
    if (key === "evaluation_ref") return rawValue ? `评估记录 ${rawValue}` : "评估记录";
    if (key === "winner_selection_policy") return rawValue ? `激活策略版本 ${rawValue}` : "激活策略版本";
    if (key === "activation_policy_id") return rawValue ? `激活策略编号 ${rawValue}` : "激活策略编号";
  }
  return readableState(text, fallback);
}

function readableCodeList(value, fallback = "当前没有额外说明") {
  if (!value) return fallback;
  if (Array.isArray(value)) {
    const items = value.map((item) => readableCodeItem(item, "")).filter(Boolean);
    return items.length ? items.join("、") : fallback;
  }
  const text = readableCodeItem(value, "");
  return text || fallback;
}

function describeOverlaySource({ activation, activationHistory, activeRevision }) {
  if (!activeRevision) {
    return {
      label: "当前无运行时覆盖",
      meta: "当前没有可确认的生效档位信息",
      tone: "neutral",
    };
  }
  const latestActivation = activationHistory.find((item) => item?.to_profile_id === activeRevision.profile_id) || null;
  const triggerType = String(latestActivation?.trigger_type || "").toLowerCase();
  if (!latestActivation && String(activation?.last_switch_reason || "").toLowerCase() === "initial_seed") {
    return {
      label: "当前无运行时覆盖",
      meta: "当前仍直接使用环境文件派生出的基础档位参数",
      tone: "neutral",
    };
  }
  if (triggerType === "ai_auto") {
    return {
      label: "AI 自动覆盖",
      meta: "当前档位覆盖由 AI 推荐并自动执行；只改运行时内存参数，不回写环境文件",
      tone: "info",
    };
  }
  if (triggerType === "system_guard") {
    return {
      label: "系统保护覆盖",
      meta: "当前档位覆盖由系统保护逻辑触发；只改运行时内存参数，不回写环境文件",
      tone: "warning",
    };
  }
  if (triggerType === "manual" || triggerType === "rollback" || latestActivation) {
    return {
      label: "管理员手动覆盖",
      meta: "当前档位覆盖由管理员操作触发；只改运行时内存参数，不回写环境文件",
      tone: "warning",
    };
  }
  return {
    label: "当前无运行时覆盖",
    meta: "当前仍直接使用环境文件派生出的基础档位参数",
    tone: "neutral",
  };
}

function buttonToneClass(tone) {
  if (tone === "primary") return "primary-button";
  if (tone === "secondary") return "secondary-button";
  if (tone === "warning") return "warning-button";
  if (tone === "danger") return "danger-button";
  return "table-button";
}
