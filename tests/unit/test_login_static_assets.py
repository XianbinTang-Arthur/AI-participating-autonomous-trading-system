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
