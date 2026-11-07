"""OpenTelemetry traces to Arize Phoenix (UI-1).

`setup_tracing` registers Phoenix's tracer provider and the OpenInference instrumentors that
are installed (LangChain/LangGraph: every LLM call, graph node and tool call). Without
`PHOENIX_COLLECTOR_ENDPOINT` it does nothing and spans are no-ops.
"""

from __future__ import annotations

from opentelemetry import trace

from silentctx.config import Settings
from silentctx.logging import get_logger

log = get_logger(__name__)


def setup_tracing(settings: Settings, service: str) -> None:
    if not settings.phoenix_collector_endpoint:
        return
    from phoenix.otel import register

    register(
        project_name=settings.phoenix_project_name,
        endpoint=settings.phoenix_collector_endpoint,
        auto_instrument=True,
        batch=True,
        set_global_tracer_provider=True,
        verbose=False,
    )
    log.info("tracing_enabled", service=service, endpoint=settings.phoenix_collector_endpoint)


def current_trace_id() -> str | None:
    """The W3C trace id of the active span, or None when tracing is off."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
