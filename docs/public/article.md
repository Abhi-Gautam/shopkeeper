---
title: I put a model behind a grocery counter
description: I built a small grocery shop with a model behind the counter. This is how it works and how I measure it.
published: 2026-10-05
project: Shopkeeper
repository: https://github.com/Abhi-Gautam/shopkeeper
sourceCommit: 2cb8293ff5340a1c4c98f470cc5b87579717e0f7
---

A customer walks up to the counter and says, "A bag of rice, please." The shopkeeper checks the shelf, tells them what is there, and sells it.

I built a shop like that, with a model behind the counter. Over the next few posts I want to make it as good as I can: more correct, faster, and cheaper. Before changing anything, I needed to know how well it works today. This post is about that.

## What the model can do

The shelf is a DuckDB database with 2,520 products from global brands, priced in US dollars. About 7% of them are out of stock on purpose, because the shopkeeper should also be able to say "we don't have that".

The model cannot read the database directly. DuckDB runs as an MCP server and gives the model exactly two tools:

| Tool | What it does |
|---|---|
| `guide` | Searches the shelf and returns up to 12 products, in-stock ones first |
| `buy` | Sells a product if there is enough stock |

The model has no way to run its own SQL.

![The runner sends customers to the counter. The counter calls gpt-6-luna for each step and calls guide and buy on the DuckDB MCP server. Traces and scores go to Phoenix, and the run can also be shown as a shop floor.](/media/shopkeeper/setup.svg)

The model is `gpt-6-luna` from OpenAI, with reasoning set to low, and the OpenAI Agents SDK runs it. For each line a customer says, the model can make at most four calls to search, sell, and reply. It also remembers the whole conversation, including what the tools returned. So when the customer says "The 5 kg, if you have it", the model still knows which rice they were talking about.

## How I watch a run

There are 100 customers, each written as a short conversation in plain English. Some ask for things the shop does not sell, like phone chargers or onions. Some ask if they can pay later.

Every run starts with a fresh copy of the shelf, so sales from one run never affect the next one. Customers arrive at a fixed rate, and a fixed number of workers serve them. I can also watch the run as a shop floor, with the queue at the door, the workers, and every model call and tool call as it happens.

![The shop floor during a run, with customers at the counter, the workers, and the replies as they come in.](/media/shopkeeper/floor.mp4)

Each customer becomes one trace in Phoenix. Under the conversation, every line the customer said has its model calls and tool calls, with their timings, tokens, and inputs and outputs. The scores are attached to the conversation.

![One customer in Phoenix. The conversation has one line, two model calls and two guide searches, and the answered, cost and seconds scores.](/media/shopkeeper/phoenix-conversation.png)

The scores are plain code. They check what DuckDB actually did, not how the reply sounds. Did every line get a reply? Did every sale use a product that `guide` had shown? Did it sell when it should have? Was it the right product, size, and quantity? No model is used to grade another model.

## What the first run showed

As expected for a first run, things went wrong, and most of it was the search tool, not the model. It kept missing products we had, so about half of the customers were told we didn't have something that was on the shelf.

- The search matched only an exact phrase. "rice" worked, but "rice 5 kg" found nothing, and 116 of 171 searches came back empty.
- The model believed an empty search and told the customer we were out.
- Results came back cheapest first, 12 at most, so bigger packs like 5 kg rice never showed up.
- The model made up categories, like `tea`, and shop rules, like "we don't deliver".

| | |
|---|---|
| Customers who bought anything | 14 of 100 |
| Wait per line | 4.0 s median, 8.8 s p95, almost all of it the model |
| Tokens per customer | about 1,700 in, 108 out |
| Cost | $0.021 for all 100 customers |

Speed and cost are fine for now. The search comes first.

---

Checked against Shopkeeper commit [`2cb8293`](https://github.com/Abhi-Gautam/shopkeeper/commit/2cb8293ff5340a1c4c98f470cc5b87579717e0f7). The relevant code is the [shopkeeper](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/agent/counter.py), the [two tools](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/store/publish.sql), the [runner](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/floor/run.py), and the [scores](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/floor/score.py).
