import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.reasons import Failure
from evals.run_agent_evals import main, run_conversations, score_turn, summary

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

    def test_max_tool_calls_fails_a_turn_that_makes_more_calls(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {}}], max_tool_calls=1)
        self.assertEqual(score_turn(c, res([call("t")])), [])
        self.assertEqual(score_turn(c, res([call("t"), call("u")])),
                         [("extra_calls", "2 tool calls, max_tool_calls is 1: t, u")])
        # Enforced whatever the pass_criteria, like must_not_call.
        c = case(pass_criteria="response_matches", expected_response_pattern="144", max_tool_calls=0)
        self.assertEqual(score_turn(c, res([call("t")], text="144")), [("extra_calls", "1 tool call, max_tool_calls is 0: t")])

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

    def test_unordered_expected_calls_each_need_their_own_call(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"id": "C1"}}, {"tool": "t", "params": {"id": "re:C"}}])
        self.assertEqual(score_turn(c, res([call("t", id="C1")])),
                         [("missing_call", "expected call t {'id': 're:C'} not made")])
        # A broad expectation listed first gives way to a stricter one instead of taking its only call.
        c = case(expected_tool_calls=[{"tool": "t", "params": {"id": "re:C"}}, {"tool": "t", "params": {"id": "C1"}}])
        self.assertEqual(score_turn(c, res([call("t", id="C1"), call("t", id="C2")])), [])

    def test_argument_values_compare_as_json(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"flag": True, "n": 1}}])
        self.assertEqual(score_turn(c, res([call("t", flag=True, n=1.0)])), [])
        self.assertEqual([f.reason for f in score_turn(c, res([call("t", flag=1, n=1)]))], ["wrong_arguments"])

    def test_response_patterns_and_latency(self):
        c = case(pass_criteria="both", expected_response_pattern=r"84\.12", response_must_not_match="sorry", max_latency_ms=50)
        fails = score_turn(c, res([], text="price is 99 sorry", latency=100))
        self.assertEqual([f.reason for f in fails], ["response_mismatch", "forbidden_response", "latency"])

    def test_platform_abort_fails_the_turn_without_checking_the_reply(self):
        # The abort note quotes the raw tool error, which must not pass or fail the reply checks.
        c = case(pass_criteria="both", expected_tool_calls=[{"tool": "t", "params": {"id": "C1"}}],
                 expected_response_pattern="private", response_must_not_match="C1")
        note = "[AGENT FAILURE: ...] Customer C1 is privately held"
        r = {**res([{"name": "t", "arguments": None, "output": ""}], text=note, abort=True), "abort_reason": "tool_error"}
        self.assertEqual(score_turn(c, r), [("platform_abort", note)])
        r["tool_calls"][0]["name"] = "u"
        self.assertEqual([f.reason for f in score_turn(c, r)], ["platform_abort", "missing_call"])

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
        self.assertEqual((out[0]["latency_ms"], out[0]["elapsed_ms"], out[0]["agent_version"], out[0]["extra_calls"]),
                         (None, None, None, None))
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

    def test_each_turn_records_what_served_it_and_its_elapsed_time(self):
        served = {"response_id": "resp_1", "agent_version": "2", "model": "gpt-4-1-mini",
                  "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
        out = self.run_turns(lambda history: {**res([], text="ok"), "attempts": 2, "elapsed_ms": 9100.04, **served}, n=1)
        self.assertEqual({k: out[0][k] for k in served}, served)
        self.assertEqual((out[0]["attempts"], out[0]["elapsed_ms"]), (2, 9100.0))

    def test_each_turn_records_its_calls_beyond_those_expected(self):
        one = {"tool": "t", "params": {}}
        kinds = {"over": {"expected_tool_calls": [one]}, "short": {"expected_tool_calls": [one] * 4},
                 "reply-only": {"pass_criteria": "response_matches", "expected_response_pattern": "."}, "not-run": {}}
        cases = [{"conversation_id": k, "turn_number": 1, "user_message": k, "category": "c", **case(**kw)}
                 for k, kw in kinds.items()]
        not_run = {**res([], text="[AGENT NOT RUN: consent]", abort=True), "abort_reason": "consent_required"}
        out = run_conversations(cases, lambda history: not_run if history[-1]["content"] == "not-run"
                                else res([call("t"), call("t"), call("u")], text="ok"))
        # Unknown when the turn doesn't check its calls, or the agent never ran.
        self.assertEqual({r["conversation_id"]: r["extra_calls"] for r in out},
                         {"over": 2, "short": 0, "reply-only": None, "not-run": None})

    def test_repetitions_rerun_every_conversation_from_a_fresh_history(self):
        seen, spans = [], []

        def agent(history):
            seen.append([m["content"] for m in history])
            return res([call("t")], text=f"r{len(seen)}")

        @contextlib.contextmanager
        def span(name, **attributes):
            spans.append(attributes)
            yield mock.Mock()

        cases = [{"conversation_id": cid, "turn_number": n, "user_message": f"{cid}{n}", "category": "c", **case()}
                 for cid, n in (("x", 1), ("x", 2), ("y", 1))]
        with mock.patch("evals.telemetry.span", span):
            out = run_conversations(cases, agent, repetitions=2)
        # Every conversation once, then all of them again.
        self.assertEqual([(r["conversation_id"], r["turn_number"], r["repetition"]) for r in out],
                         [("x", 1, 1), ("x", 2, 1), ("y", 1, 1), ("x", 1, 2), ("x", 2, 2), ("y", 1, 2)])
        self.assertEqual(seen, [["x1"], ["x1", "r1", "x2"], ["y1"], ["x1"], ["x1", "r4", "x2"], ["y1"]])
        self.assertEqual([s["eval.repetition"] for s in spans], [1, 1, 1, 2, 2, 2])


class MainTests(unittest.TestCase):
    LINE = {"conversation_id": "x", "turn_number": 1, "user_message": "Who am I?", "expected_tool_calls": [],
            "pass_criteria": "response_matches", "expected_response_pattern": "{{caller.name}}", "category": "c"}

    def main(self, turns, *argv, agent=None):
        """Run main on LINE with the conversations faked to return `turns`, and `agent` as what the project says
        about the agent version. Returns the exit code, the saved run, what the fake run got (and the console's
        output, as `stdout`), and the lookup."""
        seen = {}

        def fake_run(cases, run_agent, repetitions):
            seen.update(cases=cases, agent=target.AGENT_NAME, repetitions=repetitions)
            return turns

        agent = agent or {"name": "n", "version": None, "model": None, "toolboxes": []}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(target, "AGENT_NAME", "default-agent"), \
                mock.patch("evals.run_agent_evals.load_dotenv"), mock.patch("evals.telemetry.init"), \
                mock.patch("evals.auth.get_token", return_value="tok"), \
                mock.patch("evals.auth.caller_claims", return_value={"name": "Ada (Admin)"}), \
                mock.patch("evals.run_agent_evals.run_conversations", side_effect=fake_run), \
                mock.patch("evals.provenance.repo", return_value={"commit": "abc", "dirty": False}), \
                mock.patch("evals.provenance.agent", return_value=agent) as lookup, \
                contextlib.redirect_stdout(out):
            path = Path(tmp) / "cases.jsonl"
            path.write_text(json.dumps(self.LINE), encoding="utf-8")
            code = main([str(path), "--out", tmp, *argv])
            saved = json.loads(next(Path(tmp).glob("run-*.json")).read_text(encoding="utf-8"))
        seen["stdout"] = out.getvalue()
        return code, saved, seen, lookup

    @staticmethod
    def turn(n, **kw):
        return {"conversation_id": "x", "turn_number": n, "category": "c", "passed": True, "failures": [],
                "platform_abort": False, "abort_reason": None, "history_gap": False, "latency_ms": 1.0, **kw}

    def test_invalid_cases_exit_2_before_contacting_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            path.write_text('{"conversation_id": "x"}\n', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main([str(path)]), 2)
        self.assertIn("'turn_number' is a required property", err.getvalue())

    def test_agent_flag_and_caller_placeholders(self):
        code, saved, seen, _ = self.main([self.turn(1)], "--agent", "my-agent")
        self.assertEqual(code, 0)
        self.assertEqual((seen["agent"], saved["agent"]), ("my-agent", "my-agent"))
        self.assertEqual(seen["cases"][0]["expected_response_pattern"], "Ada\\ \\(Admin\\)")

    def test_the_run_records_what_it_ran_against_and_its_tokens(self):
        turns = [self.turn(1, agent_version="2", model="gpt-4-1-mini",
                           usage={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}),
                 self.turn(2, agent_version="2", model="gpt-4-1-mini",
                           usage={"input_tokens": 20, "output_tokens": 2, "total_tokens": 22}),
                 self.turn(3, agent_version="1", model="gpt-4o", usage=None),
                 self.turn(4, agent_version=None, model=None, usage=None)]
        # The definition couldn't be read, so the model comes from the turns.
        agent = {"name": "my-agent", "version": "2", "model": None, "toolboxes": [{"name": "tb", "version": "3"}]}
        _, saved, _, lookup = self.main(turns, "--agent", "my-agent", agent=agent)
        lookup.assert_called_once_with(target.PROJECT_ENDPOINT, "my-agent", "2")  # the version most turns used
        self.assertEqual(saved["provenance"], {"repo": {"commit": "abc", "dirty": False},
                                               "agent": {"name": "my-agent", "version": "2", "model": "gpt-4-1-mini"},
                                               "toolboxes": [{"name": "tb", "version": "3"}]})
        self.assertEqual(saved["usage"], {"input_tokens": 30, "output_tokens": 3, "total_tokens": 33})

    def test_a_repeated_turn_passes_only_if_every_run_passed(self):
        turns = [self.turn(1, repetition=1),
                 self.turn(1, repetition=2, passed=False, failures=["boom"], reasons=["missing_call"])]
        code, saved, seen, _ = self.main(turns, "--repetitions", "2")
        self.assertEqual((code, seen["repetitions"], saved["repetitions"]), (1, 2, 2))
        self.assertIn("PASS x#1 c [run 1/2] 1ms\nFAIL x#1 c [run 2/2] 1ms\n       - boom", seen["stdout"])
        self.assertIn("0/1 turns passed all 2 runs (1/2 runs passed)\npass@1 50%, pass@2 100%, pass^2 0%\n"
                      "Flaky: x#1 1/2\nFailed by reason: missing_call 1", seen["stdout"])

    def test_repetitions_must_be_at_least_one(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            main(["cases.jsonl", "--repetitions", "0"])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("must be a whole number of at least 1, not '0'", err.getvalue())

    def test_summary_counts_the_calls_beyond_those_expected(self):
        lines = summary([self.turn(1, extra_calls=2), self.turn(2, extra_calls=0), self.turn(3, extra_calls=None)])
        self.assertEqual(lines, ["3/3 passed", "Extra tool calls: 2 (x#1 2)"])


if __name__ == "__main__":
    unittest.main()
