"""Foundry cloud evaluation: evaluators in Foundry judge the agent's replies, server side.

A third view next to the deterministic checks (evals/run_agent_evals.py) and ASSERT: the judge runs in Foundry, and
each verdict is set against the deterministic checks of the same reply.

What it judges:
- An agent-eval run's stored responses: the run given, else the newest results/agent-evals/run-*.json. Foundry reads
  each turn's response by its id, with up to --max-turns turns of its conversation (default: the run's longest
  conversation). A chained run's later turns are judged with the earlier turns' tool calls and results.
- --target CASES: Foundry sends the first turn of each conversation to the agent itself and judges the reply, and
  the turn's deterministic checks score the same reply. Later turns are left out: a target run can't continue a
  conversation. The agent is --agent at --agent-version, else FOUNDRY_AGENT_NAME at FOUNDRY_AGENT_VERSION, as in the
  agent-eval runner; without a version Foundry calls the agent's latest.

Each turn gets the evaluators in --evaluators, run with the --judge model deployment. The default is this repo's two
evaluators and Foundry's built-in intent_resolution:
- tool_use: whether the turn made the tool calls the request needed, with the right arguments. A right call whose
  result is an error, such as no customer with that ID, counts as right; the reply isn't judged.
- grounded_adherence: whether every fact in the reply comes from a tool result of this turn or an earlier one, and
  the reply follows the agent's instructions and answers the request, or says plainly what isn't available.
They see the earlier turns with their tool calls and results, and replace the built-in tool_call_accuracy and
task_adherence, which failed correct replies to tool errors and figures recalled from an earlier turn. Their judge
prompts are evals/evaluators/<name>.md. Each is registered in the project's evaluator catalog as toolbox_eval_<name>,
with a new version when its prompt changes, and the evaluation uses that version. Built-in evaluators such as
task_adherence can be named too.

A turn fails if an evaluator fails it and errors if one couldn't judge it; tool_call_accuracy doesn't apply to a turn
without tool calls. With --target, built-in evaluators named tool_* also get the agent's tool definitions.

The evaluation is created in the project, named for its settings (toolbox-eval-<source>-<hash>), and reused by later
runs with the same settings, so the portal lists their runs together. Results are written to
results/cloud-evals/run-<UTC timestamp>.json: each turn's verdict, every evaluator's score and reason, the turn's
deterministic result, and the judge's tokens. scripts/report.py cloud reports them.

Usage:
  python -m evals.run_cloud_eval [results/agent-evals/run-<timestamp>.json]
  python -m evals.run_cloud_eval --target data/agent-evals/sample-toolbox.jsonl
  python -m evals.run_cloud_eval --fail-on unjudged --timeout 15   # as live CI runs it

Exit codes: 0 every turn passed, 1 some failed, errored or weren't judged, 2 setup error, an HTTP error from the
evaluation API, or the evaluation run failed, was canceled or didn't end within --timeout. With --fail-on unjudged,
turns the evaluators failed don't count, so 1 means only that some turns couldn't be judged (error or not judged):
live CI reports the judge's verdicts without failing on them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote, urlencode

from azure.core.exceptions import ClientAuthenticationError
from dotenv import load_dotenv

from evals import auth
from evals.cases import ROOT, CaseError, fill_caller, load_cases
from evals.reasons import Failure
from evals.results import agreement, by_case, result_id, score_outcome, write_run
from evals.retry import RETRY_STATUSES, retry_delay
from evals.run_agent_evals import at_least_one, run_provenance, score_turn, summary, total_usage

AGENT_RUNS = ROOT / "results" / "agent-evals"
DEFAULT_AGENT = "customer-assistant"
DEFAULT_JUDGE = "gpt-4-1-mini"
DEFAULT_EVALUATORS = ("grounded_adherence", "intent_resolution", "tool_use")
# This repo's evaluators, with their catalog descriptions. Each judges a turn 1 to 5 with the prompt in
# evals/evaluators/<name>.md and passes it from CUSTOM_THRESHOLD.
CUSTOM_EVALUATORS = {
    "tool_use": "Whether the turn made the tool calls the request needed, with the right arguments. A right call whose "
                "result is an error counts as right; the reply isn't judged.",
    "grounded_adherence": "Whether every fact in the reply comes from a tool result of this turn or an earlier one, "
                          "and the reply follows the agent's instructions and answers the request.",
}
CUSTOM_PROMPTS = Path(__file__).resolve().parent / "evaluators"
CUSTOM_THRESHOLD = 3
CATALOG_PREFIX = "toolbox_eval_"
# Creating an evaluator version is a preview feature that requests opt in to.
CATALOG_HEADERS = {"Foundry-Features": "Evaluations=V1Preview"}
_TEXT_OR_MESSAGES = {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "object"}}]}
# Where this repo's evaluators find their inputs. A stored response's query_history_list holds the agent's
# instructions and the earlier turns with their tool calls and results, then the user's message.
CUSTOM_MAPPINGS = {
    "responses": {"query": "{{item.query_history_list}}", "response": "{{sample.output_items}}",
                  "tool_definitions": "{{item.tool_definitions}}"},
    "target": {"query": "{{item.query}}", "response": "{{sample.output_items}}",
               "tool_definitions": "{{sample.tool_definitions}}"},
}
REQUEST_TIMEOUT_S = 120
MAX_ATTEMPTS = 6
# A POST the service may have processed isn't resent, so an evaluation run never starts twice.
RESEND_STATUSES = {408, 429}
ENDED = ("completed", "failed", "canceled")
PASSING = ("pass", "skipped")
UNJUDGED = ("error", "not_judged")
LABELS = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "skipped": "SKIP", "not_judged": "NOT JUDGED"}
MAX_MESSAGE = 500
MAX_RESPONSE = 500
# What each judged turn keeps from the agent run.
COPIED = ("conversation_id", "turn_number", "category", "response_id", "previous_response_id", "agent_version",
          "model", "tool_calls", "response")
ITEM_SCHEMA = {"type": "object", "required": ["query"],
               "properties": {"query": {"type": "string"}, "case": {"type": "string"}}}


class CloudEvalError(RuntimeError):
    """An HTTP error from the evaluation API that isn't retried, or still failed after retries, or a reply that
    can't be used."""

    def __init__(self, status: int, body: str):
        super().__init__(f"evaluation API {status}: {_api_message(body)[:500]}")
        self.status, self.body = status, body


def _api_message(body: str) -> str:
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return body
    return message if isinstance(message, str) else body


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _json(status: int, text: str) -> dict:
    try:
        return _dict(json.loads(text)) if text.strip() else {}
    except ValueError:
        raise CloudEvalError(status, f"expected JSON, got: {text[:200]}") from None


def _id(status: int, data: dict) -> str:
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise CloudEvalError(status, "the reply has no id: " + json.dumps(data)[:300])
    return data["id"]


class EvalsAPI:
    """The project's evaluation API: OpenAI-style evals under <project>/openai/v1, and the evaluator catalog under
    <project>/evaluators."""

    def __init__(self, project: str):
        self.project = project.rstrip("/")
        self.base = self.project + "/openai/v1"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        """Send a request and return its JSON: to the evaluator catalog if `path` starts with /evaluators/, else to
        the evals API. Throttling, transient HTTP errors and connection failures are retried, and a GET also when the
        connection broke or timed out after it went out; a POST only when the service surely didn't process it
        (RESEND_STATUSES, or no connection)."""
        data = None if body is None else json.dumps(body).encode()
        statuses = RETRY_STATUSES if method == "GET" else RESEND_STATUSES
        catalog = path.startswith("/evaluators/")
        url = (self.project if catalog else self.base) + path
        for attempt in range(MAX_ATTEMPTS):
            headers = {"Authorization": "Bearer " + auth.get_token(), "Content-Type": "application/json",
                       **(CATALOG_HEADERS if catalog else {})}
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                    return _json(resp.status, resp.read().decode(errors="replace"))
            except urllib.error.HTTPError as e:
                with e:  # closes the error response
                    text = e.read().decode(errors="replace")
                if e.code not in statuses or attempt == MAX_ATTEMPTS - 1:
                    raise CloudEvalError(e.code, text) from e
                delay = retry_delay(attempt, e.headers)
            except urllib.error.URLError:  # no connection: the request didn't get through
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = retry_delay(attempt)
            except (OSError, HTTPException):  # the connection broke or timed out after the request went out
                if method != "GET" or attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = retry_delay(attempt)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _pages(self, path: str) -> list[dict]:
        """Every entry of a paged list, following last_id while has_more."""
        out, after = [], None
        while True:
            query = {"limit": 100, **({"after": after} if after else {})}
            page = self.call("GET", f"{path}?{urlencode(query)}")
            data = [d for d in page.get("data") or [] if isinstance(d, dict)]
            out += data
            last = page.get("last_id") or (data[-1].get("id") if data else None)
            if not page.get("has_more") or not last or last == after:
                return out
            after = last

    def eval_id(self, definition: dict) -> tuple[str, bool]:
        """The id of the evaluation named definition["name"], created from `definition` if the project has none;
        and whether it was created."""
        for found in self._pages("/evals"):
            if found.get("name") == definition["name"] and found.get("id"):
                return found["id"], False
        return _id(201, self.call("POST", "/evals", definition)), True

    def catalog_version(self, entry: dict) -> tuple[str, bool]:
        """The version of catalog evaluator entry["name"] whose definition is entry["definition"], registered from
        `entry` if no version has it; and whether it was registered. Only the version is kept: a version's other
        fields name the account, the project and who registered it."""
        path = f"/evaluators/{quote(entry['name'], safe='')}/versions"
        page_path, seen = f"{path}?api-version=v1", set()
        while page_path and page_path not in seen:
            seen.add(page_path)
            page = self.call("GET", page_path)
            for found in map(_dict, page.get("value") or []):
                if found.get("definition") == entry["definition"] and found.get("version"):
                    return str(found["version"]), False
            link = page.get("nextLink")
            within = isinstance(link, str) and link.startswith(self.project + "/evaluators/")
            page_path = link[len(self.project):] if within else None
        version = self.call("POST", f"{path}?api-version=v1", entry).get("version")
        if not version:
            raise CloudEvalError(201, f"the evaluator catalog's reply has no version of {entry['name']}")
        return str(version), True

    def start(self, eval_id: str, body: dict) -> str:
        """Start an evaluation run; returns its id."""
        return _id(201, self.call("POST", f"/evals/{quote(eval_id, safe='')}/runs", body))

    def wait(self, eval_id: str, run_id: str, timeout_s: float) -> dict:
        """Poll the run until it ends or `timeout_s` passes, printing each change of its status or counts. Returns
        its last state."""
        path = f"/evals/{quote(eval_id, safe='')}/runs/{quote(run_id, safe='')}"
        deadline = time.monotonic() + timeout_s
        shown, polls = None, 0
        while True:
            run = self.call("GET", path)
            counts = ", ".join(f"{k} {v}" for k, v in _dict(run.get("result_counts")).items() if v)
            if (run.get("status"), counts) != shown:
                shown = (run.get("status"), counts)
                print(f"  {run.get('status')}" + (f": {counts}" if counts else ""), flush=True)
            left = deadline - time.monotonic()
            if run.get("status") in ENDED or left <= 0:
                return run
            polls += 1
            time.sleep(min(15.0, 5.0 * polls, left))

    def output_items(self, eval_id: str, run_id: str) -> list[dict]:
        """The run's output items: one per judged item, with each evaluator's result."""
        return self._pages(f"/evals/{quote(eval_id, safe='')}/runs/{quote(run_id, safe='')}/output_items")


def custom_entry(name: str) -> dict:
    """The catalog entry of this repo's evaluator `name`: its judge prompt, the inputs it takes and its score."""
    prompt = (CUSTOM_PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    catalog_name = CATALOG_PREFIX + name
    tools = {"anyOf": [{"type": "object"}, {"type": "array", "items": {"type": "object"}}]}
    return {"name": catalog_name, "evaluator_type": "custom", "categories": ["quality"], "display_name": catalog_name,
            "description": CUSTOM_EVALUATORS[name], "supported_evaluation_levels": ["turn"], "definition": {
                "type": "prompt", "prompt_text": prompt,
                "init_parameters": {"type": "object", "required": ["deployment_name", "threshold"], "properties": {
                    "deployment_name": {"type": "string"}, "threshold": {"type": "number"}}},
                "data_schema": {"type": "object", "required": ["query", "response"], "properties": {
                    "query": _TEXT_OR_MESSAGES, "response": _TEXT_OR_MESSAGES, "tool_definitions": tools}},
                "metrics": {catalog_name: {"type": "ordinal", "desirable_direction": "increase", "min_value": 1,
                                           "max_value": 5}}}}


def criteria(evaluators: tuple[str, ...], judge: str, source: str, versions: dict[str, str]) -> list[dict]:
    """Testing criteria: this repo's evaluators at their catalog `versions`, told where their inputs are, and
    built-in ones, told so in a target run."""
    out = []
    for name in evaluators:
        if name in CUSTOM_EVALUATORS:
            out.append({"type": "azure_ai_evaluator", "name": name, "evaluator_name": CATALOG_PREFIX + name,
                        "evaluator_version": versions[name],
                        "initialization_parameters": {"deployment_name": judge, "threshold": CUSTOM_THRESHOLD},
                        "data_mapping": dict(CUSTOM_MAPPINGS[source])})
            continue
        c = {"type": "azure_ai_evaluator", "name": name, "evaluator_name": f"builtin.{name}",
             "initialization_parameters": {"deployment_name": judge}}
        if source == "target":
            # Not sample.tool_calls: it holds messages, which makes tool_call_accuracy skip the turn. The evaluator
            # finds the calls in the response.
            c["data_mapping"] = {"query": "{{item.query}}", "response": "{{sample.output_items}}"}
            if name.startswith("tool_"):
                c["data_mapping"]["tool_definitions"] = "{{sample.tool_definitions}}"
        out.append(c)
    return out


def eval_definition(source: str, evaluators: tuple[str, ...], judge: str,
                    versions: dict[str, str] | None = None) -> dict:
    """The evaluation that judges a source (responses or target), with this repo's evaluators at their catalog
    `versions`; named for its settings."""
    tests = criteria(evaluators, judge, source, versions or {})
    if source == "responses":
        body = {"data_source_config": {"type": "azure_ai_source", "scenario": "responses"}, "testing_criteria": tests}
    else:
        body = {"data_source_config": {"type": "custom", "include_sample_schema": True, "item_schema": ITEM_SCHEMA},
                "testing_criteria": tests}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
    return {"name": f"toolbox-eval-{source}-{digest}", **body}


def responses_run(name: str, response_ids: list[str], max_turns: int) -> dict:
    """A run that judges stored responses, each with up to `max_turns` turns of its conversation."""
    return {"name": name, "data_source": {"type": "azure_ai_responses", "item_generation_params": {
        "type": "response_retrieval", "max_num_turns": max_turns, "data_mapping": {"response_id": "{{item.resp_id}}"},
        "source": {"type": "file_content", "content": [{"item": {"resp_id": i}} for i in response_ids]}}}}


def target_run(name: str, turns: list[dict], agent: str, version: str | None = None) -> dict:
    """A run in which Foundry sends each turn's user message to the agent, at `version` if given, else its latest,
    and judges the reply."""
    items = [{"item": {"query": t["user_message"], "case": result_id(t)}} for t in turns]
    target = {"type": "azure_ai_agent", "name": agent, **({"version": version} if version else {})}
    return {"name": name, "data_source": {
        "type": "azure_ai_target_completions", "source": {"type": "file_content", "content": items},
        "input_messages": {"type": "template", "template": [{"role": "user", "content": "{{item.query}}"}]},
        "target": target}}


def first_turns(cases: list[dict]) -> tuple[list[dict], list[str]]:
    """The first turn of each conversation, and the later turns, which a target run leaves out."""
    convs: dict[str, list[dict]] = {}
    for c in cases:
        convs.setdefault(c["conversation_id"], []).append(c)
    ordered = [sorted(turns, key=lambda t: t["turn_number"]) for turns in convs.values()]
    return [turns[0] for turns in ordered], [result_id(t) for turns in ordered for t in turns[1:]]


def _clip(text: str, limit: int = MAX_MESSAGE) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def error_text(error) -> str | None:
    if not error:
        return None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or json.dumps(error))
    return str(error)


def _number(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def scores(item: dict) -> dict[str, dict]:
    """What each evaluator made of an output item: {name: {status, score, label, threshold, passed, reason}}. An
    evaluator that couldn't judge the item has its error as the reason."""
    out = {}
    for r in map(_dict, item.get("results") or []):
        name = r.get("name") or r.get("metric")
        if not name:
            continue
        reason = r.get("reason") or error_text(_dict(r.get("sample")).get("error")) or ""
        out[name] = {"status": r.get("status"), "score": r.get("score"), "label": r.get("label"),
                     "threshold": r.get("threshold"), "passed": r.get("passed"), "reason": reason}
    return out


def verdict(item: dict | None, source: str) -> tuple[str, dict, list[Failure]]:
    """A turn's verdict from its output item, with the evaluators' scores and the failures. pass: an evaluator passed
    the turn and none failed it. fail: an evaluator failed it. error: an evaluator couldn't judge it, or (target)
    Foundry's call to the agent failed. skipped: no evaluator applied. not_judged: no item, or no results."""
    if item is None:
        return "not_judged", {}, [Failure("not_judged", "not judged: the evaluation run returned no result for it")]
    found = scores(item)
    if error := error_text(_dict(item.get("sample")).get("error")):
        if source == "target":
            return "error", found, [Failure("agent_error", _clip(f"the agent call failed: {error}"))]
        return "not_judged", found, [Failure("not_judged", _clip(f"not judged: {error}"))]
    outcomes = {name: score_outcome(s) for name, s in found.items()}
    failures = [Failure(name, _clip(f"{name} {s['label'] or 'fail'} (score {_number(s['score'])}): {s['reason']}"))
                for name, s in found.items() if outcomes[name] == "failed"]
    for name, s in found.items():
        if outcomes[name] == "errored":
            what = f"gave no pass or fail (score {_number(s['score'])})" if s["status"] == "completed" else s["status"]
            failures.append(Failure("evaluator_error", _clip(f"{name} {what}: {s['reason'] or 'no reason given'}")))
    if "failed" in outcomes.values():
        return "fail", found, failures
    if failures:
        return "error", found, failures
    if "passed" in outcomes.values():
        return "pass", found, []
    if found:
        return "skipped", found, []
    return "not_judged", found, [Failure("not_judged", "not judged: no evaluator returned a result")]


def _judged(v: str, found: dict, failures: list[Failure]) -> dict:
    return {"verdict": v, "passed": v in PASSING, "failures": [f.message for f in failures],
            "reasons": [f.reason for f in failures], "scores": found}


def judge_responses(turns: list[dict], items: list[dict]) -> list[dict]:
    """An agent run's turns with the verdicts on their stored responses, matched by response id, and their
    deterministic results."""
    by_response: dict[str, dict] = {}
    for item in items:
        if rid := _dict(item.get("datasource_item")).get("response_id"):
            by_response.setdefault(rid, item)
    out = []
    for r in turns:
        if r.get("response_id"):
            judged = _judged(*verdict(by_response.get(r["response_id"]), "responses"))
        else:
            judged = _judged("not_judged", {}, [Failure("not_judged", "not judged: the turn has no stored response")])
        out.append({**{k: r.get(k) for k in COPIED}, "repetition": r.get("repetition") or 1, **judged,
                    "deterministic": {"passed": bool(r.get("passed")), "failures": r.get("failures") or [],
                                      "reasons": r.get("reasons") or []}})
    return out


def _parts(messages):
    """Every content part of a target run's output messages."""
    for m in messages if isinstance(messages, list) else []:
        content = _dict(m).get("content")
        yield from map(_dict, content if isinstance(content, list) else [])


def tool_calls(messages) -> list[dict]:
    """The tool calls in a target run's output messages: [{name, arguments}]."""
    calls = []
    for c in _parts(messages):
        if c.get("type") != "tool_call":
            continue
        args = c.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        calls.append({"name": str(c.get("name") or ""), "arguments": args if isinstance(args, dict) else {}})
    return calls


def tool_outputs(messages) -> list:
    """What the tools returned, in a target run's output messages."""
    return [c.get("tool_result") for c in _parts(messages) if c.get("type") == "tool_result"]


def _usage(usage) -> dict | None:
    u = _dict(usage)
    if not u:
        return None
    return {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"),
            "total_tokens": u.get("total_tokens")}


def judge_target(turns: list[dict], items: list[dict]) -> list[dict]:
    """Each turn with the verdict on the reply Foundry got from the agent, matched by case, and the deterministic
    checks of that reply."""
    by_turn: dict[str, dict] = {}
    for item in items:
        if case := _dict(item.get("datasource_item")).get("case"):
            by_turn.setdefault(case, item)
    out = []
    for turn in turns:
        item = by_turn.get(result_id(turn))
        source, sample = _dict((item or {}).get("datasource_item")), _dict((item or {}).get("sample"))
        sent, text = source.get("sample.output_items"), source.get("sample.output_text") or ""
        calls = tool_calls(sent)
        latency = sample.get("latency_ms") if isinstance(sample.get("latency_ms"), (int, float)) else None
        checks = None
        if item is not None and not error_text(sample.get("error")):
            found = score_turn(turn, {"text": text, "tool_calls": calls, "platform_abort": False,
                                      "abort_reason": None, "latency_ms": latency}, tool_outputs(sent))
            checks = {"passed": not found, "failures": [f.message for f in found], "reasons": [f.reason for f in found]}
        version = source.get("agent_version")
        out.append({"conversation_id": turn["conversation_id"], "turn_number": turn["turn_number"], "repetition": 1,
                    "category": turn["category"], **_judged(*verdict(item, "target")), "deterministic": checks,
                    "latency_ms": None if latency is None else round(latency, 1),
                    "response_id": source.get("response_id"), "trace_id": source.get("trace_id"),
                    "agent_version": None if version is None else str(version), "model": sample.get("model") or None,
                    "usage": _usage(sample.get("usage")), "tool_calls": calls, "response": text[:MAX_RESPONSE]})
    return out


def judge_usage(items: list[dict]) -> dict | None:
    """The evaluators' tokens, summed over every result."""
    used = [u for item in items for r in item.get("results") or []
            if (u := _dict(_dict(_dict(r).get("sample")).get("usage")))]
    if not used:
        return None
    return {"input_tokens": sum(u.get("prompt_tokens") or 0 for u in used),
            "output_tokens": sum(u.get("completion_tokens") or 0 for u in used),
            "total_tokens": sum(u.get("total_tokens") or 0 for u in used)}


def tokens_text(usage: dict) -> str:
    return (f"{usage.get('total_tokens') or 0:,} ({usage.get('input_tokens') or 0:,} input, "
            f"{usage.get('output_tokens') or 0:,} output)")


def _score_text(score: dict) -> str:
    outcome = score_outcome(score)
    if outcome == "not applicable":
        return "n/a"
    return "error" if outcome == "errored" else _number(score["score"])


def turn_line(r: dict, runs: int) -> str:
    """A turn's console line: its verdict, every evaluator's score and the deterministic result."""
    line = f"{LABELS[r['verdict']]} {result_id(r)} {r['category']}"
    if runs > 1:
        line += f" [run {r['repetition']}/{runs}]"
    if r["scores"]:
        line += ": " + ", ".join(f"{name} {_score_text(s)}" for name, s in r["scores"].items())
    if checks := r.get("deterministic"):
        why = ", ".join(dict.fromkeys(checks["reasons"]))
        line += f" (checks {'PASS' if checks['passed'] else 'FAIL'}{': ' + why if why else ''})"
    return line


def agreement_line(results: list[dict]) -> str | None:
    if not (a := agreement(results)):
        return None
    judge_only = ", ".join(a["judge_only_turns"]) or "none"
    checks_only = ", ".join(a["checks_only_turns"]) or "none"
    return (f"Judge vs. checks: agree on {a['agree']}/{a['results']}; judge failed only: {judge_only}; "
            f"checks failed only: {checks_only}")


def evaluator_list(text: str) -> tuple[str, ...]:
    names = [n.strip().removeprefix("builtin.") for n in text.split(",") if n.strip()]
    if not names or not all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names):
        raise argparse.ArgumentTypeError(f"expected evaluator names such as tool_use or task_adherence, not {text!r}")
    return tuple(dict.fromkeys(names))


def newest_agent_run() -> Path | None:
    runs = sorted(AGENT_RUNS.glob("run-*.json"))
    return runs[-1] if runs else None


def load_agent_run(path: Path) -> dict:
    """The agent-eval run at `path`. Raises ValueError if it can't be read or isn't one."""
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if not isinstance(run, dict) or "agent" not in run or not isinstance(run.get("results"), list):
        raise ValueError(f"{path} is not an agent-eval run (results/agent-evals/run-*.json)")
    return run


def _setup_error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Have evaluators in Foundry judge the agent's replies.")
    ap.add_argument("run", nargs="?", type=Path,
                    help="agent-eval run whose stored responses to judge (default: the newest in results/agent-evals)")
    ap.add_argument("--target", nargs="+", metavar="CASES",
                    help="instead, have Foundry call the agent with the first turn of each conversation in these "
                         "agent-eval JSONL files or glob patterns")
    ap.add_argument("--agent", help="with --target, the agent to call (default: $FOUNDRY_AGENT_NAME, else "
                                    "customer-assistant)")
    ap.add_argument("--agent-version", type=at_least_one, metavar="N",
                    help="with --target, pin the agent version that answers, such as 2 (default: "
                         "$FOUNDRY_AGENT_VERSION, else the agent's latest version)")
    ap.add_argument("--judge", help="model deployment the evaluators use (default: $FOUNDRY_EVAL_JUDGE_DEPLOYMENT, "
                                    "else gpt-4-1-mini)")
    ap.add_argument("--evaluators", type=evaluator_list, default=DEFAULT_EVALUATORS, metavar="NAMES",
                    help="comma-separated evaluators: this repo's (" + ", ".join(CUSTOM_EVALUATORS) + ") or built-in "
                         "ones such as task_adherence (default: " + ",".join(DEFAULT_EVALUATORS) + ")")
    ap.add_argument("--max-turns", type=at_least_one, metavar="N",
                    help="turns of each response's conversation the evaluators see, the judged turn included "
                         "(default: the run's longest conversation)")
    ap.add_argument("--timeout", type=at_least_one, default=30, metavar="MINUTES",
                    help="stop waiting for the evaluation run after this long (default 30)")
    ap.add_argument("--fail-on", choices=("any", "unjudged"), default="any",
                    help="the turns that make the run exit 1: any that didn't pass (default), or only those the "
                         "evaluators couldn't judge, so the judge's failures are reported without failing the run")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "cloud-evals")
    args = ap.parse_args(argv)
    if args.run and args.target:
        return _setup_error("give an agent run or --target cases, not both")
    for flag, value in (("--agent", args.agent), ("--agent-version", args.agent_version)):
        if value and not args.target:
            return _setup_error(f"{flag} goes with --target: a run's responses are judged as they were recorded")
    if args.max_turns and args.target:
        return _setup_error("--max-turns goes with an agent run: --target judges first turns only")

    agent_run, cases, path = None, None, None
    if args.target:
        try:
            cases = load_cases(args.target, "agent")
        except CaseError as e:
            print("invalid cases:\n  " + "\n  ".join(e.problems), file=sys.stderr)
            return 2
        if not cases:
            return _setup_error("no cases found")
    else:
        path = args.run or newest_agent_run()
        if path is None:
            return _setup_error(f"no runs in {AGENT_RUNS}: run python -m evals.run_agent_evals first, or give a run")
        try:
            agent_run = load_agent_run(path)
        except ValueError as e:
            return _setup_error(str(e))
        if not any(r.get("response_id") for r in agent_run["results"]):
            return _setup_error(f"{path} has no stored responses to judge")

    load_dotenv(ROOT / ".env")
    project = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not project:
        return _setup_error("set FOUNDRY_PROJECT_ENDPOINT in .env (see .env.example)")
    try:
        auth.get_token()
        if cases is not None:
            cases = fill_caller(cases, auth.caller_claims)
    except CaseError as e:
        print("invalid cases:\n  " + "\n  ".join(e.problems), file=sys.stderr)
        return 2
    except ClientAuthenticationError as e:
        return _setup_error(f"cannot get a token for {auth.FOUNDRY_SCOPE} (signed in with az login?): {e}")
    judge = args.judge or os.environ.get("FOUNDRY_EVAL_JUDGE_DEPLOYMENT") or DEFAULT_JUDGE

    if agent_run is not None:
        source, agent, turns = "responses", agent_run["agent"], agent_run["results"]
        max_turns = args.max_turns or max(int(r.get("turn_number") or 1) for r in turns)
        history = agent_run.get("history") or "replay"  # agent runs from before the choice replayed
        ids = list(dict.fromkeys(r["response_id"] for r in turns if r.get("response_id")))
        unstored = sum(not r.get("response_id") for r in turns)
        body = responses_run(f"responses of {path.name}", ids, max_turns)
        where = {"agent_run": path.name, "history": history, "max_turns": max_turns}
        print(f"Judging {len(ids)} stored responses of {path.name} (history {history}; the evaluators see up to "
              f"{max_turns} turns of each conversation)"
              + (f"; {unstored} turns have no stored response" if unstored else ""))
    else:
        source, agent = "target", args.agent or os.environ.get("FOUNDRY_AGENT_NAME") or DEFAULT_AGENT
        version = str(args.agent_version or "") or (os.environ.get("FOUNDRY_AGENT_VERSION") or "").strip() or None
        label = f"{agent} version {version}" if version else agent
        turns, left_out = first_turns(cases)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body = target_run(f"{label} first turns {stamp}", turns, agent, version)
        where = {"cases": [str(c) for c in args.target], "left_out": left_out}
        print(f"Foundry sends the first turn of {len(turns)} conversations to agent {label}"
              + (f"; later turns left out: {len(left_out)}" if left_out else ""))
    print(f"Evaluators: {', '.join(args.evaluators)} (judge {judge})")

    api = EvalsAPI(project)
    try:
        versions = {}
        for name in (n for n in args.evaluators if n in CUSTOM_EVALUATORS):
            versions[name], registered = api.catalog_version(custom_entry(name))
            print(f"  {name}: {CATALOG_PREFIX}{name} version {versions[name]} in the project's evaluator catalog"
                  + (" (registered now)" if registered else ""))
        definition = eval_definition(source, args.evaluators, judge, versions)
        eval_id, created = api.eval_id(definition)
        began = time.monotonic()
        run_id = api.start(eval_id, body)
        print(f"Evaluation {definition['name']} ({'created' if created else 'reused'}), run {run_id}:", flush=True)
        run = api.wait(eval_id, run_id, args.timeout * 60)
        duration_s = round(time.monotonic() - began)
        if run.get("status") not in ENDED:
            return _setup_error(f"the evaluation run is still {run.get('status')} after {args.timeout} minutes and "
                                f"goes on in Foundry (evaluation {eval_id}, run {run_id})")
        if run.get("status") != "completed":
            why = error_text(run.get("error")) or "no error given"
            return _setup_error(f"the evaluation run {run.get('status')} (evaluation {eval_id}, run {run_id}): {why}")
        items = api.output_items(eval_id, run_id)
        if source == "responses":
            results, ran = judge_responses(turns, items), agent_run.get("provenance")
        else:
            results = judge_target(turns, items)
            ran = run_provenance(results, project, agent)
    except (CloudEvalError, ClientAuthenticationError, OSError, HTTPException) as e:
        return _setup_error(str(e) if isinstance(e, CloudEvalError) else f"{type(e).__name__}: {e}")

    runs = max(r["repetition"] for r in results)
    for same_turn in by_case(results).values():  # a turn's runs together
        for r in same_turn:
            print(turn_line(r, runs))
            for message in r["failures"]:
                print(f"       - {message}")
    print("\n" + "\n".join(summary(results)))
    if line := agreement_line(results):
        print(line)
    if args.fail_on == "unjudged" and any(r["verdict"] == "fail" for r in results):
        print("The judge's failures don't fail the run (--fail-on unjudged).")
    used = judge_usage(items)
    if used:
        print(f"Judge tokens: {tokens_text(used)}")
    agent_usage = total_usage(results) if source == "target" else None
    if agent_usage:
        print(f"Agent tokens: {tokens_text(agent_usage)}")
    # Live CI masks only the account and project names; the portal link names more.
    if run.get("report_url") and os.environ.get("GITHUB_ACTIONS") != "true":
        print(f"Portal: {run['report_url']}")

    out = write_run(args.out, lambda stamp: {
        "kind": "cloud", "timestamp": stamp, "source": source, "agent_name": agent, **where, "judge": judge,
        "evaluators": list(args.evaluators), "evaluator_versions": versions, "eval_name": definition["name"],
        "eval_id": eval_id, "eval_run_id": run_id, "status": run.get("status"), "duration_s": duration_s,
        "result_counts": run.get("result_counts"), "judge_usage": used, "agent_usage": agent_usage,
        "provenance": ran, "report_url": run.get("report_url"), "results": results})
    print(f"results: {out}")
    if args.fail_on == "unjudged":
        return 1 if any(r["verdict"] in UNJUDGED for r in results) else 0
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
