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
  assert specific call parameters, so the two complement each other. Both ASSERT behaviors were scored in full in
  November 2026; those runs are the baselines ([findings](../deploy/README.md#assert-findings)).
- Agent grounding: instructions done. Grounding rules in the agent's instructions (v4) cut the `figure_grounding`
  violations from 15 to 9 of 36 and the `tool_error_handling` policy violations from 3 to 1 of 18, and the deterministic
  agent evals still pass. Each violation left follows a direct user instruction that the rules forbid
  ([findings](../deploy/README.md#assert-findings)).
- Reports (`scripts/report.py`: baseline diffs, failures grouped by reason, ASSERT violations with the judge's
  justifications) and App Insights telemetry (`docs/telemetry.md`): done. Every failure carries a reason code
  (`evals/reasons.py`) in the result files and spans. Traces run from the eval turn to the toolbox's requests, for open
  and OAuth identity-passthrough toolboxes alike.
- CI: done. `.github/workflows/ci.yml` runs the offline checks (install, compile, case validation, unit tests) on Ubuntu
  with Python 3.11 and 3.14 and on Windows with 3.14; no Azure access. `live.yml` runs the sample tool tests and agent
  evals daily against the deployed toolbox and agent, as a managed identity signed in through OIDC, with the reports in
  the job summary ([live-ci.md](live-ci.md)).
- Per-user Entra identity: done ([entra.md](entra.md)). An Entra-protected sample server (`AUTH_MODE=entra`, region
  filtering by app role) behind an OAuth2 identity-passthrough connection; `{{caller.*}}` placeholders; the tool-test
  outcomes `consent_required` and `upstream_error`; and agent abort reasons for turns the agent never ran. `UserEntraToken`
  doesn't work for custom MCP servers: Foundry's token exchange rejects custom audiences.
- Provenance: done. Tool and agent run files record the commit, each toolbox's default version and, for agent runs, the
  agent version and model; agent turns record their response id, agent version, model, token usage and `elapsed_ms` (the
  whole request, retries included). Reports show these and what changed since the baseline, and agent spans carry the
  agent version and tokens. ASSERT runs record theirs in the run's `provenance.json` (`scripts/run_assert.py`), with the
  number of agent calls and how many continued a stored response.
- Multi-turn fidelity: done. A later turn continues the previous turn's stored response (`previous_response_id`), in the
  deterministic runner and the ASSERT target alike, so the agent keeps its earlier tool results; `--history replay`
  resends the text as before. In the sample suite's `c-recall`, the chained agent quoted a contract value from its
  earlier lookup in 4 of 4 runs; the replayed one lacked it and made one up in 2 of 4.
- Foundry cloud evaluation: done. `python -m evals.run_cloud_eval` has Foundry's built-in evaluators (task adherence,
  intent resolution and tool-call accuracy by default) judge an agent run's stored responses, chained history included,
  or call the agent with each conversation's first turn (`--target`). Each verdict sits beside the same reply's
  deterministic result, and `report.py cloud` shows where they agree. On the sample suite they agreed on 9 of 12 turns;
  every disagreement was a correct turn the judge failed: tool errors on `tool_call_accuracy`, and a figure recalled from
  an earlier lookup on `task_adherence`. Replayed history now sends earlier replies as `output_text`, which the
  evaluators require.
- Agent scoring: done. `--repetitions N` reruns every conversation from a fresh history; a turn passes only if every
  run passes, and the console and reports add pass@1, pass@N, pass^N and the flaky turns. `max_tool_calls` fails a turn
  that makes too many calls, and every turn that checks its calls records `extra_calls`, the calls beyond those expected.
- Typed tool outputs: done. The sample tools publish an `outputSchema` for each tool (Pydantic models; in Entra mode, an
  inline `anyOf` of the record and the denial) and return `structuredContent` plus the same JSON as text. The tool runner
  fails a call whose JSON text and structured content disagree (`invalid_result`; the MCP SDK already checks the
  `outputSchema`), and list cases can require output schemas (`require_output_schemas`).
- Hosting: the sample MCP servers keep one replica (`--min-replicas 1`). Scaling to zero was tried and reverted: the
  toolbox fails agent requests while a server starts, which the target reports as `tool_listing_failed` or
  `tool_call_failed`, and one cold start left a replica stuck ([findings](../deploy/README.md#findings)).

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
- Deterministic checks plus ASSERT as the LLM judge; Foundry's built-in evaluators as a server-side second opinion,
  reported beside the checks rather than replacing them.
- Results are local JSON files (`results/`); spans optionally go to Application Insights.
- Per-user identity uses OAuth2 identity-passthrough connections. Runs use the `az login` user, cases name no real user
  (`{{caller.*}}` placeholders), and consent links are printed to the console only, because they act like bearer secrets.

## Next

- Grounding beyond prompting: rerun both ASSERT behaviors on a fresh test set, since v4's rules were written against the
  current one, then try a stronger agent model or a check of each figure in a reply against the tool results.
- Cloud evaluation follow-ups: a custom evaluator that accepts a correct tool error and grounds a recalled figure in the
  earlier turns' tool results, so the judge stops failing those turns; then a cloud run in live CI after the agent evals.
  The CI identity holds **Foundry User** on the project only; Learn says an evaluation that calls a model deployment
  needs it on the resource.
- Identity: several test identities in one run (a role matrix, each case tagged with the identity it expects); the Entra
  suites in live CI, which need a test user who has consented; hosted agents, which forward `x-agent-foundry-call-id` to
  the toolbox; recheck `UserEntraToken` for custom audiences; confirm consent survives the connection's refresh-token
  expiry.
