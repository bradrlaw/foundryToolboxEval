"""Deterministic agent evals: run JSONL conversations against the Foundry agent and score tool calls.

Complements ASSERT, which judges behavior but does not check exact tool calls or parameters.

Usage:
  python -m evals.run_agent_evals data/agent-evals/sample-toolbox.jsonl
  python -m evals.run_agent_evals data/agent-evals/sample-toolbox-entra.jsonl --agent customer-assistant-oauth
Results are written to results/agent-evals/run-<UTC timestamp>.json.

Cases may use {{caller.oid}}, {{caller.tid}}, {{caller.name}} and {{caller.username}}, filled from the
signed-in user's token. A turn the agent could not run at all (consent_required, tool_listing_failed)
fails with that reason; consent links are printed to the console only, never saved. Each failure message
is recorded with a reason code (evals/reasons.py) so reports can say why turns failed.

Exit codes: 0 all turns passed, 1 some failed, 2 setup error (invalid cases, missing endpoint or sign-in).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from azure.core.exceptions import ClientAuthenticationError
from dotenv import load_dotenv

from evals import auth, telemetry
from evals.cases import ROOT, CaseError, fill_caller, load_cases
from evals.reasons import Failure, summary_line
from evals.toolbox_errors import AGENT_NOT_RUN

MAX_MESSAGE = 300


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


def _missed(expected: dict, calls: list[dict], used: set[int]) -> Failure:
    """Why no call matched an expected call. `used` holds calls that ordered matching already consumed."""
    what = f"expected call {expected['tool']} {expected.get('params', {})}"
    if any(i not in used and _call_matches(expected, c) for i, c in enumerate(calls)):
        return Failure("wrong_order", f"{what} made out of order")
    args = [c["arguments"] for i, c in enumerate(calls) if i not in used and c["name"] == expected["tool"]]
    if not args:
        return Failure("missing_call", f"{what} not made")
    message = f"{what} not made; {expected['tool']} was called with {'; '.join(map(str, args))}"
    return Failure("wrong_arguments", message if len(message) <= MAX_MESSAGE else message[:MAX_MESSAGE - 3] + "...")


def score_turn(case: dict, result: dict) -> list[Failure]:
    """Return the turn's failures (empty = pass)."""
    if result.get("abort_reason") in AGENT_NOT_RUN:
        # Nothing to score; must_not_call and must-not-match checks would pass vacuously.
        return [Failure(result["abort_reason"], result["text"])]
    calls = result["tool_calls"]
    criteria = case["pass_criteria"]
    failures: list[Failure] = []

    if criteria in ("tool_called_correctly", "both"):
        pos, used = 0, set()
        for exp in case["expected_tool_calls"]:
            start = pos if case.get("ordered") else 0
            idx = next((i for i in range(start, len(calls)) if _call_matches(exp, calls[i])), None)
            if idx is None:
                failures.append(_missed(exp, calls, used))
            elif case.get("ordered"):
                used.add(idx)
                pos = idx + 1
    for name in case.get("must_not_call", []):
        if any(c["name"] == name for c in calls):
            failures.append(Failure("forbidden_call", f"forbidden tool {name} was called"))

    if criteria in ("response_matches", "both"):
        pattern = case.get("expected_response_pattern")
        if pattern and not re.search(pattern, result["text"], re.I | re.S):
            failures.append(Failure("response_mismatch", f"response does not match /{pattern}/"))
    bad = case.get("response_must_not_match")
    if bad and re.search(bad, result["text"], re.I | re.S):
        failures.append(Failure("forbidden_response", f"response matches forbidden /{bad}/"))

    if (limit := case.get("max_latency_ms")) and result["latency_ms"] > limit:
        failures.append(Failure("latency", f"latency {result['latency_ms']:.0f}ms > {limit}ms"))
    return failures


def run_conversations(cases: list[dict], agent) -> list[dict]:
    """Run each conversation's turns in order, sending the accumulated history to `agent`.

    A turn the agent never answered (runner error or platform abort) keeps its user message in the
    history but adds no assistant message, and later turns in that conversation report history_gap.
    """
    convs: dict[str, list[dict]] = {}
    for c in cases:
        convs.setdefault(c["conversation_id"], []).append(c)
    results = []
    for conv_id, turns in convs.items():
        history: list[dict[str, str]] = []
        gap = False
        for case in sorted(turns, key=lambda t: t["turn_number"]):
            history.append({"role": "user", "content": case["user_message"]})
            res = None
            with telemetry.span("eval.turn", **{
                "eval.kind": "agent", "eval.conversation_id": conv_id,
                "eval.turn_number": case["turn_number"], "eval.category": case["category"],
            }) as sp:
                try:
                    res = agent(list(history))
                    found = score_turn(case, res)
                except Exception as e:  # a transport failure should fail the turn, not the run
                    found = [Failure("runner_error", f"runner error: {type(e).__name__}: {e}")]
                failures, reasons = [f.message for f in found], [f.reason for f in found]
                sp.set_attribute("eval.passed", not found)
                sp.set_attribute("eval.failures", "; ".join(failures))
                sp.set_attribute("eval.reasons", ",".join(sorted(set(reasons))))
            results.append({
                "conversation_id": conv_id, "turn_number": case["turn_number"], "category": case["category"],
                "passed": not found, "failures": failures, "reasons": reasons,
                "platform_abort": bool(res and res["platform_abort"]),
                "abort_reason": res.get("abort_reason") if res else None, "history_gap": gap,
                "latency_ms": round(res["latency_ms"], 1) if res else None,
                "attempts": res.get("attempts") if res else None,
                "tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in res["tool_calls"]] if res else [],
                "response": res["text"][:500] if res else "",
            })
            if res is None or res["platform_abort"]:
                gap = True
            else:
                history.append({"role": "assistant", "content": res["text"]})
    return results


def _fmt_ms(ms: float | None) -> str:
    return "-" if ms is None else f"{ms:.0f}ms"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run agent-eval conversations against the Foundry agent and score its tool calls.")
    ap.add_argument("cases", nargs="+", help="agent-eval JSONL files or glob patterns")
    ap.add_argument("--agent", help="agent name (default: $FOUNDRY_AGENT_NAME, else customer-assistant)")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "agent-evals")
    args = ap.parse_args(argv)

    try:
        cases = load_cases(args.cases, "agent")
    except CaseError as e:
        print("invalid cases:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    if not cases:
        print("error: no cases found", file=sys.stderr)
        return 2

    load_dotenv(ROOT / ".env")
    telemetry.init("foundry-agent-evals")
    try:
        from evals.foundry_agent import target  # reads FOUNDRY_PROJECT_ENDPOINT at import
        auth.get_token()
        cases = fill_caller(cases, auth.caller_claims)
    except CaseError as e:
        print("invalid cases:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ClientAuthenticationError as e:
        print(f"error: cannot get a token for {auth.FOUNDRY_SCOPE} (signed in with az login?): {e}", file=sys.stderr)
        return 2
    if args.agent:
        target.AGENT_NAME = args.agent

    results = run_conversations(cases, target.run_agent)
    for r in results:
        flags = ""
        if r["platform_abort"]:
            flags += f" [platform abort: {r['abort_reason']}]" if r["abort_reason"] else " [platform abort]"
        if r["history_gap"]:
            flags += " [history gap]"
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['conversation_id']}#{r['turn_number']} {r['category']}{flags} "
              f"{_fmt_ms(r['latency_ms'])}")
        for f in r["failures"]:
            print(f"       - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    if why := summary_line(results):
        print(why)
    if any(r["abort_reason"] == "consent_required" for r in results):
        print("Some turns did not run: the agent's OAuth tool connection needs the signed-in user's consent. "
              "Open the link printed above, approve, then rerun.", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / f"run-{stamp}.json"
    out.write_text(json.dumps({"agent": target.AGENT_NAME, "timestamp": stamp, "results": results}, indent=2),
                   encoding="utf-8")
    print(f"results: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
