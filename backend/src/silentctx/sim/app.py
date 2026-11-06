"""`silentctx-sim`: the network simulator for the live demo (plan 001 WS6).

Only the service shell and its health probe exist so far. SIM-1 adds the as-running
state for TRN-017 / TRN-019 / TRN-010, live telemetry and alarms at "now", and the
`POST /sim/fault` and `POST /sim/action` endpoints.
"""

from __future__ import annotations

import os

import uvicorn
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI

from silentctx import __version__
from silentctx.errors import install_error_handlers
from silentctx.health import Liveness
from silentctx.logging import REQUEST_ID_HEADER, configure_logging


def create_app() -> FastAPI:
    app = FastAPI(title="silentctx-sim", version=__version__, redoc_url=None)
    install_error_handlers(app)
    app.add_middleware(CorrelationIdMiddleware, header_name=REQUEST_ID_HEADER)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> Liveness:
        """Liveness of the simulator process."""
        return Liveness()

    return app


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "silentctx.sim.app:create_app",
        factory=True,
        host=os.environ.get("SIM_HOST", "0.0.0.0"),  # noqa: S104  # served inside the container
        port=int(os.environ.get("SIM_PORT", "8100")),
        log_config=None,
    )
