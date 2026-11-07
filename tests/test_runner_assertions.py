import unittest

from tooltests.run import check, content_mismatch, json_equal, json_path, parse_output, split_output


class OutputTests(unittest.TestCase):
    def test_split_output_strips_structured_response(self):
        raw, parsed = split_output('{"a": 1}\n{\n  "structuredResponse": {"x": 1}\n}')
        self.assertEqual(raw, '{"a": 1}')
        self.assertEqual(parsed, {"a": 1})

    def test_split_output_non_json(self):
        raw, parsed = split_output("Error executing tool: nope")
        self.assertIsNone(parsed)
        self.assertIn("nope", raw)

    def test_parse_output_prefers_json_text_then_structured_content(self):
        self.assertEqual(parse_output('{"a": 1}', {"result": '{"a": 1}'}), {"a": 1})
        self.assertEqual(parse_output("summary for the model", {"a": 2}), {"a": 2})
        self.assertIsNone(parse_output("plain text"))

    def test_content_mismatch_compares_json_text_with_structured_content(self):
        self.assertIsNone(content_mismatch('{"b": 1, "a": [2.0]}\n{\n  "structuredResponse": {}\n}',
                                           {"a": [2], "b": 1.0}))
        self.assertEqual(content_mismatch('{"a": 1, "b": 2, "c": 3}', {"a": 1, "b": 3, "d": 4}),
                         "text content and structured content differ in b, c, d")
        self.assertEqual(content_mismatch('{"a": true}', {"a": 1}), "text content and structured content differ in a")
        self.assertEqual(content_mismatch("[1]", {"a": 1}), "text content and structured content differ")

    def test_content_mismatch_skips_what_it_cannot_compare(self):
        self.assertIsNone(content_mismatch("summary for the model", {"a": 1}))
        self.assertIsNone(content_mismatch('{"a": 1}', None))
        # The SDK's {"result": ...} wrapper: a str result, a number, and a list sent as one text block per item.
        self.assertIsNone(content_mismatch('{"a": 1}', {"result": '{"a": 1}'}))
        self.assertIsNone(content_mismatch("5", {"result": 5}))
        self.assertIsNone(content_mismatch('{"a": 1}', {"result": [{"a": 1}]}))


class JsonPathTests(unittest.TestCase):
    def test_paths(self):
        doc = {"a": [{"b": 5}]}
        self.assertEqual(json_path(doc, "$.a[0].b"), 5)
        self.assertEqual(json_path(doc, "$"), doc)

    def test_paths_are_strict(self):
        with self.assertRaises(TypeError):
            json_path({"a": "xyz"}, "$.a[0]")
        with self.assertRaises(TypeError):
            json_path([1], "$.a")
        for bad in ("a", "$..a", "$.a[x]", "$a"):
            with self.assertRaises(KeyError, msg=bad):
                json_path({"a": 1}, bad)

    def test_json_equality(self):
        self.assertTrue(json_equal({"a": [1, False]}, {"a": [1.0, False]}))
        self.assertFalse(json_equal(True, 1))
        self.assertFalse(json_equal({"a": [0]}, {"a": [False]}))


class AssertionTests(unittest.TestCase):
    def test_contains_is_case_insensitive(self):
        self.assertIsNone(check({"type": "contains", "value": "NOT FOUND"}, "customer not found", None))
        self.assertIsNotNone(check({"type": "contains", "value": "x"}, "abc", None))

    def test_regex(self):
        self.assertIsNone(check({"type": "regex", "pattern": r"^\d+$"}, "123", None))
        self.assertIsNotNone(check({"type": "regex", "pattern": r"^\d+$"}, "12a", None))

    def test_json_path_equals_and_exists(self):
        doc = {"ticker": "CTSO", "n": 1, "flag": True}
        self.assertIsNone(check({"type": "json_path_equals", "path": "$.ticker", "value": "CTSO"}, "", doc))
        self.assertIn("expected", check({"type": "json_path_equals", "path": "$.ticker", "value": "X"}, "", doc))
        self.assertIsNotNone(check({"type": "json_path_equals", "path": "$.flag", "value": 1}, "", doc))
        self.assertIsNone(check({"type": "json_path_exists", "path": "$.n"}, "", doc))
        self.assertIn("not found", check({"type": "json_path_exists", "path": "$.missing"}, "", doc))
        self.assertEqual(check({"type": "json_path_exists", "path": "$.n"}, "", None), "output is not JSON")

    def test_schema(self):
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "number"}}}
        self.assertIsNone(check({"type": "schema", "schema": schema}, "", {"a": 1.5}))
        self.assertIn("'a' is a required property", check({"type": "schema", "schema": schema}, "", {}))
        self.assertIn("$.a: 'x' is not of type 'number'", check({"type": "schema", "schema": schema}, "", {"a": "x"}))

    def test_schema_formats_are_checked(self):
        schema = {"type": "object", "properties": {"as_of": {"type": "string", "format": "date"}}}
        self.assertIsNone(check({"type": "schema", "schema": schema}, "", {"as_of": "2026-10-01"}))
        self.assertIn("is not a 'date'", check({"type": "schema", "schema": schema}, "", {"as_of": "soon"}))

    def test_malformed_assertions_fail_instead_of_raising(self):
        self.assertIn("assertion error", check({"type": "regex", "pattern": "("}, "x", None))
        self.assertIn("unsupported", check({"type": "nope"}, "x", None))


if __name__ == "__main__":
    unittest.main()
