from datetime import date
from typing import Literal

from pydantic import BaseModel


class RowCounts(BaseModel):
    companies: int = 0
    financial_facts: int = 0
    prices: int = 0
    filings: int = 0
    filing_sections: int = 0
    section_chunks: int = 0
    risk_headings: int = 0


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["up", "down"]
    row_counts: RowCounts
    latest_price_date: date | None
