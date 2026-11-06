"""`silentctx-api`: run the API (FastAPI + MCP at /mcp) with uvicorn."""

from __future__ import annotations

import os

import uvicorn


def serve() -> None:
    uvicorn.run(
        "silentctx.main:create_app",
        factory=True,
        host=os.environ.get("API_HOST", "0.0.0.0"),  # noqa: S104  # served inside the container
        port=int(os.environ.get("API_PORT", "8000")),
        reload=os.environ.get("API_RELOAD", "false").lower() == "true",
        log_config=None,  # silentctx.logging owns the handlers
        proxy_headers=True,
    )
