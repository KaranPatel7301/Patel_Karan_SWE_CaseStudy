import datetime as dt
from decimal import Decimal

from app.ingest.xbrl import DERIVED_GROSS_PROFIT_TAG, normalize_companyfacts


def _fact(
    *,
    start: str | None,
    end: str,
    val: int | str,
    filed: str,
    form: str = "10-K",
    fy: int = 2099,
    accn: str = "0000000000-24-000001",
    fp: str = "FY",
) -> dict:
    row = {
        "end": end,
        "val": val,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }
    if start is not None:
        row["start"] = start
    return row


def _usd(tag: str, rows: list[dict]) -> dict:
    return {tag: {"units": {"USD": rows}}}


def _payload(us_gaap: dict, dei: dict | None = None) -> dict:
    facts: dict = {"us-gaap": us_gaap}
    if dei is not None:
        facts["dei"] = dei
    return {"facts": facts}


def _one(ticker_facts: list, concept: str, period_end: dt.date):
    matches = [
        fact
        for fact in ticker_facts
        if fact.concept == concept and fact.period_end == period_end
    ]
    assert len(matches) == 1
    return matches[0]


def test_duplicate_period_keeps_most_recent_filing() -> None:
    payload = _payload(
        _usd(
            "NetIncomeLoss",
            [
                _fact(
                    start="2023-01-01",
                    end="2023-12-31",
                    val=100,
                    filed="2024-02-01",
                    accn="0000000000-24-000001",
                    fy=2024,
                ),
                _fact(
                    start="2023-01-01",
                    end="2023-12-31",
                    val=110,
                    filed="2025-02-01",
                    accn="0000000000-25-000001",
                    fy=2025,
                ),
            ],
        )
    )
    fact = _one(normalize_companyfacts("TEST", payload), "net_income", dt.date(2023, 12, 31))
    assert fact.value == Decimal("110")
    assert fact.fiscal_year == 2023
    assert fact.filed == dt.date(2025, 2, 1)
    assert fact.accession == "0000000000-25-000001"
    assert fact.source_tag == "NetIncomeLoss"


def test_restated_eps_uses_latest_filed_value() -> None:
    payload = _payload(
        {
            "EarningsPerShareDiluted": {
                "units": {
                    "USD/shares": [
                        _fact(
                            start="2023-01-30",
                            end="2024-01-28",
                            val="11.93",
                            filed="2024-02-21",
                            accn="0000000000-24-000010",
                        ),
                        _fact(
                            start="2023-01-30",
                            end="2024-01-28",
                            val="1.19",
                            filed="2025-02-26",
                            accn="0000000000-25-000010",
                        ),
                    ]
                }
            }
        }
    )
    fact = _one(
        normalize_companyfacts("TEST", payload),
        "eps_diluted",
        dt.date(2024, 1, 28),
    )
    assert fact.value == Decimal("1.19")
    assert fact.fiscal_year == 2024
    assert fact.unit == "USD/shares"
    assert fact.source_tag == "EarningsPerShareDiluted"


def test_tag_fallback_is_resolved_per_period() -> None:
    us_gaap = {}
    us_gaap.update(
        _usd(
            "SalesRevenueNet",
            [_fact(start="2021-01-01", end="2021-12-31", val=10, filed="2022-02-01")],
        )
    )
    us_gaap.update(
        _usd(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            [_fact(start="2022-01-01", end="2022-12-31", val=20, filed="2023-02-01")],
        )
    )
    facts = normalize_companyfacts("TEST", _payload(us_gaap))
    older = _one(facts, "revenue", dt.date(2021, 12, 31))
    newer = _one(facts, "revenue", dt.date(2022, 12, 31))
    assert older.source_tag == "SalesRevenueNet"
    assert older.value == Decimal("10")
    assert newer.source_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert newer.value == Decimal("20")


def test_higher_priority_tag_wins_for_a_period_that_has_both() -> None:
    us_gaap = {}
    us_gaap.update(
        _usd(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            [
                _fact(
                    start="2023-01-01",
                    end="2023-12-31",
                    val=100,
                    filed="2024-02-01",
                    accn="0000000000-24-000001",
                )
            ],
        )
    )
    us_gaap.update(
        _usd(
            "Revenues",
            [
                _fact(
                    start="2023-01-01",
                    end="2023-12-31",
                    val=999,
                    filed="2025-02-01",
                    accn="0000000000-25-000001",
                )
            ],
        )
    )
    fact = _one(normalize_companyfacts("TEST", _payload(us_gaap)), "revenue", dt.date(2023, 12, 31))
    assert fact.source_tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert fact.value == Decimal("100")


def test_quarterly_and_non_10k_facts_are_filtered_out() -> None:
    payload = _payload(
        _usd(
            "Revenues",
            [
                _fact(start="2023-01-01", end="2023-12-31", val=50, filed="2024-02-01"),
                _fact(start="2023-10-01", end="2023-12-31", val=12, filed="2024-02-01"),
                _fact(
                    start="2023-01-01",
                    end="2023-12-31",
                    val=77,
                    filed="2024-05-01",
                    form="10-Q",
                ),
                _fact(
                    start="2023-01-01",
                    end="2023-12-31",
                    val=88,
                    filed="2024-06-01",
                    form="10-K/A",
                ),
            ],
        )
    )
    revenues = [fact for fact in normalize_companyfacts("TEST", payload) if fact.concept == "revenue"]
    assert len(revenues) == 1
    assert revenues[0].value == Decimal("50")


def test_duration_boundaries() -> None:
    def span(end: dt.date, days: int) -> dict:
        start = end - dt.timedelta(days=days)
        return _fact(
            start=start.isoformat(),
            end=end.isoformat(),
            val=days,
            filed="2024-02-01",
            accn=f"0000000000-24-{days:06d}",
        )

    rows = [
        span(dt.date(2019, 12, 31), 349),
        span(dt.date(2020, 12, 31), 350),
        span(dt.date(2021, 12, 31), 364),
        span(dt.date(2022, 12, 31), 380),
        span(dt.date(2023, 12, 31), 381),
    ]
    facts = [
        fact
        for fact in normalize_companyfacts("TEST", _payload(_usd("Revenues", rows)), years=10)
        if fact.concept == "revenue"
    ]
    assert {fact.value for fact in facts} == {
        Decimal("350"),
        Decimal("364"),
        Decimal("380"),
    }


def test_year_window_follows_revenue_not_stale_concepts() -> None:
    us_gaap: dict = {}
    us_gaap.update(
        _usd(
            "Revenues",
            [
                _fact(
                    start=f"{year}-01-01",
                    end=f"{year}-12-31",
                    val=year,
                    filed=f"{year + 1}-02-01",
                    accn=f"0000000000-{year}-000001",
                )
                for year in range(2021, 2026)
            ],
        )
    )
    us_gaap.update(
        _usd(
            "OperatingIncomeLoss",
            [
                _fact(
                    start=f"{year}-01-01",
                    end=f"{year}-12-31",
                    val=year,
                    filed=f"{year + 1}-02-01",
                    accn=f"0000000000-{year}-000002",
                )
                for year in range(2015, 2020)
            ],
        )
    )
    facts = normalize_companyfacts("TEST", _payload(us_gaap))
    assert [fact for fact in facts if fact.concept == "operating_income"] == []
    assert sorted(fact.fiscal_year for fact in facts if fact.concept == "revenue") == [
        2021,
        2022,
        2023,
        2024,
        2025,
    ]


def test_keeps_last_five_fiscal_years() -> None:
    rows = []
    for year in range(2018, 2025):
        rows.append(
            _fact(
                start=f"{year}-01-01",
                end=f"{year}-12-31",
                val=year,
                filed=f"{year + 1}-02-01",
                accn=f"0000000000-{year}-000001",
            )
        )
    facts = [
        fact
        for fact in normalize_companyfacts("TEST", _payload(_usd("Revenues", rows)))
        if fact.concept == "revenue"
    ]
    assert sorted(fact.fiscal_year for fact in facts) == [2020, 2021, 2022, 2023, 2024]


def test_gross_profit_falls_back_to_revenue_minus_cost() -> None:
    us_gaap = {}
    us_gaap.update(
        _usd(
            "Revenues",
            [_fact(start="2023-01-01", end="2023-12-31", val=100, filed="2024-02-01")],
        )
    )
    us_gaap.update(
        _usd(
            "CostOfRevenue",
            [_fact(start="2023-01-01", end="2023-12-31", val=40, filed="2024-02-01")],
        )
    )
    fact = _one(
        normalize_companyfacts("TEST", _payload(us_gaap)),
        "gross_profit",
        dt.date(2023, 12, 31),
    )
    assert fact.value == Decimal("60")
    assert fact.source_tag == DERIVED_GROSS_PROFIT_TAG
    assert fact.unit == "USD"


def test_reported_gross_profit_is_not_replaced_by_fallback() -> None:
    us_gaap = {}
    us_gaap.update(
        _usd(
            "Revenues",
            [_fact(start="2023-01-01", end="2023-12-31", val=100, filed="2024-02-01")],
        )
    )
    us_gaap.update(
        _usd(
            "CostOfRevenue",
            [_fact(start="2023-01-01", end="2023-12-31", val=40, filed="2024-02-01")],
        )
    )
    us_gaap.update(
        _usd(
            "GrossProfit",
            [_fact(start="2023-01-01", end="2023-12-31", val=55, filed="2024-02-01")],
        )
    )
    fact = _one(
        normalize_companyfacts("TEST", _payload(us_gaap)),
        "gross_profit",
        dt.date(2023, 12, 31),
    )
    assert fact.value == Decimal("55")
    assert fact.source_tag == "GrossProfit"


def test_shares_outstanding_ignores_duration_filter() -> None:
    payload = _payload(
        {},
        dei={
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        _fact(start=None, end="2024-02-16", val=2500000000, filed="2024-02-21"),
                        _fact(
                            start="2024-01-01",
                            end="2024-03-31",
                            val=1,
                            filed="2024-05-01",
                            form="10-Q",
                        ),
                    ]
                }
            }
        },
    )
    shares = [
        fact
        for fact in normalize_companyfacts("TEST", payload)
        if fact.concept == "shares_outstanding"
    ]
    assert len(shares) == 1
    assert shares[0].value == Decimal("2500000000")
    assert shares[0].period_start == dt.date(2024, 2, 16)
    assert shares[0].period_end == dt.date(2024, 2, 16)
    assert shares[0].source_tag == "dei:EntityCommonStockSharesOutstanding"
    assert shares[0].unit == "shares"
    assert shares[0].fiscal_year == 2024
