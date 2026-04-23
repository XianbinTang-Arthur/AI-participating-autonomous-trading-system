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
    # check 2 freshness, check 4a task queue, check 4b contiguous streak,
    # check 5 gap loop (1 次覆盖 3 表), check 6 empty-bar loop (1 次覆盖 2 表),
    # check 7 mode = 6 处.
    assert err_guards >= 6, f"expected ≥6 is_psql_err guards, got {err_guards}"


def test_check4_distinguishes_contiguous_streak(script_text: str) -> None:
    # 4b 必须用 last_done CTE 单独算"自上次 done 之后的非 done 数", 即末段连续
    # streak; 单纯 count 24h 总非 done 数 (4a) 抓不到 sparse vs contiguous 差异.
    assert "WITH last_done AS" in script_text
    assert "MAX(requested_at) AS last_done_at" in script_text
    # rolling workflow 白名单必须显式列出, 不能漏 microstructure_silver_15m
    # 或 candles_rolling_15m
    assert (
        "workflow IN ('microstructure_silver_15m', 'candles_rolling_15m')"
        in script_text
    )
    # last_done IS NULL 分支必须存在: 全新部署 / 该 workflow 从未成功过, 所有
    # 24h 非 done 都算 streak (worst case)
    assert "d.last_done_at IS NULL OR q.requested_at > d.last_done_at" in script_text


def test_check4_streak_thresholds(script_text: str) -> None:
    # streak ≥ 2 → WARN, ≥ 3 → FAIL. 阈值不能松绑, 否则失去早期信号意义.
    assert "max_streak -ge 3" in script_text
    assert "max_streak -ge 2" in script_text
    # streak FAIL 必须 reset 观察窗 (与 §3.5 协议一致)
    assert "自愈链路断" in script_text and "观察窗需重置" in script_text
    # streak WARN 必须引导 operator 看 log_tail (与 §3.5 协议一致)
    assert "自愈未生效" in script_text and "log_tail" in script_text


def test_check4_contiguous_query_scoped_to_24h(script_text: str) -> None:
    # 末段 streak 也必须限制在 24h 内, 与 4a 同窗口; 否则陈年单次失败会让 streak
    # 永远卡在 1, 失真.
    assert script_text.count("INTERVAL '24 hours'") >= 4  # check 4a + 4b + check 6 ×2


def test_ops_doc_documents_contiguous_streak_semantics() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    # check 4 表格行必须区分 4a / 4b
    assert "末段连续未 done streak" in doc
    assert "streak ≥ 2" in doc and "streak ≥ 3" in doc
    # threshold rationale 行必须解释 4b
    assert "4b" in doc
    # v0.1 占位 (留 v0.2 迭代点) 必须删除, 不能让 doc / 实现脱节
    assert "留 v0.2 迭代点" not in doc
    assert "**未**区分 contiguous vs sparse" not in doc


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


def test_script_emits_machine_readable_json_summary(script_text: str) -> None:
    # 机读 JSON artifact 写到 artifacts/route_a_observation_window/<date>.json;
    # 字段至少覆盖 generated_at / window_start / window_target / overall /
    # exit_code / warn_count / fail_count / checks, 自动化 (PM loop 等) 才能
    # stable consume 不用 scrape 终端文本.
    assert 'AATS_SKIP_DAILY_CHECK_JSON' in script_text
    assert '_json_dir="artifacts/route_a_observation_window"' in script_text
    assert '"${_json_dir}/${CHECK_DATE}.json"' in script_text
    for field in (
        '"generated_at":',
        '"window_start":',
        '"window_target":',
        '"overall":',
        '"exit_code":',
        '"warn_count":',
        '"fail_count":',
        '"checks":',
    ):
        assert field in script_text, f"missing JSON field emitter: {field}"


def test_script_json_verdicts_use_stable_string_values(script_text: str) -> None:
    # automation 会 switch 到这三个字面量上, 不能随手改成 "PASS" / "FAIL" 等大写
    # 变体或加前缀 — 一改就是一次消费者协议破坏.
    assert 'OVERALL="fail"' in script_text
    assert 'OVERALL="pass_with_warn"' in script_text
    assert 'OVERALL="pass"' in script_text


def test_script_records_status_for_every_pass_warn_fail(script_text: str) -> None:
    # pass/warn/fail 三个 helper 都必须 _record_status, 否则 JSON checks
    # 数组会缺条目, 下游告警 / 看板判定失真.
    assert "_record_status() {" in script_text
    assert "pass()  { _record_status pass " in script_text
    assert "warn()  { _record_status warn " in script_text
    assert "fail()  { _record_status fail " in script_text
    # step() 要更新 CURRENT_SECTION 供 _record_status 取到"所在 check 段"
    assert 'step()  { CURRENT_SECTION="$*";' in script_text


def test_script_json_written_before_exit(script_text: str) -> None:
    # JSON 必须在最终 `exit "$EXIT_CODE"` 之前写; 否则 FAIL 分支会提前退出,
    # artifacts/ 里的 JSON 永远停留在上一天的 PASS 状态.
    json_write_idx = script_text.find('_json_file="${_json_dir}/${CHECK_DATE}.json"')
    exit_idx = script_text.find('exit "$EXIT_CODE"')
    assert json_write_idx > 0 and exit_idx > 0
    assert json_write_idx < exit_idx, "JSON writer must run before final exit"


def test_script_json_escape_handles_quote_and_backslash(script_text: str) -> None:
    # 不能依赖 python/jq (项目禁止额外 runtime 依赖), 但至少要转义 \ 和 ";
    # 少转一样会产生破损 JSON.
    assert "_json_escape()" in script_text
    assert r's=${s//\\/\\\\}' in script_text  # \ → \\
    assert r's=${s//\"/\\\"}' in script_text  # " → \"


def test_ops_doc_documents_json_artifact() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    # 文档必须告诉 operator JSON 存在 + 字段表
    assert "<date>.json" in doc or "<YYYY-MM-DD>.json" in doc or ".json" in doc
    assert "machine-readable" in doc.lower() or "机读" in doc or "机器看" in doc
    # 字段必须逐条列出, 否则 operator / automation 作者只能猜
    for field in (
        "generated_at",
        "window_start",
        "window_target",
        "overall",
        "exit_code",
        "warn_count",
        "fail_count",
        "checks",
    ):
        assert field in doc, f"ops doc missing JSON field: {field}"
    # 文档必须明说同日覆盖语义, 否则 operator 以为 JSON 也是 append
    assert "覆盖" in doc
    # 必须指出 AATS_SKIP_DAILY_CHECK_JSON 的开关, 和 LOG 对称
    assert "AATS_SKIP_DAILY_CHECK_JSON" in doc
