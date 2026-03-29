from __future__ import annotations

from typing import Any


TYPE_LABELS: dict[str, str] = {
    "1": "transfer",
    "2": "trade",
    "3": "delivery",
    "4": "forced_repayment",
    "5": "liquidation",
    "6": "margin_transfer",
    "7": "interest_deduction",
    "8": "funding_fee",
    "9": "adl",
    "10": "clawback",
    "11": "system_token_conversion",
    "12": "strategy_transfer",
    "13": "ddh",
}

SUBTYPE_LABELS: dict[str, str] = {
    "1": "buy",
    "2": "sell",
    "3": "open_long",
    "4": "open_short",
    "5": "close_long",
    "6": "close_short",
    "9": "interest_deduction",
    "11": "transfer_in",
    "12": "transfer_out",
    "100": "partial_liquidation_close_long",
    "101": "partial_liquidation_close_short",
    "102": "partial_liquidation_buy",
    "103": "partial_liquidation_sell",
    "104": "liquidation_long",
    "105": "liquidation_short",
    "106": "liquidation_buy",
    "107": "liquidation_sell",
    "110": "auto_buy_or_liquidation_transfer_in",
    "111": "auto_sell_or_liquidation_transfer_out",
    "112": "delivery_long",
    "113": "delivery_short",
    "117": "delivery_or_exercise_clawback",
    "118": "system_token_conversion_transfer_in",
    "119": "system_token_conversion_transfer_out",
    "125": "adl_close_long",
    "126": "adl_close_short",
    "127": "adl_buy",
    "128": "adl_sell",
    "131": "ddh_buy",
    "132": "ddh_sell",
    "160": "manual_margin_increase",
    "161": "manual_margin_decrease",
    "162": "auto_margin_increase",
    "170": "exercised",
    "171": "counterparty_exercised",
    "172": "expired_otm",
    "173": "funding_fee_expense",
    "174": "funding_fee_income",
    "200": "system_transfer_in",
    "201": "manual_transfer_in",
    "202": "system_transfer_out",
    "203": "manual_transfer_out",
    "204": "block_trade_buy",
    "205": "block_trade_sell",
    "206": "block_trade_open_long",
    "207": "block_trade_open_short",
    "208": "block_trade_close_long",
    "209": "block_trade_close_short",
    "210": "one_click_borrow_manual_borrow",
    "211": "one_click_borrow_manual_repay",
    "212": "one_click_borrow_auto_borrow",
    "213": "one_click_borrow_auto_repay",
    "220": "usdt_option_buy_transfer_in",
    "221": "usdt_option_buy_transfer_out",
    "224": "one_click_debt_repay_buy",
    "225": "one_click_debt_repay_sell",
    "236": "small_conversion_buy",
    "237": "small_conversion_sell",
    "250": "perpetual_profit_sharing_expense",
    "251": "perpetual_profit_sharing_refund",
    "280": "spot_profit_sharing_expense",
    "281": "spot_profit_sharing_refund",
}


def okx_bill_semantic_group(*, bill_type: str, sub_type: str) -> str:
    if sub_type in {"173", "174"} or bill_type == "8":
        return "funding_fee"
    if bill_type == "7" or sub_type in {"9", "210", "211", "212", "213"}:
        return "interest_or_borrow"
    if bill_type in {"1", "6", "11", "12"} or sub_type in {"11", "12", "118", "119", "160", "161", "162", "200", "201", "202", "203", "220", "221"}:
        return "transfer_or_margin_movement"
    if bill_type in {"5", "9", "10"} or sub_type in {"100", "101", "102", "103", "104", "105", "106", "107", "110", "111", "117", "125", "126", "127", "128"}:
        return "liquidation_or_adl"
    if bill_type == "3" or sub_type in {"112", "113", "170", "171", "172"}:
        return "delivery_or_exercise"
    if bill_type in {"2", "13"} or sub_type in {"1", "2", "3", "4", "5", "6", "131", "132", "204", "205", "206", "207", "208", "209", "224", "225", "236", "237"}:
        return "trade_execution"
    return "other"


def describe_okx_bill(*, bill_type: str, sub_type: str, currency: str | None = None) -> dict[str, Any]:
    type_label = TYPE_LABELS.get(bill_type, f"type_{bill_type}")
    sub_type_label = SUBTYPE_LABELS.get(sub_type, f"sub_type_{sub_type}")
    semantic_group = okx_bill_semantic_group(bill_type=bill_type, sub_type=sub_type)
    human_label = f"{type_label}:{sub_type_label}"
    if currency not in {None, ""}:
        human_label = f"{human_label}:{currency}"
    return {
        "type_label": type_label,
        "sub_type_label": sub_type_label,
        "semantic_group": semantic_group,
        "human_label": human_label,
    }


def enrich_okx_bill_category(*, bill_type: str, sub_type: str, currency: str, count: int) -> dict[str, Any]:
    description = describe_okx_bill(bill_type=bill_type, sub_type=sub_type, currency=currency)
    return {
        "type": bill_type,
        "sub_type": sub_type,
        "currency": currency,
        "count": count,
        **description,
    }


def explain_okx_bills_for_reconciliation(
    *,
    summary: dict[str, Any],
    mismatch_categories: list[str],
    mismatch_reasons: list[str],
) -> list[dict[str, Any]]:
    rows = summary.get("top_categories", [])
    if not isinstance(rows, list) or not rows:
        return []
    explanations: list[dict[str, Any]] = []
    for row in rows[:4]:
        if not isinstance(row, dict):
            continue
        bill_type = str(row.get("type") or "unknown")
        sub_type = str(row.get("sub_type") or row.get("subType") or "unknown")
        currency = str(row.get("currency") or row.get("ccy") or "")
        description = describe_okx_bill(bill_type=bill_type, sub_type=sub_type, currency=currency)
        semantic_group = str(row.get("semantic_group") or description["semantic_group"] or "")
        human_label = str(row.get("human_label") or description["human_label"] or "")
        likely_explains: list[str] = []
        why = ""
        if semantic_group in {"funding_fee", "interest_or_borrow", "transfer_or_margin_movement"}:
            if any(
                category in mismatch_categories
                for category in (
                    "external_manual_activity_detected",
                    "local_balance_divergence",
                    "exchange_bills_activity_available",
                )
            ):
                likely_explains.append("balance_divergence")
                why = "This bill category can change account cash balances without requiring a locally recorded order fill."
        elif semantic_group in {"liquidation_or_adl", "delivery_or_exercise"}:
            if any(
                category in mismatch_categories
                for category in (
                    "local_balance_divergence",
                    "local_position_divergence",
                    "external_manual_activity_detected",
                    "exchange_bills_activity_available",
                )
            ):
                likely_explains.extend(["balance_divergence", "position_divergence"])
                why = "This bill category can affect both position inventory and account balances on the exchange side."
        elif semantic_group == "trade_execution":
            if any(
                reason in mismatch_reasons
                for reason in (
                    "local_exchange_fill_set_diverges_from_exchange_fill_set",
                    "local_position_differs_from_exchange_position",
                    "local_open_orders_diverge_from_exchange_open_orders",
                )
            ) or any(
                category in mismatch_categories
                for category in (
                    "external_manual_activity_detected",
                    "exchange_bills_activity_available",
                )
            ):
                likely_explains.extend(["fill_or_order_divergence", "position_divergence"])
                why = "This bill category is trade-linked and may correspond to exchange-side execution activity missing from the local chain."
        if not likely_explains:
            continue
        operator_case, operator_action = suggested_operator_bill_handling(
            semantic_group=semantic_group,
            mismatch_categories=mismatch_categories,
            mismatch_reasons=mismatch_reasons,
            likely_explains=likely_explains,
        )
        explanations.append(
            {
                "title": human_label,
                "semantic_group": semantic_group,
                "count": row.get("count"),
                "currency": currency or row.get("currency"),
                "likely_explains": likely_explains,
                "operator_case": operator_case,
                "operator_action": operator_action,
                "why_it_matters": why,
            }
        )
    return explanations


def suggested_operator_bill_handling(
    *,
    semantic_group: str,
    mismatch_categories: list[str],
    mismatch_reasons: list[str],
    likely_explains: list[str],
) -> tuple[str, str]:
    category_set = set(mismatch_categories)
    reason_set = set(mismatch_reasons)

    if (
        "local_open_orders_diverge_from_exchange_open_orders" in reason_set
        or "local_open_order_divergence" in category_set
    ):
        return ("open_order_unsettled", "go_cancel_on_exchange")
    if (
        "local_position_differs_from_exchange_position" in reason_set
        or "local_position_divergence" in category_set
    ) and semantic_group in {"trade_execution", "liquidation_or_adl", "delivery_or_exercise"}:
        return ("position_drift", "go_close_position_on_exchange")
    if semantic_group == "transfer_or_margin_movement":
        return ("fund_transfer", "confirm_and_rebaseline")
    if semantic_group in {"funding_fee", "interest_or_borrow"}:
        return ("manual_activity", "observe_only")
    if "external_manual_activity_detected" in category_set or semantic_group in {
        "trade_execution",
        "liquidation_or_adl",
        "delivery_or_exercise",
        "other",
    }:
        return ("manual_activity", "confirm_and_rebaseline")
    return ("manual_activity", "observe_only")
