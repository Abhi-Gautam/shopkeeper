---
title: I put a model behind a grocery counter
description: I built a small grocery shop with a model behind the counter. This is how it works and how I measure it.
published: 2026-10-05
project: Shopkeeper
repository: https://github.com/Abhi-Gautam/shopkeeper
sourceCommit: 5028e48523425e911d3ad13e161ee1d394d14fc9
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

## What changed in the next two runs

I made two changes. First, I put the list of everything the shop sells into the prompt, about 100 item names under their categories, and asked the model to work out every item a customer could mean and search for all of them at once. Second, I changed the search so it returns one row for each item and size, instead of the 12 cheapest products, so a 5 kg bag of rice can actually show up.

![Three runs compared. Customers who bought something went from 14% to 33% to 41%. Lines where the shopkeeper said we don't have it went from 63% to 8% to 5%. Searches that found nothing went from 68% to 3% to 5%.](/media/shopkeeper/runs.svg)

After both changes, 41 of 100 customers bought something, up from 14, and the shopkeeper said we didn't have something in 7 lines instead of 88. Of the six customers with a written order, five got exactly what they asked for, up from none. A line still takes about the same time, around 4.5 seconds, but the 100 customers cost $0.038 instead of $0.021, because the item list goes into every model call.

The item list only works because the shop is small. A store the size of Target would not fit in a prompt, and that is the next experiment.

## At Target size

On a real shelf of 47,516 US products from Open Food Facts, the item list cost only $0.004 per customer, but conversations took 30 seconds and hit rate limits, so I moved the work into the tool: `guide` now ranks products by the words in their brand, name and size, the prompt lists only the departments, and all 31 test customers got the right item in about 10 seconds each, at $0.0004 per customer.

---

Checked against Shopkeeper commit [`5028e48`](https://github.com/Abhi-Gautam/shopkeeper/commit/5028e48523425e911d3ad13e161ee1d394d14fc9). The relevant code is the [shopkeeper](https://github.com/Abhi-Gautam/shopkeeper/blob/5028e48523425e911d3ad13e161ee1d394d14fc9/agent/counter.py), the [two tools](https://github.com/Abhi-Gautam/shopkeeper/blob/5028e48523425e911d3ad13e161ee1d394d14fc9/store/publish.sql), the [runner](https://github.com/Abhi-Gautam/shopkeeper/blob/5028e48523425e911d3ad13e161ee1d394d14fc9/floor/run.py), and the [scores](https://github.com/Abhi-Gautam/shopkeeper/blob/5028e48523425e911d3ad13e161ee1d394d14fc9/floor/score.py).
