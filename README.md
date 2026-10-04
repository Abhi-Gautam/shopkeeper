# Shopkeeper

A kirana counter on hosted free-tier inference. DuckDB is the shelf.
The model may only guide or buy. Jev and evals are later; the typed
boundary is already the two tool schemas.

You write the next decisions (prompt, admission, eval set). The store
load and the counter loop are scaffolding.

## Shape

```text
customer text
  -> agent/counter.py          OpenRouter free model, two tools only
       guide  -> DuckDB MCP    read, markdown, no stock change
       buy    -> DuckDB MCP    one transaction, request_id is idempotent
  -> reply
         traces -> Phoenix :6006
```

One DuckDB process owns `store/kirana.duckdb` and speaks MCP on stdin
as newline-delimited JSON. Built-in SQL tools are off. If the server
ever publishes anything other than `guide` and `buy`, the counter
refuses to start.

| Tool | Does | Must not |
|---|---|---|
| `guide` | Match name, brand, category, or SKU. Returns at most 12 rows, in-stock first. | Change stock. |
| `buy` | Decrement packs and insert a sale, only if `stock >= qty`. | Guess a SKU. Sell twice for one `request_id`. |

Money is integer paise. Stock is packs (`5 kg` with stock 12 is twelve
bags). About 8% of rows are empty on purpose so "we don't have it"
is a real path.

## Run

```bash
make db
make phoenix     # message viewer on http://localhost:6006
.venv/bin/python -m pip install -r requirements.txt   # once
.venv/bin/python agent/counter.py
```

`make serve` is the same MCP process the counter spawns itself. Do not
run both; two writers on `store/kirana.duckdb` lock or corrupt the shelf.

Copy `.env.example` to `.env`. Completions need `OPENROUTER_API_KEY`.
The model must be in `models.allowlist`. Default is `openrouter/free`,
which picks a free model at random and only from ones that can call
tools. The reply names the model that actually answered. Pin a `:free`
id in `.env` when a score has to be about one model.

## Floor

`make floor` serves the picture at http://127.0.0.1:8787. Staff is how
many workers pull from the line. Arrivals is how often a new customer
walks in. The two charts read Phoenix (`GET /v1/projects/default/spans`,
last minute), so they stay put if the page is refreshed. Bubbles come
from the runner, because Phoenix does not know which worker took which
customer.

The page is `ui/index.html` and `ui/shop.js`. Opened as a file, it plays
a tape. Served, it follows `/events`. One DuckDB process still owns the
shelf, so do not run this beside `scratch/run_prompts.py`.

`floor/conversations.txt` is the set to record. A `---` starts a new
customer. Lines after it share a memory. The earlier scratch prompts
do not.

## Traces

Phoenix lives in `docker-compose.yml` (`arizephoenix/phoenix`,
container `shopkeeper-phoenix`). `make phoenix` is `docker compose up -d`.
UI is [http://localhost:6006](http://localhost:6006). The counter posts
OTLP HTTP to `http://localhost:6006/v1/traces` (`PHOENIX_OTLP` overrides it).

Each turn is one agent span with child `llm` and `tool.guide` /
`tool.buy` spans. Messages and token counts are on the `llm` span.
The OpenRouter key is never an attribute. If Phoenix is down, the
exporter times out in 2 seconds and the sale still goes through.
`make db` rebuilds the shelf; it does not wipe traces.

## What is not in this cut

- Jev / structured intent before the tool call. The tool schema is the
  type check for now.
- A restock queue. Empty shelves come back as `out_of_stock`.
- More than one writer. Do not open the duckdb file from a second
  process while `make serve` or the counter is running.
- Evals. Score the tool call (right SKU, no double sale), not the Hindi.
