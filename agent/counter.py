#!/usr/bin/env python3
"""Thin grocery counter.

The model only sees two tools, both owned by the DuckDB MCP process:
  guide  read stock, prices, substitutes
  buy    sell a SKU the guide already returned

This file does not decide what to sell. It forwards tool calls and
stops the model from inventing a third tool or a raw SQL string.
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import openai

from trace import flush, start as start_trace, tool_span, turn_span

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "models.allowlist"
DB = ROOT / "store" / "shop.duckdb"
PUBLISH = ROOT / "store" / "publish.sql"

SYSTEM = """You are the counter at a small neighborhood grocery store.
Reply in plain English. Short. Prices come only from tool results, in USD.

You have two tools and no others:
- guide: look up what is actually on the shelf. Call this before you name a price, a pack, or a substitute. Also call it when the request is vague ("something for breakfast", "which flour is better").
- buy: sell only when the customer has asked to buy AND you have a SKU from guide. Never invent a SKU. Pass that SKU, the pack count, and the request_id you were given.

If guide says in_stock is false, do not call buy. Offer another row from the same guide result, or say you will note it.
If buy status is sold, confirm pack, price, and stock left. If out_of_stock or unknown_sku, say so and guide again.
Do not mention tools, SKU format rules, or that a database exists.
"""


def allowlisted_model():
    # Seed default is the free router. Pin a :free id in .env for a fixed model.
    chosen = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
    allowed = {
        line.strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    if chosen not in allowed:
        raise SystemExit(f"model {chosen} is not in {ALLOWLIST.name}")
    return chosen


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


class Store:
    def __init__(self):
        if not DB.exists():
            raise SystemExit(f"missing {DB}. Run `make db` from the shopkeeper directory.")
        self.proc = subprocess.Popen(
            ["duckdb", "-unsigned", "-init", str(PUBLISH), str(DB)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        # One process owns the shelf. Workers may call the model at the
        # same time, but their tool calls take turns on this pipe.
        self.pipe = threading.Lock()
        self.watch = threading.local()
        self.next_id = 1
        self.rpc({
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "shopkeeper", "version": "0"},
            },
        })
        self.notify("notifications/initialized")

    def rpc(self, message):
        # Workers call the model at once but take turns on this pipe. The
        # time spent waiting for the lock is contention, not DuckDB work,
        # so the two are reported apart.
        queued = time.monotonic()
        with self.pipe:
            self.watch.waited = time.monotonic() - queued
            message = {"jsonrpc": "2.0", "id": self.next_id, **message}
            expect = self.next_id
            self.next_id += 1
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise SystemExit("duckdb MCP server exited")
                text = line.decode(errors="replace").strip()
                if not text.startswith("{"):
                    continue
                parsed = json.loads(text)
                if parsed.get("id") == expect:
                    if "error" in parsed:
                        raise SystemExit(parsed["error"])
                    return parsed["result"]

    def notify(self, method):
        with self.pipe:
            self.proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode()
            )
            self.proc.stdin.flush()

    def tools(self):
        listed = self.rpc({"method": "tools/list", "params": {}})
        names = {tool["name"] for tool in listed["tools"]}
        if names != {"guide", "buy"}:
            raise SystemExit(f"refusing store that publishes {sorted(names)}")
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["inputSchema"],
                },
            }
            for tool in listed["tools"]
        ]

    def call(self, name, arguments):
        if name not in {"guide", "buy"}:
            return f"refused unknown tool {name}"
        result = self.rpc({
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        chunks = result.get("content") or []
        return "\n".join(chunk.get("text", "") for chunk in chunks)

    def close(self):
        self.proc.terminate()


# Worth another try at the same call. Anything else is a bug or a bad key.
RETRY_ON = {408, 429, 500, 502, 503, 504}
ATTEMPTS = 4


class OpenRouterError(RuntimeError):
    """A model call that failed for good. The text starts "openrouter <code>"."""


_client = None


def client():
    global _client
    if _client is None:
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is unset. Copy .env.example to .env.")
        _client = openai.OpenAI(
            api_key=key,
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            default_headers={
                "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "http://localhost"),
                "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "shopkeeper"),
            },
            timeout=60,
            # Retries live in complete(), so each attempt is its own span.
            max_retries=0,
        )
    return _client


def routing():
    """OpenRouter-only body fields. A pinned provider keeps a score about one
    deployment of one model, not whichever host answered first."""
    extra = {}
    provider = os.environ.get("OPENROUTER_PROVIDER")
    if provider:
        extra["provider"] = {"order": [provider], "allow_fallbacks": False}
    effort = os.environ.get("OPENROUTER_REASONING_EFFORT")
    if effort:
        extra["reasoning"] = {"effort": effort}
    return extra


def complete(model, messages, tools, on_retry=None):
    """One model call, retried in place on 429, 5xx and timeouts.

    The retry stays inside the turn: same messages, same request_id, and
    every failed attempt is an error span next to the one that worked.
    """
    wait = 4
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client().chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                temperature=0.2,
                max_tokens=400,
                extra_body=routing(),
            )
            break
        except (openai.APIStatusError, openai.APITimeoutError,
                openai.APIConnectionError) as exc:
            code = getattr(exc, "status_code", None)
            if (code is not None and code not in RETRY_ON) or attempt == ATTEMPTS:
                raise OpenRouterError(
                    f"openrouter {code or 'timeout'}: {str(exc)[:500]}"
                ) from exc
            if on_retry:
                on_retry(attempt, f"{code or 'timeout'}: {str(exc)[:120]}")
            time.sleep(wait)
            wait = min(wait * 2, 30)
    if not response.choices:
        raise OpenRouterError("openrouter 200: no choices in the response")
    message = response.choices[0].message.model_dump(exclude_none=True)
    usage = response.usage.model_dump() if response.usage else {}
    # openrouter/free rewrites this to the model that actually answered.
    served = response.model or model
    return message, usage, served


def turn(store, tracer, model, tools, history, utterance, request_id,
         served=None, watch=None, session=None):
    """One utterance in, one reply out.

    `watch`, if given, is called with a dict per model call and per tool
    call as they finish. The floor page is drawn from those; nothing here
    decides what they mean.
    """
    history.append({"role": "user", "content": utterance})
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "system", "content": f"buy request_id for this turn: {request_id}"},
        *history,
    ]
    if served is None:
        served = []
    try:
        with turn_span(tracer, request_id, utterance, session) as span:
            span.set_attribute("shopkeeper.requested_model", model)
            try:
                text, outcome = _turn(
                    store, tracer, model, tools, history, messages,
                    request_id, served, watch,
                )
            finally:
                if served:
                    span.set_attribute("shopkeeper.served_models", ",".join(served))
            # replied, empty (model said nothing) or stuck (ran out of steps).
            span.set_attribute("shopkeeper.outcome", outcome)
            span.set_attribute("output.value", text[:4000])
            return text
    finally:
        flush()


def _turn(store, tracer, model, tools, history, messages, request_id,
          served, watch=None):
    def tell(event):
        if watch:
            try:
                watch(event)
            except Exception:
                # A page that cannot keep up must not lose the sale.
                pass

    # One id per row the page draws. The start and the finish of the same
    # call carry the same id, so a page that reconnects and replays the
    # stream settles rows in place instead of stacking copies of them.
    seen = {"row": 0}

    def row():
        seen["row"] += 1
        return seen["row"]

    step = 0
    for _ in range(4):
        step += 1
        llm_row = row()
        tell({"kind": "llm.start", "step": step, "row": llm_row, "model": model})
        began = time.monotonic()

        def retried(attempt, why, step=step):
            tell({"kind": "retry", "step": step,
                  "row": f"retry{step}.{attempt}",
                  "attempt": attempt, "why": why})

        message, usage, answered_by = complete(model, messages, tools, retried)
        seconds = time.monotonic() - began
        served.append(answered_by)
        tool_calls = message.get("tool_calls") or []
        tell({
            "kind": "llm",
            "step": step,
            "row": llm_row,
            "model": answered_by,
            "seconds": round(seconds, 3),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "wants": [c["function"]["name"] for c in tool_calls],
        })
        if not tool_calls:
            text = message.get("content") or ""
            history.append({"role": "assistant", "content": text})
            return text, "replied" if text.strip() else "empty"
        messages.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })
        for call in tool_calls:
            name = call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
            if name == "buy":
                arguments["request_id"] = request_id
                arguments["qty"] = int(arguments.get("qty") or 1)
            tool_row = row()
            tell({"kind": "tool.start", "step": step, "row": tool_row,
                  "name": name, "args": arguments})
            store.watch.waited = 0.0
            began = time.monotonic()
            with tool_span(tracer, name, arguments) as span:
                result = store.call(name, arguments)
                span.set_attribute("output.value", (result or "")[:4000])
            seconds = time.monotonic() - began
            watched = getattr(store.watch, "calls", None)
            if watched is not None:
                watched.append({"name": name, "result": result})
            tell({
                "kind": "tool",
                "step": step,
                "row": tool_row,
                "name": name,
                "args": arguments,
                "result": result,
                "seconds": round(seconds, 4),
                "waited": round(getattr(store.watch, "waited", 0.0) or 0.0, 4),
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", name),
                "content": result,
            })
    return "Counter is stuck in tools. Say it again, shorter.", "stuck"


def main():
    load_dotenv()
    model = allowlisted_model()
    tracer = start_trace()
    store = Store()
    tools = store.tools()
    history = []
    print(f"counter up on {model}. empty line to leave.", file=sys.stderr)
    print("traces: http://localhost:6006", file=sys.stderr)
    try:
        while True:
            try:
                utterance = input("> ").strip()
            except EOFError:
                break
            if not utterance:
                break
            request_id = uuid.uuid4().hex
            print(turn(store, tracer, model, tools, history, utterance, request_id))
    finally:
        store.close()
        flush()


if __name__ == "__main__":
    main()
