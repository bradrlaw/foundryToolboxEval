"""Deterministic agent evals: run JSONL conversations against the Foundry agent and score tool calls.

Complements ASSERT (which judges behavior but does not assert exact tool calls/params).

Usage: python -m evals.run_agent_evals data/agent-evals/sample-toolbox.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from evals import telemetry  # noqa: E402


def _value_matches(expected, actual) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        if expected.startswith("re:"):
            return re.search(expected[3:], actual, re.I) is not None
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


def _call_matches(expected: dict, actual: dict) -> bool:
    if actual["name"] != expected["tool"]:
        return False
    if actual.get("arguments") is None:  # platform abort: only the tool name is known
        return True
    args = actual["arguments"]
    return all(k in args and _value_matches(v, args[k]) for k, v in expected.get("params", {}).items())


def score_turn(case: dict, result: dict) -> list[str]:
    """Return a list of failure reasons (empty = pass)."""
    calls = result["tool_calls"]
    criteria = case["pass_criteria"]
    failures: list[str] = []

    if criteria in ("tool_called_correctly", "both"):
        pos = 0
        for exp in case["expected_tool_calls"]:
            pool = calls[pos:] if case.get("ordered") else calls
            idx = next((i for i, c in enumerate(pool) if _call_matches(exp, c)), None)
            if idx is None:
                failures.append(f"expected call {exp['tool']} {exp.get('params', {})} not made")
            elif case.get("ordered"):
                pos += idx + 1
        for name in case.get("must_not_call", []):
            if any(c["name"] == name for c in calls):
                failures.append(f"forbidden tool {name} was called")

    if criteria in ("response_matches", "both"):
        pattern = case.get("expected_response_pattern")
        if pattern and not re.search(pattern, result["text"], re.I | re.S):
            failures.append(f"response does not match /{pattern}/")
    bad = case.get("response_must_not_match")
    if bad and re.search(bad, result["text"], re.I | re.S):
        failures.append(f"response matches forbidden /{bad}/")

    if (limit := case.get("max_latency_ms")) and result["latency_ms"] > limit:
        failures.append(f"latency {result['latency_ms']:.0f}ms > {limit}ms")
    return failures


def run_conversations(cases: list[dict], agent) -> list[dict]:
    convs: dict[str, list[dict]] = {}
    for c in cases:
        convs.setdefault(c["conversation_id"], []).append(c)
    results = []
    for conv_id, turns in convs.items():
        history: list[dict[str, str]] = []
        for case in sorted(turns, key=lambda t: t["turn_number"]):
            history.append({"role": "user", "content": case["user_message"]})
            with telemetry.span("eval.turn", **{
                "eval.kind": "agent", "eval.conversation_id": conv_id,
                "eval.turn_number": case["turn_number"], "eval.category": case["category"],
            }) as sp:
                try:
                    res = agent(history)
                    failures = score_turn(case, res)
                except Exception as e:  # a transport failure should fail the turn, not the run
                    res = {"text": "", "tool_calls": [], "latency_ms": 0, "platform_abort": False}
                    failures = [f"runner error: {e}"]
                sp.set_attribute("eval.passed", not failures)
                sp.set_attribute("eval.failures", "; ".join(failures))
            history.append({"role": "assistant", "content": res["text"]})
            results.append({
                "conversation_id": conv_id, "turn_number": case["turn_number"], "category": case["category"],
                "passed": not failures, "failures": failures,
                "platform_abort": res["platform_abort"], "latency_ms": round(res["latency_ms"], 1),
                "tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in res["tool_calls"]],
                "response": res["text"][:500],
            })
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "agent-evals")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    telemetry.init("foundry-agent-evals")
    from evals.foundry_agent.target import run_agent

    cases = [json.loads(l) for p in args.cases for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    results = run_conversations(cases, run_agent)
    for r in results:
        flag = " [platform abort]" if r["platform_abort"] else ""
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['conversation_id']}#{r['turn_number']} {r['category']}{flag} {r['latency_ms']:.0f}ms")
        for f in r["failures"]:
            print(f"       - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (args.out / f"run-{stamp}.json").write_text(json.dumps({"timestamp": stamp, "results": results}, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
