# Shopkeeper

A neighborhood grocery counter run by a model, used to learn evals: how a
change to the prompt, the model or the tools moves accuracy, speed and
cost. DuckDB is the shelf. The model may only `guide` or `buy`.

```text
conversations.txt ─► floor/run.py ─► agent/counter.py ─► OpenAI (Agents SDK)
                        │                 ├─ guide ─► DuckDB MCP  read only
                        │                 └─ buy   ─► DuckDB MCP  one transaction
                        ├─ score.py ─► Phoenix experiment (one row per conversation)
                        ├─ traces ───► Phoenix project (one trace per conversation)
                        └─ --ui ─────► page at http://127.0.0.1:8787
```

## Run

```bash
cp .env.example .env            # add the OpenAI key
.venv/bin/python -m pip install -r requirements.txt
make db                         # build the shelf
make phoenix                    # http://localhost:6006
make run ARGS="--limit 10"      # play, score, log the first ten
make run ARGS="--limit 10 --ui" # same, and watch it
.venv/bin/python agent/counter.py   # talk to the counter yourself
```

A run prints its scores and the Phoenix experiment link. Compare runs in
Phoenix under Datasets → the `conversations-…` dataset → Experiments.

## Files

| Path | Is |
|---|---|
| `agent/counter.py` | The counter: system prompt, the two tools, one turn. The SDK runs the loop and the MCP client. |
| `agent/trace.py` | Phoenix wiring. The instrumentor writes model and tool spans; this adds `conversation` and `utterance`. |
| `floor/conversations.txt` | The customers. `---` starts one; `=` lines say what a good counter sells. |
| `floor/run.py` | Plays conversations at `--arrival` per minute across `--staff` workers, on a fresh copy of the shelf. |
| `floor/score.py` | Code scores and the Phoenix experiment upload. |
| `ui/` | The page `--ui` serves. Opened as a file, it plays a recorded sample. |
| `store/` | Catalog generator, schema, and the MCP surface (`publish.sql`). |

## The shelf

About 2,500 SKUs of global brands, priced in USD as integer cents. Stock
is packs (`5 kg` with stock 12 is twelve bags). About 7% of rows are
empty on purpose, so "we don't have it" is a real path.

| Tool | Does | Must not |
|---|---|---|
| `guide` | Match name, brand, category or SKU. At most 12 rows, in stock first. | Change stock. |
| `buy` | Decrement packs and record a sale, only if `stock >= qty`. | Sell twice for one `request_id`. |

The model never sees `request_id`; the counter adds it to `buy`, one per
turn. If the MCP process ever publishes a tool other than these two, the
counter refuses to start.

## Scores

Code, not a judge. Each reads what DuckDB answered, per conversation:

| Score | Question |
|---|---|
| `answered` | Did every turn get a real reply (not empty, stuck or an error)? |
| `grounded` | Did every buy use a SKU that `guide` had shown? |
| `sale_decision` | Sold when it should, held back when it should not? Needs `=`. |
| `right_items` | Right product, pack and quantity? Needs `=`. |
| `seconds` | Wall time for the conversation, under the run's load. |
| `cost_usd` | Tokens at list price (`PRICES` in `score.py`). |

## Traces

One trace per conversation in project `PHOENIX_PROJECT`:

```text
conversation                     one customer, session.id = customer id
└─ utterance                     one line: request_id, shopkeeper.outcome
   └─ Agent workflow             the SDK run (the instrumentor adds two levels)
      └─ counter                 the agent
         └─ turn                 one model step
            ├─ response          the model call: messages, tokens
            └─ guide / buy       the tool call: arguments, result
```

`shopkeeper.outcome` is `replied`, `empty` or `stuck` (4 model calls
without an answer). The last two mark the utterance and its conversation
as errors, so Phoenix's error chart counts failed turns. The
instrumentor replaces the SDK's own exporter, so nothing goes to OpenAI's
trace dashboard. If Phoenix is down, the run still plays and prints its
scores.
