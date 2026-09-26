"""Cross-check stored revenue, net income, and diluted EPS against Yahoo Finance.

Rerun after an ingestion change, from the api container:

    docker compose exec -T api python scripts/verify_against_yahoo.py
"""

import os

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text

eng = create_engine(os.environ["DATABASE_URL"])
labels = {"revenue": "Total Revenue", "net_income": "Net Income", "eps_diluted": "Diluted EPS"}

with eng.connect() as c:
    rows = c.execute(text("""
        SELECT ticker, concept, fiscal_year, period_end, value
        FROM financial_facts
        WHERE concept IN ('revenue', 'net_income', 'eps_diluted')
        ORDER BY ticker, concept, fiscal_year
    """)).all()

yahoo = {t: yf.Ticker(t).income_stmt for t in sorted({r.ticker for r in rows})}

print(f"{'TICKER':6} {'CONCEPT':12} {'FY':5} {'OURS':>20} {'YAHOO':>20} RESULT")
mismatches = 0
for r in rows:
    df = yahoo[r.ticker]
    label = labels[r.concept]
    if df is None or df.empty or label not in df.index:
        continue
    cols = [col for col in df.columns if abs((pd.Timestamp(col).date() - r.period_end).days) <= 7]
    if not cols or pd.isna(df.loc[label, cols[0]]):
        continue
    ours, theirs = float(r.value), float(df.loc[label, cols[0]])
    tol = 0.011 if r.concept == "eps_diluted" else abs(theirs) * 0.005
    result = "OK" if abs(ours - theirs) <= tol else "CHECK"
    if result != "OK":
        mismatches += 1
    print(f"{r.ticker:6} {r.concept:12} {r.fiscal_year:<5} {ours:>20,.2f} {theirs:>20,.2f} {result}")

if mismatches:
    raise SystemExit(f"{mismatches} rows did not match Yahoo Finance")
