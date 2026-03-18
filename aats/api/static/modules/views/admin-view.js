import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, listOrDash } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

export function renderAdminView(data) {
  const session = data.session || {};
  const providers = data.authProviders || {};
  const operatorUsers = data.operatorUsers || {};
  const users = operatorUsers.users || [];
  const runtimeProfiles = data.runtimeProfiles || {};
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const runtimePayload = runtimeProfiles.current_runtime_payload || {};
  const operatorUsersError = data.errors?.operatorUsers || null;
  const runtimeProfilesError = data.errors?.runtimeProfiles || null;

  return `
    <div class="panel-grid">
      <div class="span-5">
        ${surfaceCard({
          title: "登录与权限",
          kicker: "权限模型",
          copy: "把当前登录方式、账号来源和权限边界说清楚，避免误判自己是否有人工操作权限。",
          content: `
            ${callout({
              title: providers.auth_enabled ? "当前已启用控制台认证" : "当前未启用控制台认证",
              copy: providers.auth_enabled
                ? "浏览器会话、数据库用户和兼容 API Key 都会影响控制台的可操作范围。"
                : "当前是本地开放模式，页面可以直接访问，但高风险操作仍应谨慎。",
              pills: [
                pill(`会话登录：${booleanWord(providers.session_enabled)}`, providers.session_enabled ? "positive" : "outline"),
                pill(`数据库用户：${booleanWord(providers.database_backed)}`, providers.database_backed ? "info" : "outline"),
                pill(`兼容 API Key：${booleanWord(providers.api_key_compatibility_enabled)}`, providers.api_key_compatibility_enabled ? "warning" : "outline"),
              ],
            })}
            ${kvList([
              ["当前身份", session.identity || "匿名访问", readableState(session.auth_source || "anonymous")],
              ["当前角色", readableState(session.role || "anonymous"), canAdmin ? "当前具备管理员权限" : "当前没有管理员权限"],
              ["已存储用户数", formatNumber(providers.stored_user_count), `启用用户 ${formatNumber(operatorUsers.enabled_user_count)}`],
              ["启用管理员数", formatNumber(operatorUsers.enabled_admin_count), providers.database_backed ? "来自 operator_users 表" : "当前是内存模式"],
            ])}
          `,
        })}
      </div>

      <div class="span-7">
        ${surfaceCard({
          title: "运行配置摘要",
          kicker: "当前运行档位",
          copy: "把当前真正生效的运行配置讲清楚，避免把环境变量、运行时状态和人工操作记录混在一起理解。",
          content: runtimeProfilesError
            ? callout({
                title: "当前无法读取运行配置详情",
                copy: runtimeProfilesError,
                pills: [pill("需要管理员权限", "warning")],
              })
            : `
                ${statGrid([
                  { label: "配置来源", value: readableState(runtimeProfiles.profile_source || "env_fallback"), meta: runtimeProfiles.management_enabled ? "可在页面内管理" : "当前由环境文件控制" },
                  { label: "主交易标的", value: runtimePayload.default_symbol || "-", meta: listOrDash(runtimePayload.allowed_symbols) },
                  { label: "产品类型", value: readableState(runtimePayload.trading_product_type || "-"), meta: readableState(runtimePayload.margin_mode || "-") },
                  { label: "默认下单量", value: formatNumber(runtimePayload.default_order_qty), meta: `最大名义 ${formatNumber(runtimePayload.max_notional_per_symbol)}` },
                ])}
                ${kvList([
                  ["最大仓位", formatNumber(runtimePayload.max_abs_position_qty), "单品种最大绝对仓位"],
                  ["活动委托上限", formatNumber(runtimePayload.max_open_orders), "同一时刻允许的最大活动委托数"],
                  ["默认杠杆", formatNumber(runtimePayload.default_target_leverage), `最大杠杆 ${formatNumber(runtimePayload.max_target_leverage)}`],
                  ["策略偏置", booleanWord(runtimePayload.strategy_short_bias_enabled), runtimePayload.strategy_dynamic_leverage_enabled ? "已启用动态杠杆" : "未启用动态杠杆"],
                ])}
              `,
        })}
      </div>

      <div class="span-12">
        ${surfaceCard({
          title: "控制台账号",
          kicker: "账户管理",
          copy: canAdmin
            ? "管理员可以在这里创建、停用、改角色、重置密码或删除控制台账号。"
            : "当前账号没有管理员权限，因此只能查看账号概览，不能改动。",
          content: `
            ${operatorUsersError ? `<div class="notice-card tone-warning">${operatorUsersError}</div>` : ""}
            ${renderCreateForm(canAdmin)}
            ${table(
              ["用户名", "角色", "状态", "最近登录", "最近更新", "操作"],
              users.map((user) => [
                `<div><strong>${user.username || "-"}</strong><div class="table-meta mono">${user.user_id || "-"}</div></div>`,
                `${pill(readableState(user.role || "-"), user.role === "admin" ? "danger" : user.role === "operator" ? "info" : "outline")}`,
                `<div>${pill(user.enabled ? "已启用" : "已停用", user.enabled ? "positive" : "warning")}${user.protected_last_admin ? '<div class="table-meta">最后一个启用中的管理员</div>' : ""}</div>`,
                formatMaybeTimestamp(user.last_login_at),
                formatMaybeTimestamp(user.updated_at || user.created_at),
                renderUserActions(user, canAdmin),
              ]),
              "当前还没有操作员账户。"
            )}
          `,
        })}
      </div>
    </div>
  `;
}

function renderCreateForm(canAdmin) {
  return `
    <form id="operatorCreateForm" class="field-grid">
      <div class="panel-head">
        <div>
          <h3>创建新账号</h3>
          <p class="meta-copy">用于增加新的只读、操作员或管理员控制台账号。</p>
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
