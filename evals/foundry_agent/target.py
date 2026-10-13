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
from pathlib import Path
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

# Environment-specific values live in the git-ignored .env at the repo root.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_existing = trace.get_tracer_provider()
if not isinstance(_existing, TracerProvider):
    if not isinstance(getattr(_existing, "_real_provider", None), TracerProvider):
        trace.set_tracer_provider(TracerProvider())

_tracer = trace.get_tracer("foundry_agent_target")
_credential = AzureCliCredential()

PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
if not PROJECT_ENDPOINT:
    raise RuntimeError("Set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "customer-assistant")


def _token() -> str:
    for attempt in range(4):
        try:
            return _credential.get_token("https://ai.azure.com/.default").token
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def _post_responses(payload: dict) -> dict:
    token = _token()
    req = urllib.request.Request(
        f"{PROJECT_ENDPOINT}/openai/v1/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Responses API {e.code}: {e.read().decode(errors='replace')[:500]}") from e
        except (urllib.error.URLError, TimeoutError):
            if attempt == 7:
                raise
            time.sleep(min(30, 10 * (attempt + 1)))


def _tool_output_text(item: dict) -> str:
    out = item.get("output")
    if out:
        # The toolbox appends a structuredResponse block after the raw tool text.
        return out.split("\n{\n  \"structuredResponse\"")[0].strip()
    return json.dumps(item.get("error")) if item.get("error") else ""


PLATFORM_ABORT_NOTE = "[AGENT FAILURE: the platform aborted the request because a tool returned an error]"


def run_agent(messages: list[dict[str, str]]) -> dict:
    """Call the agent once. Returns text, tool_calls [{name, arguments, output}], latency_ms, platform_abort.

    Finding: a toolbox tool error fails the whole Responses call with HTTP 400, so the model never sees
    it. That is reported as platform_abort=True with the tool name recovered from the error message.
    """
    start = time.perf_counter()
    with _tracer.start_as_current_span("foundry_agent") as span:
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("input.value", messages[-1]["content"])
        try:
            data = _post_responses({
                "input": messages,
                "agent_reference": {"type": "agent_reference", "name": AGENT_NAME},
            })
        except RuntimeError as e:
            if "Responses API 400" in str(e) and "An error occurred invoking" in str(e):
                m = re.search(r"invoking '([^']+)'", str(e))
                text = f"{PLATFORM_ABORT_NOTE} {e}"
                span.set_attribute("output.value", text)
                return {
                    "text": text, "platform_abort": True,
                    "tool_calls": [{"name": m.group(1) if m else "", "arguments": None, "output": str(e)}],
                    "latency_ms": (time.perf_counter() - start) * 1000,
                }
            raise

        text, calls = "", []
        for item in data.get("output", []):
            if item.get("type") == "mcp_call":
                out = _tool_output_text(item)
                calls.append({"name": item.get("name", ""), "arguments": json.loads(item.get("arguments") or "{}"), "output": out})
                with _tracer.start_as_current_span(f"tool:{item.get('name')}") as tool:
                    tool.set_attribute("openinference.span.kind", "TOOL")
                    tool.set_attribute("tool.name", item.get("name", ""))
                    tool.set_attribute("input.value", item.get("arguments") or "{}")
                    tool.set_attribute("output.value", out)
            elif item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content", []))

        if data.get("status") != "completed":
            raise RuntimeError(f"Agent response status {data.get('status')}: {data.get('error')}")
        span.set_attribute("output.value", text)
        return {"text": text, "platform_abort": False, "tool_calls": calls,
                "latency_ms": (time.perf_counter() - start) * 1000}


def chat(message: str, history: list[dict[str, str]] | None = None) -> str:
    messages = [
        {"role": t["role"], "content": str(t.get("content") or "")}
        for t in (history or [])
        if t.get("role") in ("user", "assistant")
    ] or [{"role": "user", "content": message}]
    return run_agent(messages)["text"]


if __name__ == "__main__":
    print(chat("What is Northwind Energy's stock price?"))
