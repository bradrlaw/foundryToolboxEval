import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.run_agent_evals import run_conversations, score_turn  # noqa: E402


def res(calls, text="", latency=100.0):
    return {"text": text, "latency_ms": latency, "platform_abort": False, "tool_calls": calls}


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
        self.assertTrue(score_turn(c, res([])))
        self.assertTrue(score_turn(c, res([call("t", id="C2")])))

    def test_regex_param(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"name": "re:north"}}])
        self.assertEqual(score_turn(c, res([call("t", name="Northwind Energy")])), [])

    def test_must_not_call(self):
        c = case(must_not_call=["bad"])
        self.assertEqual(score_turn(c, res([call("ok")])), [])
        self.assertIn("forbidden", score_turn(c, res([call("bad")]))[0])

    def test_ordered(self):
        c = case(ordered=True, expected_tool_calls=[{"tool": "a", "params": {}}, {"tool": "b", "params": {}}])
        self.assertEqual(score_turn(c, res([call("a"), call("b")])), [])
        self.assertTrue(score_turn(c, res([call("b"), call("a")])))

    def test_response_patterns_and_latency(self):
        c = case(pass_criteria="both", expected_response_pattern=r"84\.12", response_must_not_match="sorry", max_latency_ms=50)
        fails = score_turn(c, res([], text="price is 99 sorry", latency=100))
        self.assertEqual(len(fails), 3)

    def test_platform_abort_matches_on_name_only(self):
        c = case(expected_tool_calls=[{"tool": "t", "params": {"id": "C1"}}])
        self.assertEqual(score_turn(c, res([{"name": "t", "arguments": None, "output": ""}])), [])


class ConversationTests(unittest.TestCase):
    def test_history_accumulates_and_errors_fail_turn(self):
        seen = []

        def agent(history):
            seen.append(len(history))
            if len(seen) == 2:
                raise RuntimeError("boom")
            return res([], text="hi")

        cases = [
            {"conversation_id": "x", "turn_number": 2, "user_message": "b", "category": "c", **case()},
            {"conversation_id": "x", "turn_number": 1, "user_message": "a", "category": "c", **case()},
        ]
        out = run_conversations(cases, agent)
        self.assertEqual([r["turn_number"] for r in out], [1, 2])
        self.assertEqual(seen, [1, 3])
        self.assertTrue(out[0]["passed"])
        self.assertIn("runner error", out[1]["failures"][0])


if __name__ == "__main__":
    unittest.main()
