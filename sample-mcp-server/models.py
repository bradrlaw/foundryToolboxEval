"""Output models of the sample tools: each tool publishes one as its outputSchema, and MCPServer checks the tool's
structured content against it before replying."""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, WithJsonSchema

Industry = Literal["manufacturing", "healthcare", "energy", "retail"]
Region = Literal["North America", "Europe", "APAC"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Customer(_Closed):
    """A customer profile."""
    customer_id: str
    name: str
    industry: Industry
    ticker: str | None = Field(description="None if the company is privately held")
    tier: Literal["enterprise", "mid-market", "smb"]
    region: Region
    account_manager: str
    annual_contract_value_usd: int
    open_support_tickets: int = Field(ge=0)
    since: date = Field(description="Customer since")


class MarketConditions(_Closed):
    """Current market conditions for an industry."""
    industry: Industry
    as_of: date
    outlook: Literal["positive", "neutral", "negative"]
    yoy_growth_pct: float = Field(description="Year-over-year growth, percent")
    sentiment_score: float = Field(ge=-1, le=1, description="-1 (bearish) to 1 (bullish)")
    drivers: list[str]
    risks: list[str]


class StockQuote(_Closed):
    """A stock quote."""
    ticker: str
    as_of: datetime
    price_usd: float
    change_pct: float = Field(description="Change since the previous close, percent")
    market_cap_usd: int
    pe_ratio: float


class AccessDenied(_Closed):
    """The signed-in user has no app role for the customer's region (AUTH_MODE=entra)."""
    access_denied: Literal[True]
    customer_id: str
    region: Region
    reason: str
    required_roles: list[str] = Field(description="App roles, any one of which grants access")


class WhoAmI(_Closed):
    """The signed-in user as the server sees them. Never includes the token itself."""
    authenticated: bool
    name: str | None = None
    username: str | None = None
    oid: str | None = None
    tid: str | None = None
    roles: list[str] = []
    scopes: list[str] = []
    client_app_id: str | None = None
    token_version: str | None = None
    auth_mode: Literal["entra"]
    regions_visible: list[Region] = Field(description="Customer regions this user may read")


def _any_of(*models: type[BaseModel]) -> WithJsonSchema:
    # An outputSchema needs an object root; inlining the members also keeps $defs and $ref out of it.
    return WithJsonSchema({"type": "object", "anyOf": [m.model_json_schema() for m in models]})


class CustomerOrDenied(RootModel[Annotated[Customer | AccessDenied, _any_of(Customer, AccessDenied)]]):
    """A customer profile, or a denial if the user's app roles don't cover the customer's region."""


class QuoteOrDenied(RootModel[Annotated[StockQuote | AccessDenied, _any_of(StockQuote, AccessDenied)]]):
    """A stock quote, or a denial if it was looked up by a customer the user may not read."""
