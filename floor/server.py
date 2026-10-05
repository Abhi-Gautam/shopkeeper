#!/usr/bin/env python3
"""The shop the page draws.

Serves ui/ and streams one event per thing that actually happened: a
customer joining the line, a worker taking them, every model call, every
guide and buy on the DuckDB process, the reply, and the walk out.

Nothing on the page is invented. If a number is on screen, an event
carried it. Token counts come from the OpenRouter usage block, step
timings from around the calls themselves, and the tool fields are parsed
from what DuckDB actually returned.

Sliders set how many workers pull from the line, and how often a new
customer walks in. Model calls overlap. Tool calls take turns on the one
DuckDB process, and that wait is reported separately from the query.

Phoenix is the second opinion on tokens and p95, not the only one. If it
is down the page falls back to this process's own completed turns.

Does not start if another process is already answering on this port.
Does not own a second duckdb file. Stop the prompt batch before this.
"""

import json
import queue
import random
import re
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import counter  # noqa: E402

UI = ROOT / "ui"
LINES = Path(__file__).with_name("conversations.txt")
HOST = "127.0.0.1"
PORT = 8787
PHOENIX = "http://127.0.0.1:6006"
MAX_WAIT = 8
WORKERS = 3

STAFF = 2
ARRIVAL = 4  # customers per minute

KEEP = 1400  # events held for a page that reloads


def load_customers():
    text = LINES.read_text()
    customers = []
    current = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "---":
            if current:
                customers.append(current)
                current = []
            continue
        current.append(line)
    if current:
        customers.append(current)
    return customers


# --- reading what DuckDB said -------------------------------------------

def markdown_rows(text):
    """Rows out of the markdown table the guide tool returns."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []

    def cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header = cells(lines[0])
    body = lines[1:]
    if body and set(body[0].replace("|", "").replace(" ", "")) <= {"-", ":"}:
        body = body[1:]
    rows = []
    for line in body:
        values = cells(line)
        if len(values) != len(header):
            continue
        rows.append(dict(zip(header, values)))
    return rows


def truthy(value):
    return str(value).strip().lower() in ("true", "t", "1", "yes")


def guide_fields(result):
    """What the page shows for a guide call: how much the shelf offered."""
    rows = markdown_rows(result)
    if not rows:
        # Some builds answer json instead of markdown. Same shape either way.
        try:
            parsed = json.loads(result)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except (json.JSONDecodeError, TypeError):
            rows = []
    in_stock = 0
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if truthy(row.get("in_stock")) or _int(row.get("stock")) > 0:
            in_stock += 1
        label = str(row.get("name") or "").strip()
        pack = str(row.get("pack_label") or "").strip()
        price = str(row.get("price_usd") or "").strip()
        if label:
            names.append({"name": label, "pack": pack, "price": price,
                          "stock": _int(row.get("stock")),
                          "sku": str(row.get("sku") or "").strip()})
    return {"rows": len(rows), "in_stock": in_stock, "offered": names[:6]}


def _int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def buy_fields(result):
    """status / sku / qty / price / stock_left out of the buy result."""
    row = None
    try:
        parsed = json.loads(result)
        if isinstance(parsed, list) and parsed:
            row = parsed[0]
        elif isinstance(parsed, dict):
            row = parsed
    except (json.JSONDecodeError, TypeError):
        row = None
    if row is None:
        rows = markdown_rows(result)
        row = rows[0] if rows else None
    if isinstance(row, dict):
        return {
            "status": str(row.get("status") or "").strip() or "unknown",
            "sku": str(row.get("sku") or "").strip(),
            "qty": _int(row.get("qty_sold")),
            "price": str(row.get("price_usd") or "").strip(),
            "stock_left": _int(row.get("stock_left")),
        }
    text = (result or "").lower()
    for word in ("sold", "out_of_stock", "unknown_sku", "bad_qty"):
        if word in text:
            return {"status": word, "sku": "", "qty": 0, "price": "",
                    "stock_left": 0}
    return {"status": "unknown", "sku": "", "qty": 0, "price": "",
            "stock_left": 0}


class Shop:
    def __init__(self, customers):
        self.customers = customers
        self.line = queue.Queue(maxsize=MAX_WAIT)
        self.events = []
        self.cond = threading.Condition()
        self.staff = STAFF
        self.arrival = ARRIVAL
        self.dropped = 0
        self.stop = threading.Event()
        self.shelf = None
        self.model = None
        self.busy = set()
        self.lock = threading.Lock()
        self.turns = []  # completed turns, for the local metric fallback
        self.tally = {"turns": 0, "sold": 0, "out": 0, "guide": 0, "error": 0,
                      "guides": 0, "buys": 0, "tokens": 0, "dropped": 0}

    def emit(self, event):
        event["t"] = time.time()
        with self.cond:
            self.events.append(event)
            if len(self.events) > KEEP:
                del self.events[: KEEP // 2]
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            return list(self.events)

    def wait_after(self, n, timeout):
        with self.cond:
            if len(self.events) > n:
                return list(self.events)
            self.cond.wait(timeout=timeout)
            return list(self.events)

    def set_control(self, staff, arrival):
        if staff is not None:
            self.staff = max(1, min(WORKERS, int(staff)))
        if arrival is not None:
            self.arrival = max(1, min(12, int(arrival)))
        self.emit({"type": "config", "staff": self.staff,
                   "arrival": self.arrival})

    def mark(self, worker, busy):
        with self.lock:
            if busy:
                self.busy.add(worker)
            else:
                self.busy.discard(worker)
        self.emit_queue()

    def emit_queue(self):
        with self.lock:
            busy = sorted(self.busy)
        self.emit({
            "type": "queue",
            "depth": self.line.qsize(),
            "cap": MAX_WAIT,
            "busy": busy,
            "dropped": self.dropped,
        })

    def record(self, event):
        """Keep the tally and the window the local metrics are computed from."""
        status = event.get("status")
        with self.lock:
            self.tally["turns"] += 1
            if status in ("sold", "out", "guide", "error"):
                self.tally[status] += 1
            self.tally["tokens"] += int(event.get("tokens") or 0)
            for step in event.get("steps") or []:
                if step.get("kind") != "tool":
                    continue
                if step.get("name") == "guide":
                    self.tally["guides"] += 1
                elif step.get("name") == "buy":
                    self.tally["buys"] += 1
            self.turns.append({
                "t": time.time(),
                "tokens": int(event.get("tokens") or 0),
                "seconds": float(event.get("seconds") or 0),
            })
            cutoff = time.time() - 120
            self.turns = [x for x in self.turns if x["t"] >= cutoff]
            tally = dict(self.tally)
        self.emit({"type": "stats", **tally})

    def local_metrics(self):
        now = time.time()
        with self.lock:
            window = [x for x in self.turns if now - x["t"] <= 60]
        tokens = sum(x["tokens"] for x in window)
        lat = sorted(x["seconds"] for x in window)
        p95 = 0.0
        if lat:
            p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        return {"tokens_per_min": tokens, "p95": round(p95, 2),
                "source": "floor"}

    def open_shelf(self):
        counter.load_dotenv()
        self.model = counter.allowlisted_model()
        counter.start_trace()
        self.shelf = counter.Shelf()

    def close_shelf(self):
        if self.shelf:
            self.shelf.close()
        counter.flush()


def spoken(reply):
    """The bubble is what the counter said, not its notes to itself.

    Some free models narrate the prompt ("The user asks...") or leave a
    think tag in the text. Neither belongs over the counter.
    """
    text = (reply or "").replace("\n", " ").strip()
    if "</think>" in text:
        text = text.split("</think>")[-1].strip()
    text = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S).strip()
    lower = text.lower()
    if lower.startswith("the user asks") or lower.startswith("the assistant should"):
        return "One moment."
    return text[:240]


def status_of(steps, reply):
    """The grade is the shelf result, not the sentence."""
    saw_buy = False
    for step in steps:
        if step.get("kind") != "tool" or step.get("name") != "buy":
            continue
        saw_buy = True
        status = (step.get("buy") or {}).get("status")
        if status == "sold":
            return "sold"
        if status in ("out_of_stock", "unknown_sku", "bad_qty"):
            return "out"
    if saw_buy:
        return "guide"
    if "stuck in tools" in (reply or ""):
        return "error"
    return "guide"


def serve_one(shop, worker, cid, tid, ask, history):
    """One utterance. Streams its own steps, returns the reply event."""
    served = []
    steps = []
    started = time.monotonic()
    reply = ""

    def watch(event):
        kind = event.get("kind")
        out = {"type": "step", "cid": cid, "tid": tid, "worker": worker, **event}
        if kind == "tool" and event.get("name") == "guide":
            out["guide"] = guide_fields(event.get("result"))
        elif kind == "tool" and event.get("name") == "buy":
            out["buy"] = buy_fields(event.get("result"))
        out.pop("result", None)  # the raw markdown is for Phoenix, not the page
        if kind in ("llm", "tool"):
            steps.append(dict(out))
        shop.emit(out)

    # 429s and timeouts are retried by the SDK's client, so the customer
    # keeps their history and Phoenix keeps one turn per utterance. What
    # reaches here has already failed for good.
    try:
        reply = counter.turn(
            shop.shelf, shop.model, history, ask, tid, served, watch,
            session=cid,
        )
    except counter.ModelError:
        reply = "One moment."

    seconds = round(time.monotonic() - started, 2)
    tokens = sum(
        int(s.get("prompt_tokens") or 0) + int(s.get("completion_tokens") or 0)
        for s in steps if s.get("kind") == "llm"
    )
    status = "wait" if reply == "One moment." else status_of(steps, reply)
    return {
        "type": "reply",
        "cid": cid,
        "tid": tid,
        "worker": worker,
        "text": spoken(reply),
        "status": status,
        "tokens": tokens,
        "seconds": seconds,
        "served": served,
        "steps": steps,
    }


def worker_loop(shop, index):
    while not shop.stop.is_set():
        if index >= shop.staff:
            time.sleep(0.3)
            continue
        try:
            job = shop.line.get(timeout=0.5)
        except queue.Empty:
            continue
        cid = job["id"]
        lines = job["lines"]
        waited = round(time.time() - job["queued"], 2)
        shop.mark(index, True)
        history = []
        try:
            for turn_index, ask in enumerate(lines):
                if shop.stop.is_set():
                    break
                tid = uuid.uuid4().hex
                if turn_index:
                    # A follow-up. Already at the counter, so no queue wait.
                    shop.emit({"type": "arrive", "cid": cid, "tid": tid,
                               "text": ask, "turn": turn_index + 1,
                               "turns": len(lines), "queued": False})
                shop.emit({"type": "assign", "cid": cid, "tid": tid,
                           "worker": index, "turn": turn_index + 1,
                           "turns": len(lines),
                           "waited": waited if turn_index == 0 else 0.0})
                try:
                    event = serve_one(shop, index, cid, tid, ask, history)
                except BaseException as exc:
                    print(f"worker {index} stopped the turn: {exc}",
                          file=sys.stderr)
                    event = {
                        "type": "reply", "cid": cid, "tid": tid,
                        "worker": index, "text": "One moment.",
                        "status": "error", "tokens": 0, "seconds": 0,
                        "served": [], "steps": [],
                    }
                    shop.emit(event)
                    shop.record(event)
                    break
                shop.emit(event)
                shop.record(event)
                time.sleep(1.4)
        finally:
            shop.emit({"type": "leave", "cid": cid})
            shop.mark(index, False)
        time.sleep(0.4)


def arrival_loop(shop):
    """One customer is the whole conversation, not one line.

    A follow-up ("the 5 kg, if you have it") has to reach the same worker
    with the memory of the line before it. Splitting those across the pool
    is how the first 377 prompts came back as strangers.

    The arrive event is emitted here, when they walk in, not when a worker
    frees up. That is the whole point of drawing a queue.
    """
    order = list(range(len(shop.customers)))
    random.shuffle(order)
    cursor = 0
    while not shop.stop.is_set():
        per_minute = max(1, shop.arrival)
        time.sleep(random.expovariate(per_minute / 60))
        if shop.stop.is_set():
            return
        lines = shop.customers[order[cursor % len(order)]]
        cursor += 1
        cid = uuid.uuid4().hex[:8]
        shop.emit({"type": "arrive", "cid": cid, "tid": None,
                   "text": lines[0], "turn": 1, "turns": len(lines),
                   "queued": True})
        try:
            shop.line.put({"id": cid, "lines": lines, "queued": time.time()},
                          timeout=2)
            shop.emit_queue()
        except queue.Full:
            shop.dropped += 1
            with shop.lock:
                shop.tally["dropped"] = shop.dropped
            shop.emit({"type": "drop", "cid": cid, "depth": shop.line.qsize(),
                       "cap": MAX_WAIT})
            shop.emit({"type": "leave", "cid": cid})
            shop.emit_queue()


def phoenix_metrics():
    """Tokens and p95 from Phoenix spans in the last minute.

    Returns None when Phoenix is down. The page then uses the floor's own
    completed turns, which carry the same usage numbers.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=60)
    query = (
        f"?start_time={start.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
        f"&end_time={end.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
        "&limit=1000"
    )
    url = PHOENIX + "/v1/projects/default/spans" + query
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            page = json.load(resp)
    except Exception:
        return None
    spans = page.get("data") or []
    tokens = 0
    lat = []
    for span in spans:
        if span.get("name") != "utterance":
            continue
        try:
            a = datetime.fromisoformat(span["start_time"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(span["end_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        lat.append((b - a).total_seconds())
    for span in spans:
        if span.get("span_kind") != "LLM":
            continue
        attrs = span.get("attributes") or {}
        tokens += int(attrs.get("llm.token_count.prompt") or 0)
        tokens += int(attrs.get("llm.token_count.completion") or 0)
    if not lat:
        return {"tokens_per_min": tokens, "p95": 0, "source": "phoenix"}
    lat.sort()
    return {
        "tokens_per_min": tokens,
        "p95": round(lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))], 2),
        "source": "phoenix",
    }


def metrics_loop(shop):
    while not shop.stop.is_set():
        numbers = phoenix_metrics()
        shop.emit({"type": "metrics", **(numbers or shop.local_metrics())})
        shop.emit_queue()
        shop.stop.wait(5)


class Handler(BaseHTTPRequestHandler):
    shop = None

    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._events()
            return
        if path in ("/", "/index.html"):
            self._send(200, (UI / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
            return
        if path == "/shop.js":
            self._send(200, (UI / "shop.js").read_bytes(),
                       "text/javascript; charset=utf-8")
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/control":
            self._send(404, "not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, "bad json", "text/plain")
            return
        self.shop.set_control(body.get("staff"), body.get("arrival"))
        self._send(200, '{"ok":true}', "application/json")

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        with self.shop.lock:
            tally = dict(self.shop.tally)
        hello = {
            "type": "hello",
            "staff": self.shop.staff,
            "arrival": self.shop.arrival,
            "model": self.shop.model,
            "workers": WORKERS,
            "cap": MAX_WAIT,
            "conversations": len(self.shop.customers),
            "tally": tally,
        }
        try:
            self._write(hello)
            seen = 0
            # Replay the board so a reload is not an empty shop. Every event
            # the page understands is idempotent, so replay is just catch-up.
            for event in self.shop.snapshot():
                if event["type"] != "metrics":
                    self._write(event)
                seen += 1
            while not self.shop.stop.is_set():
                events = self.shop.wait_after(seen, timeout=15)
                if len(events) == seen:
                    self.wfile.write(b": keep\n\n")
                    self.wfile.flush()
                    continue
                for event in events[seen:]:
                    self._write(event)
                seen = len(events)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write(self, event):
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


def main():
    customers = load_customers()
    if not customers:
        raise SystemExit(f"no conversations in {LINES.name}")
    shop = Shop(customers)
    shop.open_shelf()
    Handler.shop = shop
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        shop.close_shelf()
        raise SystemExit(f"cannot listen on {HOST}:{PORT}: {exc}") from exc
    print(f"floor on http://{HOST}:{PORT}  model {shop.model}  "
          f"customers {len(customers)}", file=sys.stderr)
    print("charts read Phoenix at http://127.0.0.1:6006", file=sys.stderr)
    threads = [
        threading.Thread(target=worker_loop, args=(shop, i), daemon=True)
        for i in range(WORKERS)
    ]
    threads.append(threading.Thread(target=arrival_loop, args=(shop,), daemon=True))
    threads.append(threading.Thread(target=metrics_loop, args=(shop,), daemon=True))
    for thread in threads:
        thread.start()
    shop.emit({"type": "config", "staff": shop.staff, "arrival": shop.arrival})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shop.stop.set()
        httpd.shutdown()
        shop.close_shelf()


if __name__ == "__main__":
    main()
