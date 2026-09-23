"""Entry point for the Treeweft MCP server.

stdio is the default transport. The MCP server is a stateless per-caller
adapter over the indexer, so the natural deployment is one process per user,
spawned by the client, with the user's credential supplied via the environment
— which is what the MCP specification prescribes for stdio:

    Implementations using an STDIO transport SHOULD NOT follow this
    [authorization] specification, and instead retrieve credentials from the
    environment.

stdio also has no network surface at all: no port, no Origin header to
validate, no DNS-rebinding exposure, and no way for an uncredentialed third
party to reach the tools.

The HTTP+SSE transport remains available behind TREEWEFT_MCP_TRANSPORT=sse but
is deprecated upstream (since protocol revision 2025-03-26) and warns on start.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# FastMCP.run() accepts exactly these.
SUPPORTED_TRANSPORTS = ("stdio", "sse", "streamable-http")

# Deprecated upstream; kept only for existing deployments.
DEPRECATED_TRANSPORTS = frozenset({"sse"})

DEFAULT_TRANSPORT = "stdio"


def resolve_transport(raw: str | None) -> str:
    """Normalise TREEWEFT_MCP_TRANSPORT. Unknown values raise rather than
    falling back, so a typo can never silently open a network listener."""
    value = (raw or "").strip().lower()
    if not value:
        return DEFAULT_TRANSPORT
    if value not in SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"Unknown TREEWEFT_MCP_TRANSPORT {raw!r}. "
            f"Supported: {', '.join(SUPPORTED_TRANSPORTS)} (default: stdio)."
        )
    return value


def main() -> None:
    """Console-script entry point (`treeweft-mcp`)."""
    transport = resolve_transport(os.environ.get("TREEWEFT_MCP_TRANSPORT"))
    if transport in DEPRECATED_TRANSPORTS:
        logger.warning(
            "TREEWEFT_MCP_TRANSPORT=%s selects the HTTP+SSE transport, "
            "deprecated upstream since MCP protocol revision 2025-03-26 and "
            "eligible for removal. It also exposes a network port with no "
            "authentication of its own. Prefer the default stdio transport.",
            transport,
        )
    # Imported here, not at module scope: the stdio path must not pull in the
    # HTTP application or its dependencies.
    from treeweft.application.mcp_server import mcp

    mcp.run(transport=transport)
