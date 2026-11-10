"""Run files and their results, shared by the runners and scripts/report.py."""
from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def result_id(r: dict) -> str:
    """The case a result belongs to: its case_id (tool tests) or conversation#turn (agent turns)."""
    return r.get("case_id") or f"{r['conversation_id']}#{r['turn_number']}"


def by_case(results: list[dict]) -> dict[str, list[dict]]:
    """Results grouped by case, in the order cases first ran: one result per run of the case."""
    out: dict[str, list[dict]] = {}
    for r in results:
        out.setdefault(result_id(r), []).append(r)
    return out


def pass_rates(results: list[dict]) -> dict | None:
    """How repeated cases fared, or None when no case ran more than once (agent evals run with --repetitions).

    k is the most runs a case had. pass@1 is the mean share of its runs each case passed (the chance that a single
    run passes), pass@k the share of cases that passed at least one run, and pass^k the share that passed every run.
    `flaky` lists the cases that passed some runs but not all, as (case, passed runs, runs)."""
    cases = by_case(results)
    k = max((len(runs) for runs in cases.values()), default=0)
    if k < 2:
        return None
    passed = {c: sum(bool(r["passed"]) for r in runs) for c, runs in cases.items()}
    share = {c: passed[c] / len(runs) for c, runs in cases.items()}
    return {"k": k, "pass@1": sum(share.values()) / len(cases),
            "pass@k": sum(p > 0 for p in passed.values()) / len(cases),
            "pass^k": sum(s == 1 for s in share.values()) / len(cases),
            "flaky": [(c, passed[c], len(runs)) for c, runs in cases.items() if 0 < passed[c] < len(runs)]}


def pass_rate_text(rates: dict) -> str:
    k = rates["k"]
    return f"pass@1 {rates['pass@1']:.0%}, pass@{k} {rates['pass@k']:.0%}, pass^{k} {rates['pass^k']:.0%}"


def extra_calls_by_case(results: list[dict]) -> dict[str, int]:
    """Tool calls beyond those expected, per case and summed over its runs, for the cases that made any."""
    out: dict[str, int] = {}
    for r in results:
        if r.get("extra_calls"):
            out[result_id(r)] = out.get(result_id(r), 0) + r["extra_calls"]
    return out


def score_outcome(score: dict) -> str:
    """What one evaluator made of a turn in a cloud evaluation: passed, failed, errored (it couldn't judge the turn)
    or not applicable (as tool_call_accuracy on a turn without tool calls)."""
    if score.get("status") == "skipped" or score.get("label") == "not_applicable":
        return "not applicable"
    if score.get("status") != "completed" or not isinstance(score.get("passed"), bool):
        return "errored"
    return "passed" if score["passed"] else "failed"


def evaluator_tally(results: list[dict]) -> dict[str, Counter]:
    """Per evaluator of a cloud evaluation, how many results had each outcome (score_outcome)."""
    out: dict[str, Counter] = {}
    for r in results:
        for name, score in (r.get("scores") or {}).items():
            out.setdefault(name, Counter())[score_outcome(score)] += 1
    return out


def agreement(results: list[dict]) -> dict | None:
    """How a cloud evaluation's verdicts compare with the deterministic checks of the agent run it judged, over the
    results with both: a pass or fail verdict, and the checks' result. Counts every run of a repeated turn and lists
    the turns where the two disagree. None when no result has both."""
    both = [r for r in results if r.get("deterministic") and r.get("verdict") in ("pass", "fail")]
    if not both:
        return None
    pairs = [(r["verdict"] == "pass", bool(r["deterministic"]["passed"])) for r in both]

    def turns(judge: bool, checks: bool) -> list[str]:
        return list(dict.fromkeys(result_id(r) for r, pair in zip(both, pairs) if pair == (judge, checks)))

    cells = Counter(pairs)
    return {"results": len(both), "agree": cells[(True, True)] + cells[(False, False)],
            "both_passed": cells[(True, True)], "both_failed": cells[(False, False)],
            "judge_only": cells[(False, True)], "checks_only": cells[(True, False)],
            "judge_only_turns": turns(False, True), "checks_only_turns": turns(True, False)}


def write_run(folder: Path, record: Callable[[str], dict]) -> Path:
    """Write record(timestamp) to a new folder/run-<UTC timestamp>.json and return its path.

    Never overwrites an earlier run: if another run already took this second's name, wait for the next second,
    so names stay unique and sort in run order (scripts/report.py picks the last one)."""
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        text = json.dumps(record(stamp), indent=2)
        try:
            with (folder / f"run-{stamp}.json").open("x", encoding="utf-8") as f:
                f.write(text)
            return folder / f"run-{stamp}.json"
        except FileExistsError:
            time.sleep(0.1)
