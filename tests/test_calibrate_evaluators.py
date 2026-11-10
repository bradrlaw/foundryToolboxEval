import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals import calibrate_evaluators as calibrate

PROJECT = "https://example.test/api/projects/p"
USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
TOOLS = [{"name": "get_customer", "parameters": {"type": "object"}}]


def case(name, notes="why", **expect):
    return {"case": name, "expect": expect, "notes": notes,
            "query": [{"role": "system", "content": "Answer from tool results."},
                      {"role": "user", "content": [{"type": "input_text", "text": f"question {name}"}]}],
            "response": [{"role": "assistant", "content": [{"type": "output_text", "text": f"answer {name}"}]}],
            "tool_definitions": TOOLS}


def score(name, value, passed=True, reason="fine", status="completed", error=None):
    sample = {"usage": USAGE} if status == "completed" else {"error": {"code": "UserError", "message": error}}
    return {"type": "azure_ai_evaluator", "name": name, "metric": name, "score": value, "passed": passed,
            "label": "pass" if passed else "fail", "status": status, "reason": None if error else reason,
            "threshold": 3, "sample": sample}


def item(name, *results):
    """An output item: the case it judged and each evaluator's result."""
    return {"object": "eval.run.output_item", "datasource_item": {"case": name}, "results": list(results)}


class MainTests(unittest.TestCase):
    CASES = [case("recall-ok", tool_use="pass", grounded_adherence="pass"),
             case("invented", notes="The 4% figure is in no tool result.", tool_use="pass", grounded_adherence="fail"),
             case("no-retry", tool_use="fail")]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cases = Path(tmp.name) / "cases.jsonl"
        self.cases.write_text("".join(json.dumps(c) + "\n" for c in self.CASES), encoding="utf-8")
        patches = [mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": PROJECT + "/"}),
                   mock.patch.object(calibrate, "load_dotenv"),
                   mock.patch("evals.auth.get_token", return_value="tok")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("FOUNDRY_EVAL_JUDGE_DEPLOYMENT", None)  # restored with the rest of the environment
        self.api = {}
        for name in ("catalog_version", "eval_id", "start", "wait", "output_items"):
            p = mock.patch.object(calibrate.EvalsAPI, name)
            self.api[name] = p.start()
            self.addCleanup(p.stop)
        self.api["catalog_version"].side_effect = lambda entry: (
            ("7", True) if entry["name"] == "toolbox_eval_tool_use" else ("4", False))
        self.api["eval_id"].return_value = ("e1", True)
        self.api["start"].side_effect = ["r1", "r2"]
        self.api["wait"].return_value = {"status": "completed"}

    def main(self, *argv):
        stdout, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(err):
            code = calibrate.main([str(self.cases), *argv])
        return code, stdout.getvalue(), err.getvalue()

    def test_lists_the_verdicts_that_differ_from_the_expected_ones(self):
        self.api["output_items"].return_value = [
            item("recall-ok", score("tool_use", 5), score("grounded_adherence", 5)),
            item("invented", score("tool_use", 5), score("grounded_adherence", 4, reason="Every figure is sourced.")),
            item("no-retry", score("tool_use", 2, passed=False))]
        code, out, _ = self.main()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"invented\s+grounded_adherence\s+fail\s+pass  MISMATCH")
        self.assertRegex(out, r"no-retry\s+tool_use\s+fail\s+fail\n")
        self.assertIn("Mismatches: 1 of 5 verdicts:\n"
                      "- invented grounded_adherence: expected fail, got pass (score 4): Every figure is sourced.", out)
        self.assertIn("Judge tokens: 550 (500 input, 50 output)", out)
        self.assertIn("tool_use: toolbox_eval_tool_use version 7 (registered now)", out)

    def test_the_judge_sees_the_turns_and_tools_but_not_the_expected_verdicts(self):
        self.api["output_items"].return_value = []
        self.main("--judge", "gpt-judge")
        definition = self.api["eval_id"].call_args.args[0]
        self.assertTrue(definition["name"].startswith("toolbox-eval-calibration-"))
        tests = definition["testing_criteria"]
        self.assertEqual([(c["evaluator_name"], c["evaluator_version"]) for c in tests],
                         [("toolbox_eval_tool_use", "7"), ("toolbox_eval_grounded_adherence", "4")])
        self.assertEqual({c["initialization_parameters"]["deployment_name"] for c in tests}, {"gpt-judge"})
        self.assertEqual(tests[0]["data_mapping"], {"query": "{{item.query}}", "response": "{{item.response}}",
                                                    "tool_definitions": "{{item.tool_definitions}}"})
        eval_id, body = self.api["start"].call_args.args
        self.assertEqual(eval_id, "e1")
        sent = [c["item"] for c in body["data_source"]["source"]["content"]]
        self.assertEqual(sent, [{k: c[k] for k in ("case", "query", "response", "tool_definitions")}
                                for c in self.CASES])

    def test_each_repetition_is_a_run_and_a_case_left_unjudged_is_a_mismatch(self):
        judged = [item("recall-ok", score("tool_use", 4), score("grounded_adherence", 5)),
                  item("invented", score("tool_use", 5), score("grounded_adherence", 1, passed=False)),
                  item("no-retry", score("tool_use", 1, passed=False))]
        unjudged = [judged[0], item("invented", score("tool_use", 5),
                                    score("grounded_adherence", None, passed=None, status="error", error="timeout"))]
        self.api["output_items"].side_effect = [judged, unjudged]
        code, out, _ = self.main("--repetitions", "2")
        self.assertEqual(code, 1)
        self.assertEqual(self.api["start"].call_count, 2)
        self.assertRegex(out, r"recall-ok\s+tool_use\s+pass\s+pass pass\n")
        self.assertRegex(out, r"invented\s+grounded_adherence\s+fail\s+fail error  MISMATCH")
        self.assertIn("Mismatches: 2 of 10 verdicts:\n"
                      "- invented grounded_adherence, run 2: expected fail, got error: timeout\n"
                      "- no-retry tool_use, run 2: expected fail, got error: the run has no result for this case", out)

    def test_exits_0_when_every_verdict_matches(self):
        self.api["output_items"].return_value = [
            item("recall-ok", score("tool_use", 5), score("grounded_adherence", 5)),
            item("invented", score("tool_use", 5), score("grounded_adherence", 2, passed=False)),
            item("no-retry", score("tool_use", 1, passed=False))]
        code, out, _ = self.main("--evaluators", "tool_use")
        self.assertEqual(code, 0)
        self.assertIn("Mismatches: 0 of 3 verdicts\n", out)
        self.assertNotIn("grounded_adherence", out)
        self.assertEqual([c.args[0]["name"] for c in self.api["catalog_version"].call_args_list],
                         ["toolbox_eval_tool_use"])

    def test_setup_problems_exit_2_before_any_run(self):
        self.cases.write_text(json.dumps(case("only-tool-use", tool_use="pass")) + "\n", encoding="utf-8")
        code, _, err = self.main("--evaluators", "grounded_adherence")
        self.assertEqual((code, err), (2, "error: no case expects a verdict from grounded_adherence\n"))
        with mock.patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": ""}):
            code, _, err = self.main("--evaluators", "tool_use")
        self.assertEqual(code, 2)
        self.assertIn("set FOUNDRY_PROJECT_ENDPOINT", err)
        self.api["catalog_version"].assert_not_called()
        with self.assertRaises(SystemExit) as stopped, contextlib.redirect_stderr(io.StringIO()):
            calibrate.main(["--evaluators", "task_adherence"])
        self.assertEqual(stopped.exception.code, 2)

    def test_a_failed_run_exits_2(self):
        self.api["wait"].return_value = {"status": "failed", "error": {"code": "x", "message": "quota exceeded"}}
        code, _, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn("the evaluation run failed (evaluation e1, run r1): quota exceeded", err)
        self.api["output_items"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
