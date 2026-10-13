import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sample-mcp-server"))
import data  # noqa: E402


class SampleToolTests(unittest.TestCase):
    def test_customer_by_id_and_name(self):
        self.assertEqual(data.get_customer(customer_id="c1001")["name"], "Contoso Manufacturing")
        self.assertEqual(data.get_customer(name="fabrikam")["customer_id"], "C1002")

    def test_customer_errors(self):
        with self.assertRaises(data.ToolError):
            data.get_customer(customer_id="NOPE")
        with self.assertRaises(data.ToolError):
            data.get_customer()

    def test_market_conditions(self):
        self.assertEqual(data.get_market_conditions("Energy")["outlook"], "negative")
        with self.assertRaises(data.ToolError):
            data.get_market_conditions("aerospace")

    def test_every_customer_industry_has_market_data(self):
        for rec in data.CUSTOMERS.values():
            self.assertIn(rec["industry"], data.MARKETS)

    def test_stock_quote(self):
        self.assertEqual(data.get_stock_quote(customer_id="C1002")["ticker"], "FBHL")
        self.assertEqual(data.get_stock_quote(ticker="ctso")["market_cap_usd"], 12_400_000_000)
        with self.assertRaises(data.ToolError):
            data.get_stock_quote(customer_id="C1005")  # private company


if __name__ == "__main__":
    unittest.main()
