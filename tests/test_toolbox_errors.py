import contextlib
import io
import json
import unittest
from unittest import mock

from evals import toolbox_errors as te

LINK = "https://logic-region-001.consent.azure-apihub.net/login?data=abc_DEF-123"


def list_failure(*sources):
    errors = json.dumps({"errors": list(sources)}, separators=(",", ":"))
    return f"tools/list failed for {len(sources)} tool source(s), succeeded for 0 tool source(s) {errors}"


def source(name, code, message):
    return {"name": name, "type": "mcp", "error": {"code": code, "message": message}}


class SourceErrorsTests(unittest.TestCase):
    def test_parses_the_embedded_list_despite_trailing_text(self):
        text = list_failure(source("sample", "HTTP_401", "denied")) + ". Troubleshooting guide: https://learn.example"
        self.assertEqual(te.source_errors(text), [te.SourceError("sample", "HTTP_401", "denied")])

    def test_missing_or_malformed_list(self):
        self.assertEqual(te.source_errors("No tool config matches tool name 'x'"), [])
        self.assertEqual(te.source_errors('failed {"errors": [{"name": "s"'), [])


class ClassifyTests(unittest.TestCase):
    def test_list_consent_keeps_the_link_out_of_the_detail(self):
        f = te.classify(list_failure(source("sample", "CONSENT_REQUIRED", LINK)), code=-32006)
        self.assertEqual((f.outcome, f.detail, f.consent),
                         ("consent_required", "tool source 'sample': user consent required", (("sample", LINK),)))

    def test_list_upstream_failure(self):
        f = te.classify(list_failure(source("sample", "HTTP_401", "Authentication failed")), code=-32006)
        self.assertEqual((f.outcome, f.detail, f.consent),
                         ("upstream_error", "tool source 'sample': HTTP_401: Authentication failed", ()))

    def test_consent_wins_over_other_source_failures(self):
        f = te.classify(list_failure(source("a", "HTTP_500", "boom"), source("b", "CONSENT_REQUIRED", LINK)))
        self.assertEqual(f.outcome, "consent_required")
        self.assertEqual(f.detail, "tool source 'a': HTTP_500: boom; tool source 'b': user consent required")
        self.assertEqual(f.consent, (("b", LINK),))

    def test_call_consent(self):
        f = te.classify(LINK, {"grpcStatus": "FailedPrecondition", "detail": LINK}, -32006)
        self.assertEqual((f.outcome, f.consent), ("consent_required", (("", LINK),)))
        self.assertNotIn(LINK, f.detail)
        # A bare link is never recorded, whatever the code.
        self.assertEqual(te.classify(LINK, None, -32000).outcome, "consent_required")

    def test_an_unparsed_consent_list_is_never_echoed(self):
        f = te.classify('tools/list failed {"errors":[{"error":{"code":"CONSENT_REQUIRED","message":"%s"' % LINK)
        self.assertEqual((f.outcome, f.detail, f.consent), ("consent_required", te.CONSENT_DETAIL, (("", LINK),)))

    def test_call_upstream_failures(self):
        f = te.classify("Authentication failed when connecting to the MCP server: https://x/mcp: 401",
                        {"grpcStatus": "Unauthenticated", "detail": "x"}, -32002)
        self.assertEqual((f.outcome, f.consent), ("upstream_error", ()))
        self.assertEqual(te.classify("Connection resolution failed for 'conn'", None, -32602).outcome, "upstream_error")
        self.assertEqual(te.classify("boom", {"grpcStatus": "Unavailable"}).outcome, "upstream_error")

    def test_other_errors_are_not_tool_source_failures(self):
        self.assertIsNone(te.classify("No tool config matches tool name 'nope'", None, -32602))
        self.assertIsNone(te.classify("Invalid arguments", {"grpcStatus": "InvalidArgument"}))


class AnnounceTests(unittest.TestCase):
    def test_prints_once_per_tool_source_to_stderr(self):
        err = io.StringIO()
        with mock.patch.object(te, "_announced", set()), contextlib.redirect_stderr(err):
            te.announce_consent([("sample", LINK)], "ada@example.com")
            te.announce_consent([("sample", LINK + "2"), ("other", "")])
        self.assertEqual(err.getvalue().count(LINK), 1)
        self.assertIn("Tool source 'sample'", err.getvalue())
        self.assertIn("signed in as ada@example.com", err.getvalue())


if __name__ == "__main__":
    unittest.main()
