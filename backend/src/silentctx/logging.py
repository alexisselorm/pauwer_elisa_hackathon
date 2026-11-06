"""structlog setup. Every log line carries the request id set by `asgi-correlation-id`."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from asgi_correlation_id import correlation_id

REQUEST_ID_HEADER = "X-Request-ID"


def _add_request_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    request_id = correlation_id.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """Route both structlog and stdlib logging (uvicorn, neo4j, httpx) through one renderer."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers; let its records propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
