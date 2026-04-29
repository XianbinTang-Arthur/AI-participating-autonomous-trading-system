import { requestJson } from "./modules/api-client.js";

const nodes = {
  form: document.getElementById("loginForm"),
  lead: document.getElementById("loginLead"),
  username: document.getElementById("loginUsername"),
  password: document.getElementById("loginPassword"),
  button: document.getElementById("loginButton"),
  message: document.getElementById("loginMessage"),
};

const DEFAULT_LOGIN_MESSAGE = "请输入账号和密码后继续。";
const DEFAULT_LOGIN_LEAD = "使用已配置的控制台账号登录。登录后可查看当前能否交易、策略判断、委托进展、盈亏变化，以及人工干预入口。";
const ROLE_LABELS = {
  admin: "管理员",
  operator: "操作员",
  read_only: "只读",
  readonly: "只读",
  viewer: "只读",
};
const loginReason = new URLSearchParams(window.location.search).get("reason") || "";
const state = {
  loginAvailable: false,
};

init();

async function init() {
  nodes.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!nodes.form.checkValidity()) {
      showFormValidationMessage();
      nodes.form.reportValidity?.();
      return;
    }
    void login();
  });
  [nodes.username, nodes.password].forEach((field) => {
    field.addEventListener("invalid", showFormValidationMessage);
    field.addEventListener("input", refreshFormHint);
  });
  await renderProviders();
}

async function renderProviders() {
  try {
    const payload = await requestJson("/auth/providers");
    updateLoginLead(payload);
    updateLoginAvailability(payload);
  } catch (error) {
    updateLoginAvailability({ auth_enabled: false, session_enabled: false });
    setMessage(localizeLoginError(error?.message || "登录能力检查失败。"), "danger");
  }
}

async function login() {
  nodes.button.disabled = true;
  nodes.button.textContent = "登录中…";
  setMessage("正在验证账号和权限，请稍候。", "info");
  try {
    await requestJson("/auth/login", {
      method: "POST",
      body: {
        username: nodes.username.value.trim(),
        password: nodes.password.value,
      },
    });
    window.location.assign("/ui");
  } catch (error) {
    setMessage(localizeLoginError(error?.message || "登录失败，请检查账号、密码和权限。"), "danger");
    nodes.button.disabled = !state.loginAvailable;
    nodes.button.textContent = "登录";
  }
}

function updateLoginAvailability(payload) {
  const blockedReason = String(payload.auth_blocked_reason || "").trim();
  const transportCompatible = payload.transport_compatible !== false;
  const loginAvailable = Boolean(payload.auth_enabled && payload.session_enabled && transportCompatible && !blockedReason);
  state.loginAvailable = loginAvailable;
  nodes.username.disabled = !loginAvailable;
  nodes.password.disabled = !loginAvailable;
  nodes.button.disabled = !loginAvailable;
  nodes.form.classList.toggle("is-disabled", !loginAvailable);

  if (!payload.auth_enabled) {
    setMessage("当前环境没有启用登录认证。本地开发模式下可直接访问 /ui。", "info");
    return;
  }
  if (!payload.session_enabled) {
    setMessage("当前没有启用浏览器会话登录，请先补齐 operator session 配置。", "warning");
    return;
  }
  if (!transportCompatible || blockedReason) {
    setMessage(localizeLoginError(blockedReason || "operator_https_required_for_secure_session"), "warning");
    return;
  }
  if (loginReason) {
    setMessage(localizeLoginError(loginReason), "warning");
    return;
  }
  setMessage(DEFAULT_LOGIN_MESSAGE, "info");
}

function updateLoginLead(payload) {
  if (!nodes.lead) return;
  if (!payload.auth_enabled) {
    nodes.lead.textContent = "当前环境未启用登录认证。本地开发模式可直接访问控制台。";
    return;
  }
  const roleLabels = Array.from(new Set((payload.configured_roles || []).map(roleLabel).filter(Boolean)));
  if (!roleLabels.length) {
    nodes.lead.textContent = DEFAULT_LOGIN_LEAD;
    return;
  }
  const roleText = `${formatRoleList(roleLabels)}账号`;
  nodes.lead.textContent = `使用${roleText}登录。登录后可查看当前能否交易、策略判断、委托进展、盈亏变化，以及人工干预入口。`;
}

function roleLabel(role) {
  return ROLE_LABELS[String(role || "").toLowerCase().replaceAll("-", "_")] || "";
}

function formatRoleList(labels) {
  if (labels.length <= 1) return labels[0] || "";
  if (labels.length === 2) return labels.join("或");
  return `${labels.slice(0, -1).join("、")}或${labels.at(-1)}`;
}

function showFormValidationMessage() {
  if (!state.loginAvailable) return;
  const usernameMissing = !nodes.username.value.trim();
  const passwordMissing = !nodes.password.value;
  if (usernameMissing && passwordMissing) {
    setMessage("请先填写用户名和密码。", "warning");
    return;
  }
  if (usernameMissing) {
    setMessage("请先填写用户名。", "warning");
    return;
  }
  if (passwordMissing) {
    setMessage("请先填写密码。", "warning");
  }
}

function refreshFormHint() {
  if (!state.loginAvailable) return;
  const usernameMissing = !nodes.username.value.trim();
  const passwordMissing = !nodes.password.value;
  if (usernameMissing || passwordMissing) {
    showFormValidationMessage();
    return;
  }
  setMessage(DEFAULT_LOGIN_MESSAGE, "info");
}

function setMessage(message, tone) {
  nodes.message.className = `notice-card tone-${tone}`;
  nodes.message.textContent = message;
}

function localizeLoginError(message) {
  const text = String(message || "").trim();
  if (text === "operator_auth_required") return "当前操作需要先登录。";
  if (text === "operator_https_required_for_secure_session") {
    return "当前入口使用 HTTP，安全会话只能通过 HTTPS 建立。请使用 HTTPS 访问控制台。";
  }
  if (text === "operator_login_failed") return "用户名或密码错误。";
  if (text === "operator_session_auth_not_configured") return "当前没有启用浏览器会话登录。";
  if (/failed to fetch|networkerror|network error|load failed/i.test(text)) {
    return "登录接口不可达，请先确认服务已启动。";
  }
  return text || "登录失败，请稍后重试。";
}
