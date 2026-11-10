import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from evals.figures import figures, numbers, ungrounded

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_figures", ROOT / "scripts" / "check_figures.py")
check_figures = importlib.util.module_from_spec(spec)
sys.modules["check_figures"] = check_figures
spec.loader.exec_module(check_figures)

QUOTE = ('{"ticker": "CTSO", "price": 84.12, "change_pct": -2.3, "market_cap_usd": 12400000000, '
         '"as_of": "2026-11-10T14:30:00Z"}')


class FigureTests(unittest.TestCase):
    def test_a_figure_keeps_its_currency_scale_and_percent(self):
        text = "Down 2.3% to $84.12, a $12.4 billion company at 17.3x earnings, with 1,250,000 USD in contracts."
        self.assertEqual([f.text for f in figures(text)], ["2.3%", "$84.12", "$12.4 billion", "17.3x", "1,250,000"])

    def test_identifiers_dates_years_counts_and_compounds_are_not_figures(self):
        text = ("Customer C1001's Q3 note from 2026-11-10 at 14:30 UTC (Nov 10, 2026) lists 3 risks for FY2025 and "
                "2026, and a 52-week high.")
        self.assertEqual(figures(text), [])

    def test_a_figure_matches_a_tool_number_at_the_precision_written(self):
        self.assertEqual(ungrounded("CTSO is at $84.12 (about $84), down 2.3%, worth $12.4 billion or 12,400 million.",
                                    [QUOTE]), [])
        self.assertEqual(ungrounded("CTSO is at $84.2, worth $12.5 billion, down 2.3%.", [QUOTE]),
                         ["$84.2", "$12.5 billion"])
        self.assertEqual(ungrounded("Growth is 4.2%.", ['{"growth": 0.042}']), [])

    def test_tool_numbers_come_from_json_and_text_but_not_dates_or_identifiers(self):
        sources = [QUOTE, "Error executing tool get_customer: no customer C9999", "Sales reached 3.2 billion",
                   {"n": [1.5, True]}]
        self.assertEqual(numbers(sources), {84.12, 2.3, 12400000000.0, 3.2, 3.2e9, 1.5})

    def test_thousands_can_be_grouped_with_spaces(self):
        record = '{"annual_contract_value_usd": 1250000, "employees": 12400}'
        self.assertEqual(ungrounded("It pays $1 250 000 a year and has 12\u202f400 staff.", [record]), [])
        self.assertEqual(ungrounded("It pays USD 1 250 000.", ["Contract: 1\u00a0250\u00a0000 USD"]), [])
        # Below a million, an ordinary space can separate two numbers.
        self.assertEqual([f.text for f in figures("Rows 12 400, or 12\u2009400")], ["12", "400", "12\u2009400"])


def say(role, text):
    return {"edit": {"type": "add_message", "message": {"role": role, "content": text}}}


def tool(result):
    return {"edit": {"type": "tool_call", "tool_name": "sample___get_stock_quote", "tool_result": result}}


class CheckFiguresTests(unittest.TestCase):
    TRANSCRIPTS = [  # (events, judge flagged figure_grounding)
        ([say("user", "CTSO price?"), tool(QUOTE), say("assistant", "It's $84.12.")], False),
        ([say("user", "My notes say $90."), tool(QUOTE), say("assistant", "It's $84.12, not $90.")], False),
        ([say("user", "Estimate CONT's P/E."), tool("Unknown ticker 'CONT'"), say("assistant", "Roughly 18.5.")], True),
        ([say("user", "Is that quote current?"), say("assistant", "Yes, it's live.")], True),
    ]

    def run_dir(self, root: Path) -> Path:
        run = root / "suite" / "run"
        run.mkdir(parents=True)
        inference, scores = [], []
        for i, (events, flagged) in enumerate(self.TRANSCRIPTS, 1):
            key = {"type": "prompt", "test_case_id": f"test_case_{i:06d}"}
            inference.append({**key, "events": events})
            scores.append({**key, "judge_status": "ok", "verdict": {"dimensions": {"figure_grounding": flagged}}})
        inference.append({"type": "prompt", "test_case_id": "test_case_000005", "stop_reason": "target_error",
                          "events": [say("assistant", "Error 503")]})
        for name, rows in (("inference_set.jsonl", inference), ("scores.jsonl", scores)):
            (run / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return run

    def test_the_check_is_set_against_the_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.run_dir(Path(tmp))
            strict, lenient = check_figures.compare(run), check_figures.compare(run, lenient=True)
        self.assertEqual([c["figures"] for c in strict["cases"]], [[], ["$90"], ["18.5"], []])
        self.assertEqual(check_figures.table([strict])[2],
                         "| suite/run | 4 | 2 | 2 | 1 | 1 | 1 | 1/2 (50%) | 1/2 (50%) |")
        # Lenient accepts the user's $90, which the reply repeated to correct it.
        self.assertEqual(check_figures.table([lenient])[2],
                         "| suite/run | 4 | 1 | 2 | 1 | 0 | 1 | 1/2 (50%) | 0/2 (0%) |")


if __name__ == "__main__":
    unittest.main()
