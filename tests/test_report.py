import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("report", ROOT / "scripts" / "report.py")
report = importlib.util.module_from_spec(spec)
sys.modules["report"] = report
spec.loader.exec_module(report)


def r(cid, passed, cat="a", p50=100.0):
    return {"case_id": cid, "category": cat, "passed": passed, "failures": [] if passed else ["x"], "latency_p50_ms": p50}


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


if __name__ == "__main__":
    unittest.main()
