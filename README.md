# Shopkeeper

This is a small grocery shop with a model behind the counter. Customers come in and ask for things, and the model looks them up on the shelf and sells them. I am using it to learn how evals work, and to make the shopkeeper more correct, faster and cheaper one change at a time.

The shelf is a DuckDB database of about 2,500 products. The model cannot touch it directly. DuckDB runs as an MCP server with two tools: `guide` searches the shelf and `buy` sells something. The model is `gpt-6-luna`, run with the OpenAI Agents SDK.

The customers are 100 short conversations in `floor/conversations.txt`. A run plays all of them through the shopkeeper on a fresh copy of the shelf, scores each conversation in code, and sends the traces and scores to Phoenix, so one run can be compared with the next. You can also watch a run as a little shop floor in the browser.

To run it, copy `.env.example` to `.env` and add an OpenAI key, install `requirements.txt` into `.venv`, then `make db` to build the shelf, `make phoenix` to start Phoenix on port 6006, and `make run ARGS="--ui"` to play the customers and watch them at http://127.0.0.1:8787. `--limit 10` plays only the first ten. To talk to the shopkeeper yourself, run `.venv/bin/python agent/counter.py`.

The write-up of this work is in `docs/public/article.md`.
