---
title: I put a model behind a grocery counter
description: I built a small grocery shop with a model behind the counter. This is how it works, how I measure it, and what happened in the first full run.
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

The model has no way to run its own SQL. If the server ever offered a third tool, the shopkeeper would refuse to start.

Each sale carries a `request_id`. If `buy` is called again with the same id, it does not sell a second time. The model never picks this id. The shopkeeper adds a new one for every line the customer says, so a line that gets retried cannot sell twice.

![The runner sends customers to the counter. The counter calls gpt-6-luna for each step and calls guide and buy on the DuckDB MCP server. Traces and scores go to Phoenix, and the run can also be shown as a shop floor.](/media/shopkeeper/setup.svg)

The model is `gpt-6-luna` from OpenAI, with reasoning set to low, and the OpenAI Agents SDK runs it. For each line a customer says, the model can make at most four calls to search, sell, and reply. It also remembers the whole conversation, including what the tools returned. So when the customer says "The 5 kg, if you have it", the model still knows which rice they were talking about.

## How I watch a run

There are 100 customers, each written as a short conversation in plain English. Some ask for things the shop does not sell, like phone chargers or onions. Some ask if they can pay later.

Every run starts with a fresh copy of the shelf, so sales from one run never affect the next one. Customers arrive at a fixed rate, and a fixed number of workers serve them. I can also watch the run as a shop floor, with the queue at the door, the workers, and every model call and tool call as it happens.

Each customer becomes one trace in Phoenix:

```text
conversation        one customer, with their scores
└─ utterance        one line they said
   ├─ Response      a model call, with messages and tokens
   ├─ guide         what the model searched for and what came back
   └─ buy           the product, the quantity, and whether it sold
```

The scores are plain code. They check what DuckDB actually did, not how the reply sounds. Did every line get a reply? Did every sale use a product that `guide` had shown? Did it sell when it should have? Was it the right product, size, and quantity? No model is used to grade another model.

For the first ten customers, I also wrote down what the shopkeeper should end up selling:

```text
A bag of rice, please.
The 5 kg, if you have it.
= rice | 5 kg | 1
```

## The first run

I ran all 100 customers through the shop, at 12 customers a minute with 3 workers:

| | |
|---|---|
| Lines that got a reply | 140 of 140 |
| Sales that used a product from `guide` | 20 of 20 |
| Customers who bought anything | 14 of 100 |
| Customers who got exactly what they asked for, of the six with a written answer | 0 of 6 |
| Time per line | 4.0 seconds median, 8.8 seconds at p95 |
| Total cost | $0.021 for all 100 customers |

The shopkeeper replied every time, and it never sold anything it had not looked up first. But only 14 of the 100 customers left with something.

The replies show what went wrong:

```text
> I need oil for frying. Which one is the better deal?
< I don't have any frying oils on the shelf right now.
> The 1 litre.
< Sorry, I don't have a 1-litre frying oil in stock.
```

The shelf had 13 packs of Mazola Sunflower Oil, 1 litre.

## Why it does not sell

The model called `guide` 199 times, and 138 of those searches returned nothing.

`guide` looks for the whole search text as one exact phrase:

```sql
WHERE (name ILIKE '%' || $query || '%'
    OR brand ILIKE '%' || $query || '%'
    OR category ILIKE '%' || $query || '%'
    OR sku ILIKE '%' || $query || '%')
  AND ($category IS NULL OR category = $category)
```

A search for `sunflower oil 1 litre` finds nothing, because no product name contains those exact words in that order. 104 of the empty searches failed like this.

The other 33 asked for a category that does not exist. The real categories have names like `tea_coffee` and `grains`, but the model asked for `tea`, `cookies`, and `rice`.

When a search comes back empty, the model believes it and tells the customer the shop does not have the item. Fixing that is where the next post starts.

## The limits of this run

This is a single run, with one model at one reasoning setting. The times were measured with 3 workers and 12 customers a minute, so they include some waiting for the shared database.

Only the first ten customers have a written expected answer, so "got exactly what they asked for" covers just six customers. The other numbers cover all 100.

The scores only check what was sold. They do not check whether the reply was polite, clear, or quoted the right price.

---

Checked against Shopkeeper commit [`2cb8293`](https://github.com/Abhi-Gautam/shopkeeper/commit/2cb8293ff5340a1c4c98f470cc5b87579717e0f7). The relevant code is the [shopkeeper](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/agent/counter.py), the [two tools](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/store/publish.sql), the [runner](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/floor/run.py), and the [scores](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/floor/score.py).
