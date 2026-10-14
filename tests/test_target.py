import email.message
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

# Import without the real .env or App Insights: the target reads its settings at import time.
with mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test/api/projects/p/"}), \
        mock.patch("dotenv.load_dotenv"), mock.patch("evals.telemetry.init"):
    from evals.foundry_agent import target


def http_error(code, body="", **headers):
    hdrs = email.message.Message()
    for k, v in headers.items():
        hdrs[k.replace("_", "-")] = v
    return urllib.error.HTTPError("https://example.test", code, "error", hdrs, io.BytesIO(body.encode()))


def ok(data):
    return io.BytesIO(json.dumps(data).encode())


class PostResponsesTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(target.auth, "get_token", return_value="tok"),
            mock.patch.object(target.time, "sleep"),
            mock.patch.object(target.urllib.request, "urlopen"),
        ]
        _, self.sleep, self.urlopen = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def test_endpoint_trailing_slash_is_trimmed(self):
        self.urlopen.side_effect = [ok({})]
        target._post_responses({})
        self.assertEqual(self.urlopen.call_args.args[0].full_url,
                         "https://example.test/api/projects/p/openai/v1/responses")

    def test_throttling_honors_retry_after(self):
        self.urlopen.side_effect = [http_error(429, "slow down", Retry_After="7"), ok({"status": "completed"})]
        data, latency, attempts = target._post_responses({})
        self.assertEqual((data, attempts), ({"status": "completed"}, 2))
        self.sleep.assert_called_once_with(7.0)
        self.assertGreaterEqual(latency, 0)

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
        err = target.ResponsesAPIError(400, "An error occurred invoking 'get_customer': not found", 12.0, 2)
        with mock.patch.object(target, "_post_responses", side_effect=err):
            out = target.run_agent([{"role": "user", "content": "hi"}])
        self.assertTrue(out["platform_abort"])
        self.assertEqual((out["tool_calls"][0]["name"], out["latency_ms"], out["attempts"]), ("get_customer", 12.0, 2))
        self.assertTrue(out["text"].startswith(target.PLATFORM_ABORT_NOTE))

    def test_other_api_errors_propagate(self):
        err = target.ResponsesAPIError(401, "denied", 1.0)
        with mock.patch.object(target, "_post_responses", side_effect=err), self.assertRaises(target.ResponsesAPIError):
            target.run_agent([{"role": "user", "content": "hi"}])

    def test_parses_tool_calls_and_message(self):
        data = {"status": "completed", "output": [
            {"type": "mcp_call", "name": "get_stock_quote", "arguments": '{"ticker": "CTSO"}',
             "output": '{"price": 84.12}\n{\n  "structuredResponse": {}\n}'},
            {"type": "message", "content": [{"text": "Contoso trades at 84.12."}]},
        ]}
        with mock.patch.object(target, "_post_responses", return_value=(data, 50.0, 1)):
            out = target.run_agent([{"role": "user", "content": "price?"}])
        self.assertEqual(out["tool_calls"], [{"name": "get_stock_quote", "arguments": {"ticker": "CTSO"},
                                              "output": '{"price": 84.12}'}])
        self.assertEqual((out["text"], out["latency_ms"], out["platform_abort"]), ("Contoso trades at 84.12.", 50.0, False))


if __name__ == "__main__":
    unittest.main()
