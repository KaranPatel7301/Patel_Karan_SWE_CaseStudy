import datetime as dt
from decimal import Decimal

import pytest

from app.services.metrics import (
    NOT_REPORTED_IN_XBRL,
    Fact,
    UnknownMetricError,
    annual_metrics,
    compare_metric,
    compute_valuation,
)


def _fact(concept: str, year: int, value: str, month: int = 12, day: int = 31) -> Fact:
    return Fact(concept, year, dt.date(year, month, day), Decimal(value))


def _bundle(year: int, *, operating: str | None = "40", capex: str | None = "5") -> list[Fact]:
    facts = [
        _fact("revenue", year, "100"),
        _fact("gross_profit", year, "60"),
        _fact("net_income", year, "20"),
        _fact("eps_diluted", year, "2"),
        _fact("operating_cash_flow", year, "30"),
    ]
    if operating is not None:
        facts.append(_fact("operating_income", year, operating))
    if capex is not None:
        facts.append(_fact("capex", year, capex))
    return facts


def test_metric_math_for_a_complete_year() -> None:
    facts = _bundle(2023) + _bundle(2024, operating="50", capex="8")
    periods = annual_metrics(facts, years=1)
    assert len(periods) == 1
    latest = periods[0]
    assert latest.fiscal_year == 2024
    assert latest.period_end == dt.date(2024, 12, 31)
    assert latest.gross_margin.value == pytest.approx(0.6)
    assert latest.operating_margin.value == pytest.approx(0.5)
    assert latest.net_margin.value == pytest.approx(0.2)
    assert latest.revenue_yoy.value == pytest.approx(0.0)
    assert latest.net_income_yoy.value == pytest.approx(0.0)
    assert latest.eps_yoy.value == pytest.approx(0.0)
    assert latest.free_cash_flow.value == pytest.approx(22.0)
    assert latest.gross_margin.reason is None


def test_yoy_uses_prior_year_outside_the_returned_window() -> None:
    facts = _bundle(2023) + [
        _fact("revenue", 2024, "150"),
        _fact("gross_profit", 2024, "90"),
        _fact("net_income", 2024, "30"),
        _fact("eps_diluted", 2024, "3"),
        _fact("operating_income", 2024, "60"),
        _fact("operating_cash_flow", 2024, "40"),
        _fact("capex", 2024, "10"),
    ]
    latest = annual_metrics(facts, years=1)[0]
    assert latest.revenue_yoy.value == pytest.approx(0.5)
    assert latest.net_income_yoy.value == pytest.approx(0.5)
    assert latest.eps_yoy.value == pytest.approx(0.5)


def test_missing_operating_income_returns_not_reported_in_xbrl() -> None:
    periods = annual_metrics(_bundle(2024, operating=None), years=1)
    margin = periods[0].operating_margin
    assert margin.value is None
    assert margin.reason == NOT_REPORTED_IN_XBRL
    assert periods[0].gross_margin.value == pytest.approx(0.6)


def test_missing_capex_returns_null_free_cash_flow() -> None:
    cash_flow = annual_metrics(_bundle(2024, capex=None), years=1)[0].free_cash_flow
    assert cash_flow.value is None
    assert cash_flow.reason == "capex not reported in XBRL"


def test_zero_revenue_does_not_divide() -> None:
    facts = [_fact("revenue", 2024, "0"), _fact("gross_profit", 2024, "10")]
    margin = annual_metrics(facts, years=1)[0].gross_margin
    assert margin.value is None
    assert margin.reason == "revenue is zero"


def test_first_year_growth_is_null_without_a_prior() -> None:
    growth = annual_metrics(_bundle(2024), years=1)[0].revenue_yoy
    assert growth.value is None
    assert growth.reason == "prior fiscal year not reported in XBRL"


def test_compare_ranks_values_and_lists_missing_operating_margin() -> None:
    facts = {
        "NVDA": _bundle(2026, operating="80"),
        "MSFT": _bundle(2026, operating="40"),
        "ETN": _bundle(2025, operating=None),
    }
    ranked, unavailable = compare_metric("operating_margin", facts)
    assert [row.ticker for row in ranked] == ["NVDA", "MSFT"]
    assert ranked[0].value == pytest.approx(0.8)
    assert ranked[0].period_end == dt.date(2026, 12, 31)
    assert len(unavailable) == 1
    assert unavailable[0].ticker == "ETN"
    assert unavailable[0].reason == NOT_REPORTED_IN_XBRL
    assert unavailable[0].fiscal_year == 2025
    assert unavailable[0].period_end == dt.date(2025, 12, 31)


def test_unknown_compare_metric_is_rejected() -> None:
    with pytest.raises(UnknownMetricError):
        compare_metric("not_a_metric", {})


def test_trailing_pe_uses_close_and_price_to_sales_uses_shares() -> None:
    facts = [
        _fact("eps_diluted", 2025, "5"),
        _fact("revenue", 2025, "1000"),
        Fact("shares_outstanding", 2026, dt.date(2026, 2, 20), Decimal("20")),
    ]
    result = compute_valuation(
        facts,
        close=Decimal("100"),
        price_date=dt.date(2026, 9, 25),
    )
    assert result.price == Decimal("100")
    assert result.price_date == dt.date(2026, 9, 25)
    assert result.trailing_pe.value == pytest.approx(20.0)
    assert result.trailing_pe.fiscal_year == 2025
    assert result.trailing_pe.period_end == dt.date(2025, 12, 31)
    assert result.price_to_sales.value == pytest.approx(2.0)
    assert result.price_to_sales.period_end == dt.date(2025, 12, 31)
    assert result.shares_period_end == dt.date(2026, 2, 20)


def test_valuation_missing_inputs_are_null() -> None:
    no_price = compute_valuation(
        [_fact("eps_diluted", 2025, "5"), _fact("revenue", 2025, "1000")],
        close=None,
        price_date=None,
    )
    assert no_price.trailing_pe.value is None
    assert no_price.trailing_pe.reason == "no price in database"
    assert no_price.price_to_sales.reason == "no price in database"

    no_eps = compute_valuation(
        [_fact("revenue", 2025, "1000")],
        close=Decimal("10"),
        price_date=dt.date(2026, 9, 25),
    )
    assert no_eps.trailing_pe.value is None
    assert no_eps.trailing_pe.reason == "eps_diluted not reported in XBRL"
    assert no_eps.price_to_sales.reason == "shares_outstanding not reported in XBRL"
