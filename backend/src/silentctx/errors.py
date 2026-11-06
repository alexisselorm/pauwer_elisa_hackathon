"""RFC 9457 problem details (`application/problem+json`) for every error the API returns."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from silentctx.logging import get_logger

PROBLEM_JSON = "application/problem+json"
log = get_logger(__name__)


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
    body: dict[str, Any] = {
        "type": type_,
        "title": title or HTTPStatus(status).phrase,
        "status": status,
        "instance": request.url.path,
    }
    if detail is not None:
        body["detail"] = detail
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        body["request_id"] = request_id
    body.update(extensions)
    return JSONResponse(
        jsonable_encoder(body), status_code=status, media_type=PROBLEM_JSON, headers=headers
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
    # Starlette runs this handler outside the user middleware, so the request id is no
    # longer bound to the log context: pass it explicitly.
    log.exception(
        "unhandled_error",
        error_type=type(exc).__name__,
        request_id=getattr(request.state, "request_id", None),
        path=request.url.path,
    )
    # Never leak internals in the body; the request id links the response to the log.
    return problem_response(request, 500, detail="An unexpected error occurred.")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_error)
