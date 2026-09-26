"""Tool schemas and dispatch. Each tool calls the same services as the HTTP API."""

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import CompanyConfig, get_settings
from app.db import SessionLocal
from app.ingest.xbrl import CONCEPT_TAGS
from app.models import Filing, FinancialFact, Price
from app.services.metrics import (
    ANNUAL_METRICS,
    Fact,
    UnknownMetricError,
    ValuationMetric,
    annual_metrics,
    compare_metric,
    compute_valuation,
)
from app.services.risk_diff import diff_risk_factors
from app.services.search import search_chunks

FACT_CONCEPTS: tuple[str, ...] = tuple(CONCEPT_TAGS)
FINANCIAL_METRICS: tuple[str, ...] = FACT_CONCEPTS + ANNUAL_METRICS
SEARCH_ITEMS = ("risk_factors", "mdna", "any")
FILING_CHOICES = ("latest", "prior", "any")


class ToolError(Exception):
    """The tool arguments or the requested data cannot be served."""


class _EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _FinancialsArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    metrics: list[str] = Field(min_length=1)
    years: int = Field(default=3, ge=1, le=20)


class _CompareArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metric: str


class _TickerArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    query: str = Field(min_length=1)
    item: str = "any"
    filing: str = "any"
    k: int = Field(default=5, ge=1, le=20)


def _function_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def tool_schemas() -> list[dict[str, Any]]:
    metric_enum = list(FINANCIAL_METRICS)
    return [
        _function_tool(
            "list_companies",
            "List the configured universe of companies. Takes no arguments.",
            {"type": "object", "properties": {}},
        ),
        _function_tool(
            "get_financials",
            "Reported annual facts and computed metrics for one company. "
            "Computed metrics are already calculated. years is the number of latest fiscal years.",
            {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string", "enum": metric_enum},
                    },
                    "years": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["ticker", "metrics"],
            },
        ),
        _function_tool(
            "compare_companies",
            "Rank every company on one computed metric for each company's latest fiscal year. "
            f"metric is one of: {', '.join(ANNUAL_METRICS)}.",
            {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": list(ANNUAL_METRICS)},
                },
                "required": ["metric"],
            },
        ),
        _function_tool(
            "get_valuation",
            "Trailing P/E and price to sales from the latest stored close, not a live quote. "
            "sources lists both sides of the join: a price source and the eps_diluted or revenue fact.",
            {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        ),
        _function_tool(
            "search_filings",
            "Full-text search over 10-K chunks. item is risk_factors, mdna, or any. "
            "filing is latest, prior, or any. Cite hits by chunk_id.",
            {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "query": {"type": "string"},
                    "item": {"type": "string", "enum": list(SEARCH_ITEMS)},
                    "filing": {"type": "string", "enum": list(FILING_CHOICES)},
                    "k": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["ticker", "query"],
            },
        ),
        _function_tool(
            "diff_risk_factors",
            "Compare risk-factor headings in the latest 10-K with the prior 10-K. "
            "added headings are new. reworded pairs changed wording and are not new headings; "
            "report both texts and the score. Unchanged headings are omitted. "
            "Deterministic; do not redo the match yourself.",
            {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        ),
        _function_tool(
            "final_answer",
            "Finish the question. Call this only after you have the tool results you will cite. "
            "If declined is false, sources must be non-empty.",
            {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "declined": {"type": "boolean"},
                    "data_used": {
                        "type": "string",
                        "enum": ["numbers", "text", "both", "none"],
                    },
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["fact", "filing_chunk", "price"]},
                                "ticker": {"type": "string"},
                                "concept": {"type": "string"},
                                "fiscal_year": {"type": "integer"},
                                "period_end": {"type": "string"},
                                "chunk_id": {"type": "integer"},
                                "item": {"type": "string"},
                                "filing_fiscal_year": {"type": "integer"},
                                "date": {"type": "string"},
                                "close": {"type": "number"},
                            },
                            "required": ["type", "ticker"],
                        },
                    },
                },
                "required": ["answer", "sources", "data_used", "declined"],
            },
        ),
    ]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "list_companies": _list_companies,
        "get_financials": _get_financials,
        "compare_companies": _compare_companies,
        "get_valuation": _get_valuation,
        "search_filings": _search_filings,
        "diff_risk_factors": _diff_risk_factors,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool: {name}")
    try:
        return handler(args)
    except ValidationError as exc:
        raise ToolError(exc.errors()[0]["msg"] if exc.errors() else "invalid arguments") from exc


def _list_companies(_args: dict[str, Any]) -> dict[str, Any]:
    _EmptyArgs.model_validate(_args)
    return {
        "companies": [
            {"ticker": company.ticker, "name": company.name}
            for company in get_settings().companies
        ]
    }


def _get_financials(args: dict[str, Any]) -> dict[str, Any]:
    parsed = _FinancialsArgs.model_validate(args)
    company = _company(parsed.ticker)
    unknown = [metric for metric in parsed.metrics if metric not in FINANCIAL_METRICS]
    if unknown:
        allowed = ", ".join(FINANCIAL_METRICS)
        raise ToolError(f"Unknown metric: {', '.join(unknown)}. Expected one of: {allowed}")
    requested_facts = _unique(metric for metric in parsed.metrics if metric in FACT_CONCEPTS)
    requested_metrics = _unique(metric for metric in parsed.metrics if metric in ANNUAL_METRICS)
    with SessionLocal() as session:
        facts, missing = _recent_facts(session, company.ticker, requested_facts, parsed.years)
        metric_rows = []
        if requested_metrics:
            metric_rows = _metric_rows(
                _load_facts(session, company.ticker),
                requested_metrics,
                parsed.years,
            )
    return {
        "ticker": company.ticker,
        "name": company.name,
        "facts": facts,
        "missing_facts": missing,
        "metrics": metric_rows,
    }


def _compare_companies(args: dict[str, Any]) -> dict[str, Any]:
    parsed = _CompareArgs.model_validate(args)
    if parsed.metric not in ANNUAL_METRICS:
        raise ToolError(
            f"Unknown metric: {parsed.metric}. Expected one of: {', '.join(ANNUAL_METRICS)}"
        )
    facts_by_ticker: dict[str, list[Fact]] = {}
    with SessionLocal() as session:
        for company in get_settings().companies:
            facts_by_ticker[company.ticker] = _load_facts(session, company.ticker)
    try:
        ranked, unavailable = compare_metric(parsed.metric, facts_by_ticker)
    except UnknownMetricError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "metric": parsed.metric,
        "ranked": [
            {
                "ticker": row.ticker,
                "value": row.value,
                "fiscal_year": row.fiscal_year,
                "period_end": None if row.period_end is None else row.period_end.isoformat(),
                "source": {
                    "type": "fact",
                    "ticker": row.ticker,
                    "concept": parsed.metric,
                    "fiscal_year": row.fiscal_year,
                    "period_end": None if row.period_end is None else row.period_end.isoformat(),
                },
            }
            for row in ranked
        ],
        "unavailable": [
            {
                "ticker": row.ticker,
                "reason": row.reason,
                "fiscal_year": row.fiscal_year,
                "period_end": None if row.period_end is None else row.period_end.isoformat(),
            }
            for row in unavailable
        ],
    }


def _get_valuation(args: dict[str, Any]) -> dict[str, Any]:
    parsed = _TickerArgs.model_validate(args)
    company = _company(parsed.ticker)
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
    price_source = _price_citation(company.ticker, close, price_date)
    eps_source = _fact_citation(company.ticker, "eps_diluted", result.trailing_pe)
    revenue_source = _fact_citation(company.ticker, "revenue", result.price_to_sales)
    return {
        "ticker": company.ticker,
        "price": None if result.price is None else float(result.price),
        "price_date": None if result.price_date is None else result.price_date.isoformat(),
        "trailing_pe": _valuation_field(result.trailing_pe),
        "price_to_sales": _valuation_field(result.price_to_sales),
        "shares_outstanding": (
            None if result.shares_outstanding is None else float(result.shares_outstanding)
        ),
        "shares_period_end": (
            None if result.shares_period_end is None else result.shares_period_end.isoformat()
        ),
        "sources": [source for source in (price_source, eps_source, revenue_source) if source],
    }


def _search_filings(args: dict[str, Any]) -> dict[str, Any]:
    parsed = _SearchArgs.model_validate(args)
    if parsed.item not in SEARCH_ITEMS:
        raise ToolError(
            f"Unknown item: {parsed.item}. Expected one of: {', '.join(SEARCH_ITEMS)}"
        )
    if parsed.filing not in FILING_CHOICES:
        raise ToolError(
            f"Unknown filing: {parsed.filing}. Expected one of: {', '.join(FILING_CHOICES)}"
        )
    company = _company(parsed.ticker)
    with SessionLocal() as session:
        fiscal_year = _filing_year(session, company.ticker, parsed.filing)
        if parsed.filing != "any" and fiscal_year is None:
            return {
                "ticker": company.ticker,
                "query": parsed.query,
                "hits": [],
                "note": f"no {parsed.filing} 10-K on file",
            }
        hits = search_chunks(
            session,
            ticker=company.ticker,
            query=parsed.query,
            item=None if parsed.item == "any" else parsed.item,
            fiscal_year=fiscal_year,
            limit=parsed.k,
        )
    return {
        "ticker": company.ticker,
        "query": parsed.query,
        "item": parsed.item,
        "filing": parsed.filing,
        "hits": [
            {
                "chunk_id": hit["chunk_id"],
                "text": hit["text"],
                "item": hit["item"],
                "filing_fiscal_year": hit["fiscal_year"],
                "period_end": hit["period_end"].isoformat(),
                "rank": hit["rank"],
                "source": {
                    "type": "filing_chunk",
                    "chunk_id": hit["chunk_id"],
                    "ticker": company.ticker,
                    "item": hit["item"],
                    "filing_fiscal_year": hit["fiscal_year"],
                },
            }
            for hit in hits
        ],
    }


def _diff_risk_factors(args: dict[str, Any]) -> dict[str, Any]:
    parsed = _TickerArgs.model_validate(args)
    company = _company(parsed.ticker)
    diff = diff_risk_factors(company.ticker)
    return {
        "ticker": diff.ticker,
        "latest_fiscal_year": diff.latest_fiscal_year,
        "prior_fiscal_year": diff.prior_fiscal_year,
        "added": diff.added,
        "removed": diff.removed,
        "reworded": [
            {"latest": item.latest, "prior": item.prior, "score": item.score}
            for item in diff.reworded
        ],
        "note": (
            "added headings are new. reworded pairs are the same risk with different wording, "
            "not new risks. removed headings were dropped."
        ),
    }


def _company(ticker: str) -> CompanyConfig:
    wanted = ticker.strip().upper()
    settings = get_settings()
    for company in settings.companies:
        if company.ticker.upper() == wanted:
            return company
    universe = ", ".join(company.ticker for company in settings.companies)
    raise ToolError(f"Unknown ticker: {ticker}. Configured universe: {universe}")


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


def _recent_facts(
    session: Session,
    ticker: str,
    concepts: list[str],
    years: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not concepts:
        return [], []
    rows = session.scalars(
        select(FinancialFact).where(
            FinancialFact.ticker == ticker,
            FinancialFact.concept.in_(concepts),
        )
    ).all()
    grouped: dict[str, list[FinancialFact]] = defaultdict(list)
    for row in rows:
        grouped[row.concept].append(row)
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    for concept in concepts:
        concept_rows = sorted(grouped.get(concept, []), key=lambda row: row.period_end)
        if not concept_rows:
            missing.append(concept)
            continue
        for row in concept_rows[-years:]:
            selected.append(
                {
                    "concept": row.concept,
                    "fiscal_year": row.fiscal_year,
                    "period_end": row.period_end.isoformat(),
                    "value": float(row.value),
                    "unit": row.unit,
                    "source_tag": row.source_tag,
                    "source": {
                        "type": "fact",
                        "ticker": ticker,
                        "concept": row.concept,
                        "fiscal_year": row.fiscal_year,
                        "period_end": row.period_end.isoformat(),
                    },
                }
            )
    return selected, missing


def _metric_rows(facts: list[Fact], names: list[str], years: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period in annual_metrics(facts, years):
        entry: dict[str, Any] = {
            "fiscal_year": period.fiscal_year,
            "period_end": period.period_end.isoformat(),
        }
        for name in names:
            metric = getattr(period, name)
            entry[name] = {"value": metric.value, "reason": metric.reason}
        rows.append(entry)
    return rows


def _valuation_field(metric: ValuationMetric) -> dict[str, Any]:
    return {
        "value": metric.value,
        "reason": metric.reason,
        "fiscal_year": metric.fiscal_year,
        "period_end": None if metric.period_end is None else metric.period_end.isoformat(),
    }


def _price_citation(
    ticker: str,
    close: Decimal | None,
    price_date: dt.date | None,
) -> dict[str, Any] | None:
    if close is None or price_date is None:
        return None
    return {
        "type": "price",
        "ticker": ticker,
        "date": price_date.isoformat(),
        "close": float(close),
    }


def _fact_citation(ticker: str, concept: str, metric: ValuationMetric) -> dict[str, Any] | None:
    if metric.fiscal_year is None or metric.period_end is None:
        return None
    return {
        "type": "fact",
        "ticker": ticker,
        "concept": concept,
        "fiscal_year": metric.fiscal_year,
        "period_end": metric.period_end.isoformat(),
    }


def _filing_year(session: Session, ticker: str, filing: str) -> int | None:
    if filing == "any":
        return None
    filings = list(
        session.scalars(
            select(Filing).where(Filing.ticker == ticker).order_by(Filing.fiscal_year.desc())
        ).all()
    )
    if filing == "latest":
        return None if not filings else filings[0].fiscal_year
    if len(filings) < 2:
        return None
    return filings[1].fiscal_year


def _unique(items: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered
