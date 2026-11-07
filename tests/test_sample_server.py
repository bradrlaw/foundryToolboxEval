"""In-memory MCP round trips against the sample server (no network)."""
import asyncio
import json
import logging
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sample-mcp-server"))
_root = logging.getLogger()
_saved = (_root.level, list(_root.handlers))
import data  # noqa: E402
import server  # noqa: E402  (MCPServer() calls logging.basicConfig at INFO; undone below)
_root.setLevel(_saved[0])
_root.handlers[:] = _saved[1]


async def _session(fn):
    async with Client(server.mcp, mode="legacy") as client:
        return await fn(client)


def call(tool: str, args: dict) -> tuple[bool, str]:
    result = asyncio.run(_session(lambda c: c.call_tool(tool, args)))
    return result.is_error, "\n".join(c.text for c in result.content if c.type == "text")


class SampleServerTests(unittest.TestCase):
    def test_lists_the_three_tools(self):
        result = asyncio.run(_session(lambda c: c.list_tools()))
        self.assertEqual(sorted(t.name for t in result.tools),
                         ["get_customer", "get_market_conditions", "get_stock_quote"])

    def test_success_returns_json_text(self):
        is_error, text = call("get_customer", {"customer_id": "C1001"})
        self.assertFalse(is_error)
        self.assertEqual(json.loads(text)["name"], "Contoso Manufacturing")

    def test_every_record_matches_its_output_schema_and_text(self):
        # Each schema requires exactly the fields its tool returns, and the text a model reads is the same JSON.
        calls = ([("get_customer", {"customer_id": cid}) for cid in data.CUSTOMERS]
                 + [("get_market_conditions", {"industry": industry}) for industry in data.MARKETS]
                 + [("get_stock_quote", {"ticker": ticker}) for ticker in data.QUOTES])

        async def go(client):
            schemas = {t.name: t.output_schema for t in (await client.list_tools()).tools}
            return schemas, [await client.call_tool(tool, args) for tool, args in calls]

        schemas, results = asyncio.run(_session(go))
        for (tool, args), result in zip(calls, results):
            with self.subTest(tool=tool, args=args):
                self.assertFalse(result.is_error)
                self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                Draft202012Validator(schemas[tool]).validate(result.structured_content)
                self.assertEqual(set(schemas[tool]["required"]), set(result.structured_content))

    def test_tool_errors_keep_their_message(self):
        # The tool-test cases in data/tool-tests/sample-toolbox.jsonl assert on these messages.
        for tool, args, expected in [
            ("get_customer", {"customer_id": "BAD"}, "not found"),
            ("get_customer", {"name": "o"}, "matches more than one customer"),
            ("get_customer", {"name": " "}, "Provide customer_id or name"),
            ("get_stock_quote", {"customer_id": "C1005"}, "privately held"),
            ("get_market_conditions", {"industry": "aerospace"}, "Unsupported industry"),
            ("get_market_conditions", {}, "industry"),
        ]:
            with self.subTest(tool=tool, args=args):
                is_error, text = call(tool, args)
                self.assertTrue(is_error)
                self.assertTrue(text.startswith(f"Error executing tool {tool}: "), text)
                self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
