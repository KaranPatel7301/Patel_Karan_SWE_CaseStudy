"""Margins, growth, free cash flow, and valuation. Computed in Python, never by the LLM."""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

ANNUAL_METRICS: tuple[str, ...] = (
    "gross_margin",
    "operating_margin",
    "net_margin",
    "revenue_yoy",
    "net_income_yoy",
    "eps_yoy",
    "free_cash_flow",
)

NOT_REPORTED_IN_XBRL = "not reported in XBRL"


class UnknownMetricError(ValueError):
    def __init__(self, metric: str) -> None:
        self.metric = metric
        super().__init__(f"Unknown metric: {metric}")


@dataclass(frozen=True)
class Fact:
    concept: str
    fiscal_year: int
    period_end: dt.date
    value: Decimal


@dataclass(frozen=True)
class MetricValue:
    value: float | None
    reason: str | None


@dataclass(frozen=True)
class AnnualMetrics:
    fiscal_year: int
    period_end: dt.date
    gross_margin: MetricValue
    operating_margin: MetricValue
    net_margin: MetricValue
    revenue_yoy: MetricValue
    net_income_yoy: MetricValue
    eps_yoy: MetricValue
    free_cash_flow: MetricValue


@dataclass(frozen=True)
class CompareRow:
    ticker: str
    value: float | None
    reason: str | None
    fiscal_year: int | None
    period_end: dt.date | None


@dataclass(frozen=True)
class ValuationMetric:
    value: float | None
    reason: str | None
    fiscal_year: int | None
    period_end: dt.date | None


@dataclass(frozen=True)
class Valuation:
    price: Decimal | None
    price_date: dt.date | None
    trailing_pe: ValuationMetric
    price_to_sales: ValuationMetric
    shares_outstanding: Decimal | None
    shares_period_end: dt.date | None


def annual_metrics(facts: list[Fact], years: int) -> list[AnnualMetrics]:
    """Metrics for the last `years` revenue periods. YoY uses the prior period even if it is outside that window."""
    by_concept = _index(facts)
    revenue_ends = sorted(by_concept.get("revenue", {}))
    selected = revenue_ends[-years:]
    rows: list[AnnualMetrics] = []
    for period_end in selected:
        prior_end = _prior(revenue_ends, period_end)
        revenue = by_concept["revenue"][period_end]
        rows.append(
            AnnualMetrics(
                fiscal_year=revenue.fiscal_year,
                period_end=period_end,
                gross_margin=_margin(by_concept, "gross_profit", period_end),
                operating_margin=_operating_margin(by_concept, period_end),
                net_margin=_margin(by_concept, "net_income", period_end),
                revenue_yoy=_yoy(by_concept, "revenue", period_end, prior_end),
                net_income_yoy=_yoy(by_concept, "net_income", period_end, prior_end),
                eps_yoy=_yoy(by_concept, "eps_diluted", period_end, prior_end),
                free_cash_flow=_free_cash_flow(by_concept, period_end),
            )
        )
    return rows


def compare_metric(metric: str, facts_by_ticker: dict[str, list[Fact]]) -> tuple[list[CompareRow], list[CompareRow]]:
    """Rank tickers that have a value. Tickers without one are returned separately, in universe order."""
    if metric not in ANNUAL_METRICS:
        raise UnknownMetricError(metric)
    ranked: list[CompareRow] = []
    unavailable: list[CompareRow] = []
    for ticker, facts in facts_by_ticker.items():
        periods = annual_metrics(facts, years=1)
        if not periods:
            unavailable.append(
                CompareRow(ticker, None, "revenue not reported in XBRL", None, None)
            )
            continue
        latest = periods[-1]
        result: MetricValue = getattr(latest, metric)
        row = CompareRow(
            ticker,
            result.value,
            result.reason,
            latest.fiscal_year,
            latest.period_end,
        )
        if result.value is None:
            unavailable.append(row)
        else:
            ranked.append(row)
    ranked.sort(key=lambda row: (-(row.value or 0), row.ticker))
    return ranked, unavailable


def compute_valuation(
    facts: list[Fact],
    *,
    close: Decimal | None,
    price_date: dt.date | None,
) -> Valuation:
    """Trailing P/E uses `close`, not dividend-adjusted close."""
    eps = _latest(facts, "eps_diluted")
    revenue = _latest(facts, "revenue")
    shares = _latest(facts, "shares_outstanding")
    return Valuation(
        price=close,
        price_date=price_date,
        trailing_pe=_trailing_pe(close, price_date, eps),
        price_to_sales=_price_to_sales(close, price_date, shares, revenue),
        shares_outstanding=None if shares is None else shares.value,
        shares_period_end=None if shares is None else shares.period_end,
    )


def _trailing_pe(
    close: Decimal | None,
    price_date: dt.date | None,
    eps: Fact | None,
) -> ValuationMetric:
    fiscal_year = None if eps is None else eps.fiscal_year
    period_end = None if eps is None else eps.period_end
    if close is None or price_date is None:
        return ValuationMetric(None, "no price in database", fiscal_year, period_end)
    if eps is None:
        return ValuationMetric(None, "eps_diluted not reported in XBRL", None, None)
    if eps.value == 0:
        return ValuationMetric(None, "eps_diluted is zero", eps.fiscal_year, eps.period_end)
    return ValuationMetric(float(close / eps.value), None, eps.fiscal_year, eps.period_end)


def _price_to_sales(
    close: Decimal | None,
    price_date: dt.date | None,
    shares: Fact | None,
    revenue: Fact | None,
) -> ValuationMetric:
    fiscal_year = None if revenue is None else revenue.fiscal_year
    period_end = None if revenue is None else revenue.period_end
    if close is None or price_date is None:
        return ValuationMetric(None, "no price in database", fiscal_year, period_end)
    if shares is None:
        return ValuationMetric(None, "shares_outstanding not reported in XBRL", fiscal_year, period_end)
    if revenue is None:
        return ValuationMetric(None, "revenue not reported in XBRL", None, None)
    if revenue.value == 0:
        return ValuationMetric(None, "revenue is zero", revenue.fiscal_year, revenue.period_end)
    value = float((close * shares.value) / revenue.value)
    return ValuationMetric(value, None, revenue.fiscal_year, revenue.period_end)


def _index(facts: list[Fact]) -> dict[str, dict[dt.date, Fact]]:
    grouped: dict[str, dict[dt.date, Fact]] = defaultdict(dict)
    for fact in facts:
        grouped[fact.concept][fact.period_end] = fact
    return grouped


def _prior(period_ends: list[dt.date], period_end: dt.date) -> dt.date | None:
    earlier = [end for end in period_ends if end < period_end]
    if not earlier:
        return None
    return earlier[-1]


def _operating_margin(by_concept: dict[str, dict[dt.date, Fact]], period_end: dt.date) -> MetricValue:
    if period_end not in by_concept.get("operating_income", {}):
        return MetricValue(None, NOT_REPORTED_IN_XBRL)
    return _margin(by_concept, "operating_income", period_end)


def _margin(
    by_concept: dict[str, dict[dt.date, Fact]],
    numerator: str,
    period_end: dt.date,
) -> MetricValue:
    current = by_concept.get(numerator, {}).get(period_end)
    revenue = by_concept.get("revenue", {}).get(period_end)
    if current is None:
        return MetricValue(None, f"{numerator} not reported in XBRL")
    if revenue is None:
        return MetricValue(None, "revenue not reported in XBRL")
    if revenue.value == 0:
        return MetricValue(None, "revenue is zero")
    return MetricValue(float(current.value / revenue.value), None)


def _yoy(
    by_concept: dict[str, dict[dt.date, Fact]],
    concept: str,
    period_end: dt.date,
    prior_end: dt.date | None,
) -> MetricValue:
    current = by_concept.get(concept, {}).get(period_end)
    if current is None:
        return MetricValue(None, f"{concept} not reported in XBRL")
    if prior_end is None or prior_end not in by_concept.get(concept, {}):
        return MetricValue(None, "prior fiscal year not reported in XBRL")
    prior = by_concept[concept][prior_end]
    if prior.value == 0:
        return MetricValue(None, f"prior {concept} is zero")
    return MetricValue(float(current.value / prior.value - 1), None)


def _free_cash_flow(by_concept: dict[str, dict[dt.date, Fact]], period_end: dt.date) -> MetricValue:
    cash_flow = by_concept.get("operating_cash_flow", {}).get(period_end)
    capex = by_concept.get("capex", {}).get(period_end)
    if cash_flow is None:
        return MetricValue(None, "operating_cash_flow not reported in XBRL")
    if capex is None:
        return MetricValue(None, "capex not reported in XBRL")
    return MetricValue(float(cash_flow.value - capex.value), None)


def _latest(facts: list[Fact], concept: str) -> Fact | None:
    matches = [fact for fact in facts if fact.concept == concept]
    if not matches:
        return None
    return max(matches, key=lambda fact: fact.period_end)
