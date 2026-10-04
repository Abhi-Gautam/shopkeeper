"""Send each counter turn to Phoenix. A dead collector must not stop a sale.

Spans follow OpenInference names so the Phoenix UI shows the messages,
not a pile of unnamed spans. The OpenRouter key is never an attribute.
"""

import json
import os
from datetime import datetime, timezone

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
    return trace.get_tracer("shopkeeper")


def flush():
    provider = trace.get_tracer_provider()
    force = getattr(provider, "force_flush", None)
    if force:
        force(timeout_millis=2000)


def text_of(message):
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    calls = message.get("tool_calls") or []
    if not calls:
        return content if isinstance(content, str) else ""
    parts = []
    for call in calls:
        fn = call.get("function") or {}
        parts.append(f"{fn.get('name')}({fn.get('arguments')})")
    return "\n".join(parts)


def messages_on(span, prefix, messages):
    for index, message in enumerate(messages):
        role = message.get("role") or "unknown"
        span.set_attribute(f"{prefix}.{index}.message.role", role)
        span.set_attribute(
            f"{prefix}.{index}.message.content", text_of(message)
        )


def turn_span(tracer, request_id, utterance):
    span = tracer.start_span("turn")
    span.set_attribute("openinference.span.kind", "AGENT")
    span.set_attribute("session.id", SESSION)
    span.set_attribute("shopkeeper.request_id", request_id)
    span.set_attribute("input.value", utterance)
    return span


def llm_span(tracer, parent, model, messages, reply, usage):
    context = trace.set_span_in_context(parent)
    span = tracer.start_span("llm", context=context)
    span.set_attribute("openinference.span.kind", "LLM")
    span.set_attribute("llm.model_name", model)
    span.set_attribute("input.value", json.dumps(messages, default=str)[:8000])
    span.set_attribute("output.value", text_of(reply)[:4000])
    messages_on(span, "llm.input_messages", messages)
    messages_on(span, "llm.output_messages", [reply])
    if usage:
        span.set_attribute(
            "llm.token_count.prompt", int(usage.get("prompt_tokens") or 0)
        )
        span.set_attribute(
            "llm.token_count.completion",
            int(usage.get("completion_tokens") or 0),
        )
    span.end()


def tool_span(tracer, parent, name, arguments, result):
    context = trace.set_span_in_context(parent)
    span = tracer.start_span(f"tool.{name}", context=context)
    span.set_attribute("openinference.span.kind", "TOOL")
    span.set_attribute("tool.name", name)
    span.set_attribute("input.value", json.dumps(arguments, default=str)[:4000])
    span.set_attribute("output.value", (result or "")[:4000])
    span.end()
