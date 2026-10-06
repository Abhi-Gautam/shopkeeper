"""Score finished conversations: onto the conversation's trace as
annotations, and into a Phoenix experiment as one run each.

Scores are code, not a judge. They read what DuckDB answered, not how the
reply sounds. A score is None when it does not apply: no buys to check,
or no `=` outcome written for that conversation.
"""

import hashlib
import json
import re
import statistics
import sys
from datetime import datetime, timezone

from phoenix.client import Client

# Dollars per 1M tokens: input, cached input, output. Checked 2026-10-05
# on developers.openai.com/api/docs/pricing, standard tier.
PRICES = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}


def cost(model, tokens):
    price = PRICES.get(model)
    if not price:
        return None
    fresh = tokens["in"] - tokens["cached"]
    return round((fresh * price[0] + tokens["cached"] * price[1]
                  + tokens["out"] * price[2]) / 1e6, 6)


# --- scores. Each answers one question: (score, label, explanation). ------

def answered(out, expect):
    """Every turn got a real reply: not empty, not stuck, not an error."""
    bad = [t["outcome"] for t in out["turns"] if t["outcome"] != "replied"]
    return 1 - len(bad) / len(out["turns"]), ",".join(bad) or "ok", None


def grounded(out, expect):
    """Every buy used a SKU that guide had shown in this conversation."""
    if not out["buys"]:
        return None, "no buys", None
    loose = [b["sku"] for b in out["buys"] if not b["grounded"]]
    why = "not from guide: " + ", ".join(map(str, loose)) if loose else None
    return 1 - len(loose) / len(out["buys"]), "invented" if loose else "ok", why


def sale_decision(out, expect):
    """Sold when it should, held back when it should not."""
    if expect is None:
        return None, "no outcome written", None
    want = bool(expect)
    sold = any(b["status"] == "sold" for b in out["buys"])
    label = ("sold" if sold else "no sale") + (" (right)" if sold == want else " (wrong)")
    return float(sold == want), label, None


def right_items(out, expect):
    """Of the items that should be sold: right product, pack and quantity."""
    if not expect:
        return None, "nothing to sell", None
    sold = [b for b in out["buys"] if b["status"] == "sold"]
    missed = [
        f"{name} {pack or ''} x{qty}"
        for name, pack, qty in expect
        if not any(re.search(name, f'{b.get("brand") or ""} {b["name"] or ""}', re.I)
                   and pack in (None, b["pack_label"]) and b["qty"] == qty
                   for b in sold)
    ]
    why = "missed: " + "; ".join(missed) if missed else None
    return 1 - len(missed) / len(expect), "missed" if missed else "ok", why


def seconds(out, expect):
    """Wall time for the whole conversation, under this run's load."""
    return out["seconds"], None, None


def cost_usd(out, expect):
    """Dollars for the whole conversation at list price."""
    return out["cost_usd"], None, None


SCORES = [answered, grounded, sale_decision, right_items, seconds, cost_usd]


class Experiment:
    """One run = one Phoenix experiment over the dataset of conversations
    it played. Phoenix being down costs the upload, not the run."""

    def __init__(self, url, customers, name, metadata):
        self.customers = customers
        self.scores = []
        self.client = None
        try:
            client = Client(base_url=url)
            dataset = self.dataset(client, customers)
            self.examples = {e["metadata"]["index"]: e["id"] for e in dataset.examples}
            self.experiment = client.experiments.create(
                dataset_id=dataset.id,
                experiment_name=name,
                experiment_metadata=metadata,
            )
            self.url = client.experiments.get_experiment_url(
                dataset_id=dataset.id, experiment_id=self.experiment["id"])
            self.client = client
        except Exception as exc:
            print(f"phoenix experiment off: {exc}", file=sys.stderr)

    @staticmethod
    def dataset(client, customers):
        """Same conversations, same dataset. Edit the file, get a new one."""
        body = json.dumps([[c["lines"], c["expect"]] for c in customers])
        name = f"conversations-{len(customers)}-{hashlib.sha256(body.encode()).hexdigest()[:6]}"
        try:
            return client.datasets.get_dataset(dataset=name)
        except Exception:
            return client.datasets.create_dataset(
                name=name,
                inputs=[{"lines": c["lines"]} for c in customers],
                outputs=[{"expect": c["expect"]} for c in customers],
                metadata=[{"index": i} for i in range(len(customers))],
            )

    def log(self, index, out, started, ended, trace_id, span_id):
        expect = self.customers[index]["expect"]
        results = {f.__name__: f(out, expect) for f in SCORES}
        self.scores.append(results)
        if self.client is None:
            return
        try:
            for name, (score, label, why) in results.items():
                if score is not None:
                    self.client.spans.add_span_annotation(
                        span_id=span_id, annotation_name=name, annotator_kind="CODE",
                        score=score, label=label, explanation=why,
                    )
            run = self.client.experiments.log_run(
                experiment_id=self.experiment["id"],
                dataset_example_id=self.examples[index],
                output=out,
                start_time=started,
                end_time=ended,
                trace_id=trace_id,
            )
            now = datetime.now(timezone.utc)
            for name, (score, label, why) in results.items():
                self.client.experiments.log_evaluation(
                    experiment_run_id=run["id"], name=name, score=score,
                    label=label, explanation=why, start_time=now, end_time=now,
                )
        except Exception as exc:
            print(f"phoenix log failed: {exc}", file=sys.stderr)

    def summary(self):
        lines = []
        for f in SCORES:
            values = [r[f.__name__][0] for r in self.scores if r[f.__name__][0] is not None]
            if not values:
                continue
            if f in (seconds, cost_usd):
                total = f" total {sum(values):.4f}" if f is cost_usd else ""
                lines.append(f"{f.__name__:14} mean {statistics.mean(values):.4f}"
                             f"  max {max(values):.4f}{total}  (n={len(values)})")
            else:
                lines.append(f"{f.__name__:14} {statistics.mean(values):.0%}  (n={len(values)})")
        if self.client is not None:
            lines.append(self.url)
        return "\n".join(lines)
