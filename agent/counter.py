#!/usr/bin/env python3
"""Thin kirana counter.

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
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from trace import flush, llm_span, start as start_trace, tool_span, turn_span

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "models.allowlist"
DB = ROOT / "store" / "kirana.duckdb"
PUBLISH = ROOT / "store" / "publish.sql"

SYSTEM = """You are the counter at a small Indian kirana.
Speak the way the customer speaks. Short. Prices come only from tool results, in INR.

You have two tools and no others:
- guide: look up what is actually on the shelf. Call this before you name a price, a pack, or a substitute. Also call it when the request is vague ("something for tea", "atta kaunsa better").
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


def complete(model, messages, tools):
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is unset. Copy .env.example to .env.")
    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "temperature": 0.2,
        "max_tokens": 400,
    }
    request = urllib.request.Request(
        os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", "http://localhost"),
            "X-Title": os.environ.get("OPENROUTER_APP_TITLE", "shopkeeper"),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"openrouter {exc.code}: {detail[:500]}") from exc
    # openrouter/free rewrites this to the model that actually answered.
    served = payload.get("model") or model
    return payload["choices"][0]["message"], payload.get("usage") or {}, served


def turn(store, tracer, model, tools, history, utterance, request_id,
         served=None, watch=None):
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
    span = turn_span(tracer, request_id, utterance)
    span.set_attribute("shopkeeper.requested_model", model)
    if served is None:
        served = []
    try:
        return _turn(
            store, tracer, span, model, tools, history, messages, request_id,
            served, watch,
        )
    finally:
        if served:
            span.set_attribute("shopkeeper.served_models", ",".join(served))
        span.end()
        flush()


def _turn(store, tracer, span, model, tools, history, messages, request_id,
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
        message, usage, answered_by = complete(model, messages, tools)
        seconds = time.monotonic() - began
        served.append(answered_by)
        llm_span(tracer, span, answered_by, messages, message, usage)
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
            span.set_attribute("output.value", text[:4000])
            return text
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
            result = store.call(name, arguments)
            seconds = time.monotonic() - began
            watched = getattr(store.watch, "calls", None)
            if watched is not None:
                watched.append({"name": name, "result": result})
            tool_span(tracer, span, name, arguments, result)
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
    text = "Counter is stuck in tools. Say it again, shorter."
    span.set_attribute("output.value", text)
    return text


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
