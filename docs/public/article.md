---
title: I put a model behind a grocery counter
description: A small shop where a model can only look up the shelf or sell, the first numbers I could trust, and why it almost never sells.
published: 2026-10-05
project: Shopkeeper
repository: https://github.com/Abhi-Gautam/shopkeeper
sourceCommit: 2cb8293ff5340a1c4c98f470cc5b87579717e0f7
---

A customer walks up to the counter and says, "A bag of rice, please." The shopkeeper checks the shelf, says what is there, and sells it.

I built that shop with a model behind the counter. The plan is to make it as good as it can get: right more often, faster, and cheaper. This is the starting point. Before changing anything, I wanted to know how it does now.

## The counter can only look or sell

The shelf is a DuckDB database with 2,520 products from global brands, priced in USD. About 7% of them are out of stock on purpose, so "we don't have that" is a real answer the counter has to give.

The model never sees the database. DuckDB runs as an MCP server, and it publishes two tools:

| Tool | What it does |
|---|---|
| `guide` | Searches the shelf and returns up to 12 rows, in stock first |
| `buy` | Sells a SKU if there is enough stock, in one transaction |

There is no SQL tool and no third tool. If the server ever published one, the counter would refuse to start.

`buy` takes a `request_id`, and a second call with the same id does not sell again. The model never chooses that id. The counter adds one for every line the customer says, so a retried line cannot sell twice.

![The runner plays customers through the counter. The counter calls gpt-6-luna, and the guide and buy tools on the DuckDB MCP server. Every run is traced and scored in Phoenix, and can be drawn as a shop floor.](/media/shopkeeper/setup.svg)

The model is `gpt-6-luna` with reasoning set to low, run by the OpenAI Agents SDK. For each line a customer says, it gets at most four model calls to look things up, sell, and answer. It keeps the whole conversation, including what the tools returned, so "the 5 kg, if you have it" still refers to the rice from the line before.

## The first numbers were about the setup, not the model

The first version ran on free models through OpenRouter's free router. I collected 727 customer lines and went to read them.

44% had come back empty. Most of those failed in a fraction of a second, before any model answered. They were rate limits, not the model saying nothing. The free router also picked a different model for each call, so 412 of those lines were answered by more than one model. And every model call and tool call in the traces lasted about zero milliseconds, because the spans were written after each call had already returned.

None of that said anything about the counter. A score built on it would have measured rate limits and a random model mix.

So the counter moved to one paid model on one provider. Model calls are now timed around the real request, and a failed call shows up as an error instead of an empty reply.

## Watching a run

The 100 customers are short conversations in plain English. Some are simple. Some ask for things the shop does not sell, like phone chargers or onions. Some ask whether they can pay later.

Each run plays them on a fresh copy of the shelf, so one run's sales never change the next run's stock. Customers walk in at a set rate, and a set number of workers serve them. A run can be drawn as a shop floor: the line at the door, the workers, and each model call and tool call as it happens.

Every customer is one trace:

```text
conversation        one customer, with its scores
└─ utterance        one line they said
   ├─ Response      a model call: messages and tokens
   ├─ guide         what it searched for and the rows that came back
   └─ buy           SKU, quantity, and whether it sold
```

The scores are code. They read what DuckDB answered, not how the reply sounds: did every line get a reply, did every sale use a SKU that `guide` had shown, did it sell when it should, and was it the right product, pack size and quantity. No model grades another model.

For the first ten customers, I wrote down what a good counter would sell:

```text
A bag of rice, please.
The 5 kg, if you have it.
= rice | 5 kg | 1
```

## It always answers, and it almost never sells

One run of all 100 customers, at 12 customers a minute with 3 workers:

| | |
|---|---|
| Lines that got a reply | 140 of 140 |
| Sales that used a SKU from `guide` | 20 of 20 |
| Customers who bought anything | 14 of 100 |
| Right items, for the six who should buy | 0 of 6 |
| Time per line | 4.0 s median, 8.8 s p95 |
| Cost | $0.021 for all 100 customers |

It never failed to answer, and it never sold something it had not looked up. But only 14 of 100 customers left with anything.

The replies explain why:

```text
> I need oil for frying. Which one is the better deal?
< I don't have any frying oils on the shelf right now.
> The 1 litre.
< Sorry, I don't have a 1-litre frying oil in stock.
```

Mazola Sunflower Oil, 1 L, had 13 packs on the shelf.

## Most searches find nothing

The model called `guide` 199 times. 138 of those searches came back with no rows.

`guide` matches the whole query as one piece of text:

```sql
WHERE (name ILIKE '%' || $query || '%'
    OR brand ILIKE '%' || $query || '%'
    OR category ILIKE '%' || $query || '%'
    OR sku ILIKE '%' || $query || '%')
  AND ($category IS NULL OR category = $category)
```

So `sunflower oil 1 litre` finds nothing, because no product name contains that exact phrase. 104 of the empty searches were queries like that.

The other 33 asked for a category that does not exist. The categories are fixed names like `tea_coffee` and `grains`. The model asked for `tea`, `cookies` and `rice`.

When a search comes back empty, the model believes it and tells the customer the shop has none. That is where the next part starts.

## What this baseline does not say

This is one run, with one model at one reasoning setting. Time per line was measured with 3 workers and 12 customers a minute, so it includes waiting for the shared DuckDB process.

Only the first ten customers have a written expected outcome, so the right-items score covers six customers. The other scores cover all 100.

The scores check what was sold. They do not check whether the reply was polite, clear, or correct about prices.

---

Checked against Shopkeeper commit [`2cb8293`](https://github.com/Abhi-Gautam/shopkeeper/commit/2cb8293ff5340a1c4c98f470cc5b87579717e0f7). The relevant code is the [counter](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/agent/counter.py), the [two tools](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/store/publish.sql), the [runner](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/floor/run.py), and the [scores](https://github.com/Abhi-Gautam/shopkeeper/blob/2cb8293ff5340a1c4c98f470cc5b87579717e0f7/floor/score.py).
