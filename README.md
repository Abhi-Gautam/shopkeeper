# Shopkeeper

This is a grocery shop with a model behind the counter. Customers come in and ask for things, and the model looks them up on the shelf and sells them. I am using it to learn how evals work, and to make the shopkeeper more correct, faster and cheaper one change at a time.

The shelf is 47,516 real US grocery products from Open Food Facts, in DuckDB, with made-up prices and stock. The model cannot touch it directly. DuckDB runs as an MCP server with two tools: `guide` searches the shelf and `buy` sells something. The model is `gpt-6-luna`, run with the OpenAI Agents SDK.

The customers are 33 short conversations in `floor/conversations.txt`, each with the order a good shopkeeper should end up selling. A run plays all of them through the shopkeeper on a fresh copy of the shelf, ten at a time, and every customer becomes a trace in Phoenix. Nothing is scored in code: the traces are read and judged against the written orders. You can also watch a run as a little shop floor in the browser.

To run it, copy `.env.example` to `.env` and add an OpenAI key, install `requirements.txt` into `.venv`, and put the Open Food Facts export in `store/off/off.duckdb`. Then `make db` builds the shelf, `make phoenix` starts Phoenix on port 6006, and `make run` plays the customers. `make run ARGS="--ui"` lets you watch them at http://127.0.0.1:8787, and `--limit 10` plays only the first ten. To talk to the shopkeeper yourself, run `.venv/bin/python agent/counter.py`.

The write-up of this work is in `docs/public/article.md`.
