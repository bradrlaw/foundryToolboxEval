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

    def __init__(self, status: int, body: str, latency_ms: float, attempts: int = 1, elapsed_ms: float | None = None):
        super().__init__(f"Responses API {status}: {body[:500]}")
        self.status, self.body, self.latency_ms, self.attempts = status, body, latency_ms, attempts
        self.elapsed_ms = latency_ms if elapsed_ms is None else elapsed_ms


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


def _post_responses(payload: dict) -> tuple[dict, float, int, float]:
    """POST to the Responses API. Returns (response JSON, latency of the final attempt in ms, attempts,
    elapsed ms). Elapsed covers the whole call: token fetches, every attempt and the waits between them.

    Throttling and transient HTTP errors are retried, as are connection failures. A read timeout is not:
    the agent may already be running tools, and the latency would be meaningless anyway.
    """
    body = json.dumps(payload).encode()
    began = time.perf_counter()
    for attempt in range(MAX_ATTEMPTS):
        headers = {"Authorization": "Bearer " + auth.get_token(), "Content-Type": "application/json"}
        telemetry.inject(headers)
        req = urllib.request.Request(f"{PROJECT_ENDPOINT}/openai/v1/responses", data=body, headers=headers)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            end = time.perf_counter()
            return data, (end - start) * 1000, attempt + 1, (end - began) * 1000
        except urllib.error.HTTPError as e:
            end = time.perf_counter()
            with e:  # closes the error response
                text = e.read().decode(errors="replace")
            if e.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS - 1:
                raise ResponsesAPIError(e.code, text, (end - start) * 1000, attempt + 1, (end - began) * 1000) from e
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
CALL_FAILED_NOTE = "[AGENT FAILURE: the platform aborted the request because the toolbox could not call a tool]"
CONSENT_NOTE = "[AGENT NOT RUN: the signed-in user has not consented to the agent's OAuth tool connection]"
LISTING_NOTE = "[AGENT NOT RUN: the toolbox could not list the agent's tools]"
LISTING_FAILED = "McpProtocolException encountered while enumerating tools"
CALL_FAILED = "McpProtocolException encountered while invoking tool"


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


TOKENS = ("input_tokens", "output_tokens", "total_tokens")
NOT_SERVED = {"response_id": None, "agent_version": None, "model": None, "usage": None}


def _served(data: dict) -> dict:
    """The response's id, the agent version and model that produced it, and its token usage."""
    ref = data.get("agent_reference") if isinstance(data.get("agent_reference"), dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    version = ref.get("version")
    return {"response_id": data.get("id"), "agent_version": None if version is None else str(version),
            "model": data.get("model"), "usage": {k: usage.get(k) for k in TOKENS} if usage else None}


def _record_served(span, served: dict) -> None:
    """The OpenTelemetry GenAI attributes of an agent invocation: the agent version and the tokens it used."""
    if served["agent_version"] is not None:
        span.set_attribute("gen_ai.agent.version", served["agent_version"])
    for k in ("input_tokens", "output_tokens"):
        if isinstance((served["usage"] or {}).get(k), int):
            span.set_attribute(f"gen_ai.usage.{k}", served["usage"][k])


def _aborted(span, text: str, reason: str, calls: list, meta: dict) -> dict:
    span.set_attribute("eval.platform_abort", True)
    span.set_attribute("eval.abort_reason", reason)
    span.set_attribute("output.value", text)
    return {"text": text, "platform_abort": True, "abort_reason": reason, "tool_calls": calls, **meta}


def run_agent(messages: list[dict[str, str]]) -> dict:
    """Call the agent once. Returns text, tool_calls [{name, arguments, output}], platform_abort, abort_reason,
    latency_ms, attempts, elapsed_ms, and the response_id, agent_version, model and usage (input, output and
    total tokens) of the response, which are null when the call failed with an HTTP error. latency_ms covers the
    final HTTP attempt only; elapsed_ms also covers token fetches, earlier attempts and retry waits.

    Findings, reported as platform_abort=True with an abort_reason:
    - tool_error: the platform failed the whole Responses call with HTTP 400 because a toolbox tool returned
      an error, so the model never saw it. This happened for every tool error through October 2026; since
      November 2026 the model gets the error text as the tool output instead. The tool name is recovered
      from the error message.
    - consent_required: a tool source uses OAuth identity passthrough and the signed-in user has not
      consented. The response completes with an oauth_consent_request item instead of a reply; the
      consent link goes to the console only.
    - tool_listing_failed: the toolbox could not list a tool source's tools (HTTP 400), e.g. the MCP
      server rejected the user's token.
    - tool_call_failed: the toolbox could not call the tool the model chose, so the platform failed the whole
      call with HTTP 400. The tool name is recovered from the error message.
    Either of the last two happens while an MCP server that scaled to zero is still starting: the toolbox's
    MCP client asks for protocol version 2026-07-28 and fails instead of falling back to the initialize
    handshake when its server/discover probe times out (seen November 2026).
    """
    with _tracer.start_as_current_span("foundry_agent") as span:
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", AGENT_NAME)
        span.set_attribute("input.value", messages[-1]["content"])
        try:
            data, latency_ms, attempts, elapsed_ms = _post_responses({
                "input": messages,
                "agent_reference": {"type": "agent_reference", "name": AGENT_NAME},
            })
        except ResponsesAPIError as e:
            meta = {"latency_ms": e.latency_ms, "attempts": e.attempts, "elapsed_ms": e.elapsed_ms, **NOT_SERVED}
            if e.status == 400 and "An error occurred invoking" in e.body:
                m = re.search(r"invoking '([^']+)'", e.body)
                calls = [{"name": m.group(1) if m else "", "arguments": None, "output": str(e)}]
                return _aborted(span, f"{PLATFORM_ABORT_NOTE} {e}", "tool_error", calls, meta)
            if e.status == 400 and LISTING_FAILED in e.body:
                message = _api_error_message(e.body)
                failure = toolbox_errors.classify(message)
                if failure and failure.consent:
                    toolbox_errors.announce_consent(failure.consent, _username())
                    return _aborted(span, f"{CONSENT_NOTE} {failure.detail}", "consent_required", [], meta)
                detail = failure.detail if failure else message
                return _aborted(span, f"{LISTING_NOTE} {detail}", "tool_listing_failed", [], meta)
            if e.status == 400 and CALL_FAILED in e.body:
                message = _api_error_message(e.body)
                m = re.search(r"invoking tool ([^\s:]+)", message)
                calls = [{"name": m.group(1) if m else "", "arguments": None, "output": message}]
                return _aborted(span, f"{CALL_FAILED_NOTE} {message}", "tool_call_failed", calls, meta)
            raise

        served = _served(data)
        _record_served(span, served)
        meta = {"latency_ms": latency_ms, "attempts": attempts, "elapsed_ms": elapsed_ms, **served}
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
            return _aborted(span, f"{CONSENT_NOTE} {detail}", "consent_required", calls, meta)
        if data.get("status") != "completed":
            raise RuntimeError(f"Agent response status {data.get('status')}: {data.get('error')}")
        span.set_attribute("output.value", text)
        return {"text": text, "platform_abort": False, "abort_reason": None, "tool_calls": calls, **meta}


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
