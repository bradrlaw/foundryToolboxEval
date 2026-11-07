# Foundry Toolbox Eval

Evaluate how well a Microsoft Foundry **toolbox** works, at two levels:

1. **Tool tests** (unit-test style) - call the toolbox's tools directly over its MCP
   endpoint and assert on correctness, schema conformance, error handling, and latency.
2. **Agent evals** - run a Foundry agent connected to the toolbox against scenarios and check
   that it selects the right tools, passes the right arguments, and gives a good answer.

Terminology: *tests* call the toolbox directly; *evals* involve an agent.

This repo is independent of `copilot-studio-evals`; it reuses the idea of a canonical
JSONL format but defines its own schemas.

Docs: [getting started](docs/getting-started.md) (deploy and run the samples), [existing environment](docs/existing-environment.md) (your own Foundry project/toolbox/agents), [Entra identity](docs/entra.md) (per-user identity passed through to an Entra-protected MCP server), [architecture](docs/architecture.md) (components, flows and diagrams), [telemetry](docs/telemetry.md) (App Insights), [live CI](docs/live-ci.md) (daily live runs in GitHub Actions), [plan](docs/plan.md).

## Layout

| Path | Purpose |
| --- | --- |
| `tooltests/` | Tool-test runner: an MCP client for the toolbox endpoint |
| `evals/` | Deterministic agent-eval runner, the agent target ASSERT calls (`foundry_agent/target.py`), ASSERT configs (`configs/`), shared auth, case loading and telemetry |
| `schemas/` | JSON Schemas for tool-test and agent-eval JSONL |
| `data/tool-tests/`, `data/agent-evals/` | Tool-test cases and agent-eval scenarios (JSONL) |
| `config/` | Optional target config example; `*.local.json` is git-ignored |
| `scripts/` | `validate_eval_jsonl.py`, `report.py`, `run_assert.py` (ASSERT wrapper), `deploy_toolbox.py` (connection + toolbox, no auth or OAuth2 identity passthrough), `deploy_agent.py`, `setup_entra.py` (Entra apps for the Entra sample) |
| `sample-mcp-server/` | Synthetic MCP server (`get_customer`, `get_market_conditions`, `get_stock_quote`) to deploy and test against; run locally with `.\.venv\Scripts\python sample-mcp-server\server.py` (`http://localhost:8000/mcp`). With `AUTH_MODE=entra` it accepts only Entra user tokens, filters customers by the caller's app roles and adds `whoami` |
| `deploy/` | Notes and findings for the deployed sample toolbox |
| `docs/` | Setup guides, architecture, telemetry, plan |
| `tests/` | Offline unit tests |
| `.github/workflows/` | CI: the offline checks on Ubuntu and Windows (`ci.yml`), and daily live runs (`live.yml`) |
| `results/` | Run output (git-ignored) |

## Status

Working: sample MCP server, tool-test runner, deterministic agent-eval runner, ASSERT behavior configs, reports that show
why cases failed, App Insights telemetry, offline CI, daily live runs in CI ([docs/live-ci.md](docs/live-ci.md)), and
per-user Entra identity through OAuth2 identity passthrough ([docs/entra.md](docs/entra.md)).

## Setup

```powershell
python -m venv .venv                              # Python 3.11+
.\.venv\Scripts\pip install -r requirements.txt   # pinned deps, plus this repo in editable mode
copy .env.example .env                            # then fill in your values
az login --tenant <tenant-id>
```

Environment-specific values stay out of git. All subscription, tenant and resource identifiers live in git-ignored files:
`.env` and, optionally, `config/*.local.json` (copy from `config/target.example.json`). Nothing in tracked files is specific
to one tenant. Use the venv's python for every command below.

## Tool tests (direct toolbox calls)

```powershell
.\.venv\Scripts\python -m tooltests.run data\tool-tests\sample-toolbox.jsonl   # writes results\tool-tests\run-*.json
```

The endpoint is `<FOUNDRY_PROJECT_ENDPOINT>/toolboxes/<FOUNDRY_TOOLBOX_NAME>/mcp` unless `--endpoint`, `--config` or
`--toolbox` is given. Case arguments accept globs (`"data\tool-tests\*.jsonl"`). Each case's result prints as soon as it
finishes; a request that gets no answer fails after `--timeout` seconds (default 60).
Case kinds: `call` (default), `list` (tools/list checks: `expect_tools`, `require_schemas`, `require_output_schemas`),
`auth` (bad/missing token -> expected status).
Each call ends in one outcome (`ok`, `tool_error`, `protocol_error`, `consent_required`, `upstream_error`, `invalid_result`,
`transport_error`, `runner_error`); `expect_error` (`true`/`"tool"` or `"protocol"`) sets the expected one. Assertion types:
contains, regex, json_path_equals, json_path_exists, schema; plus `max_latency_ms` and `repetitions`.
`invalid_result` means the result broke the MCP result format or the tool's `outputSchema` (the MCP SDK validates
`structuredContent`), or its JSON text disagrees with its `structuredContent`.
`consent_required` (the signed-in user hasn't consented to a tool source's OAuth connection) and `upstream_error` (the
toolbox couldn't reach or authenticate to a tool source) always fail; consent links go to the console only.
Failure reasons: the unexpected outcome (for example `tool_error`), `unexpected_success`, `assertion`, `latency`, a listing
problem (`missing_tool`, `duplicate_tool`, `tool_definition`), `auth_status` or `not_run`.

Cases can check the caller's identity with `{{caller.oid}}`, `{{caller.tid}}`, `{{caller.name}}` and
`{{caller.username}}`, filled from the signed-in user's token (both runners; see [docs/entra.md](docs/entra.md)).

## Agent evals (deterministic)

```powershell
.\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\sample-toolbox.jsonl   # writes results\agent-evals\run-*.json
.\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\sample-toolbox.jsonl --repetitions 3
```

Calls the agent `--agent`, else `FOUNDRY_AGENT_NAME` (default `customer-assistant`), through the Responses API and checks
tool calls and arguments, `must_not_call`, `max_tool_calls` (the most calls a turn may make, expected or not), response
patterns and latency. Turns sharing a `conversation_id` run in order as one conversation.
`--repetitions N` runs every conversation N times, each from a fresh history, to show how consistent the agent is. A turn
then passes only if every run passes, and the console adds pass@1 (the mean pass rate), pass@N (passed at least once),
pass^N (passed every run) and the flaky turns.
Each result records its `repetition`, `extra_calls` (calls beyond those the turn expects; null when the turn doesn't check
its calls or none are known), `tool_calls`, `response`, `latency_ms` (the final HTTP try; null when the call failed), `elapsed_ms`
(the whole request, including retries and their waits), `attempts` (HTTP tries, including retries), what served the turn
(`response_id`, `agent_version`, `model` and token `usage`), `platform_abort` with its `abort_reason`, and `history_gap`:
an earlier turn got no answer (error or abort), so this turn's history lacks that reply. Abort reasons: `tool_error` (the platform ended the request because a tool returned
an error) and `tool_call_failed` (because the toolbox couldn't make a call, as when the MCP server is starting from
zero); both fail the turn as `platform_abort`, and its tool calls are still checked. `consent_required` or
`tool_listing_failed`: the agent never ran, so the turn fails.
Failure reasons: `missing_call`, `wrong_arguments`, `wrong_order`, `forbidden_call`, `extra_calls`, `response_mismatch`,
`forbidden_response`, `latency`, `platform_abort` and `runner_error` (no reply), or the abort reason of a turn the agent
never ran. Unordered expected calls are paired with distinct calls, and non-string params compare as JSON (`true` is not
`1`); see [architecture.md](docs/architecture.md#scoring-a-turn).

Both runners validate their cases first and exit 0 when everything passes, 1 when a case fails, and 2 on invalid cases or a
setup error (a missing endpoint or config, or no `az login`). The tool runner also exits 2 when its first connection
fails, including a tool source that needs consent; the agent runner fails the affected turns instead. Each result keeps
`failures` (messages) and a parallel `reasons` list
with one code per message (all codes are described in `evals\reasons.py`), and the console ends with the number of failed
cases per reason. Each run file also records its `provenance`: the commit (and whether the working tree had changes), the
toolbox's default version and, for agent runs, the agent version and model; agent runs add the run's total token `usage`.
Versions are read from `FOUNDRY_PROJECT_ENDPOINT` only and are null when they can't be read. Validate case files without
running them:

```powershell
.\.venv\Scripts\python scripts\validate_eval_jsonl.py "data\tool-tests\*.jsonl" "data\agent-evals\*.jsonl"
```

## Agent evals with ASSERT

Microsoft ASSERT (`assert-ai`) judges agent behaviors with an LLM. Its callable target is `evals/foundry_agent/target.py`.
Run configs through the wrapper, which keeps ASSERT's artifacts in `results\assert` (git-ignored) and loads `.env`:

```powershell
.\.venv\Scripts\python scripts\run_assert.py evals\configs\ungrounded_figures.yaml   # extra args go to assert-ai run
```

Configs (one atomic behavior each): `evals\configs\ungrounded_figures.yaml`, and `evals\configs\tool_error_handling.yaml`,
which catches raw platform-failure replies and leaked internals. The judge needs a large `max_tokens` (24000) for gpt-5.6-sol.
To judge another agent, set `$env:FOUNDRY_AGENT_NAME` first. A turn the agent never ran (missing consent, or its tools
couldn't be listed) is recorded as a target error and not scored.

Findings so far ([deploy/README.md](deploy/README.md#assert-findings)): when a tool returns no data and the user insists,
the agent often supplies an estimate of its own (`figure_grounding` violated in 15 of 36 cases), while it handles tool
errors well (`graceful_error` violated in 2 of 18). Tool errors, which aborted the agent request through October 2026,
now reach the model as text.

## Reports

```powershell
.\.venv\Scripts\python scripts\report.py tool  [run.json] [--baseline older.json]     # newest run by default
.\.venv\Scripts\python scripts\report.py agent [run.json] [--baseline older.json]
.\.venv\Scripts\python scripts\report.py assert results\assert\results\<suite>\<run> [--baseline <other run dir>] [--out report.md]
```

Tool and agent reports show what the run ran against (agent, model, toolbox versions and commit), the agent run's tokens,
pass rates by category, latency, platform aborts, a *Why cases failed* table (failed cases per reason) and each failure
grouped by reason; regressions against a baseline list their reasons, and the report lists what changed since the
baseline (for example ``agent `customer-assistant` v2 -> v3``). Runs recorded before reasons existed show `unrecorded`.
Agent reports count the tool calls beyond those expected. For a run with `--repetitions`, they count a turn as passed only
if every run passed, in the totals, categories and regressions alike; they add pass@1, pass@k, pass^k and the flaky turns,
and list a failure that recurs over a turn's runs once, for example `[2/3 runs]`. The ASSERT report adds each violation with the judge's justification per dimension, and
the error for each case it couldn't score (for example the Responses API's error code and message).

## Unit tests and CI

```powershell
.\.venv\Scripts\python -m unittest discover -s tests   # offline: sample server, runners, scoring, validation
```

GitHub Actions (`.github\workflows\ci.yml`) runs the offline checks on pushes to `main`, pull requests and manual runs:
dependency install and `pip check`, a byte-compile of every source file, case validation and the unit tests, on Ubuntu
with Python 3.11 and 3.14 and on Windows with 3.14. Changes that touch only docs or Markdown skip it. `ci.yml` has no
Azure credentials and calls nothing live. Dependabot (`.github\dependabot.yml`) opens weekly PRs for the pinned Python
packages (grouping minor and patch bumps), the sample server's base image and the workflows' actions; CI checks each one.

`.github\workflows\live.yml` runs the sample tool tests and agent evals against the deployed toolbox and agent every day
at 12:00 UTC and on demand, signed in as a managed identity through OIDC, and puts both reports in the job summary.
Setup and limits: [docs/live-ci.md](docs/live-ci.md).
