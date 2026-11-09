"""Deterministic agent evals: run JSONL conversations against the Foundry agent and score tool calls.

Complements ASSERT, which judges behavior but does not check exact tool calls or parameters.

Usage:
  python -m evals.run_agent_evals data/agent-evals/sample-toolbox.jsonl
  python -m evals.run_agent_evals data/agent-evals/sample-toolbox-entra.jsonl --agent customer-assistant-oauth
  python -m evals.run_agent_evals data/agent-evals/sample-toolbox.jsonl --repetitions 3
Results are written to results/agent-evals/run-<UTC timestamp>.json, with what the run ran against (commit,
agent version and model, toolbox versions; evals/provenance.py) and its token totals.

--repetitions N runs every conversation N times, each from a fresh history, to measure how consistent the agent is:
a turn passes only if every run passes (pass^N), and the console adds pass@1, pass@N and pass^N and the flaky turns.
Each turn also records `extra_calls`, its tool calls beyond those it expects; `max_tool_calls` makes too many fail.

--history chain (the default, or $FOUNDRY_AGENT_HISTORY) continues each later turn from the previous turn's stored
response (previous_response_id), so the agent keeps the tool results it already has, as in a real chat. --history replay
sends the conversation so far as text instead: the user's messages and the agent's replies, without tool results.

Cases may use {{caller.oid}}, {{caller.tid}}, {{caller.name}} and {{caller.username}}, filled from the
signed-in user's token. A turn the agent could not run at all (consent_required, tool_listing_failed)
fails with that reason; consent links are printed to the console only, never saved. Each failure message
is recorded with a reason code (evals/reasons.py) so reports can say why turns failed.

Exit codes: 0 all turns passed (every run), 1 some failed, 2 setup error (invalid cases, missing endpoint or sign-in).
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from azure.core.exceptions import ClientAuthenticationError
from dotenv import load_dotenv

from evals import auth, provenance, telemetry
from evals.cases import ROOT, CaseError, fill_caller, json_equal, load_cases
from evals.reasons import Failure, summary_line
from evals.results import by_case, extra_calls_by_case, pass_rate_text, pass_rates, write_run
from evals.toolbox_errors import AGENT_NOT_RUN

MAX_MESSAGE = 300
# What served each turn, recorded with its result: null when the request failed.
SERVED = ("response_id", "agent_version", "model", "usage")
TOKENS = ("input_tokens", "output_tokens", "total_tokens")


def _value_matches(expected, actual) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        if expected.startswith("re:"):
            return re.search(expected[3:], actual, re.I) is not None
        return expected.strip().lower() == actual.strip().lower()
    return json_equal(expected, actual)


def _call_matches(expected: dict, actual: dict) -> bool:
    if actual["name"] != expected["tool"]:
        return False
    if actual.get("arguments") is None:  # platform abort: only the tool name is known; the turn fails anyway
        return True
    args = actual["arguments"]
    return all(k in args and _value_matches(v, args[k]) for k, v in expected.get("params", {}).items())


def _pair_unordered(expected: list[dict], calls: list[dict]) -> dict[int, int]:
    """Pair as many expected calls as possible with distinct actual calls: {expected index: call index}.

    Augmenting paths, so an earlier expectation that a broad `re:` param lets take any call gives way to a
    later, stricter one."""
    owner: dict[int, int] = {}  # call index -> expected index

    def place(e: int, tried: set[int]) -> bool:
        for i, c in enumerate(calls):
            if i not in tried and _call_matches(expected[e], c):
                tried.add(i)
                if i not in owner or place(owner[i], tried):
                    owner[i] = e
                    return True
        return False

    for e in range(len(expected)):
        place(e, set())
    return {e: i for i, e in owner.items()}


def _clip(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE else message[:MAX_MESSAGE - 3] + "..."


def _missed(expected: dict, calls: list[dict], used: set[int]) -> Failure:
    """Why no call matched an expected call. `used` holds calls that other expected calls already took."""
    what = f"expected call {expected['tool']} {expected.get('params', {})}"
    if any(i not in used and _call_matches(expected, c) for i, c in enumerate(calls)):
        return Failure("wrong_order", f"{what} made out of order")
    args = [c["arguments"] for i, c in enumerate(calls) if i not in used and c["name"] == expected["tool"]]
    if not args:
        return Failure("missing_call", f"{what} not made")
    return Failure("wrong_arguments", _clip(f"{what} not made; {expected['tool']} was called with {'; '.join(map(str, args))}"))


def score_turn(case: dict, result: dict) -> list[Failure]:
    """Return the turn's failures (empty = pass)."""
    if result.get("abort_reason") in AGENT_NOT_RUN:
        # Nothing to score; must_not_call and must-not-match checks would pass vacuously.
        return [Failure(result["abort_reason"], result["text"])]
    calls = result["tool_calls"]
    criteria = case["pass_criteria"]
    failures: list[Failure] = []
    if result.get("platform_abort"):
        # A tool error or a failed tool call ended the whole request, so the user got no reply: response checks
        # are skipped.
        failures.append(Failure("platform_abort", result["text"]))

    if criteria in ("tool_called_correctly", "both"):
        expected = case["expected_tool_calls"]
        if case.get("ordered"):
            pos, used = 0, set()
            for exp in expected:
                idx = next((i for i in range(pos, len(calls)) if _call_matches(exp, calls[i])), None)
                if idx is None:
                    failures.append(_missed(exp, calls, used))
                else:
                    used.add(idx)
                    pos = idx + 1
        else:
            pairs = _pair_unordered(expected, calls)
            used = set(pairs.values())
            failures += [_missed(exp, calls, used) for e, exp in enumerate(expected) if e not in pairs]
    for name in case.get("must_not_call", []):
        if any(c["name"] == name for c in calls):
            failures.append(Failure("forbidden_call", f"forbidden tool {name} was called"))
    if (limit := case.get("max_tool_calls")) is not None and len(calls) > limit:
        made = f"{len(calls)} tool call{'s' if len(calls) > 1 else ''}"
        failures.append(Failure("extra_calls", _clip(f"{made}, max_tool_calls is {limit}: "
                                                     + ", ".join(c["name"] for c in calls))))

    if criteria in ("response_matches", "both") and not result.get("platform_abort"):
        pattern = case.get("expected_response_pattern")
        if pattern and not re.search(pattern, result["text"], re.I | re.S):
            failures.append(Failure("response_mismatch", f"response does not match /{pattern}/"))
    bad = case.get("response_must_not_match")
    if bad and not result.get("platform_abort") and re.search(bad, result["text"], re.I | re.S):
        failures.append(Failure("forbidden_response", f"response matches forbidden /{bad}/"))

    if (limit := case.get("max_latency_ms")) and result["latency_ms"] > limit:
        failures.append(Failure("latency", f"latency {result['latency_ms']:.0f}ms > {limit}ms"))
    return failures


def extra_calls(case: dict, result: dict | None) -> int | None:
    """Tool calls beyond the number the turn expects. None when the turn doesn't check its calls (pass_criteria
    response_matches) or no calls are known (the request raised, or the agent never ran)."""
    if result is None or result.get("abort_reason") in AGENT_NOT_RUN or case["pass_criteria"] == "response_matches":
        return None
    return max(0, len(result["tool_calls"]) - len(case["expected_tool_calls"]))


def run_conversations(cases: list[dict], agent, repetitions: int = 1, chain: bool = True) -> list[dict]:
    """Run each conversation's turns in order with `agent(messages)`.

    With `chain`, a turn that follows an answered turn continues that turn's stored response:
    `agent(messages, previous_response_id=...)`, where `messages` are only those the response hasn't seen. Without it,
    or when a reply has no response id, the agent gets the conversation so far as text.

    With `repetitions`, every conversation runs that many times, each from a fresh history: all conversations
    once, then all of them again, so a brief platform problem doesn't fail every run of one conversation.
    A turn the agent never answered (runner error or platform abort) keeps its user message in the
    history but adds no assistant message, and later turns in that conversation report history_gap.
    """
    convs: dict[str, list[dict]] = {}
    for c in cases:
        convs.setdefault(c["conversation_id"], []).append(c)
    results = []
    for repetition in range(1, repetitions + 1):
        for conv_id, turns in convs.items():
            results += _run_conversation(conv_id, sorted(turns, key=lambda t: t["turn_number"]), agent, repetition,
                                         chain)
    return results


def _run_conversation(conv_id: str, turns: list[dict], agent, repetition: int, chain: bool) -> list[dict]:
    results = []
    pending: list[dict[str, str]] = []  # the messages the agent hasn't seen: all of them when replaying
    previous = None  # the stored response that answered the last answered turn, when chaining
    gap = False
    for case in turns:
        pending.append({"role": "user", "content": case["user_message"]})
        res = None
        with telemetry.span("eval.turn", **{
            "eval.kind": "agent", "eval.conversation_id": conv_id, "eval.turn_number": case["turn_number"],
            "eval.repetition": repetition, "eval.category": case["category"],
        }) as sp:
            try:
                res = agent(list(pending), previous_response_id=previous) if previous else agent(list(pending))
                found = score_turn(case, res)
            except Exception as e:  # a transport failure should fail the turn, not the run
                found = [Failure("runner_error", f"runner error: {type(e).__name__}: {e}")]
            failures, reasons = [f.message for f in found], [f.reason for f in found]
            extra = extra_calls(case, res)
            sp.set_attribute("eval.passed", not found)
            sp.set_attribute("eval.failures", "; ".join(failures))
            sp.set_attribute("eval.reasons", ",".join(sorted(set(reasons))))
            if extra is not None:
                sp.set_attribute("eval.extra_calls", extra)
        results.append({
            "conversation_id": conv_id, "turn_number": case["turn_number"], "repetition": repetition,
            "category": case["category"], "passed": not found, "failures": failures, "reasons": reasons,
            "platform_abort": bool(res and res["platform_abort"]),
            "abort_reason": res.get("abort_reason") if res else None, "history_gap": gap,
            "latency_ms": _round(res["latency_ms"]) if res else None,
            "elapsed_ms": _round(res.get("elapsed_ms")) if res else None,
            "attempts": res.get("attempts") if res else None,
            **{k: res.get(k) if res else None for k in SERVED},
            "previous_response_id": previous,
            "extra_calls": extra,
            "tool_calls": [{"name": c["name"], "arguments": c["arguments"]} for c in res["tool_calls"]] if res else [],
            "response": res["text"][:500] if res else "",
        })
        if res is None or res["platform_abort"]:
            gap = True
        elif chain and res.get("response_id"):
            previous, pending = res["response_id"], []
        else:
            pending.append({"role": "assistant", "content": res["text"]})
    return results


def _round(ms: float | None) -> float | None:
    return None if ms is None else round(ms, 1)


def _fmt_ms(ms: float | None) -> str:
    return "-" if ms is None else f"{ms:.0f}ms"


def _timing(r: dict) -> str:
    if (r.get("attempts") or 0) < 2:
        return _fmt_ms(r["latency_ms"])
    return f"{_fmt_ms(r['latency_ms'])} ({r['attempts']} attempts, {_fmt_ms(r.get('elapsed_ms'))} in all)"


def _most_common(values) -> str | None:
    found = Counter(v for v in values if v)
    return found.most_common(1)[0][0] if found else None


def run_provenance(results: list[dict], project: str, agent_name: str) -> dict:
    """What the run ran against: the repo, and the agent version that served the most turns, with its model and
    toolboxes. Each turn records the version and model that served it."""
    found = provenance.agent(project, agent_name, _most_common(r.get("agent_version") for r in results))
    model = found["model"] or _most_common(r.get("model") for r in results)
    return {"repo": provenance.repo(), "agent": {"name": agent_name, "version": found["version"], "model": model},
            "toolboxes": found["toolboxes"]}


def total_usage(results: list[dict]) -> dict | None:
    """Tokens summed over the turns that report usage."""
    used = [r["usage"] for r in results if r.get("usage")]
    return {k: sum(u.get(k) or 0 for u in used) for k in TOKENS} if used else None


def summary(results: list[dict]) -> list[str]:
    """The console's closing lines: how many turns passed (every run, with repetitions, and how consistently), the
    tool calls beyond those expected, and the failed turns per reason."""
    passed = sum(r["passed"] for r in results)
    if rates := pass_rates(results):
        cases = by_case(results)
        every = sum(all(r["passed"] for r in runs) for runs in cases.values())
        lines = [f"{every}/{len(cases)} turns passed all {rates['k']} runs ({passed}/{len(results)} runs passed)",
                 pass_rate_text(rates),
                 "Flaky: " + (", ".join(f"{c} {p}/{n}" for c, p, n in rates["flaky"]) or "none")]
    else:
        lines = [f"{passed}/{len(results)} passed"]
    if extra := extra_calls_by_case(results):
        lines.append(f"Extra tool calls: {sum(extra.values())} ({', '.join(f'{c} {n}' for c, n in extra.items())})")
    if why := summary_line(results):
        lines.append(why)
    return lines


def _at_least_one(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {text!r}")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run agent-eval conversations against the Foundry agent and score its tool calls.")
    ap.add_argument("cases", nargs="+", help="agent-eval JSONL files or glob patterns")
    ap.add_argument("--agent", help="agent name (default: $FOUNDRY_AGENT_NAME, else customer-assistant)")
    ap.add_argument("--repetitions", type=_at_least_one, default=1, metavar="N",
                    help="run every conversation N times, each from a fresh history; a turn passes only if every run "
                         "passes (default 1)")
    ap.add_argument("--history", choices=("chain", "replay"),
                    help="chain: continue each turn from the previous turn's stored response; replay: send the "
                         "conversation as text, without earlier tool results (default: $FOUNDRY_AGENT_HISTORY, "
                         "else chain)")
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
    history = args.history or target.HISTORY

    results = run_conversations(cases, target.run_agent, args.repetitions, history == "chain")
    for runs in by_case(results).values():  # a turn's runs together
        for r in runs:
            flags = f" [run {r['repetition']}/{args.repetitions}]" if args.repetitions > 1 else ""
            if r["platform_abort"]:
                flags += f" [platform abort: {r['abort_reason']}]" if r["abort_reason"] else " [platform abort]"
            if r["history_gap"]:
                flags += " [history gap]"
            print(f"{'PASS' if r['passed'] else 'FAIL'} {r['conversation_id']}#{r['turn_number']} {r['category']}"
                  f"{flags} {_timing(r)}")
            for f in r["failures"]:
                print(f"       - {f}")
    print("\n" + "\n".join(summary(results)))
    if any(r["abort_reason"] == "consent_required" for r in results):
        print("Some turns did not run: the agent's OAuth tool connection needs the signed-in user's consent. "
              "Open the link printed above, approve, then rerun.", file=sys.stderr)

    ran = run_provenance(results, target.PROJECT_ENDPOINT, target.AGENT_NAME)
    out = write_run(args.out, lambda stamp: {"agent": target.AGENT_NAME, "timestamp": stamp,
                                             "repetitions": args.repetitions, "history": history, "provenance": ran,
                                             "usage": total_usage(results), "results": results})
    print(f"results: {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
