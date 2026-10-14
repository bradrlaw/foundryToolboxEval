# Foundry Toolbox Eval

Evaluate how well a Microsoft Foundry **toolbox** works, at two levels:

1. **Tool tests** (unit-test style) - call the toolbox's tools directly over its MCP
   endpoint and assert on correctness, schema conformance, error handling, and latency.
2. **Agent evals** - run a Foundry agent connected to the toolbox against scenarios and check
   that it selects the right tools, passes the right arguments, and gives a good answer.

Terminology: *tests* call the toolbox directly; *evals* involve an agent.

This repo is independent of `copilot-studio-evals`; it reuses the idea of a canonical
JSONL format but defines its own schemas.

Docs: [getting started](docs/getting-started.md) (deploy and run the samples), [existing environment](docs/existing-environment.md) (your own Foundry project/toolbox/agents), [telemetry](docs/telemetry.md) (App Insights), [plan](docs/plan.md).

## Layout

| Path | Purpose |
| --- | --- |
| `tooltests/` | Tool-test runner: an MCP client for the toolbox endpoint |
| `evals/` | Deterministic agent-eval runner, the agent target ASSERT calls (`foundry_agent/target.py`), ASSERT configs (`configs/`), shared auth, case loading and telemetry |
| `schemas/` | JSON Schemas for tool-test and agent-eval JSONL |
| `data/tool-tests/`, `data/agent-evals/` | Tool-test cases and agent-eval scenarios (JSONL) |
| `config/` | Optional target config example; `*.local.json` is git-ignored |
| `scripts/` | `validate_eval_jsonl.py`, `report.py`, `run_assert.py` (ASSERT wrapper), `deploy_toolbox.py` (connection + toolbox), `deploy_agent.py` |
| `sample-mcp-server/` | Synthetic MCP server (`get_customer`, `get_market_conditions`, `get_stock_quote`) to deploy and test against; run locally with `.\.venv\Scripts\python sample-mcp-server\server.py` (`http://localhost:8000/mcp`) |
| `deploy/` | Notes and findings for the deployed sample toolbox |
| `docs/` | Setup guides, telemetry, plan |
| `tests/` | Offline unit tests |
| `results/` | Run output (git-ignored) |

## Status

Working: sample MCP server, tool-test runner, deterministic agent-eval runner, ASSERT behavior configs, reports, App Insights telemetry.

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
`--toolbox` is given. Case arguments accept globs (`"data\tool-tests\*.jsonl"`).
Case kinds: `call` (default), `list` (tools/list checks), `auth` (bad/missing token -> expected status).
Each call ends in one outcome (`ok`, `tool_error`, `protocol_error`, `invalid_result`, `transport_error`, `runner_error`);
`expect_error` (`true`/`"tool"` or `"protocol"`) sets the expected one. Assertion types: contains, regex, json_path_equals,
json_path_exists, schema; plus `max_latency_ms` and `repetitions`.

## Agent evals (deterministic)

```powershell
.\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\sample-toolbox.jsonl   # writes results\agent-evals\run-*.json
```

Calls the agent `FOUNDRY_AGENT_NAME` (default `customer-assistant`) through the Responses API and checks tool calls and
arguments, `must_not_call`, response patterns and latency. Turns sharing a `conversation_id` run in order as one conversation.
Each result records `tool_calls`, `response`, `latency_ms` (null when the call failed), `attempts` (HTTP tries, including
retries), `platform_abort`, and `history_gap`: an earlier turn got no answer (error or abort), so this turn's history
lacks that reply.

Both runners validate their cases first and exit 0 when everything passes, 1 when a case fails, and 2 on invalid cases or a
setup error (config, sign-in, connection). Validate case files without running them:

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

Early findings: the agent applied "manufacturing" market data to aerospace, and platform tool errors abort the agent request
(see `deploy/README.md`).

## Reports

```powershell
.\.venv\Scripts\python scripts\report.py tool  [run.json] [--baseline older.json]     # newest run by default
.\.venv\Scripts\python scripts\report.py agent [run.json] [--baseline older.json]
.\.venv\Scripts\python scripts\report.py assert results\assert\results\<suite>\<run> [--baseline <other run dir>] [--out report.md]
```

## Unit tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests   # offline: sample server, runners, scoring, validation
```
