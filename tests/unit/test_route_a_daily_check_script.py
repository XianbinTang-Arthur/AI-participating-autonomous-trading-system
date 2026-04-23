"""Regression checks for scripts/ops/route_a_daily_check.sh hardening.

脚本是 bash, 仓库里没有 shell runtime 的 test harness, 强行 PATH-shim
wsl/docker/psql 去端到端跑属于过度工程. 这里沿用 tests/unit/test_deploy_scripts.py
的做法做文本级回归: 确保 "infra/查询失败不能被静默吞成 PASS" 的约束
(psql_q/psql_live 哨兵, docker ps rc 检测, 删除 `${var:-0}` 折叠, 每个
调用点显式识别哨兵) 不被后续修改无意回退.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "route_a_daily_check.sh"
DOC_PATH = (
    REPO_ROOT / "docs" / "operations" / "route_a_observation_window_daily_check.md"
)


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_psql_helpers_emit_err_sentinel_on_failure(script_text: str) -> None:
    assert "readonly PSQL_ERR='__PSQL_ERR__'" in script_text
    assert "is_psql_err()" in script_text
    assert "_psql_run()" in script_text
    # psql_q / psql_live 走统一内部实现
    assert "psql_q()    { _psql_run aats_research" in script_text
    assert "psql_live() { _psql_run aats_live_derivatives" in script_text
    # 捕获 rc + 打印 stderr + 输出哨兵
    assert 'printf \'%s\' "$PSQL_ERR"' in script_text
    assert "psql 查询失败 (rc=%s, db=%s)" in script_text


def test_psql_helpers_no_longer_silently_drop_stderr(script_text: str) -> None:
    # 旧实现直接把 psql 的 stderr 送去 /dev/null, 错误原因完全被吞.
    # 新实现必须把 psql 的 stderr 定向到可回显的临时文件.
    assert 'psql -U admin -d "$db" -tA -c "$*" 2>/dev/null' not in script_text
    assert 'psql -U admin -d "$db" -tA -c "$*" 2>"$err_file"' in script_text


def test_tf_empty_default_to_zero_is_removed(script_text: str) -> None:
    # 原 bug 之一: `tf_empty=${tf_empty:-0}` / `ob_empty=${ob_empty:-0}` 把
    # 查询失败 (空字符串) 折叠成 0, 下游判 "0 = PASS 零饿死". 必须删掉.
    assert "tf_empty=${tf_empty:-0}" not in script_text
    assert "ob_empty=${ob_empty:-0}" not in script_text


def test_every_psql_call_site_handles_err_sentinel(script_text: str) -> None:
    # 所有对 psql_q / psql_live 的捕获赋值必须配套 is_psql_err 判断.
    err_guards = script_text.count("is_psql_err ")
    # check 2 freshness, check 4 task queue, check 5 gap loop (1 次覆盖 3 表),
    # check 6 empty-bar loop (1 次覆盖 2 表), check 7 mode = 5 处.
    assert err_guards >= 5, f"expected ≥5 is_psql_err guards, got {err_guards}"


def test_count_queries_reject_non_numeric_output(script_text: str) -> None:
    # check 5 / 6 的 COUNT(*) 在 psql 正常时必返回一行非负整数. 空串 / 非数字
    # 都视为 infra 异常走 FAIL, 不能走 bash 算术默认 0 的路径.
    assert script_text.count('=~ ^[0-9]+$') >= 2


def test_docker_ps_uses_return_code_guard(script_text: str) -> None:
    # check 1 必须走 rc 分支 + 对 "一条 aats-* 都没返回" 显式 FAIL; 不能再
    # `docker ps 2>/dev/null | grep ...` 静默退化.
    assert "docker_ps_rc=$?" in script_text
    assert "docker ps 查询失败 (rc=" in script_text
    assert "未返回任何 aats-* 容器" in script_text


def test_ops_doc_documents_fail_semantics() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "infra/查询失败语义" in doc
    assert "__PSQL_ERR__" in doc
    assert "数据源本身不可用" in doc
