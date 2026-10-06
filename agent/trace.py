"""Send counter traffic to Phoenix. A dead collector must not stop a sale.

    conversation      one customer, the root of the trace; scores are
    │                 annotations on it
    └─ utterance      one message they said: request_id, outcome
       ├─ Response    each model call, from the OpenInference OpenAI
       │              instrumentor: messages, tokens, errors
       ├─ guide / buy each tool call: arguments, result
       └─ receipt     the sale the counter printed, in the receipt flow

A turn typed at the CLI has no conversation, so its utterance is the root.
The Agents SDK's own tracing is off: its spans only wrapped these in empty
levels, and its default exporter uploads to OpenAI.
The API key is never an attribute.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from agents import set_tracing_disabled
from openinference.instrumentation import using_session
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SESSION = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
PROJECT = os.environ.get("PHOENIX_PROJECT", "shopkeeper")


class WithoutMcp(BatchSpanProcessor):
    """Drop the MCP library's own transport spans. They repeat the guide
    and buy spans one level down, and its startup probes show up as
    stray root traces."""

    def on_end(self, span):
        if span.instrumentation_scope.name != "mcp-python-sdk":
            super().on_end(span)


def start():
    endpoint = os.environ.get("PHOENIX_OTLP", "http://localhost:6006/v1/traces")
    provider = TracerProvider(resource=Resource.create({
        "service.name": "shopkeeper",
        "openinference.project.name": PROJECT,
    }))
    provider.add_span_processor(
        WithoutMcp(OTLPSpanExporter(endpoint=endpoint, timeout=2))
    )
    trace.set_tracer_provider(provider)
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    set_tracing_disabled(True)


def flush():
    trace.get_tracer_provider().force_flush(timeout_millis=2000)


@contextmanager
def conversation_span(session, lines, run=None):
    """One customer, start to finish. Always the root of its trace.
    `run` names the run it belongs to, so runs side by side stay apart."""
    tracer = trace.get_tracer("shopkeeper")
    with using_session(session), tracer.start_as_current_span(
        "conversation",
        context=Context(),
        attributes={
            "openinference.span.kind": "CHAIN",
            "session.id": session,
            "shopkeeper.run": run or SESSION,
            "input.value": "\n".join(lines),
        },
    ) as span:
        yield span


@contextmanager
def turn_span(request_id, utterance, model, session=None, parent=None):
    """One utterance, under `parent` (a conversation) or as its own root.

    The parent is passed in, not picked up from ambient context: turns run
    on the shelf's event loop thread, where the caller's context is not.
    An exception that escapes marks the span as an error.
    """
    session = session or SESSION
    tracer = trace.get_tracer("shopkeeper")
    with using_session(session), tracer.start_as_current_span(
        "utterance",
        context=parent if parent is not None else Context(),
        attributes={
            "openinference.span.kind": "CHAIN",
            "session.id": session,
            "shopkeeper.request_id": request_id,
            "shopkeeper.model": model,
            "input.value": utterance,
        },
    ) as span:
        yield span


@contextmanager
def tool_span(name, arguments):
    """One guide or buy call, timed around the MCP request itself."""
    tracer = trace.get_tracer("shopkeeper")
    with tracer.start_as_current_span(name, attributes={
        "openinference.span.kind": "TOOL",
        "tool.name": name,
        "input.value": json.dumps(arguments),
    }) as span:
        yield span


@contextmanager
def step_span(name, output):
    """A step the counter does in code, like printing a receipt."""
    tracer = trace.get_tracer("shopkeeper")
    with tracer.start_as_current_span(name, attributes={
        "openinference.span.kind": "CHAIN",
        "output.value": output,
    }) as span:
        yield span
