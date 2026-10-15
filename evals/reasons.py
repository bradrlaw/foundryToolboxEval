"""Why a case failed: a reason code recorded with each failure message.

Result files keep `failures` (readable messages) and add `reasons`, a parallel list: reasons[i] classifies
failures[i]. scripts/report.py groups failures by reason, and telemetry spans carry the distinct reasons as
eval.reasons.
"""
from __future__ import annotations

from collections import Counter
from typing import NamedTuple


class Failure(NamedTuple):
    reason: str
    message: str


# Tool tests. A call that ends with another outcome than the case expects fails with that outcome as its reason.
TOOL_REASONS = {
    "tool_error": "The tool returned an error result (isError).",
    "protocol_error": "The toolbox returned a JSON-RPC error, such as an unknown tool or invalid arguments.",
    "invalid_result": "The result broke the tool's output schema or the MCP result format.",
    "consent_required": "The signed-in user hasn't consented to a tool source's OAuth connection.",
    "upstream_error": "The toolbox couldn't reach or authenticate to a tool source.",
    "transport_error": "HTTP or network failure, a timeout, or a lost MCP session.",
    "unexpected_success": "The call succeeded, but the case expects an error.",
    "assertion": "The output failed one of the case's assertions.",
    "latency": "Slower than the case's max_latency_ms.",
    "missing_tool": "An expected tool isn't listed.",
    "duplicate_tool": "A tool is listed more than once.",
    "tool_definition": "A listed tool has an empty description or an invalid input or output schema.",
    "auth_status": "A request with a missing or invalid token got an unexpected HTTP status.",
    "not_run": "Never ran: the MCP session was lost and reconnecting failed.",
    "runner_error": "The test runner itself failed on this case.",
}

AGENT_REASONS = {
    "consent_required": "The agent never ran: the signed-in user hasn't consented to its OAuth tool connection.",
    "tool_listing_failed": "The agent never ran: the toolbox couldn't list its tools.",
    "missing_call": "An expected tool call wasn't made.",
    "wrong_arguments": "The expected tool was called, but never with the expected arguments.",
    "wrong_order": "The expected calls were made, but not in the expected order.",
    "forbidden_call": "A tool listed in must_not_call was called.",
    "response_mismatch": "The reply doesn't match expected_response_pattern.",
    "forbidden_response": "The reply matches response_must_not_match.",
    "latency": "Slower than the case's max_latency_ms.",
    "runner_error": "No reply: the request raised, for example an HTTP error from the Responses API.",
}


def tally(results: list[dict]) -> Counter:
    """Failed cases per reason; a case counts once for each of its reasons."""
    return Counter(reason for r in results for reason in dict.fromkeys(r.get("reasons") or ()))


def summary_line(results: list[dict]) -> str:
    """'Failed by reason: a 3, b 1' for the console, or '' when nothing failed."""
    counts = tally(results)
    return "Failed by reason: " + ", ".join(f"{k} {n}" for k, n in counts.most_common()) if counts else ""
