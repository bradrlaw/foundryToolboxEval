import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR, INVALID_PARAMS, REQUEST_TIMEOUT

from evals import telemetry
from tooltests import run

TARGET = run.Target("https://toolbox.example/mcp")
LINK = "https://logic-region-001.consent.azure-apihub.net/login?data=abc"
CONSENT_LIST_ERROR = ('tools/list failed for 1 tool source(s), succeeded for 0 tool source(s) {"errors":[{"name":"sample",'
                      '"type":"mcp","error":{"code":"CONSENT_REQUIRED","message":"%s"}}]}' % LINK)


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


class PingClient:
    def __init__(self, ping_error=None):
        self.ping_error = ping_error

    async def send_ping(self):
        if self.ping_error:
            raise self.ping_error


class ClassificationTests(NoTokenTestCase):
    def classify(self, error, http=(), earlier=(), ping_error=None):
        errors = run.HttpErrors()
        errors.seen += earlier
        tb = run.Toolbox(client=PingClient(ping_error), http_errors=errors, target=TARGET)

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

    def test_connection_closed_is_a_lost_session_only_if_a_ping_fails_the_same_way(self):
        # The SDK raises CONNECTION_CLOSED when one request's response stream ends, and for every request once
        # the session's own stream has ended.
        closed = MCPError(CONNECTION_CLOSED, "Connection closed")
        self.assertEqual(self.classify(closed, ping_error=MCPError(-32601, "no ping")).outcome, "transport_error")
        with self.assertRaises(run.SessionLost) as ctx:
            self.classify(closed, ping_error=closed)
        self.assertEqual(str(ctx.exception), f"MCP error {CONNECTION_CLOSED}: Connection closed")

    def test_http_errors_from_earlier_requests_are_ignored(self):
        o = self.classify(MCPError(INVALID_PARAMS, "bad"), earlier=[run.HttpError(503, "old", jsonrpc=False)])
        self.assertEqual(o.outcome, "protocol_error")

    def test_output_schema_violations_are_invalid_results(self):
        self.assertEqual(self.classify(RuntimeError("Invalid structured content")).outcome, "invalid_result")

    def test_text_that_disagrees_with_structured_content_is_an_invalid_result(self):
        class Client:
            async def call_tool(self, tool, arguments):
                return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"price": 1}')],
                                       structured_content={"price": 2}, is_error=False)

        o = asyncio.run(run.Toolbox(Client(), run.HttpErrors(), TARGET).call("t", {}))
        self.assertEqual(o.outcome, "invalid_result")
        self.assertEqual(o.text, 'text content and structured content differ in price: {"price": 1}')

    def test_tool_source_failures(self):
        err = io.StringIO()
        with mock.patch.object(run.toolbox_errors, "_announced", set()), contextlib.redirect_stderr(err), \
                mock.patch.object(run.auth, "caller_claims", return_value={"username": "ada@example.com"}):
            o = self.classify(MCPError(-32006, LINK, {"grpcStatus": "FailedPrecondition", "detail": LINK}))
        self.assertEqual(o.outcome, "consent_required")
        self.assertNotIn(LINK, o.text)
        self.assertIn(f"signed in as ada@example.com, approve, then rerun:\n  {LINK}", err.getvalue())
        o = self.classify(MCPError(-32002, "Authentication failed when connecting to the MCP server: x",
                                   {"grpcStatus": "Unauthenticated"}))
        self.assertEqual(o.outcome, "upstream_error")


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
        self.assertEqual((r["outcomes"], r["reasons"]), ({"protocol_error": 1}, []))

    def test_assertions_are_skipped_when_the_outcome_is_wrong(self):
        c = case(assertions=[{"type": "contains", "value": "zzz"}])
        r = self.run_call(c, run.Outcome("tool_error", "Customer 'BAD' not found", latency_ms=5))
        self.assertEqual(r["failures"], ["expected ok, got tool_error: Customer 'BAD' not found"])
        self.assertEqual(r["reasons"], ["tool_error"])

    def test_success_when_an_error_is_expected(self):
        r = self.run_call(case(expect_error="tool"), run.Outcome("ok", "{}", {}, 5))
        self.assertEqual((r["failures"], r["reasons"]), (["expected tool_error, got ok: {}"], ["unexpected_success"]))

    def test_repetitions_count_failures_and_latency(self):
        c = case(repetitions=3, max_latency_ms=500, assertions=[{"type": "json_path_exists", "path": "$.a"}])
        r = self.run_call(c, run.Outcome("ok", "{}", {}, 10), run.Outcome("ok", '{"a":1}', {"a": 1}, 600),
                          run.Outcome("transport_error", "HTTP 503: busy", latency_ms=700))
        self.assertEqual(r["failures"], [
            "path $.a not found (1/3 runs)",
            "expected ok, got transport_error: HTTP 503: busy (1/3 runs)",
            "latency > 500ms in 1/3 runs (max 600ms)",
        ])
        self.assertEqual(r["reasons"], ["assertion", "transport_error", "latency"])
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
        self.assertEqual(r["reasons"], ["duplicate_tool", "missing_tool"] + ["tool_definition"] * 3)

    def test_list_can_require_output_schemas(self):
        tools = [tool("a", output_schema={"type": "object"}), tool("b")]
        c = {"case_id": "l", "category": "x", "kind": "list", "expect_tools": ["a", "b"], "require_output_schemas": True}
        r = asyncio.run(run.run_list(FakeToolbox(tools=tools), c))
        self.assertEqual((r["failures"], r["reasons"]), (["b: no output schema"], ["tool_definition"]))


class RunAllTests(NoTokenTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(run, "RECONNECT_BACKOFF_S", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connects = 0

    def run_all(self, cases, run_case, connect_failures=(), connect_error=None, toolbox=None):
        @contextlib.asynccontextmanager
        async def fake_connect(target, http_errors):
            self.connects += 1
            if self.connects in connect_failures:
                if connect_error:
                    raise connect_error
                http_errors.seen.append(run.HttpError(401, "denied", jsonrpc=False))
                raise ExceptionGroup("tg", [MCPError(INTERNAL_ERROR, "Server returned an error response")])
            yield toolbox(http_errors) if toolbox else FakeToolbox()

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
        self.assertEqual(results[1]["reasons"], ["transport_error"])
        self.assertEqual(results[1]["outcomes"], {"transport_error": 1})
        self.assertEqual((self.connects, info["protocol_version"]), (2, "2025-11-25"))

    def test_closed_session_reconnects_and_runs_the_remaining_cases(self):
        closed = MCPError(CONNECTION_CLOSED, "Connection closed")

        class Client(PingClient):  # the first session closes after one call, as the SDK does at stream EOF
            protocol_version, server_info = "2025-11-25", None

            def __init__(self, calls_before_close):
                super().__init__()
                self.calls_left = calls_before_close

            async def call_tool(self, name, arguments):
                if self.calls_left == 0:
                    self.ping_error = closed
                    raise closed
                self.calls_left -= 1
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")],
                                       structured_content=None, is_error=False)

        def toolbox(http_errors):
            return run.Toolbox(Client(1 if self.connects == 1 else 99), http_errors, TARGET)

        results, _ = self.run_all([case("a"), case("b"), case("c")], run.run_case, toolbox=toolbox)
        self.assertEqual([(r["case_id"], r["passed"]) for r in results], [("a", True), ("b", False), ("c", True)])
        self.assertEqual(results[1]["failures"], [f"session lost: MCP error {CONNECTION_CLOSED}: Connection closed"])
        self.assertEqual((results[1]["reasons"], self.connects), (["transport_error"], 2))

    def test_gives_up_after_max_reconnects(self):
        async def run_case(tb, c):
            raise ExceptionGroup("tg", [httpx2.ReadError("reset")])

        results, _ = self.run_all([case(str(i)) for i in range(6)], run_case)
        lost = [r for r in results if r["failures"][0].startswith("session lost")]
        not_run = [r for r in results if r["failures"][0].startswith("not run")]
        self.assertEqual((len(lost), len(not_run)), (run.MAX_RECONNECTS + 1, 6 - run.MAX_RECONNECTS - 1))
        self.assertEqual({r["reasons"][0] for r in not_run}, {"not_run"})

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

    def test_consent_needed_at_connect_is_a_setup_error_that_carries_the_link_separately(self):
        async def run_case(tb, c):
            raise AssertionError("not reached")

        error = ExceptionGroup("tg", [MCPError(-32006, CONSENT_LIST_ERROR)])
        with self.assertRaises(run.SetupError) as ctx:
            self.run_all([case()], run_case, connect_failures={1}, connect_error=error)
        self.assertTrue(str(ctx.exception).endswith(": tool source 'sample': user consent required"))
        self.assertNotIn(LINK, str(ctx.exception))
        self.assertEqual(ctx.exception.consent, [("sample", LINK)])


class MainTests(NoTokenTestCase):
    def main(self, cases, run_all, claims=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.jsonl"
            path.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(run, "load_dotenv"), mock.patch.object(run.telemetry, "init"), \
                    mock.patch.object(run, "run_all", run_all), \
                    mock.patch.object(run.auth, "caller_claims", return_value=claims or {}), \
                    mock.patch.object(run.toolbox_errors, "_announced", set()), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = run.main([str(path), "--endpoint", TARGET.endpoint, "--out", tmp])
            self.saved = [json.loads(p.read_text(encoding="utf-8")) for p in Path(tmp).glob("run-*.json")]
        return code, out.getvalue(), err.getvalue()

    def test_the_run_records_what_it_ran_against(self):
        async def run_all(cases, target):
            return [], {}

        toolbox = {"name": "tb", "version": "3"}
        with mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": "https://project.example"}), \
                mock.patch.object(run.provenance, "repo", return_value={"commit": "abc", "dirty": True}), \
                mock.patch.object(run.provenance, "toolbox", return_value=toolbox) as lookup:
            self.main([case()], run_all)
        lookup.assert_called_once_with(TARGET.endpoint, "https://project.example")
        self.assertEqual(self.saved[0]["provenance"], {"repo": {"commit": "abc", "dirty": True}, "toolboxes": [toolbox]})

    def test_consent_needed_at_connect_exits_2_and_prints_the_link_after_the_error(self):
        async def run_all(cases, target):
            raise run.SetupError("cannot connect: tool source 'sample': user consent required", [("sample", LINK)])

        code, _, err = self.main([case()], run_all)
        self.assertEqual(code, 2)
        self.assertLess(err.index("error: cannot connect"), err.index(LINK))

    def test_caller_placeholders_are_filled_before_the_run(self):
        seen = []

        async def run_all(cases, target):
            seen.extend(cases)
            return [], {}

        assertion = {"type": "json_path_equals", "path": "$.oid", "value": "{{caller.oid}}"}
        code, _, _ = self.main([case(assertions=[assertion])], run_all, claims={"oid": "o-1"})
        self.assertEqual((code, seen[0]["assertions"][0]["value"]), (0, "o-1"))

        code, _, err = self.main([case(arguments={"id": "{{caller.oid}}"})], run_all)
        self.assertEqual(code, 2)
        self.assertIn("{{caller.oid}}: no matching claim", err)


class TraceContextTests(NoTokenTestCase):
    def test_requests_carry_the_current_trace_as_an_http_header(self):
        # The toolbox ignores the copy of the trace context that the MCP SDK puts in params._meta.
        with mock.patch.dict(os.environ, {"APPLICATIONINSIGHTS_CONNECTION_STRING": ""}):
            telemetry.init("unit-test")  # an SDK tracer provider, so spans get IDs; nothing is exported
        root = logging.getLogger()
        saved = (root.level, list(root.handlers))
        server = MCPServer("echo")  # calls logging.basicConfig at INFO; undone below
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]

        @server.tool()
        def echo(text: str) -> str:
            return text

        app = server.streamable_http_app(host="0.0.0.0")
        sent = []

        class Recorder(httpx2.ASGITransport):
            async def handle_async_request(self, request):
                sent.append(request)
                return await super().handle_async_request(request)

        async def go():
            async with app.router.lifespan_context(app), run.connect(TARGET, run.HttpErrors()) as tb:
                mark = len(sent)
                with telemetry.span("unit.case") as sp:
                    self.assertEqual((await tb.call("echo", {"text": "hi"})).outcome, "ok")
                    await run.run_auth(TARGET, case(kind="auth", token="missing", expect_status=401))
                return sent[mark:], f"{sp.get_span_context().trace_id:032x}"

        with mock.patch.object(httpx2, "AsyncHTTPTransport", lambda **_: Recorder(app=app)):
            requests, trace_id = asyncio.run(go())
        self.assertEqual([json.loads(r.content)["method"] for r in requests], ["tools/call", "initialize"])
        self.assertEqual([r.headers["traceparent"].split("-")[1] for r in requests], [trace_id] * 2)


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
        with mock.patch.object(Path, "read_text", return_value='{"toolbox": {"mcp_endpoint": "https://c/mcp", "token_scope": null}}'):
            self.assertEqual(self.resolve(config=Path("x.json")).scope, run.auth.FOUNDRY_SCOPE)
        for text in ('{"agent": {}}', '[1]', '"x"', '{"toolbox": "x"}', '{"toolbox": null}', '{"toolbox": {"mcp_endpoint": 5}}'):
            with self.subTest(config=text), mock.patch.object(Path, "read_text", return_value=text), \
                    self.assertRaises(run.SetupError):
                self.resolve(config=Path("x.json"))

    def test_missing_endpoint(self):
        with self.assertRaises(run.SetupError):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
