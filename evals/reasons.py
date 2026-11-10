"""Why a case failed: a reason code recorded with each failure message.

Result files keep `failures` (readable messages) and add `reasons`, a parallel list: reasons[i] classifies
failures[i]. scripts/report.py groups failures by reason, and telemetry spans carry the distinct reasons as
eval.reasons.
"""
from __future__ import annotations

from collections import Counter
from typing import NamedTuple

from evals.results import result_id


class Failure(NamedTuple):
    reason: str
    message: str


# Tool tests. A call that ends with another outcome than the case expects fails with that outcome as its reason.
TOOL_REASONS = {
    "tool_error": "The tool returned an error result (isError).",
    "protocol_error": "The toolbox returned a JSON-RPC error, such as an unknown tool or invalid arguments.",
    "invalid_result": "The result broke the tool's output schema or the MCP result format, or its JSON text and "
                      "structured content disagree.",
    "consent_required": "The signed-in user hasn't consented to a tool source's OAuth connection.",
    "upstream_error": "The toolbox couldn't reach or authenticate to a tool source.",
    "transport_error": "HTTP or network failure, a timeout, or a lost MCP session.",
    "unexpected_success": "The call succeeded, but the case expects an error.",
    "assertion": "The output failed one of the case's assertions.",
    "latency": "Slower than the case's max_latency_ms.",
    "missing_tool": "An expected tool isn't listed.",
    "duplicate_tool": "A tool is listed more than once.",
    "tool_definition": "A listed tool has an empty description, an invalid input or output schema, or no output "
                       "schema where the case requires one.",
    "auth_status": "A request with a missing or invalid token got an unexpected HTTP status.",
    "not_run": "Never ran: the MCP session was lost and reconnecting failed.",
    "runner_error": "The test runner itself failed on this case.",
}

AGENT_REASONS = {
    "consent_required": "The agent never ran: the signed-in user hasn't consented to its OAuth tool connection.",
    "tool_listing_failed": "The agent never ran: the toolbox couldn't list its tools.",
    "platform_abort": "A tool error, or a tool call the toolbox couldn't make, ended the whole request (HTTP 400), "
                      "so the user got no reply.",
    "missing_call": "An expected tool call wasn't made.",
    "wrong_arguments": "The expected tool was called, but never with the expected arguments.",
    "wrong_order": "The expected calls were made, but not in the expected order.",
    "forbidden_call": "A tool listed in must_not_call was called.",
    "extra_calls": "The turn made more tool calls than the case's max_tool_calls allows.",
    "response_mismatch": "The reply doesn't match expected_response_pattern.",
    "forbidden_response": "The reply matches response_must_not_match.",
    "ungrounded_figure": "The reply states a figure that matches no number a tool returned in the conversation "
                         "(figures_from_tools; evals/figures.py).",
    "latency": "Slower than the case's max_latency_ms.",
    "runner_error": "No reply: the request raised, for example an HTTP error from the Responses API.",
}

# Cloud evaluation (evals/run_cloud_eval.py). A turn an evaluator fails gets the evaluator's name as its reason;
# scripts/report.py describes built-in evaluators missing here in general terms.
CLOUD_REASONS = {
    "grounded_adherence": "The judge found a fact or figure in the reply that no tool result in the conversation "
                          "supports, or a reply that breaks the agent's instructions or doesn't answer the request "
                          "(this repo's evaluator, scored 1 to 5, failing below 3).",
    "tool_use": "The judge found the turn's tool calls wrong, missing or unneeded; a right call whose result is an "
                "error counts as right (this repo's evaluator, scored 1 to 5, failing below 3).",
    "task_adherence": "The judge found that the reply strays from the agent's instructions or the user's task, for "
                      "example a figure it can't trace to a tool result in the conversation (scored 1 or 0).",
    "intent_resolution": "The judge found that the reply doesn't resolve what the user asked (scored 1 to 5, failing "
                         "below 3).",
    "tool_call_accuracy": "The judge found the turn's tool calls wrong, incomplete or unneeded (scored 1 to 5, failing "
                          "below 3).",
    "evaluator_error": "An evaluator couldn't judge the turn, for example because an input it needs was missing.",
    "agent_error": "Foundry's call to the agent failed (--target), so there was no reply to judge.",
    "not_judged": "No verdict: the turn has no stored response (its agent request failed or never ran), or the "
                  "evaluation run returned no result for it.",
}


def tally(results: list[dict]) -> Counter:
    """Failed cases per reason; a case counts once for each of its reasons, however many of its runs failed."""
    failed = dict.fromkeys((result_id(r), reason) for r in results for reason in r.get("reasons") or ())
    return Counter(reason for _, reason in failed)


def summary_line(results: list[dict]) -> str:
    """'Failed by reason: a 3, b 1' for the console, or '' when nothing failed."""
    counts = tally(results)
    return "Failed by reason: " + ", ".join(f"{k} {n}" for k, n in counts.most_common()) if counts else ""
