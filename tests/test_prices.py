import datetime as dt
from decimal import Decimal

import pandas as pd

from app.ingest.prices import prices_from_frame


def test_prices_from_frame_keeps_close_and_adj_close() -> None:
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    columns = pd.MultiIndex.from_product([["NVDA"], ["Close", "Adj Close", "Volume"]])
    frame = pd.DataFrame(
        [[10.5, 9.25, 1000], [11.0, 9.5, 1100]],
        index=index,
        columns=columns,
    )
    rows = prices_from_frame(frame, ["NVDA"])
    assert len(rows) == 2
    assert rows[0]["date"] == dt.date(2024, 1, 2)
    assert rows[0]["close"] == Decimal("10.5000")
    assert rows[0]["adj_close"] == Decimal("9.2500")
    assert rows[0]["volume"] == 1000
    assert rows[0]["close"] != rows[0]["adj_close"]


def test_prices_from_frame_grouped_by_field() -> None:
    index = pd.to_datetime(["2024-06-03"])
    columns = pd.MultiIndex.from_product(
        [["Close", "Adj Close", "Volume"], ["MSFT"]],
    )
    frame = pd.DataFrame([[400.125, 390.5, 50]], index=index, columns=columns)
    rows = prices_from_frame(frame, ["MSFT"])
    assert rows == [
        {
            "ticker": "MSFT",
            "date": dt.date(2024, 6, 3),
            "close": Decimal("400.1250"),
            "adj_close": Decimal("390.5000"),
            "volume": 50,
        }
    ]


def test_prices_from_frame_skips_missing_closes() -> None:
    index = pd.to_datetime(["2024-01-02"])
    frame = pd.DataFrame(
        {"Close": [float("nan")], "Adj Close": [1.0], "Volume": [10]},
        index=index,
    )
    assert prices_from_frame(frame, ["AAPL"]) == []
