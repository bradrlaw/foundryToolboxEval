import re
import unittest
from pathlib import Path

from evals.reasons import AGENT_REASONS, TOOL_REASONS, summary_line, tally
from evals.toolbox_errors import AGENT_NOT_RUN

ROOT = Path(__file__).resolve().parent.parent


class ReasonTests(unittest.TestCase):
    def test_tally_counts_a_case_once_per_reason(self):
        results = [{"reasons": ["assertion", "assertion", "latency"]}, {"reasons": ["assertion"]}, {"reasons": []}, {}]
        self.assertEqual(tally(results), {"assertion": 2, "latency": 1})
        self.assertEqual(summary_line(results), "Failed by reason: assertion 2, latency 1")
        self.assertEqual(summary_line([{"reasons": []}]), "")

    def test_reasons_the_runners_write_out_are_described(self):
        for path, described in ((ROOT / "tooltests" / "run.py", TOOL_REASONS),
                                (ROOT / "evals" / "run_agent_evals.py", AGENT_REASONS)):
            written = set(re.findall(r'Failure\(\s*"(\w+)"', path.read_text(encoding="utf-8")))
            self.assertTrue(written, path)
            self.assertLessEqual(written, set(described), path.name)
        self.assertLessEqual(AGENT_NOT_RUN, set(AGENT_REASONS))

    def test_tool_outcomes_are_described(self):
        # A call that ends in an unexpected outcome fails with the outcome as its reason.
        sources = ((ROOT / "tooltests" / "run.py", r'Outcome\(\s*"(\w+)"|\.outcome = "(\w+)"|error_result\([^,]+, "(\w+)"'),
                   (ROOT / "evals" / "toolbox_errors.py", r'SourceFailure\(\s*"(\w+)"(?: if \w+ else "(\w+)")?'))
        outcomes = {name for path, pattern in sources
                    for groups in re.findall(pattern, path.read_text(encoding="utf-8")) for name in groups if name}
        self.assertLessEqual({"tool_error", "transport_error", "runner_error", "upstream_error"}, outcomes)
        self.assertLessEqual(outcomes - {"ok"}, set(TOOL_REASONS))


if __name__ == "__main__":
    unittest.main()
