import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.reasons import Failure
from evals.run_agent_evals import main, run_conversations, score_turn

# main imports the target, which reads its settings at import time.
with mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": "https://example.test/api/projects/p/"}), \
        mock.patch("dotenv.load_dotenv"), mock.patch("evals.telemetry.init"):
    from evals.foundry_agent import target


def res(calls, text="", latency=100.0, abort=False):
    return {"text": text, "latency_ms": latency, "platform_abort": abort, "tool_calls": calls, "attempts": 1}


def call(tool, **args):
    return {"name": tool, "arguments": args, "output": ""}


def case(**kw):
    base = {"expected_tool_calls": [], "pass_criteria": "tool_called_correctly"}
    base.update(kw)
    return base


class ScoreTurnTests(unittest.TestCase):
    def test_expected_call_matches_params_case_insensitively(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"industry": "Healthcare"}}])
        self.assertEqual(score_turn(c, res([call("t", industry="healthcare", extra=1)])), [])

    def test_missing_call_and_wrong_params_fail(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"id": "C1"}}])
        self.assertEqual(score_turn(c, res([call("u")])), [("missing_call", "expected call t {'id': 'C1'} not made")])
        self.assertEqual(score_turn(c, res([call("t", id="C2"), call("t", id="C3")])), [
            ("wrong_arguments", "expected call t {'id': 'C1'} not made; t was called with {'id': 'C2'}; {'id': 'C3'}")])

    def test_regex_param(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"name": "re:north"}}])
        self.assertEqual(score_turn(c, res([call("t", name="Northwind Energy")])), [])

    def test_must_not_call(self):
        c = case(must_not_call=["bad"])
        self.assertEqual(score_turn(c, res([call("ok")])), [])
        self.assertEqual(score_turn(c, res([call("bad")]))[0].reason, "forbidden_call")

    def test_must_not_call_enforced_for_any_criteria(self):
        c = case(pass_criteria="response_matches", expected_response_pattern="fine", must_not_call=["bad"])
        self.assertEqual(score_turn(c, res([call("bad")], text="fine")),
                         [Failure("forbidden_call", "forbidden tool bad was called")])

    def test_response_matches_ignores_expected_calls(self):
        c = case(pass_criteria="response_matches", expected_response_pattern="fine",
                 expected_tool_calls=[{"tool": "t", "params": {}}])
        self.assertEqual(score_turn(c, res([], text="fine")), [])

    def test_ordered(self):
        c = case(ordered=True, expected_tool_calls=[{"tool": "a", "params": {}}, {"tool": "b", "params": {}}])
        self.assertEqual(score_turn(c, res([call("a"), call("b")])), [])
        self.assertEqual(score_turn(c, res([call("b"), call("a")])), [("wrong_order", "expected call b {} made out of order")])

    def test_ordered_repeat_needs_a_second_call(self):
        c = case(ordered=True, expected_tool_calls=[{"tool": "a", "params": {}}, {"tool": "a", "params": {}}])
        self.assertEqual(score_turn(c, res([call("a"), call("a")])), [])
        self.assertEqual([f.reason for f in score_turn(c, res([call("a")]))], ["missing_call"])

    def test_response_patterns_and_latency(self):
        c = case(pass_criteria="both", expected_response_pattern=r"84\.12", response_must_not_match="sorry", max_latency_ms=50)
        fails = score_turn(c, res([], text="price is 99 sorry", latency=100))
        self.assertEqual([f.reason for f in fails], ["response_mismatch", "forbidden_response", "latency"])

    def test_platform_abort_matches_on_name_only(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"id": "C1"}}])
        self.assertEqual(score_turn(c, res([{"name": "t", "arguments": None, "output": ""}])), [])

    def test_a_turn_the_agent_never_ran_fails_with_the_reason(self):
        # Without the early exit this case would pass vacuously: no forbidden call, no forbidden text.
        c = case(pass_criteria="response_matches", expected_response_pattern=".*", must_not_call=["t"],
                 response_must_not_match="secret")
        for reason in ("consent_required", "tool_listing_failed"):
            r = {**res([], text=f"[AGENT NOT RUN: {reason}]", abort=True), "abort_reason": reason}
            self.assertEqual(score_turn(c, r), [(reason, f"[AGENT NOT RUN: {reason}]")])


class ConversationTests(unittest.TestCase):
    def run_turns(self, agent, n=2):
        # Listed in reverse so the runner must sort by turn_number.
        cases = [{"conversation_id": "x", "turn_number": i, "user_message": f"m{i}", "category": "c", **case()}
                 for i in range(n, 0, -1)]
        return run_conversations(cases, agent)

    def test_history_accumulates_in_turn_order(self):
        seen = []

        def agent(history):
            seen.append([(m["role"], m["content"]) for m in history])
            return res([], text=f"r{len(seen)}")

        out = self.run_turns(agent)
        self.assertEqual([r["turn_number"] for r in out], [1, 2])
        self.assertEqual(seen, [[("user", "m1")], [("user", "m1"), ("assistant", "r1"), ("user", "m2")]])
        self.assertEqual([r["history_gap"] for r in out], [False, False])

    def test_runner_error_fails_turn_and_is_not_replayed(self):
        seen = []

        def agent(history):
            seen.append([m["role"] for m in history])
            if len(seen) == 1:
                raise RuntimeError("boom")
            return res([], text="ok")

        out = self.run_turns(agent)
        self.assertEqual(out[0]["failures"], ["runner error: RuntimeError: boom"])
        self.assertEqual(out[0]["reasons"], ["runner_error"])
        self.assertIsNone(out[0]["latency_ms"])
        self.assertEqual(seen[1], ["user", "user"])
        self.assertTrue(out[1]["passed"])
        self.assertEqual(out[1]["reasons"], [])
        self.assertTrue(out[1]["history_gap"])

    def test_platform_abort_note_is_not_replayed(self):
        seen = []

        def agent(history):
            seen.append([m["content"] for m in history])
            if len(seen) == 1:
                return res([{"name": "t", "arguments": None, "output": "x"}], text="[AGENT FAILURE: ...]", abort=True)
            return res([], text="ok")

        out = self.run_turns(agent, n=3)
        self.assertTrue(out[0]["platform_abort"])
        self.assertEqual(seen[1], ["m1", "m2"])
        self.assertEqual(seen[2], ["m1", "m2", "ok", "m3"])
        self.assertEqual([r["history_gap"] for r in out], [False, True, True])


class MainTests(unittest.TestCase):
    def test_invalid_cases_exit_2_before_contacting_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text('{"conversation_id": "x"}\n', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main([str(path)]), 2)
        self.assertIn("'turn_number' is a required property", err.getvalue())

    def test_agent_flag_and_caller_placeholders(self):
        seen = {}

        def fake_run(cases, agent):
            seen.update(cases=cases, agent=target.AGENT_NAME)
            return [{"conversation_id": "x", "turn_number": 1, "category": "c", "passed": True, "failures": [],
                     "platform_abort": False, "abort_reason": None, "history_gap": False, "latency_ms": 1.0}]

        line = {"conversation_id": "x", "turn_number": 1, "user_message": "Who am I?", "expected_tool_calls": [],
                "pass_criteria": "response_matches", "expected_response_pattern": "{{caller.name}}", "category": "c"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(target, "AGENT_NAME", "default-agent"), \
                mock.patch("evals.run_agent_evals.load_dotenv"), mock.patch("evals.telemetry.init"), \
                mock.patch("evals.auth.get_token", return_value="tok"), \
                mock.patch("evals.auth.caller_claims", return_value={"name": "Ada (Admin)"}), \
                mock.patch("evals.run_agent_evals.run_conversations", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps(line), encoding="utf-8")
            self.assertEqual(main([str(path), "--agent", "my-agent", "--out", tmp]), 0)
            saved = json.loads(next(Path(tmp).glob("run-*.json")).read_text(encoding="utf-8"))
        self.assertEqual((seen["agent"], saved["agent"]), ("my-agent", "my-agent"))
        self.assertEqual(seen["cases"][0]["expected_response_pattern"], "Ada\\ \\(Admin\\)")


if __name__ == "__main__":
    unittest.main()
