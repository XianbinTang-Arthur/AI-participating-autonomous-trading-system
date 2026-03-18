const nodes = {
  form: document.getElementById("loginForm"),
  username: document.getElementById("loginUsername"),
  password: document.getElementById("loginPassword"),
  button: document.getElementById("loginButton"),
  message: document.getElementById("loginMessage"),
};

init();

async function init() {
  await renderProviders();
  nodes.form.addEventListener("submit", (event) => {
    event.preventDefault();
    void login();
  });
}

async function renderProviders() {
  try {
    const payload = await requestJson("/auth/providers");
    updateLoginAvailability(payload);
  } catch (error) {
    updateLoginAvailability({ auth_enabled: false, session_enabled: false });
    setMessage(error.message || "登录能力检查失败。", "danger");
  }
}

async function login() {
  nodes.button.disabled = true;
  nodes.button.textContent = "登录中…";
  setMessage("正在验证账号与权限…", "info");
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
    setMessage(error.message || "登录失败，请检查账号、密码和权限。", "danger");
    nodes.button.disabled = false;
    nodes.button.textContent = "登录";
  }
}

function updateLoginAvailability(payload) {
  const loginAvailable = Boolean(payload.auth_enabled && payload.session_enabled);
  nodes.username.disabled = !loginAvailable;
  nodes.password.disabled = !loginAvailable;
  nodes.button.disabled = !loginAvailable;
  nodes.form.classList.toggle("is-disabled", !loginAvailable);

  if (!payload.auth_enabled) {
    setMessage("当前环境没有启用登录认证。本地开发模式下可直接访问 /ui。", "info");
  } else if (!payload.session_enabled) {
    setMessage("当前没有启用浏览器会话登录，请先补齐 operator session 配置。", "warning");
  } else {
    setMessage("请输入账号和密码后继续。", "info");
  }
}

async function requestJson(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  const payload = text ? safeJsonParse(text) : null;
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? payload.detail
        : text || response.statusText;
    throw new Error(localizeLoginError(typeof detail === "string" ? detail : JSON.stringify(detail)));
  }
  return payload;
}

function safeJsonParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function setMessage(message, tone) {
  nodes.message.className = `notice-card tone-${tone}`;
  nodes.message.textContent = message;
}

function localizeLoginError(message) {
  const text = String(message || "").trim();
  if (text === "operator_auth_required") return "当前操作需要先登录。";
  if (text === "operator_login_failed") return "用户名或密码错误。";
  if (text === "operator_session_auth_not_configured") return "当前没有启用浏览器会话登录。";
  return text;
}
