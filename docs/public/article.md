---
title: I put a model behind a grocery counter
description: I built a grocery shop with a model behind the counter, and changed it one step at a time on the same shelf and the same customers.
published: 2026-10-05
project: Shopkeeper
repository: https://github.com/Abhi-Gautam/shopkeeper
sourceCommit: e723f27d0d00dc0a7602099ccbf773a7f3513be5
---

A customer walks up to the counter and says, "Heinz ketchup, the 32 ounce bottle." The shopkeeper checks the shelf, finds it, and sells it.

I built a shop like that, with a model behind the counter, and I want to make it as good as I can: more correct, faster, and cheaper. This post is how it works, how I judge it, and every version it went through.

## The shop

The shelf is real. It has 47,516 US grocery products from Open Food Facts, with their real brands, names and pack sizes, like "Dave's Killer Bread Good Seed, 27 oz". Prices and stock are not in that data, so I made them up, and about 7% of products are out of stock on purpose, because the shopkeeper should also be able to say "we don't have that". The first versions ran on a small shop of 2,520 products taken from the same shelf.

The model cannot read the database directly. DuckDB runs as an MCP server and gives the model exactly two tools:

| Tool | What it does |
|---|---|
| `guide` | Searches the shelf and returns matching products with their size, price and stock |
| `buy` | Sells a product if there is enough stock |

The model has no way to run its own SQL. How `guide` searches is the thing that changed the most.

![The runner sends customers to the counter. The counter calls gpt-6-luna and calls guide and buy on the DuckDB MCP server. Every customer becomes a trace in Phoenix, and a run can also be shown as a shop floor.](/media/shopkeeper/setup.svg)

The model is `gpt-6-luna` from OpenAI, with reasoning set to low, and the OpenAI Agents SDK runs it. For each thing a customer says, the model can make at most four calls to search, sell, and reply. It also remembers the whole conversation, including what the tools returned. So when the customer says "The cheapest one", the model still knows they were talking about jasmine rice.

## How I judge a run

There are 33 customers, each a short conversation in plain English. Most of them want something specific, like two bags of Doritos Spicy Nacho in the 9.25 ounce size. Some want something that is out of stock, some ask for things a grocery shop does not sell, like phone chargers, and one asks to pay next week. Next to every customer I wrote down the order a good shopkeeper ends up selling, or that nothing should be sold.

Every run starts with a fresh copy of the shelf, so sales from one run never affect the next one. Ten customers are served at once. I can also watch a run as a shop floor, with the queue at the door, the workers, and every model call and tool call as it happens.

![The shop floor during a run, with customers at the counter, the workers, and the replies as they come in.](/media/shopkeeper/floor.mp4)

Each customer becomes one trace in Phoenix: everything they said, every model call and tool call under it, with timings, tokens, inputs and outputs. Nothing is scored in code. After each run, Claude reads every conversation in Phoenix next to the order written for that customer, and says which ones went right and why. A customer counts as right only if they got exactly their order, nothing missing and nothing extra, or were correctly sold nothing.

## The first shop

The first version ran on the small shop. `guide` matched the customer's words as one exact phrase against a product's name or brand, and returned the 12 cheapest matches.

7 of the 33 customers got the right order. Six of those were the customers who should not buy anything. The model put the whole request into one search, like "Heinz ketchup 32 ounce bottle", and since no product name contains that exact phrase, almost every search came back empty. The model believed the empty search and told the customer we did not have it.

## Putting the item list in the prompt

Next I put the list of everything the shop sells into the prompt, every product type under its department, and asked the model to search with those item names. Then I changed `guide` to return one row for each item and size, the cheapest one, instead of the 12 cheapest products.

Neither helped. 6 and then 8 customers got the right order. On real data the item names are labels like "Sour creams" or "Canned chickpeas", and they are not words in the product names, so searching with them still found nothing. Worse, the shopkeeper started selling whatever did come back: whole grain naan for someone who asked for Dave's Killer Bread, and corn chips for someone who asked for Doritos.

The list also does not scale. On the full shelf of 47,516 products it is about 27,000 tokens in every model call. The 33 customers cost $0.15 instead of $0.03, a customer took 9.3 seconds instead of 6.7, and 7 of 33 got the right order.

## Ranked search

So I moved the work out of the prompt and into the tool. `guide` now ranks products by how well the words in their brand, name, type and size match what the model searched for, with BM25 over a full-text index in DuckDB, and returns the best ten. "Folgers decaf" finds "Classic Decaf" by Folgers, and "Cheez-It 21" puts the 21 oz box first. The prompt lists only the store's departments, about 700 characters, however big the shelf gets.

On the full shelf, 23 of 33 customers got the right order, with no wrong sales at all. The search found the right product for every customer. The 33 customers cost $0.010, and a customer took 5.7 seconds.

That closes the work on search. Every fix that worked was the same kind of fix: when the shopkeeper got something wrong, the problem was what the tool gave the model, not what the prompt told it. An exact phrase match hid products the shop had, and a list of item names only worked while the names were clean and the shop was small. A ranked search finds the product from the customer's own words at any size of shop, in about 10 milliseconds. Almost all of the time left is the model.

## The steps around the tools

All 10 customers who still went wrong had named a product the search found. The model did every step of a sale itself. It searched, then it decided whether to sell, and once the sale went through, it was called once more just to read the sale back to the customer.

![Before: the customer's message goes to the model, which searches, reads the rows and calls buy, and then the model is called again to write the reply. A sale took three model calls.](/media/shopkeeper/flow-before.svg)

The prompt told the model to sell only when the customer asked to buy. So when a customer said "Heinz ketchup, the 32 ounce bottle", it quoted the price and asked "Would you like it?", and those 10 customers left without their order. And every sale spent one more model call, about two seconds, on a sentence that only repeated what was sold.

So I changed two things. A customer naming a product is now an order: when exactly one product fits, the shopkeeper sells it. And once the sale goes through, the counter prints the receipt itself instead of asking the model to write it. If the customer asked something else in the same message, like "and do you deliver?", only that part goes back to the model, after the sale.

![After: the model searches and sells when one product fits, the counter prints the receipt, and the model is called again only if the customer asked something else. A sale takes two model calls.](/media/shopkeeper/flow-after.svg)

27 of 33 customers got the right order, up from 23. A message with a sale now takes 2.1 model calls instead of 2.5, and 4.3 seconds instead of 4.8. A whole customer still takes about 5.7 seconds, and the run cost $0.013, because more customers buy.

The six that went wrong show the next problem. Four customers got too much: the shopkeeper sold on their first message, and then treated their next one, like "Just one box" or "The cheapest one", as another order. The other two asked for something with two near-identical choices, like two Goya chickpea listings at different prices, and the shopkeeper asked which one.

Reading these traces also caught a bug that no score would have. Every sale in one customer message shared one idempotency key, so when a customer asked for ketchup and mustard together, the mustard was reported as sold but never left the shelf. Each sale now has its own key, and the numbers above are from the run after the fix.

## A model for the decision

Those six mistakes are all the same decision: is this message an order, for which product, and how many. That decision does not need a model that writes text. It is a choice between a few answers, and gpt-6-luna spends about two seconds on each one.

[Jev](https://openrouter.ai/docs/guides/community/jev), from TypeSafe, is built for exactly this. You send it a state and a few typed questions, a yes or no, or a pick from a list, and it sends back a probability for each answer, in about a third of a second. It never writes a reply.

So now the model only searches and talks, and it no longer has the `buy` tool at all. After every search, Jev reads the conversation, every receipt already printed, and the products the search found, and answers five questions: does the latest message order a product, which of these products was searched for, how many does the customer want in total, how many different products does the message order, and does it also ask something else. When Jev is sure about the order, the product and the amount, the counter subtracts what this customer already bought, sells the rest, and prints the receipt. When it is not sure, the model replies as before. A message like "The cheaper one", which picks from products already shown, goes to Jev before the model, and often needs no model call at all.

![With Jev: the model searches, Jev decides after each search, the counter sells and prints the receipt, and the model is called again only when Jev is not sure or the customer asked for more. A plain sale takes one model call.](/media/shopkeeper/flow-jev.svg)

Getting the questions right took three versions, and each time the traces showed what was wrong. In the first, the question about the order counted any question in the message as a no, so "Barilla penne, and is there parking?" was not an order, and "ketchup and mustard" sold only the ketchup; 28 of 33 customers got their order. In the second, I asked Jev how many more to sell after what was already sold. That is arithmetic, the one thing TypeSafe says Jev is weak at, and its confidence on that question fell below the bar for most customers; only 14 of 33 got their order. In the third, every question tests exactly one thing, Jev only reads what the customer said, and code does the counting. Before the second and third runs, I checked the questions on messages that are not among the 33, and the confidence bars never changed.

30 of 33 customers got the right order, with no wrong sales and no extra ones. "The Eggo one. Just one box" now sells nothing more, because the counter knows one box is already sold. A customer takes 4.0 seconds instead of 5.7, a message with a sale 2.4 seconds instead of 4.3, and the 33 customers cost $0.0095 instead of $0.0126, Jev's share included. The three that went wrong were really unclear: two regular Campbell's soups under different names, a regular and an organic Dave's Killer Bread loaf, and "Then the big one" after a reply that never mentioned the big bag.

![All seven versions on the same 33 customers. Customers who got the right order: 7, 6, 8 on the small shop, 7 with the item list on the full shelf, 23 with ranked search, 27 with a printed receipt, 30 with Jev deciding the sale. Seconds per customer: 5.7, 6.6, 6.7, 9.3, 5.7, 5.7, 4.0. Cost for the 33 customers: $0.0063, $0.0259, $0.0268, $0.1455, $0.0099, $0.0126, $0.0095.](/media/shopkeeper/stages.svg)

---

Checked against Shopkeeper commit [`e723f27`](https://github.com/Abhi-Gautam/shopkeeper/commit/e723f27d0d00dc0a7602099ccbf773a7f3513be5). The relevant code is the [shopkeeper](https://github.com/Abhi-Gautam/shopkeeper/blob/e723f27d0d00dc0a7602099ccbf773a7f3513be5/agent/counter.py), the [two tools](https://github.com/Abhi-Gautam/shopkeeper/blob/e723f27d0d00dc0a7602099ccbf773a7f3513be5/store/publish.sql), the [runner](https://github.com/Abhi-Gautam/shopkeeper/blob/e723f27d0d00dc0a7602099ccbf773a7f3513be5/floor/run.py), and the [Jev decision](https://github.com/Abhi-Gautam/shopkeeper/blob/e723f27d0d00dc0a7602099ccbf773a7f3513be5/agent/jev.py).
