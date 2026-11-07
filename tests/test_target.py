import contextlib
import email.message
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Import without the real .env or App Insights: the target reads its settings at import time.
with mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test/api/projects/p/"}), \
        mock.patch("dotenv.load_dotenv"), mock.patch("evals.telemetry.init"):
    from evals.foundry_agent import target

LINK = "https://logic-region-001.consent.azure-apihub.net/login?data=abc"


def http_error(code, body="", **headers):
    hdrs = email.message.Message()
    for k, v in headers.items():
        hdrs[k.replace("_", "-")] = v
    return urllib.error.HTTPError("https://example.test", code, "error", hdrs, io.BytesIO(body.encode()))


def ok(data):
    return io.BytesIO(json.dumps(data).encode())


class PostResponsesTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        patches = [
            mock.patch.object(target.auth, "get_token", return_value="tok"),
            mock.patch.object(target, "time"),
            mock.patch.object(target.urllib.request, "urlopen"),
        ]
        _, clock, self.urlopen = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        # A fake clock: it moves only while a request runs (see respond) or the client waits.
        clock.perf_counter.side_effect = lambda: self.now
        clock.sleep.side_effect = self.wait
        self.sleep = clock.sleep

    def wait(self, seconds):
        self.now += seconds

    def respond(self, *steps):
        """Answer each request with the next (seconds it takes, response or error to raise)."""
        steps = list(steps)

        def urlopen(*args, **kwargs):
            seconds, outcome = steps.pop(0)
            self.now += seconds
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        self.urlopen.side_effect = urlopen

    def test_endpoint_trailing_slash_is_trimmed(self):
        self.urlopen.side_effect = [ok({})]
        target._post_responses({})
        self.assertEqual(self.urlopen.call_args.args[0].full_url,
                         "https://example.test/api/projects/p/openai/v1/responses")

    def test_throttling_honors_retry_after(self):
        self.urlopen.side_effect = [http_error(429, "slow down", Retry_After="7"), ok({"status": "completed"})]
        data, _, attempts, _ = target._post_responses({})
        self.assertEqual((data, attempts), ({"status": "completed"}, 2))
        self.sleep.assert_called_once_with(7.0)

    def test_elapsed_time_covers_every_attempt_and_wait_and_latency_only_the_last(self):
        self.respond((1.0, http_error(429, Retry_After="7")), (0.5, ok({})))
        _, latency, attempts, elapsed = target._post_responses({})
        self.assertEqual((latency, attempts, elapsed), (500.0, 2, 8500.0))

        self.now = 0.0
        self.respond((0.25, http_error(503)), (1.0, http_error(400, "bad input")))  # 5 s backoff after the 503
        with self.assertRaises(target.ResponsesAPIError) as ctx:
            target._post_responses({})
        self.assertEqual((ctx.exception.latency_ms, ctx.exception.attempts, ctx.exception.elapsed_ms),
                         (1000.0, 2, 6250.0))

    def test_client_errors_are_not_retried(self):
        self.urlopen.side_effect = [http_error(400, "bad input")]
        with self.assertRaises(target.ResponsesAPIError) as ctx:
            target._post_responses({})
        self.assertEqual((ctx.exception.status, ctx.exception.body, ctx.exception.attempts), (400, "bad input", 1))
        self.assertEqual(str(ctx.exception), "Responses API 400: bad input")
        self.sleep.assert_not_called()

    def test_retries_give_up_after_max_attempts(self):
        self.urlopen.side_effect = [http_error(503) for _ in range(target.MAX_ATTEMPTS)]
        with self.assertRaises(target.ResponsesAPIError) as ctx:
            target._post_responses({})
        self.assertEqual(ctx.exception.attempts, target.MAX_ATTEMPTS)
        self.assertEqual(self.sleep.call_count, target.MAX_ATTEMPTS - 1)

    def test_connection_failures_are_retried_but_read_timeouts_are_not(self):
        self.urlopen.side_effect = [urllib.error.URLError("getaddrinfo failed"), ok({})]
        self.assertEqual(target._post_responses({})[2], 2)
        self.urlopen.side_effect = [TimeoutError("timed out")]
        with self.assertRaises(TimeoutError):
            target._post_responses({})


class RetryDelayTests(unittest.TestCase):
    def test_backoff_without_hints(self):
        self.assertEqual([target.retry_delay(n) for n in range(5)], [5, 10, 20, 30, 30])

    def test_server_hints(self):
        self.assertEqual(target.retry_delay(0, {"retry-after-ms": "1500"}), 1.5)
        self.assertEqual(target.retry_delay(0, {"Retry-After": "999"}), target.MAX_RETRY_AFTER_S)
        self.assertEqual(target.retry_delay(0, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), 0)
        self.assertEqual(target.retry_delay(2, {"Retry-After": "soon"}), 20)


class RunAgentTests(unittest.TestCase):
    def test_tool_error_abort_becomes_a_result(self):
        err = target.ResponsesAPIError(400, "An error occurred invoking 'get_customer': not found", 12.0, 2, 5012.0)
        with mock.patch.object(target, "_post_responses", side_effect=err):
            out = target.run_agent([{"role": "user", "content": "hi"}])
            self.assertTrue(target.chat("hi").startswith(target.PLATFORM_ABORT_NOTE))  # the judge sees it
        self.assertEqual((out["platform_abort"], out["abort_reason"]), (True, "tool_error"))
        self.assertEqual((out["tool_calls"][0]["name"], out["latency_ms"], out["attempts"], out["elapsed_ms"]),
                         ("get_customer", 12.0, 2, 5012.0))
        self.assertEqual({k: out[k] for k in target.NOT_SERVED}, target.NOT_SERVED)
        self.assertTrue(out["text"].startswith(target.PLATFORM_ABORT_NOTE))

    def test_consent_request_aborts_and_the_link_only_reaches_the_console(self):
        data = {"status": "completed", "output": [
            {"type": "mcp_list_tools", "server_label": "sample", "tools": []},
            {"type": "oauth_consent_request", "id": "oauthreq_1", "consent_link": LINK, "server_label": "sample"},
        ]}
        err = io.StringIO()
        with mock.patch.object(target, "_post_responses", return_value=(data, 30.0, 1, 30.0)), \
                mock.patch.object(target.toolbox_errors, "_announced", set()), \
                mock.patch.object(target, "_username", return_value="ada@example.com"), \
                contextlib.redirect_stderr(err):
            out = target.run_agent([{"role": "user", "content": "hi"}])
            with self.assertRaises(target.AgentNotRun) as ctx:
                target.chat("hi")
        self.assertEqual((out["platform_abort"], out["abort_reason"], out["tool_calls"]), (True, "consent_required", []))
        self.assertEqual(out["text"], f"{target.CONSENT_NOTE} tool source 'sample': user consent required")
        self.assertNotIn(LINK, out["text"] + str(ctx.exception))
        self.assertEqual(err.getvalue().count(LINK), 1)

    def test_tool_listing_failure_reports_the_tool_source_error(self):
        sources = '{"errors":[{"name":"sample","type":"mcp","error":{"code":"%s","message":"%s"}}]}'
        for code, message, reason, text in (
            ("HTTP_401", "Authentication failed", "tool_listing_failed",
             f"{target.LISTING_NOTE} tool source 'sample': HTTP_401: Authentication failed"),
            ("CONSENT_REQUIRED", LINK, "consent_required",
             f"{target.CONSENT_NOTE} tool source 'sample': user consent required"),
        ):
            body = json.dumps({"error": {"type": "invalid_request_error", "code": "tool_user_error", "message":
                f"{target.LISTING_FAILED}: Request failed (remote): tools/list failed for 1 tool source(s), succeeded "
                f"for 0 tool source(s) {sources % (code, message)}. Troubleshooting guide: https://learn.example"}})
            err = target.ResponsesAPIError(400, body, 20.0, 1)
            with mock.patch.object(target, "_post_responses", side_effect=err), \
                    mock.patch.object(target.toolbox_errors, "_announced", set()), \
                    mock.patch.object(target, "_username", return_value=None), \
                    contextlib.redirect_stderr(io.StringIO()):
                out = target.run_agent([{"role": "user", "content": "hi"}])
                with self.assertRaises(target.AgentNotRun):
                    target.chat("hi")
            self.assertEqual((out["abort_reason"], out["text"], out["tool_calls"]), (reason, text, []))

    def test_other_api_errors_propagate(self):
        err = target.ResponsesAPIError(401, "denied", 1.0)
        with mock.patch.object(target, "_post_responses", side_effect=err), self.assertRaises(target.ResponsesAPIError):
            target.run_agent([{"role": "user", "content": "hi"}])

    def test_parses_tool_calls_and_message(self):
        data = {
            "id": "resp_1", "status": "completed", "model": "gpt-4-1-mini",
            "agent_reference": {"type": "agent_reference", "name": "customer-assistant", "version": 2},
            "usage": {"input_tokens": 900, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 40,
                      "total_tokens": 940},
            "output": [
                {"type": "mcp_call", "name": "get_stock_quote", "arguments": '{"ticker": "CTSO"}',
                 "output": '{"price": 84.12}\n{\n  "structuredResponse": {}\n}'},
                {"type": "message", "content": [{"text": "Contoso trades at 84.12."}]},
            ],
        }
        with mock.patch.object(target, "_post_responses", return_value=(data, 50.0, 2, 5050.0)):
            out = target.run_agent([{"role": "user", "content": "price?"}])
        self.assertEqual(out["tool_calls"], [{"name": "get_stock_quote", "arguments": {"ticker": "CTSO"},
                                              "output": '{"price": 84.12}'}])
        self.assertEqual((out["text"], out["latency_ms"], out["platform_abort"], out["abort_reason"]),
                         ("Contoso trades at 84.12.", 50.0, False, None))
        self.assertEqual((out["attempts"], out["elapsed_ms"]), (2, 5050.0))
        self.assertEqual((out["response_id"], out["agent_version"], out["model"], out["usage"]),
                         ("resp_1", "2", "gpt-4-1-mini", {"input_tokens": 900, "output_tokens": 40, "total_tokens": 940}))

    def test_the_agent_span_records_the_agent_version_and_tokens_it_reported(self):
        full = {"status": "completed", "output": [], "agent_reference": {"name": "customer-assistant", "version": 2},
                "usage": {"input_tokens": 900, "output_tokens": 40, "total_tokens": 940}}
        for data, expected in (
            (full, {"gen_ai.agent.version": "2", "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 40}),
            ({"status": "completed", "output": []}, {}),
        ):
            exporter = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            with mock.patch.object(target, "_tracer", provider.get_tracer("test")), \
                    mock.patch.object(target, "_post_responses", return_value=(data, 50.0, 1, 50.0)):
                target.run_agent([{"role": "user", "content": "hi"}])
            (span,) = exporter.get_finished_spans()
            self.assertEqual({k: v for k, v in span.attributes.items()
                              if k.startswith(("gen_ai.agent.version", "gen_ai.usage."))}, expected)


if __name__ == "__main__":
    unittest.main()
