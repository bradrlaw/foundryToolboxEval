"""ASSERT target: calls the deployed Foundry prompt agent (toolbox attached) via the Responses API.

Each toolbox call the agent makes (mcp_call output items) is re-emitted as an OpenInference TOOL span
so the ASSERT judge can cite tool names, arguments, and results.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv
from opentelemetry import trace

from evals import auth, telemetry, toolbox_errors

# Environment-specific values live in the git-ignored .env at the repo root.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Reuses ASSERT's tracer provider if present; adds an App Insights exporter when configured.
telemetry.init("foundry-agent-target")

_tracer = trace.get_tracer("foundry_agent_target")

PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
if not PROJECT_ENDPOINT:
    raise RuntimeError("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "customer-assistant")

REQUEST_TIMEOUT_S = 180
# Local DNS outages have lasted minutes, so connection failures get about 2.5 minutes of backoff.
MAX_ATTEMPTS = 8
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 60.0


class ResponsesAPIError(RuntimeError):
    """Non-retryable (or retries exhausted) HTTP error from the Responses API."""

    def __init__(self, status: int, body: str, latency_ms: float, attempts: int = 1):
        super().__init__(f"Responses API {status}: {body[:500]}")
        self.status, self.body, self.latency_ms, self.attempts = status, body, latency_ms, attempts


def retry_delay(attempt: int, headers=None) -> float:
    """Seconds to wait before retrying after `attempt` (0-based): the server's retry-after-ms or
    Retry-After (capped at 60 s), else exponential backoff of 5, 10, 20, then 30 s."""
    hints = {k.lower(): v for k, v in headers.items()} if headers is not None else {}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = hints.get(name)
        if value is None:
            continue
        try:
            wait = float(value) * scale
        except ValueError:
            try:
                wait = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                continue
        return min(MAX_RETRY_AFTER_S, max(0.0, wait))
    return min(30.0, 5.0 * 2 ** attempt)


def _post_responses(payload: dict) -> tuple[dict, float, int]:
    """POST to the Responses API. Returns (response JSON, latency of the final attempt in ms, attempts).

    Throttling and transient HTTP errors are retried, as are connection failures. A read timeout is not:
    the agent may already be running tools, and the latency would be meaningless anyway.
    """
    body = json.dumps(payload).encode()
    for attempt in range(MAX_ATTEMPTS):
        headers = {"Authorization": "Bearer " + auth.get_token(), "Content-Type": "application/json"}
        if telemetry.AVAILABLE:
            from opentelemetry.propagate import inject
            inject(headers)  # traceparent, so Foundry's server-side spans join our trace
        req = urllib.request.Request(f"{PROJECT_ENDPOINT}/openai/v1/responses", data=body, headers=headers)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            return data, (time.perf_counter() - start) * 1000, attempt + 1
        except urllib.error.HTTPError as e:
            latency = (time.perf_counter() - start) * 1000
            with e:  # closes the error response
                text = e.read().decode(errors="replace")
            if e.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS - 1:
                raise ResponsesAPIError(e.code, text, latency, attempt + 1) from e
            delay = retry_delay(attempt, e.headers)
        except (urllib.error.URLError, ConnectionError):
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = retry_delay(attempt)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _tool_output_text(item: dict) -> str:
    out = item.get("output")
    if out:
        # The toolbox appends a structuredResponse block after the raw tool text.
        return out.split("\n{\n  \"structuredResponse\"")[0].strip()
    return json.dumps(item.get("error")) if item.get("error") else ""


PLATFORM_ABORT_NOTE = "[AGENT FAILURE: the platform aborted the request because a tool returned an error]"
CONSENT_NOTE = "[AGENT NOT RUN: the signed-in user has not consented to the agent's OAuth tool connection]"
LISTING_NOTE = "[AGENT NOT RUN: the toolbox could not list the agent's tools]"
LISTING_FAILED = "McpProtocolException encountered while enumerating tools"


class AgentNotRun(RuntimeError):
    """The agent could not run (consent missing or its tools could not be listed): nothing to judge."""


def _api_error_message(body: str) -> str:
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return body
    return message if isinstance(message, str) else body


def _username() -> str | None:
    try:  # only used to word a console hint
        return auth.caller_claims().get("username")
    except Exception:
        return None


def _aborted(span, text: str, reason: str, calls: list, latency_ms: float, attempts: int) -> dict:
    span.set_attribute("eval.platform_abort", True)
    span.set_attribute("eval.abort_reason", reason)
    span.set_attribute("output.value", text)
    return {"text": text, "platform_abort": True, "abort_reason": reason, "tool_calls": calls,
            "latency_ms": latency_ms, "attempts": attempts}


def run_agent(messages: list[dict[str, str]]) -> dict:
    """Call the agent once. Returns text, tool_calls [{name, arguments, output}], latency_ms, attempts,
    platform_abort and abort_reason. latency_ms covers the final HTTP attempt only (no token fetch or
    retry waits).

    Findings, reported as platform_abort=True with an abort_reason:
    - tool_error: a toolbox tool error fails the whole Responses call with HTTP 400, so the model never
      sees it. The tool name is recovered from the error message.
    - consent_required: a tool source uses OAuth identity passthrough and the signed-in user has not
      consented. The response completes with an oauth_consent_request item instead of a reply; the
      consent link goes to the console only.
    - tool_listing_failed: the toolbox could not list a tool source's tools (HTTP 400), e.g. the MCP
      server rejected the user's token.
    """
    with _tracer.start_as_current_span("foundry_agent") as span:
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", AGENT_NAME)
        span.set_attribute("input.value", messages[-1]["content"])
        try:
            data, latency_ms, attempts = _post_responses({
                "input": messages,
                "agent_reference": {"type": "agent_reference", "name": AGENT_NAME},
            })
        except ResponsesAPIError as e:
            if e.status == 400 and "An error occurred invoking" in e.body:
                m = re.search(r"invoking '([^']+)'", e.body)
                calls = [{"name": m.group(1) if m else "", "arguments": None, "output": str(e)}]
                return _aborted(span, f"{PLATFORM_ABORT_NOTE} {e}", "tool_error", calls, e.latency_ms, e.attempts)
            if e.status == 400 and LISTING_FAILED in e.body:
                message = _api_error_message(e.body)
                failure = toolbox_errors.classify(message)
                if failure and failure.consent:
                    toolbox_errors.announce_consent(failure.consent, _username())
                    return _aborted(span, f"{CONSENT_NOTE} {failure.detail}", "consent_required", [],
                                    e.latency_ms, e.attempts)
                detail = failure.detail if failure else message
                return _aborted(span, f"{LISTING_NOTE} {detail}", "tool_listing_failed", [], e.latency_ms, e.attempts)
            raise

        text, calls, consent = "", [], []
        for item in data.get("output", []):
            if item.get("type") == "mcp_call":
                out = _tool_output_text(item)
                calls.append({"name": item.get("name", ""), "arguments": json.loads(item.get("arguments") or "{}"), "output": out})
                with _tracer.start_as_current_span(f"tool:{item.get('name')}") as tool:
                    tool.set_attribute("openinference.span.kind", "TOOL")
                    tool.set_attribute("gen_ai.operation.name", "execute_tool")
                    tool.set_attribute("gen_ai.tool.name", item.get("name", ""))
                    tool.set_attribute("tool.name", item.get("name", ""))
                    tool.set_attribute("input.value", item.get("arguments") or "{}")
                    tool.set_attribute("output.value", out)
            elif item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content", []))
            elif item.get("type") == "oauth_consent_request":
                consent.append((str(item.get("server_label") or ""), str(item.get("consent_link") or "")))

        if consent:
            toolbox_errors.announce_consent(consent, _username())
            detail = "; ".join(f"tool source '{s}': {toolbox_errors.CONSENT_DETAIL}" for s, _ in consent)
            return _aborted(span, f"{CONSENT_NOTE} {detail}", "consent_required", calls, latency_ms, attempts)
        if data.get("status") != "completed":
            raise RuntimeError(f"Agent response status {data.get('status')}: {data.get('error')}")
        span.set_attribute("output.value", text)
        return {"text": text, "platform_abort": False, "abort_reason": None, "tool_calls": calls,
                "latency_ms": latency_ms, "attempts": attempts}


def chat(message: str, history: list[dict[str, str]] | None = None) -> str:
    """ASSERT entry point. Raises AgentNotRun when the agent could not run, which ASSERT records as a
    target_error and leaves unscored; a tool_error abort returns its note for the judge to see."""
    messages = [
        {"role": t["role"], "content": str(t.get("content") or "")}
        for t in (history or [])
        if t.get("role") in ("user", "assistant")
    ] or [{"role": "user", "content": message}]
    out = run_agent(messages)
    if out["abort_reason"] in toolbox_errors.AGENT_NOT_RUN:
        raise AgentNotRun(out["text"])
    return out["text"]


if __name__ == "__main__":
    print(chat("What is Northwind Energy's stock price?"))
