import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, listOrDash } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

const RISK_LEVEL_LABELS = {
  conservative: "保守",
  normal: "常规",
  aggressive: "激进",
};

const TRIGGER_LABELS = {
  manual: "人工切换",
  ai_auto: "AI 自动切换",
  rollback: "回滚",
  system_guard: "系统保护",
};

export function renderAdminView(data) {
  const session = data.session || {};
  const providers = data.authProviders || {};
  const operatorUsers = data.operatorUsers || {};
  const users = operatorUsers.users || [];
  const runtimeProfiles = data.runtimeProfiles || {};
  const runtimePayload = runtimeProfiles.current_runtime_payload || {};
  const strategyProfiles = data.strategyProfiles || {};
  const activation = strategyProfiles.activation || {};
  const activeRevision = strategyProfiles.active_revision || null;
  const pendingRevision = strategyProfiles.pending_revision || null;
  const latestRecommendation = strategyProfiles.latest_recommendation || null;
  const activationHistory = strategyProfiles.activation_history || [];
  const rejectionHistory = strategyProfiles.rejections || [];
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const operatorUsersError = data.errors?.operatorUsers || null;
  const runtimeProfilesError = data.errors?.runtimeProfiles || null;
  const strategyProfilesError = data.errors?.strategyProfiles || null;

  return `
    <div class="panel-grid">
      <div class="span-4">
        ${surfaceCard({
          title: "登录与权限",
          kicker: "控制台访问",
          copy: "先说明当前是谁登录、登录能力来自哪里，以及当前账号是否具备改策略、停机或恢复交易的权限。",
          content: `
            ${callout({
              title: providers.auth_enabled ? "控制台认证已启用" : "当前未启用控制台认证",
              copy: providers.auth_enabled
                ? "浏览器会话、数据库用户和兼容 API Key 会共同决定当前会话能执行哪些人工操作。"
                : "当前是开放访问模式。页面可以直接访问，但高风险操作仍应谨慎使用。",
              pills: [
                pill(`会话登录 ${booleanWord(providers.session_enabled)}`, providers.session_enabled ? "positive" : "outline"),
                pill(`数据库账号 ${booleanWord(providers.database_backed)}`, providers.database_backed ? "info" : "outline"),
                pill(`API Key ${booleanWord(providers.api_key_compatibility_enabled)}`, providers.api_key_compatibility_enabled ? "warning" : "outline"),
              ],
            })}
            ${kvList([
              ["当前身份", session.identity || "未登录", readableState(session.auth_source || "anonymous")],
              ["当前角色", readableState(session.role || "anonymous"), canAdmin ? "具备管理策略和账号的权限" : "当前只能查看或执行低权限操作"],
              ["已存储用户数", formatNumber(providers.stored_user_count), `启用中的用户 ${formatNumber(operatorUsers.enabled_user_count)}`],
              ["启用管理员数", formatNumber(operatorUsers.enabled_admin_count), providers.database_backed ? "来自 operator_users 表" : "当前是内存模式"],
            ])}
          `,
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "当前运行参数",
          kicker: "系统实际生效值",
          copy: "这里展示系统当前真正在用的运行参数，便于对照“当前策略档位”到底改动了什么。",
          content: runtimeProfilesError
            ? callout({
                title: "暂时无法读取运行参数",
                copy: runtimeProfilesError,
                pills: [pill("需要管理员权限", "warning")],
              })
            : `
                ${statGrid([
                  { label: "配置来源", value: readableState(runtimeProfiles.profile_source || "env_fallback"), meta: runtimeProfiles.management_enabled ? "可在页面内管理" : "当前仍由环境文件控制" },
                  { label: "主交易标的", value: runtimePayload.default_symbol || "-", meta: listOrDash(runtimePayload.allowed_symbols) },
                  { label: "产品类型", value: readableState(runtimePayload.trading_product_type || "-"), meta: readableState(runtimePayload.margin_mode || "-") },
                  { label: "基础下单数量", value: formatNumber(runtimePayload.default_order_qty), meta: `名义上限 ${formatNumber(runtimePayload.max_notional_per_symbol)}` },
                ])}
                ${kvList([
                  ["最大仓位数量", formatNumber(runtimePayload.max_abs_position_qty), "单品种最大绝对仓位"],
                  ["活动委托上限", formatNumber(runtimePayload.max_open_orders), "同一时刻允许的最大活动委托数"],
                  ["默认杠杆", formatNumber(runtimePayload.default_target_leverage), `最大杠杆 ${formatNumber(runtimePayload.max_target_leverage)}`],
                  ["动态杠杆", booleanWord(runtimePayload.strategy_dynamic_leverage_enabled), runtimePayload.strategy_short_bias_enabled ? "允许做空偏置" : "当前没有做空偏置"],
                ])}
              `,
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "策略档位状态",
          kicker: "当前档位 / 待审批 / 自动切换",
          copy: "这块只回答三个问题：当前用的是哪一档、有没有待审批的新档、系统是否允许 AI 自动切到更保守档位。",
          actions: canAdmin ? actionButton("立即评估当前市场", "evaluate-strategy-profile", "", "secondary") : "",
          content: strategyProfilesError
            ? callout({
                title: "暂时无法读取策略档位状态",
                copy: strategyProfilesError,
                pills: [pill("需要管理员权限", "warning")],
              })
            : `
                ${statGrid([
                  { label: "当前档位", value: activeRevision?.profile_label || "-", meta: activeRevision?.profile_id || "尚未初始化" },
                  { label: "风险级别", value: readableRiskLevel(activeRevision?.risk_level), meta: readableState(activeRevision?.market_intent || "-") },
                  { label: "待审批档位", value: pendingRevision?.profile_label || "无", meta: pendingRevision?.profile_id || "当前没有待生效的策略档位" },
                  { label: "自动切换", value: activation.auto_switch_enabled ? "已启用" : "已关闭", meta: activation.cooldown_until ? `冷却至 ${formatMaybeTimestamp(activation.cooldown_until)}` : "当前不在冷却期" },
                ])}
                ${kvList([
                  ["上次切换结果", readableState(activation.last_activation_result || "-"), activation.last_activation_at ? formatMaybeTimestamp(activation.last_activation_at) : "尚未发生过切换"],
                  ["上次切换原因", localizeError(activation.last_switch_reason || "-"), activation.last_switch_actor || "系统默认值"],
                  ["上个稳定档位", activation.previous_active_revision_id || "-", "用于人工回滚时作为默认目标"],
                  ["当前摘要", profileSummary(activeRevision), "展示当前档位最关键的决策与门槛参数"],
                ])}
              `,
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "AI 调参建议",
          kicker: "推荐 / 审批 / 回滚",
          copy: canAdmin
            ? "AI 只负责推荐安全档位，真正生效仍然受本地规则和管理员操作约束。这里可以立即采纳、转成待审批，或者拒绝这条建议。"
            : "当前账号只能查看最近一条 AI 调参建议及其状态，不能直接审批或回滚。",
          actions: renderStrategyActions({ canAdmin, latestRecommendation, pendingRevision, activation }),
          content: strategyProfilesError
            ? ""
            : renderRecommendationPanel({ latestRecommendation, activeRevision, pendingRevision, activation }),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "最近切换记录",
          kicker: "激活 / 自动切换 / 回滚",
          copy: "记录每次策略档位切换是怎么发生的，便于追溯是谁批准的、是人工还是 AI 自动切换，以及改动了哪些关键字段。",
          content: renderActivationHistoryTable(activationHistory),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "最近拒绝记录",
          kicker: "未采纳的建议",
          copy: "如果一条调参建议没有被采纳，这里会明确记录是谁拒绝的、为何拒绝，以及是被本地规则挡住还是被管理员手动否决。",
          content: renderRejectionHistoryTable(rejectionHistory),
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "控制台账号",
          kicker: "账户管理",
          copy: canAdmin
            ? "管理员可以在这里创建、停用、改角色、重置密码或删除控制台账号。"
            : "当前账号没有管理员权限，因此这里只能查看账号概览，不能改动。",
          content: `
            ${operatorUsersError ? `<div class="notice-card tone-warning">${operatorUsersError}</div>` : ""}
            ${renderCreateForm(canAdmin)}
            ${table(
              ["用户名", "角色", "状态", "最近登录", "最近更新", "操作"],
              users.map((user) => [
                `<div><strong>${user.username || "-"}</strong><div class="table-meta mono">${user.user_id || "-"}</div></div>`,
                `${pill(readableState(user.role || "-"), user.role === "admin" ? "danger" : user.role === "operator" ? "info" : "outline")}`,
                `<div>${pill(user.enabled ? "已启用" : "已停用", user.enabled ? "positive" : "warning")}${user.protected_last_admin ? '<div class="table-meta">当前最后一个启用中的管理员</div>' : ""}</div>`,
                formatMaybeTimestamp(user.last_login_at),
                formatMaybeTimestamp(user.updated_at || user.created_at),
                renderUserActions(user, canAdmin),
              ]),
              "当前还没有控制台账号。"
            )}
          `,
        })}
      </div>
    </div>
  `;
}

function readableRiskLevel(value) {
  return RISK_LEVEL_LABELS[String(value || "").toLowerCase()] || "-";
}

function renderStrategyActions({ canAdmin, latestRecommendation, pendingRevision, activation }) {
  const actions = [];
  if (canAdmin && latestRecommendation?.recommendation_id && latestRecommendation.decision_status === "pending") {
    actions.push(actionButton("立即采纳建议", "accept-strategy-profile-now", latestRecommendation.recommendation_id, "primary"));
    actions.push(actionButton("转为待审批", "stage-strategy-profile", latestRecommendation.recommendation_id, "secondary"));
    actions.push(actionButton("拒绝这条建议", "reject-strategy-profile", latestRecommendation.recommendation_id, "warning"));
  }
  if (canAdmin && pendingRevision?.revision_id) {
    actions.push(actionButton("激活待审批档位", "activate-pending-strategy-profile", pendingRevision.revision_id, "secondary"));
  }
  if (canAdmin && activation?.previous_active_revision_id) {
    actions.push(actionButton("回滚到上个稳定档位", "rollback-strategy-profile", activation.previous_active_revision_id, "danger"));
  }
  return actions.length ? `<div class="table-actions">${actions.join("")}</div>` : "";
}

function renderRecommendationPanel({ latestRecommendation, activeRevision, pendingRevision, activation }) {
  if (!latestRecommendation) {
    return callout({
      title: "最近还没有新的调参建议",
      copy: "当前还没有 AI 调参输出。可以先点击“立即评估当前市场”，生成一条新的档位建议。",
      pills: [pill(`自动切换 ${activation.auto_switch_enabled ? "已启用" : "已关闭"}`, activation.auto_switch_enabled ? "positive" : "outline")],
    });
  }
  return `
    ${callout({
      title: `${latestRecommendation.decision_status === "accepted" ? "最近建议已采纳" : latestRecommendation.decision_status === "rejected" ? "最近建议已拒绝" : "最近建议等待处理"}：${latestRecommendation.recommended_profile_id}`,
      copy: latestRecommendation.human_summary || "这条建议没有提供额外说明。",
      pills: [
        pill(`置信度 ${formatNumber(latestRecommendation.confidence, 2)}`, "info"),
        pill(`有效至 ${formatMaybeTimestamp(latestRecommendation.expires_at)}`, "outline"),
        pill(`当前档位 ${activeRevision?.profile_id || "-"}`, "neutral"),
      ],
    })}
    ${kvList([
      ["建议档位", latestRecommendation.recommended_profile_id, latestRecommendation.fallback_profile_id ? `回退档位 ${latestRecommendation.fallback_profile_id}` : "未提供回退档位"],
      ["建议状态", readableState(latestRecommendation.decision_status || "-"), latestRecommendation.decision_reason_code ? localizeError(latestRecommendation.decision_reason_code) : "当前尚未决定是否生效"],
      ["推荐理由", listOrDash(latestRecommendation.reason_codes), listOrDash(latestRecommendation.risk_notes)],
      ["当前待审批档位", pendingRevision?.profile_label || "无", pendingRevision?.profile_id || "当前没有待生效档位"],
      ["自动切换冷却", activation.cooldown_until ? formatMaybeTimestamp(activation.cooldown_until) : "当前不在冷却期", activation.auto_switch_enabled ? "系统允许自动切向更保守的档位" : "当前未开启自动切换"],
      ["建议时间", formatMaybeTimestamp(latestRecommendation.generated_at), formatRelativeAge(latestRecommendation.generated_at)],
    ])}
  `;
}

function renderActivationHistoryTable(history) {
  return table(
    ["时间", "切换路径", "触发方式", "结果", "关键改动"],
    history.map((item) => [
      `<div><strong>${formatRelativeAge(item.executed_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.executed_at)}</div></div>`,
      `<div><strong>${item.from_profile_id || "-"}</strong><div class="table-meta">→ ${item.to_profile_id || "-"}</div></div>`,
      `<div><strong>${TRIGGER_LABELS[item.trigger_type] || readableState(item.trigger_type || "-")}</strong><div class="table-meta">${item.actor_identity || item.actor_role || "-"}</div></div>`,
      `<div><strong>${readableState(item.result || "-")}</strong><div class="table-meta">${localizeError(item.reason_code || "-")}</div></div>`,
      `<div><strong>${formatChangedFieldCount(item.diff)}</strong><div class="table-meta">${listChangedFields(item.diff)}</div></div>`,
    ]),
    "最近还没有策略档位切换记录。"
  );
}

function renderRejectionHistoryTable(history) {
  return table(
    ["时间", "建议档位", "拒绝来源", "拒绝原因"],
    history.map((item) => [
      `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
      `<div><strong>${item.recommended_profile_id || "-"}</strong><div class="table-meta">${item.recommendation_id || "-"}</div></div>`,
      `<div><strong>${readableState(item.rejection_source || "-")}</strong><div class="table-meta">${item.actor_identity || item.actor_role || "-"}</div></div>`,
      `<div><strong>${localizeError(item.rejection_reason_code || "-")}</strong><div class="table-meta">${item.rejection_reason_detail || "没有额外说明"}</div></div>`,
    ]),
    "最近还没有被拒绝的调参建议。"
  );
}

function profileSummary(revision) {
  if (!revision?.payload) return "-";
  const payload = revision.payload;
  return [
    `15m 最小间隔 ${formatNumber(payload.decision_min_interval_seconds_15m, 0)} 秒`,
    `净边际门 ${formatNumber(payload.strategy_min_net_edge_bps, 1)} bps`,
    `开仓阈值 ${formatNumber(payload.strategy_entry_alpha_min, 2)}`,
  ].join(" / ");
}

function formatChangedFieldCount(diff) {
  const count = Array.isArray(diff?.changed_fields) ? diff.changed_fields.length : 0;
  return `${count} 项`;
}

function listChangedFields(diff) {
  const fields = Array.isArray(diff?.changed_fields) ? diff.changed_fields : [];
  return fields.length ? fields.slice(0, 3).join("、") : "没有字段变化";
}

function renderCreateForm(canAdmin) {
  return `
    <form id="operatorCreateForm" class="field-grid">
      <div class="panel-head">
        <div>
          <h3>创建新账号</h3>
          <p class="meta-copy">用于增加只读、操作员或管理员账号。</p>
        </div>
        ${canAdmin ? pill("可操作", "positive") : pill("仅管理员可操作", "warning")}
      </div>
      <div class="panel-grid">
        <div class="span-4">
          <label class="field-label" for="operatorCreateUsername">用户名</label>
          <input id="operatorCreateUsername" type="text" placeholder="例如 trader01" ${canAdmin ? "" : "disabled"}>
        </div>
        <div class="span-4">
          <label class="field-label" for="operatorCreatePassword">初始密码</label>
          <input id="operatorCreatePassword" type="password" placeholder="请输入初始密码" ${canAdmin ? "" : "disabled"}>
        </div>
        <div class="span-2">
          <label class="field-label" for="operatorCreateRole">角色</label>
          <select id="operatorCreateRole" ${canAdmin ? "" : "disabled"}>
            <option value="viewer">只读用户</option>
            <option value="operator">操作员</option>
            <option value="admin">管理员</option>
          </select>
        </div>
        <div class="span-2">
          <label class="field-label" for="operatorCreateEnabled">启用状态</label>
          <select id="operatorCreateEnabled" ${canAdmin ? "" : "disabled"}>
            <option value="true">立即启用</option>
            <option value="false">先停用</option>
          </select>
        </div>
      </div>
      <div class="stack-actions">
        <button id="operatorCreateButton" class="primary-button" type="submit" ${canAdmin ? "" : "disabled"}>创建账号</button>
      </div>
    </form>
  `;
}

function renderUserActions(user, canAdmin) {
  if (!canAdmin) return '<span class="meta-copy">需要管理员权限</span>';
  return `
    <div class="table-actions">
      ${actionButton("改角色", "change-user-role", user.username)}
      ${actionButton("重置密码", "reset-user-password", user.username)}
      ${actionButton(user.enabled ? "停用" : "启用", "toggle-user", user.username, user.enabled ? "warning" : "secondary")}
      ${actionButton("删除", "delete-user", user.username, "danger")}
    </div>
  `;
}
