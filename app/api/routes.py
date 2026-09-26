import logging

from fastapi import APIRouter, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

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
from app.schemas import HealthResponse, RowCounts

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
