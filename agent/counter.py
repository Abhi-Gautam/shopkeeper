#!/usr/bin/env python3
"""Grocery counter on the OpenAI Agents SDK.

The model sees two tools, both owned by the DuckDB MCP process:
  guide  search the shelf
  buy    sell a SKU guide returned

SHOPKEEPER_FLOW picks who replies after a sale. model: the model reads
the sale and writes the reply. receipt: a named product is an order, the
counter prints the sale itself, and the model is called again only for
whatever else the customer asked in that message.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import openai
from agents import (
    Agent,
    FunctionTool,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelSettings,
    OpenAIResponsesModel,
    RunHooks,
    Runner,
    ToolsToFinalOutputResult,
)
from agents.mcp import MCPServerStdio
from openai.types.shared import Reasoning
from opentelemetry.trace import Status, StatusCode

from models import (
    ENV_DB, ENV_KEY, BuyArgs, BuyRequest, BuyStatus, Flow, GuideArgs, ItemType,
    LlmDone, LlmStart, Offer, Outcome, Role, Sale, Settings, Step, Tool, ToolDone,
    ToolStart, buy_result_of, offers_of, said, shown_offers,
)
from trace import PROJECT, flush, start as start_trace, step_span, tool_span, turn_span

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "store" / "publish.sql"
# Read at import: the floor copies the shelf before .env loads.
DB = Path(os.environ.get(ENV_DB) or ROOT / "store" / "shop.duckdb")

AGENT_NAME = "counter"
MCP_NAME = "shelf"
MCP_TIMEOUT_SECONDS = 30
MAX_STEPS = 4  # model calls per customer message before the counter gives up
MAX_TOKENS = 400
MAX_RETRIES = 4
TIMEOUT_SECONDS = 60
ERROR_CHARS = 500
REPLY_CHARS = 4000
REQUEST_ID = "request_id"
REST = "rest"
RECEIPT_SPAN = "receipt"

STUCK = "Counter is stuck in tools. Say it again, shorter."
MISSING_KEY = f"{ENV_KEY} is unset. Copy .env.example to .env."

RECEIPT_LINE = "Sold: {qty} x {product}, ${price} each. {left} left."
UNKNOWN_PRODUCT = "item {sku}"
# Told to the model when it is called back for what a sold message left open.
REST_NOTE = """
The sale in this line is done and the customer has its receipt. Do not mention it or sell it again.
Answer only this part of what they said: {rest}
"""

REST_SCHEMA = {"type": "string", "description": (
    "Anything else the customer asked in this line that the sale does not answer, "
    "in their words. Empty when the sale is all they asked for.")}

BUY_RULE = {
    Flow.MODEL: "- buy: sell only when the customer has asked to buy AND you have a SKU from guide. Never invent a SKU. Pass that SKU and the pack count.",
    Flow.RECEIPT: "- buy: a customer naming a product at the counter is ordering it. When exactly one in-stock row from guide fits everything they said (brand, product, size, the one they picked), call buy at once with that SKU and the count they said, or 1. Do not ask them to confirm. Ask a question only when several different products fit, or none does. Never invent a SKU. In rest, put anything else they asked in the same line.",
}

SYSTEM = """You are the counter at a neighborhood grocery store.
Reply in plain English. Short. Prices come only from tool results, in USD.

You have two tools and no others:
- guide: look up what is actually on the shelf. Call this before you name a price, a pack, or a substitute. Also call it when the request is vague ("something for breakfast", "which flour is better").
{buy_rule}

If guide says in_stock is false, do not call buy. Offer another row from the same guide result, or say you will note it.
If buy status is sold, confirm pack, price, and stock left. If out_of_stock or unknown_sku, say so and guide again.
Do not mention tools, SKU format rules, or that a database exists.

The shop's departments, so you know what kind of shop this is: {aisles}.

Search guide with the words the customer used for each item: brand, product, flavor. One item per call; for several items, call guide for each in the same step. If the first search misses, try other words before saying the shop does not have it. Pick the size, price and quantity from the rows that come back.
"""

BUY_SLOT, AISLES_SLOT, CATALOG_SLOT = "{buy_rule}", "{aisles}", "{catalog}"
CATALOG_SQL = ("SELECT string_agg('- ' || category || ': ' || items, chr(10) ORDER BY category) FROM "
               "(SELECT category, string_agg(DISTINCT item, ', ' ORDER BY item) AS items "
               "FROM products GROUP BY category)")
AISLES_SQL = ("SELECT string_agg(category, ', ' ORDER BY n DESC) FROM "
              "(SELECT category, count(*) AS n FROM products GROUP BY category)")


class ModelError(RuntimeError):
    """A model call that failed after the client's own retries."""


def load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def aisles_of(db: Path) -> str:
    """The departments, biggest first, straight from the shelf."""
    return subprocess.run(["duckdb", "-readonly", "-noheader", "-list", str(db), "-c", AISLES_SQL],
                          capture_output=True, text=True, check=True).stdout.strip()


def catalog_of(db: Path) -> str:
    """One line per department with its item names, straight from the shelf."""
    return subprocess.run(["duckdb", "-readonly", "-noheader", "-list", str(db), "-c", CATALOG_SQL],
                          capture_output=True, text=True, check=True).stdout.strip()


def prompt_of(path: str | None, buy_rule: str, db: Path) -> str:
    """SYSTEM, or a prompt file from an experiment. Either may ask for the
    departments or the full item list, which are read from the shelf."""
    text = Path(path).read_text() if path else SYSTEM
    for slot, fill in ((BUY_SLOT, lambda: buy_rule), (AISLES_SLOT, lambda: aisles_of(db)),
                       (CATALOG_SLOT, lambda: catalog_of(db))):
        if slot in text:
            text = text.replace(slot, fill())
    return text


def receipt(sales: list[Sale]) -> str:
    """What the counter prints for the sales in one line."""
    lines = []
    for sale in sales:
        offer = sale.offer
        product = (f"{offer.brand} {offer.name}, {offer.pack_label}" if offer
                   else UNKNOWN_PRODUCT.format(sku=sale.result.sku))
        lines.append(RECEIPT_LINE.format(qty=sale.result.qty_sold, product=product,
                                         price=sale.result.price_usd, left=sale.result.stock_left))
    return "\n".join(lines)


@dataclass
class Turn:
    """Run context. The tools and hooks read it; the model never sees it."""
    request_id: str
    model: str
    offers: dict[str, Offer]
    watch: Callable[[dict], None] | None = None
    sales: list[Sale] = field(default_factory=list)
    printed: bool = False  # the last run ended on a printed receipt
    step: int = 0
    rows: int = 0
    llm_row: int = 0
    began: float = 0.0

    def tell(self, event: Step):
        if self.watch:
            try:
                self.watch(event.model_dump(mode="json"))
            except Exception:
                # A page that cannot keep up must not lose the sale.
                pass

    def row(self) -> int:
        self.rows += 1
        return self.rows


class Steps(RunHooks):
    """Model-call events for the floor. Phoenix gets its model spans from
    the instrumentor, not from here."""

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        turn = context.context
        turn.step += 1
        turn.llm_row = turn.row()
        turn.began = time.monotonic()
        turn.tell(LlmStart(step=turn.step, row=turn.llm_row, model=turn.model))

    async def on_llm_end(self, context, agent, response):
        turn = context.context
        usage = response.usage
        turn.tell(LlmDone(
            step=turn.step, row=turn.llm_row, model=turn.model,
            seconds=round(time.monotonic() - turn.began, 3),
            prompt_tokens=usage.input_tokens,
            cached_tokens=usage.input_tokens_details.cached_tokens or 0,
            completion_tokens=usage.output_tokens,
            wants=[item.name for item in response.output
                   if getattr(item, "type", None) == ItemType.FUNCTION_CALL],
        ))


def stop_after_sale(context, results) -> ToolsToFinalOutputResult:
    """The receipt flow: a step whose sales all went through ends the model's
    part of the line. Anything else, like out of stock, goes back to it."""
    buys = [r for r in results if r.tool.name == Tool.BUY]
    if not buys or any(buy_result_of(r.output).status != BuyStatus.SOLD for r in buys):
        return ToolsToFinalOutputResult(is_final_output=False)
    turn = context.context
    turn.printed = True
    return ToolsToFinalOutputResult(is_final_output=True, final_output=receipt(turn.sales))


class Shelf:
    """The DuckDB MCP process, the model client, and the event loop the
    SDK runs on.

    Callers are plain threads (the floor's workers). They hand each turn to
    this one loop and wait for it, so one MCP session serves everyone.
    """

    def __init__(self, db: Path = DB):
        self.settings = Settings.from_env()
        if not Path(db).exists():
            raise SystemExit(f"missing {db}. Run `make db` from the shopkeeper directory.")
        if not self.settings.api_key:
            raise SystemExit(MISSING_KEY)
        self.model = self.settings.model
        self.flow = self.settings.flow
        # Read before the MCP process opens the file for writing.
        self.publish = Path(self.settings.publish or PUBLISH)
        self.instructions = prompt_of(self.settings.prompt, BUY_RULE[self.flow], db)
        self.client = openai.AsyncOpenAI(api_key=self.settings.api_key,
                                         base_url=self.settings.base_url,
                                         max_retries=MAX_RETRIES, timeout=TIMEOUT_SECONDS)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server = MCPServerStdio(
            params={"command": "duckdb", "args": ["-unsigned", "-init", str(self.publish), str(db)]},
            name=MCP_NAME, client_session_timeout_seconds=MCP_TIMEOUT_SECONDS)
        self.run(self.server.connect())
        listed = self.run(self.server.list_tools())
        if {tool.name for tool in listed} != set(Tool):
            raise SystemExit(f"refusing store that publishes {sorted(t.name for t in listed)}")
        # DuckDB answers one call at a time. Waiting for it is contention,
        # not query time, so the two are reported apart.
        self.pipe = asyncio.Lock()
        self.tools = [self.wrap(tool) for tool in listed]

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def wrap(self, tool) -> FunctionTool:
        name = Tool(tool.name)
        schema = json.loads(json.dumps(tool.input_schema))
        if name == Tool.BUY:
            # The model never writes the idempotency key. One per customer message.
            schema["properties"].pop(REQUEST_ID, None)
            schema["required"] = [k for k in schema.get("required", []) if k != REQUEST_ID]
            if self.flow == Flow.RECEIPT:
                schema["properties"][REST] = REST_SCHEMA

        async def invoke(ctx, raw: str) -> str:
            turn = ctx.context
            if name == Tool.GUIDE:
                text = await self.call(turn, name, GuideArgs.model_validate_json(raw))
                turn.offers.update({offer.sku: offer for offer in offers_of(text)})
                return text
            args = BuyArgs.model_validate_json(raw)
            text = await self.call(turn, name, BuyRequest(sku=args.sku, qty=args.qty,
                                                          request_id=turn.request_id))
            result = buy_result_of(text)
            if result.status == BuyStatus.SOLD:
                turn.sales.append(Sale(offer=turn.offers.get(args.sku), result=result,
                                       rest=args.rest))
            return text

        return FunctionTool(name=name, description=tool.description or "",
                            params_json_schema=schema, on_invoke_tool=invoke,
                            strict_json_schema=False)

    async def call(self, turn: Turn, name: Tool, args: GuideArgs | BuyRequest) -> str:
        """One MCP tool call, timed and traced, reported to the floor."""
        arguments = args.model_dump(mode="json", exclude_none=True)
        row = turn.row()
        turn.tell(ToolStart(step=turn.step, row=row, name=name, args=arguments))
        queued = time.monotonic()
        async with self.pipe:
            began = time.monotonic()
            with tool_span(name, arguments) as span:
                result = await self.server.call_tool(name, arguments)
                text = "\n".join(getattr(c, "text", "") for c in result.content)
                span.set_attribute("output.value", text)
                span.set_status(Status(StatusCode.OK))
        done = time.monotonic()
        turn.tell(ToolDone(step=turn.step, row=row, name=name, args=arguments, result=text,
                           seconds=round(done - began, 4), waited=round(began - queued, 4)))
        return text

    def agent(self, extra: str = "") -> Agent:
        effort = self.settings.effort
        return Agent(
            name=AGENT_NAME,
            instructions=self.instructions + extra,
            model=OpenAIResponsesModel(self.model, self.client),
            model_settings=ModelSettings(
                max_tokens=MAX_TOKENS,
                reasoning=Reasoning(effort=effort) if effort else None),
            tools=self.tools,
            tool_use_behavior=stop_after_sale if self.flow == Flow.RECEIPT else "run_llm_again",
        )

    def close(self):
        try:
            self.run(self.client.close())
            self.run(self.server.cleanup())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


def turn(shelf: Shelf, history: list, utterance: str, request_id: str,
         watch=None, session=None, parent=None) -> str:
    """One utterance in, one reply out. Blocks the calling thread.

    `history` is the SDK's input list and is replaced in place, so the
    next turn sees this turn's tool calls and results, not just its text.
    `watch` gets one dict per step as it finishes.
    `parent` is the trace context of the conversation this turn belongs to.
    """
    return shelf.run(_turn(shelf, history, utterance, request_id, watch, session, parent))


async def _answer(shelf: Shelf, agent: Agent, items: list, context: Turn) -> tuple[str, list]:
    """One run of the model over `items`; the reply and the items after it."""
    try:
        result = await Runner.run(agent, items, context=context,
                                  max_turns=MAX_STEPS, hooks=Steps())
    except MaxTurnsExceeded:
        return STUCK, [*items, said(Role.ASSISTANT, STUCK)]
    except openai.APIError as exc:
        code = getattr(exc, "status_code", None) or "timeout"
        raise ModelError(f"model {code}: {str(exc)[:ERROR_CHARS]}") from exc
    except ModelBehaviorError as exc:
        # e.g. the reply ran past max_tokens and came back incomplete.
        raise ModelError(f"model reply unusable: {str(exc)[:ERROR_CHARS]}") from exc
    text = str(result.final_output or "")
    after = result.to_input_list()
    if context.printed:
        # The receipt is the counter's reply, not the model's; the next run must see it.
        after.append(said(Role.ASSISTANT, text))
        context.printed = False
    return text, after


async def _turn(shelf, history, utterance, request_id, watch, session, parent):
    items = [*history, said(Role.USER, utterance)]
    context = Turn(request_id=request_id, model=shelf.model,
                   offers=shown_offers(history), watch=watch)
    try:
        with turn_span(request_id, utterance, shelf.model, session, parent) as span:
            text, history[:] = await _answer(shelf, shelf.agent(), items, context)
            rest = " ".join(sale.rest for sale in context.sales if sale.rest.strip())
            if context.sales and shelf.flow == Flow.RECEIPT:
                with step_span(RECEIPT_SPAN, text) as receipt_span:
                    receipt_span.set_attribute(REST, rest)
                if rest:
                    # The rest of the message goes back through the counter, sale already done.
                    sold = len(context.sales)
                    more, history[:] = await _answer(shelf, shelf.agent(REST_NOTE.format(rest=rest)),
                                                     history, context)
                    if len(context.sales) > sold:
                        more = receipt(context.sales[sold:])
                    text = f"{text}\n{more}"
            outcome = (Outcome.STUCK if text == STUCK else
                       Outcome.REPLIED if text.strip() else Outcome.EMPTY)
            # Empty and stuck are failures, so Phoenix counts them as errors.
            span.set_attribute("shopkeeper.outcome", outcome)
            span.set_status(Status(StatusCode.OK) if outcome == Outcome.REPLIED
                            else Status(StatusCode.ERROR, outcome))
            span.set_attribute("output.value", text[:REPLY_CHARS])
            return text
    finally:
        flush()


def main():
    load_dotenv()
    start_trace()
    shelf = Shelf()
    history = []
    print(f"counter up on {shelf.model}, {shelf.flow} flow. empty line to leave.", file=sys.stderr)
    print(f"traces: http://localhost:6006 project {PROJECT}", file=sys.stderr)
    try:
        while True:
            try:
                utterance = input("> ").strip()
            except EOFError:
                break
            if not utterance:
                break
            print(turn(shelf, history, utterance, uuid.uuid4().hex))
    finally:
        shelf.close()


if __name__ == "__main__":
    main()
