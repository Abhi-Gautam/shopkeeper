"""Send counter traffic to Phoenix. A dead collector must not stop a sale.

The OpenInference Agents instrumentor turns the SDK's own trace into
spans: the agent run, every model call (`response`) and every tool call
(`guide` / `buy`). It replaces the SDK's default exporter, so nothing is
uploaded to OpenAI.

On top of that, this file writes what only the counter knows:
  conversation  one customer, the root of the trace
  utterance     one line they said: request_id, outcome
A turn typed at the CLI has no conversation, so its utterance is the root.
The API key is never an attribute.
"""

import os
from contextlib import contextmanager
from datetime import datetime, timezone

from openinference.instrumentation import using_session
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
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
    OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)


def flush():
    trace.get_tracer_provider().force_flush(timeout_millis=2000)


@contextmanager
def conversation_span(session, lines):
    """One customer, start to finish. Always the root of its trace."""
    tracer = trace.get_tracer("shopkeeper")
    with using_session(session), tracer.start_as_current_span(
        "conversation",
        context=Context(),
        attributes={
            "openinference.span.kind": "CHAIN",
            "session.id": session,
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
