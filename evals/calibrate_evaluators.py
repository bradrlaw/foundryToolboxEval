"""Check this repo's Foundry evaluators against cases with known verdicts, and list the verdicts that differ.

data/evaluator-calibration/*.jsonl holds turns as the evaluators see them: the agent's instructions, the conversation
with its tool calls and results, the judged turn and the agent's tool definitions. Each case gives the verdict that
tool_use and grounded_adherence should reach, for example a figure recalled from an earlier turn's tool result
(pass), one invented (fail), one taken from the user (fail), or a misspelled lookup the agent didn't retry (tool_use
fails, grounded_adherence passes). Run it after changing a judge prompt (evals/evaluators/<name>.md) or the judge
model, before trusting the evaluators on agent runs.

The evaluators are registered in the project's evaluator catalog as evals/run_cloud_eval.py registers them, with a new
version when a prompt changes, and each repetition is one evaluation run that judges every case. A verdict that
differs from the expected one is a mismatch, and so is a case the evaluator couldn't judge.

Usage:
  python -m evals.calibrate_evaluators [CASES ...] [--judge DEPLOYMENT] [--evaluators NAMES] [--repetitions N]

Exit codes: 0 every verdict matched, 1 some didn't, 2 setup error, an HTTP error from the evaluation API, or a run that
failed, was canceled or didn't end within --timeout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from http.client import HTTPException

from azure.core.exceptions import ClientAuthenticationError
from dotenv import load_dotenv

from evals import auth
from evals.cases import ROOT, CaseError, load_cases
from evals.results import score_outcome
from evals.run_agent_evals import at_least_one
from evals.run_cloud_eval import (CATALOG_PREFIX, CUSTOM_EVALUATORS, CUSTOM_THRESHOLD, DEFAULT_JUDGE, ENDED,
                                  CloudEvalError, EvalsAPI, custom_entry, error_text, evaluator_list, judge_usage,
                                  scores, tokens_text)

DEFAULT_CASES = ROOT / "data" / "evaluator-calibration" / "*.jsonl"
ITEM_FIELDS = ("case", "query", "response", "tool_definitions")
ITEM_SCHEMA = {"type": "object", "required": list(ITEM_FIELDS), "properties": {
    "case": {"type": "string"}, "query": {"type": "array"}, "response": {"type": "array"},
    "tool_definitions": {"type": "array"}}}
MAPPING = {"query": "{{item.query}}", "response": "{{item.response}}", "tool_definitions": "{{item.tool_definitions}}"}
VERDICTS = {"passed": "pass", "failed": "fail", "errored": "error", "not applicable": "n/a"}
MAX_REASON = 300


def evaluator_names(text: str) -> tuple[str, ...]:
    names = evaluator_list(text)
    if unknown := [n for n in names if n not in CUSTOM_EVALUATORS]:
        raise argparse.ArgumentTypeError(f"only this repo's evaluators ({', '.join(CUSTOM_EVALUATORS)}) have "
                                         f"calibration cases, not {', '.join(unknown)}")
    return names


def calibration_definition(evaluators: tuple[str, ...], judge: str, versions: dict[str, str]) -> dict:
    """The evaluation that judges calibration cases with this repo's evaluators at their catalog `versions`; named
    for its settings, so later runs with the same settings reuse it."""
    tests = [{"type": "azure_ai_evaluator", "name": name, "evaluator_name": CATALOG_PREFIX + name,
              "evaluator_version": versions[name],
              "initialization_parameters": {"deployment_name": judge, "threshold": CUSTOM_THRESHOLD},
              "data_mapping": dict(MAPPING)} for name in evaluators]
    body = {"data_source_config": {"type": "custom", "include_sample_schema": False, "item_schema": ITEM_SCHEMA},
            "testing_criteria": tests}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
    return {"name": f"toolbox-eval-calibration-{digest}", **body}


def calibration_run(name: str, cases: list[dict]) -> dict:
    """A run that judges every case, sent inline."""
    content = [{"item": {k: c[k] for k in ITEM_FIELDS}} for c in cases]
    return {"name": name, "data_source": {"type": "jsonl", "source": {"type": "file_content", "content": content}}}


def verdicts(cases: list[dict], items: list[dict], evaluators: tuple[str, ...]) -> list[dict]:
    """For each case and each of `evaluators` it expects a verdict from: the verdict expected and the one the run's
    output items give: pass, fail, n/a, or error (the evaluator couldn't judge the case, or the run has no result)."""
    by_case: dict[str, dict] = {}
    for item in items:
        source = item.get("datasource_item")
        if isinstance(source, dict) and isinstance(source.get("case"), str):
            by_case.setdefault(source["case"], item)
    out = []
    for case in cases:
        item = by_case.get(case["case"])
        found = scores(item) if item else {}
        for name in (n for n in evaluators if n in case["expect"]):
            if score := found.get(name):
                got, reason = VERDICTS[score_outcome(score)], score["reason"] or "no reason given"
            elif item:
                got, reason = "error", f"the run's result for this case has no {name} score"
            else:
                got, reason = "error", "the run has no result for this case"
            out.append({"case": case["case"], "evaluator": name, "expected": case["expect"][name], "got": got,
                        "score": score["score"] if score else None, "reason": reason})
    return out


def table(runs: list[list[dict]]) -> list[str]:
    """Each case's expected verdict from each evaluator, and the verdict of every run."""
    rows: dict[tuple[str, str], tuple[str, list[str]]] = {}
    for run in runs:
        for v in run:
            rows.setdefault((v["case"], v["evaluator"]), (v["expected"], []))[1].append(v["got"])
    width = max([len("Case")] + [len(case) for case, _ in rows])
    lines = [f"{'Case':{width}}  {'Evaluator':18}  {'Expected':8}  Verdicts"]
    for (case, name), (expected, got) in rows.items():
        flag = "  MISMATCH" if any(g != expected for g in got) else ""
        lines.append(f"{case:{width}}  {name:18}  {expected:8}  {' '.join(got)}{flag}")
    return lines


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_REASON else text[:MAX_REASON - 3] + "..."


def mismatches(runs: list[list[dict]]) -> list[str]:
    """A line for each verdict that differs from the expected one, with the evaluator's score and reason."""
    out = []
    for n, run in enumerate(runs, 1):
        for v in run:
            if v["got"] != v["expected"]:
                score = "" if v["score"] is None else f" (score {v['score']})"
                where = f", run {n}" if len(runs) > 1 else ""
                out.append(f"- {v['case']} {v['evaluator']}{where}: expected {v['expected']}, got {v['got']}{score}: "
                           f"{_clip(v['reason'])}")
    return out


def _setup_error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", nargs="*", default=[str(DEFAULT_CASES)],
                    help="calibration JSONL files or glob patterns (default: data/evaluator-calibration/*.jsonl)")
    ap.add_argument("--judge", help="model deployment the evaluators use (default: $FOUNDRY_EVAL_JUDGE_DEPLOYMENT, "
                                    "else gpt-4-1-mini)")
    ap.add_argument("--evaluators", type=evaluator_names, default=tuple(CUSTOM_EVALUATORS), metavar="NAMES",
                    help="comma-separated evaluators to calibrate (default: " + ",".join(CUSTOM_EVALUATORS) + ")")
    ap.add_argument("--repetitions", type=at_least_one, default=1, metavar="N",
                    help="judge every case N times, one evaluation run each (default 1)")
    ap.add_argument("--timeout", type=at_least_one, default=30, metavar="MINUTES",
                    help="stop waiting for an evaluation run after this long (default 30)")
    args = ap.parse_args(argv)
    try:
        cases = load_cases(args.cases, "calibration")
    except CaseError as e:
        print("invalid cases:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    if not cases:
        return _setup_error("no cases found")
    if unused := [n for n in args.evaluators if not any(n in c["expect"] for c in cases)]:
        return _setup_error(f"no case expects a verdict from {', '.join(unused)}")

    load_dotenv(ROOT / ".env")
    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not project:
        return _setup_error("set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
    judge = args.judge or os.environ.get("FOUNDRY_EVAL_JUDGE_DEPLOYMENT") or DEFAULT_JUDGE
    print(f"Calibrating {', '.join(args.evaluators)} on {len(cases)} cases, {args.repetitions} "
          f"run{'s' if args.repetitions > 1 else ''} (judge {judge})")
    api = EvalsAPI(project)
    runs, items = [], []
    try:
        auth.get_token()
        versions = {}
        for name in args.evaluators:
            versions[name], registered = api.catalog_version(custom_entry(name))
            print(f"  {name}: {CATALOG_PREFIX}{name} version {versions[name]}"
                  + (" (registered now)" if registered else ""))
        definition = calibration_definition(args.evaluators, judge, versions)
        eval_id, created = api.eval_id(definition)
        print(f"Evaluation {definition['name']} ({'created' if created else 'reused'})")
        for n in range(1, args.repetitions + 1):
            run_id = api.start(eval_id, calibration_run(f"calibration {n} of {args.repetitions}", cases))
            print(f"Run {n} of {args.repetitions}, {run_id}:", flush=True)
            run = api.wait(eval_id, run_id, args.timeout * 60)
            if run.get("status") not in ENDED:
                return _setup_error(f"the evaluation run is still {run.get('status')} after {args.timeout} minutes "
                                    f"and goes on in Foundry (evaluation {eval_id}, run {run_id})")
            if run.get("status") != "completed":
                why = error_text(run.get("error")) or "no error given"
                return _setup_error(f"the evaluation run {run.get('status')} (evaluation {eval_id}, run {run_id}): "
                                    f"{why}")
            found = api.output_items(eval_id, run_id)
            items += found
            runs.append(verdicts(cases, found, args.evaluators))
    except ClientAuthenticationError as e:
        return _setup_error(f"cannot get a token for {auth.FOUNDRY_SCOPE} (signed in with az login?): {e}")
    except (CloudEvalError, OSError, HTTPException) as e:
        return _setup_error(str(e) if isinstance(e, CloudEvalError) else f"{type(e).__name__}: {e}")

    print("\n" + "\n".join(table(runs)))
    wrong = mismatches(runs)
    total = sum(map(len, runs))
    print(f"\nMismatches: {len(wrong)} of {total} verdicts" + (":" if wrong else ""))
    for line in wrong:
        print(line)
    if used := judge_usage(items):
        print(f"Judge tokens: {tokens_text(used)}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
