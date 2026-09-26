"""Normalize SEC companyfacts JSON into annual financial_facts rows."""

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import CompanyConfig, get_settings
from app.db import SessionLocal, init_db
from app.ingest.sec_client import SecClient
from app.models import Company, FinancialFact

logger = logging.getLogger(__name__)

ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
YEARS_TO_KEEP = 5
DERIVED_GROSS_PROFIT_TAG = "derived:revenue-cost_of_revenue"
CHECKPOINT_CONCEPTS = ("revenue", "net_income", "eps_diluted")

# Tags are listed in priority order. Fallback is resolved per period_end.
CONCEPT_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "cost_of_revenue": (
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss",),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "eps_basic": ("EarningsPerShareBasic",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "shares_outstanding": ("EntityCommonStockSharesOutstanding",),
}

CONCEPT_UNITS: dict[str, str] = {
    "revenue": "USD",
    "cost_of_revenue": "USD",
    "gross_profit": "USD",
    "operating_income": "USD",
    "net_income": "USD",
    "eps_diluted": "USD/shares",
    "eps_basic": "USD/shares",
    "operating_cash_flow": "USD",
    "capex": "USD",
    "shares_outstanding": "shares",
}

# shares_outstanding is a cover-page instant fact in the dei taxonomy.
CONCEPT_TAXONOMY: dict[str, str] = {
    concept: "dei" if concept == "shares_outstanding" else "us-gaap"
    for concept in CONCEPT_TAGS
}


@dataclass(frozen=True)
class NormalizedFact:
    ticker: str
    concept: str
    fiscal_year: int
    period_start: dt.date
    period_end: dt.date
    value: Decimal
    unit: str
    source_tag: str
    accession: str
    filed: dt.date


@dataclass(frozen=True)
class _Candidate:
    priority: int
    source_tag: str
    period_start: dt.date
    period_end: dt.date
    value: Decimal
    unit: str
    accession: str
    filed: dt.date


def normalize_companyfacts(
    ticker: str,
    payload: dict,
    *,
    years: int = YEARS_TO_KEEP,
) -> list[NormalizedFact]:
    """Turn one companyfacts document into annual facts.

    fiscal_year comes from period_end.year. The filing's `fy` field is ignored
    because comparatives inside a 10-K carry the filing year, not the period year.
    """
    facts: list[NormalizedFact] = []
    for concept, tags in CONCEPT_TAGS.items():
        chosen = _select_concept(ticker, concept, tags, payload)
        facts.extend(
            NormalizedFact(
                ticker=ticker,
                concept=concept,
                fiscal_year=candidate.period_end.year,
                period_start=candidate.period_start,
                period_end=candidate.period_end,
                value=candidate.value,
                unit=candidate.unit,
                source_tag=candidate.source_tag,
                accession=candidate.accession,
                filed=candidate.filed,
            )
            for candidate in chosen
        )
        if not chosen:
            logger.warning("%s %s: no qualifying 10-K facts", ticker, concept)

    facts.extend(_derive_missing_gross_profit(ticker, facts))
    trimmed = _keep_recent_years(facts, years)
    kept_concepts = {fact.concept for fact in trimmed}
    for concept in CONCEPT_TAGS:
        if any(fact.concept == concept for fact in facts) and concept not in kept_concepts:
            logger.warning(
                "%s %s: qualifying facts are older than the last %s fiscal years",
                ticker,
                concept,
                years,
            )
    for fact in sorted(trimmed, key=lambda row: (row.concept, row.period_end)):
        logger.info(
            "%s %s period_end=%s fiscal_year=%s source_tag=%s filed=%s value=%s",
            fact.ticker,
            fact.concept,
            fact.period_end.isoformat(),
            fact.fiscal_year,
            fact.source_tag,
            fact.filed.isoformat(),
            fact.value,
        )
    return trimmed


def ingest_xbrl() -> None:
    settings = get_settings()
    init_db()
    with SecClient(settings.sec_user_agent) as client, SessionLocal() as session:
        _upsert_companies(session, settings.companies)
        session.commit()
        for company in settings.companies:
            payload = client.company_facts(company.cik)
            entity_name = payload.get("entityName")
            logger.info(
                "fetched companyfacts ticker=%s cik=%s entityName=%s",
                company.ticker,
                company.cik,
                entity_name,
            )
            facts = normalize_companyfacts(company.ticker, payload)
            if not facts:
                raise RuntimeError(f"no normalized facts for {company.ticker}")
            _replace_facts(session, company.ticker, facts)
            session.commit()
            logger.info("stored %s facts for %s", len(facts), company.ticker)
        print_checkpoint(session, [company.ticker for company in settings.companies])


def print_checkpoint(session: Session, tickers: list[str]) -> None:
    """Print revenue, net income, and diluted EPS for the last three fiscal years."""
    rows = session.scalars(
        select(FinancialFact).where(FinancialFact.concept.in_(CHECKPOINT_CONCEPTS))
    ).all()
    by_ticker: dict[str, list[FinancialFact]] = defaultdict(list)
    for row in rows:
        by_ticker[row.ticker].append(row)

    selected: list[FinancialFact] = []
    for ticker in tickers:
        ticker_rows = by_ticker.get(ticker, [])
        years = sorted({row.fiscal_year for row in ticker_rows}, reverse=True)[:3]
        year_set = set(years)
        selected.extend(row for row in ticker_rows if row.fiscal_year in year_set)

    selected.sort(key=lambda row: (row.ticker, row.concept, row.period_end))
    headers = ("ticker", "concept", "fiscal_year", "period_end", "value", "source_tag")
    table = [
        (
            row.ticker,
            row.concept,
            str(row.fiscal_year),
            row.period_end.isoformat(),
            format(row.value, "f"),
            row.source_tag,
        )
        for row in selected
    ]
    widths = [
        max(len(header), *(len(record[index]) for record in table)) if table else len(header)
        for index, header in enumerate(headers)
    ]
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for record in table:
        print(" | ".join(record[index].ljust(widths[index]) for index in range(len(headers))))


def _select_concept(
    ticker: str,
    concept: str,
    tags: tuple[str, ...],
    payload: dict,
) -> list[_Candidate]:
    taxonomy = CONCEPT_TAXONOMY[concept]
    unit = CONCEPT_UNITS[concept]
    point_in_time = concept == "shares_outstanding"
    candidates: list[_Candidate] = []
    for priority, tag in enumerate(tags):
        raw_facts = _raw_facts(payload, taxonomy, tag, unit)
        if raw_facts is None:
            logger.info("%s %s: tag %s not present in companyfacts", ticker, concept, tag)
            continue
        for raw in raw_facts:
            parsed = _parse_fact(raw, priority, tag, unit, point_in_time=point_in_time)
            if parsed is not None:
                candidates.append(parsed)
    return _pick_by_period(ticker, concept, candidates)


def _raw_facts(payload: dict, taxonomy: str, tag: str, unit: str) -> list[dict] | None:
    concept_node = payload.get("facts", {}).get(taxonomy, {}).get(tag)
    if not isinstance(concept_node, dict):
        return None
    units = concept_node.get("units", {})
    if not isinstance(units, dict) or unit not in units:
        logger.warning(
            "tag %s:%s has no %s unit (units=%s)",
            taxonomy,
            tag,
            unit,
            sorted(units) if isinstance(units, dict) else None,
        )
        return []
    rows = units[unit]
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _parse_fact(
    raw: dict,
    priority: int,
    tag: str,
    unit: str,
    *,
    point_in_time: bool,
) -> _Candidate | None:
    if raw.get("form") != "10-K":
        return None
    end_text = raw.get("end")
    filed_text = raw.get("filed")
    accession = raw.get("accn")
    value = raw.get("val")
    if not end_text or not filed_text or not accession or value is None:
        return None
    period_end = dt.date.fromisoformat(str(end_text))
    filed = dt.date.fromisoformat(str(filed_text))
    start_text = raw.get("start")
    if point_in_time:
        # Instant facts have no duration. Store period_start = period_end.
        period_start = (
            dt.date.fromisoformat(str(start_text)) if start_text else period_end
        )
    else:
        if not start_text:
            return None
        period_start = dt.date.fromisoformat(str(start_text))
        duration_days = (period_end - period_start).days
        if not ANNUAL_MIN_DAYS <= duration_days <= ANNUAL_MAX_DAYS:
            return None
    source_tag = f"dei:{tag}" if tag == "EntityCommonStockSharesOutstanding" else tag
    return _Candidate(
        priority=priority,
        source_tag=source_tag,
        period_start=period_start,
        period_end=period_end,
        value=_decimal(value),
        unit=unit,
        accession=str(accession),
        filed=filed,
    )


def _pick_by_period(
    ticker: str,
    concept: str,
    candidates: list[_Candidate],
) -> list[_Candidate]:
    """Prefer the earliest tag that has this period, then the latest filed date."""
    grouped: dict[dt.date, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.period_end].append(candidate)

    chosen: list[_Candidate] = []
    for period_end, group in grouped.items():
        best_priority = min(candidate.priority for candidate in group)
        in_priority = [candidate for candidate in group if candidate.priority == best_priority]
        latest_filed = max(candidate.filed for candidate in in_priority)
        latest = [candidate for candidate in in_priority if candidate.filed == latest_filed]
        if len({candidate.value for candidate in latest}) > 1:
            logger.warning(
                "%s %s period_end=%s has conflicting values on filed=%s; keeping highest accession",
                ticker,
                concept,
                period_end.isoformat(),
                latest_filed.isoformat(),
            )
        pick = max(latest, key=lambda candidate: candidate.accession)
        skipped = sorted({candidate.source_tag for candidate in group if candidate.source_tag != pick.source_tag})
        if skipped:
            logger.info(
                "%s %s period_end=%s chose source_tag=%s over %s",
                ticker,
                concept,
                period_end.isoformat(),
                pick.source_tag,
                ", ".join(skipped),
            )
        chosen.append(pick)
    return chosen


def _derive_missing_gross_profit(ticker: str, facts: list[NormalizedFact]) -> list[NormalizedFact]:
    existing = {fact.period_end for fact in facts if fact.concept == "gross_profit"}
    revenue = {fact.period_end: fact for fact in facts if fact.concept == "revenue"}
    cost = {fact.period_end: fact for fact in facts if fact.concept == "cost_of_revenue"}
    derived: list[NormalizedFact] = []
    for period_end, revenue_fact in revenue.items():
        if period_end in existing or period_end not in cost:
            continue
        cost_fact = cost[period_end]
        donor = revenue_fact if revenue_fact.filed >= cost_fact.filed else cost_fact
        value = revenue_fact.value - cost_fact.value
        logger.info(
            "%s gross_profit period_end=%s derived as revenue - cost_of_revenue = %s",
            ticker,
            period_end.isoformat(),
            value,
        )
        derived.append(
            NormalizedFact(
                ticker=ticker,
                concept="gross_profit",
                fiscal_year=period_end.year,
                period_start=revenue_fact.period_start,
                period_end=period_end,
                value=value,
                unit="USD",
                source_tag=DERIVED_GROSS_PROFIT_TAG,
                accession=donor.accession,
                filed=donor.filed,
            )
        )
    return derived


def _keep_recent_years(facts: list[NormalizedFact], years: int) -> list[NormalizedFact]:
    """Keep facts in the company's last `years` fiscal years.

    The window is anchored on revenue so a concept that stopped being tagged
    (for example operating income with no recent facts) is not stored as if
    those older years were the current history.
    """
    if not facts:
        return []
    revenue_years = [fact.fiscal_year for fact in facts if fact.concept == "revenue"]
    anchor_year = max(revenue_years) if revenue_years else max(fact.fiscal_year for fact in facts)
    cutoff = anchor_year - (years - 1)
    kept = [fact for fact in facts if fact.fiscal_year >= cutoff]
    dropped = len(facts) - len(kept)
    if dropped:
        logger.info(
            "%s: dropped %s facts older than fiscal_year %s",
            facts[0].ticker,
            dropped,
            cutoff,
        )
    return kept


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _upsert_companies(session: Session, companies: list[CompanyConfig]) -> None:
    rows = [
        {"ticker": company.ticker, "cik": company.cik, "name": company.name}
        for company in companies
    ]
    stmt = insert(Company).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker"],
        set_={"cik": stmt.excluded.cik, "name": stmt.excluded.name},
    )
    session.execute(stmt)


def _replace_facts(session: Session, ticker: str, facts: list[NormalizedFact]) -> None:
    keys = [(fact.concept, fact.period_end) for fact in facts]
    session.execute(
        delete(FinancialFact).where(
            FinancialFact.ticker == ticker,
            tuple_(FinancialFact.concept, FinancialFact.period_end).notin_(keys),
        )
    )
    rows = [
        {
            "ticker": fact.ticker,
            "concept": fact.concept,
            "fiscal_year": fact.fiscal_year,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "value": fact.value,
            "unit": fact.unit,
            "source_tag": fact.source_tag,
            "accession": fact.accession,
            "filed": fact.filed,
        }
        for fact in facts
    ]
    stmt = insert(FinancialFact).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_financial_facts_ticker_concept_period_end",
        set_={
            "fiscal_year": stmt.excluded.fiscal_year,
            "period_start": stmt.excluded.period_start,
            "value": stmt.excluded.value,
            "unit": stmt.excluded.unit,
            "source_tag": stmt.excluded.source_tag,
            "accession": stmt.excluded.accession,
            "filed": stmt.excluded.filed,
        },
    )
    session.execute(stmt)
