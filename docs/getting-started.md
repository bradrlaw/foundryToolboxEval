# Getting started: run the included samples

This walks through deploying the sample toolbox and agent in your own Azure subscription, then running every
test layer. If you already have a Foundry project, toolbox and agent, skip to
[existing-environment.md](existing-environment.md).

## 1. Prerequisites

| Need | Notes |
| --- | --- |
| Azure subscription | Permission to create resources (Contributor) in a resource group |
| Microsoft Foundry resource + project | Create in the Azure portal / ai.azure.com. Note the project endpoint: `https://<resource>.services.ai.azure.com/api/projects/<project>` |
| Chat model deployment in the project | For the agent, e.g. `gpt-4-1-mini` |
| Judge model deployment (ASSERT only) | Default config uses `gpt-5.6-sol`; any strong model works if you edit the YAML `azure/<deployment>` names. The deployment must be on the same resource as `AZURE_API_BASE` |
| Foundry roles | **Azure AI User** (or higher) on the project for the signed-in user; **Cognitive Services OpenAI User** on the resource for the judge |
| Tools | Python 3.11+, Azure CLI (`az`), Git. Docker is not required (`az acr build` builds in the cloud) |

```powershell
az login --tenant <tenant-id>
az account set --subscription <subscription-id>
```

## 2. Install

```powershell
git clone https://github.com/<you>/foundryToolboxEval; cd foundryToolboxEval
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env                       # fill in FOUNDRY_PROJECT_ENDPOINT, AZURE_API_BASE
copy config\target.example.json config\sample-toolbox.local.json   # fill in (step 5)
python -m unittest discover tests            # offline sanity check, no Azure needed
```

`.env` and `config\*.local.json` are git-ignored; keep tenant-specific values there only.

## 3. Deploy the sample MCP server

The server (`sample-mcp-server\`) exposes `get_customer`, `get_market_conditions`, `get_stock_quote` over synthetic data.
You can first try it locally: `python sample-mcp-server\server.py` serves `http://localhost:8000/mcp`.

```powershell
$env:PYTHONIOENCODING = "utf-8"    # avoids az acr build log crash on Windows
az group create -n rg-toolbox-eval-sample -l <region>
az acr create -n <uniqueacrname> -g rg-toolbox-eval-sample --sku Basic --admin-enabled true
az acr build -r <uniqueacrname> -t sample-mcp:v1 sample-mcp-server
az containerapp env create -n sample-env -g rg-toolbox-eval-sample -l <region>
az containerapp create -n sample-mcp -g rg-toolbox-eval-sample --environment sample-env `
  --image <uniqueacrname>.azurecr.io/sample-mcp:v1 --registry-server <uniqueacrname>.azurecr.io `
  --target-port 8000 --ingress external
```

The MCP URL is `https://<app-fqdn>/mcp`. It is unauthenticated, so use only synthetic data.

## 4. Create the connection and toolbox

Scripted (idempotent; each run publishes a new toolbox version and makes it the default):

```powershell
.\.venv\Scripts\python scripts\deploy_toolbox.py --mcp-url https://<app-fqdn>/mcp --resource-group <foundry-resource-group>
```

It creates the project connection `sample-mcp-conn` (category `RemoteTool`, auth none) and the toolbox
`sample-customer-toolbox` (server label `sample`). The account and project come from `FOUNDRY_PROJECT_ENDPOINT` in `.env`;
`--connection`, `--server-label`, `--toolbox` and `--subscription` override defaults, and `--delete` removes both.
Needs Contributor on the Foundry resource and Azure AI User on the project. The portal (project → **Tools**) works too.

The toolbox consumer endpoint is
`<project endpoint>/toolboxes/sample-customer-toolbox/mcp?api-version=v1`. Tools appear as
`sample___get_customer` (three underscores). Bearer token scope: `https://ai.azure.com/.default`.

## 5. Create the agent and configure the runners

```powershell
.\.venv\Scripts\python scripts\deploy_agent.py --model <your-chat-deployment>
```

Edit `config\sample-toolbox.local.json`:

```json
{ "toolbox": { "mcp_endpoint": "<project endpoint>/toolboxes/sample-customer-toolbox/mcp?api-version=v1",
               "token_scope": "https://ai.azure.com/.default" },
  "agent":   { "project_endpoint": "<project endpoint>" } }
```

## 6. Run

```powershell
# Tool tests: call the toolbox directly (schema, errors, auth, latency)
.\.venv\Scripts\python -m tooltests.run data\tool-tests\sample-toolbox.jsonl

# Agent evals: deterministic checks of tool selection, arguments and answer content
.\.venv\Scripts\python -m evals.run_agent_evals data\agent-evals\sample-toolbox.jsonl

# ASSERT behavior evals (LLM-judged). First edit artifacts_root in the YAML to an absolute local path
.\.venv\Scripts\assert-ai run --config evals\configs\ungrounded_figures.yaml
.\.venv\Scripts\assert-ai run --config evals\configs\tool_error_handling.yaml

# Reports (markdown); add --baseline <older run> to diff
python scripts\report.py tool
python scripts\report.py agent
python scripts\report.py assert results\assert\results\<suite>\<run>
```

Expected for the samples: tool tests 10/10 and agent evals 9/9. ASSERT results are not fixed; the first baseline found real
violations (e.g. manufacturing data applied to aerospace).

Add telemetry with [telemetry.md](telemetry.md).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| 401 from toolbox | `az login` to the right tenant; the token scope must be `https://ai.azure.com/.default` |
| `FOUNDRY_PROJECT_ENDPOINT` missing | Set it in `.env` |
| Agent run returns HTTP 400 `An error occurred invoking 'sample___...'` | Platform behavior: any tool error aborts the whole agent request. The runners record it as `[AGENT FAILURE]` / `platform_abort` |
| Intermittent DNS / connection errors | Transient; the runners retry. Rerun failed ASSERT stages, completed stages are cached |
| ASSERT writes into site-packages | `artifacts_root` must be absolute |
| ASSERT judge output truncated | Keep `max_tokens: 24000` for the judge |
| `pip` conflicts with openai | Don't install `azure-ai-projects` in this venv |
| `az acr build` crashes printing logs | `$env:PYTHONIOENCODING="utf-8"`; check `az acr task list-runs` — the build usually succeeded |
