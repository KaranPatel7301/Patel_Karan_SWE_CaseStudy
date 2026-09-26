import datetime as dt
import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, Response
from openai import APIStatusError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.agent.loop import run_agent
from app.config import CompanyConfig, get_settings
from app.db import SessionLocal
from app.models import (
    Base,
    Company,
    Filing,
    FilingSection,
    FinancialFact,
    Price,
    RiskHeading,
    SectionChunk,
)
from app.schemas import (
    AnnualMetricsOut,
    AskRequest,
    AskResponse,
    CompanyOut,
    CompareResponse,
    CompareRowOut,
    FundamentalFactOut,
    FundamentalsResponse,
    HealthResponse,
    MetricValueOut,
    MetricsResponse,
    PriceOut,
    PricesResponse,
    RowCounts,
    SearchHit,
    SearchResponse,
    UnavailableRowOut,
    ValuationMetricOut,
    ValuationResponse,
)
from app.services.search import search_chunks
from app.services.metrics import (
    ANNUAL_METRICS,
    CompareRow,
    Fact,
    MetricValue,
    ValuationMetric,
    annual_metrics,
    compare_metric,
    compute_valuation,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_COUNT_TARGETS: tuple[tuple[str, type[Base]], ...] = (
    ("companies", Company),
    ("financial_facts", FinancialFact),
    ("prices", Price),
    ("filings", Filing),
    ("filing_sections", FilingSection),
    ("section_chunks", SectionChunk),
    ("risk_headings", RiskHeading),
)


def _count(session: Session, model: type[Base]) -> int:
    value = session.scalar(select(func.count()).select_from(model))
    return int(value or 0)


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            counts = {
                name: _count(session, model) for name, model in _COUNT_TARGETS
            }
            latest_price_date = session.scalar(select(func.max(Price.date)))
        return HealthResponse(
            status="ok",
            database="up",
            row_counts=RowCounts(**counts),
            latest_price_date=latest_price_date,
        )
    except Exception:
        logger.exception("database health check failed")
        response.status_code = 503
        return HealthResponse(
            status="degraded",
            database="down",
            row_counts=RowCounts(),
            latest_price_date=None,
        )


@router.get("/companies", response_model=list[CompanyOut])
def list_companies() -> list[CompanyOut]:
    return [
        CompanyOut(ticker=company.ticker, cik=company.cik, name=company.name)
        for company in get_settings().companies
    ]


@router.get("/companies/{ticker}/fundamentals", response_model=FundamentalsResponse)
def fundamentals(
    ticker: str,
    years: int = Query(default=3, ge=1, le=20),
) -> FundamentalsResponse:
    company = _require_company(ticker)
    with SessionLocal() as session:
        rows = session.scalars(
            select(FinancialFact).where(FinancialFact.ticker == company.ticker)
        ).all()
    recent = _recent_rows(list(rows), years)
    return FundamentalsResponse(
        ticker=company.ticker,
        facts=[
            FundamentalFactOut(
                concept=row.concept,
                fiscal_year=row.fiscal_year,
                period_start=row.period_start,
                period_end=row.period_end,
                value=row.value,
                unit=row.unit,
                source_tag=row.source_tag,
                accession=row.accession,
                filed=row.filed,
            )
            for row in recent
        ],
    )


@router.get("/companies/{ticker}/metrics", response_model=MetricsResponse)
def metrics(
    ticker: str,
    years: int = Query(default=3, ge=1, le=20),
) -> MetricsResponse:
    company = _require_company(ticker)
    with SessionLocal() as session:
        facts = _load_facts(session, company.ticker)
    periods = annual_metrics(facts, years)
    return MetricsResponse(
        ticker=company.ticker,
        periods=[
            AnnualMetricsOut(
                fiscal_year=period.fiscal_year,
                period_end=period.period_end,
                gross_margin=_metric_out(period.gross_margin),
                operating_margin=_metric_out(period.operating_margin),
                net_margin=_metric_out(period.net_margin),
                revenue_yoy=_metric_out(period.revenue_yoy),
                net_income_yoy=_metric_out(period.net_income_yoy),
                eps_yoy=_metric_out(period.eps_yoy),
                free_cash_flow=_metric_out(period.free_cash_flow),
            )
            for period in periods
        ],
    )


@router.get("/companies/{ticker}/valuation", response_model=ValuationResponse)
def valuation(ticker: str) -> ValuationResponse:
    company = _require_company(ticker)
    with SessionLocal() as session:
        facts = _load_facts(session, company.ticker)
        latest = session.scalar(
            select(Price)
            .where(Price.ticker == company.ticker)
            .order_by(Price.date.desc())
            .limit(1)
        )
    # P/E uses close. adj_close is also dividend-adjusted.
    close = None if latest is None else latest.close
    price_date = None if latest is None else latest.date
    result = compute_valuation(facts, close=close, price_date=price_date)
    return ValuationResponse(
        ticker=company.ticker,
        price=result.price,
        price_date=result.price_date,
        trailing_pe=_valuation_out(result.trailing_pe),
        price_to_sales=_valuation_out(result.price_to_sales),
        shares_outstanding=result.shares_outstanding,
        shares_period_end=result.shares_period_end,
    )


@router.get("/companies/{ticker}/prices", response_model=PricesResponse)
def prices(
    ticker: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> PricesResponse:
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    company = _require_company(ticker)
    stmt = select(Price).where(Price.ticker == company.ticker)
    if start is not None:
        stmt = stmt.where(Price.date >= start)
    if end is not None:
        stmt = stmt.where(Price.date <= end)
    stmt = stmt.order_by(Price.date)
    with SessionLocal() as session:
        rows = session.scalars(stmt).all()
    return PricesResponse(
        ticker=company.ticker,
        prices=[
            PriceOut(date=row.date, close=row.close, adj_close=row.adj_close, volume=row.volume)
            for row in rows
        ],
    )


_SECTION_ITEMS = {"risk_factors", "mdna", "full_text"}


@router.get("/companies/{ticker}/filings/search", response_model=SearchResponse)
def search_filings(
    ticker: str,
    q: str = Query(min_length=1),
    item: str | None = None,
    fiscal_year: int | None = None,
) -> SearchResponse:
    if item is not None and item not in _SECTION_ITEMS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown item: {item}. Expected one of: {', '.join(sorted(_SECTION_ITEMS))}",
        )
    company = _require_company(ticker)
    with SessionLocal() as session:
        hits = search_chunks(
            session,
            ticker=company.ticker,
            query=q,
            item=item,
            fiscal_year=fiscal_year,
        )
    return SearchResponse(
        ticker=company.ticker,
        query=q,
        hits=[SearchHit(**hit) for hit in hits],
    )


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest) -> AskResponse:
    try:
        return run_agent(body.question)
    except APIStatusError as exc:
        logger.error("LLM request failed: %s", exc)
        raise HTTPException(status_code=502, detail=_llm_error_detail(exc)) from exc


@router.get("/compare", response_model=CompareResponse)
def compare(metric: str = Query(...)) -> CompareResponse:
    if metric not in ANNUAL_METRICS:
        raise HTTPException(status_code=422, detail=f"Unknown metric: {metric}")
    settings = get_settings()
    facts_by_ticker: dict[str, list[Fact]] = {}
    with SessionLocal() as session:
        for company in settings.companies:
            facts_by_ticker[company.ticker] = _load_facts(session, company.ticker)
    ranked, unavailable = compare_metric(metric, facts_by_ticker)
    return CompareResponse(
        metric=metric,
        ranked=[_ranked_row(row) for row in ranked],
        unavailable=[
            UnavailableRowOut(
                ticker=row.ticker,
                reason=row.reason or "not reported in XBRL",
                fiscal_year=row.fiscal_year,
                period_end=row.period_end,
            )
            for row in unavailable
        ],
    )


def _llm_error_detail(exc: APIStatusError) -> str:
    body = exc.body
    error: object = body
    if isinstance(body, list) and body:
        error = body[0]
    if isinstance(error, dict):
        nested = error.get("error", error)
        if isinstance(nested, dict) and isinstance(nested.get("message"), str):
            return nested["message"]
    return "The language model request failed."


def _require_company(ticker: str) -> CompanyConfig:
    settings = get_settings()
    wanted = ticker.strip().upper()
    for company in settings.companies:
        if company.ticker.upper() == wanted:
            return company
    universe = ", ".join(company.ticker for company in settings.companies)
    raise HTTPException(
        status_code=404,
        detail=f"Unknown ticker: {ticker}. Configured universe: {universe}",
    )


def _load_facts(session: Session, ticker: str) -> list[Fact]:
    rows = session.scalars(select(FinancialFact).where(FinancialFact.ticker == ticker)).all()
    return [
        Fact(
            concept=row.concept,
            fiscal_year=row.fiscal_year,
            period_end=row.period_end,
            value=row.value,
        )
        for row in rows
    ]


def _recent_rows(rows: list[FinancialFact], years: int) -> list[FinancialFact]:
    grouped: dict[str, list[FinancialFact]] = defaultdict(list)
    for row in rows:
        grouped[row.concept].append(row)
    selected: list[FinancialFact] = []
    for concept_rows in grouped.values():
        concept_rows.sort(key=lambda row: row.period_end)
        selected.extend(concept_rows[-years:])
    selected.sort(key=lambda row: (row.concept, row.period_end))
    return selected


def _ranked_row(row: CompareRow) -> CompareRowOut:
    if row.value is None or row.fiscal_year is None or row.period_end is None:
        raise RuntimeError(f"ranked compare row for {row.ticker} is missing a value")
    return CompareRowOut(
        ticker=row.ticker,
        value=row.value,
        fiscal_year=row.fiscal_year,
        period_end=row.period_end,
    )


def _metric_out(metric: MetricValue) -> MetricValueOut:
    return MetricValueOut(value=metric.value, reason=metric.reason)


def _valuation_out(metric: ValuationMetric) -> ValuationMetricOut:
    return ValuationMetricOut(
        value=metric.value,
        reason=metric.reason,
        fiscal_year=metric.fiscal_year,
        period_end=metric.period_end,
    )
