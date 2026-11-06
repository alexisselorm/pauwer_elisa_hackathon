"""App factory behaviour with Neo4j unreachable (no Docker needed)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from silentctx.config import Settings
from silentctx.errors import ProblemError
from silentctx.main import create_app
from tests.conftest import AGENT_TOKEN, OTHER_TOKEN

RID = "0f8fad5bd9cb469fa16570867728950e"  # a valid uuid4 hex
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}


@pytest.fixture
def client(make_settings: Callable[..., Settings]) -> Iterator[TestClient]:
    app = create_app(make_settings())

    boom = APIRouter()

    @boom.get("/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("secret internals")

    @boom.get("/_test/problem")
    async def _problem() -> None:
        raise ProblemError(409, "Conflict", "already superseded", assertion_id="ASR-0001")

    @boom.get("/_test/typed/{n}")
    async def _typed(n: int) -> dict[str, int]:
        return {"n": n}

    app.include_router(boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_healthz_is_ok_even_when_neo4j_is_down(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_is_503_when_neo4j_is_down(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "down"
    assert body["checks"]["neo4j"]["status"] == "down"
    assert body["checks"]["schema"]["status"] == "down"
    assert body["checks"]["llm"] == {
        "status": "degraded",
        "missing": ["ANTHROPIC_API_KEY", "FEATHERLESS_API_KEY"],
    }


def test_app_keeps_serving_while_neo4j_is_down(client: TestClient) -> None:
    # The bootstrap task retries in the background; the process must not exit.
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    r = client.get("/healthz")
    generated = r.headers["x-request-id"]
    assert len(generated) == 32
    r = client.get("/healthz", headers={"X-Request-ID": RID})
    assert r.headers["x-request-id"] == RID


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "bad id\twith junk"})
    assert r.headers["x-request-id"] != "bad id\twith junk"
    assert len(r.headers["x-request-id"]) == 32


def _assert_problem(r: Any, status: int) -> dict[str, Any]:
    assert r.status_code == status
    assert r.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = r.json()
    assert body["status"] == status
    assert body["type"] == "about:blank"
    assert body["title"]
    return body


def test_404_is_problem_json(client: TestClient) -> None:
    body = _assert_problem(client.get("/nope", headers={"X-Request-ID": RID}), 404)
    assert body["title"] == "Not Found"
    assert body["instance"] == "/nope"
    assert body["request_id"] == RID
    assert "detail" not in body


def test_405_is_problem_json(client: TestClient) -> None:
    _assert_problem(client.post("/healthz"), 405)


def test_validation_error_is_problem_json(client: TestClient) -> None:
    body = _assert_problem(client.get("/_test/typed/abc"), 422)
    assert body["errors"][0]["loc"] == ["path", "n"]


def test_problem_error_carries_extensions(client: TestClient) -> None:
    body = _assert_problem(client.get("/_test/problem"), 409)
    assert body["detail"] == "already superseded"
    assert body["assertion_id"] == "ASR-0001"


def test_unhandled_error_is_500_without_internals(client: TestClient) -> None:
    r = client.get("/_test/boom", headers={"X-Request-ID": RID})
    body = _assert_problem(r, 500)
    assert "secret internals" not in r.text
    assert r.headers["x-request-id"] == RID
    assert body["request_id"] == RID


# --- MCP mount -----------------------------------------------------------------------


def _rpc(client: TestClient, payload: dict[str, Any], token: str | None) -> Any:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=payload, headers=headers)


def _sse_json(r: Any) -> dict[str, Any]:
    """The JSON-RPC message from an SSE (or plain JSON) MCP response."""
    import json

    if r.headers["content-type"].startswith("application/json"):
        return dict(r.json())
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return dict(json.loads(line[len("data: ") :]))
    raise AssertionError(f"no data line in {r.text!r}")


def test_mcp_requires_a_bearer_token(client: TestClient) -> None:
    assert _rpc(client, INITIALIZE, None).status_code == 401
    assert _rpc(client, INITIALIZE, "wrong-token-wrong-token").status_code == 401


def test_mcp_initialize_with_token(client: TestClient) -> None:
    r = _rpc(client, INITIALIZE, AGENT_TOKEN)
    assert r.status_code == 200, r.text
    result = _sse_json(r)["result"]
    assert result["serverInfo"]["name"] == "silentctx"


@pytest.mark.parametrize(
    ("token", "agent_id"), [(AGENT_TOKEN, "ops-agent"), (OTHER_TOKEN, "other-agent")]
)
def test_mcp_ping_reports_the_calling_agent(client: TestClient, token: str, agent_id: str) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {}},
    }
    r = _rpc(client, call, token)
    assert r.status_code == 200, r.text
    result = _sse_json(r)["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["agent_id"] == agent_id
