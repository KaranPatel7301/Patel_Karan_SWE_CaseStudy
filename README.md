# Fundamentals Tracker

A small service that tracks NVDA, MSFT, AAPL, GOOGL, and ETN from SEC filings and stored prices, and answers questions over the numbers and the 10-K narrative.

This project was built with Cursor. Each milestone was reviewed, tested, and verified against independent data.

## Run

```bash
cp .env.example .env
```

Put a real key in `LLM_API_KEY`, set `LLM_BASE_URL` and `LLM_MODEL` for your provider, then:

```bash
docker compose up
```

The first start builds the API image and loads `seed/seed.sql.gz` into an empty Postgres volume. The API listens on port 8000. Ingestion is not required to serve the seeded snapshot.

## Environment

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL. Compose sets `postgresql+psycopg://tracker:tracker@db:5432/tracker` for the API. |
| `SEC_USER_AGENT` | User-Agent sent on every SEC request during ingestion. SEC rejects requests without one. |
| `LLM_BASE_URL` | OpenAI-compatible API root, for example `https://api.openai.com/v1` or `https://generativelanguage.googleapis.com/v1beta/openai/`. A trailing slash is optional. The client strips it and calls `{base}/chat/completions`. |
| `LLM_API_KEY` | Bearer token for that endpoint. |
| `LLM_MODEL` | Model name sent on each chat completion. |
| `LLM_MAX_TOOL_ITERATIONS` | Cap on tool-calling rounds. The default is 6. Hitting the cap returns a decline and the tool trace. |

To point at another OpenAI-compatible proxy, change `LLM_BASE_URL` and `LLM_MODEL`. Both `https://proxy.example/v1` and `https://proxy.example/v1/` resolve to the same chat-completions URL.

## Model

Answers are produced with Google AI Studio, model `gemini-3.8-flash`, through the OpenAI-compatible endpoint `https://generativelanguage.googleapis.com/v1beta/openai/`.

## Ingestion and the seed

`make ingest` runs `python -m app.ingest.cli all` inside the API container. That refreshes company facts, the latest two 10-Ks, and daily prices from the SEC and Yahoo. The API does not call those services while answering a request.

`make dump` writes `seed/seed.sql.gz` with `pg_dump`. A fresh volume loads that file from `docker-entrypoint-initdb.d` on first boot:

```bash
docker compose down -v && docker compose up
```

## Yahoo check

`scripts/verify_against_yahoo.py` compares stored revenue, net income, and diluted EPS with Yahoo Finance annual income-statement figures whose period ends fall within seven days. Dollar lines must be within 0.5 percent of Yahoo. Diluted EPS must be within 0.011, which leaves room for split-adjusted rounding. Rows Yahoo does not publish are skipped. The script exits non-zero if any compared row disagrees.

```bash
docker compose exec -T api python scripts/verify_against_yahoo.py
```

The script calls Yahoo. It is a check after ingestion, not part of serving.

## Examples

Valuation joins the latest stored close to the latest annual diluted EPS. Price-to-sales uses that close, shares outstanding, and annual revenue.

```bash
curl -sS http://127.0.0.1:8000/companies/AAPL/valuation
```

```json
{
    "ticker": "AAPL",
    "price": 341.07,
    "price_date": "2026-09-25",
    "trailing_pe": {
        "value": 45.71983914209115,
        "reason": null,
        "fiscal_year": 2025,
        "period_end": "2025-09-27"
    },
    "price_to_sales": {
        "value": 12.110146596413408,
        "reason": null,
        "fiscal_year": 2025,
        "period_end": "2025-09-27"
    },
    "shares_outstanding": 14776353000.0,
    "shares_period_end": "2025-10-17"
}
```

Compare ranks one computed metric on each company's own latest fiscal year. Period ends are not aligned across the universe.

```bash
curl -sS "http://127.0.0.1:8000/compare?metric=gross_margin"
```

```json
{
    "metric": "gross_margin",
    "ranked": [
        {"ticker": "NVDA", "value": 0.7106808435754708, "fiscal_year": 2026, "period_end": "2026-01-25"},
        {"ticker": "MSFT", "value": 0.6794409337058032, "fiscal_year": 2026, "period_end": "2026-06-30"},
        {"ticker": "GOOGL", "value": 0.5965231508603998, "fiscal_year": 2025, "period_end": "2025-12-31"},
        {"ticker": "AAPL", "value": 0.4690516410716045, "fiscal_year": 2025, "period_end": "2025-09-27"},
        {"ticker": "ETN", "value": 0.3758743806470417, "fiscal_year": 2025, "period_end": "2025-12-31"}
    ],
    "unavailable": []
}
```

`POST /ask` takes `{"question": "..."}`. This response is from `gemini-3.8-flash` against the seeded database.

```bash
curl -sS http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What was MSFT revenue growth last year, and what was management'\''s explanation?"}'
```

```json
{
  "answer": "For the fiscal year ended June 30, 2026 (period_end: 2026-06-30), Microsoft's revenue and revenue growth were as follows:\n\n**Computed Numbers / Reported Facts:**\n- Fiscal Year 2026 Revenue: $331,839,000,000 (period_end: 2026-06-30), compared to $281,724,000,000 for Fiscal Year 2025 (period_end: 2025-06-30).\n- Revenue YoY Growth: 0.1778868679984666 (or approximately 17.79% / 18%).\n\n**Management Explanation (MD&A Excerpt):**\nAccording to Microsoft's MD&A in the latest Form 10-K (chunk_id: 9218):\n\"Revenue increased $50.1 billion or 18% driven by growth in Microsoft Cloud. Intelligent Cloud revenue increased driven by Azure. Productivity and Business Processes revenue increased driven by Microsoft 365 Commercial cloud. More Personal Computing revenue decreased driven by XBOX (formerly Gaming), offset in part by growth in Search advertising.\"",
  "declined": false,
  "data_used": "both",
  "sources": [
    {"type": "fact", "ticker": "MSFT", "concept": "revenue", "fiscal_year": 2026, "period_end": "2026-06-30"},
    {"type": "fact", "ticker": "MSFT", "concept": "revenue", "fiscal_year": 2025, "period_end": "2025-06-30"},
    {"type": "filing_chunk", "chunk_id": 9218, "ticker": "MSFT", "item": "mdna", "filing_fiscal_year": 2026}
  ],
  "tool_trace": [
    {"tool": "get_financials", "args": {"ticker": "MSFT", "metrics": ["revenue", "revenue_yoy"], "years": 2}, "ok": true},
    {"tool": "search_filings", "args": {"ticker": "MSFT", "item": "mdna", "k": 5, "filing": "latest", "query": "revenue increased Intelligent Cloud Productivity and Business Processes"}, "ok": true},
    {"tool": "final_answer", "args": {"declined": false, "data_used": "both"}, "ok": true}
  ]
}
```

The live `final_answer` arguments also echo the answer and sources. The trace above keeps the tool names, the data-tool arguments, and the outcome.
