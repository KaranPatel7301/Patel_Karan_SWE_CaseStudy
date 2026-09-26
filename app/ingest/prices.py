"""Daily prices from Yahoo Finance. The API reads these rows and does not call Yahoo."""

import datetime as dt
import logging
from decimal import Decimal

import pandas as pd
import yfinance as yf
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.ingest.xbrl import _upsert_companies
from app.models import Price

logger = logging.getLogger(__name__)

_PRICE_QUANTUM = Decimal("0.0001")


def prices_from_frame(frame: pd.DataFrame, tickers: list[str]) -> list[dict]:
    """Normalize a yfinance.download frame into price rows.

    Accepts either a single-ticker frame or a MultiIndex grouped by ticker
    or by price field. Rows with a missing close, adjusted close, or volume
    are skipped.
    """
    rows: list[dict] = []
    for ticker, single in _frames_by_ticker(frame, tickers).items():
        for label, record in single.iterrows():
            close = record.get("Close")
            adj_close = record.get("Adj Close")
            volume = record.get("Volume")
            if _missing(close) or _missing(adj_close) or _missing(volume):
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "date": _as_date(label),
                    "close": _money(close),
                    "adj_close": _money(adj_close),
                    "volume": int(volume),
                }
            )
    return rows


def ingest_prices() -> None:
    settings = get_settings()
    tickers = [company.ticker for company in settings.companies]
    logger.info("downloading 5y daily prices for %s", ", ".join(tickers))
    frame = yf.download(
        tickers,
        period="5y",
        auto_adjust=False,
        group_by="ticker",
        threads=False,
        progress=False,
    )
    if frame is None or frame.empty:
        raise RuntimeError("yfinance returned no prices")
    rows = prices_from_frame(frame, tickers)
    if not rows:
        raise RuntimeError("price download contained no usable rows")
    init_db()
    with SessionLocal() as session:
        _upsert_companies(session, settings.companies)
        _upsert_prices(session, rows)
        session.commit()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["ticker"]] = counts.get(row["ticker"], 0) + 1
    for ticker in tickers:
        logger.info("stored %s price rows for %s", counts.get(ticker, 0), ticker)


def _frames_by_ticker(frame: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    if not isinstance(frame.columns, pd.MultiIndex):
        if len(tickers) != 1:
            raise ValueError("flat price frame requires exactly one ticker")
        return {tickers[0]: frame}
    level0 = set(map(str, frame.columns.get_level_values(0)))
    level1 = set(map(str, frame.columns.get_level_values(1)))
    wanted = set(tickers)
    if wanted <= level0:
        return {ticker: frame[ticker] for ticker in tickers}
    if wanted <= level1:
        return {ticker: frame.xs(ticker, axis=1, level=1) for ticker in tickers}
    raise ValueError(f"price frame columns do not match tickers {tickers}")


def _upsert_prices(session: Session, rows: list[dict]) -> None:
    for start in range(0, len(rows), 1000):
        batch = rows[start : start + 1000]
        stmt = insert(Price).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker", "date"],
            set_={
                "close": stmt.excluded.close,
                "adj_close": stmt.excluded.adj_close,
                "volume": stmt.excluded.volume,
            },
        )
        session.execute(stmt)


def _as_date(label: object) -> dt.date:
    if isinstance(label, dt.datetime):
        return label.date()
    if isinstance(label, dt.date):
        return label
    return pd.Timestamp(label).date()


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(_PRICE_QUANTUM)


def _missing(value: object) -> bool:
    return value is None or bool(pd.isna(value))
