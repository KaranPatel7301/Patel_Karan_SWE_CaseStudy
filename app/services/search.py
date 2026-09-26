"""Postgres full-text search over filing section chunks."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Filing, FilingSection, SectionChunk


def search_chunks(
    session: Session,
    *,
    ticker: str,
    query: str,
    item: str | None = None,
    fiscal_year: int | None = None,
    limit: int = 10,
) -> list[dict]:
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(SectionChunk.tsv, tsquery)
    stmt = (
        select(
            SectionChunk.id,
            SectionChunk.ordinal,
            SectionChunk.text,
            FilingSection.item,
            FilingSection.extraction_method,
            Filing.accession,
            Filing.form,
            Filing.fiscal_year,
            Filing.period_end,
            Filing.filed,
            rank.label("rank"),
        )
        .join(FilingSection, FilingSection.id == SectionChunk.section_id)
        .join(Filing, Filing.id == FilingSection.filing_id)
        .where(Filing.ticker == ticker)
        .where(SectionChunk.tsv.op("@@")(tsquery))
        .order_by(rank.desc(), SectionChunk.id)
        .limit(limit)
    )
    if item is not None:
        stmt = stmt.where(FilingSection.item == item)
    if fiscal_year is not None:
        stmt = stmt.where(Filing.fiscal_year == fiscal_year)
    return [
        {
            "chunk_id": row.id,
            "ordinal": row.ordinal,
            "text": row.text,
            "item": row.item,
            "extraction_method": row.extraction_method,
            "accession": row.accession,
            "form": row.form,
            "fiscal_year": row.fiscal_year,
            "period_end": row.period_end,
            "filed": row.filed,
            "rank": float(row.rank),
        }
        for row in session.execute(stmt).all()
    ]
