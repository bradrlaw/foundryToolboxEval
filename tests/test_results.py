"""evals/results.py: run files, and how repeated cases fared."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from evals import results


class PassRateTests(unittest.TestCase):
    @staticmethod
    def runs(cid, *passed):
        return [{"case_id": cid, "passed": p} for p in passed]

    def test_pass_rates_over_repeated_runs(self):
        rates = results.pass_rates(self.runs("a", True, True, True) + self.runs("b", True, False, True)
                                   + self.runs("c", False, False, False))
        self.assertEqual((rates["k"], rates["flaky"]), (3, [("b", 2, 3)]))
        self.assertAlmostEqual(rates["pass@1"], (1 + 2 / 3 + 0) / 3)
        self.assertAlmostEqual(rates["pass@k"], 2 / 3)
        self.assertAlmostEqual(rates["pass^k"], 1 / 3)
        self.assertEqual(results.pass_rate_text(rates), "pass@1 56%, pass@3 67%, pass^3 33%")

    def test_no_pass_rates_when_no_case_was_repeated(self):
        self.assertIsNone(results.pass_rates(self.runs("a", True) + self.runs("b", False)))


class CloudVerdictTests(unittest.TestCase):
    @staticmethod
    def judged(cid, verdict, checks_passed, repetition=1):
        checks = None if checks_passed is None else {"passed": checks_passed}
        return {"case_id": cid, "verdict": verdict, "deterministic": checks, "repetition": repetition}

    def test_an_evaluator_that_gave_no_verdict_errored_unless_it_did_not_apply(self):
        outcome = results.score_outcome
        self.assertEqual(outcome({"status": "completed", "passed": False, "label": "fail"}), "failed")
        self.assertEqual(outcome({"status": "completed", "passed": None, "label": "pass"}), "errored")
        self.assertEqual(outcome({"status": "error", "passed": None, "label": None}), "errored")
        self.assertEqual(outcome({"status": "skipped", "passed": None, "label": "not_applicable"}), "not applicable")
        tally = results.evaluator_tally([{"scores": {"e": {"status": "completed", "passed": True}}},
                                         {"scores": {"e": {"status": "error"}}}, {"scores": {}}])
        self.assertEqual(tally, {"e": {"passed": 1, "errored": 1}})

    def test_agreement_compares_pass_or_fail_verdicts_with_the_checks(self):
        found = results.agreement([
            self.judged("a", "pass", True), self.judged("b", "fail", True), self.judged("b", "fail", True, 2),
            self.judged("c", "pass", False), self.judged("d", "fail", False),
            self.judged("e", "error", True), self.judged("f", "not_judged", False), self.judged("g", "pass", None)])
        self.assertEqual(found, {"results": 5, "agree": 2, "both_passed": 1, "both_failed": 1, "judge_only": 2,
                                 "checks_only": 1, "judge_only_turns": ["b"], "checks_only_turns": ["c"]})
        self.assertIsNone(results.agreement([self.judged("e", "error", True), self.judged("g", "pass", None)]))


class WriteRunTests(unittest.TestCase):
    def test_runs_in_the_same_second_get_unique_names_in_run_order(self):
        t0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 11, 7, 12, 0, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(results, "datetime") as clock, \
                mock.patch.object(results.time, "sleep"):
            clock.now.side_effect = [t0, t0, t1]
            folder = Path(tmp) / "runs"
            first = results.write_run(folder, lambda stamp: {"timestamp": stamp, "run": 1})
            second = results.write_run(folder, lambda stamp: {"timestamp": stamp, "run": 2})
            self.assertEqual([first.name, second.name], ["run-20261107T120000Z.json", "run-20261107T120001Z.json"])
            self.assertEqual(sorted(folder.iterdir()), [first, second])
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"timestamp": "20261107T120000Z", "run": 1})
            self.assertEqual(json.loads(second.read_text(encoding="utf-8")), {"timestamp": "20261107T120001Z", "run": 2})


if __name__ == "__main__":
    unittest.main()
