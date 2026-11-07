"""RFC 9457 problem details (`application/problem+json`) for every error the API returns."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from asgi_correlation_id import correlation_id
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from silentctx.logging import REQUEST_ID_HEADER, get_logger

PROBLEM_JSON = "application/problem+json"
log = get_logger(__name__)


class Problem(BaseModel):
    """RFC 9457 problem document. Extension members are allowed (e.g. `errors`)."""

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int = Field(ge=400, le=599)
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = None


class ProblemError(Exception):
    """Raise from application code to return a specific problem document."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        type_: str = "about:blank",
        **extensions: Any,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_
        self.extensions = extensions


class WriteRejectedError(ProblemError):
    """A context-layer write that would break a rule (unknown entity or event, a temporal
    invariant): nothing is written."""

    def __init__(self, detail: str) -> None:
        super().__init__(422, "Write rejected", detail)


def problem_response(
    request: Request,
    status: int,
    *,
    title: str | None = None,
    detail: str | None = None,
    type_: str = "about:blank",
    headers: dict[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    problem = Problem(
        type=type_,
        title=title or HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=request.url.path,
        request_id=correlation_id.get(),
        **extensions,
    )
    return JSONResponse(
        problem.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


async def _problem_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(
        request,
        exc.status,
        title=exc.title,
        detail=exc.detail,
        type_=exc.type,
        **exc.extensions,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail: str | None = exc.detail if isinstance(exc.detail, str) else None
    # Don't repeat the reason phrase as the detail ("Not Found" / "Not Found").
    if detail == HTTPStatus(exc.status_code).phrase:
        detail = None
    headers = dict(exc.headers) if exc.headers else None
    return problem_response(request, exc.status_code, detail=detail, headers=headers)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem_response(
        request,
        422,
        detail="The request is invalid.",
        errors=exc.errors(),
    )


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", error_type=type(exc).__name__, path=request.url.path)
    # Starlette sends a 500 from outside the user middleware, so the correlation-id
    # middleware can't add its header: add it here (as asgi-correlation-id documents).
    request_id = correlation_id.get()
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    # Never leak internals in the body; the request id links the response to the log.
    return problem_response(request, 500, detail="An unexpected error occurred.", headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_error)
