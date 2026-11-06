"""Liveness and readiness probes."""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from silentctx.state import AppStateDep

READY_PING_TIMEOUT_S = 2.0

router = APIRouter(tags=["ops"])


class CheckStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class Liveness(BaseModel):
    status: CheckStatus = CheckStatus.OK


class Check(BaseModel):
    status: CheckStatus
    error: str | None = None
    missing: list[str] | None = None


class Readiness(BaseModel):
    status: CheckStatus
    checks: dict[str, Check]

    @classmethod
    def from_checks(cls, checks: dict[str, Check]) -> Readiness:
        statuses = {c.status for c in checks.values()}
        if CheckStatus.DOWN in statuses:
            overall = CheckStatus.DOWN
        elif CheckStatus.DEGRADED in statuses:
            overall = CheckStatus.DEGRADED
        else:
            overall = CheckStatus.OK
        return cls(status=overall, checks=checks)


@router.get("/healthz")
async def healthz() -> Liveness:
    """Liveness: the process is up. Never touches dependencies."""
    return Liveness()


@router.get(
    "/readyz",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "A required dependency is down"}},
)
async def readyz(app_state: AppStateDep) -> JSONResponse:
    """Readiness: Neo4j answers and migrations are applied. A missing LLM key is reported
    as degraded (still 200), so the seeded demo keeps working offline."""
    checks: dict[str, Check] = {}

    try:
        await app_state.graph.ping(READY_PING_TIMEOUT_S)
        checks["neo4j"] = Check(status=CheckStatus.OK)
    except Exception as exc:  # noqa: BLE001  # any failure means "not ready"
        checks["neo4j"] = Check(status=CheckStatus.DOWN, error=type(exc).__name__)

    graph = app_state.graph
    if graph.fatal_error:
        checks["schema"] = Check(status=CheckStatus.DOWN, error=graph.fatal_error)
    elif graph.migrated:
        checks["schema"] = Check(status=CheckStatus.OK)
    else:
        checks["schema"] = Check(status=CheckStatus.DOWN, error=graph.last_error)

    missing = app_state.settings.missing_llm_keys()
    checks["llm"] = (
        Check(status=CheckStatus.DEGRADED, missing=missing)
        if missing
        else Check(status=CheckStatus.OK)
    )

    readiness = Readiness.from_checks(checks)
    code = 503 if readiness.status is CheckStatus.DOWN else 200
    return JSONResponse(readiness.model_dump(mode="json", exclude_none=True), status_code=code)
