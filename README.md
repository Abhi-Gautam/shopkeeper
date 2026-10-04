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
walks in.

Three surfaces, one event stream:

| Surface | Is | Reads |
|---|---|---|
| canvas | the room, 448x252 native, scaled by whole numbers | `arrive` `assign` `step` `reply` `leave` |
| overlay | every word on the picture, as DOM so it stays sharp | the same events |
| rail | one card per turn, step by step, newest first | `step` `reply` |

Nothing on the page is invented. Token counts are the OpenRouter usage
block, step times are measured around the calls themselves, and the
`guide` / `buy` fields are parsed from what DuckDB answered. If a number
is on screen, an event carried it.

The events:

```text
hello    staff, arrival, model, workers, queue cap, conversations, tally
config   a slider moved
arrive   cid, text, turn n of m   queued=true is the door, false a follow-up
assign   cid, tid, worker, waited   how long they stood in the line
step     llm.start | llm | tool.start | tool | retry
reply    text, status, seconds, tokens, served models
leave    cid walks out
drop     the line was full
queue    depth, cap, which workers are busy
stats    running tally of sold / guide / out, and guide / buy calls
metrics  tokens per minute and p95, from Phoenix or from this process
```

One customer is a whole conversation, not one line. `cid` is stable
across every line they say, so a follow-up ("the 5 kg, if you have it")
draws as the same person with the same memory, on the same worker. `tid`
is one utterance, and is the `request_id` the `buy` tool is idempotent on.

A guide call sweeps the shelves. A sold `buy` puts a bag on the counter
and the customer carries it out. An `out_of_stock` blinks an empty gap.
The waterfall in the rail is to scale against the wall clock of the turn,
so the model call dwarfing the DuckDB call is the first thing you see:
`guide` is tens of milliseconds, the completion is seconds.

Tool calls take turns on the one DuckDB process. That wait is reported
apart from the query, as `waited ... on the pipe`, so contention is
visible instead of hiding inside the tool time.

Phoenix is the second opinion on tokens and p95, not the only one. If it
is down the page falls back to this process's own completed turns and the
card says `this floor` instead of `phoenix`.

The page is `ui/index.html` and `ui/shop.js`. Opened as a file, it plays
a tape that exercises the whole protocol. Served, it follows `/events`.
One DuckDB process still owns the shelf, so do not run this beside
`scratch/run_prompts.py`.

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
