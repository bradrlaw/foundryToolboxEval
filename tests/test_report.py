import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from evals.reasons import AGENT_REASONS, TOOL_REASONS

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("report", ROOT / "scripts" / "report.py")
report = importlib.util.module_from_spec(spec)
sys.modules["report"] = report
spec.loader.exec_module(report)


def r(cid, passed, cat="a", p50=100.0):
    return {"case_id": cid, "category": cat, "passed": passed, "failures": [] if passed else ["x"], "latency_p50_ms": p50}


def failed(cid, *pairs, **kw):
    return {"case_id": cid, "category": "a", "passed": False, "failures": [m for _, m in pairs],
            "reasons": [k for k, _ in pairs], "latency_p50_ms": 100.0, **kw}


def turn(n, reason, message, **kw):
    return {"conversation_id": "c", "turn_number": n, "category": "a", "passed": False, "failures": [message],
            "reasons": [reason], "platform_abort": False, "abort_reason": None, "history_gap": False,
            "latency_ms": 10.0, **kw}


class ReportTests(unittest.TestCase):
    def test_by_category(self):
        self.assertEqual(report.by_category([r("1", True), r("2", False), r("3", True, "b")]), {"a": (1, 2), "b": (1, 1)})

    def test_diff(self):
        base = [r("same", True), r("reg", True), r("fix", False), r("gone", True), r("slow", True, p50=100)]
        cur = [r("same", True), r("reg", False), r("fix", True), r("new", True), r("slow", True, p50=200)]
        d = report.diff_cases(cur, base)
        self.assertEqual(d["regressions"], ["reg"])
        self.assertEqual(d["fixes"], ["fix"])
        self.assertEqual(d["new"], ["new"])
        self.assertEqual(d["removed"], ["gone"])
        self.assertEqual([s[0] for s in d["slower"]], ["slow"])

    def test_case_report_mentions_regression(self):
        cur = {"timestamp": "t2", "results": [r("a", False)]}
        base = {"timestamp": "t1", "results": [r("a", True)]}
        text = report.case_report("T", cur, base)
        self.assertIn("0/1 passed", text)
        self.assertIn("Regressions: `a`", text)


class WhyFailedTests(unittest.TestCase):
    def test_groups_failures_by_reason_most_cases_first(self):
        res = [failed("a", ("latency", "latency 900ms > 500ms"), ("assertion", "path $.x not found")),
               failed("b", ("assertion", "missing text 'y'")), r("c", True)]
        groups = report.by_reason(res)
        self.assertEqual(list(groups), ["assertion", "latency"])
        self.assertEqual([(x["case_id"], m) for x, m in groups["assertion"]],
                         [("a", "path $.x not found"), ("b", "missing text 'y'")])

    def test_tool_report_explains_reasons_and_regressions(self):
        cur = {"results": [failed("a", ("tool_error", "expected ok, got tool_error: boom"), tool="get_customer"),
                           r("b", True)]}
        text = report.case_report("T", cur, {"results": [r("a", True), r("b", True)]}, TOOL_REASONS)
        self.assertIn(f"| `tool_error` | 1 | {TOOL_REASONS['tool_error']} |", text)
        self.assertIn("### `tool_error`\n\n- `a` get_customer: expected ok, got tool_error: boom", text)
        self.assertIn("Regressions: `a` (tool_error)", text)
        self.assertNotIn("Platform aborts", text)

    def test_agent_report_counts_aborts_and_flags_turns(self):
        res = [turn(1, "response_mismatch", "response does not match /84/", platform_abort=True,
                    abort_reason="tool_error"),
               turn(2, "consent_required", "[AGENT NOT RUN: no consent]", platform_abort=True,
                    abort_reason="consent_required", history_gap=True)]
        text = report.case_report("Agent evals", {"results": res}, None, AGENT_REASONS)
        self.assertIn("Platform aborts: 2 (tool_error 1, consent_required 1). Turns with a history gap: 1.", text)
        self.assertIn("- `c#1` [platform abort: tool_error]: response does not match /84/", text)
        self.assertIn("- `c#2` [history gap]: [AGENT NOT RUN: no consent]", text)

    def test_results_without_reasons_still_list_their_failures(self):
        text = report.case_report("T", {"results": [r("old", False)]}, None, TOOL_REASONS)
        self.assertIn(f"| `unrecorded` | 1 | {report.UNRECORDED_MEANING} |", text)
        self.assertIn("- `old`: x", text)

    def test_abort_without_a_recorded_reason_is_only_counted(self):
        old = turn(1, "runner_error", "x", platform_abort=True)
        del old["abort_reason"], old["reasons"]
        text = report.case_report("Agent evals", {"results": [old]}, None, AGENT_REASONS)
        self.assertIn("Platform aborts: 1. Turns with a history gap: 0.", text)

    def test_all_passed_has_no_failure_sections(self):
        text = report.case_report("T", {"results": [r("a", True)]}, None, TOOL_REASONS)
        self.assertNotIn("Why cases failed", text)


class AssertReportTests(unittest.TestCase):
    DESIGN = {"behavior": "Unsupported assertions", "data_availability": "tool_error", "pressure": "user_insists"}

    def write_run(self, tmp, scores, inference):
        run_dir = Path(tmp) / "suite" / "run1"
        run_dir.mkdir(parents=True)
        for name, rows in (("scores.jsonl", scores), ("inference_set.jsonl", inference)):
            (run_dir / name).write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        return run_dir

    def test_lists_violations_with_justifications_and_unscored_cases_with_target_errors(self):
        scores = [
            {"type": "prompt", "test_case_id": "tc2", "judge_status": "ok", "dimensions": self.DESIGN,
             "verdict": {"dimensions": {"policy_violation": True, "pii_leakage": False}, "justification": "overall",
                         "dimension_justifications": {"policy_violation": "Used manufacturing data for aerospace [1]."}}},
            {"type": "prompt", "test_case_id": "tc1", "judge_status": "ok", "dimensions": self.DESIGN,
             "verdict": {"dimensions": {"policy_violation": False, "pii_leakage": False}}},
            {"type": "scenario", "test_case_id": "tc7", "judge_status": "scoring_skipped",
             "judge_error": "scoring_skipped: target_error", "dimensions": self.DESIGN, "verdict": None},
        ]
        body = json.dumps({"error": {"message": "Unknown ticker 'GE'.", "code": "tool_user_error"}})
        inference = [{"type": "scenario", "test_case_id": "tc7", "stop_reason": "target_error", "events": [
            {"edit": {"type": "add_message", "message": {"role": "user", "content": "hi"}}},
            {"edit": {"type": "add_message", "message": {"role": "system", "content":
                f"[TARGET ERROR: Responses API 400: {body}]\nTraceback (most recent call last):\n  File x"}}}]}]
        with tempfile.TemporaryDirectory() as tmp:
            text = report.assert_report(self.write_run(tmp, scores, inference), None)
        self.assertIn("Judge status: ok=2, scoring_skipped=1", text)
        case = "(prompt: Unsupported assertions; data_availability=tool_error, pressure=user_insists)"
        self.assertIn(f"## Violations\n\n- `tc2` {case}\n  - policy_violation: Used manufacturing data for aerospace [1].",
                      text)
        self.assertNotIn("`tc1`", text)
        self.assertIn("- `tc7` (scenario: Unsupported assertions; data_availability=tool_error, pressure=user_insists): "
                      "scoring_skipped: target_error. Target error: Responses API 400 (tool_user_error): "
                      "Unknown ticker 'GE'.", text)
        self.assertNotIn("Traceback", text)

    def test_target_errors_that_do_not_parse_are_kept(self):
        self.assertEqual(report._readable_error("Responses API 500: {cut off"), "Responses API 500: {cut off")
        self.assertEqual(report._readable_error("[AGENT NOT RUN: no consent]"), "[AGENT NOT RUN: no consent]")


if __name__ == "__main__":
    unittest.main()
