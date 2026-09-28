# Fundamentals Tracker

A FastAPI service that tracks five companies from SEC filings and Yahoo prices stored in Postgres, and answers natural-language questions over both the numbers and the filing text. Ingestion fills the database. A request, including `/ask`, only reads that snapshot.

Design writeup: [DESIGN.md](DESIGN.md)

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | Database status, row counts, latest price date |
| GET | `/companies` | Configured tickers |
| GET | `/companies/{ticker}/fundamentals?years=` | Reported annual facts |
| GET | `/companies/{ticker}/metrics?years=` | Margins, growth, free cash flow |
| GET | `/companies/{ticker}/valuation` | Trailing P/E, price to sales, price and as-of date |
| GET | `/companies/{ticker}/prices?start=&end=` | Daily prices |
| GET | `/companies/{ticker}/filings/search?q=&item=&fiscal_year=` | Ranked 10-K chunks |
| GET | `/compare?metric=` | One metric ranked across the five companies |
| POST | `/ask` | Agent answer, citations, and tool trace |

With the stack running, FastAPI lists every endpoint at [http://localhost:8000/docs](http://localhost:8000/docs).

## Setup

`.env` is gitignored. A fresh clone has `.env.example` only.

1. Clone the repo and enter it.

```bash
git clone https://github.com/KaranPatel7301/Patel_Karan_SWE_CaseStudy.git
cd Patel_Karan_SWE_CaseStudy
```

2. Copy the example env file.

```bash
cp .env.example .env
```

3. Open `.env` and fill in each variable.

| Variable | Required? | What it is for | Example |
|---|---|---|---|
| `LLM_BASE_URL` | Required for `/ask` | OpenAI-compatible API root. The client strips a trailing slash and calls `{base}/chat/completions`. | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `LLM_API_KEY` | Required for `/ask` | Bearer token for that endpoint. | `changeme` until you paste a real key |
| `LLM_MODEL` | Required for `/ask` | Model name sent on each chat completion. | `gemini-3.8-flash` |
| `SEC_USER_AGENT` | Only for `make ingest` | User-Agent on live SEC requests. The seeded service does not call the SEC. | `"Your Name your-email@example.com"` |
| `DATABASE_URL` | Leave the default | Postgres URL for the API. It matches `docker-compose.yml`. | `postgresql+psycopg://tracker:tracker@db:5432/tracker` |
| `LLM_MAX_TOOL_ITERATIONS` | Optional | Cap on tool-calling rounds. Hitting it returns a decline and the tool trace. | `6` |
| `COMPANIES_PATH` | Optional | Path to the universe file, read inside the API container. | `companies.yaml` |

The values above for `LLM_BASE_URL` and `LLM_MODEL` are what this project used: Google AI Studio, `gemini-3.8-flash`, via `https://generativelanguage.googleapis.com/v1beta/openai/`. To use another OpenAI-compatible proxy instead, set `LLM_BASE_URL` to that API root and `LLM_MODEL` to the model name. A trailing slash on the base URL is optional.

4. Build and start in the background.

```bash
docker compose up -d --build
```

5. Wait about 15 seconds, then check health. The seeded database should report these row counts:

```bash
curl -sS http://localhost:8000/health
```

```json
{
  "status": "ok",
  "database": "up",
  "row_counts": {
    "companies": 5,
    "financial_facts": 266,
    "prices": 6275,
    "filings": 10,
    "filing_sections": 20,
    "section_chunks": 1478,
    "risk_headings": 284
  },
  "latest_price_date": "2026-09-25"
}
```

Then ask one question. A successful call returns HTTP 200. For this question the answer should say operating margin was not reported in XBRL and should not invent a number.

```bash
curl -sS http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What was Eaton'\''s operating margin last year?"}'
```

6. The first boot on an empty volume loads `seed/seed.sql.gz` automatically, so no live SEC or Yahoo access is needed to run the service.

## Troubleshooting

Port 8000 is already in use. Another Compose stack is bound to it. From that other project directory, run `docker compose down`, then start this one again.

`/ask` returns 502. Check `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` in `.env`. Compose reads that file when the API container is created, so recreate it after any edit:

```bash
docker compose up -d --force-recreate api
```

Start fresh. `docker compose down -v` deletes the database volume. The next `docker compose up -d --build` loads `seed/seed.sql.gz` again.

Seed doesn't load (health shows zero rows). On Colima, only your home directory is shared with Docker by default. Clone under your home folder, not /tmp, then docker compose down -v and start again.

## Model

Answers are produced with Google AI Studio, model `gemini-3.8-flash`, through the OpenAI-compatible endpoint `https://generativelanguage.googleapis.com/v1beta/openai/`.

This project was built with Cursor. Each milestone was reviewed, tested, and verified against independent data.

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

## Tests

The API container has to be running. `make test` runs pytest inside it.

```bash
make test
```

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
  "answer": "**Computed Numbers:**\n- For the fiscal year ended 2026-06-30, Microsoft's revenue was $331.8B (compared to $281.7B for the fiscal year ended 2025-06-30), resulting in year-over-year revenue growth of 17.8%.\n\n**MD&A Excerpt (Management's Explanation):**\n- According to Microsoft's Item 7 (MD&A) in the 2026 10-K:\n  \"Revenue increased $50.1 billion or 18% driven by growth in Microsoft Cloud. Intelligent Cloud revenue increased driven by Azure. Productivity and Business Processes revenue increased driven by Microsoft 365 Commercial cloud. More Personal Computing revenue decreased driven by XBOX (formerly Gaming), offset in part by growth in Search advertising.\"",
  "declined": false,
  "data_used": "both",
  "sources": [
    {"type": "fact", "ticker": "MSFT", "concept": "revenue", "fiscal_year": 2026, "period_end": "2026-06-30"},
    {"type": "fact", "ticker": "MSFT", "concept": "revenue", "fiscal_year": 2025, "period_end": "2025-06-30"},
    {"type": "filing_chunk", "chunk_id": 9218, "ticker": "MSFT", "item": "mdna", "filing_fiscal_year": 2026}
  ],
  "tool_trace": [
    {"tool": "get_financials", "args": {"ticker": "MSFT", "metrics": ["revenue", "revenue_yoy"], "years": 2}, "ok": true},
    {"tool": "search_filings", "args": {"ticker": "MSFT", "item": "mdna", "filing": "latest", "query": "revenue increased driven by"}, "ok": true},
    {"tool": "final_answer", "args": {"declined": false, "data_used": "both"}, "ok": true}
  ]
}
```

The live `final_answer` arguments also echo the answer and sources. The trace above keeps the tool names, the data-tool arguments, and the outcome.
