"""Run the deterministic figure check (evals/figures.py) over ASSERT transcripts, and set it against the judge.

Usage: python scripts/check_figures.py RUN_DIR [RUN_DIR ...] [--lenient] [--details]

Each RUN_DIR is an ASSERT run, results/assert/results/<suite>/<run>. Every agent reply in a transcript is checked
against the tool results before it in the transcript, and the transcript is flagged if a reply states a figure that
matches none of them. Where the judge scored figure_grounding, the table compares the two: transcripts both flagged,
those only the check flagged (a false alarm, or a violation the judge missed), and those only the judge flagged (a
violation without a number, or with a number some tool did return). Catch rate: of the transcripts the judge flagged,
the share the check flagged too. False alarms: of those the judge passed, the share the check flagged.

--lenient also accepts numbers the user wrote, so a reply that repeats the user's figure, to correct it or to adopt it,
isn't flagged. --details lists each flagged or disagreeing transcript with its figures and the judge's reason.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals.figures import ungrounded

DIMENSION = "figure_grounding"
MAX_REASON = 240


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_transcript(events: list[dict], lenient: bool = False) -> list[str]:
    """The figures in the transcript's agent replies that match no tool result before them (nor, with `lenient`, a
    number the user wrote)."""
    sources: list[object] = []
    found: list[str] = []
    for event in events:
        edit = event.get("edit") or {}
        if edit.get("type") == "tool_call":
            sources.append(edit.get("tool_result"))
            continue
        message = (edit.get("message") or {}) if edit.get("type") == "add_message" else {}
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if message.get("role") == "user" and lenient:
            sources.append(content)
        elif message.get("role") == "assistant":
            found += ungrounded(content, sources)
    return list(dict.fromkeys(found))


def judged(run_dir: Path) -> dict[tuple, dict]:
    """The judge's scored verdicts, by (case type, test case id)."""
    return {(r.get("type"), r.get("test_case_id")): r for r in _jsonl(run_dir / "scores.jsonl")
            if r.get("judge_status") == "ok" and (r.get("verdict") or {}).get("dimensions")}


def compare(run_dir: Path, lenient: bool = False) -> dict:
    """Each answered transcript with the check's figures and, where scored, the judge's figure_grounding verdict."""
    verdicts = judged(run_dir)
    cases = []
    for rec in _jsonl(run_dir / "inference_set.jsonl"):
        if rec.get("stop_reason") == "target_error":
            continue
        key = (rec.get("type"), rec.get("test_case_id"))
        verdict = (verdicts.get(key) or {}).get("verdict") or {}
        dims = verdict.get("dimensions") or {}
        reason = (verdict.get("dimension_justifications") or {}).get(DIMENSION) or verdict.get("justification")
        cases.append({"case": f"{key[1]} ({key[0]})", "figures": check_transcript(rec.get("events") or [], lenient),
                      "judge": dims.get(DIMENSION), "reason": reason or ""})
    return {"run": f"{run_dir.parent.name}/{run_dir.name}", "cases": cases}


def tally(cases: list[dict]) -> dict:
    scored = [c for c in cases if c["judge"] is not None]
    both = sum(bool(c["figures"]) and c["judge"] for c in scored)
    judge = sum(bool(c["judge"]) for c in scored)
    check_only = sum(bool(c["figures"]) and not c["judge"] for c in scored)
    return {"transcripts": len(cases), "flagged": sum(bool(c["figures"]) for c in cases), "scored": len(scored),
            "judge": judge, "both": both, "check_only": check_only, "judge_only": judge - both,
            "clean": len(scored) - judge}


def _rate(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.0%})" if d else "n/a"


def table(results: list[dict]) -> list[str]:
    lines = ["| Run | Transcripts | Check flagged | Judge flagged | Both | Check only | Judge only | Catch rate | "
             "False alarms |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in results:
        t = tally(r["cases"])
        if not t["scored"]:
            lines.append(f"| {r['run']} | {t['transcripts']} | {t['flagged']} | n/a | | | | | |")
            continue
        lines.append(f"| {r['run']} | {t['transcripts']} | {t['flagged']} | {t['judge']} | {t['both']} | "
                     f"{t['check_only']} | {t['judge_only']} | {_rate(t['both'], t['judge'])} | "
                     f"{_rate(t['check_only'], t['clean'])} |")
    return lines


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 3] + "..."


def details(results: list[dict]) -> list[str]:
    lines = []
    for r in results:
        shown = [c for c in r["cases"] if c["figures"] or c["judge"]]
        if not shown:
            continue
        lines += ["", f"## {r['run']}", ""]
        for c in shown:
            judge = {True: "judge flagged", False: "judge passed", None: "not judged"}[c["judge"]]
            figures = ", ".join(c["figures"]) or "none"
            lines.append(f"- {c['case']}: {judge}; check: {figures}")
            if c["judge"]:
                lines.append(f"  - judge: {_clip(c['reason'])}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", type=Path, help="ASSERT run directories")
    ap.add_argument("--lenient", action="store_true", help="also accept numbers the user wrote")
    ap.add_argument("--details", action="store_true", help="list flagged and disagreeing transcripts")
    args = ap.parse_args(argv)
    missing = [str(r) for r in args.runs if not (r / "inference_set.jsonl").is_file()]
    if missing:
        print("no inference_set.jsonl in: " + ", ".join(missing), file=sys.stderr)
        return 2
    results = [compare(r, args.lenient) for r in args.runs]
    print("\n".join(table(results) + (details(results) if args.details else [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
