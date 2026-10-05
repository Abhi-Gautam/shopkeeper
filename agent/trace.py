"""Send each counter turn to Phoenix. A dead collector must not stop a sale.

The OpenInference Agents instrumentor turns the SDK's own trace into
spans: the agent run, every model call (messages, tokens, finish reason,
errors) and every tool call with its arguments and result. It replaces
the SDK's default exporter, so nothing is uploaded to OpenAI.

This file adds one span around that, `utterance` (the SDK already names
its own per-step spans `turn`), carrying what only the
counter knows (request_id, the customer as session, the outcome).
The API key is never an attribute.
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone

from openinference.instrumentation import using_session
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
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
    OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)


def flush():
    provider = trace.get_tracer_provider()
    force = getattr(provider, "force_flush", None)
    if force:
        force(timeout_millis=2000)


@contextmanager
def turn_span(request_id, utterance, model, session=None):
    """One utterance. The agent run and its spans nest under it.

    An exception that escapes marks the turn as an error with the
    exception attached, so a call that ran out of retries is visible.
    """
    tracer = trace.get_tracer("shopkeeper")
    session = session or SESSION
    with using_session(session), tracer.start_as_current_span("utterance", attributes={
        "openinference.span.kind": "CHAIN",
        "session.id": session,
        "shopkeeper.request_id": request_id,
        "shopkeeper.requested_model": model,
        "input.value": utterance,
    }) as span:
        yield span
