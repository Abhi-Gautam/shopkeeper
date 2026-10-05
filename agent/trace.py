"""Send each counter turn to Phoenix. A dead collector must not stop a sale.

Model calls are traced by the OpenInference OpenAI instrumentor. It opens
the span before the request and closes it after, records messages, tool
calls, tokens and the model that answered, and marks a 429 as an error.
This file only adds what no library can see: the turn around those calls
and the two DuckDB tools, which go over our own MCP pipe.

The OpenRouter key is never an attribute.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SESSION = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def start():
    endpoint = os.environ.get(
        "PHOENIX_OTLP", "http://localhost:6006/v1/traces"
    )
    provider = TracerProvider(
        resource=Resource.create({"service.name": "shopkeeper"})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=2))
    )
    trace.set_tracer_provider(provider)
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    return trace.get_tracer("shopkeeper")


def flush():
    provider = trace.get_tracer_provider()
    force = getattr(provider, "force_flush", None)
    if force:
        force(timeout_millis=2000)


@contextmanager
def turn_span(tracer, request_id, utterance, session=None):
    """One utterance. Model and tool spans opened inside nest under it.

    An exception that escapes marks the turn as an error with the
    exception attached, so a 429 that ran out of retries is visible.
    """
    with tracer.start_as_current_span("turn", attributes={
        "openinference.span.kind": "AGENT",
        "session.id": session or SESSION,
        "shopkeeper.request_id": request_id,
        "input.value": utterance,
    }) as span:
        yield span


@contextmanager
def tool_span(tracer, name, arguments):
    """Timed around the MCP call itself, lock wait included."""
    with tracer.start_as_current_span(f"tool.{name}", attributes={
        "openinference.span.kind": "TOOL",
        "tool.name": name,
        "input.value": json.dumps(arguments, default=str)[:4000],
    }) as span:
        yield span
