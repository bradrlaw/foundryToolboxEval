# Plan

## Goals

- Measure tool behavior from the toolbox: correctness, schema conformance, errors, latency, reliability.
- Measure agent behavior when using the toolbox: tool selection, argument quality, multi-step flows, final answer quality, negative cases (tools that must not be called).
- Produce repeatable, comparable runs (baseline vs. regression).

## Evaluation layers

### 1. Tool tests (direct, unit-test style)
Connect to the toolbox endpoint, `tools/list`, then `tools/call` per case.
Checks: tool is listed, input schema valid, result matches assertions (JSON path / regex / contains),
error cases return structured errors, latency percentiles (p50/p95) over N repetitions, auth failure behavior.

### 2. Agent evals (end to end)
Run a Foundry agent with the toolbox attached against multi-turn scenarios.
Capture: tool calls (name, args, result, order), final response, latency, token usage.
Checks: expected tool calls (+ param matching), `must_not_call`, response regex / LLM judge, pass criteria.

### Status
- Tool tests: runner done (`python -m tooltests.run data/tool-tests/sample-toolbox.jsonl`; results in `results/tool-tests/`).
- Agent evals: Microsoft ASSERT (`assert-ai`) drives behavior/policy evals against the `customer-assistant` agent
  (`evals/foundry_agent/target.py`, configs in `evals/configs/`; one atomic behavior per config). Exact tool-call checks run via `python -m evals.run_agent_evals data/agent-evals/sample-toolbox.jsonl` (expected calls + params, ordered, `must_not_call`, response regex, latency; multi-turn by `conversation_id`). ASSERT does not assert specific call parameters, so the two complement each other.

## Data format (JSONL, one case per line)

- Tool tests: `schemas/tool-test.schema.json` (`data/tool-tests/`)
- Agent-level: `schemas/agent-eval.schema.json` (adapted from the copilot-studio-evals canonical turn: `conversation_id`, `turn_number`, `user_message`, `expected_tool_calls`, `expected_response_pattern`, `pass_criteria`, `must_not_call`, `category`)

## Milestones

1. Scaffold + schemas + validator (`scripts/validate_eval_jsonl.py`)
2. Tool-test runner (MCP client, latency stats, JSON results)
3. Agent-level runner (Foundry Agent Service SDK, capture tool calls from run steps)
4. Scoring + report (pass rate by category, latency, diff vs. baseline)
5. Optional: LLM-judge, Application Insights / tracing correlation, CI workflow

## Open questions

- Which toolbox/tools are under test, and what is the endpoint/auth (Entra ID, managed identity, key)?
- Language/SDK: Python assumed (matches copilot-studio-evals).
- Foundry agent type: Agent Service (azure-ai-projects) vs. other?
- Need LLM-judge scoring, or deterministic checks only for v1?
- Where do results go (local files only vs. App Insights / Foundry evals)?
