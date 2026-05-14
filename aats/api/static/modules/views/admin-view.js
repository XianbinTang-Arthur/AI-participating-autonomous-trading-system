import { callout, kvList, notice, pill, primaryStatusPanel, responsiveTable, surfaceCard } from "../components.js";
import { booleanWord, escapeHtml, formatMaybeTimestamp, formatNumber, middleEllipsis } from "../formatters.js";
import { permissionStatusLabel, readableState } from "../terms.js";

export function renderAdminView(data) {
  const session = data.session || {};
  const providers = data.authProviders || {};
  const operatorUsers = data.operatorUsers || {};
  const users = operatorUsers.users || [];
  const canAdmin = session.role === "admin" || session.identity === "api_key_write";
  const operatorUsersError = data.errors?.operatorUsers || null;

  return `
    <div class="panel-grid">
      <div class="span-12">
        ${primaryStatusPanel({
          eyebrow: "控制面",
          title: "账户与权限工作区",
          headline: canAdmin ? "这里专门处理控制台账号、角色和访问权限" : "当前账号可以查看权限状态，但不能改动账号治理",
          summary: providers.auth_enabled
            ? "AI 配置页现在只保留策略档位状态、自动切换结论和管理员手动切换入口，这里只保留和人员权限直接相关的操作。"
            : "当前控制台认证未启用，但仍建议把账号治理和 AI 配置分开处理。",
          tone: canAdmin ? "positive" : "info",
          pills: [
            pill(`当前身份 ${session.identity || "未登录"}`, session.authenticated ? "positive" : "outline"),
            pill(`当前角色 ${readableState(session.role || "anonymous")}`, canAdmin ? "danger" : "outline"),
            pill(`管理能力 ${permissionStatusLabel(canAdmin)}`, canAdmin ? "positive" : "warning"),
          ],
          metrics: [
            {
              label: "控制台认证",
              value: providers.auth_enabled ? "已启用" : "未启用",
              meta: providers.session_enabled ? "支持浏览器会话登录" : "当前不支持浏览器会话登录",
              tone: providers.auth_enabled ? "positive" : "warning",
            },
            {
              label: "已存储用户数",
              value: formatNumber(providers.stored_user_count, 0),
              meta: `启用中 ${formatNumber(operatorUsers.enabled_user_count, 0)}`,
              tone: Number(operatorUsers.enabled_user_count || 0) > 0 ? "info" : "warning",
            },
            {
              label: "管理员数量",
              value: formatNumber(operatorUsers.enabled_admin_count, 0),
              meta: providers.database_backed ? "来自 operator_users 表" : "当前是内存模式",
              tone: Number(operatorUsers.enabled_admin_count || 0) > 0 ? "positive" : "warning",
            },
            {
              label: "AI 配置入口",
              value: "已独立成页",
              meta: "请在 AI 配置页继续处理策略档位、自动切换状态和管理员手动切换。",
              tone: "info",
            },
          ],
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "登录概览",
          kicker: "访问状态",
          copy: "先确认是谁在操作控制台，再决定是否继续执行账号管理。",
          content: `
            ${callout({
              title: providers.auth_enabled ? "控制台认证已启用" : "当前未启用控制台认证",
              copy: providers.auth_enabled
                ? "浏览器会话、数据库用户和 API Key 兼容模式会共同决定当前会话能执行哪些人工操作。"
                : "当前是开放访问模式。页面可以直接访问，但高风险操作仍应谨慎使用。",
              pills: [
                pill(`会话登录 ${booleanWord(providers.session_enabled)}`, providers.session_enabled ? "positive" : "outline"),
                pill(`数据库账号 ${booleanWord(providers.database_backed)}`, providers.database_backed ? "info" : "outline"),
                pill(`API Key ${booleanWord(providers.api_key_compatibility_enabled)}`, providers.api_key_compatibility_enabled ? "warning" : "outline"),
              ],
            })}
            ${kvList([
              ["当前身份", session.identity || "未登录", readableState(session.auth_source || "anonymous")],
              ["当前角色", readableState(session.role || "anonymous"), canAdmin ? "具备账号管理权限" : "当前只能查看，不能改动账号或角色"],
              ["启用管理员数", formatNumber(operatorUsers.enabled_admin_count, 0), providers.database_backed ? "来自 operator_users 表" : "当前是内存模式"],
            ])}
          `,
        })}
      </div>

      <div class="span-8">
        ${surfaceCard({
          title: "账号记录",
          kicker: "账号列表",
          copy: canAdmin
            ? "管理员可以在这里创建、停用、改角色、重置密码或删除控制台账号。"
            : "当前账号没有管理员权限，因此这里只能查看账户概览，不能改动。",
          content: `
            ${operatorUsersError ? notice(operatorUsersError, "warning") : ""}
            ${renderCreateForm(canAdmin)}
            ${renderChangeRoleForm(canAdmin)}
            ${renderResetPasswordForm(canAdmin)}
            ${responsiveTable(
              ["用户名", "角色", "账号状态", "最近登录", "最近更新", "操作"],
              users.map((user) => [
                `<div><strong>${escapeHtml(user.username || "待确认")}</strong><div class="table-meta mono">${middleEllipsis(user.user_id)}</div></div>`,
                `${pill(readableState(user.role || "unknown"), user.role === "admin" ? "danger" : user.role === "operator" ? "info" : "outline")}`,
                `<div>${pill(user.enabled ? "已启用" : "已停用", user.enabled ? "positive" : "warning")}${user.protected_last_admin ? '<div class="table-meta">当前最后一个启用中的管理员</div>' : ""}</div>`,
                formatMaybeTimestamp(user.last_login_at),
                formatMaybeTimestamp(user.updated_at || user.created_at),
                renderUserActions(user, canAdmin, session),
              ]),
              "当前暂无控制台账号。",
              users.map((user) => ({
                kicker: "控制台账号",
                title: `${user.username || "待确认"} | ${readableState(user.role || "unknown")}`,
                meta: middleEllipsis(user.user_id),
                tone: user.enabled ? "positive" : "warning",
                badge: pill(user.enabled ? "已启用" : "已停用", user.enabled ? "positive" : "warning"),
                fields: [
                  { label: "最近登录", value: formatMaybeTimestamp(user.last_login_at) },
                  { label: "最近更新", value: formatMaybeTimestamp(user.updated_at || user.created_at), meta: user.protected_last_admin ? "当前最后一个启用中的管理员" : "" },
                ],
                details: [
                  { label: "账号角色", value: readableState(user.role || "unknown") },
                  { label: "账号状态", value: user.enabled ? "已启用" : "已停用" },
                  { label: "账号标识", value: middleEllipsis(user.user_id) },
                ],
                detailLabel: "展开账号详情",
                action: renderUserActions(user, canAdmin, session),
              }))
            )}
          `,
        })}
      </div>
    </div>
  `;
}

// #41 修复：原本 <form id="operatorCreateForm"> 是整个前端里唯一通过 DOM id
// 绑定事件的表单（app.js init() 用 form.id === "operatorCreateForm" 拦 submit），
// 其它按钮 / 表单全部走 data-action 分发。改成走相同的 data-action 名字，让事
// 件分发只读 dataset，不再依赖 DOM id 字符串匹配。
//
// 这里仍然用 form 的 submit 事件（而非 click + data-action），是为了保留浏览
// 器原生的"按 Enter 提交 / 浏览器密码管理器自动填充"行为；只是把"我是哪个
// action"的判定从 form.id 改成 form.dataset.action，和其它 dispatchAction 分发
// 路径在概念上对齐。
function renderCreateForm(canAdmin) {
  return `
    <form data-action="submit-create-operator" class="field-grid">
      <div class="panel-head">
        <div>
          <h3>创建新账号</h3>
          <p class="meta-copy">用于增加只读、操作员或管理员账号。</p>
        </div>
        ${canAdmin ? pill("可操作", "positive") : pill("仅管理员可操作", "warning")}
      </div>
      <div class="panel-grid">
        <div class="span-3">
          <label class="field-label" for="operatorCreateUsername">用户名</label>
          <input id="operatorCreateUsername" type="text" placeholder="例如 trader01" ${canAdmin ? "required" : "disabled"} autocomplete="username">
        </div>
        <div class="span-3">
          <label class="field-label" for="operatorCreatePassword">初始密码</label>
          <input id="operatorCreatePassword" type="password" placeholder="请输入初始密码" ${canAdmin ? "required" : "disabled"} autocomplete="new-password">
        </div>
        <div class="span-3">
          <label class="field-label" for="operatorCreateRole">角色</label>
          <select id="operatorCreateRole" ${canAdmin ? "required" : "disabled"}>
            <option value="viewer">只读用户</option>
            <option value="operator">操作员</option>
            <option value="admin">管理员</option>
          </select>
        </div>
        <div class="span-3">
          <label class="field-label" for="operatorCreateEnabled">启用状态</label>
          <select id="operatorCreateEnabled" ${canAdmin ? "required" : "disabled"}>
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

function renderChangeRoleForm(canAdmin) {
  // #29 同系列修复：原本这里用 windowRef.prompt() 采集新角色，改为统一的下拉表单。
  // 每行“改角色”按钮会把目标用户名预填到 #changeRoleUsername，再由此表单发起 PATCH。
  return `
    <form id="changeRoleForm" class="field-grid" data-action="confirm-change-user-role">
      <div class="panel-head">
        <div>
          <h3>修改账号角色</h3>
          <p class="meta-copy">先在下方账号列表点击“改角色”预填用户名，再在这里选择新角色后确认。</p>
        </div>
        ${canAdmin ? pill("可操作", "positive") : pill("仅管理员可操作", "warning")}
      </div>
      <div class="panel-grid">
        <div class="span-4">
          <label class="field-label" for="changeRoleUsername">目标用户名</label>
          <input id="changeRoleUsername" type="text" placeholder="点击账号行“改角色”后自动填充" ${canAdmin ? "required" : "disabled"} readonly>
        </div>
        <div class="span-4">
          <label class="field-label" for="changeRoleValue">新角色</label>
          <select id="changeRoleValue" ${canAdmin ? "required" : "disabled"}>
            <option value="viewer">只读用户</option>
            <option value="operator">操作员</option>
            <option value="admin">管理员</option>
          </select>
        </div>
        <div class="span-4 stack-actions">
          <button id="changeRoleConfirmButton" class="primary-button" type="submit" ${canAdmin ? "" : "disabled"}>确认修改角色</button>
        </div>
      </div>
    </form>
  `;
}

function renderResetPasswordForm(canAdmin) {
  // #29 核心修复：原本 resetOperatorPassword 通过 windowRef.prompt() 弹窗采集明文密码，
  // 密码在 prompt 输入框里不会被掩码，对审计/敏感部署不合适。改为专用 <input type="password">。
  return `
    <form id="resetPasswordForm" class="field-grid" data-action="confirm-reset-user-password">
      <div class="panel-head">
        <div>
          <h3>重置账号密码</h3>
          <p class="meta-copy">先在下方账号列表点击“重置密码”预填用户名，再在这里输入新密码后确认。</p>
        </div>
        ${canAdmin ? pill("可操作", "positive") : pill("仅管理员可操作", "warning")}
      </div>
      <div class="panel-grid">
        <div class="span-4">
          <label class="field-label" for="resetPasswordUsername">目标用户名</label>
          <input id="resetPasswordUsername" type="text" placeholder="点击账号行“重置密码”后自动填充" ${canAdmin ? "required" : "disabled"} readonly>
        </div>
        <div class="span-4">
          <label class="field-label" for="resetPasswordValue">新密码</label>
          <input id="resetPasswordValue" type="password" autocomplete="new-password" placeholder="请输入新密码" ${canAdmin ? "required" : "disabled"}>
        </div>
        <div class="span-4 stack-actions">
          <button id="resetPasswordConfirmButton" class="primary-button" type="submit" ${canAdmin ? "" : "disabled"}>确认重置密码</button>
        </div>
      </div>
    </form>
  `;
}

function renderUserActions(user, canAdmin, session = {}) {
  if (!canAdmin) return '<span class="table-meta">无管理权限</span>';
  const username = user.username || "";
  const isSessionUser = session.auth_source === "session" && username && username === session.identity;
  const lastAdminReason = user.protected_last_admin ? "系统至少需要保留一个启用中的管理员。" : "";
  const selfDisableReason = isSessionUser && user.enabled ? "不能停用当前登录的管理员账号。" : "";
  const selfDeleteReason = isSessionUser ? "不能删除当前登录的管理员账号。" : "";
  const toggleDisabledReason = lastAdminReason || selfDisableReason;
  const roleDisabledReason = lastAdminReason;
  const deleteDisabledReason = lastAdminReason || selfDeleteReason;
  return `
    <div class="table-actions table-actions--compact">
      ${adminActionButton(user.enabled ? "停用" : "启用", "toggle-user", username, user.enabled ? "warning-button" : "secondary-button", toggleDisabledReason)}
      ${adminActionButton("改角色", "change-user-role", username, "table-button", roleDisabledReason)}
      ${adminActionButton("重置密码", "reset-user-password", username, "table-button")}
      ${adminActionButton("删除", "delete-user", username, "danger-button", deleteDisabledReason)}
    </div>
  `;
}

function adminActionButton(label, action, value, className, disabledReason = "") {
  const disabled = disabledReason ? " disabled" : "";
  const title = disabledReason ? ` title="${escapeHtml(disabledReason)}"` : "";
  return `<button class="${escapeHtml(className)}" data-action="${escapeHtml(action)}" data-value="${escapeHtml(value)}"${title}${disabled}>${escapeHtml(label)}</button>`;
}
