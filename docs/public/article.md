---
title: A shopkeeper agent, and where it starts
description: The setup and the first measurements of a model-run grocery counter that I am going to optimize for accuracy, speed and cost.
published: 2026-10-05
project: Shopkeeper
repository: https://github.com/Abhi-Gautam/shopkeeper
sourceCommit: 2cb8293ff5340a1c4c98f470cc5b87579717e0f7
---

I built a small grocery shop where a model works the counter. Customers walk in and say what they want. The model looks things up on the shelf and sells.

The plan is to take this setup and optimize it as far as it goes: more right answers, faster replies, lower cost. This article is the starting point. It covers what the shop is, how I watch it, and how it does before any tuning.

![The runner sends customer lines to the counter. The counter calls gpt-6-luna and the guide and buy tools on a DuckDB MCP server. Traces and scores go to Phoenix, and the runner can also draw the shop floor page.](/media/shopkeeper/setup.svg)

## The shelf

The shelf is a DuckDB database with 2,520 products from global brands, priced in USD. About 7% of them are out of stock on purpose, so "we don't have it" is a real answer the counter has to give.

The model never touches the database directly. DuckDB runs as an MCP server that publishes exactly two tools:

| Tool | Does |
|---|---|
| `guide` | Searches the shelf by name, brand, category or SKU. Returns up to 12 rows, in stock first. |
| `buy` | Sells a SKU if there is enough stock, in one transaction. |

Each tool is a SQL query registered with the server. `buy` takes a `request_id`, and a second call with the same id does not sell again. The model never sees that id. The counter adds one per customer line, so a retried line cannot sell twice.

## The counter

The counter is the model plus a short system prompt, run by the OpenAI Agents SDK. For each line a customer says, the model can call the tools and then answer, using at most four model calls. If it needs more, the line counts as stuck.

The model keeps the whole conversation, including what the tools returned. So when a customer says "the 5 kg, if you have it", the model still has the rice it found a line earlier.

For this baseline the model is `gpt-6-luna` with reasoning set to low.

## The customers

There are 100 customers, written as short conversations in plain English:

```text
---
A bag of rice, please.
The 5 kg, if you have it.
= rice | 5 kg | 1
---
Do you sell phone chargers?
= none
```

Some ask for things the shop does not sell, like phone chargers or onions. Some ask about paying later or delivery. For the first ten, the `=` lines say what a good counter should end up selling: one 5 kg bag of rice above, nothing at all for the chargers.

## Running it

One command plays the customers through the counter. They walk in at a set rate, 12 a minute here, and a set number of workers, 3 here, serve them. A customer stays with one worker for the whole conversation. Every run starts on a fresh copy of the shelf, so one run's sales never change the next run's stock.

With `--ui`, the run is also drawn as a shop floor: the line at the door, the workers, each model call and tool call as it happens.

## Watching it

Every customer is one trace in Phoenix:

```text
conversation        one customer, with the scores attached
└─ utterance        one line the customer said
   ├─ Response      a model call: messages, tokens
   ├─ guide         the search and the rows it returned
   └─ buy           SKU, quantity, status
```

The model calls are traced by the OpenInference OpenAI instrumentation. The counter writes the other spans itself.

Each finished conversation is scored in code. The scores go onto its trace and into a Phoenix experiment, so two runs can be compared side by side.

| Score | Question |
|---|---|
| `answered` | Did every line get a real reply, not an empty or stuck one? |
| `grounded` | Did every sale use a SKU that `guide` had shown? |
| `sale_decision` | Did it sell when it should, and hold back when it should not? |
| `right_items` | Right product, pack size and quantity? |
| `seconds` | How long did the conversation take? |
| `cost_usd` | What did its tokens cost at list price? |

No model grades another model here. The scores read what DuckDB answered. `sale_decision` and `right_items` need the `=` lines, so they only cover the first ten customers for now.

## The baseline

One run of all 100 customers on 2026-10-05, with `gpt-6-luna` at low reasoning, 3 workers and 12 customers a minute:

| Measure | Result |
|---|---|
| Lines that got a reply | 140 of 140 |
| Sales using a SKU from `guide` | 20 of 20 |
| Customers who bought anything | 14 of 100 |
| Right sale decision, first ten | 5 of 10 |
| Exactly the right items, of the six who should buy | 0 of 6 |
| Time per line | 4.0 s median, 8.8 s p95 |
| Model calls per line | 2 for most lines, 3 or 4 for 26 of them |
| Cost | $0.021 for all 100 customers |

The counter always answers, and it never sold something the shelf had not shown it. But it rarely sells. Only 14 of 100 customers left with anything, and none of the six with a written order got exactly what they asked for.

Speed and cost are not the problem yet. Correctness is, and that is where the next part starts.
