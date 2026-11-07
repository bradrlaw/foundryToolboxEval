r"""Sample MCP server exposing synthetic customer, market, and stock tools.

Run locally from the repo root:  .\.venv\Scripts\python sample-mcp-server\server.py
Serves streamable HTTP at http://localhost:8000/mcp (HOST and PORT override the bind address).

AUTH_MODE=none (default): no authentication.
AUTH_MODE=entra: every request needs an Entra access token issued to the server's API app on
behalf of a user (see entra_auth.py for the settings). Adds a whoami tool, and customer data is
limited to the regions the caller's app roles allow (data.REGION_ROLES).

Each tool publishes an outputSchema (models.py) and returns its result as structured content,
plus the same JSON as text for the model.
"""
import json
import os
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

import data
import entra_auth
import models

NAME, VERSION = "sample-customer-tools", "1.2.0"


def _result(fn) -> CallToolResult:
    """The tool's dict as structured content, and as compact JSON text."""
    try:
        value = fn()
    except data.ToolError as e:
        # Only ToolError keeps its message in the isError result; other exceptions
        # reach the caller as a bare "Error executing tool <name>".
        raise ToolError(str(e)) from e
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value))], structured_content=value)


def create_server(entra: entra_auth.EntraSettings | None = None, verifier=None) -> MCPServer:
    """Build the server. With `entra` settings, requests need a valid token and data is filtered by app role."""
    if entra is None:
        mcp = MCPServer(NAME, version=VERSION)
    else:
        mcp = MCPServer(NAME, version=VERSION, token_verifier=verifier or entra_auth.EntraTokenVerifier(entra),
                        auth=entra.auth_settings())

    def caller(ctx: Context | None):
        return entra_auth.caller(ctx.request_context.request) if ctx is not None else None

    def roles(ctx: Context | None) -> list[str]:
        token = caller(ctx)
        return list((token.claims or {}).get("roles", [])) if token else []

    def gate(ctx: Context | None, customer: dict) -> dict | None:
        return data.access_denied(customer, roles(ctx)) if entra else None

    # With Entra, a lookup by customer can return a denial instead, so its schema admits either shape.
    customer_output = models.Customer if entra is None else models.CustomerOrDenied
    quote_output = models.StockQuote if entra is None else models.QuoteOrDenied

    @mcp.tool()
    def get_customer(customer_id: str | None = None, name: str | None = None,
                     ctx: Context | None = None) -> Annotated[CallToolResult, customer_output]:
        """Look up a customer profile (industry, tier, region, account manager, contract value, ticker).
        Provide customer_id (e.g. C1001) or a full/partial company name that matches one customer."""
        def run():
            customer = data.get_customer(customer_id=customer_id, name=name)
            return gate(ctx, customer) or customer
        return _result(run)

    @mcp.tool()
    def get_market_conditions(industry: str) -> Annotated[CallToolResult, models.MarketConditions]:
        """Get current market conditions for an industry: outlook, YoY growth, sentiment, drivers, risks.
        Supported industries: manufacturing, healthcare, energy, retail."""
        return _result(lambda: data.get_market_conditions(industry=industry))

    @mcp.tool()
    def get_stock_quote(customer_id: str | None = None, ticker: str | None = None,
                        ctx: Context | None = None) -> Annotated[CallToolResult, quote_output]:
        """Get a customer's current stock price and market cap by customer_id or ticker.
        Privately held customers have no quote."""
        def run():
            # A lookup by customer reveals which customer the quote belongs to; by ticker it is public data.
            if customer_id and (denied := gate(ctx, data.get_customer(customer_id=customer_id))):
                return denied
            return data.get_stock_quote(customer_id=customer_id, ticker=ticker)
        return _result(run)

    if entra is not None:
        @mcp.tool()
        def whoami(ctx: Context) -> Annotated[CallToolResult, models.WhoAmI]:
            """Show who the signed-in user is as this server sees them: name, username, object ID, tenant,
            app roles, scopes, the calling client app, and the customer regions they may read."""
            return _result(lambda: {**entra_auth.describe(caller(ctx)), "auth_mode": "entra",
                                    "regions_visible": data.visible_regions(roles(ctx))})

    return mcp


def _from_env() -> MCPServer:
    mode = os.environ.get("AUTH_MODE", "none").strip().lower()
    if mode not in ("none", "entra"):
        raise SystemExit(f"AUTH_MODE must be none or entra, not {mode!r}")
    return create_server(entra_auth.EntraSettings.from_env() if mode == "entra" else None)


mcp = _from_env()


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
