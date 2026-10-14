# Sample toolbox deployment

Real values for this deployment are kept in the git-ignored `deploy/resources.local.md`.

| Item | Value |
| --- | --- |
| Foundry resource / project | `<foundry-resource>` (rg `<foundry-rg>`) / `<project>` |
| MCP host | Container App `sample-mcp` in `<sample-rg>` (`<region>`), ACR `<acr-name>` |
| Project connection | `sample-mcp-conn` (category `RemoteTool`, auth `None`) |
| Toolbox | `sample-customer-toolbox` (v1, default) |
| Consumer endpoint | `{project_endpoint}/toolboxes/sample-customer-toolbox/mcp?api-version=v1` |

The host is unauthenticated (synthetic data only). Calls to the toolbox need a bearer token for `https://ai.azure.com/.default`
(`az account get-access-token --resource https://ai.azure.com`).

## Findings

- Toolbox tool names use `{server_label}___{tool}` (three underscores), e.g. `sample___get_customer`, not the dotted form shown in the docs.
- An unknown name without a server prefix returns a JSON-RPC error (-32602 `No tool config matches tool name ...`), not a tool
  result. An unknown name with a valid prefix (`sample___nope`) is passed to the MCP server and comes back as `isError`.
- Tool errors from the MCP server surface as `isError: true` with the original message, and so do missing or wrong-type
  arguments (with the validation message).

- **A tool error aborts the whole agent request.** When an MCP tool returns `isError: true` while an agent is calling it through the toolbox,
  the Responses API returns HTTP 400 (`An error occurred invoking 'sample___...'`) instead of giving the error to the model.
  The agent cannot say "that customer was not found"; callers must handle the 400. Direct toolbox calls are unaffected (tool tests see `isError`).
  The ASSERT target reports this as an `[AGENT FAILURE ...]` reply so it is scored.
- Responses API calls can hit transient DNS errors; the target retries connection errors, 408, 429 and 5xx (honoring `Retry-After`).

## Agent

Prompt agent `customer-assistant` (model `gpt-4-1-mini`) with the toolbox endpoint as an MCP tool (`require_approval: never`).
Create a new version with `.\.venv\Scripts\python scripts\deploy_agent.py [--model <deployment>] [--dry-run]`.
Invoke: `POST {project}/openai/v1/responses` with `agent_reference`.

## Redeploy

Recreate the connection and toolbox (new toolbox version, promoted to default; `--dry-run` prints the requests):

```powershell
.\.venv\Scripts\python scripts\deploy_toolbox.py --mcp-url https://<app-fqdn>/mcp --resource-group <foundry-rg>
```

Rebuild the MCP server (mcp 2.x `MCPServer`) under a new tag; keep the previous tag for rollback:

```powershell
az acr build -r <acr-name> -t sample-mcp:<new-tag> sample-mcp-server
az containerapp update -n sample-mcp -g <sample-rg> --image <acr-name>.azurecr.io/sample-mcp:<new-tag>
```

If `az acr build` crashes printing logs (Windows codepage), set `$env:PYTHONIOENCODING="utf-8"`; the build still completes.
