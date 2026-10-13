"""Synthetic data and pure logic for the sample tools. No real customer or market data."""

CUSTOMERS = {
    "C1001": {"name": "Contoso Manufacturing", "industry": "manufacturing", "ticker": "CTSO", "tier": "enterprise",
              "region": "North America", "account_manager": "Dana Reyes", "annual_contract_value_usd": 1250000,
              "open_support_tickets": 3, "since": "2016-04-12"},
    "C1002": {"name": "Fabrikam Health", "industry": "healthcare", "ticker": "FBHL", "tier": "enterprise",
              "region": "Europe", "account_manager": "Priya Nair", "annual_contract_value_usd": 880000,
              "open_support_tickets": 0, "since": "2019-09-01"},
    "C1003": {"name": "Northwind Energy", "industry": "energy", "ticker": "NWND", "tier": "mid-market",
              "region": "North America", "account_manager": "Sam Okafor", "annual_contract_value_usd": 410000,
              "open_support_tickets": 1, "since": "2021-02-23"},
    "C1004": {"name": "Adventure Works Retail", "industry": "retail", "ticker": "ADVW", "tier": "mid-market",
              "region": "APAC", "account_manager": "Lena Fischer", "annual_contract_value_usd": 265000,
              "open_support_tickets": 5, "since": "2022-07-18"},
    "C1005": {"name": "Tailspin Logistics", "industry": "manufacturing", "ticker": None, "tier": "smb",
              "region": "Europe", "account_manager": "Dana Reyes", "annual_contract_value_usd": 72000,
              "open_support_tickets": 2, "since": "2023-11-05"},
}

MARKETS = {
    "manufacturing": {"outlook": "neutral", "yoy_growth_pct": 1.8, "sentiment_score": 0.05,
                      "drivers": ["Easing supply chain constraints", "Higher input costs", "Soft export demand"],
                      "risks": ["Tariff changes", "Labor shortages"]},
    "healthcare": {"outlook": "positive", "yoy_growth_pct": 5.4, "sentiment_score": 0.42,
                   "drivers": ["Aging population", "Digital health adoption", "Stable reimbursement"],
                   "risks": ["Regulatory scrutiny", "Staffing costs"]},
    "energy": {"outlook": "negative", "yoy_growth_pct": -2.3, "sentiment_score": -0.31,
               "drivers": ["Renewable transition capex", "Lower commodity prices"],
               "risks": ["Price volatility", "Policy shifts"]},
    "retail": {"outlook": "neutral", "yoy_growth_pct": 2.1, "sentiment_score": 0.1,
               "drivers": ["E-commerce growth", "Resilient consumer spending"],
               "risks": ["Inventory markdowns", "Rising logistics costs"]},
}

QUOTES = {
    "CTSO": {"price_usd": 84.12, "change_pct": -0.6, "market_cap_usd": 12_400_000_000, "pe_ratio": 17.3},
    "FBHL": {"price_usd": 152.40, "change_pct": 1.2, "market_cap_usd": 31_900_000_000, "pe_ratio": 24.8},
    "NWND": {"price_usd": 41.75, "change_pct": -2.1, "market_cap_usd": 5_600_000_000, "pe_ratio": 11.2},
    "ADVW": {"price_usd": 23.30, "change_pct": 0.4, "market_cap_usd": 2_100_000_000, "pe_ratio": 14.9},
}


class ToolError(ValueError):
    pass


def get_customer(customer_id: str | None = None, name: str | None = None) -> dict:
    if customer_id:
        record = CUSTOMERS.get(customer_id.strip().upper())
        if not record:
            raise ToolError(f"Customer '{customer_id}' not found")
        return {"customer_id": customer_id.strip().upper(), **record}
    if name:
        needle = name.strip().lower()
        for cid, rec in CUSTOMERS.items():
            if needle in rec["name"].lower():
                return {"customer_id": cid, **rec}
        raise ToolError(f"No customer matching name '{name}'")
    raise ToolError("Provide customer_id or name")


def get_market_conditions(industry: str) -> dict:
    key = industry.strip().lower()
    data = MARKETS.get(key)
    if not data:
        raise ToolError(f"Unsupported industry '{industry}'. Supported: {', '.join(sorted(MARKETS))}")
    return {"industry": key, "as_of": "2026-10-01", **data}


def get_stock_quote(customer_id: str | None = None, ticker: str | None = None) -> dict:
    if customer_id:
        cust = get_customer(customer_id=customer_id)
        ticker = cust["ticker"]
        if not ticker:
            raise ToolError(f"Customer {cust['customer_id']} ({cust['name']}) is privately held; no stock quote available")
    if not ticker:
        raise ToolError("Provide customer_id or ticker")
    quote = QUOTES.get(ticker.strip().upper())
    if not quote:
        raise ToolError(f"Unknown ticker '{ticker}'")
    return {"ticker": ticker.strip().upper(), "as_of": "2026-10-13T16:00:00Z", **quote}
