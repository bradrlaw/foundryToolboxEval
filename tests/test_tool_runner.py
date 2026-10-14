import argparse
import asyncio
import contextlib
import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx2
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR, INVALID_PARAMS, REQUEST_TIMEOUT

from tooltests import run

TARGET = run.Target("https://toolbox.example/mcp")


def case(case_id="c1", **kw):
    return {"case_id": case_id, "category": "x", "tool": "t", "arguments": {}, **kw}


class FakeToolbox:
    def __init__(self, outcomes=(), tools=()):
        self.outcomes, self.tools, self.target = list(outcomes), list(tools), TARGET

    async def call(self, tool, arguments):
        return self.outcomes.pop(0)

    async def list_tools(self):
        return run.Outcome("ok", data=self.tools, latency_ms=10)

    def describe(self):
        return {"protocol_version": "2025-11-25", "server": None, "tools": [t.name for t in self.tools]}


def tool(name, description="d", input_schema=None, output_schema=None):
    return SimpleNamespace(name=name, description=description,
                           input_schema=input_schema or {"type": "object"}, output_schema=output_schema)


class NoTokenTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(run.auth, "get_token", return_value="token")
        patcher.start()
        self.addCleanup(patcher.stop)


class ClassificationTests(NoTokenTestCase):
    def classify(self, error, http=(), earlier=()):
        errors = run.HttpErrors()
        errors.seen += earlier
        tb = run.Toolbox(client=None, http_errors=errors, target=TARGET)

        async def request():
            errors.seen.extend(http)
            raise error
        return asyncio.run(tb.timed(request))

    def test_jsonrpc_errors_are_protocol_errors(self):
        o = self.classify(MCPError(INVALID_PARAMS, "No tool config matches tool name 'nope'"))
        self.assertEqual((o.outcome, o.text), ("protocol_error", "No tool config matches tool name 'nope'"))
        o = self.classify(MCPError(INVALID_PARAMS, "bad"), http=[run.HttpError(400, "{}", jsonrpc=True)])
        self.assertEqual(o.outcome, "protocol_error")

    def test_http_failures_timeouts_and_lost_connections_are_transport_errors(self):
        o = self.classify(MCPError(INTERNAL_ERROR, "Server returned an error response"),
                          http=[run.HttpError(503, "busy", jsonrpc=False)])
        self.assertEqual((o.outcome, o.text), ("transport_error", "HTTP 503: busy"))
        for code in (REQUEST_TIMEOUT, CONNECTION_CLOSED):
            self.assertEqual(self.classify(MCPError(code, "x")).outcome, "transport_error")

    def test_http_errors_from_earlier_requests_are_ignored(self):
        o = self.classify(MCPError(INVALID_PARAMS, "bad"), earlier=[run.HttpError(503, "old", jsonrpc=False)])
        self.assertEqual(o.outcome, "protocol_error")

    def test_output_schema_violations_are_invalid_results(self):
        self.assertEqual(self.classify(RuntimeError("Invalid structured content")).outcome, "invalid_result")


class HttpErrorHookTests(unittest.TestCase):
    def record(self, status, method="POST", **body):
        errors = run.HttpErrors()
        response = httpx2.Response(status, request=httpx2.Request(method, "https://x"), **body)
        asyncio.run(errors.hook(response))
        return errors.seen

    def test_records_post_errors_and_spots_jsonrpc_bodies(self):
        [azure] = self.record(401, json={"error": {"code": "401", "message": "Access denied"}})
        self.assertFalse(azure.jsonrpc)
        self.assertIn("Access denied", str(azure))
        [rpc] = self.record(400, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}})
        self.assertTrue(rpc.jsonrpc)

    def test_ignores_successes_and_other_methods(self):
        self.assertEqual(self.record(200, text="ok"), [])
        self.assertEqual(self.record(405, method="GET", text="no"), [])


class CallCaseTests(unittest.TestCase):
    def run_call(self, c, *outcomes):
        return asyncio.run(run.run_call(FakeToolbox(outcomes), c))

    def test_expected_protocol_error_passes_with_assertions(self):
        c = case(expect_error="protocol", assertions=[{"type": "contains", "value": "No tool config"}])
        r = self.run_call(c, run.Outcome("protocol_error", "No tool config matches", latency_ms=5))
        self.assertTrue(r["passed"], r["failures"])
        self.assertEqual(r["outcomes"], {"protocol_error": 1})

    def test_assertions_are_skipped_when_the_outcome_is_wrong(self):
        c = case(assertions=[{"type": "contains", "value": "zzz"}])
        r = self.run_call(c, run.Outcome("tool_error", "Customer 'BAD' not found", latency_ms=5))
        self.assertEqual(r["failures"], ["expected ok, got tool_error: Customer 'BAD' not found"])

    def test_repetitions_count_failures_and_latency(self):
        c = case(repetitions=3, max_latency_ms=500, assertions=[{"type": "json_path_exists", "path": "$.a"}])
        r = self.run_call(c, run.Outcome("ok", "{}", {}, 10), run.Outcome("ok", '{"a":1}', {"a": 1}, 600),
                          run.Outcome("transport_error", "HTTP 503: busy", latency_ms=700))
        self.assertEqual(r["failures"], [
            "path $.a not found (1/3 runs)",
            "expected ok, got transport_error: HTTP 503: busy (1/3 runs)",
            "latency > 500ms in 1/3 runs (max 600ms)",
        ])
        self.assertEqual((r["latency_p50_ms"], r["latency_p95_ms"]), (600, 700))
        self.assertEqual(r["outcomes"], {"ok": 2, "transport_error": 1})


class ListCaseTests(unittest.TestCase):
    def test_list_checks(self):
        tools = [tool("a"), tool("a"), tool("b", description=" ", input_schema={"type": "string"},
                                            output_schema={"type": "nope"})]
        c = {"case_id": "l", "category": "x", "kind": "list", "expect_tools": ["a", "c"], "require_schemas": True}
        r = asyncio.run(run.run_list(FakeToolbox(tools=tools), c))
        self.assertEqual(r["sample_output"], "a, b")
        self.assertEqual(r["failures"][:2], ["tool 'a' listed 2 times", "tool 'c' not listed"])
        self.assertIn("b: empty description", r["failures"])
        self.assertIn("b: input schema is not an object schema", r["failures"])
        self.assertTrue(any(f.startswith("b: invalid output schema") for f in r["failures"]))


class RunAllTests(NoTokenTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(run, "RECONNECT_BACKOFF_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connects = 0

    def run_all(self, cases, run_case, connect_failures=()):
        @contextlib.asynccontextmanager
        async def fake_connect(target, http_errors):
            self.connects += 1
            if self.connects in connect_failures:
                http_errors.seen.append(run.HttpError(401, "denied", jsonrpc=False))
                raise ExceptionGroup("tg", [MCPError(INTERNAL_ERROR, "Server returned an error response")])
            yield FakeToolbox()

        with mock.patch.object(run, "connect", fake_connect), mock.patch.object(run, "run_case", run_case), \
                contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(run.run_all(cases, TARGET))

    def test_lost_session_fails_the_in_flight_case_and_reconnects(self):
        seen = []

        async def run_case(tb, c):
            seen.append(c["case_id"])
            if seen == ["a", "b"]:
                raise ExceptionGroup("tg", [httpx2.ConnectError("boom")])
            return {"case_id": c["case_id"], "passed": True}

        results, info = self.run_all([case("a"), case("b"), case("c")], run_case)
        self.assertEqual([r["case_id"] for r in results], ["a", "b", "c"])
        self.assertEqual(results[1]["failures"], ["session lost: ConnectError: boom"])
        self.assertEqual(results[1]["outcomes"], {"transport_error": 1})
        self.assertEqual((self.connects, info["protocol_version"]), (2, "2025-11-25"))

    def test_gives_up_after_max_reconnects(self):
        async def run_case(tb, c):
            raise ExceptionGroup("tg", [httpx2.ReadError("reset")])

        results, _ = self.run_all([case(str(i)) for i in range(6)], run_case)
        lost = [r for r in results if r["failures"][0].startswith("session lost")]
        not_run = [r for r in results if r["failures"][0].startswith("not run")]
        self.assertEqual((len(lost), len(not_run)), (run.MAX_RECONNECTS + 1, 6 - run.MAX_RECONNECTS - 1))

    def test_failed_reconnect_is_retried(self):
        seen = []

        async def run_case(tb, c):
            seen.append(c["case_id"])
            if seen == ["a"]:
                raise ExceptionGroup("tg", [httpx2.ConnectError("boom")])
            return {"case_id": c["case_id"], "passed": True}

        results, _ = self.run_all([case("a"), case("b")], run_case, connect_failures={2})
        self.assertEqual([r["case_id"] for r in results], ["a", "b"])
        self.assertTrue(results[1]["passed"])
        self.assertEqual(self.connects, 3)

    def test_first_connect_failure_is_a_setup_error_with_the_http_reason(self):
        async def run_case(tb, c):
            raise AssertionError("not reached")

        with self.assertRaises(run.SetupError) as ctx:
            self.run_all([case()], run_case, connect_failures={1})
        self.assertIn("HTTP 401: denied", str(ctx.exception))


class ExplainTests(unittest.TestCase):
    def test_flattens_nested_groups(self):
        error = ExceptionGroup("a", [ExceptionGroup("b", [httpx2.ConnectError("dns")]), ValueError("x")])
        self.assertEqual(run.explain(error, run.HttpErrors()), "ConnectError: dns; ValueError: x")


class ResolveTargetTests(unittest.TestCase):
    def resolve(self, env=None, **kw):
        args = argparse.Namespace(**{"endpoint": None, "config": None, "toolbox": None, "timeout": 60, **kw})
        with mock.patch.dict(os.environ, env or {}, clear=True):
            return run.resolve_target(args)

    def test_precedence(self):
        env = {"FOUNDRY_PROJECT_ENDPOINT": "https://p/api/projects/x/", "FOUNDRY_TOOLBOX_NAME": "tbx"}
        self.assertEqual(self.resolve(env).endpoint, "https://p/api/projects/x/toolboxes/tbx/mcp?api-version=v1")
        self.assertIn("/toolboxes/other/", self.resolve(env, toolbox="other").endpoint)
        self.assertIn(f"/toolboxes/{run.DEFAULT_TOOLBOX}/", self.resolve({"FOUNDRY_PROJECT_ENDPOINT": "https://p"}).endpoint)
        self.assertEqual(self.resolve(env, endpoint="https://e/mcp").endpoint, "https://e/mcp")

    def test_config_file(self):
        with mock.patch.object(Path, "read_text", return_value='{"toolbox": {"mcp_endpoint": "https://c/mcp", "token_scope": "s"}}'):
            target = self.resolve(config=Path("x.json"))
        self.assertEqual((target.endpoint, target.scope), ("https://c/mcp", "s"))
        with mock.patch.object(Path, "read_text", return_value='{"agent": {}}'), self.assertRaises(run.SetupError):
            self.resolve(config=Path("x.json"))

    def test_missing_endpoint(self):
        with self.assertRaises(run.SetupError):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
