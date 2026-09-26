from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PlainSerializer, field_validator

JsonDecimal = Annotated[
    Decimal,
    PlainSerializer(lambda value: float(value), return_type=float),
]


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


class CompanyOut(BaseModel):
    ticker: str
    cik: str
    name: str


class FundamentalFactOut(BaseModel):
    concept: str
    fiscal_year: int
    period_start: date
    period_end: date
    value: JsonDecimal
    unit: str
    source_tag: str
    accession: str
    filed: date


class FundamentalsResponse(BaseModel):
    ticker: str
    facts: list[FundamentalFactOut]


class MetricValueOut(BaseModel):
    value: float | None
    reason: str | None


class AnnualMetricsOut(BaseModel):
    fiscal_year: int
    period_end: date
    gross_margin: MetricValueOut
    operating_margin: MetricValueOut
    net_margin: MetricValueOut
    revenue_yoy: MetricValueOut
    net_income_yoy: MetricValueOut
    eps_yoy: MetricValueOut
    free_cash_flow: MetricValueOut


class MetricsResponse(BaseModel):
    ticker: str
    periods: list[AnnualMetricsOut]


class ValuationMetricOut(BaseModel):
    value: float | None
    reason: str | None
    fiscal_year: int | None
    period_end: date | None


class ValuationResponse(BaseModel):
    ticker: str
    price: JsonDecimal | None
    price_date: date | None
    trailing_pe: ValuationMetricOut
    price_to_sales: ValuationMetricOut
    shares_outstanding: JsonDecimal | None
    shares_period_end: date | None


class PriceOut(BaseModel):
    date: date
    close: JsonDecimal
    adj_close: JsonDecimal
    volume: int


class PricesResponse(BaseModel):
    ticker: str
    prices: list[PriceOut]


class CompareRowOut(BaseModel):
    ticker: str
    value: float
    fiscal_year: int
    period_end: date


class UnavailableRowOut(BaseModel):
    ticker: str
    reason: str
    fiscal_year: int | None
    period_end: date | None


class CompareResponse(BaseModel):
    metric: str
    ranked: list[CompareRowOut]
    unavailable: list[UnavailableRowOut]


class SearchHit(BaseModel):
    chunk_id: int
    ordinal: int
    text: str
    rank: float
    item: str
    extraction_method: str
    accession: str
    form: str
    fiscal_year: int
    period_end: date
    filed: date


class SearchResponse(BaseModel):
    ticker: str
    query: str
    hits: list[SearchHit]


class FactSource(BaseModel):
    type: Literal["fact"]
    ticker: str
    concept: str
    fiscal_year: int
    period_end: date


class FilingChunkSource(BaseModel):
    type: Literal["filing_chunk"]
    chunk_id: int
    ticker: str
    item: str
    filing_fiscal_year: int


Source = Annotated[FactSource | FilingChunkSource, Field(discriminator="type")]


class ToolTraceEntry(BaseModel):
    tool: str
    args: dict[str, Any]
    ok: bool


class AskRequest(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped


class FinalAnswer(BaseModel):
    answer: str
    sources: list[Source]
    data_used: Literal["numbers", "text", "both", "none"]
    declined: bool


class AskResponse(BaseModel):
    answer: str
    declined: bool
    data_used: Literal["numbers", "text", "both", "none"]
    sources: list[Source]
    tool_trace: list[ToolTraceEntry]
