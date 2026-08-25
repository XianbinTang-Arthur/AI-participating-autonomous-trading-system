from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "aats" / "api" / "static"


def _run_node_json(script: str) -> object:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip())
    return json.loads(result.stdout)


class DashboardTermsLocalizationTests(unittest.TestCase):
    def test_readable_state_humanizes_runtime_reason_codes(self) -> None:
        terms_uri = (STATIC_DIR / "modules" / "terms.js").resolve().as_uri()
        output = _run_node_json(
            f"""
            import {{ readableState }} from {json.dumps(terms_uri)};
            console.log(JSON.stringify({{
              approval: readableState("approved_for_non_protective_execution"),
              claimed: readableState("execution_submit_command_claimed_without_terminal_order_ack"),
              advisory: readableState("primary_candidate_advisory_only_suppressed_after_approval"),
            }}));
            """
        )

        self.assertEqual(output["approval"], "已批准非保护性执行")
        self.assertEqual(output["claimed"], "提交命令已声明，但缺少终态订单确认")
        self.assertEqual(output["advisory"], "主候选已批准，但资金分配压零，仅保留为建议")

    def test_readable_state_localizes_derivatives_exposure_limit_reasons(self) -> None:
        terms_uri = (STATIC_DIR / "modules" / "terms.js").resolve().as_uri()
        output = _run_node_json(
            f"""
            import {{ readableState }} from {json.dumps(terms_uri)};
            console.log(JSON.stringify({{
              long: readableState("risk_max_long_notional_exceeded"),
              short: readableState("risk_max_short_notional_exceeded"),
              gross: readableState("risk_max_gross_notional_exceeded"),
              net: readableState("risk_max_net_notional_exceeded"),
            }}));
            """
        )

        self.assertEqual(output["long"], "多头名义敞口超过上限，不能继续扩大多头仓位")
        self.assertEqual(output["short"], "空头名义敞口超过上限，不能继续扩大空头仓位")
        self.assertEqual(output["gross"], "多空合计名义敞口超过上限，不能继续扩大总敞口")
        self.assertEqual(output["net"], "多空相抵后的净名义敞口超过上限，不能继续扩大净敞口")

    def test_localize_error_handles_pre_upgrade_rdp_mixed_language_flag(self) -> None:
        terms_uri = (STATIC_DIR / "modules" / "terms.js").resolve().as_uri()
        output = _run_node_json(
            f"""
            import {{ localizeError }} from {json.dumps(terms_uri)};
            console.log(JSON.stringify(localizeError("治理目标参数与当前实盘 active 参数不一致")));
            """
        )

        self.assertEqual(output, "治理目标参数与当前实盘参数不一致。")

    def test_readable_state_preserves_symbols_and_compacts_known_ids(self) -> None:
        terms_uri = (STATIC_DIR / "modules" / "terms.js").resolve().as_uri()
        output = _run_node_json(
            f"""
            import {{ readableState }} from {json.dumps(terms_uri)};
            console.log(JSON.stringify({{
              symbol: readableState("BTC-USDT-SWAP"),
              decision: readableState("decision_1234567890abcdef1234567890abcdef"),
            }}));
            """
        )

        self.assertEqual(output["symbol"], "BTC-USDT-SWAP")
        self.assertEqual(output["decision"], "决策 decision_1...90abcdef")

    def test_raw_json_defaults_to_collapsed_debug_details(self) -> None:
        formatters_uri = (STATIC_DIR / "modules" / "formatters.js").resolve().as_uri()
        output = _run_node_json(
            f"""
            import {{ rawJson }} from {json.dumps(formatters_uri)};
            const html = rawJson({{ alpha: 1 }});
            console.log(JSON.stringify({{
              hasDetails: html.includes('<details class="debug-json">'),
              hasSummary: html.includes('<summary>展开排障原文</summary>'),
              hasPre: html.includes('<pre class="raw-json">'),
            }}));
            """
        )

        self.assertTrue(output["hasDetails"])
        self.assertTrue(output["hasSummary"])
        self.assertTrue(output["hasPre"])


if __name__ == "__main__":
    unittest.main()
