#!/usr/bin/env python3
"""Grocery counter on the OpenAI Agents SDK.

The model only sees two tools, both owned by the DuckDB MCP process:
  guide  read stock, prices, substitutes
  buy    sell a SKU the guide already returned

The SDK runs the loop and speaks MCP. This file decides only what the SDK
cannot: which two tools exist, that buy gets the turn's request_id from
us and never from the model, and what each turn reports.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
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
)
from agents.mcp import MCPServerStdio
from openai.types.shared import Reasoning
from opentelemetry.trace import Status, StatusCode

from trace import PROJECT, flush, start as start_trace, tool_span, turn_span

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "models.allowlist"
# SHOPKEEPER_SHOP=big plays the Open Food Facts shelf (make bigdb) instead
# of the small made-up one.
SHOP = os.environ.get("SHOPKEEPER_SHOP", "small")
if SHOP not in ("small", "big"):
    raise SystemExit(f"SHOPKEEPER_SHOP must be small or big, not {SHOP}")
DB = ROOT / "store" / ("big.duckdb" if SHOP == "big" else "shop.duckdb")
PUBLISH = ROOT / "store" / "publish.sql"

# Model calls per turn before the counter gives up.
MAX_STEPS = 4
STUCK = "Counter is stuck in tools. Say it again, shorter."

SYSTEM = """You are the counter at a small neighborhood grocery store.
Reply in plain English. Short. Prices come only from tool results, in USD.

You have two tools and no others:
- guide: look up what is actually on the shelf. Call this before you name a price, a pack, or a substitute. Also call it when the request is vague ("something for breakfast", "which flour is better").
- buy: sell only when the customer has asked to buy AND you have a SKU from guide. Never invent a SKU. Pass that SKU and the pack count.

If guide says in_stock is false, do not call buy. Offer another row from the same guide result, or say you will note it.
If buy status is sold, confirm pack, price, and stock left. If out_of_stock or unknown_sku, say so and guide again.
Do not mention tools, SKU format rules, or that a database exists.
"""

# SHOPKEEPER_PROMPT=catalog adds what the shop sells, read from the shelf,
# and asks for every search up front. "base" is SYSTEM alone.
CATALOG = """
What the shop sells, by category. These are the item names on the shelf:
{catalog}

Before you answer a customer line, work out every item they could mean and call guide once for each of them, all in the same step. Search with the item name from the list, like "Long Grain Rice", never with sizes, quantities or other words. If they ask for something general, like "oil" or "something for breakfast", search for each item that fits. Then pick the size, price and quantity from the rows that come back.
If what they ask for is not in the list, the shop does not sell it.
"""


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


def prompt_name():
    name = os.environ.get("SHOPKEEPER_PROMPT", "base")
    if name not in ("base", "catalog"):
        raise SystemExit(f"SHOPKEEPER_PROMPT must be base or catalog, not {name}")
    return name


def catalog_of(db):
    """One line per category with its item names, straight from the shelf."""
    rows = json.loads(subprocess.run(
        ["duckdb", "-readonly", "-json", str(db), "-c",
         "SELECT category, string_agg(DISTINCT item, ', ' ORDER BY item) AS items "
         "FROM products GROUP BY category ORDER BY category"],
        capture_output=True, text=True, check=True,
    ).stdout)
    return "\n".join(f"- {r['category']}: {r['items']}" for r in rows)


def allowlisted_model():
    chosen = os.environ.get("OPENROUTER_MODEL", "gpt-6-luna")
    allowed = {
        line.strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    if chosen not in allowed:
        raise SystemExit(f"model {chosen} is not in {ALLOWLIST.name}")
    return chosen


@dataclass
class Turn:
    """Run context. The tools and hooks read it; the model never sees it."""
    request_id: str
    model: str
    watch: Callable | None = None
    step: int = 0
    rows: int = 0
    llm_row: int = 0
    began: float = 0.0

    def tell(self, event):
        if self.watch:
            try:
                self.watch(event)
            except Exception:
                # A page that cannot keep up must not lose the sale.
                pass

    def row(self):
        self.rows += 1
        return self.rows


class Steps(RunHooks):
    """Model-call events for the page and the scores. Phoenix gets its
    model spans from the instrumentor, not from here."""

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        turn = context.context
        turn.step += 1
        turn.llm_row = turn.row()
        turn.began = time.monotonic()
        turn.tell({"kind": "llm.start", "step": turn.step,
                   "row": turn.llm_row, "model": turn.model})

    async def on_llm_end(self, context, agent, response):
        turn = context.context
        usage = response.usage
        turn.tell({
            "kind": "llm",
            "step": turn.step,
            "row": turn.llm_row,
            "model": turn.model,
            "seconds": round(time.monotonic() - turn.began, 3),
            "prompt_tokens": usage.input_tokens,
            "cached_tokens": usage.input_tokens_details.cached_tokens or 0,
            "completion_tokens": usage.output_tokens,
            "wants": [item.name for item in response.output
                      if getattr(item, "type", "") == "function_call"],
        })


class Shelf:
    """The DuckDB MCP process, the OpenAI client, and the event loop the
    SDK runs on.

    Callers are plain threads (the floor's workers, the eval runner). They
    hand each turn to this one loop and wait for it, so one MCP session
    serves everyone and the floor does not need to be async.
    """

    def __init__(self, db=DB):
        if not Path(db).exists():
            raise SystemExit(f"missing {db}. Run `make {'bigdb' if SHOP == 'big' else 'db'}` from the shopkeeper directory.")
        # Read before the MCP process opens the file for writing.
        self.prompt = prompt_name()
        self.instructions = SYSTEM
        if self.prompt == "catalog":
            self.instructions += CATALOG.format(catalog=catalog_of(db))
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is unset. Copy .env.example to .env.")
        self.client = openai.AsyncOpenAI(
            api_key=key,
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://api.openai.com/v1"),
            max_retries=4,
            timeout=60,
        )
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server = MCPServerStdio(
            params={"command": "duckdb",
                    "args": ["-unsigned", "-init", str(PUBLISH), str(db)]},
            name="shelf",
            client_session_timeout_seconds=30,
        )
        self.run(self.server.connect())
        listed = self.run(self.server.list_tools())
        names = {tool.name for tool in listed}
        if names != {"guide", "buy"}:
            raise SystemExit(f"refusing store that publishes {sorted(names)}")
        # DuckDB answers one call at a time. Waiting for it is contention,
        # not query time, so the two are reported apart.
        self.pipe = asyncio.Lock()
        self.tools = [self.wrap(tool) for tool in listed]

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def wrap(self, tool):
        schema = json.loads(json.dumps(tool.input_schema))
        if tool.name == "buy":
            # The model never writes the idempotency key. One per turn.
            schema["properties"].pop("request_id", None)
            schema["required"] = [k for k in schema.get("required", []) if k != "request_id"]

        async def invoke(ctx, raw):
            turn = ctx.context
            try:
                arguments = json.loads(raw or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if tool.name == "buy":
                arguments["request_id"] = turn.request_id
                arguments["qty"] = int(arguments.get("qty") or 1)
            row = turn.row()
            turn.tell({"kind": "tool.start", "step": turn.step, "row": row,
                       "name": tool.name, "args": arguments})
            queued = time.monotonic()
            async with self.pipe:
                began = time.monotonic()
                with tool_span(tool.name, arguments) as span:
                    result = await self.server.call_tool(tool.name, arguments)
                    text = "\n".join(getattr(c, "text", "") for c in result.content)
                    span.set_attribute("output.value", text)
                    span.set_status(Status(StatusCode.OK))
            done = time.monotonic()
            turn.tell({"kind": "tool", "step": turn.step, "row": row,
                       "name": tool.name, "args": arguments, "result": text,
                       "seconds": round(done - began, 4),
                       "waited": round(began - queued, 4)})
            return text

        return FunctionTool(
            name=tool.name,
            description=tool.description or "",
            params_json_schema=schema,
            on_invoke_tool=invoke,
            strict_json_schema=False,
        )

    def close(self):
        try:
            self.run(self.client.close())
            self.run(self.server.cleanup())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


def turn(shelf, model, history, utterance, request_id,
         watch=None, session=None, parent=None):
    """One utterance in, one reply out. Blocks the calling thread.

    `history` is the SDK's input list and is replaced in place, so the
    next turn sees this turn's tool calls and results, not just its text.
    `watch` gets one dict per model call and tool call as they finish.
    `parent` is the trace context of the conversation this turn belongs to.
    """
    return shelf.run(_turn(shelf, model, history, utterance, request_id,
                           watch, session, parent))


async def _turn(shelf, model, history, utterance, request_id,
                watch, session, parent):
    effort = os.environ.get("OPENROUTER_REASONING_EFFORT")
    agent = Agent(
        name="counter",
        instructions=shelf.instructions,
        model=OpenAIResponsesModel(model, shelf.client),
        model_settings=ModelSettings(
            max_tokens=400,
            reasoning=Reasoning(effort=effort) if effort else None,
        ),
        tools=shelf.tools,
    )
    items = [*history, {"role": "user", "content": utterance}]
    context = Turn(request_id=request_id, model=model, watch=watch)
    try:
        with turn_span(request_id, utterance, model, session, parent) as span:
            try:
                result = await Runner.run(agent, items, context=context,
                                          max_turns=MAX_STEPS, hooks=Steps())
            except MaxTurnsExceeded:
                text, outcome = STUCK, "stuck"
                history[:] = [*items, {"role": "assistant", "content": text}]
            except openai.APIError as exc:
                code = getattr(exc, "status_code", None) or "timeout"
                raise ModelError(f"model {code}: {str(exc)[:500]}") from exc
            except ModelBehaviorError as exc:
                # e.g. the reply ran past max_tokens and came back incomplete.
                raise ModelError(f"model reply unusable: {str(exc)[:500]}") from exc
            else:
                text = str(result.final_output or "")
                outcome = "replied" if text.strip() else "empty"
                history[:] = result.to_input_list()
            # replied, empty (model said nothing) or stuck (ran out of steps).
            # The last two are failures, so Phoenix counts them as errors.
            span.set_attribute("shopkeeper.outcome", outcome)
            span.set_status(Status(StatusCode.OK) if outcome == "replied"
                            else Status(StatusCode.ERROR, outcome))
            span.set_attribute("output.value", text[:4000])
            return text
    finally:
        flush()


def main():
    load_dotenv()
    model = allowlisted_model()
    start_trace()
    shelf = Shelf()
    history = []
    print(f"counter up on {model}. empty line to leave.", file=sys.stderr)
    print(f"traces: http://localhost:6006 project {PROJECT}", file=sys.stderr)
    try:
        while True:
            try:
                utterance = input("> ").strip()
            except EOFError:
                break
            if not utterance:
                break
            print(turn(shelf, model, history, utterance, uuid.uuid4().hex))
    finally:
        shelf.close()


if __name__ == "__main__":
    main()
