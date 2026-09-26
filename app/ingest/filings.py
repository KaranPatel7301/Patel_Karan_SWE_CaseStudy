"""Fetch the latest two 10-Ks and store extracted sections plus search chunks."""

import datetime as dt
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.ingest.sec_client import SecClient
from app.ingest.xbrl import _upsert_companies
from app.models import Filing, FilingSection, RiskHeading, SectionChunk

logger = logging.getLogger(__name__)

MIN_SECTION_CHARS = 5_000
MIN_HEADING_CHARS = 40
MAX_HEADING_CHARS = 400
CHUNK_TARGET = 1_200
CHUNK_OVERLAP = 200
EXTRACTION_REGEX = "regex"
EXTRACTION_HEADING = "heading"
EXTRACTION_FALLBACK = "fallback"

_START_RISK = re.compile(r"(?im)^[ \t]{0,12}item\s+1a\b[\.\:\-\s]*risk\s+factors\b")
_END_RISK = re.compile(r"(?im)^[ \t]{0,12}item\s+1b\b")
_END_RISK_FALLBACK = re.compile(r"(?im)^[ \t]{0,12}item\s+2\b")
_START_MDNA = re.compile(
    r"(?im)^[ \t]{0,12}item\s+7\b[\.\:\-\s]*management(?:['\u2019]s)?\s+discussion\b"
)
_END_MDNA = re.compile(r"(?im)^[ \t]{0,12}item\s+7a\b")
_END_MDNA_FALLBACK = re.compile(r"(?im)^[ \t]{0,12}item\s+8\b")
_HEADING_MDNA = re.compile(
    r"(?im)^[ \t]{0,12}management(?:['\u2019]s)?\s+discussion\s+and\s+analysis\b"
)
_END_MDNA_MAJOR = re.compile(
    r"(?im)^[ \t]{0,12}(?:"
    r"quantitative\s+and\s+qualitative\s+disclosures\b|"
    r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm\b|"
    r"consolidated\s+statements\s+of\s+income\b|"
    r"consolidated\s+balance\s+sheets\b|"
    r"(?:consolidated\s+)?financial\s+statements\b|"
    r"item\s+8\b"
    r")"
)


@dataclass(frozen=True)
class ExtractedSection:
    item: str
    text: str
    extraction_method: str

    @property
    def char_count(self) -> int:
        return len(self.text)


_HEADING_BLOCKS = {"p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}
_EMPHASIS_TAGS = {"b", "strong", "i", "em"}

_BLOCK_TAGS = {
    "p",
    "div",
    "tr",
    "td",
    "th",
    "li",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "section",
    "article",
    "blockquote",
    "dt",
    "dd",
    "hr",
}


def html_to_text(html: str) -> str:
    """Drop hidden inline XBRL and render the remaining document as text.

    Inline tags are concatenated so a heading split across spans stays one line.
    Block tags become line breaks, which keeps the table of contents short.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = _render(soup).replace("\xa0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return collapsed.strip()


def _render(node: object) -> str:
    from bs4 import NavigableString, Tag

    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or not node.name:
        return "".join(_render(child) for child in getattr(node, "children", []))
    name = node.name.lower()
    if name in {"script", "style"} or name == "ix:header" or name.endswith(":header"):
        return ""
    style = (node.attrs or {}).get("style")
    if isinstance(style, list):
        style = " ".join(style)
    if isinstance(style, str) and re.search(r"display\s*:\s*none", style, re.I):
        return ""
    inner = "".join(_render(child) for child in node.children)
    if name == "br" or name in _BLOCK_TAGS:
        return f"\n{inner}\n"
    return inner


def extract_sections(
    text: str,
    *,
    ticker: str = "",
    fiscal_year: int | None = None,
) -> list[ExtractedSection]:
    """Extract Item 1A and MD&A. MD&A falls through to a heading match before full text."""
    label = ticker
    if fiscal_year is not None:
        label = f"{label} fy={fiscal_year}".strip()
    prefix = f"{label} " if label else ""
    sections: list[ExtractedSection] = []
    failed = False

    risk = _longest_span(text, _START_RISK, (_END_RISK, _END_RISK_FALLBACK))
    risk_length = 0 if risk is None else len(risk)
    if risk is None or risk_length < MIN_SECTION_CHARS:
        logger.warning(
            "%srisk_factors extraction failed (chars=%s, minimum=%s); using full document",
            prefix,
            risk_length,
            MIN_SECTION_CHARS,
        )
        failed = True
    else:
        logger.info(
            "%spicked risk_factors span chars=%s method=%s",
            prefix,
            risk_length,
            EXTRACTION_REGEX,
        )
        sections.append(ExtractedSection("risk_factors", risk, EXTRACTION_REGEX))

    mdna = _extract_mdna(text)
    if mdna is None:
        item7 = _longest_span(text, _START_MDNA, (_END_MDNA, _END_MDNA_FALLBACK))
        heading = _mdna_heading_span(text)
        logger.warning(
            "%smdna extraction failed (item7_chars=%s, heading_chars=%s, minimum=%s); using full document",
            prefix,
            0 if item7 is None else len(item7),
            0 if heading is None else len(heading),
            MIN_SECTION_CHARS,
        )
        failed = True
    else:
        logger.info(
            "%spicked mdna span chars=%s method=%s",
            prefix,
            mdna.char_count,
            mdna.extraction_method,
        )
        sections.append(mdna)

    if failed:
        sections.append(ExtractedSection("full_text", text, EXTRACTION_FALLBACK))
    return sections


def chunk_text(text: str, *, target: int = CHUNK_TARGET, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Pack paragraphs into chunks of about `target` characters, with a short overlap."""
    pieces = _paragraph_pieces(text, target, overlap)
    if not pieces:
        return []
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
            continue
        combined = f"{current}\n\n{piece}"
        if len(combined) <= target:
            current = combined
            continue
        chunks.append(current)
        tail = current[-overlap:].lstrip()
        current = f"{tail}\n\n{piece}" if tail else piece
    if current:
        chunks.append(current)
    return chunks


def ingest_filings() -> None:
    settings = get_settings()
    init_db()
    with SecClient(settings.sec_user_agent, timeout=120.0) as client, SessionLocal() as session:
        _upsert_companies(session, settings.companies)
        session.commit()
        for company in settings.companies:
            payload = client.submissions(company.cik)
            filings = _latest_10ks(payload)
            logger.info("%s: selected %s 10-K filings", company.ticker, len(filings))
            for filing in filings:
                html = client.filing_document(company.cik, filing["accession"], filing["primary_document"])
                text = html_to_text(html)
                sections = extract_sections(
                    text,
                    ticker=company.ticker,
                    fiscal_year=filing["fiscal_year"],
                )
                risk_text = next((section.text for section in sections if section.item == "risk_factors"), "")
                headings = extract_risk_headings(html, risk_text)
                logger.info(
                    "%s fy=%s risk_headings=%s",
                    company.ticker,
                    filing["fiscal_year"],
                    len(headings),
                )
                _replace_filing(session, company.ticker, filing, sections, headings)
                session.commit()
                for section in sections:
                    logger.info(
                        "%s fy=%s item=%s chars=%s extraction_method=%s",
                        company.ticker,
                        filing["fiscal_year"],
                        section.item,
                        section.char_count,
                        section.extraction_method,
                    )
        _print_section_report(session, [company.ticker for company in settings.companies])


def _latest_10ks(payload: dict) -> list[dict]:
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filed_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    documents = recent.get("primaryDocument", [])
    selected: list[dict] = []
    for index, form in enumerate(forms):
        if form != "10-K":
            continue
        report_date = report_dates[index] if index < len(report_dates) else ""
        filed = filed_dates[index] if index < len(filed_dates) else ""
        accession = accessions[index] if index < len(accessions) else ""
        primary = documents[index] if index < len(documents) else ""
        if not report_date or not filed or not accession or not primary:
            logger.warning("skipping 10-K with missing metadata accession=%s", accession)
            continue
        period_end = dt.date.fromisoformat(report_date)
        selected.append(
            {
                "accession": accession,
                "form": "10-K",
                "fiscal_year": period_end.year,
                "period_end": period_end,
                "filed": dt.date.fromisoformat(filed),
                "primary_document": primary,
            }
        )
        if len(selected) == 2:
            break
    return selected


def extract_risk_headings(html: str, risk_text: str) -> list[str]:
    """Bold or italic risk-factor paragraphs, 40 to 400 characters, in document order."""
    if not risk_text.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    risk_norm = _normalize_ws(risk_text)
    candidates: list[tuple[object, str]] = []
    for tag in soup.find_all(list(_HEADING_BLOCKS)):
        if _is_hidden(tag):
            continue
        if not _block_text_is_emphasized(tag):
            continue
        heading = _normalize_ws(tag.get_text(" ", strip=True))
        if not (MIN_HEADING_CHARS <= len(heading) <= MAX_HEADING_CHARS):
            continue
        if heading not in risk_norm:
            continue
        candidates.append((tag, heading))
    kept: list[tuple[object, str]] = []
    kept_ids: list[int] = []
    for tag, heading in candidates:
        if any(id(parent) in kept_ids for parent in getattr(tag, "parents", [])):
            continue
        kept_ids.append(id(tag))
        kept.append((tag, heading))
    return _merge_wrapped_headings(kept)


def _merge_wrapped_headings(parts: list[tuple[object, str]]) -> list[str]:
    """Join sibling blocks that continue a wrapped sentence, then drop runs over 400 characters."""
    merged: list[tuple[object, str]] = []
    for tag, heading in parts:
        previous = merged[-1] if merged else None
        continues = (
            previous is not None
            and getattr(tag, "parent", None) is getattr(previous[0], "parent", None)
            and _continues_sentence(heading)
        )
        if continues and previous is not None:
            merged[-1] = (tag, f"{previous[1]} {heading}")
            continue
        merged.append((tag, heading))
    return [
        heading
        for _tag, heading in merged
        if MIN_HEADING_CHARS <= len(heading) <= MAX_HEADING_CHARS
    ]


def _continues_sentence(heading: str) -> bool:
    stripped = heading.lstrip("•-–— ")
    return bool(stripped) and stripped[0].islower()


def _replace_filing(
    session: Session,
    ticker: str,
    filing: dict,
    sections: list[ExtractedSection],
    headings: list[str],
) -> None:
    url = _document_url(ticker, filing)
    existing = session.scalar(select(Filing).where(Filing.accession == filing["accession"]))
    if existing is None:
        row = Filing(
            ticker=ticker,
            accession=filing["accession"],
            form=filing["form"],
            fiscal_year=filing["fiscal_year"],
            period_end=filing["period_end"],
            filed=filing["filed"],
            url=url,
        )
        session.add(row)
        session.flush()
    else:
        existing.ticker = ticker
        existing.form = filing["form"]
        existing.fiscal_year = filing["fiscal_year"]
        existing.period_end = filing["period_end"]
        existing.filed = filing["filed"]
        existing.url = url
        row = existing
        section_ids = list(
            session.scalars(select(FilingSection.id).where(FilingSection.filing_id == row.id)).all()
        )
        if section_ids:
            session.execute(delete(SectionChunk).where(SectionChunk.section_id.in_(section_ids)))
            session.execute(delete(FilingSection).where(FilingSection.filing_id == row.id))
        session.execute(delete(RiskHeading).where(RiskHeading.filing_id == row.id))
        session.flush()

    for section in sections:
        stored = FilingSection(
            filing_id=row.id,
            item=section.item,
            text=section.text,
            char_count=section.char_count,
            extraction_method=section.extraction_method,
        )
        session.add(stored)
        session.flush()
        for ordinal, chunk in enumerate(chunk_text(section.text)):
            session.add(SectionChunk(section_id=stored.id, ordinal=ordinal, text=chunk))
    for ordinal, heading in enumerate(headings):
        session.add(RiskHeading(filing_id=row.id, ordinal=ordinal, heading=heading))


def _document_url(ticker: str, filing: dict) -> str:
    settings = get_settings()
    company = next(item for item in settings.companies if item.ticker == ticker)
    cik_int = str(int(company.cik))
    accession = filing["accession"].replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{cik_int}/{accession}/{filing['primary_document']}"
    )


def _print_section_report(session: Session, tickers: list[str]) -> None:
    rows = session.execute(
        select(
            Filing.ticker,
            Filing.fiscal_year,
            Filing.accession,
            FilingSection.item,
            FilingSection.char_count,
            FilingSection.extraction_method,
        )
        .join(FilingSection, FilingSection.filing_id == Filing.id)
        .where(Filing.ticker.in_(tickers))
        .order_by(Filing.ticker, Filing.fiscal_year.desc(), FilingSection.item)
    ).all()
    headers = ("ticker", "fiscal_year", "accession", "item", "char_count", "extraction_method")
    table = [
        (row.ticker, str(row.fiscal_year), row.accession, row.item, str(row.char_count), row.extraction_method)
        for row in rows
    ]
    widths = [
        max([len(header), *(len(record[index]) for record in table)] or [len(header)])
        for index, header in enumerate(headers)
    ]
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for record in table:
        marker = " FALLBACK" if record[-1] == EXTRACTION_FALLBACK else ""
        print(" | ".join(record[index].ljust(widths[index]) for index in range(len(headers))) + marker)


def _extract_mdna(text: str) -> ExtractedSection | None:
    """Item 7 regex first, then a line-start MD&A heading, then give up."""
    item7 = _longest_span(text, _START_MDNA, (_END_MDNA, _END_MDNA_FALLBACK))
    if item7 is not None and len(item7) >= MIN_SECTION_CHARS:
        return ExtractedSection("mdna", item7, EXTRACTION_REGEX)
    heading = _mdna_heading_span(text)
    if heading is not None and len(heading) >= MIN_SECTION_CHARS:
        return ExtractedSection("mdna", heading, EXTRACTION_HEADING)
    return None


def _mdna_heading_span(text: str) -> str | None:
    """Longest line-start MD&A heading, through the next major section or the document end.

    The table of contents and the Item 7 cross-reference are short spans, so the
    narrative heading wins. A quoted mention inside Item 7 does not start a line.
    """
    spans: list[str] = []
    for match in _HEADING_MDNA.finditer(text):
        end = _END_MDNA_MAJOR.search(text, match.end())
        end_at = end.start() if end is not None else len(text)
        if end_at <= match.start():
            continue
        spans.append(text[match.start() : end_at].strip())
    if not spans:
        return None
    return max(spans, key=len)


def _longest_span(text: str, start_re: re.Pattern[str], end_patterns: tuple[re.Pattern[str], ...]) -> str | None:
    spans: list[str] = []
    for match in start_re.finditer(text):
        end_at: int | None = None
        for pattern in end_patterns:
            end = pattern.search(text, match.end())
            if end is not None:
                end_at = end.start()
                break
        if end_at is None or end_at <= match.start():
            continue
        spans.append(text[match.start() : end_at].strip())
    if not spans:
        return None
    return max(spans, key=len)


def _normalize_ws(text: str) -> str:
    cleaned = text.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _tag_style(tag: object) -> str:
    attrs = getattr(tag, "attrs", None) or {}
    style = attrs.get("style")
    if isinstance(style, list):
        return " ".join(style)
    return style or ""


def _is_hidden(tag: object) -> bool:
    current = tag
    while current is not None and getattr(current, "name", None):
        name = current.name.lower()
        if name in {"script", "style"} or name == "ix:header" or name.endswith(":header"):
            return True
        if re.search(r"display\s*:\s*none", _tag_style(current), re.I):
            return True
        parent = getattr(current, "parent", None)
        current = parent if getattr(parent, "name", None) else None
    return False


def _is_emphasis(tag: object) -> bool:
    name = (getattr(tag, "name", None) or "").lower()
    if name in _EMPHASIS_TAGS:
        return True
    style = _tag_style(tag)
    if re.search(r"font-weight\s*:\s*(?:700|bold)\b", style, re.I):
        return True
    return bool(re.search(r"font-style\s*:\s*italic\b", style, re.I))


def _block_text_is_emphasized(tag: object) -> bool:
    from bs4 import NavigableString, Tag

    if not isinstance(tag, Tag):
        return False
    if _is_emphasis(tag):
        return True
    saw_text = False
    for child in tag.descendants:
        if not isinstance(child, NavigableString) or not str(child).strip():
            continue
        saw_text = True
        parent = child.parent
        emphasized = False
        while parent is not None and parent is not tag:
            if isinstance(parent, Tag) and _is_emphasis(parent):
                emphasized = True
                break
            parent = parent.parent
        if not emphasized:
            return False
    return saw_text


def _paragraph_pieces(text: str, target: int, overlap: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= target:
            pieces.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            pieces.append(paragraph[start : start + target])
            if start + target >= len(paragraph):
                break
            start += target - overlap
    return pieces
