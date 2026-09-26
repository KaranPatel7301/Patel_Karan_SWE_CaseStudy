# Fundamentals Tracker: Build Spec

Context file for Cursor. Read this before generating code. It is the source of truth for scope, structure, and conventions.

## 1. What we are building

A small, runnable service that tracks five companies from SEC filings and market data, and answers natural-language questions over both the numbers and the filing narrative.

- Universe (config-driven, never hardcoded): NVDA, MSFT, AAPL, GOOGL, ETN
- Stack: Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2.x, Pydantic v2, httpx, yfinance, BeautifulSoup, OpenAI-compatible client
- One command brings everything up: `docker compose up`
- The audience is a PM or analyst. Answers must be grounded, cite sources, show period end dates, and decline when data is missing.

## 2. Working agreements for Cursor

- Never hardcode tickers, CIKs, or company names in code. Everything comes from `companies.yaml`.
- Never invent XBRL tag names. When unsure, inspect the actual companyfacts JSON.
- Type hints everywhere. Pydantic models for every API request and response.
- Ingestion and serving are separate. The API never calls SEC or Yahoo at request time.
- The LLM never computes numbers. Metrics are computed in Python and passed to the model.
- Add a dependency only when it clearly saves time. Prefer the standard library and the stack above.
- Keep functions small and testable. Log decisions such as which XBRL tag was used and which span was picked as a section.
- Build in the milestone order in section 10. Do not start the agent until the numbers are verified.

## 3. Repo layout

```
.
├── app/
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # pydantic-settings, loads env + companies.yaml
│   ├── db.py                # engine, session
│   ├── models.py            # SQLAlchemy tables
│   ├── schemas.py           # Pydantic API models
│   ├── ingest/
│   │   ├── cli.py           # python -m app.ingest.cli [xbrl|filings|prices|all]
│   │   ├── sec_client.py    # httpx client, User-Agent, rate limit
│   │   ├── xbrl.py          # companyfacts -> normalized annual facts
│   │   ├── filings.py       # 10-K fetch, section extraction, chunking, risk headings
│   │   └── prices.py        # yfinance daily prices
│   ├── services/
│   │   ├── metrics.py       # margins, growth, FCF, P/E, P/S
│   │   ├── search.py        # Postgres full-text search over chunks
│   │   └── risk_diff.py     # heading diff between latest and prior 10-K
│   ├── agent/
│   │   ├── tools.py         # tool schemas + dispatch to services
│   │   ├── loop.py          # tool-calling loop, final_answer validation
│   │   └── prompts.py       # system prompt
│   └── api/
│       └── routes.py
├── companies.yaml
├── seed/
│   └── seed.sql.gz          # committed pg_dump, auto-loaded on first boot
├── tests/
├── docker-compose.yml
├── Dockerfile
├── Makefile                 # ingest, dump, test
├── .env.example
├── README.md
└── DESIGN.md
```

## 4. Configuration

`companies.yaml`:

```yaml
companies:
  - ticker: NVDA
    cik: "0001045810"
    name: NVIDIA
  - ticker: MSFT
    cik: "0000789019"
    name: Microsoft
  - ticker: AAPL
    cik: "0000320193"
    name: Apple
  - ticker: GOOGL
    cik: "0001652044"
    name: Alphabet
  - ticker: ETN
    cik: "0001551182"
    name: Eaton
```

Verify each CIK against EDGAR before trusting it.

`.env.example`:

```
DATABASE_URL=postgresql+psycopg://tracker:tracker@db:5432/tracker
SEC_USER_AGENT="Karan <your-email@example.com>"
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=changeme
LLM_MODEL=changeme
LLM_MAX_TOOL_ITERATIONS=6
```

## 5. Data model

```
companies(ticker PK, cik, name)

financial_facts(
  id PK, ticker FK, concept,          -- normalized name, e.g. revenue
  fiscal_year int, period_start date, period_end date,
  value numeric, unit text,           -- USD or USD/shares
  source_tag text,                    -- XBRL tag that supplied the value
  accession text, filed date,
  UNIQUE(ticker, concept, period_end)
)

prices(ticker FK, date, close numeric, adj_close numeric, volume bigint,
       PRIMARY KEY(ticker, date))

filings(id PK, ticker FK, accession UNIQUE, form, fiscal_year int,
        period_end date, filed date, url text)

filing_sections(id PK, filing_id FK, item text,   -- risk_factors | mdna
                text text, char_count int, extraction_method text)

section_chunks(id PK, section_id FK, ordinal int, text text,
               tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED)
  -- GIN index on tsv

risk_headings(id PK, filing_id FK, ordinal int, heading text)
```

## 6. Ingestion

### SEC client
- Every request sends `User-Agent` from `SEC_USER_AGENT`. Requests without it fail.
- Throttle below 10 requests per second. Retry with backoff on 429 and 5xx.
- Endpoints:
  - Company facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`
  - Submissions: `https://data.sec.gov/submissions/CIK{cik}.json`
  - Filing document: `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primaryDocument}`

### XBRL normalization (the hard part, test it well)

Concept map with tags in priority order:

| concept | tags |
|---|---|
| revenue | RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, SalesRevenueNet, RevenueFromContractWithCustomerIncludingAssessedTax |
| cost_of_revenue | CostOfRevenue, CostOfGoodsAndServicesSold, CostOfGoodsSold |
| gross_profit | GrossProfit (fallback: revenue minus cost_of_revenue) |
| operating_income | OperatingIncomeLoss (do not derive when absent) |
| net_income | NetIncomeLoss |
| eps_diluted | EarningsPerShareDiluted |
| eps_basic | EarningsPerShareBasic |
| operating_cash_flow | NetCashProvidedByUsedInOperatingActivities |
| capex | PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets |
| shares_outstanding | dei:EntityCommonStockSharesOutstanding, us-gaap:CommonStockSharesOutstanding (point in time, no duration filter) |

Rules:
1. Keep only facts where `form == "10-K"`.
2. Keep only full-year durations: `end - start` between 350 and 380 days.
3. Do not use the `fy` field as the period's fiscal year. It is the fiscal year of the filing, so prior-year comparatives carry the wrong label. Derive `fiscal_year` from `period_end.year` and always store `period_end`.
4. For duplicate periods, keep the value from the most recent `filed` date. This picks up restatements and split-adjusted EPS (NVDA 10:1 in 2024, GOOGL 20:1 in 2022).
5. Resolve tag fallback per period, not per company. Companies switch tags over the years (AAPL moved off SalesRevenueNet around 2018).
6. Record `source_tag` on every row.
7. Keep about the last 5 fiscal years.

Checkpoint: print a table of revenue, net income, and diluted EPS for all five companies and spot check each against the latest 10-K income statement before moving on.

### 10-K sections
1. From submissions `filings.recent`, take the latest two entries with `form == "10-K"` (exclude 10-K/A).
2. Fetch the primary document. Remove hidden inline XBRL (`ix:header`, elements with `display:none`). Convert to text with newline separators and normalize whitespace.
3. Extract sections with case-insensitive regexes anchored near line starts:
   - Risk factors: from `Item 1A. Risk Factors` to `Item 1B` (fallback end: `Item 2`)
   - MD&A: from `Item 7. Management's Discussion` to `Item 7A` (fallback end: `Item 8`)
4. The first match is usually the table of contents. Collect all candidate spans and keep the longest.
5. MD&A uses a three-step strategy:
   1. Take the Item 7 span from step 3. If it is at least 5,000 characters, store it as `item = mdna` with `extraction_method = regex`.
   2. If that span is under 5,000 characters, search for the heading `MANAGEMENT'S DISCUSSION AND ANALYSIS` (case-insensitive). Use the occurrence that is not in the table of contents and not the Item 7 cross-reference, and extract until the next major heading such as `QUANTITATIVE AND QUALITATIVE DISCLOSURES`, `REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM`, or the start of the financial statements. If that span is at least 5,000 characters, store it as `item = mdna` with `extraction_method = heading`.
   3. If both MD&A attempts are under 5,000 characters, or risk-factor extraction is under 5,000 characters, log the failure and store the full document text as `item = full_text` with `extraction_method = fallback`. `full_text` is only the last resort.
6. Chunk by paragraph into roughly 1,200 characters with a small overlap.
7. Risk headings: from the Risk Factors HTML, collect bold or italic paragraphs (`<b>`, `<strong>`, `font-weight:700|bold`, `font-style:italic`) between 40 and 400 characters. Store in order.

### Prices
- `yfinance.download(tickers, period="5y", auto_adjust=False)` so both `close` and `adj_close` are stored.
- Upsert on `(ticker, date)`.

### Seed
- `make ingest` runs `python -m app.ingest.cli all` inside the api container.
- `make dump` writes `seed/seed.sql.gz` via `pg_dump`.
- Compose mounts `seed/` into `/docker-entrypoint-initdb.d/` so a fresh volume loads the snapshot automatically.

## 7. Metrics (services/metrics.py)

All computed in Python from `financial_facts` and `prices`:

- gross_margin = gross_profit / revenue
- operating_margin = operating_income / revenue
- net_margin = net_income / revenue
- revenue_yoy, net_income_yoy, eps_yoy = (current / prior) - 1
- free_cash_flow = operating_cash_flow - capex
- trailing_pe = latest close / latest annual eps_diluted
- price_to_sales = (latest close * shares_outstanding) / latest annual revenue

Every metric response includes `fiscal_year`, `period_end`, and for valuation, `price_date`. Return `null` with a reason instead of guessing when an input is missing.

Use `close`, not `adj_close`, for P/E. Both are split adjusted, but `adj_close` is also dividend adjusted.

## 8. API

| Method | Path | Returns |
|---|---|---|
| GET | /health | db status, row counts, latest price date |
| GET | /companies | configured universe |
| GET | /companies/{ticker}/fundamentals?years=3 | reported facts with period_end and source_tag |
| GET | /companies/{ticker}/metrics?years=3 | margins, growth, FCF |
| GET | /companies/{ticker}/valuation | trailing P/E, P/S, price and as-of date |
| GET | /companies/{ticker}/prices?start=&end= | daily prices |
| GET | /compare?metric=gross_margin | metric across all companies for each one's latest fiscal year, with period_end shown |
| GET | /companies/{ticker}/filings/search?q=&item=&fiscal_year= | ranked chunks with filing metadata |
| POST | /ask | agent answer (schema below) |

Unknown ticker returns 404 with a clear message. Unknown metric returns 422.

## 9. Agent layer

### Client
OpenAI-compatible chat completions with tool calling, configured from `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. No framework needed.

### Tools (thin wrappers over services, same code paths as the API)
- `list_companies()`
- `get_financials(ticker, metrics: list[str], years: int)`
- `compare_companies(metric: str)`
- `get_valuation(ticker)`
- `search_filings(ticker, query, item: risk_factors|mdna|any, filing: latest|prior|any, k: int = 5)`
- `diff_risk_factors(ticker)`: fuzzy-matches latest vs prior headings (rapidfuzz `token_set_ratio`, threshold about 85) and returns added and removed headings. Deterministic, no LLM.
- `final_answer(answer, sources, data_used, declined)`: the model must end by calling this.

Use a `final_answer` tool instead of `response_format` JSON schema because tool calling is more widely supported across proxied models.

### Loop
1. System prompt plus user question.
2. Loop up to `LLM_MAX_TOOL_ITERATIONS`: execute tool calls, append results as JSON.
3. Validate `final_answer` with Pydantic.
4. Guardrail: if `declined` is false and `sources` is empty, override to a decline. An answer with no supporting data is not returned.
5. If the iteration limit is hit, return a decline with the tool trace.

### Response schema

```json
{
  "answer": "string",
  "declined": false,
  "data_used": "numbers | text | both | none",
  "sources": [
    {"type": "fact", "ticker": "MSFT", "concept": "revenue", "fiscal_year": 2025, "period_end": "2025-06-30"},
    {"type": "filing_chunk", "chunk_id": 123, "ticker": "MSFT", "item": "mdna", "filing_fiscal_year": 2025}
  ],
  "tool_trace": [{"tool": "get_financials", "args": {}, "ok": true}]
}
```

### System prompt rules
- Use only data returned by tools. Never use prior knowledge for numbers or filing content.
- State fiscal period end dates. Fiscal years differ across companies.
- "Right now" means the latest price in the database. Say the date.
- Decline forward guidance, estimates, price targets, and companies outside the universe. Say what data is available instead.
- For "why" questions, pair numbers with MD&A excerpts and label which is which.

### Acceptance questions (all must pass before writing docs)
1. NVDA revenue and net income for the last three fiscal years (numbers)
2. Highest gross margin last year across the five (numbers, comparative)
3. AAPL trailing P/E right now (cross-source)
4. New NVDA risk factors in the latest 10-K vs the prior year (text, via diff tool)
5. MSFT revenue growth last year and management's explanation (both)
6. Forward guidance for next quarter (must decline)

## 10. Build order

1. Compose, Dockerfile, config, models, `/health`. Stack boots.
2. SEC client and XBRL normalizer. Checkpoint: numbers match the filings.
3. Prices, metrics service, fundamentals/metrics/valuation/compare endpoints.
4. Filing fetch, section extraction, chunks, FTS search endpoint. Checkpoint: every section passes the length check or is logged as a fallback.
5. Risk headings and diff.
6. Agent tools, loop, `/ask`. Run the six acceptance questions.
7. Seed dump. Test on a fresh volume: `docker compose down -v && docker compose up`.
8. Tests, README, DESIGN.md.

## 11. Tests (proportional to a prototype)

- XBRL normalizer on a small fixture: duplicate period across filings, a restated EPS value, a tag switch between years, a quarterly fact that must be filtered out.
- Section extractor on a saved HTML snippet containing a table of contents plus the real section.
- Metric math, including missing inputs returning null.
- Agent with a mocked LLM: a normal tool path and the empty-sources decline override.

## 12. Deliberately out of scope

- Quarterly and TTM figures (annual only)
- Scheduled refresh and incremental ingestion
- Auth and multi-user concerns
- Embeddings and vector search (Postgres FTS is enough and has no extra dependency on the model proxy)
- Forward estimates of any kind
- Frontend

Optional if time remains: Form 4 insider transactions from EDGAR as the bonus dataset.

## 13. README checklist
- One command to run
- Env vars table, including how to point at a different model
- Provider and exact model used
- How to re-run ingestion and regenerate the seed
- 2 to 3 examples: curl for API endpoints and `/ask` questions with responses

## 14. DESIGN.md outline (1 to 2 pages)
- Architecture and data model, and why
- Source integration, especially the P/E join and period alignment
- Where the LLM is in the path (routing, synthesis) and where it is not (numbers, risk diff, search), and why
- Failure modes handled (missing tags, bad section parse, ungrounded answers)
- What was cut and why

## 15. Submission
- Repo name: `LastName_FirstName_SWE_CaseStudy`
- Email fecooteam@schonfeld.com within 72 hours with the required public-information statement (copy the exact wording from the original prompt)
