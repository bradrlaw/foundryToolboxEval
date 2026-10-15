"""Tool tests: call a Foundry toolbox's MCP endpoint directly and check each case's expectations.

Usage:
  python -m tooltests.run data/tool-tests/sample-toolbox.jsonl
  python -m tooltests.run "data/tool-tests/*.jsonl" --toolbox my-toolbox

The endpoint is --endpoint, else toolbox.mcp_endpoint from --config, else
$FOUNDRY_PROJECT_ENDPOINT/toolboxes/<--toolbox, $FOUNDRY_TOOLBOX_NAME or sample-customer-toolbox>/mcp.
Results are written to results/tool-tests/run-<UTC timestamp>.json.

Each request ends in one outcome: ok, tool_error (a result with isError), protocol_error (a JSON-RPC
error such as an unknown tool), consent_required (the signed-in user has not yet consented to a tool
source's OAuth connection), upstream_error (the toolbox could not reach or authenticate to a tool
source), invalid_result (a result that breaks the tool's output schema), transport_error (HTTP or network
failure, or timeout) or runner_error. A case passes when every run has the expected outcome and passes
its assertions; assertions only run on the expected outcome. Each failure message is recorded with a reason
code (evals/reasons.py) so reports can say why cases failed.

Cases may use {{caller.oid}}, {{caller.tid}}, {{caller.name}} and {{caller.username}}, filled from the
signed-in user's token. Consent links are printed to the console only, never saved.

Exit codes: 0 all cases passed, 1 some failed, 2 setup error (cases, config, sign-in, consent or connection).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
import httpx2
from azure.core.exceptions import ClientAuthenticationError
from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, Implementation
from pydantic import ValidationError

from evals import __version__, auth, telemetry, toolbox_errors
from evals.cases import ROOT, CaseError, fill_caller, format_error, load_cases, schema_validator
from evals.reasons import Failure, summary_line

CLIENT_INFO = Implementation(name="foundry-toolbox-tests", version=__version__)
AUTH_PROBE_PROTOCOL = "2025-11-25"
DEFAULT_TOOLBOX = "sample-customer-toolbox"
MAX_PAGES = 100
MAX_RECONNECTS = 3
RECONNECT_BACKOFF_S = 2.0
EXPECTED = {False: "ok", True: "tool_error", "tool": "tool_error", "protocol": "protocol_error"}
SESSION_CLOSED = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)


class SetupError(RuntimeError):
    """Configuration or connection problem that stops the whole run (exit code 2)."""

    def __init__(self, message: str, consent: list[tuple[str, str]] | None = None):
        super().__init__(message)
        self.consent = consent or []  # (tool source, consent link): print, never record


@dataclass
class Target:
    endpoint: str
    scope: str = auth.FOUNDRY_SCOPE
    timeout: float = 60.0


@dataclass
class Outcome:
    outcome: str
    text: str = ""
    data: Any = None  # parsed JSON output (call), tool list (list) or HTTP status (auth)
    latency_ms: float | None = None


@dataclass
class HttpError:
    status: int
    body: str
    jsonrpc: bool  # the body was a JSON-RPC error, which the client raises as that MCPError

    def __str__(self) -> str:
        return f"HTTP {self.status}" + (f": {self.body}" if self.body else "")


class HttpErrors:
    """Records HTTP errors on POSTs: the MCP client reports most of them only as a generic -32603."""

    def __init__(self):
        self.seen: list[HttpError] = []

    async def hook(self, response: httpx2.Response) -> None:
        if response.status_code < 400 or response.request.method != "POST":
            return
        await response.aread()
        text = response.text
        jsonrpc = False
        if response.headers.get("content-type", "").lower().startswith("application/json"):
            with contextlib.suppress(ValueError):
                doc = json.loads(text)
                jsonrpc = isinstance(doc, dict) and doc.get("jsonrpc") == "2.0" and "error" in doc
        self.seen.append(HttpError(response.status_code, " ".join(text.split())[:300], jsonrpc))


class BearerAuth(httpx2.Auth):
    def __init__(self, scope: str):
        self.scope = scope

    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer " + auth.get_token(self.scope)
        yield request


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _username(scope: str) -> str | None:
    with contextlib.suppress(Exception):  # only used to word a console hint
        return auth.caller_claims(scope).get("username")
    return None


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---- output parsing and assertions -------------------------------------------------------------

def split_output(text: str) -> tuple[str, object | None]:
    """Strip the structuredResponse block some toolbox versions append to tool text; parse JSON if possible."""
    raw = text.split('\n{\n  "structuredResponse"')[0].strip()
    try:
        return raw, json.loads(raw)
    except ValueError:
        return raw, None


def parse_output(text: str, structured: object = None) -> object | None:
    """JSON for path and schema assertions: the text content (what a model sees) if it is JSON, else
    the structured content. MCP servers should send structured content as JSON text as well."""
    parsed = split_output(text)[1]
    return parsed if parsed is not None else structured


_PATH_STEP = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]")


def json_path(doc: object, path: str) -> object:
    """Resolve `$`, `$.a.b`, `$.a[0].b`; raise KeyError, IndexError or TypeError if absent."""
    if not path.startswith("$"):
        raise KeyError(path)
    cur, pos = doc, 1
    for m in _PATH_STEP.finditer(path, 1):
        if m.start() != pos:
            break
        key, index = m.groups()
        if index is not None:
            if not isinstance(cur, list):
                raise TypeError(f"{path[:m.start()] or '$'} is not an array")
            cur = cur[int(index)]
        else:
            if not isinstance(cur, dict):
                raise TypeError(f"{path[:m.start()] or '$'} is not an object")
            cur = cur[key]
        pos = m.end()
    if pos != len(path):
        raise KeyError(path)
    return cur


def json_equal(a: object, b: object) -> bool:
    """JSON equality: unlike Python, true != 1 and 0 != false."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(json_equal, a, b))
    return a == b


def check(assertion: dict, raw: str, parsed: object | None) -> str | None:
    """Return a failure message, or None if the assertion holds."""
    kind = assertion.get("type")
    try:
        if kind == "contains":
            return None if assertion["value"].lower() in raw.lower() else f"missing text {assertion['value']!r}"
        if kind == "regex":
            return None if re.search(assertion["pattern"], raw) else f"regex {assertion['pattern']!r} not matched"
        if kind in ("json_path_equals", "json_path_exists", "schema") and parsed is None:
            return "output is not JSON"
        if kind in ("json_path_equals", "json_path_exists"):
            try:
                value = json_path(parsed, assertion["path"])
            except (KeyError, IndexError, TypeError):
                return f"path {assertion['path']} not found"
            if kind == "json_path_equals" and not json_equal(value, assertion["value"]):
                return f"{assertion['path']} = {value!r}, expected {assertion['value']!r}"
            return None
        if kind == "schema":
            errors = sorted(schema_validator(assertion["schema"]).iter_errors(parsed), key=lambda e: e.json_path)
            return "; ".join(f"schema: {format_error(e)}" for e in errors) or None
        return f"unsupported assertion type {kind!r}"
    except Exception as e:  # a malformed assertion fails the case instead of crashing the run
        return f"{kind} assertion error: {type(e).__name__}: {e}"


def tool_definition_problems(tool) -> list[str]:
    problems = []
    if not (tool.description or "").strip():
        problems.append(f"{tool.name}: empty description")
    if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
        problems.append(f"{tool.name}: input schema is not an object schema")
    for label, schema in (("input", tool.input_schema), ("output", tool.output_schema)):
        if isinstance(schema, dict):
            try:
                validator_for(schema, default=Draft202012Validator).check_schema(schema)
            except SchemaError as e:
                problems.append(f"{tool.name}: invalid {label} schema: {e.message}")
    return problems


# ---- the MCP session ---------------------------------------------------------------------------

async def list_all_tools(client: Client) -> list:
    tools, cursor = [], None
    for _ in range(MAX_PAGES):
        page = await client.list_tools(cursor=cursor)
        tools += page.tools
        cursor = page.next_cursor
        if not cursor:
            return tools
    raise RuntimeError(f"tools/list still paginating after {MAX_PAGES} pages")


@dataclass
class Toolbox:
    client: Client
    http_errors: HttpErrors
    target: Target
    tools: list = field(default_factory=list)

    def describe(self) -> dict:
        info = self.client.server_info
        return {
            "protocol_version": self.client.protocol_version,
            "server": {"name": info.name, "version": info.version} if info else None,
            "tools": sorted(t.name for t in self.tools),
        }

    async def timed(self, request: Callable[[], Awaitable[Any]]) -> Outcome:
        """Await one MCP request and classify how it ended."""
        auth.get_token(self.target.scope)  # refresh a near-expiry token before the clock starts
        mark = len(self.http_errors.seen)
        start = time.perf_counter()
        try:
            value = await request()
        except MCPError as e:
            latency = _ms(start)
            http = [h for h in self.http_errors.seen[mark:] if not h.jsonrpc]
            if http or e.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT):
                detail = "; ".join(map(str, http)) or f"MCP error {e.code}: {e}"
                return Outcome("transport_error", detail, latency_ms=latency)
            if failure := toolbox_errors.classify(e.message, e.data, e.code):
                if failure.consent:
                    toolbox_errors.announce_consent(failure.consent, _username(self.target.scope))
                return Outcome(failure.outcome, failure.detail, latency_ms=latency)
            return Outcome("protocol_error", str(e), latency_ms=latency)
        except (RuntimeError, ValidationError) as e:  # output-schema or protocol-shape violations
            return Outcome("invalid_result", str(e), latency_ms=_ms(start))
        return Outcome("ok", data=value, latency_ms=_ms(start))

    async def call(self, tool: str, arguments: dict) -> Outcome:
        o = await self.timed(lambda: self.client.call_tool(tool, arguments))
        if o.outcome == "ok":
            result = o.data
            o.text = "\n".join(c.text for c in result.content if c.type == "text")
            o.data = parse_output(o.text, result.structured_content)
            if result.is_error:
                o.outcome = "tool_error"
        return o

    async def list_tools(self) -> Outcome:
        return await self.timed(lambda: list_all_tools(self.client))


@contextlib.asynccontextmanager
async def connect(target: Target, http_errors: HttpErrors):
    # No HTTP read timeout: one inside a POST would tear down the whole session. The MCP-level
    # read_timeout_seconds bounds each request instead and reports REQUEST_TIMEOUT.
    async with httpx2.AsyncClient(
        auth=BearerAuth(target.scope),
        timeout=httpx2.Timeout(30, connect=15, read=None),
        transport=httpx2.AsyncHTTPTransport(retries=3),
        event_hooks={"response": [http_errors.hook]},
    ) as http, Client(
        streamable_http_client(target.endpoint, http_client=http, terminate_on_close=False),
        mode="legacy", cache=None, client_info=CLIENT_INFO, read_timeout_seconds=target.timeout,
    ) as client:
        toolbox = Toolbox(client, http_errors, target)
        # Also primes the client's output-schema cache, which call_tool would otherwise fill
        # with its own tools/list inside the first timed call.
        toolbox.tools = await list_all_tools(client)
        yield toolbox


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in _leaves(sub)]
    return [error]


def _source_failure(leaf: BaseException) -> toolbox_errors.SourceFailure | None:
    return toolbox_errors.classify(leaf.message, leaf.data, leaf.code) if isinstance(leaf, MCPError) else None


def consent_needed(error: BaseException) -> list[tuple[str, str]]:
    """(tool source, consent link) pairs from a connection failure."""
    return [c for leaf in _leaves(error) if (f := _source_failure(leaf)) for c in f.consent]


def explain(error: BaseException, http_errors: HttpErrors) -> str:
    """Readable reason for a connection failure, with the HTTP status and body in place of the
    client's generic 'Server returned an error response', and tool-source failures without consent links."""
    messages = []
    for leaf in _leaves(error):
        if isinstance(leaf, MCPError) and http_errors.seen and not http_errors.seen[-1].jsonrpc:
            messages.append(str(http_errors.seen[-1]))
        elif failure := _source_failure(leaf):
            messages.append(failure.detail)
        else:
            messages.append(f"{type(leaf).__name__}: {leaf}" if str(leaf) else type(leaf).__name__)
    return "; ".join(dict.fromkeys(messages))


# ---- cases -------------------------------------------------------------------------------------

def summarize(case: dict, runs: list[tuple[Outcome, list[Failure]]], extra: list[Failure] = ()) -> dict:
    kind = case.get("kind", "call")
    n = len(runs)
    counts = Counter(f for _, failures in runs for f in dict.fromkeys(failures))
    found = [f if n == 1 else f._replace(message=f"{f.message} ({c}/{n} runs)") for f, c in counts.items()]
    found += extra
    lats = sorted(o.latency_ms for o, _ in runs if o.latency_ms is not None)
    return {
        "case_id": case["case_id"], "kind": kind, "tool": case.get("tool") or kind,
        "category": case["category"], "passed": not found,
        "failures": [f.message for f in found], "reasons": [f.reason for f in found],
        "runs": n, "outcomes": dict(Counter(o.outcome for o, _ in runs)),
        "latency_p50_ms": round(statistics.median(lats), 1) if lats else None,
        "latency_p95_ms": round(lats[math.ceil(0.95 * len(lats)) - 1], 1) if lats else None,
        "sample_output": runs[0][0].text[:300],
    }


def error_result(case: dict, outcome: str, message: str, reason: str | None = None) -> dict:
    return summarize(case, [(Outcome(outcome, message), [Failure(reason or outcome, message)])])


def latency_failures(case: dict, outcomes: list[Outcome], expected: str = "ok") -> list[Failure]:
    """One failure for all runs over max_latency_ms (runs with another outcome already failed)."""
    limit = case.get("max_latency_ms")
    slow = [o.latency_ms for o in outcomes
            if limit and o.outcome == expected and o.latency_ms is not None and o.latency_ms > limit]
    if not slow:
        return []
    if len(outcomes) == 1:
        return [Failure("latency", f"latency {slow[0]:.0f}ms > {limit}ms")]
    return [Failure("latency", f"latency > {limit}ms in {len(slow)}/{len(outcomes)} runs (max {max(slow):.0f}ms)")]


async def run_call(tb: Toolbox, case: dict) -> dict:
    expected = EXPECTED[case.get("expect_error", False)]
    runs = []
    for _ in range(case.get("repetitions", 1)):
        o = await tb.call(case["tool"], case["arguments"])
        if o.outcome != expected:
            reason = o.outcome if o.outcome != "ok" else "unexpected_success"
            failures = [Failure(reason, f"expected {expected}, got {o.outcome}: {_clip(o.text)}")]
        else:
            failures = [Failure("assertion", f) for a in case.get("assertions", []) if (f := check(a, o.text, o.data))]
        runs.append((o, failures))
    return summarize(case, runs, latency_failures(case, [o for o, _ in runs], expected))


async def run_list(tb: Toolbox, case: dict) -> dict:
    o = await tb.list_tools()
    if o.outcome != "ok":
        return summarize(case, [(o, [Failure(o.outcome, f"tools/list failed ({o.outcome}): {_clip(o.text)}")])])
    tools, failures = o.data, []
    names = [t.name for t in tools]
    failures += [Failure("duplicate_tool", f"tool {n!r} listed {c} times") for n, c in Counter(names).items() if c > 1]
    failures += [Failure("missing_tool", f"tool {n!r} not listed") for n in case["expect_tools"] if n not in names]
    if case.get("require_schemas"):
        failures += [Failure("tool_definition", p) for t in tools for p in tool_definition_problems(t)]
    o.text = ", ".join(sorted(set(names)))
    return summarize(case, [(o, failures)], latency_failures(case, [o]))


async def run_auth(target: Target, case: dict) -> dict:
    """POST initialize with a bad or missing token and check the HTTP status."""
    headers = {"Accept": "application/json, text/event-stream"}
    if case["token"] == "invalid":
        headers["Authorization"] = "Bearer invalid-token"
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": AUTH_PROBE_PROTOCOL, "capabilities": {},
        "clientInfo": {"name": CLIENT_INFO.name, "version": CLIENT_INFO.version}}}
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(target.timeout, connect=15),
                                  transport=httpx2.AsyncHTTPTransport(retries=3)) as http:
        start = time.perf_counter()
        try:
            r = await http.post(target.endpoint, json=body, headers=headers)
            o = Outcome("ok", _clip(f"HTTP {r.status_code}: {r.text}", 300), r.status_code, _ms(start))
        except httpx2.HTTPError as e:
            o = Outcome("transport_error", f"{type(e).__name__}: {e}", latency_ms=_ms(start))
    if o.outcome != "ok":
        failures = [Failure(o.outcome, f"request failed: {o.text}")]
    elif o.data != case["expect_status"]:
        failures = [Failure("auth_status", f"status {o.data}, expected {case['expect_status']}")]
    else:
        failures = []
    return summarize(case, [(o, failures)])


async def run_case(tb: Toolbox, case: dict) -> dict:
    kind = case.get("kind", "call")
    with telemetry.span("toolbox.test_case", **{
        "eval.kind": kind, "eval.case_id": case["case_id"], "eval.category": case["category"],
        "gen_ai.tool.name": case.get("tool") or kind,
    }) as sp:
        try:
            if kind == "list":
                result = await run_list(tb, case)
            elif kind == "auth":
                result = await run_auth(tb.target, case)
            else:
                result = await run_call(tb, case)
        except SESSION_CLOSED:
            raise  # the session is gone; run_all reconnects
        except Exception as e:  # a runner bug fails this case, not the run (cancellation still propagates)
            result = error_result(case, "runner_error", f"runner error: {type(e).__name__}: {e}")
        sp.set_attribute("eval.passed", result["passed"])
        sp.set_attribute("eval.outcome", ",".join(sorted(result["outcomes"])))
        if result["latency_p50_ms"] is not None:
            sp.set_attribute("eval.latency_p50_ms", result["latency_p50_ms"])
        sp.set_attribute("eval.failures", "; ".join(result["failures"]))
        sp.set_attribute("eval.reasons", ",".join(sorted(set(result["reasons"]))))
        return result


async def run_all(cases: list[dict], target: Target) -> tuple[list[dict], dict]:
    """Run cases in order on one MCP session, reconnecting if the session dies mid-run."""
    results: list[dict] = []
    info: dict = {}
    pending = list(cases)
    reconnects = 0
    ever_connected = False
    while pending:
        http_errors = HttpErrors()
        connected = False
        try:
            async with connect(target, http_errors) as tb:
                connected = ever_connected = True
                info = info or tb.describe()
                while pending:
                    results.append(await run_case(tb, pending[0]))
                    pending.pop(0)
        except Exception as e:  # transport failures arrive as (nested) ExceptionGroups
            reason = explain(e, http_errors)
            if not ever_connected:
                raise SetupError(f"cannot connect to {target.endpoint}: {reason}", consent_needed(e)) from e
            toolbox_errors.announce_consent(consent_needed(e), _username(target.scope))
            if not pending:
                print(f"warning: error closing the MCP session: {reason}", file=sys.stderr)
                break
            if connected:
                results.append(error_result(pending.pop(0), "transport_error", f"session lost: {reason}"))
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                results += [error_result(c, "transport_error", f"not run: session lost ({reason})", "not_run")
                            for c in pending]
                break
            print(f"session lost ({reason}); reconnecting ({reconnects}/{MAX_RECONNECTS})", file=sys.stderr)
            await asyncio.sleep(RECONNECT_BACKOFF_S * 2 ** (reconnects - 1))
    return results, info


# ---- CLI ---------------------------------------------------------------------------------------

def resolve_target(args: argparse.Namespace) -> Target:
    scope, endpoint = auth.FOUNDRY_SCOPE, args.endpoint
    if not endpoint and args.config:
        try:
            toolbox = json.loads(args.config.read_text(encoding="utf-8-sig")).get("toolbox") or {}
        except (OSError, ValueError) as e:
            raise SetupError(f"cannot read {args.config}: {e}") from e
        endpoint, scope = toolbox.get("mcp_endpoint"), toolbox.get("token_scope", scope)
        if not endpoint:
            raise SetupError(f"{args.config} has no toolbox.mcp_endpoint")
    if not endpoint and (project := os.environ.get("FOUNDRY_PROJECT_ENDPOINT")):
        name = args.toolbox or os.environ.get("FOUNDRY_TOOLBOX_NAME") or DEFAULT_TOOLBOX
        endpoint = f"{project.rstrip('/')}/toolboxes/{name}/mcp?api-version=v1"
    if not endpoint:
        raise SetupError("no toolbox endpoint: pass --endpoint or --config, or set FOUNDRY_PROJECT_ENDPOINT in .env")
    return Target(endpoint, scope, args.timeout)


def _fmt_ms(value: float | None) -> str:
    return f"{value:>8}ms" if value is not None else f"{'-':>8}  "


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run tool tests directly against a Foundry toolbox's MCP endpoint.")
    ap.add_argument("cases", nargs="+", help="tool-test JSONL files or glob patterns")
    ap.add_argument("--endpoint", help="toolbox MCP endpoint URL")
    ap.add_argument("--config", type=Path, help="JSON file with toolbox.mcp_endpoint (and optional token_scope)")
    ap.add_argument("--toolbox", help="toolbox name in $FOUNDRY_PROJECT_ENDPOINT (default: $FOUNDRY_TOOLBOX_NAME, "
                                      f"else {DEFAULT_TOOLBOX})")
    ap.add_argument("--timeout", type=float, default=60, help="seconds to wait for each MCP request (default 60)")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "tool-tests")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    try:
        target = resolve_target(args)
        cases = load_cases(args.cases, "tool")
        if not cases:
            raise SetupError("no cases found")
        auth.get_token(target.scope)
        cases = fill_caller(cases, lambda: auth.caller_claims(target.scope))
    except CaseError as e:
        print("invalid cases:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ClientAuthenticationError as e:
        print(f"error: cannot get a token for {target.scope} (signed in with az login?): {e}", file=sys.stderr)
        return 2

    telemetry.init("foundry-toolbox-tests")
    try:
        results, info = asyncio.run(run_all(cases, target))
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        toolbox_errors.announce_consent(e.consent, _username(target.scope))
        return 2

    if server := info.get("server"):
        print(f"{server['name']} {server['version']}, protocol {info['protocol_version']}, {len(info['tools'])} tools\n")
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['case_id']:<18} {r['tool']:<32} "
              f"p50={_fmt_ms(r['latency_p50_ms'])} p95={_fmt_ms(r['latency_p95_ms'])}")
        for f in r["failures"]:
            print(f"       - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")
    if why := summary_line(results):
        print(why)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / f"run-{stamp}.json"
    out.write_text(json.dumps({"endpoint": target.endpoint, "timestamp": stamp, **info, "results": results},
                              indent=2), encoding="utf-8")
    print(f"results: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
