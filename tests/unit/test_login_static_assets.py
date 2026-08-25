from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_login_script_localizes_network_fetch_errors_in_chinese() -> None:
    text = (REPO_ROOT / "aats" / "api" / "static" / "login.js").read_text(encoding="utf-8")

    assert "登录接口不可达，请先确认服务已启动。" in text
    assert "operator_login_failed" in text
    assert "failed to fetch" in text.lower()


def test_login_script_surfaces_https_requirement_for_secure_sessions() -> None:
    text = (REPO_ROOT / "aats" / "api" / "static" / "login.js").read_text(encoding="utf-8")

    assert "operator_https_required_for_secure_session" in text
    assert "当前入口使用 HTTP，安全会话只能通过 HTTPS 建立。请使用 HTTPS 访问控制台。" in text


def test_login_page_exposes_runtime_role_copy_target() -> None:
    html = (REPO_ROOT / "aats" / "api" / "static" / "login.html").read_text(encoding="utf-8")
    script = (REPO_ROOT / "aats" / "api" / "static" / "login.js").read_text(encoding="utf-8")

    assert 'id="loginLead"' in html
    assert "configured_roles" in script
    assert "使用${roleText}登录" in script


def test_login_page_has_mobile_width_guards() -> None:
    css = (REPO_ROOT / "aats" / "api" / "static" / "app.css").read_text(encoding="utf-8")

    assert ".login-shell" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert ".login-panel" in css
    assert "width: 100%;" in css
    assert "max-width: 560px;" in css
    assert ".login-copy h1,\n.login-copy .lead,\n.login-panel .notice-card" in css
    assert "overflow-wrap: anywhere;" in css


def test_login_script_surfaces_form_validation_in_page_notice() -> None:
    text = (REPO_ROOT / "aats" / "api" / "static" / "login.js").read_text(encoding="utf-8")

    assert 'addEventListener("invalid", showFormValidationMessage)' in text
    assert "请先填写用户名和密码。" in text
    assert "请先填写用户名。" in text
    assert "请先填写密码。" in text


def test_readme_distinguishes_local_http_from_disabled_live_profiles() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8001/ui" in text
    assert "http://127.0.0.1:8001/healthz" in text
    assert "Phase 3F" in text
    assert "硬禁用标准部署和本地启动入口的所有 live profile" in text
    assert "没有 override" in text
    assert "非 loopback 地址" in text
    assert "http://127.0.0.1:8011/ui" not in text
