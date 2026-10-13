import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tooltests.run import check, json_path, split_output  # noqa: E402


class AssertionTests(unittest.TestCase):
    def test_split_output_strips_structured_response(self):
        raw, parsed = split_output('{"a": 1}\n{\n  "structuredResponse": {"x": 1}\n}')
        self.assertEqual(raw, '{"a": 1}')
        self.assertEqual(parsed, {"a": 1})

    def test_split_output_non_json(self):
        raw, parsed = split_output("Error executing tool: nope")
        self.assertIsNone(parsed)
        self.assertIn("nope", raw)

    def test_json_path(self):
        self.assertEqual(json_path({"a": [{"b": 5}]}, "$.a[0].b"), 5)

    def test_contains_is_case_insensitive(self):
        self.assertIsNone(check({"type": "contains", "value": "NOT FOUND"}, "customer not found", None))
        self.assertIsNotNone(check({"type": "contains", "value": "x"}, "abc", None))

    def test_regex(self):
        self.assertIsNone(check({"type": "regex", "pattern": r"^\d+$"}, "123", None))
        self.assertIsNotNone(check({"type": "regex", "pattern": r"^\d+$"}, "12a", None))

    def test_json_path_equals_and_exists(self):
        doc = {"ticker": "CTSO", "n": 1}
        self.assertIsNone(check({"type": "json_path_equals", "path": "$.ticker", "value": "CTSO"}, "", doc))
        self.assertIn("expected", check({"type": "json_path_equals", "path": "$.ticker", "value": "X"}, "", doc))
        self.assertIsNone(check({"type": "json_path_exists", "path": "$.n"}, "", doc))
        self.assertIn("not found", check({"type": "json_path_exists", "path": "$.missing"}, "", doc))
        self.assertEqual(check({"type": "json_path_exists", "path": "$.n"}, "", None), "output is not JSON")

    def test_schema(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "number"}}}
        self.assertIsNone(check({"type": "schema", "schema": schema}, "", {"a": 1.5}))
        self.assertIn("missing", check({"type": "schema", "schema": schema}, "", {}))
        self.assertIn("expected", check({"type": "schema", "schema": schema}, "", {"a": "x"}))


if __name__ == "__main__":
    unittest.main()
