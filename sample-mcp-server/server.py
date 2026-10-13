"""Sample MCP server exposing synthetic customer, market, and stock tools.

Run locally: python sample-mcp-server\server.py   (serves http://localhost:8000/mcp)
"""
import json
import os

from mcp.server.fastmcp import FastMCP

import data

mcp = FastMCP(
    "sample-customer-tools",
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
)


def _run(fn, **kwargs) -> str:
    try:
        return json.dumps(fn(**kwargs))
    except data.ToolError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
def get_customer(customer_id: str | None = None, name: str | None = None) -> str:
    """Look up a customer profile (industry, tier, region, account manager, contract value, ticker).
    Provide customer_id (e.g. C1001) or a full/partial company name."""
    return _run(data.get_customer, customer_id=customer_id, name=name)


@mcp.tool()
def get_market_conditions(industry: str) -> str:
    """Get current market conditions for an industry: outlook, YoY growth, sentiment, drivers, risks.
    Supported industries: manufacturing, healthcare, energy, retail."""
    return _run(data.get_market_conditions, industry=industry)


@mcp.tool()
def get_stock_quote(customer_id: str | None = None, ticker: str | None = None) -> str:
    """Get a customer's current stock price and market cap by customer_id or ticker.
    Privately held customers have no quote."""
    return _run(data.get_stock_quote, customer_id=customer_id, ticker=ticker)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
