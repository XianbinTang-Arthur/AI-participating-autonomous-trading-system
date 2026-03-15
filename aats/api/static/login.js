const nodes = {
  form: document.getElementById("loginForm"),
  username: document.getElementById("loginUsername"),
  password: document.getElementById("loginPassword"),
  button: document.getElementById("loginButton"),
  message: document.getElementById("loginMessage"),
  providers: document.getElementById("loginProviders"),
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
    nodes.providers.innerHTML = `
      <div class="fact-grid">
        <div class="fact-row">
          <span class="fact-key">Auth Enabled</span>
          <strong class="fact-value">${payload.auth_enabled ? "yes" : "no"}</strong>
        </div>
        <div class="fact-row">
          <span class="fact-key">Session Login</span>
          <strong class="fact-value">${payload.session_enabled ? "configured" : "not configured"}</strong>
        </div>
        <div class="fact-row">
          <span class="fact-key">Configured Roles</span>
          <strong class="fact-value">${payload.configured_roles?.length ? payload.configured_roles.join(", ") : "-"}</strong>
        </div>
        <div class="fact-row">
          <span class="fact-key">API-Key Compatibility</span>
          <strong class="fact-value">${payload.api_key_compatibility_enabled ? "enabled" : "disabled"}</strong>
        </div>
      </div>
    `;
  } catch (error) {
    updateLoginAvailability({ auth_enabled: false, session_enabled: false });
    nodes.providers.innerHTML = `<div class="empty-state">${escapeHtml(error.message || "Failed to load auth providers.")}</div>`;
  }
}

async function login() {
  nodes.button.disabled = true;
  nodes.button.textContent = "Signing In...";
  setMessage("Signing in...", "info");
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
    setMessage(error.message || "Login failed.", "danger");
    nodes.button.disabled = false;
    nodes.button.textContent = "Sign In";
  }
}

function updateLoginAvailability(payload) {
  const loginAvailable = Boolean(payload.auth_enabled && payload.session_enabled);
  nodes.username.disabled = !loginAvailable;
  nodes.password.disabled = !loginAvailable;
  nodes.button.disabled = !loginAvailable;
  nodes.form.classList.toggle("is-disabled", !loginAvailable);
  if (!payload.auth_enabled) {
    setMessage("Operator auth is disabled. Open /ui directly in local development mode.", "info");
  } else if (!payload.session_enabled) {
    setMessage("Session login is not configured. Add operator session credentials before using the browser console.", "warning");
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
    const detail = typeof payload === "object" && payload !== null && "detail" in payload ? payload.detail : text || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
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
  nodes.message.className = `alert alert-${tone}`;
  nodes.message.textContent = message;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
