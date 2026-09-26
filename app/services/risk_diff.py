"""Fuzzy diff of risk-factor headings between the latest and prior 10-K."""

from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Filing, RiskHeading

MATCH_THRESHOLD = 85


@dataclass(frozen=True)
class RiskHeadingDiff:
    ticker: str
    latest_fiscal_year: int | None
    prior_fiscal_year: int | None
    added: list[str]
    removed: list[str]


def diff_headings(
    latest: list[str],
    prior: list[str],
    *,
    threshold: int = MATCH_THRESHOLD,
) -> tuple[list[str], list[str]]:
    """Pair headings with token_set_ratio. Unmatched latest rows are added; unmatched prior rows are removed."""
    pairs: list[tuple[int, int, int]] = []
    for latest_index, latest_heading in enumerate(latest):
        for prior_index, prior_heading in enumerate(prior):
            score = int(fuzz.token_set_ratio(latest_heading, prior_heading))
            if score >= threshold:
                pairs.append((score, latest_index, prior_index))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_latest: set[int] = set()
    used_prior: set[int] = set()
    for _score, latest_index, prior_index in pairs:
        if latest_index in used_latest or prior_index in used_prior:
            continue
        used_latest.add(latest_index)
        used_prior.add(prior_index)
    added = [heading for index, heading in enumerate(latest) if index not in used_latest]
    removed = [heading for index, heading in enumerate(prior) if index not in used_prior]
    return added, removed


def diff_risk_factors(ticker: str, session: Session | None = None) -> RiskHeadingDiff:
    """Compare stored headings for a ticker's two most recent 10-Ks."""
    owns_session = session is None
    if session is None:
        session = SessionLocal()
    try:
        filings = list(
            session.scalars(
                select(Filing).where(Filing.ticker == ticker).order_by(Filing.fiscal_year.desc())
            ).all()
        )
        latest = filings[0] if filings else None
        prior = filings[1] if len(filings) > 1 else None
        latest_headings = _headings(session, latest.id) if latest is not None else []
        prior_headings = _headings(session, prior.id) if prior is not None else []
        added, removed = diff_headings(latest_headings, prior_headings)
        return RiskHeadingDiff(
            ticker=ticker,
            latest_fiscal_year=None if latest is None else latest.fiscal_year,
            prior_fiscal_year=None if prior is None else prior.fiscal_year,
            added=added,
            removed=removed,
        )
    finally:
        if owns_session:
            session.close()


def _headings(session: Session, filing_id: int) -> list[str]:
    rows = session.scalars(
        select(RiskHeading.heading)
        .where(RiskHeading.filing_id == filing_id)
        .order_by(RiskHeading.ordinal)
    ).all()
    return list(rows)
