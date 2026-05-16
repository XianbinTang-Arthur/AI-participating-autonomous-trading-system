import pytest

from aats.data_platform.research_factory.features.functions import (
    FactorEvaluationResult,
    evaluate_factor_expression,
)
from aats.data_platform.research_factory.features.expressions import (
    FactorExpression,
    parse_factor_expression,
)


def test_parse_factor_expression_accepts_whitelisted_field_and_function() -> None:
    parsed = parse_factor_expression("Mean(close, 20)")

    assert isinstance(parsed, FactorExpression)
    assert parsed.expression == "Mean(close, 20)"
    assert parsed.fields == ("close",)
    assert parsed.functions == ("Mean",)
    assert parsed.to_dict()["fields"] == ["close"]


def test_parse_factor_expression_accepts_basic_arithmetic_composition() -> None:
    parsed = parse_factor_expression("Return(close, 3) + ZScore(volume, 5) / 2")

    assert parsed.fields == ("close", "volume")
    assert parsed.functions == ("Return", "ZScore")


def test_parse_factor_expression_rejects_import_call() -> None:
    with pytest.raises(ValueError, match="dunder"):
        parse_factor_expression('__import__("os")')


def test_parse_factor_expression_rejects_attribute_access() -> None:
    with pytest.raises(ValueError, match="attribute access"):
        parse_factor_expression("close.__class__")


def test_parse_factor_expression_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="unknown factor field"):
        parse_factor_expression("Mean(market_cap, 10)")


def test_parse_factor_expression_rejects_unknown_function() -> None:
    with pytest.raises(ValueError, match="unknown factor function"):
        parse_factor_expression("Eval(close)")


def test_parse_factor_expression_rejects_future_ref_offset() -> None:
    with pytest.raises(ValueError, match="future Ref"):
        parse_factor_expression("Ref(close, -1)")


def test_parse_factor_expression_accepts_past_ref_offset() -> None:
    parsed = parse_factor_expression("Ref(close, 1)")

    assert parsed.fields == ("close",)
    assert parsed.functions == ("Ref",)


def test_parse_factor_expression_rejects_lambda() -> None:
    with pytest.raises(ValueError, match="lambda"):
        parse_factor_expression("(lambda x: x)(close)")


def test_parse_factor_expression_rejects_comprehension() -> None:
    with pytest.raises(ValueError, match="comprehension"):
        parse_factor_expression("[close for close in volume]")


def test_parse_factor_expression_rejects_keyword_arguments() -> None:
    with pytest.raises(ValueError, match="keyword"):
        parse_factor_expression("Mean(close, window=20)")


def test_parse_factor_expression_rejects_string_constants() -> None:
    with pytest.raises(ValueError, match="numeric"):
        parse_factor_expression('Mean(close, "20")')


def test_evaluate_factor_expression_computes_return_mean_std_and_delta() -> None:
    rows = [
        {"close": 100.0},
        {"close": 110.0},
        {"close": 121.0},
    ]

    returns = evaluate_factor_expression("Return(close, 1)", rows)
    means = evaluate_factor_expression("Mean(close, 2)", rows)
    stds = evaluate_factor_expression("Std(close, 2)", rows)
    deltas = evaluate_factor_expression("Delta(close, 2)", rows)

    assert isinstance(returns, FactorEvaluationResult)
    assert returns.values == pytest.approx((None, 0.1, 0.1))
    assert means.values == pytest.approx((None, 105.0, 115.5))
    assert stds.values == pytest.approx((None, 5.0, 5.5))
    assert deltas.values == pytest.approx((None, None, 21.0))


def test_evaluate_factor_expression_ref_uses_half_open_past_semantics() -> None:
    rows = [
        {"close": 100.0},
        {"close": 110.0},
        {"close": 121.0},
    ]

    current = evaluate_factor_expression("Ref(close, 0)", rows)
    previous = evaluate_factor_expression("Ref(close, 1)", rows)

    assert current.values == pytest.approx((100.0, 110.0, 121.0))
    assert previous.values == pytest.approx((None, 100.0, 110.0))
    assert previous.missing_reasons[0] == ("insufficient history for Ref(close, 1)",)


def test_evaluate_factor_expression_records_missing_reason_for_insufficient_window() -> None:
    result = evaluate_factor_expression("Mean(close, 3)", [{"close": 1.0}, {"close": 2.0}])

    assert result.values == (None, None)
    assert result.missing_reasons[0] == ("insufficient history for close window 3",)
    assert result.missing_reasons[1] == ("insufficient history for close window 3",)


def test_evaluate_factor_expression_handles_division_by_zero() -> None:
    result = evaluate_factor_expression("close / volume", [{"close": 10.0, "volume": 0.0}])

    assert result.values == (None,)
    assert result.missing_reasons[0] == ("division by zero",)


def test_evaluate_factor_expression_handles_missing_and_null_fields() -> None:
    result = evaluate_factor_expression(
        "Mean(close, 2)",
        [
            {"close": 10.0},
            {"close": None},
            {"open": 12.0},
        ],
    )

    assert result.values == (None, None, None)
    assert "field 'close' is null" in result.missing_reasons[1]
    assert "field 'close' is null" in result.missing_reasons[2]


def test_evaluate_factor_expression_does_not_mutate_input_rows() -> None:
    rows = [
        {"close": 100.0, "volume": 10.0},
        {"close": 110.0, "volume": 12.0},
    ]
    before = [dict(row) for row in rows]

    evaluate_factor_expression("Mean(close, 2) + Delta(volume, 1)", rows)

    assert rows == before


def test_evaluate_factor_expression_revalidates_factor_expression_input() -> None:
    unsafe = FactorExpression(
        expression="close.__class__",
        normalized_ast="",
        fields=("close",),
        functions=(),
    )

    with pytest.raises(ValueError, match="attribute access"):
        evaluate_factor_expression(unsafe, [{"close": 100.0}])
