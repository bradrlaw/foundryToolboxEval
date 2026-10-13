"""Direct tool-test runner: calls toolbox MCP tools over JSON-RPC and checks assertions.

Usage:
  python -m tooltests.run data/tool-tests/sample-toolbox.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get_token(scope: str) -> str:
    resource = scope.removesuffix("/.default")
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True, check=True,
    )
    return out.stdout.strip()


class McpClient:
    def __init__(self, endpoint: str, token: str):
        self.endpoint, self.token, self._id, self.session = endpoint, token, 0, None

    def rpc(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            body["id"] = self._id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.endpoint, json.dumps(body).encode(), headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.session = resp.headers.get("Mcp-Session-Id") or self.session
                    raw = resp.read().decode()
                    ctype = resp.headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as e:
                return {"error": {"code": e.code, "message": e.read().decode(errors="replace")[:500]}}
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    return {"error": {"code": None, "message": f"network error: {e}"}}
                time.sleep(5 * (attempt + 1))
        if notify or not raw.strip():
            return None
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
                    if msg.get("id") == body["id"]:
                        return msg
            return {"error": {"message": "no response in event stream"}}
        return json.loads(raw)

    def initialize(self) -> None:
        self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "foundry-toolbox-tests", "version": "0.1"},
        })
        self.rpc("notifications/initialized", notify=True)

    def call(self, tool: str, arguments: dict) -> tuple[bool, str, str | None]:
        """Returns (is_error, text, protocol_error)."""
        msg = self.rpc("tools/call", {"name": tool, "arguments": arguments}) or {}
        if "error" in msg:
            return True, "", msg["error"].get("message", str(msg["error"]))
        result = msg.get("result", {})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return bool(result.get("isError")), text, None


def split_output(text: str) -> tuple[str, object | None]:
    """The toolbox appends a structuredResponse block after the tool's raw text; strip it off."""
    marker = '\n{\n  "structuredResponse"'
    raw = text.split(marker)[0].strip()
    try:
        return raw, json.loads(raw)
    except ValueError:
        return raw, None


def json_path(doc: object, path: str):
    cur = doc
    for part in re.findall(r"[^.\[\]$]+|\[\d+\]", path):
        cur = cur[int(part[1:-1])] if part.startswith("[") else cur[part]
    return cur


def _validator():
    """Reuse the stdlib JSON-schema-subset checker from the JSONL validator."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("validate_eval_jsonl", ROOT / "scripts" / "validate_eval_jsonl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(assertion: dict, raw: str, parsed: object | None) -> str | None:
    kind = assertion["type"]
    try:
        if kind == "contains":
            return None if assertion["value"].lower() in raw.lower() else f"missing text {assertion['value']!r}"
        if kind == "regex":
            return None if re.search(assertion["pattern"], raw) else f"regex {assertion['pattern']!r} not matched"
        if kind in ("json_path_equals", "json_path_exists"):
            if parsed is None:
                return "output is not JSON"
            val = json_path(parsed, assertion["path"])
            if kind == "json_path_equals" and val != assertion["value"]:
                return f"{assertion['path']} = {val!r}, expected {assertion['value']!r}"
            return None
        if kind == "schema":
            if parsed is None:
                return "output is not JSON"
            errors: list[str] = []
            _validator().check(parsed, assertion["schema"], "$", errors)
            return "; ".join(errors) or None
        return f"unsupported assertion type {kind}"
    except (KeyError, IndexError, TypeError):
        return f"path {assertion.get('path')} not found"


def _summarize(case: dict, failures: list[str], lats: list[float], sample: str, runs: int) -> dict:
    lats = sorted(lats)
    return {
        "case_id": case["case_id"], "kind": case.get("kind", "call"),
        "tool": case.get("tool") or case.get("kind", "call"), "category": case["category"],
        "passed": not failures, "failures": sorted(set(failures)), "runs": runs,
        "latency_p50_ms": round(statistics.median(lats), 1),
        "latency_p95_ms": round(lats[min(len(lats) - 1, int(len(lats) * 0.95))], 1),
        "sample_output": sample,
    }


def run_list_case(client: McpClient, case: dict) -> dict:
    start = time.perf_counter()
    msg = client.rpc("tools/list") or {}
    latency = (time.perf_counter() - start) * 1000
    failures = []
    tools = msg.get("result", {}).get("tools", [])
    if "error" in msg:
        failures.append(f"tools/list error: {msg['error'].get('message')}")
    names = {t["name"] for t in tools}
    failures += [f"tool {n!r} not listed" for n in case["expect_tools"] if n not in names]
    if case.get("require_schemas"):
        for t in tools:
            if not t.get("description"):
                failures.append(f"{t['name']}: empty description")
            if (t.get("inputSchema") or {}).get("type") != "object":
                failures.append(f"{t['name']}: inputSchema is not an object schema")
    if (limit := case.get("max_latency_ms")) and latency > limit:
        failures.append(f"latency {latency:.0f}ms > {limit}ms")
    return _summarize(case, failures, [latency], ", ".join(sorted(names)), 1)


def run_auth_case(endpoint: str, case: dict) -> dict:
    anon = McpClient(endpoint, "invalid-token" if case["token"] == "invalid" else "")
    if case["token"] == "none":
        anon.token = ""
    start = time.perf_counter()
    msg = anon.rpc("tools/list") or {}
    latency = (time.perf_counter() - start) * 1000
    status = msg.get("error", {}).get("code")
    failures = [] if status == case["expect_status"] else [f"status {status}, expected {case['expect_status']}"]
    return _summarize(case, failures, [latency], str(msg.get("error", {}).get("message", ""))[:200], 1)


def run_case(client: McpClient, case: dict) -> dict:
    kind = case.get("kind", "call")
    if kind == "list":
        return run_list_case(client, case)
    if kind == "auth":
        return run_auth_case(client.endpoint, case)
    expect_error = case.get("expect_error", False)
    runs = []
    for _ in range(case.get("repetitions", 1)):
        start = time.perf_counter()
        is_error, text, proto_err = client.call(case["tool"], case["arguments"])
        latency = (time.perf_counter() - start) * 1000
        raw, parsed = split_output(proto_err or text)
        failures = []
        if is_error != expect_error:
            failures.append(f"expected error={expect_error}, got error={is_error}")
        for a in case.get("assertions", []):
            if (f := check(a, raw, parsed)):
                failures.append(f)
        if (limit := case.get("max_latency_ms")) and latency > limit:
            failures.append(f"latency {latency:.0f}ms > {limit}ms")
        runs.append({"latency_ms": round(latency, 1), "failures": failures, "output": raw[:300]})
    lats = sorted(r["latency_ms"] for r in runs)
    p95 = lats[min(len(lats) - 1, int(len(lats) * 0.95))]
    failures = sorted({f for r in runs for f in r["failures"]})
    return {
        "case_id": case["case_id"], "tool": case["tool"], "category": case["category"],
        "passed": not failures, "failures": failures, "runs": len(runs),
        "latency_p50_ms": round(statistics.median(lats), 1), "latency_p95_ms": p95,
        "sample_output": runs[0]["output"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="+", type=Path)
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "sample-toolbox.local.json")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "tool-tests")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text())["toolbox"]
    client = McpClient(cfg["mcp_endpoint"], get_token(cfg.get("token_scope", "https://ai.azure.com/.default")))
    client.initialize()

    results = []
    for path in args.cases:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(run_case(client, json.loads(line)))

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{status} {r['case_id']:<18} {r['tool']:<32} p50={r['latency_p50_ms']:>7}ms p95={r['latency_p95_ms']:>7}ms")
        for f in r["failures"]:
            print(f"       - {f}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} passed")

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (args.out / f"run-{stamp}.json").write_text(
        json.dumps({"endpoint": cfg["mcp_endpoint"], "timestamp": stamp, "results": results}, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
