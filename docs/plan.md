# Plan

How the implementation works is in [architecture.md](architecture.md).

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
Capture: tool calls (name, arguments, order), final response, latency, retry attempts, platform aborts.
Checks: expected tool calls (+ param matching), `must_not_call`, response regex / LLM judge, pass criteria.

### Status
- Tool tests: done. `python -m tooltests.run data/tool-tests/sample-toolbox.jsonl` uses an MCP client (mcp 2.x) against the
  toolbox endpoint; each call ends in an outcome (`ok`, `tool_error`, `protocol_error`, ...) compared with `expect_error`.
- Agent evals: Microsoft ASSERT (`assert-ai`) drives behavior/policy evals against the `customer-assistant` agent
  (`evals/foundry_agent/target.py`, configs in `evals/configs/`, run with `scripts/run_assert.py`; one atomic behavior per
  config). Exact tool-call checks run via `python -m evals.run_agent_evals data/agent-evals/sample-toolbox.jsonl` (expected
  calls + params, ordered, `must_not_call`, response regex, latency; multi-turn by `conversation_id`). ASSERT does not
  assert specific call parameters, so the two complement each other.
- Reports (`scripts/report.py`: baseline diffs, failures grouped by reason, ASSERT violations with the judge's
  justifications) and App Insights telemetry (`docs/telemetry.md`): done. Every failure carries a reason code
  (`evals/reasons.py`) in the result files and spans.
- CI: done. `.github/workflows/ci.yml` runs the offline checks (install, compile, case validation, unit tests) on Ubuntu
  with Python 3.11 and 3.14 and on Windows with 3.14; no Azure access.
- Per-user Entra identity: done ([entra.md](entra.md)). An Entra-protected sample server (`AUTH_MODE=entra`, region
  filtering by app role) behind an OAuth2 identity-passthrough connection; `{{caller.*}}` placeholders; the tool-test
  outcomes `consent_required` and `upstream_error`; and agent abort reasons for turns the agent never ran. `UserEntraToken`
  doesn't work for custom MCP servers: Foundry's token exchange rejects custom audiences.

## Data format (JSONL, one case per line)

- Tool tests: `schemas/tool-test.schema.json` (`data/tool-tests/`)
- Agent-level: `schemas/agent-eval.schema.json` (adapted from the copilot-studio-evals canonical turn: `conversation_id`, `turn_number`, `user_message`, `expected_tool_calls`, `expected_response_pattern`, `pass_criteria`, `must_not_call`, `category`)
- Both are validated by `evals/cases.py` (JSON Schema plus cross-field rules) on load and by `scripts/validate_eval_jsonl.py`.

## Milestones

1. Scaffold + schemas + validator - done
2. Tool-test runner (MCP client, latency stats, JSON results) - done
3. Agent-level runner (Responses API with `agent_reference`; tool calls read from `mcp_call` output items) - done
4. Scoring + report (pass rate by category, latency, diff vs. baseline) - done
5. LLM judge (ASSERT), Application Insights tracing and CI workflow - done

## Decisions

- Target: a Foundry toolbox (MCP endpoint `<project>/toolboxes/<name>/mcp?api-version=v1`, Entra token for
  `https://ai.azure.com/.default`) and a Foundry prompt agent invoked through the Responses API.
- Python, no Foundry SDK: `azure-ai-projects` conflicts with assert-ai's `openai` pin, so the agent target calls REST directly.
- Deterministic checks plus ASSERT as the LLM judge.
- Results are local JSON files (`results/`); spans optionally go to Application Insights.
- Per-user identity uses OAuth2 identity-passthrough connections. Runs use the `az login` user, cases name no real user
  (`{{caller.*}}` placeholders), and consent links are printed to the console only, because they act like bearer secrets.

## Next

- Read MCP `structuredContent` and validate it against each tool's `outputSchema`; return typed results from the sample tools.
- Agent scoring: penalize extra calls, pass@k over repetitions.
- Multi-turn fidelity (`previous_response_id` or Foundry conversations instead of text replay) and per-run provenance
  (agent version, model, toolbox version, token usage).
- Live runs in CI (an OIDC federated credential and a test project); Foundry cloud evaluation as a server-side
  alternative.
- Identity: several test identities in one run (a role matrix, each case tagged with the identity it expects); hosted
  agents, which forward `x-agent-foundry-call-id` to the toolbox; recheck `UserEntraToken` for custom audiences; confirm
  consent survives the connection's refresh-token expiry.
