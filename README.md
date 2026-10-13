# Foundry Toolbox Eval

Evaluate how well an Azure AI Foundry **toolbox** works, at two levels:

1. **Tool tests** (unit-test style) - call the toolbox's tools directly over its MCP
   endpoint and assert on correctness, schema conformance, error handling, and latency.
2. **Agent evals** - run a Foundry-connected agent against scenarios and evaluate that
   it selects the right tools, passes the right arguments, and gives a good answer.

Terminology: *tests* call the toolbox directly; *evals* involve an agent.

This repo is independent of `copilot-studio-evals`; it reuses the idea of a canonical
JSONL format but defines its own schemas.

Docs: [getting started](docs/getting-started.md) (deploy and run the samples), [existing environment](docs/existing-environment.md) (your own Foundry project/toolbox/agents), [telemetry](docs/telemetry.md) (App Insights), [plan](docs/plan.md).

## Layout

| Path | Purpose |
| --- | --- |
| `docs/` | Plan, architecture, decisions |
| `schemas/` | JSON Schemas for tool-test and agent-eval JSONL |
| `config/` | Example target config (endpoints, auth, agent ids); `*.local.json` is git-ignored |
| `data/tool-tests/` | Direct toolbox test cases (JSONL) |
| `data/agent-evals/` | Agent eval scenarios (JSONL) |
| `deploy/` | Notes and script for the deployed sample toolbox |
| `sample-mcp-server/` | Synthetic MCP server (`get_customer`, `get_market_conditions`, `get_stock_quote`) to deploy and test against; run with `python sample-mcp-server\server.py` (`http://localhost:8000/mcp`) |
| `tests/` | Unit tests (`python -m unittest discover tests`) |
| `scripts/` | Validators, runners, `deploy_toolbox.py` (connection + toolbox), `deploy_agent.py` |
| `results/` | Run output (git-ignored) |

## Status

Working: sample MCP server, tool-test runner, deterministic agent-eval runner, ASSERT behavior configs, reports.

## Setup (environment-specific values stay out of git)

All subscription/tenant/resource identifiers live in git-ignored files: `.env` (copy from `.env.example`) and
`config/*.local.json` (copy from `config/target.example.json`). Nothing in tracked files is specific to one tenant.

## Agent evals with ASSERT

Agent: `customer-assistant` (prompt agent, `gpt-4-1-mini`) in your Foundry project, using toolbox `sample-customer-toolbox` (MCP, no auth).
Evals use Microsoft ASSERT (`assert-ai`) with a callable target in `evals/foundry_agent/target.py`.

```powershell
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in your values
az login --tenant <tenant-id>
.\.venv\Scripts\assert-ai run --config evals\configs\ungrounded_figures.yaml
```

Notes: `artifacts_root` in each config must be an absolute path on your machine (git-ignored output in `results\assert`). The judge needs a large `max_tokens` (24000) for gpt-5.6-sol.
Early finding: the agent applied "manufacturing" market data to aerospace; platform tool errors abort the agent request (see `deploy/README.md`).

## Tool tests (direct toolbox calls)

```powershell
python -m tooltests.run data\tool-tests\sample-toolbox.jsonl   # needs az login; writes results\tool-tests\run-*.json
```


## Agent evals (deterministic)

```powershell
.\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\sample-toolbox.jsonl   # results\agent-evals\run-*.json
python -m unittest discover -s tests   # offline unit tests (sample tools, runner scoring)
```

Tool-test case kinds: `call` (default), `list` (tools/list checks), `auth` (bad/missing token -> expected status). Assertion types: contains, regex, json_path_equals, json_path_exists, schema.

## Reports

```powershell
python scripts\report.py tool  [run.json] [--baseline older.json]     # newest run by default
python scripts\report.py agent [run.json] [--baseline older.json]
python scripts\report.py assert results\assert\results\<suite>\<run> [--baseline <other run dir>] [--out report.md]
```r

ASSERT behavior configs (one atomic behavior each): `evals\configs\ungrounded_figures.yaml`, `evals\configs\tool_error_handling.yaml` (the second catches raw platform-failure replies and leaked internals).
