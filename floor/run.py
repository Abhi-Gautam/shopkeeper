#!/usr/bin/env python3
"""Play conversations through the counter, optionally draw them.

    .venv/bin/python floor/run.py --limit 10         # the first ten
    .venv/bin/python floor/run.py --limit 10 --ui    # same, watched at :8787

Each conversation in conversations.txt is one customer. They walk in at
--arrival per minute and --staff workers serve them, so a run is also a
load test: latency is measured under that load.

A run plays on a fresh copy of the shelf, so no run sees another's sales.
Every conversation is one trace in PHOENIX_PROJECT; the traces are what
gets read and judged.

With --ui the page follows the same events, live. Nothing on it is
invented: token counts come from the usage block, step timings from
around the calls, and tool fields from what DuckDB answered.
"""

import argparse
import json
import queue
import shutil
import sys
import tempfile
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
import trace as tracing  # noqa: E402
from opentelemetry import trace as otel  # noqa: E402
from models import BuyStatus, Tool, buy_result_of, offers_of  # noqa: E402

UI = ROOT / "ui"
LINES = Path(__file__).with_name("conversations.txt")
HOST = "127.0.0.1"
PORT = 8787
PHOENIX = "http://127.0.0.1:6006"
MAX_WAIT = 8  # customers the line can hold; the next one waits at the door
WORKERS = 10
KEEP = 1400  # events held for a page that reloads


def load_customers(limit):
    """One dict per `---` block: the lines said, and the outcome from its
    `=` lines (see the file's header), or None when it has none."""
    customers = []
    for raw in LINES.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "---":
            customers.append({"lines": [], "expect": None})
        elif line.startswith("="):
            want = line[1:].strip()
            expect = customers[-1]["expect"] or []
            if want != "none":
                name, pack, qty = (part.strip() for part in want.split(" | "))
                expect.append([name, None if pack == "-" else pack, int(qty)])
            customers[-1]["expect"] = expect
        else:
            customers[-1]["lines"].append(line)
    customers = [c for c in customers if c["lines"]]
    return customers[:limit] if limit else customers


# --- reading what DuckDB said -------------------------------------------

def guide_fields(result):
    """What the page shows for a guide call: how much the shelf offered."""
    offers = offers_of(result)
    return {"rows": len(offers),
            "in_stock": sum(offer.in_stock for offer in offers),
            "offered": [{"name": o.name, "pack": o.pack_label, "price": str(o.price_usd),
                         "stock": o.stock, "sku": o.sku} for o in offers[:6]]}


def buy_fields(result):
    """status / sku / qty / price / stock_left out of the buy result."""
    sold = buy_result_of(result)
    return {"status": sold.status, "sku": sold.sku or "", "qty": sold.qty_sold,
            "price": str(sold.price_usd or ""), "stock_left": sold.stock_left or 0}


def status_of(steps, reply):
    """The page's grade for one turn is the shelf result, not the sentence."""
    for step in steps:
        if step["kind"] == "tool" and step["name"] == Tool.BUY:
            status = step["buy"]["status"]
            if status == BuyStatus.SOLD:
                return "sold"
            if status in (BuyStatus.OUT_OF_STOCK, BuyStatus.UNKNOWN_SKU, BuyStatus.BAD_QTY):
                return "out"
    return "error" if reply == counter.STUCK else "guide"


class Shop:
    def __init__(self, customers, staff, arrival):
        self.customers = customers
        self.staff = staff
        self.arrival = arrival
        self.line = queue.Queue(maxsize=MAX_WAIT)
        self.left = len(customers)  # conversations not yet finished
        self.finished = threading.Event()
        self.stop = threading.Event()
        self.events = []
        self.cond = threading.Condition()
        self.lock = threading.Lock()
        self.busy = set()
        self.turns = []  # completed turns, for the local metric fallback
        self.tally = {"turns": 0, "sold": 0, "out": 0, "guide": 0, "error": 0,
                      "guides": 0, "buys": 0, "tokens": 0, "dropped": 0}
        self.work = Path(tempfile.mkdtemp(prefix="shelf-"))
        shutil.copy(counter.DB, self.work / "shop.duckdb")
        self.shelf = counter.Shelf(self.work / "shop.duckdb")
        self.model = self.shelf.model

    def close(self):
        self.shelf.close()
        counter.flush()
        shutil.rmtree(self.work, ignore_errors=True)

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
            if len(self.events) <= n:
                self.cond.wait(timeout=timeout)
            return list(self.events)

    def set_control(self, staff, arrival):
        if staff is not None:
            self.staff = max(1, min(WORKERS, int(staff)))
        if arrival is not None:
            self.arrival = max(1, min(60, int(arrival)))
        self.emit({"type": "config", "staff": self.staff, "arrival": self.arrival})

    def mark(self, worker, busy):
        with self.lock:
            (self.busy.add if busy else self.busy.discard)(worker)
        self.emit_queue()

    def emit_queue(self):
        with self.lock:
            busy = sorted(self.busy)
        self.emit({"type": "queue", "depth": self.line.qsize(), "cap": MAX_WAIT,
                   "busy": busy, "dropped": 0})

    def record(self, event):
        """Keep the tally and the window the local metrics are computed from."""
        with self.lock:
            self.tally["turns"] += 1
            self.tally[event["status"]] = self.tally.get(event["status"], 0) + 1
            self.tally["tokens"] += event["tokens"]
            for step in event["steps"]:
                if step["kind"] == "tool":
                    self.tally[step["name"] + "s"] += 1
            now = time.time()
            self.turns = [x for x in self.turns if x["t"] >= now - 120]
            self.turns.append({"t": now, "tokens": event["tokens"],
                               "seconds": event["seconds"]})
            tally = dict(self.tally)
        self.emit({"type": "stats", **tally})

    def done_one(self):
        with self.lock:
            self.left -= 1
            if self.left == 0:
                self.finished.set()

    def local_metrics(self):
        now = time.time()
        with self.lock:
            window = [x for x in self.turns if now - x["t"] <= 60]
        lat = sorted(x["seconds"] for x in window)
        p95 = lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] if lat else 0.0
        return {"tokens_per_min": sum(x["tokens"] for x in window),
                "p95": round(p95, 2), "source": "floor"}


def serve_one(shop, worker, cid, tid, ask, history, parent):
    """One utterance. Streams its steps; returns the reply event."""
    raw, steps = [], []
    started = time.monotonic()

    def watch(event):
        raw.append(event)
        out = {"type": "step", "cid": cid, "tid": tid, "worker": worker, **event}
        if event["kind"] == "tool":
            parse = guide_fields if event["name"] == Tool.GUIDE else buy_fields
            out[event["name"]] = parse(out.pop("result"))
        if event["kind"] in ("llm", "tool"):
            steps.append(out)
        shop.emit(out)

    error = None
    try:
        reply = counter.turn(shop.shelf, history, ask, tid,
                             watch, session=cid, parent=parent)
    except counter.ModelError as exc:
        # The client already retried 429s and timeouts. This one is final.
        reply, error = "One moment.", str(exc)[:200]
    event = {
        "type": "reply",
        "cid": cid,
        "tid": tid,
        "worker": worker,
        "text": " ".join(reply.split())[:240],
        "status": "error" if error else status_of(steps, reply),
        "tokens": sum(s["prompt_tokens"] + s["completion_tokens"]
                      for s in steps if s["kind"] == "llm"),
        "seconds": round(time.monotonic() - started, 2),
        "served": [shop.model],
        "steps": steps,
    }
    return event, reply, error


def serve_customer(shop, worker, job):
    """A whole conversation on one worker, one memory, one trace."""
    cid, index = job["id"], job["index"]
    lines = shop.customers[index]["lines"]
    history, failed, reply = [], [], ""
    with tracing.conversation_span(cid, lines, shop.name) as span:
        parent = otel.set_span_in_context(span)
        for n, ask in enumerate(lines):
            if shop.stop.is_set():
                break
            tid = uuid.uuid4().hex
            if n:
                # A follow-up. Already at the counter, so no queue wait.
                shop.emit({"type": "arrive", "cid": cid, "tid": tid, "text": ask,
                           "turn": n + 1, "turns": len(lines), "queued": False})
            shop.emit({"type": "assign", "cid": cid, "tid": tid, "worker": worker,
                       "turn": n + 1, "turns": len(lines),
                       "waited": job["waited"] if n == 0 else 0.0})
            event, reply, error = serve_one(shop, worker, cid, tid, ask, history, parent)
            shop.emit(event)
            shop.record(event)
            if error or reply == counter.STUCK or not reply.strip():
                failed.append(error or reply or "empty")
            if error:
                break
            time.sleep(1.4)  # the customer reads the reply
        span.set_attribute("output.value", reply[:4000])
        span.set_status(otel.Status(otel.StatusCode.ERROR, "; ".join(failed)[:500])
                        if failed else otel.Status(otel.StatusCode.OK))


def worker_loop(shop, worker):
    while not shop.stop.is_set():
        if worker >= shop.staff:
            time.sleep(0.3)
            continue
        try:
            job = shop.line.get(timeout=0.5)
        except queue.Empty:
            continue
        job["waited"] = round(time.time() - job["queued"], 2)
        shop.mark(worker, True)
        try:
            serve_customer(shop, worker, job)
        except Exception as exc:
            print(f"worker {worker} lost customer {job['id']}: {exc}", file=sys.stderr)
        finally:
            shop.emit({"type": "leave", "cid": job["id"]})
            shop.mark(worker, False)
            shop.done_one()


def arrival_loop(shop):
    """Every conversation walks in once, in file order, spaced by the
    arrival rate. A full line makes the next customer wait at the door.

    The arrive event is emitted when they walk in, not when a worker frees
    up. That is the whole point of drawing a queue.
    """
    for index, customer in enumerate(shop.customers):
        if index:
            shop.stop.wait(60 / shop.arrival)
        if shop.stop.is_set():
            return
        cid = uuid.uuid4().hex[:8]
        lines = customer["lines"]
        shop.emit({"type": "arrive", "cid": cid, "tid": None, "text": lines[0],
                   "turn": 1, "turns": len(lines), "queued": True})
        shop.line.put({"id": cid, "index": index, "queued": time.time()})
        shop.emit_queue()


def phoenix_metrics():
    """Tokens and utterance p95 from Phoenix spans in the last minute.
    None when Phoenix is down; the page then uses the floor's own turns."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=60)
    url = (f"{PHOENIX}/v1/projects/{tracing.PROJECT}/spans"
           f"?start_time={start.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
           f"&end_time={end.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}&limit=1000")
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            spans = json.load(resp).get("data") or []
    except Exception:
        return None
    lat = sorted(
        (datetime.fromisoformat(s["end_time"]) - datetime.fromisoformat(s["start_time"])).total_seconds()
        for s in spans if s["name"] == "utterance"
    )
    tokens = sum(
        int(s["attributes"].get("llm.token_count.prompt") or 0)
        + int(s["attributes"].get("llm.token_count.completion") or 0)
        for s in spans if s["span_kind"] == "LLM"
    )
    p95 = lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] if lat else 0
    return {"tokens_per_min": tokens, "p95": round(p95, 2), "source": "phoenix"}


def metrics_loop(shop):
    while not shop.stop.is_set():
        shop.emit({"type": "metrics", **(phoenix_metrics() or shop.local_metrics())})
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
        elif path in ("/", "/index.html"):
            self._send(200, (UI / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/shop.js":
            self._send(200, (UI / "shop.js").read_bytes(), "text/javascript; charset=utf-8")
        else:
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
        try:
            self._write({"type": "hello", "staff": self.shop.staff,
                         "arrival": self.shop.arrival, "model": self.shop.model,
                         "workers": WORKERS, "cap": MAX_WAIT,
                         "conversations": len(self.shop.customers), "tally": tally})
            # Replay the board so a reload is not an empty shop. Every event
            # the page understands is idempotent, so replay is just catch-up.
            seen = 0
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
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, help="first N conversations only")
    parser.add_argument("--staff", type=int, default=10, help=f"workers, 1-{WORKERS}")
    parser.add_argument("--arrival", type=int, default=120, help="customers per minute")
    parser.add_argument("--ui", action="store_true", help=f"serve the page on :{PORT}")
    parser.add_argument("--label", default="", help="added to the run's name in Phoenix")
    args = parser.parse_args()

    counter.load_dotenv()
    customers = load_customers(args.limit)
    if not customers:
        raise SystemExit(f"no conversations in {LINES.name}")
    tracing.start()
    shop = Shop(customers, max(1, min(WORKERS, args.staff)), args.arrival)
    settings = shop.shelf.settings
    name = shop.name = (f"{shop.model}-{settings.effort or 'default'}"
                        f"-{settings.flow}{'-' + args.label if args.label else ''}"
                        f"-{time.strftime('%m%d-%H%M%S')}")
    httpd = None
    if args.ui:
        Handler.shop = shop
        try:
            httpd = ThreadingHTTPServer((HOST, PORT), Handler)
        except OSError as exc:
            shop.close()
            raise SystemExit(f"cannot listen on {HOST}:{PORT}: {exc}") from exc
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=metrics_loop, args=(shop,), daemon=True).start()
        print(f"floor on http://{HOST}:{PORT}", file=sys.stderr)
    print(f"{name}: {len(customers)} conversations, staff {shop.staff}, "
          f"{shop.arrival}/min", file=sys.stderr)
    for worker in range(WORKERS):
        threading.Thread(target=worker_loop, args=(shop, worker), daemon=True).start()
    threading.Thread(target=arrival_loop, args=(shop,), daemon=True).start()
    shop.emit({"type": "config", "staff": shop.staff, "arrival": shop.arrival})
    try:
        shop.finished.wait()
        print(f"{name}: done. traces in {PHOENIX} project {tracing.PROJECT}", file=sys.stderr)
        if httpd:
            print("run finished; page stays up until Ctrl-C", file=sys.stderr)
            shop.stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        shop.stop.set()
        if httpd:
            httpd.shutdown()
        shop.close()


if __name__ == "__main__":
    main()
