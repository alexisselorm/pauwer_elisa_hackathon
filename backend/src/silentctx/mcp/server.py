"""The MCP server, served by the API process at `/mcp` (Streamable HTTP).

Each agent authenticates with its own bearer token from `MCP_TOKENS`
(agent_id -> token), so every write is attributable (plan 002, plan 001 §7).
The read tools are MCP-1 and the write tools are MCP-2; this module only provides the
server, its auth, and a `ping` tool so that clients can check the connection.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, SecretStr

from silentctx import __version__

SERVER_NAME = "silentctx"
INSTRUCTIONS = (
    "Operational context layer for a telecom transport network. Knowledge captured "
    "from experts' chat and email, bound to network entities and valid over time. "
    "Resolve entities first, then read context as of a time. Cite assertion ids."
)


class StaticTokenVerifier:
    """Maps a bearer token to its agent_id. Constant-time comparison."""

    def __init__(self, tokens: dict[str, SecretStr]) -> None:
        self._tokens = [(agent_id, t.get_secret_value().encode()) for agent_id, t in tokens.items()]

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode()
        match: str | None = None
        # Compare against every token so the timing doesn't reveal which one matched.
        for agent_id, expected in self._tokens:
            if hmac.compare_digest(presented, expected):
                match = agent_id
        if match is None:
            return None
        return AccessToken(token=token, client_id=match, scopes=["context"])


def current_agent_id() -> str | None:
    """The authenticated agent for the current MCP request, if any."""
    access = get_access_token()
    return access.client_id if access else None


def create_mcp_server(tokens: dict[str, SecretStr]) -> FastMCP:
    """A fresh server per app: a session manager can only be run once."""
    server = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        token_verifier=StaticTokenVerifier(tokens),
        auth=AuthSettings(
            # Required by the SDK's settings model; we issue static tokens, so no OAuth
            # server exists. Resource metadata is not published (resource_server_url=None).
            issuer_url=AnyHttpUrl("http://localhost"),
            resource_server_url=None,
            required_scopes=["context"],
        ),
        streamable_http_path="/mcp",
        # Stateless: no session survives in memory, so an API restart (or --reload)
        # doesn't break connected agents.
        stateless_http=True,
        # Every request carries a bearer token, so DNS-rebinding protection (meant for
        # unauthenticated local servers) would only block `http://api:8000` in compose.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool()
    async def ping() -> dict[str, str | None]:
        """Check the connection. Returns the server version and your agent id."""
        return {"server": SERVER_NAME, "version": __version__, "agent_id": current_agent_id()}

    return server
