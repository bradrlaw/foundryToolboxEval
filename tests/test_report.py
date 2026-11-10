import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from evals.reasons import AGENT_REASONS, CLOUD_REASONS, TOOL_REASONS

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
        self.assertNotIn("Extra tool calls", text)

    def test_agent_report_counts_aborts_and_flags_turns(self):
        res = [turn(1, "response_mismatch", "response does not match /84/", platform_abort=True,
                    abort_reason="tool_error"),
               turn(2, "consent_required", "[AGENT NOT RUN: no consent]", platform_abort=True,
                    abort_reason="consent_required", history_gap=True),
               turn(3, "platform_abort", "[AGENT FAILURE: boom]", platform_abort=True, abort_reason="tool_error")]
        text = report.case_report("Agent evals", {"results": res}, None, AGENT_REASONS)
        self.assertIn("Platform aborts: 3 (tool_error 2, consent_required 1). Turns with a history gap: 1.", text)
        self.assertIn("- `c#1` [platform abort: tool_error]: response does not match /84/", text)
        self.assertIn("- `c#2` [history gap]: [AGENT NOT RUN: no consent]", text)
        self.assertIn("- `c#3`: [AGENT FAILURE: boom]", text)

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


def ran(agent_version="2", model="gpt-4-1-mini", toolbox_version="3", commit="abc1234def", dirty=False, **kw):
    return {"timestamp": "t", "results": [r("a", True)], **kw, "provenance": {
        "repo": {"commit": commit, "dirty": dirty},
        "agent": {"name": "assistant", "version": agent_version, "model": model},
        "toolboxes": [{"name": "tb", "version": toolbox_version}]}}


def run(n, repetition, failure=None, latency=10.0, extra_calls=0):
    """Run `repetition` of agent turn c#n: passed, or failed with a (reason, message)."""
    if failure is None:
        return {**turn(n, "", "", repetition=repetition, latency_ms=latency, extra_calls=extra_calls),
                "passed": True, "failures": [], "reasons": []}
    return turn(n, *failure, repetition=repetition, latency_ms=latency, extra_calls=extra_calls)


class RepeatedRunTests(unittest.TestCase):
    MISS = ("missing_call", "expected call t {} not made")

    def test_a_repeated_turn_passes_only_if_every_run_passed(self):
        res = [run(1, 1, extra_calls=1), run(2, 1, latency=100.0), run(3, 1, self.MISS),
               run(1, 2), run(2, 2, self.MISS, latency=300.0), run(3, 2, self.MISS),
               run(1, 3, extra_calls=1), run(2, 3, latency=200.0), run(3, 3, self.MISS)]
        base = {"timestamp": "b", "results": [run(1, 1), run(2, 1), run(3, 1, self.MISS)]}
        text = report.case_report("Agent evals", {"timestamp": "t", "results": res}, base, AGENT_REASONS)
        self.assertIn("Run `t`, 3 runs of each case: **1/3 passed every run**\n\n"
                      "pass@1 56%, pass@3 67%, pass^3 33% (5/9 runs passed). "
                      "Flaky (passed some runs, not all): `c#2` 2/3.", text)
        self.assertIn("| a | 1 | 3 |", text)
        self.assertIn("Latency (per-case p50): median 10 ms, max 200 ms", text)  # c#2's median run took 200 ms
        self.assertIn("Extra tool calls (beyond those expected, over all runs): 2 (`c#1` 2)", text)
        # A failure that recurs over a turn's runs is listed once.
        self.assertIn(f"| `missing_call` | 2 | {AGENT_REASONS['missing_call']} |", text)
        self.assertIn("### `missing_call`\n\n- `c#3` [3/3 runs]: expected call t {} not made\n"
                      "- `c#2` [1/3 runs]: expected call t {} not made\n", text)
        # c#2 passed its one baseline run; c#3 failed then and now.
        self.assertIn("- Regressions: `c#2` (missing_call)\n- Fixed: none", text)

    def test_an_unrepeated_run_reports_extra_calls_even_when_there_are_none(self):
        text = report.case_report("Agent evals", {"results": [run(1, 1), run(2, 1)]}, None, AGENT_REASONS)
        self.assertIn("**2/2 passed**", text)
        self.assertIn("Extra tool calls (beyond those expected): none", text)
        self.assertNotIn("pass@", text)


class ProvenanceReportTests(unittest.TestCase):
    def test_says_what_the_run_ran_against_and_its_tokens(self):
        usage = {"input_tokens": 12000, "output_tokens": 345, "total_tokens": 12345}
        text = report.case_report("Agent evals", ran(toolbox_version=None, dirty=True, usage=usage), None)
        self.assertIn("Ran against agent `assistant` v2, model `gpt-4-1-mini`, toolbox `tb` (version unknown), "
                      "commit `abc1234` with uncommitted changes.", text)
        self.assertIn("Tokens: 12,345 (12,000 input, 345 output)", text)

    def test_baseline_section_lists_what_changed(self):
        base = ran(agent_version="1", model="gpt-4o", toolbox_version=None, commit="0123456789")
        base["provenance"]["toolboxes"].append({"name": "old", "version": "5"})
        text = report.case_report("Agent evals", ran(), base)
        # The baseline didn't know its toolbox version, so tb doesn't count as changed.
        self.assertIn("- Changed since the baseline: agent `assistant` v1 -> v2; model `gpt-4o` -> `gpt-4-1-mini`; "
                      "commit `0123456` -> `abc1234`; toolbox `old` v5 (only in the baseline)", text)
        self.assertIn("- Changed since the baseline: none", report.case_report("T", ran(), ran()))

    def test_runs_without_provenance_get_no_provenance_lines(self):
        old = {"timestamp": "t0", "results": [r("a", True)]}
        for cur, base in ((old, None), (ran(), old), (old, ran())):
            text = report.case_report("T", cur, base)
            self.assertNotIn("Changed since the baseline", text)
        self.assertNotIn("Ran against", report.case_report("T", old, None))

    def test_agent_reports_say_how_later_turns_carried_the_conversation(self):
        chained = ran(agent="assistant", history="chain")
        text = report.case_report("Agent evals", chained, ran(agent="assistant"))
        self.assertIn("History: chain (later turns continued the previous turn's stored response).", text)
        # Agent runs from before the choice replayed the conversation.
        self.assertIn("- Changed since the baseline: history replay -> chain", text)
        self.assertNotIn("History:", report.case_report("Tool tests", ran(), None))


class CloudReportTests(unittest.TestCase):
    OK = {"status": "completed", "passed": True, "label": "pass", "score": 5}
    BAD = {"status": "completed", "passed": False, "label": "fail", "score": 0}
    BROKEN = {"status": "error", "passed": None, "label": None, "score": None}
    NOT_APPLICABLE = {"status": "skipped", "passed": None, "label": "not_applicable", "score": None}

    @staticmethod
    def judged(n, verdict, scores, *failures, checks=None):
        """Turn c#n of a cloud evaluation: the judge's verdict with its (reason, message) failures, and the
        deterministic checks' result: passed, or the reasons they failed."""
        return {"conversation_id": "c", "turn_number": n, "repetition": 1, "category": "a", "verdict": verdict,
                "passed": verdict in ("pass", "skipped"), "failures": [m for _, m in failures],
                "reasons": [k for k, _ in failures], "scores": scores,
                "deterministic": None if checks is None else {"passed": checks is True,
                                                              "reasons": [] if checks is True else checks}}

    def report(self, **kw):
        ok, bad = self.OK, self.BAD
        results = [
            self.judged(1, "pass", {"task_adherence": ok, "tool_call_accuracy": ok}, checks=True),
            self.judged(2, "fail", {"task_adherence": bad, "tool_call_accuracy": self.NOT_APPLICABLE},
                        ("task_adherence", "task_adherence fail (score 0): a figure in no tool result"), checks=True),
            self.judged(3, "pass", {"task_adherence": ok, "tool_call_accuracy": ok}, checks=["missing_call"]),
            self.judged(4, "error", {"task_adherence": ok, "tool_call_accuracy": self.BROKEN},
                        ("evaluator_error", "tool_call_accuracy error: no tool definitions"), checks=True),
            self.judged(5, "fail", {"coherence": bad}, ("coherence", "coherence fail (score 0): rambles"))]
        run = {"kind": "cloud", "timestamp": "t", "source": "responses", "agent_name": "assistant",
               "agent_run": "run-a.json", "history": "chain", "max_turns": 2, "judge": "gpt-4-1-mini",
               "evaluators": ["task_adherence", "tool_call_accuracy", "coherence"],
               "eval_name": "toolbox-eval-responses-12345678", "eval_run_id": "r1", "duration_s": 25,
               "judge_usage": {"input_tokens": 9000, "output_tokens": 1000, "total_tokens": 10000},
               "agent_usage": None, "results": results, **kw}
        return report.case_report("Cloud evaluation", run, None, report.cloud_meanings(run), report.cloud_intro(run),
                                  report.judge_vs_checks(results))

    def test_says_what_was_judged_how_each_evaluator_judged_it_and_where_the_judge_and_checks_disagree(self):
        text = self.report()
        self.assertIn("Run `t`: **2/5 passed**\n\n"
                      "History: chain (later turns continued the previous turn's stored response).\n\n"
                      "Judged the stored responses of agent run `run-a.json`; the evaluators saw up to 2 turns of "
                      "each conversation.\n\n"
                      "Judge `gpt-4-1-mini`, evaluators `task_adherence`, `tool_call_accuracy`, `coherence`. "
                      "Evaluation `toolbox-eval-responses-12345678`, run `r1`, 25 s.\n\n"
                      "Verdicts: pass 2, fail 2, error 1. Judge tokens: 10,000 (9,000 input, 1,000 output).\n\n"
                      "| Evaluator | Passed | Failed | Errored | Not applicable |\n"
                      "| --- | ---: | ---: | ---: | ---: |\n"
                      "| `task_adherence` | 3 | 1 | 0 | 0 |\n"
                      "| `tool_call_accuracy` | 2 | 0 | 1 | 1 |\n"
                      "| `coherence` | 0 | 1 | 0 | 0 |\n", text)
        # c#4 errored and c#5 has no checks result, so neither is compared.
        self.assertIn("## Judge vs. deterministic checks\n\n"
                      "Of the 3 replies with a pass or fail verdict and a checks result, the judge and the checks "
                      "agree on 1.\n\n"
                      "| | Checks passed | Checks failed |\n| --- | ---: | ---: |\n"
                      "| Judge passed | 1 | 1 |\n| Judge failed | 1 | 0 |\n\n"
                      "- Failed by the judge only: `c#2` (task_adherence)\n"
                      "- Failed by the checks only: `c#3` (missing_call)\n", text)
        self.assertIn(f"| `task_adherence` | 1 | {CLOUD_REASONS['task_adherence']} |", text)
        self.assertIn("| `coherence` | 1 | The judge failed the turn on the built-in coherence evaluator. |", text)
        self.assertIn("- `c#4`: tool_call_accuracy error: no tool definitions", text)
        self.assertNotIn("Tokens:", text)

    def test_this_repos_evaluators_show_their_catalog_version(self):
        text = self.report(evaluators=["tool_use", "coherence"], evaluator_versions={"tool_use": "6"})
        self.assertIn("Judge `gpt-4-1-mini`, evaluators `tool_use` (this repo's, catalog version 6), `coherence`. ",
                      text)

    def test_a_target_run_says_which_cases_foundry_sent_and_the_agents_tokens(self):
        usage = {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}
        text = self.report(source="target", history=None, cases=["data/x.jsonl"], left_out=["c#2"], agent_usage=usage)
        self.assertIn("Foundry sent the first turn of each conversation in `data/x.jsonl` to agent `assistant` and "
                      "judged its reply. Later turns left out (a target run can't continue a conversation): 1.", text)
        self.assertIn("Agent tokens: 1,000 (900 input, 100 output).", text)
        self.assertNotIn("History:", text)


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

    def test_provenance_says_what_the_run_ran_against_and_what_changed(self):
        score = {"type": "prompt", "test_case_id": "tc1", "judge_status": "ok",
                 "verdict": {"dimensions": {"policy_violation": False}}}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.write_run(tmp, [score], [])
            base_dir = run_dir.parent / "run0"
            base_dir.mkdir()
            (base_dir / "scores.jsonl").write_text(json.dumps(score) + "\n", encoding="utf-8")
            for where, prov in ((run_dir, ran(agent="assistant", history="chain", requests=4, continued=2,
                                              usage={"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000})),
                                (base_dir, ran(agent_version="1", agent="assistant", history="replay"))):
                (where / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
            text = report.assert_report(run_dir, base_dir)
            without = report.assert_report(run_dir, None)
        self.assertIn("Ran against agent `assistant` v2, model `gpt-4-1-mini`, toolbox `tb` v3, commit `abc1234`.\n\n"
                      "History: chain (later turns continued the previous turn's stored response).\n\n"
                      "Agent calls: 4 (2 continued a stored response). Tokens: 1,000 (900 input, 100 output).", text)
        self.assertIn("Changed since the baseline `run0`: agent `assistant` v1 -> v2; history replay -> chain.", text)
        self.assertNotIn("Changed since", without)


if __name__ == "__main__":
    unittest.main()
